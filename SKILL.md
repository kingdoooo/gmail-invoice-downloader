---
name: gmail-invoice-downloader
display_name: Gmail 发票下载器
description: "Batch-download Chinese invoices, receipts, hotel folios, and billing documents from Gmail as PDFs, then use LLM OCR to extract vendor/date/amount and pair hotel folios↔invoices + ride-hailing receipts↔invoices. Outputs a ready-to-submit report (下载报告.md), Excel-compatible CSV (发票汇总.csv), and zip bundle. Use when: (1) downloading invoices/receipts for 报销 / expense reimbursement, (2) batch-collecting billing documents from Gmail, (3) the user mentions 发票, invoice, 水单, 报销, 收据, folio, or hotel e-folio. Extensible to new Chinese invoice platforms via `scripts/probe-platform.py` + reverse-engineering playbook in `references/platforms.md`."
icon: "🧾"
---

> **Skill target**: This Skill is invoked by **OpenClaw Agents**, not by end users directly. The non-standard frontmatter keys (`display_name`, `icon`) are OpenClaw runtime extensions — they are **not** part of the Anthropic Skill spec and do nothing in a plain Claude Code / Claude.ai context. The agent-facing contract — exit codes, `REMEDIATION:` stderr lines, `missing.json` schema v1.0 — is documented in § Exit Codes and § Loop Playbook below.

# Gmail Invoice Downloader (v5.7.2)

搜索 Gmail 中用户指定日期范围内的发票/收据/水单/行程单，用 LLM OCR 提取销售方/日期/金额，按 P1 remark / P2 日期+金额 / P3 同日兜底 三层规则配对酒店水单↔住宿发票、按金额 0.01 容差配对网约车发票↔行程单，输出 `下载报告.md` + `发票汇总.csv` + `发票打包_YYYYMMDD-HHMMSS.zip`。

**已支持平台**（Step 3 决策树自动分类）：直接 PDF 附件 / ZIP 附件（取 PDF 丢 OFD）/ 9 种中国发票平台链接（百望云 3 种模板 + 诺诺网 + fapiao.com + xforceplus + 云票 + 百旺金穗云 + 金财数科 + 克如云）/ 12306 支付通知（过滤）/ 酒店 e-folio。详见 [`references/platforms.md`](references/platforms.md)。

## Quick Start

```bash
# Default: AWS Bedrock via IAM role / instance profile (no credential setup needed on EC2/ECS)
python3 scripts/download-invoices.py \
    --start 2026/01/01 \
    --end 2026/05/01 \
    --output ~/invoices/2026-Q1
```

一条命令完成：preflight → 搜索 → 分类 → 下载 → 校验 → LLM OCR → 重命名 → 匹配 → 生成 3 份交付物 + zip。典型 60 封邮件跑完约 90-120 秒（首次）/ ~50 秒（缓存命中）。

## Prerequisites

### 必备
- Gmail API OAuth2 credentials + token：
  - `~/.openclaw/credentials/gmail/credentials.json`（Desktop app 类型 OAuth client）
  - `~/.openclaw/credentials/gmail/token.json`（scope: `gmail.readonly`）
- Python 3.10+
- `curl`（链接型下载，会跟随重定向）
- `pdftotext`（poppler-utils — LLM 幻觉金额检测需要）

### LLM Provider（v5.3 新增）

脚本通过 `scripts/core/llm_client.py` adapter 调用 LLM。支持 **6 种 provider**，默认 Bedrock。`LLM_PROVIDER` 环境变量或 `--llm-provider` CLI 参数切换。

#### 1. `bedrock`（默认）— AWS Bedrock

支持 3 种 AWS 认证方式，boto3 自动按优先级选择：

| 认证方式 | 设置 | 典型场景 |
|---|---|---|
| IAM Role / Instance Profile | 无需设置 | EC2 / ECS / Lambda |
| AWS Profile | `AWS_PROFILE=myprofile` | 本地开发 |
| AKSK (Access Key) | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | CI / 第三方环境 |
| Bedrock API Key | `AWS_BEARER_TOKEN_BEDROCK=<key>` | boto3 ≥ 1.35.17 |

```bash
export AWS_REGION=us-east-1                                # 可选，默认 us-east-1
export BEDROCK_MODEL_ID=global.anthropic.claude-opus-4-7 # 可选
```

boto3 凭证解析链：环境变量 → `AWS_BEARER_TOKEN_BEDROCK` → `~/.aws/credentials` → IAM Role / Instance Profile → ECS Task Role。

#### 2. `anthropic` — Anthropic API 官方

```bash
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY='<your-key>'
export ANTHROPIC_MODEL=claude-sonnet-4-6   # 可选
```

#### 3. `anthropic-compatible` — Anthropic SDK + 兼容端点

适用于 LiteLLM proxy / OpenRouter / Zhipu / Dashscope 等提供 Anthropic Messages API 兼容接口的端点：

