---
date: 2026-05-02
topic: ignore-non-reimbursable-receipts-review-integration
supersedes: docs/brainstorms/2026-05-01-ignore-non-reimbursable-receipts-requirements.md
amends_plan: docs/plans/2026-05-01-003-feat-ignore-non-reimbursable-receipts-plan.md
review_source: 飞书文档 GWf0dx50KoBiwsxH70lcBvwOnOh (Agama, 2026-05-02)
---

# 非报销票据 IGNORED 分类 — 评审整合增量

## Problem Frame

原 brainstorm（2026-05-01）和原 Plan（`003-feat-ignore-non-reimbursable-receipts`, status=active, 未实施）已经完成"fallthrough UNKNOWN → IGNORED + 水单 docType-only 分支收窄"的基本设计。本文件是**评审整合增量**——在保持原 Plan 骨架不动的前提下，把飞书评审（Agama）的 4 条补充、我和用户二轮 brainstorm 的 2 条新发现、1 条显式拒绝，整合进原 Plan 的 6 个 Unit。

触发二轮 brainstorm 的三个信号：

1. **飞书评审**给原 Plan 打 4–5 星，但提了 4 条补充（闭环 CTA、replay 反查表、`ignored_count` 字段、版号风格）
2. **用户提供 7 份合法水单 + 2 份 SaaS 样本**，让我实际核对原 Plan 里"5 字段 OR gate"是否会误踢合法样本
3. **用户指出下游无阻碍**：「本次过滤解决后，给 reimbursement 插件的就都是正常发票了，不需要那边同步更新」——破除了我之前"prompt 改动要去 upstream 协调"的保守顾虑

## Scope of This Document

本文件**不重写**原 Plan。它是一份 "diff 式增量" ——对原 Plan 6 个 Unit 的修订、新增、补完逐条定位。writing-plans 阶段把这份增量和原 Plan 合并成新的实施计划文档（编号递增，如 `2026-05-02-007-...-plan.md`）。

原 Plan 文件保留不动作为历史基线，新 Plan 在 Overview 注明 `supersedes: 003-plan`。

## Key Findings From Sample Analysis

### 合法水单样本（9 份）共性

| 样本 | hotelName | confirmationNo | internalCodes | roomNumber | arrivalDate | departureDate | 触发路径 |
|---|---|---|---|---|---|---|---|
| 无锡 Marriott | ✅ | ✅ | ✅ (Opera) | ✅ | ✅ | ✅ | `is_hotel_folio_by_fields` 3/3 |
| 中卫简版 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | fields 3/3 |
| DoubleTree 无锡 | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | fields 3/3 |
| 南京 12.02（扫描） | 依赖 LLM 多模态 | | | | | | 预期 fields 路径 |
| Sofitel 广州 | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | fields 3/3 |
| Pullman 无锡 | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | fields 3/3 |
| 230920 南京（扫描旋转） | 依赖 LLM 多模态 | | | | | | 预期 fields 路径 |
| 飞猪账单/水单 | ✅ | ✅ (订单号) | ❌ | ❌ | ✅ | ✅ | fields 2/3 |
| Sofitel 南京（扫描） | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | fields 3/3 |

**关键结论**：**所有**可文本提取的合法水单都走 `is_hotel_folio_by_fields` 强特征路径（3-choose-2 of `roomNumber / arrivalDate / departureDate`），**根本触不到** `is_hotel_folio_by_doctype` docType narrow gate。收紧 narrow gate 对合法水单零回归风险。

### SaaS 样本（Termius + Anthropic）逃逸通道

| 通道 | Termius | Anthropic | 应由哪层防御 |
|---|---|---|---|
| docType 路径（`is_hotel_folio_by_doctype`）被 "Statement"/"Invoice" 关键字命中 | 是（2025Q4 实观察） | 疑似 | **Unit 1 narrow gate** 收紧 |
| fields 路径（`is_hotel_folio_by_fields`）被订阅区间 `Nov 12, 2025 – Nov 12, 2026` 填进 `arrivalDate + departureDate` | **理论存在**，未观察到 | 不适用（单日 `Date of issue`） | **Unit 0 prompt 层** 条件抽取 |
| `balance` 被 "Amount paid / Balance" 语义误填 | 可能 | 可能 | 不加进 narrow gate 就行 |
| `confirmationNo` 被 "Invoice number" 误填 | 可能 | 可能 | narrow gate 阈值 ≥2（单字段幻觉无法过 gate）|

### 源 PDF 原文日期标签

