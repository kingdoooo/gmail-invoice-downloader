---
name: gmail-invoice-downloader
display_name: Gmail 发票下载器
description: "Search Gmail for invoices, receipts, hotel folios, and billing documents, then download them as PDFs. Use when: (1) downloading invoices/receipts for expense reimbursement, (2) batch-collecting billing documents from Gmail, (3) the user mentions 发票/invoice/水单/报销/收据. Handles direct PDF attachments, ZIP attachments (extract PDF, skip OFD), 9+ Chinese invoice platforms (百望云 3 templates, 诺诺网, fapiao.com, xforceplus, 云票, 百旺金穗云, 金财数科, 克如云), 12306 payment notification handling, and hotel folio↔invoice pairing by same-day rule. **Extensible to unknown platforms**: ships with `scripts/probe-platform.py` + a reverse engineering playbook in `references/platforms.md` — when a new invoice platform appears in MANUAL emails, run probe, follow the 5-step playbook to add support in <30 min."
icon: "🧾"
---

# Gmail Invoice Downloader

搜索 Gmail 中用户指定日期范围内的发票/收据/水单/行程单，下载为 PDF，并按"同日配对"规则生成酒店水单↔住宿发票对照报告。

## Quick Start

```bash
python3 scripts/download-invoices.py \
    --start 2026/01/01 \
    --end 2026/05/01 \
    --output ~/invoices/2026-Q1
```

一条命令完成：搜索 → 分类 → 下载 → 校验 → 配对 → 生成 `下载报告.md`。典型 60 封邮件跑完约 50 秒。

## Prerequisites

### 必备
- Gmail API OAuth2 credentials + token：
  - `~/.openclaw/credentials/gmail/credentials.json`（Desktop app 类型 OAuth client）
  - `~/.openclaw/credentials/gmail/token.json`（scope: `gmail.readonly`）
- Python 3.10+（标准库即可）
- `curl`（链接型下载，会跟随重定向）
- `pdftotext`（poppler-utils 包，用于发票类别识别 `*住宿服务*` vs `*餐饮服务*`）

### 选装（遇到新平台时用）
- **Chromium + CDP**（OpenClaw 的 `browser` 工具）— 用于 reverse engineer 未知平台的短链/API。安装：
  ```bash
  pip install playwright --break-system-packages
  playwright install chromium
  # 或依赖 OpenClaw 默认 bundled 的，直接 `browser(action=status)` 检查
  ```
- `pyzbar` + `pillow`（可选）— 解码二维码内嵌的下载 URL

**首次授权**：运行 `scripts/gmail-auth.py`，按提示浏览器授权一次，拿到 `token.json`。详见 `references/setup.md`。

**Ubuntu 安装 pdftotext**：`sudo apt install -y poppler-utils`

## Architecture

```
┌──────────────────────────────────┐  ┌────────────────────────────┐
│ scripts/download-invoices.py     │  │ scripts/invoice_helpers.py │
│  (end-to-end CLI, ~460 lines)    │◄─│  (pure functions, ~580)    │
│  - GmailClient (auth + paging)   │  │  - classify_email          │
│  - Query builder                 │  │  - extract_*_url (baiwang, │
│  - Download pipeline             │  │    fapiao, xforceplus)     │
│  - Pairing (same-day rule)       │  │  - resolve_baiwang_short…  │
│  - Markdown report generator     │  │  - classify_invoice_       │
│                                  │  │    category (住宿/餐饮)    │
│ scripts/gmail-auth.py            │  │  - extract_merchant_from_  │
│  (one-time OAuth bootstrap)      │  │    body/attachment         │
│                                  │  │  - validate_pdf_header     │
│ scripts/gmail-helper.py          │  │  - generate_filename       │
│  (ad-hoc Gmail search CLI)       │  │                            │
└──────────────────────────────────┘  └────────────────────────────┘

learned_exclusions.json  ← single source of truth for Gmail -from:/- subject: filters
references/
 ├── setup.md            ← Gmail API 授权流程
 └── platforms.md        ← 百望云 / fapiao / xforceplus / 12306 等平台细节
```