```bash
export LLM_PROVIDER=anthropic-compatible
export ANTHROPIC_BASE_URL='<your-proxy-base-url>'
export ANTHROPIC_API_KEY='<endpoint-specific-key>'
export ANTHROPIC_MODEL=claude-sonnet-4-6   # 各端点 model 命名可能不同
```

已验证：LiteLLM proxy（claude-sonnet-4-6 / claude-haiku-4-5 / claude-opus-4-7）均可正确提取发票字段。

#### 4. `openai` — OpenAI API 官方

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY='<your-key>'
export OPENAI_MODEL=gpt-4o        # 可选，默认 gpt-4o
```

PDF 以 base64 data URL 形式内联发送（`type: "file"` 消息块），无需 Files API。适配 GPT-4o+ 的文件输入。

#### 5. `openai-compatible` — OpenAI SDK + 兼容端点

适用于 LiteLLM proxy / DeepSeek / Kimi / Qwen / vLLM / LocalAI / Azure OpenAI：

```bash
export LLM_PROVIDER=openai-compatible
export OPENAI_BASE_URL='<your-proxy-base-url>/v1'
export OPENAI_API_KEY='<endpoint-specific-key>'
export OPENAI_MODEL=claude-sonnet-4-6   # 端点支持的模型名
```

已验证：LiteLLM proxy 的 chat.completions + `type: "file"` (base64 data URL) 成功提取发票字段。**注意**：并非所有 OpenAI 兼容端点都支持 file 消息块（尤其是纯文本模型）。不支持时调用会 400/500 报错 → 切到 anthropic-compatible 或同 endpoint 的其他模型。

#### 6. `none` — 跳过 LLM

`--no-llm` 或 `--llm-provider=none`。所有 PDF 归 UNPARSED，只用邮件元数据命名。调试/成本敏感场景。

**数据主权说明**：PDF 会以 base64 传给云端 LLM。发票含身份证号 / 手机号 / 房号 / 行程时间等敏感信息。本地模型支持未计划（未来可能通过 `LLM_PROVIDER=ollama` 扩展）。

### LLM OCR 并发控制

- 默认 5 并发（Bedrock 默认配额 + 大多数 OpenAI/Anthropic tier-2+ 足够）
- Anthropic tier-1：`export LLM_OCR_CONCURRENCY=2`
- 自建代理/低带宽：按需调低
- 非正整数 → doctor 标红 + `analyze_pdf_batch` 抛 `LLMConfigError`（exit 3）

### OCR 结果缓存

`~/.cache/gmail-invoice-downloader/ocr/` 按 PDF 的 SHA-256 缓存。同一个 PDF 重跑 = 0 LLM 调用。LRU 10000 条。

### 选装（遇到新平台时用）
- **Chromium + CDP**（OpenClaw 的 `browser` 工具）— 用于 reverse engineer 未知平台的短链/API
- `pyzbar` + `pillow`（可选）— 解码二维码内嵌的下载 URL

### 安装依赖

```bash
pip install anthropic                # 直连 anthropic 时需要，可选
pip install openai                   # 直连 openai 时需要，可选
pip install boto3                    # 默认 provider, LLM_PROVIDER=bedrock 时需要
```

**首次授权**：运行 `scripts/gmail-auth.py`。详见 `references/setup.md`。  
**Ubuntu 安装 pdftotext**：`sudo apt install -y poppler-utils`

### Preflight 自检

```bash
python3 scripts/doctor.py
```

独立运行（或 download-invoices.py 开头自动跑）。红色项 + REMEDIATION 给明确下一步。退出码 2 = 有失败项。

## Architecture

```
┌──────────────────────────────────┐  ┌────────────────────────────┐
│ scripts/download-invoices.py     │  │ scripts/invoice_helpers.py │
│  (CLI + main, ~950 lines)        │◄─│  (pure functions, ~1000)   │
│  - GmailClient (auth + paging)   │  │  - classify_email          │
│  - Query builder                 │  │  - extract_*_url (×9 platf)│
│  - Download pipeline (unchanged) │  │  - resolve_*_short_url     │
│  - write_report_v53 (new)        │  │  - validate_pdf_header     │
│  - exit codes 0/2/3/4/5          │  │  - generate_filename       │
│                                  │  │                            │
│ scripts/doctor.py (new v5.3)     │  │ scripts/postprocess.py     │
│  - preflight check + REMEDIATION │  │  - analyze_pdf_batch       │
│  - exit 2 on any fail            │  │  - rename_by_ocr           │
│                                  │  │  - do_all_matching         │
│ scripts/gmail-auth.py            │  │  - write_summary_csv       │
│ scripts/gmail-helper.py          │  │  - write_missing_json      │
│ scripts/probe-platform.py        │  │  - zip_output              │
└──────────────────────────────────┘  │  - merge_supplemental      │
                                      └────────────────────────────┘
                                             │
              ┌──────────────────────────────┘
              ▼