- **所有** 9 份合法水单原文都含至少一个酒店标签（`Arrival / Departure / 入住日期 / 抵店日期 / 离店日期 / 退房日期 / 入离日期 / Room No. / 房号` 等）
- **所有** SaaS 样本原文**不含**任何酒店标签（只有 `Date of issue / Date due / Date paid / Subscription period`）

这个信号的干净度让 prompt-层条件抽取成为可行方案。

## Integration Diff vs Original Plan

### Unit 0（新增）：prompt 层酒店字段条件抽取

**Goal**: 从 OCR 源头让 LLM 不把订阅期间 / 账单周期 / 开票到期日等非酒店入住场景的日期填进 `arrivalDate / departureDate / checkInDate / checkOutDate`，也不把非酒店场景编号填进 `roomNumber`。堵 `is_hotel_folio_by_fields` 3-choose-2 被订阅发票触发的潜在逃逸。

**Requirements trace**: 新增 R5（见下）

**Files**:
- Modify: `scripts/core/prompts.py` — 在 hotel-specific 字段块附近加一条 rule
- Modify: `scripts/core/__init__.py` — Modifications from source 清单 +1 条
- Modify: `SKILL.md` — Unit 5 的 `🟢 v5.7` Lessons Learned 条目里附带说明（不另开条目）
- Test: `tests/test_postprocess.py` 或新增 `TestPromptContract` — 子串检查 rule 存在（防下次 snapshot sync 无声覆盖）

**Rule 草案**:

> **Hotel-specific field conditional extraction**:
> `arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` MUST be populated **only** when the source PDF contains explicit hotel-domain labels near the value, including: `Arrival / Departure / Check-in / Check-out / Check in / Check out / 入住日期 / 抵店日期 / 离店日期 / 退房日期 / 到达日期 / 离开日期 / 入离日期 / Room No. / Room Number / 房号 / 房间号 / 房间号码`.
>
> If a date appears only in non-hotel contexts such as subscription period, service period, billing cycle, date of issue, date due, date paid, or payment history, these fields MUST remain `null`. Do NOT infer or guess.

**Rationale**: 下游 reimbursement-helper 消费的是经 IGNORED 过滤后的干净发票，不会收到 SaaS 订阅；prompt 改动对下游 pure win，本地 fork 零成本；未来 snapshot sync 可同步推给 upstream，双方都受益。这条 decision 是本次整合的关键洞察——原 brainstorm 的 "不改 prompts.py" Scope Boundary 在"下游只见干净输入"的前提下失效。

**Test scenarios**:
- `TestPromptContract::test_hotel_field_conditional_rule_present` — prompt 字符串包含关键子串（`"subscription period"` / `"date due"` / `"入离日期"` 等），防止 snapshot sync 丢失
- （可选回归）用 Termius fixture 在真 LLM 下跑一次，断言返回的 `arrivalDate / departureDate / roomNumber` 都是 null

---

### Unit 1（修订）：narrow gate 字段集 + 阈值

**原 Plan 写法（已失效，请在新 Plan 替换）**:

```python
if invoice.get('hotelName') or invoice.get('confirmationNo') \
   or invoice.get('internalCodes') or invoice.get('roomNumber') \
   or invoice.get('balance') is not None:
    category = 'HOTEL_FOLIO'
```

**修订后**:

```python
hotel_field_signals = sum([
    bool(invoice.get('hotelName')),
    bool(invoice.get('confirmationNo')),
    bool(invoice.get('internalCodes')),
    bool(invoice.get('roomNumber')),
])
if hotel_field_signals >= 2:
    category = 'HOTEL_FOLIO'
# else: fall through to IGNORED
```

**字段选择的理由**:

- `balance` **移出**：Stripe / Termius 类发票有 "Amount paid / Amount due / Balance" 语义，LLM 容易幻觉填入——原 Termius bug 最可能的变体通道
- `arrivalDate / departureDate` **不加**：它们归 `is_hotel_folio_by_fields` 3-choose-2 处理；narrow gate 是 fields 路径之后的兜底，重复加进去只会扩大"LLM 误填订阅区间"的攻击面。Termius `Nov 12, 2025 – Nov 12, 2026` 是典型 2-date 攻击面，防御应在 prompt 层（Unit 0），不在 classifier 层
- **阈值 ≥2**：单一字段幻觉（如孤立 `confirmationNo ← "Invoice number"` 或 `hotelName ← "Termius Corporation"`）无法过 gate；9 份合法水单走 fields 路径根本触不到 narrow gate，收紧对合法水单零回归