## Workflow (8 steps, all automated in download-invoices.py)

### Step 1 — 构建查询

从 `learned_exclusions.json` 读所有排除规则（发件人/主题黑名单），拼成 Gmail query：

```
after:{START} before:{END} ({INVOICE_KEYWORDS}) {EXCLUSIONS}
```

`INVOICE_KEYWORDS`：发票 OR invoice OR 水单 OR receipt OR folio OR e-folio OR 账单 OR 话费 OR 滴滴 OR 行程报销单 OR 电子发票 OR (from:12306@rails.com.cn has:attachment)

**learned_exclusions.json 格式**：
```json
{
  "exclusions": [
    { "rule": "-from:cmbchina", "reason": "招商银行月结单", "confirmed": "2026-04-30" }
  ]
}
```

### Step 2 — Gmail 搜索（分页）

`GmailClient.search()` 用 `nextPageToken` 分页直到空，`resultSizeEstimate` 不可靠。默认上限 1000 封。

### Step 3 — 抓全文 + 分类

对每封邮件调用 `classify_email()`，返回：

```python
{
    "doc_type": "TAX_INVOICE" | "HOTEL_FOLIO" | "TRAIN_TICKET" | "TRIP_RECEIPT" | "OTHER_RECEIPT" | "UNKNOWN" | "IGNORE",
    "method": "ATTACHMENT" | "ATTACHMENT_ZIP" | "LINK_FAPIAO_COM" | "LINK_BAIWANG" | "LINK_XFORCEPLUS" | "MANUAL" | "IGNORE",
    "pdf_attachments": [...],
    "zip_attachments": [...],
    "download_url": "...",          # or "BAIWANG_SHORT:http://u.baiwang.com/xxx"
    "hotel_name": "...",            # subject → attachment filename → body → sender fallback
    "merchant": "...",              # alias of hotel_name when not hotel
    "invoice_date": "20260319",     # from body "开具日期" if available
    ...
}
```

**分类决策树：**

```
Email
  ├── sender = 12306@rails.com.cn AND 无 PDF/ZIP → IGNORE
  │     （这是支付通知，真火车票发票需登录 12306.cn 下载）
  ├── sender = 12306 AND 有 PDF/ZIP → TRAIN_TICKET
  ├── 有 PDF 附件 → ATTACHMENT, 按 subject/filename 判 doc_type
  ├── 只有 ZIP → ATTACHMENT_ZIP, 解压取 PDF（丢 OFD/XML）
  └── 无附件 → 分析正文 URL：
        ├── fapiao.com/dzfp-web/pdf/download → LINK_FAPIAO_COM
        ├── pis.baiwang.com/smkp-vue → LINK_BAIWANG（构造 /downloadFormat URL）
        ├── u.baiwang.com/xxx → LINK_BAIWANG（短链，后续跟 301 拿 param）
        ├── s.xforceplus.com 标 "(PDF)" → LINK_XFORCEPLUS
        └── 都不匹配 → MANUAL
```

### Step 4-5 — 下载（附件 + 链接）

- **ATTACHMENT**：`GmailClient.get_attachment_bytes()` → base64 解码 → 写盘
- **ATTACHMENT_ZIP**：下载 ZIP → `extract_pdfs_from_zip()` 只取 .pdf → 移动到输出目录
- **LINK_FAPIAO_COM / LINK_XFORCEPLUS**：直接 `curl -sL`
- **LINK_BAIWANG (pis)**：`extract_baiwang_download_url()` 构造 `/bwmg/mix/bw/downloadFormat?param=XXX&formatType=pdf` → curl
- **LINK_BAIWANG (u.)**：`resolve_baiwang_short_url()` 先 GET 短链拿 301 Location，提取 param，再构造 downloadFormat URL → curl

**命名约定**：`{YYYYMMDD}_{商户}_{类型}.pdf`