┌────────────────────────────────────────────┐
│ scripts/core/  (snapshot + new modules)    │
│  From reimbursement-helper (commit a0e8515)│
│  - classify.py  (modified: no random meal) │
│  - matching.py  (unchanged)                │
│  - location.py  (unchanged)                │
│  - (helpers.py NOT copied — Concur-only)   │
│  NEW for v5.3:                             │
│  - llm_client.py (Anthropic/Bedrock)       │
│  - llm_ocr.py   (extract_from_bytes + cache)│
│  - prompts.py   (OCR prompt)               │
│  - validation.py (anti-hallucination)      │
└────────────────────────────────────────────┘
              │
              ▼
~/.cache/gmail-invoice-downloader/ocr/{sha16}.json  ← OCR results cache

learned_exclusions.json  ← single source of truth for Gmail -from:/- subject: filters
references/
 ├── setup.md            ← Gmail API 授权流程
 └── platforms.md        ← 百望云 / fapiao / xforceplus / 12306 等平台细节
```

## Workflow (10 steps, all automated in download-invoices.py)

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

### Step 5 — 全量校验

每个文件 `validate_pdf_header()` 检查 `%PDF` 魔数。失败文件进 failed 列表并在报告里标出。

### Step 6 — LLM OCR + 可信度校验（v5.3 新增）

下载完的每张 PDF 送 LLM OCR 提取结构化字段（销售方、日期、金额、确认号等）。ThreadPoolExecutor max_workers=5（可通过 `LLM_OCR_CONCURRENCY` 覆盖；详见上文"LLM OCR 并发控制"节）。指数退避 2s/4s/8s 处理 429/5xx。

**抗幻觉校验**（`scripts/core/validation.py`）：
- **金额合理性**：用 `pdftotext -layout` 扫 PDF，LLM 金额偏离页面任何数字 >10% → 标 `_amountConfidence: "low"`
- **日期合理性**：LLM 日期超出邮件 `internalDate ± 90 天` → 标 `_dateConfidence: "low"`
- **销售方识别**：LLM 把购买方填错位置 → `validate_and_fix_vendor_info` 自动用 sellerName/hotelName 回退

LLM 失败（auth / rate limit / parse error 耗尽重试）→ 文件重命名为 `UNPARSED_{msgid}_{orig}.pdf`，missing.json 标 `extraction_failed`。**不再用邮件元数据伪装**（避免静默错误数据）。

### Step 7 — 按 OCR 重命名（v5.3 新增）

```
{YYYYMMDD}_{vendorName_from_LLM}_{category_label}.pdf
```

例：`20260319_无锡万怡酒店_酒店发票.pdf`、`20260315_滴滴出行_网约车发票.pdf`

`sanitize_filename()` 强制：去 `/ \ : * ? " < > |`、去 `\0`、折叠 `..`、去首尾 `.-_`、长度截 80。LLM 返回 `../../etc/passwd` → 变 `etc_passwd`，不可能逃出 `pdfs/` 目录。

### Step 8 — 三层匹配（v5.3 新增）

**酒店**（`core.matching.match_hotel_pairs` + v5.2 兜底）：

1. **P1 remark**：发票 `remark == folio.confirmationNo`（或 ∈ `internalCodes`）→ 最强信号
2. **P2 date+amount**：`invoice.transactionDate == folio.checkOutDate` AND `amount` 0.01 容差
3. **P3 date-only (v5.2 fallback)**：P1/P2 都失败，但同日的酒店发票 + 水单 → 低置信匹配，报告里标 `⚠️ 日期匹配`

**网约车**（`core.matching.match_ride_hailing_pairs`）：
- 金额 0.01 容差；多张同金额发票用文件名序号（如 `滴滴电子发票 (1).pdf` 的 `1`）消歧

**餐饮**：不自动匹配（v5.2 Lessons Learned 仍适用——开票日 ≠ 就餐日）

### Step 9 — 三份交付物

1. **`下载报告.md`**：摘要 + P1/P2/P3 配对表 + 网约车配对 + 餐饮聚合 + 未匹配项 + 补搜建议
2. **`发票汇总.csv`**（UTF-8 BOM，Excel 直开）：列 = 序号 / 开票日期 / 类别 / 金额 / 销售方 / 备注 / 文件名 / 数据可信度
3. **`missing.json`**（schema v1.0，Agent 读）：含 `status`、`recommended_next_action`、`convergence_hash`、`items[]`

### Step 10 — 打包 zip

`发票打包_YYYYMMDD-HHMMSS.zip`（写在 output_dir 的父目录）：
- Allowlist：只含 `.pdf` / `.md` / `.csv`。**JSON 快照和 run.log 不入包**
- 原子写：`.zip.tmp` → `os.replace` → final
- 自排除：不嵌套旧 `发票打包_*.zip`
- Manifest 检查：至少 1 份 CSV + 1 份 MD，否则抛错