**新测试 `TestHotelFolioNarrowGate`**（替代原 Plan 的相关 edge case）:

| Input | Expected | 说明 |
|---|---|---|
| `{docType: "Statement", hotelName: "Marriott Shanghai", confirmationNo: "123456"}` | `HOTEL_FOLIO` | 2/4 通过 |
| `{docType: "Statement", hotelName: "Marriott Shanghai"}` | `IGNORED` | 1/4 |
| `{docType: "Invoice", hotelName: "Termius Corporation"}` | `IGNORED` | Termius 单字段幻觉场景，1/4 |
| `{docType: "Guest Folio", balance: 1260.0}` | `IGNORED` | balance 已移出字段集 |
| `{docType: "Statement", roomNumber: "1205", confirmationNo: "86690506"}` | `HOTEL_FOLIO` | 2/4 通过 |

**注意**：`is_hotel_folio_by_fields` 3-choose-2 强特征路径 **本 Unit 不动**——它已被 Unit 0 的 prompt rule 间接防御（订阅区间不会填进 arrivalDate/departureDate）。

---

### Unit 1 Verification 补完：replay 脚本 sha256 反查表

**来源**: 飞书评审补充 #2

**动机**: 原 Plan 让 `scripts/dev/replay_classify.py` 遍历 `~/.cache/gmail-invoice-downloader/ocr/*.json` 输出新旧分类差集，但 OCR cache 只存 `{"ocr": ..., "schema_version": ...}`，**不存 source PDF path**。差集里"这条记录对应哪个 PDF"需要 sha 反查，原 Plan 没给方案。

**补完伪代码**:

```python
# scripts/dev/replay_classify.py
import hashlib, pathlib, json, glob, os, sys
from typing import Dict, List

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core.classify import classify_invoice as classify_new
# 内嵌或引一份 legacy classify_invoice 用于对比

sha_to_path: Dict[str, List[str]] = {}
for pdf in glob.glob(os.path.expanduser("~/invoices/**/pdfs/*.pdf"), recursive=True):
    with open(pdf, "rb") as f:
        sha16 = hashlib.sha256(f.read()).hexdigest()[:16]
    sha_to_path.setdefault(sha16, []).append(pdf)

cache_dir = os.path.expanduser("~/.cache/gmail-invoice-downloader/ocr")
diffs = []
for cache_file in glob.glob(f"{cache_dir}/*.json"):
    sha16 = pathlib.Path(cache_file).stem
    ocr = json.load(open(cache_file))["ocr"]
    old_cat = classify_legacy(ocr)
    new_cat = classify_new(ocr)
    if old_cat != new_cat:
        paths = sha_to_path.get(sha16, ["<orphan OCR cache (PDF not in ~/invoices)>"])
        diffs.append((old_cat, new_cat, paths))

for old_cat, new_cat, paths in sorted(diffs):
    print(f"{old_cat} → {new_cat}")
    for p in paths:
        print(f"  {p}")
```

**关键点**: `setdefault([]).append(...)` 因为跨季度同 PDF 重复下载 → 一个 sha16 可能对应多个 path。

**差集处理**:
- 空 → 保留脚本，跳过后续
- 非空且含合法水单 → 固化 fixture 到 `tests/fixtures/ocr/legitimate_folios/*.json`，加 `TestHotelFolioNarrowing` 锁定
- 全是 SaaS（预期） → 接受，无后续

**新场景（评审未提，我补的）**: 若差集出现 `HOTEL_FOLIO → IGNORED` 且 PDF 是 SaaS（Termius / Anthropic），说明 Unit 0 prompt + Unit 1 narrow gate 生效，印证成功。

---

### Unit 4 补完：learned_exclusions CTA

**来源**: 飞书评审补充 #1

**动机**: 原 Plan 的 OpenClaw summary 只说"已忽略 3 张"，没有**可执行建议**。Step 6 + OCR 已经花了钱跑完 Anthropic/Termius 才判 IGNORED，下次跑同一季度还会再花一次（除非 cache 命中）。更高效的做法是 IGNORED 发生后，在报告末尾主动提示用户把 sender 加进 `learned_exclusions.json` → 下次 Step 1 Gmail 搜索直接过滤 → **0 LLM 成本 + 0 下载**。

**渲染形态**:

```markdown
## 📭 已忽略的非报销票据 (3)

以下票据被识别为非发票 / 非水单 / 非行程单，已自动过滤...

- billing@termius.com：¥120.00
- receipts@openrouter.ai：¥20.00
- invoice@anthropic.com：¥20.00

💡 下次避免 OCR 成本：可把这些 sender 加到 learned_exclusions.json

    -from:termius.com       # 已过滤 1 次
    -from:openrouter.ai     # 已过滤 1 次
    -from:anthropic.com     # 已过滤 1 次
```

**聚合规则**:
- key = `sender_email.split("@", 1)[-1]`（domain）
- N ≥ 1 即渲染 CTA（narrow gate + prompt 双闸把 IGNORED 误报率压得够低，跟评审原意一致）
- 每行格式：`-from:<domain>       # 已过滤 N 次`
- 空 sender_email 记录不进 CTA（它们仍出现在上方 sender 列表里为"未知发件人"）

**OpenClaw summary 末尾追加**:
```
📭 已忽略 {N} 张非报销票据（详见下载报告.md §已忽略的非报销票据）
```
（N=0 整行省略）

**落地位置**: Unit 4 的 Approach 里，`write_report_v53` 的"已忽略"章节在 sender 列表之后紧跟 CTA 块；`print_openclaw_summary` 的 Deliverables 行之前加指针。

**新测试 `TestIgnoredCtaRendering`**（或扩 `TestMatchingTiersContract`）:

| Scenario | Expected |
|---|---|
| 3 条 IGNORED（Termius + OpenRouter + Anthropic 各 1） | CTA 含 3 行 `-from:`，按 domain 字母序 |
| 同 domain 2 条 IGNORED（Termius Pro + Termius Corp） | `-from:termius.com       # 已过滤 2 次`（单行聚合） |
| 0 条 IGNORED | 报告完全不含"已忽略"字串 |
| IGNORED 含空 sender_email | CTA 不渲染该条目（仅在 sender 列表里出现"未知发件人"） |

---

### Unit 5 修订：SKILL.md Lessons Learned 版号 + 内容扩展

**来源**: 飞书评审补充 #4 + 本次整合

**修订**: 原 Plan 写 `🟢 2026-05 — IGNORED 白名单分类`，改为 **`🟢 v5.7 — IGNORED 白名单分类 + 非报销票据过滤`**（SKILL.md 当前 line 10 = v5.6，CHAT_* 已占用）。

**内容扩展**（覆盖本次增量双层设计）:

> **背景**：2025Q4 smoke 里 Termius 订阅发票（Stripe 模板 + 英文 docType "Invoice"）被 `is_hotel_folio_by_doctype` 的 "Statement"/"Invoice" 类关键字命中，滑入 HOTEL_FOLIO 管道永不匹配。
>
> **决策**（双层防御）：
> 1. **Prompt 层**（`scripts/core/prompts.py`）：`arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` 要求原文含明确酒店标签，否则保持 null。堵 `is_hotel_folio_by_fields` 3-choose-2 路径被订阅区间 / 账单周期日期误触发。
> 2. **Classifier 层**（`scripts/core/classify.py::classify_invoice`）：fallthrough 出口从 `UNKNOWN` 改为 `IGNORED`；`is_hotel_folio_by_doctype` 命中后 narrow gate 要求 **≥2 of {hotelName, confirmationNo, internalCodes, roomNumber}** — 故意不含 `balance`（SaaS 冲突）、不含 `arrivalDate/departureDate`（已由 prompt 层约束）。
>
> **IGNORED 记录处理**：`IGNORED_` 前缀保留在 output_dir，不进 CSV / zip / `missing.json.items[]`。报告末尾「已忽略」节追加 `learned_exclusions.json` 建议 CTA 让用户一次性加入 Gmail 过滤规则，下次跑省 OCR 成本。
>
> **Agent 合约不动**：`missing.json` schema 保持 `"1.0"`，不新增 `ignored_count` 字段（对无消费者字段做契约迁移属 YAGNI）。
>
> **不要再做**：回加"docType 含 Invoice/Statement 就是 HOTEL_FOLIO"的宽松逻辑。同步 `~/reimbursement-helper/backend/agent/utils/` 时，注意保留本地 `prompts.py` 的酒店字段条件抽取规则 + `classify.py` 的 narrow gate 收窄 / fallthrough 改名。

---

### 显式拒绝的提议