- 日期优先级：正文"开具日期：YYYY年MM月DD日" → subject/filename 里的 YYYYMMDD → email `internalDate`（CST）
- 商户优先级：subject `【XXX】`模式 → 附件文件名 `dzfp_{no}_{merchant}_{ts}.pdf` → 正文"XXX 为您开具了"/"开票单位：XXX" → sender 域名回退
- 同名自动 `(1)`、`(2)`；多附件加 `-1`、`-2`

### Step 6 — 全量校验

每个文件 `validate_pdf_header()` 检查 `%PDF` 魔数。失败文件进 failed 列表并在报告里标出。

### Step 7 — 酒店水单 ↔ 住宿发票配对

**v5.2 核心规则（极简）**：

```
for each 水单 (HOTEL_FOLIO):
    匹配同一日期的 *住宿服务* 发票（category=LODGING，从 PDF 内容识别）
```

**为什么这么简单就够了**：一个人同一天不可能在两家酒店各住一晚，所以"退房日 = 开票日"已经是强约束。**不需要品牌归一化、城市匹配、别名映射**。

**为什么餐饮发票不自动配**：中国餐饮发票的**开票日 ≠ 就餐日**，常把多天消费合并一张发票。实测 2026-01-06 一天出现 3 张茵赫发票（无锡 + 杭州 + 苏州），显然不是同一天吃的。餐饮发票一律独立列出。

### Step 8 — 生成 Markdown 报告

输出 `下载报告.md`，包含：

1. 📊 摘要（水单/住宿/餐饮/其他数量）
2. 🏨 酒店入住配对表
3. ⚠️ 未匹配的住宿发票（同日无水单）
4. 🍽️ 餐饮发票按商户聚合
5. 📄 其他发票
6. 🚖 非发票单据（行程单/火车票等）
7. ❌ 下载失败清单

同时保存中间快照：`step3_classified.json`、`step4_downloaded.json`、`run.log`。

## Output

```
{output_dir}/
├── pdfs/
│   ├── 20260319_绿发酒店管理（北京）有限公司无锡万怡酒店_发票.pdf
│   ├── 20260319_无锡鲁能万怡酒店_水单.pdf
│   ├── 20260331_滴滴出行_发票-1.pdf
│   ├── 20260331_滴滴出行_发票-2.pdf
│   └── ...
├── 下载报告.md
├── step3_classified.json       # 分类快照
├── step4_downloaded.json       # 下载清单快照
└── run.log                     # 运行日志
```

## Handling Unknown Platforms (extensibility)

**新的中国发票平台每月都可能出现**。当 `下载报告.md` 里出现 MANUAL（或 skipped）邮件且确是真发票时，按以下三步操作：

### 1. 诊断：用 probe 脚本看 URL 属于哪种模式

```bash
python3 scripts/probe-platform.py "https://新平台.com/xxx"
```

脚本会输出：
- 启发式匹配（是否为已知的 9 种模式）
- 完整的 302 跳转链
- 最终 URL 分类：**直链 / paramList 型 / pdfUrl 型 / SPA 型**
- 具体的下一步操作

### 2. 执行：按 `references/platforms.md` 的 5 步 playbook