## Output

```
{output_dir}/
├── pdfs/
│   ├── 20260319_无锡万怡酒店_酒店发票.pdf
│   ├── 20260319_无锡万怡酒店_水单.pdf
│   ├── 20260315_滴滴出行_网约车发票.pdf
│   ├── 20260315_滴滴出行_行程单.pdf
│   └── UNPARSED_m12ab3_原始文件名.pdf   # LLM OCR 失败时
├── 下载报告.md                         # ← 交付物 1
├── 发票汇总.csv                        # ← 交付物 2（Excel 直开）
├── missing.json                       # Agent 读
├── step3_classified.json              # 内部快照
├── step4_downloaded.json              # 内部快照
└── run.log

{output_dir}/../发票打包_YYYYMMDD-HHMMSS.zip   # ← 交付物 3（发给财务）
```

## Agent First-Run Procedure

当用户说 "下载我 {X 时期} 的发票" 时，Agent 按以下顺序执行：

### 1. 解析日期（NL → Gmail 格式 YYYY/MM/DD）

| 用户说 | start | end（exclusive）|
|---|---|---|
| "2026Q1" / "2026年一季度" | 2026/01/01 | 2026/04/01 |
| "2026年3月" / "三月的" | 2026/03/01 | 2026/04/01 |
| "上个月" | 上月 1 日 | 本月 1 日 |
| "最近三个月" | 今天减 3 月 | 明天 |
| "2025 年全年" | 2025/01/01 | 2026/01/01 |

### 2. 选 output 目录（每次新跑都用新目录）

**核心规则**：初次跑一个日期区间 MUST 用全新目录。如果目标目录已经存在，自增后缀直到找到空目录。**复用旧目录**仅用于 `--supplemental` 补搜（见 Loop Playbook）。

**命名约定**：
- 首选：`~/invoices/{YYYY-QN}` 或 `~/invoices/{YYYY-MM}`（人类可读）
- 碰撞时自增：`{base}` 存在 → 试 `{base}-2` → `{base}-3` → ...（Agent 负责探测）

**为什么**：同一个 output_dir 跨批次累积 PDF 会把下载报告、CSV 和 zip 变得混乱。从 v5.7.1 起 zip 已经用白名单把跨批次残留排除在外（正确性不受影响），但 pdfs/ 本身仍会累积文件，用户开 Finder 看会困惑。**每次新跑用新目录 = 零歧义**。

**Agent 逻辑伪代码**：

```python
def pick_output_dir(base: str) -> str:
    """base = '~/invoices/2026-Q1' 这种未自增的基础名。"""
    path = os.path.expanduser(base)
    if not os.path.exists(path) or _is_empty(path):
        return path
    n = 2
    while os.path.exists(f"{path}-{n}"):
        n += 1
    return f"{path}-{n}"

# _is_empty: pdfs/ 里没有非 IGNORED_ PDF 且无 step4_downloaded.json / missing.json
# / 下载报告.md / 发票汇总.csv 就算空。download-invoices.py::_inspect_existing_output_dir
# 和这个判断一致，是运行期兜底。
```

**补搜**：Agent 读上一次运行的 `missing.json.batch_dir`，把那个路径原样传给 `--output` + 加 `--supplemental` flag。不新建目录。

**运行期兜底**：即便 Agent 忘了新建目录，`download-invoices.py` 在 initial run 下会在 stdout + run.log 输出一条 `⚠️ output_dir 不是空的 ...` 提示行（不阻断，纯告知），方便用户发现并决定是否中断重跑到新目录。`--supplemental` 模式下该提示自动静默。

### 3. 跑 preflight
```bash
python3 scripts/doctor.py
```
非 0 退出码 → 向用户报告缺失项 + 退出。常见 REMEDIATION：
- `ANTHROPIC_API_KEY not set` → 提醒用户 export 或 `--no-llm`
- `Gmail token.json not found` → 跑 `scripts/gmail-auth.py`

### 4. 初始下载

```bash
python3 scripts/download-invoices.py \
    --start {start} --end {end} --output {output_dir}
```
等待 90-120s。退出码解读：
- `0` = 全部成功 → 转 6
- `5` = partial（有 UNPARSED 项或 failed 下载）→ 转 5
- `2` = Gmail auth 失败 → REMEDIATION 跑 gmail-auth.py
- `3` = LLM config 失败 → REMEDIATION 按 stderr 指示
- `4` = Gmail quota → 等 60 秒重试

### 5. 读 missing.json → 决定 Loop（见下）

### 6. 最终交付

查找 `find {output_dir}/.. -name '发票打包_*.zip' -newer {start_time}` → 最新 zip。
向用户报告：
```
共 N 份 PDF，M 对匹配成功，K 项需手动补充（见 missing.json）。
交付：{zip_path}
```

---