| 提议 | 来源 | 拒绝理由 |
|---|---|---|
| `missing.json` 顶层加 `ignored_count: int` | 飞书评审补充 #3 | 原 brainstorm Scope Boundaries 明确写"不为无 Agent 消费者的字段做契约迁移"；未来若真有 supervisor agent 出现，向后兼容的 optional 字段随时可加，非 blocking。保持 schema_version `"1.0"` 不动 |
| narrow gate 加 `balance` 作第 5 字段 | 原 Plan（本文件推翻） | Stripe/Termius 类 "Amount paid/due" 语义冲突；实际 9 份合法水单走 fields 路径根本触不到 narrow gate，balance 没必要 |
| narrow gate 加 `arrivalDate / departureDate` | 二轮 brainstorm 中间建议（本文件推翻） | 订阅区间 `Nov 12, 2025 – Nov 12, 2026` 是典型 2-date 攻击面；这两个字段的防御应在 prompt 层（Unit 0）而非 classifier 层 |
| 本次 Plan 不改 `scripts/core/prompts.py`（保持 pure snapshot） | 二轮 brainstorm 中间建议（本文件推翻） | 用户指出：本 Plan 落地后下游 reimbursement-helper 只看到干净发票，prompt 改动对下游 pure win，本地 fork 零成本 |

## Requirements (Delta)

在原 brainstorm R1–R4 基础上新增 R5：

- **R5（新）.** `scripts/core/prompts.py` 加 "Hotel-specific field conditional extraction" rule，要求 LLM 在 PDF 原文不含明确酒店标签时，`arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` 保持 null。改动登记在 `scripts/core/__init__.py` 的 Modifications from source 清单 + SKILL.md Lessons Learned v5.7 条目。

原 R1（classify）需要修订为反映 narrow gate 新条件（≥2 of 4 字段，去 balance）——具体修订词条见 Unit 1 修订节。

R2 / R3 / R4 **不变**。

## Success Criteria (Delta)

在原 brainstorm Success Criteria 基础上追加：

- Termius 样本在 prompt Unit 0 落地后重跑 OCR 时，返回的 `arrivalDate / departureDate / roomNumber` 字段都是 null（prompt 层生效）
- Termius 样本分类到 IGNORED，**不是**因为 balance 缺失，而是因为 narrow gate 4 字段中 ≤1 个命中（classifier 层生效）
- Anthropic / OpenRouter 样本同上行为
- 9 份合法水单样本在 `scripts/dev/replay_classify.py` 下旧→新 classify 差集均无 `HOTEL_FOLIO → IGNORED`（或若有，样本被固化为 fixture 锁定）
- 报告「已忽略」节按 domain 聚合并渲染 `-from:` CTA 行

## Unit Execution Order

```
Unit 0 (prompt rule)                [新增]   可独立先落地；低风险
  └─ Unit 1 (classify narrow gate)  [修订]   依赖 Unit 0 的 prompt 确保字段来源干净
       ├─ Unit 2 (sender 透传)      [原 Plan]
       └─ Unit 3 (rename + 切分)    [原 Plan]
            └─ Unit 4 (三路通知 + CTA) [补完]  依赖 Unit 3 ignored_records 可传
                 └─ Unit 5 (文档 + SKILL.md v5.7) [修订]
                      └─ Unit 6 (可选 UNKNOWN carve-out) [原 Plan]
```

**建议顺序**: 先 Unit 0 后 Unit 1。这样 Unit 1 的 replay verification 可以跑两遍（历史 cache / `--force` 重跑 cache），分离"prompt 改动贡献"和"classify 改动贡献"，差集更干净。

## Open Questions (New)

- **Q7（遗留风险）**: Unit 1 replay 脚本若在历史 `~/.cache/gmail-invoice-downloader/ocr/*.json` 里发现 legacy 已经走 `is_hotel_folio_by_fields` 命中的 SaaS 样本（旧 OCR 把 subscription 区间填进了 arrivalDate/departureDate），说明 prompt Unit 0 只对未来 OCR 生效，对历史缓存无效。**处理方案**: (a) 可选 `--force` flag 强行重跑一次 OCR 清旧缓存；(b) 文档里标注"历史缓存可能仍有历史误差，触发需要手动删对应 cache + 重跑"。Unit 1 实施时拍板。
- **Q8（扩展性）**: prompt Unit 0 的酒店标签列表目前是中 / 英枚举。遇到非中英文水单（日文 / 韩文酒店）需要扩列表。本次 Plan 不前瞻性覆盖，遇到再扩。

## Next Steps

→ `/superpowers:writing-plans` to create the integrated implementation plan file under `docs/plans/2026-05-02-007-...-plan.md`，基于本文件 + 原 003 Plan，最终产出一个完整可执行的新 Plan。