详见 [**"Adding support for a new platform"**](references/platforms.md#adding-support-for-a-new-platform-reverse-engineering-playbook) 章节：

1. **检查邮件正文** — 找可疑 URL
2. **用 `curl -sIL`** 探 302 链（一样的脚本也能干这事）
3. **用 OpenClaw `browser` 工具**打开 SPA，`performance.getEntriesByType('resource')` 拋出隐藏的 XHR/fetch API
4. **`fetch()` replay API** 拿到真实 PDF URL
5. **固化为 2 个函数** — `extract_<platform>_url` + `resolve_<platform>_short_url`（如需）两个纯函数加到 `invoice_helpers.py`

### 3. 注册：加到决策树和下载管道

- `classify_email` 最后的 `extract_*_url` 分支列表里新增 `LINK_<PLATFORM>`
- `download-invoices.py` 的 `download_link` 分支里添加新的 marker 解析
- `references/platforms.md` 添一段新平台文档（可复制已有章节作模板）

### 大部分平台本质上是以下 3 种模式之一

| 模式 | 判别 | 实现难度 | 示例 |
|------|------|----------|------|
| **直链型** | 邮件正文直接包含 `*.pdf` 或带 `?format=pdf`/`Wjgs=PDF` 的 URL | 最简单（5行正则）| fapiao.com, 百旺金穗云, jincai |
| **Query 参数型** | 短链 302 后 Location 里含 `pdfUrl=URLEncoded` | 中等（跳一次 + 解析 query）| 云票 bwjf |
| **SPA + API 型** | 短链 3 跳到 Vue 预览页，URL 里有 `paramList=...!!!...!false` | 最复杂（要找隐藏 API）| 诺诺网, 百望云 bwfp |

**大多数新平台 30 分钟内能支持完成**。

## Lessons Learned（踩过的坑 + 解决方案）

这是多次迭代（v1 → v5.2）沉淀下来的经验，**直接写在 SKILL.md 里保证分享给其他用户时完整**。

### 🔴 12306 支付通知邮件无附件

**现象**：17 封 `12306@rails.com.cn` 的 "网上购票系统-用户支付通知" 被误分类为 TRAIN_TICKET，然后跳到 MANUAL。  
**真相**：这些只是支付通知邮件，**不含车票附件**，真正的行程报销单需要登录 12306.cn 官网自己下载。  
**解决**：`classify_email` 里 12306 分支加前置检查 —— 无 PDF 和 ZIP 时返回 `doc_type=IGNORE`；同时加排除规则 `-subject:用户支付通知` 预防再命中。

### 🔴 UTC 时间戳导致日期偏一天

**现象**：邮件 `internalDate` 在 UTC 下是 3月18日 17:22，但实际开票时间是 CST 3月19日 01:22。用 `datetime.fromtimestamp` 不带 tz 时得到本机时区，结果不稳定。  
**解决**：所有时间戳回退都显式 `datetime.datetime.fromtimestamp(ts, tz=CST)`。更好的做法是优先从正文读"开具日期：YYYY年MM月DD日"。

### 🔴 日期正则 `DD/MM/YY` 误识别为 `YYYY`

**现象**：万豪邮件 subject `E-Folio From 28/01/26 To 29/01/26` 被解析成 2028-01-26。原正则 `(2[4-9])[-/](\d{2})[-/](\d{2})` 太贪心。  
**解决**：`extract_date_from_email` 删除 DD/MM/YY 模糊模式；新增 `max_date` 参数（默认 CST 今日），任何提取出的未来日期一律丢弃。优先级改为：正文"开具日期" → 明确的 YYYYMMDD → YYYY-MM-DD → 回退到 internalDate (CST)。

### 🟡 Platform 类细节 → 见 `references/platforms.md`

各平台（百望云 3 种模板 + 短链 / fapiao.com token / xforceplus / 12306 / 酒店多样发件人）的采集方式和常见失败模式集中放在 platforms.md，SKILL.md 不再重复。面向新平台的接入流程：platforms.md 找一个平台作为模板 → `invoice_helpers.py` 加 `extract_{platform}_url()` 纯函数 → `classify_email` 决策树插入分支 → platforms.md 补一段文档。

### 🟡 酒店发票商户名的 4 层 fallback

`extract_hotel_name()` 按优先级：

1. **subject** 匹配 `【XXX】开具`、`来自【XXX】`、`入住XXX的电子`、`E-Folio of XXX From`
2. **附件文件名** 如 `dzfp_{发票号}_{酒店全名}_{时间戳}.pdf`（最稳的信号）
3. **邮件正文** 匹配 `XXX 为您开具了电子发票`、`销售方名称：XXX`、`开票单位：XXX`
4. **sender 域名** 兜底（返回品牌级名如"万豪酒店"，精度最低）

酒店发票可能从 3 种渠道来：酒店系统邮箱 / 前台个人邮箱（如 `17768335659@163.com`）/ 百望云代发，**不能只看 sender**。

### 🟡 Google Play 海外收据 / 银行月结单是噪音

Kent 的实际场景里，这些不需要报销，全部加进 `learned_exclusions.json`：
- 银行：cmbchina, citiccard, bocomcc, cgbchina, czbank, hsbc.co, bosc.cn
- 券商：guodu.com.hk, dfzq.com.hk, phillip.com.hk, eddid, gfgroup.com.hk
- 订阅：justmysocks, nexitally, amazonaws.com
- 海外：apple.com, googleplay-noreply@google.com
- 非发票主题：预订确认, 预订取消, 客房升级, 退票, 改签, 还款, 贷款, eStatement, 月结单, 用户支付通知

`learned_exclusions.json` 是单一事实源。新用户可以全部清空从零开始，跑几次后自己确认要排除什么。

### 🟢 短链种类无穷 — 用工具教会自己扩展

已遇到 9 种不同平台，还会继续冒出新的。穷举正则是死路。**见上文 § Handling Unknown Platforms**，里面有 probe 诊断 + 5 步 playbook。大部分新平台 30 分钟内能搞定。

### 🟢 别名不靠谱，日期是最稳的配对信号

**v1~v4 走的弯路**：维护 `HOTEL_BRAND_KEYWORDS` + `CITY_KEYWORDS` + 别名映射（景枫↔万豪南京、来朋↔福朋喜来登），代码膨胀且永远追不上新的开票主体。

**v5.2 觉悟**：酒店的"品牌名"、"开票主体"、"城市"这些都在变，但**"我同一天不会在两家酒店各住一晚"这个物理事实不会变**。只按日期配对：

```python
for folio in hotel_folios:
    # 同日的所有 *住宿服务* 发票就是这次入住
    lodging_matches = lodging_by_date.get(folio.date, [])
```

删除了 70 行品牌/城市/别名代码，未来零维护。

### 🟢 餐饮发票：开票日 ≠ 就餐日

实证：2026-01-06 一天出现 3 张茵赫餐饮发票，销售方分别是 **无锡茵赫**、**杭州茵赫**、**苏州茵赫**。显然不是 0106 当天吃的，是前几天就餐积累后 0106 统一开票。

所以餐饮发票永远不自动关联酒店入住，独立列出让用户人工认领。

### 🟢 PDF 头字节校验不可省

中国发票平台对无效 URL 的响应很诡异：**HTTP 200 + `text/html` + 看起来正常的页面**（实则"文件下载失败"的 Vue 路由）。只有 `%PDF` 魔数校验能捕获这种情况。下载完立刻校验，失败的进 failed 列表并保留文件供 debug。

| 头字节 | 实际内容 | 典型原因 |
|--------|----------|----------|
| `25504446` (`%PDF`) | ✅ 合法 PDF | — |
| `3c21646f` / `3c21444f` (`<!do` / `<!DO`) | HTML 预览页 | 拿到了预览 URL，不是下载 URL |
| `504b0304` (`PK..`) | ZIP / OFD | 下错格式，找 PDF 专属链接 |
| `89504e47` (`.PNG`) | PNG 图 | 拿到二维码或 logo |
| `ffd8` | JPEG | 同上 |

## Scripts 索引

| 脚本 | 作用 |
|------|------|
| `scripts/download-invoices.py` | 端到端 CLI（推荐入口）|
| `scripts/gmail-auth.py` | 一次性 OAuth 授权，生成 `token.json` |
| `scripts/gmail-helper.py` | 快速 Gmail 搜索调试（`gmail-helper.py "query"`）|
| `scripts/invoice_helpers.py` | 纯函数库（classify / extract / validate / normalize）|
| `scripts/probe-platform.py` | 遇到未知平台 URL 时用于 reverse engineering：诊断跳转链 + 猜测 API + 下步建议 |

## References

| 文件 | 内容 |
|------|------|
| `references/setup.md` | Gmail API 申请 + OAuth 首次授权流程 |
| `references/platforms.md` | 百望云 / fapiao.com / xforceplus / 滴滴 / 12306 / Marriott 等平台下载细节 |
| `learned_exclusions.json` | 噪音发件人/主题排除规则（用户可编辑）|