## Loop Playbook（Agent 使用）

初始运行后，Agent 读 `out/missing.json` 并按 `status` / `recommended_next_action` 字段决策。**不自己管状态机** — 脚本已算好。

### missing.json schema v1.0

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-04-30T22:00:00+08:00",
  "iteration": 1,
  "iteration_cap": 3,
  "status": "needs_retry",                        // converged | needs_retry | max_iterations_reached | user_action_required
  "recommended_next_action": "run_supplemental",  // run_supplemental | stop | ask_user
  "convergence_hash": "a1b2c3d4e5f6...",
  "batch_dir": "~/invoices/2026-Q1",
  "items": [
    {
      "type": "hotel_folio",                      // | hotel_invoice | ridehailing_receipt | ridehailing_invoice | extraction_failed
      "needed_for": "20260410_希尔顿_酒店发票.pdf",
      "expected_date": "2026-04-10",
      "expected_merchant": "希尔顿",
      "expected_amount": 1280.00,
      "remark_from_invoice": "HT20260410",
      "hint": "发票 remark=HT20260410 未在任何水单的 confirmationNo 中出现",
      "search_suggestion": {
        "query": "水单 OR folio OR \"Guest Folio\" 希尔顿",
        "date_range_start": "2026/04/08",
        "date_range_end": "2026/04/12",
        "priority": "high"
      }
    }
  ],
  "out_of_range_items": [                        // v5.5 — additive to v1.0
    {
      "type": "hotel_invoice",
      "needed_for": "20250318_杭州万豪_水单.pdf",
      "business_date": "2025-03-18",
      "reason": "business_date_out_of_range",
      "expected_merchant": "杭州万豪",
      "hint": "水单退房日早于本批次 --start，已跳过自动补搜",
      "search_suggestion": null
    }
  ]
}
```

`out_of_range_items` 不参与 `convergence_hash` / `status` 计算，也不触发 `run_supplemental`。

### Loop 决策

```
读 out/missing.json
switch status:
  case "converged":                 → DONE（收敛）
  case "max_iterations_reached":    → DONE（请用户手动补，提示 items 中 needed_for 列表）
  case "user_action_required":      → DONE。items 中可能包含:
                                     - type=extraction_failed (LLM 读不出的 PDF，人工核查 PDF 是否损坏)
                                     - type=unknown_platform (probe 也识别不出的新平台，列出清单
                                       提示用户，建议参考 references/platforms.md 的 5-step playbook
                                       评估是否扩展支持)
  case "needs_retry":
      # 聚合所有 search_suggestion 为一个 union query，一次补搜一网打尽
      union_query = " OR ".join(item.search_suggestion.query for item in items if item.search_suggestion)
      start = min(item.search_suggestion.date_range_start for item in items if item.search_suggestion)
      end   = max(item.search_suggestion.date_range_end   for item in items if item.search_suggestion)

      python3 scripts/download-invoices.py \
          --supplemental \
          --start {start} --end {end} \
          --output {batch_dir} \
          --query "{union_query}"
      # --iteration 自动递增，不用传

      # 重新读 missing.json
      goto 读 out/missing.json
```

**关键：每轮只做一次 union call**（不是 for-item 跑 N 个调用）。脚本 merge 新下载到 step4_downloaded.json，重算 matching 和 missing.json。

### Failed-link auto-probe (exit 5, before run_supplemental)

当 `run.log` 中出现 `failed to resolve … short link` 条目时，在决定 `run_supplemental` **之前** 先逐一处理：

1. 从 `run.log` 提取原始 URL。
2. 运行：`python3 scripts/probe-platform.py "<url>"`
3. 若 probe 输出了 PDF 下载 URL：
   - `curl -sL --max-time 60 -H "User-Agent: Mozilla/5.0" -o <out>/pdfs/probe_<msgid>.pdf "<pdf_url>"`
   - 校验 `%PDF` 头（前 4 字节）。
   - 成功后，重新跑 postprocess：
     `python3 scripts/download-invoices.py --postprocess-only --output <out>`
4. 若 probe 找不到 PDF URL（未知平台 / 链路断裂）：
   - 从 `<out>/step3_classified.json` 取邮件 subject / from。
   - 通过 helper 上浮给用户：
     ```
     python3 scripts/record-unknown-platform.py \
         --output <out> \
         --url "<url>" \
         --email-subject "<subject>" \
         --email-from "<from>" \
         --probe-suggestion "<probe stdout 下一步建议>"
     ```
     该 helper 会向 `missing.json.items[]` 追加 `unknown_platform` 条目、
     将 `status` 翻转为 `user_action_required`、重算 `convergence_hash`。

所有失败链路处理完后：

- 全部自动恢复 → 重新读 `missing.json`，继续 Loop 决策。
- 存在未恢复项 → `missing.json.status` 变为 `user_action_required`，
  本批次 Loop 终止。在 OpenClaw 聊天总结中列出：
  - 原邮件 subject + 发件人
  - probe 主机 / 建议
  - 指向 `references/platforms.md` 的 5-step 新平台接入 playbook

### 收敛保护

- **`iteration_cap=3`** — 脚本在第 3 轮输出 `status=max_iterations_reached`
- **convergence_hash** — 连续两轮 items 不变 → 脚本自动输出 `status=converged`
- **Agent 不重试 extraction_failed 项** — 这些需要人工核查 PDF

---

## Exit Codes

| Code | 含义 | stderr REMEDIATION |
|------|------|--------------------|
| 0 | 全部成功 | — |
| 1 | 未知错误 | 查 run.log |
| 2 | Gmail auth 失败 | `run scripts/gmail-auth.py` |
| 3 | LLM config 失败 | 查 stderr REMEDIATION：针对当前 provider 调 AWS/Anthropic/OpenAI 凭证，或切 `--llm-provider=none` |
| 4 | Gmail 配额超限 | 等 60 秒 + `--max-results` 降低 |
| 5 | 部分成功 | 正常出交付物，但有 UNPARSED 或 failed 项 → 查 missing.json → 先跑 auto-probe（见 Loop Playbook 子节）再决定 run_supplemental |

Agent pattern-match stderr `REMEDIATION:` 行自动恢复。

---

## Presenting Results to the User

Every successful (or partially successful) `print_openclaw_summary` run emits **two stdout sentinels** so the wrapping Agent can deliver the result to the user faithfully across any chat channel (飞书 / Slack / Discord / iMessage / …). The Skill itself is channel-agnostic and does **not** call any IM API directly.

### Sentinel 1 — `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END`

Two bare anchor lines (no colon, no payload) wrap the full human-readable summary on every code path that reaches `print_openclaw_summary`, including the R16b empty-result branch.

```
CHAT_MESSAGE_START
📄 发票报销包 — 2025/04/01 → 2025/07/01

✅ 共 63 份凭证，合计 ¥48231.00
  ...(summary body)...

💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？
   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。
CHAT_MESSAGE_END
```

### Sentinel 2 — `CHAT_ATTACHMENTS:`

A single-line JSON declaring deliverable files. Emitted only on the R16a non-empty path, after `CHAT_MESSAGE_END`. Skipped entirely on R16b.

```
CHAT_ATTACHMENTS: {"files":[{"path":"/abs/.../发票打包_xxx.zip","caption":"报销包"},{"path":"/abs/.../下载报告.md","caption":"报告"},{"path":"/abs/.../发票汇总.csv","caption":"明细"}]}
```

Schema:
- `files[]`: ordered list; the array order is the recommended upload order (报销包 → 报告 → 明细).
- `files[].path`: absolute path on the Skill host.
- `files[].caption`: short display label. Current values: `"报销包"` / `"报告"` / `"明细"`.

When `zip_output` fails (DEC-6), the zip entry is omitted but MD + CSV are still declared.

### Agent Playbook

After each Skill run:

1. Scan stdout for `CHAT_MESSAGE_START` and `CHAT_MESSAGE_END` lines.
2. Forward the content **between** those two lines to the user **verbatim** — do not add, remove, translate, summarize, or cherry-pick. Preserve all emoji, Chinese text, blank lines, and the tail invite.
3. If the stdout also contains `CHAT_ATTACHMENTS: {...}`, parse the JSON and, **in order**, upload each `file.path` as an attachment to the current chat using the channel's native message tool. Use `file.caption` as the attachment label/caption.
   - 飞书 channel: use the message tool's file-upload capability.
   - Slack / Discord / WhatsApp / iMessage / other IM: use the equivalent message tool in the Agent's tool set.
   - If the current channel's message tool does **not** support file attachments (e.g., plain SMS): skip the upload and include the absolute path in the forwarded text instead.
4. If a single upload fails, **do not abort**. Append one warning line to the same reply:
   ```
   ⚠️ {filename} 上传失败（{reason}），请从 {abs_path} 取
   ```
   Then continue with the next file.
5. If `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END` are absent (early-error path), follow the `REMEDIATION:` stderr line as documented in § Exit Codes. Do not attempt attachments.

**Delivery order:** summary text first, attachments after.

**Redundancy is intentional:** the human-readable summary already shows absolute paths (e.g., `📦 报销包（提交这个）: /abs/...`). Those lines remain in the forwarded text so that if an upload fails (channel limit, network, unsupported), the user still has the path.

### Invariants

- Each sentinel appears at most **once** per Skill run.
- Strict ordering: `CHAT_MESSAGE_START` → `CHAT_MESSAGE_END` → `CHAT_ATTACHMENTS:` (the last two may be absent).
- `CHAT_ATTACHMENTS:` present ⇒ `CHAT_MESSAGE_START` / `END` both present.
- Regression-tested in `tests/test_agent_contract.py::TestChatSentinelContract` (R18).

---

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

这是多次迭代（v1 → v5.3）沉淀下来的经验，**直接写在 SKILL.md 里保证分享给其他用户时完整**。

### 🟢 v5.3 — 用 LLM 提取销售方替代 pdftotext 拼凑的 rationale

**v5.2 问题**：`extract_seller_from_pdf` / `extract_hotel_name` 的 4 层 fallback（subject → filename → body → sender 域名）在酒店品牌邮件里还好，但跨 9 个发票平台时经常返回 `未知商户`。pdftotext 抽到一段文本后做正则，各家模板不一样就漏。

**v5.3 解决**：用 LLM（Claude Sonnet）从 PDF 提取结构化字段。`vendorName` 严格取发票右侧（销售方）而非左侧（购买方），`validate_and_fix_vendor_info` 在 LLM 把亚马逊误当 vendor 时用 `sellerName` / `hotelName` 回退。

**代价 + 缓解**：~$0.02-0.05/invoice。按 PDF SHA-256 缓存到 `~/.cache/gmail-invoice-downloader/ocr/` —— 重跑同一批 = $0。

### 🟢 v5.3 — LLM 失败 ≠ 自动降级到邮件元数据

**原本可能犯的错**：LLM rate limit / auth 失败 → fallback 到 v5.2 的 email metadata 猜 vendorName。

**为什么错**：用户看文件名 `20260319_万豪酒店_发票.pdf` 会以为数据是 LLM 提取的；实际是 email subject 里抓出来的，可能错。静默数据污染最糟。

**v5.3 规则**：LLM 失败 → 文件重命名为 `UNPARSED_{msgid}_{orig}.pdf`，missing.json 标 `extraction_failed`，CSV 数据可信度列 = `failed`。**让失败可见**。`--no-llm` 模式是合法的主动跳过（和失败不同），所有记录标 UNPARSED 走同一路径。

### 🟢 v5.3 — LLM 也会幻觉数值，必须 cross-check

**发现**：LLM 偶尔返回格式正确但数值错误的 JSON。例如 `transactionAmount: 128.00` 实际 PDF 上是 1280.00（漏位），或 `transactionDate: "2024-03-19"` 实际是 2026。

**v5.3 解决**（`scripts/core/validation.py`）：
- 金额：`pdftotext -layout` 抽页面上所有 `\d+\.\d{2}` 金额，LLM 金额偏离任何一个 >10% → `_amountConfidence: "low"`
- 日期：超出邮件 `internalDate ± 90 天` → `_dateConfidence: "low"`
- 两类 flag 都显示在 CSV `数据可信度` 列 + `备注` 列的 `⚠️金额可疑` 标记

### 🟢 v5.3 — 酒店匹配 P3 兜底：remark 空时仍用同日规则

**问题**：v5.3 主匹配 P1 `remark==confirmationNo` / P2 `日期+金额` 比 v5.2 更强，但 LLM 偶尔提不到 remark，或金额差几分税额 → P1/P2 都 miss。

**解决**：`do_all_matching` 在 P1/P2 之后加 P3 —— 同日的酒店发票 + 水单（有任意一个匹配）标 `match_type: "date_only (v5.2 fallback)"`，confidence: `low`。报告表格显示 `P3 (仅日期)⚠️`。这样匹配率不会因为 v5.3 新规则而回归。

### 🟢 v5.3 — scripts/core/ 是快照，不会自动同步

`scripts/core/` 从 `reimbursement-helper/backend/agent/utils/` 复制（commit `a0e8515`）。reimbursement-helper 未来新增酒店品牌 / 新类别 / 修 bug 时，**本目录不会自动跟**。

**检测漂移**：`diff -r scripts/core/ ~/reimbursement-helper/backend/agent/utils/`
**手动同步**：把变更逐文件 cherry-pick 过来（注意 `classify.py` 已删了 `detect_meal_type` 的随机逻辑，不要覆盖这个删除）。

### 🟢 v5.7 — IGNORED 白名单分类 + 非报销票据过滤（双层防御）

**背景**：2025Q4 smoke 里 Termius 订阅发票（Stripe 模板 + 英文 docType "Invoice"）被 `is_hotel_folio_by_doctype` 的 "Statement"/"Invoice" 类关键字命中，滑入 HOTEL_FOLIO 管道永不匹配，成了 `missing.json` 里永远修不好的 `hotel_invoice` 缺口。Agent loop 通过 `convergence_hash` 兜底判定为 `converged`，但这是**假收敛**——该记录根本不属于可报销范围。下一个英文 SaaS 供应商（Notion / Figma / GitHub / Linear ...）必然再踩同一坑。

**决策**（双层防御）：

1. **Prompt 层**（`scripts/core/prompts.py`）：`arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` 要求 PDF 原文含明确酒店标签（`Arrival / Departure / Check-in / Check-out / 入住日期 / 抵店日期 / 离店日期 / 退房日期 / 入离日期 / Room No. / 房号` 等），否则保持 null。堵 `is_hotel_folio_by_fields` 3-choose-2 路径被订阅区间（`Nov 12, 2025 – Nov 12, 2026`）、账单周期、开票到期日误触发。
2. **Classifier 层**（`scripts/core/classify.py::classify_invoice`）：fallthrough 出口从 `UNKNOWN` 改 `IGNORED`；`is_hotel_folio_by_doctype` 命中后 narrow gate 要求 **≥2 of {hotelName, confirmationNo, internalCodes, roomNumber}** — 故意**不含** `balance`（Stripe / Termius "Amount due / Amount paid" 语义冲突）、**不含** `arrivalDate / departureDate`（已由 prompt 层约束）。`is_hotel_folio_by_fields` 3-choose-2 强特征路径不动。

**IGNORED 记录处理**：`IGNORED_{sender_short}_{原名}.pdf` 前缀保留在 output_dir，不进 CSV / zip / `missing.json.items[]`。`sender_short` 从邮件 `from` 头 domain 取（`billing@termius.com` → `termius`，上限 20 字符），空时 `unknown`。报告末尾「已忽略的非报销票据」节追加 `learned_exclusions.json` CTA 块（按 domain 聚合的 `-from:xxx.com  # 已过滤 N 次`），让用户下次 Gmail 搜索直接过滤省 OCR 成本。

**Agent 合约不动**：`missing.json` schema 保持 `"1.0"`；IGNORED 不进 `items[]`、不影响 `convergence_hash` / `status` / `recommended_next_action`。**显式拒绝**在 `missing.json` 加 `ignored_count` 顶层字段——对无 Agent 消费者的字段做契约迁移属 YAGNI。未来若真有 supervisor agent 需要可观察性，向后兼容的 optional 字段随时可加，非 blocking。

**下游影响**：`scripts/core/` 是 `~/reimbursement-helper/backend/agent/utils/` 的快照。本次改动只在本地 fork，下游 reimbursement-helper 在 IGNORED 过滤后只会收到正常发票，prompt 改动对下游 pure win；未来 snapshot sync 把这两条改动（prompts.py rule + classify.py narrow gate + fallthrough）一起推上游即可。

**不要再做**：
- 回加「docType 含 Invoice/Statement 就是 HOTEL_FOLIO」的宽松逻辑——这是 Termius bug 的根因。
- 在 narrow gate 加 `balance`——Stripe 类发票 `Amount due` 会误过。
- 在 narrow gate 加 `arrivalDate / departureDate`——订阅区间会误过。这些应由 prompt 层（Unit 0 rule）防御。
- 同步 `~/reimbursement-helper/backend/agent/utils/` 时覆盖本地改动。注意 `scripts/core/__init__.py` 的 Modifications from source 清单和本条 Lessons Learned。

**验证工具**：`scripts/dev/replay_classify.py` committed 供 regression check——扫 `~/.cache/gmail-invoice-downloader/ocr/*.json` 跑旧 / 新 classify 差集，附带 sha256→pdf_path 反查。差集含合法水单 → 固化到 `tests/fixtures/ocr/legitimate_folios/*.json` + 扩 `TestHotelFolioNarrowGate` 锁定。差集含 SaaS（Termius / Anthropic / OpenRouter）→ 预期效果，接受。

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

酒店发票可能从 3 种渠道来：酒店系统邮箱 / 前台个人邮箱（如 `1XXXXXXXXXX@163.com` — 前台手机号作邮箱前缀）/ 百望云代发，**不能只看 sender**。

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
| `scripts/doctor.py` | v5.3 preflight 检查（Gmail / LLM / pdftotext / cache 目录）|
| `scripts/postprocess.py` | 下载后的分析 / 重命名 / 匹配 / CSV / missing.json / zip（v5.3 引入）|
| `scripts/gmail-auth.py` | 一次性 OAuth 授权，生成 `token.json` |
| `scripts/gmail-helper.py` | 快速 Gmail 搜索调试（`gmail-helper.py "query"`）|
| `scripts/invoice_helpers.py` | 纯函数库（classify / extract / validate / normalize）|
| `scripts/probe-platform.py` | 遇到未知平台 URL 时用于 reverse engineering：诊断跳转链 + 猜测 API + 下步建议 |
| `scripts/core/` | v5.3 从 reimbursement-helper 复制的 LLM OCR / 分类 / 匹配模块（见 §Lessons Learned v5.3）|

## References

| 文件 | 内容 |
|------|------|
| `references/setup.md` | Gmail API 申请 + OAuth 首次授权流程 |
| `references/platforms.md` | 百望云 / fapiao.com / xforceplus / 滴滴 / 12306 / Marriott 等平台下载细节 |
| `learned_exclusions.json` | 噪音发件人/主题排除规则（用户可编辑）|
