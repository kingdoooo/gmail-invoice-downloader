---
title: 非「发票 / 水单 / 行程单」票据一律过滤（IGNORED 分类）— 评审整合版
type: feat
status: active
date: 2026-05-02
origin: docs/brainstorms/2026-05-02-ignore-non-reimbursable-receipts-review-integration-requirements.md
supersedes: docs/plans/2026-05-01-003-feat-ignore-non-reimbursable-receipts-plan.md
---

# 非报销票据 IGNORED 分类 — 评审整合实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 双层防御把非发票 / 水单 / 行程单票据（SaaS 订阅、营销收据等）过滤成 `IGNORED` 类别，既不进 CSV / zip / `missing.json.items[]`，又不造成假收敛；同时给用户 `learned_exclusions.json` CTA 让下次 Gmail 搜索直接过滤省 OCR 成本。

**Architecture:**
- **Unit 0（prompt 层）**：`scripts/core/prompts.py` 加酒店字段条件抽取 rule — LLM 只在原文有明确酒店标签时填 `arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber`，堵 Termius 类订阅区间触发 `is_hotel_folio_by_fields` 3-choose-2 路径的逃逸。
- **Unit 1（classifier 层）**：`scripts/core/classify.py::classify_invoice` 的 fallthrough 出口从 `UNKNOWN` 改 `IGNORED`，`is_hotel_folio_by_doctype` 的 docType-only 分支收紧为 `≥2 of {hotelName, confirmationNo, internalCodes, roomNumber}`。
- **Unit 2–4（下游）**：sender 字段穿透到 download record；`rename_by_ocr` 第三条 IGNORED 分支；main() 在 `valid_records` 入口切分 `ignored_records`；报告 + OpenClaw summary 三路通知 + `learned_exclusions` CTA；zip 排除 IGNORED_*.pdf。
- **Unit 5–6（文档 / 收尾）**：`scripts/core/__init__.py` + SKILL.md Lessons Learned v5.7 登记；可选 UNKNOWN 死代码清理。
- **Agent 合约不动**：`missing.json` schema_version `"1.0"`，不加 `ignored_count` 字段（显式拒绝评审补充 #3）。

**Tech Stack:** Python 3.10+, pytest, pdftotext (validation.py), boto3 / anthropic / openai SDKs (OCR provider-agnostic).

---

## File Structure

### 新建

- `scripts/dev/replay_classify.py` — Unit 1 Verification 工具。一次性离线回放脚本，扫 `~/.cache/gmail-invoice-downloader/ocr/*.json` 跑新旧 classify 对比，差集附带 sha256→pdf_path 反查结果（committed，供未来 snapshot sync 复用）。
- `tests/fixtures/ocr/legitimate_folios/` — 目录占位。若 Unit 1 replay 差集里有合法水单被误踢，脱敏后固化于此。**本 Plan 目录创建，样本按需添加**。

### 修改

| 文件 | 修改范围 | Unit |
|---|---|---|
| `scripts/core/prompts.py` | `get_ocr_prompt()` 新增 "Hotel-specific field conditional extraction" rule 段落（插在 "酒店水单(Guest Folio)专用字段" 段落之后） | 0 |
| `scripts/core/classify.py` | `:339-341` docType narrow gate 改 ≥2 of 4 字段；`:375-377` fallthrough `UNKNOWN` → `IGNORED`；文件头 MODIFIED 注释追加条目 | 1 |
| `scripts/core/__init__.py` | Modifications from source 清单追加 2 条（prompt rule + classify changes）；加一行指向 SKILL.md Lessons Learned | 0 / 1 / 5 |
| `scripts/invoice_helpers.py` | `classify_email` 返回 dict 追加 `"sender_email"` 键 | 2 |
| `scripts/download-invoices.py` | `download_attachment` / `download_zip` / `download_link` 三处 record 构造加 `"sender"` + `"sender_email"`；`write_report_md` 签名加 `ignored_records=None` 关键字参数 + 渲染「已忽略」节 + CTA 块；`main` 在 `valid_records` 构造后切分 `ignored_records` / `reimbursable_records`，调用点传 `ignored_records` / `ignored_count` | 2 / 3 / 4 |
| `scripts/postprocess.py` | `CATEGORY_LABELS` 加 `"IGNORED": "已忽略"`；`rename_by_ocr` 新增第三条 IGNORED 分支；`print_openclaw_summary` 加 `ignored_count: int = 0` 参数 + 末尾渲染；`zip_output` 加 `IGNORED_*.pdf` 前缀过滤；`build_aggregation` 入口防御性断言 no IGNORED | 3 / 4 |
| `SKILL.md` | Lessons Learned 插入 `🟢 v5.7` 条目（在 `🟢 v5.3 — scripts/core/ 是快照` 之后、`🔴 12306` 之前） | 5 |
| `tests/test_postprocess.py` | 新增 `TestClassifyIgnored`、`TestHotelFolioNarrowGate`、`TestPromptContract`、`TestRenameIgnoredBranch`、`TestIgnoredCtaRendering`、扩 `TestPrintOpenClawSummary` / `TestZipAtomic` / `TestCategoryConstants` | 0-4 |
| `tests/test_agent_contract.py` | `TestMatchingTiersContract` 和 `TestMissingJsonSchemaContract` 回归（schema 不变，`ignored_count` 显式不加） | 4 |

---

## Requirements Trace

- **R1.** `classify_invoice` 新增合法返回值 `"IGNORED"`；fallthrough 默认改 `"IGNORED"`；`is_hotel_folio_by_doctype` 分支收紧为 `≥2 of {hotelName, confirmationNo, internalCodes, roomNumber}`。→ Unit 1
- **R2.** `rename_by_ocr` IGNORED 分支 `IGNORED_{sender_short}_{原名}.pdf`；`main()` 入口 `ignored_records / reimbursable_records` 切分；`CATEGORY_LABELS["IGNORED"] = "已忽略"`；`CATEGORY_ORDER` 不登记（落到默认 `get(..., 50)`）。→ Unit 3
- **R3.** 下载报告末尾「已忽略的非报销票据 (N)」节 + `learned_exclusions.json` CTA 块（N=0 整节省略）；`print_openclaw_summary` 追加「📭 已忽略 {N} 张」一行（N=0 省略）；`zip_output` 排除 `IGNORED_*.pdf`（保留 `UNPARSED_*.pdf`）。→ Unit 4
- **R4.** `missing.json` schema_version `"1.0"` 不动；IGNORED 不进 `items[]`；`convergence_hash` / `status` / `recommended_next_action` 不变；**不**加 `ignored_count` 顶层字段。→ Unit 3 / Unit 4 / 显式拒绝
- **R5（新）.** `scripts/core/prompts.py` 加 "Hotel-specific field conditional extraction" rule；LLM 在 PDF 原文无明确酒店标签时 `arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` 保持 null。→ Unit 0

---

## Scope Boundaries

- **不**改 `scripts/core/matching.py`（矩配算法无关）。
- **不**引 `learned_exclusions.json` 的新规则（两者正交保留，CTA 只建议不自动写）。
- **不**新增 LLM 调用（IGNORED 判定纯消费 OCR 字段）。
- **不**改 `scripts/invoice_helpers.py::classify_email` 的决策树（只扩返回 dict）。
- **不**动 Agent 合约：`status / recommended_next_action / convergence_hash / schema_version` 全保持当前语义和字面量。
- **不**自动写 `learned_exclusions.json`——只 CTA 给用户 `-from:xxx` 建议行。
- **不**加 `missing.json.ignored_count`——显式拒绝评审补充 #3，保持 `missing.json` schema 对 IGNORED 完全透明。

---

## Unit 执行顺序

```
Unit 0 (prompt rule)               [独立，可先落地]
  └─ Unit 1 (classify narrow gate + replay) [依赖 Unit 0 字段语义]
       ├─ Unit 2 (sender 透传)     [和 Unit 1 可并行]
       └─ Unit 3 (rename + 切分)   [依赖 Unit 1 + Unit 2]
            └─ Unit 4 (三路通知 + CTA) [依赖 Unit 3 ignored_records 可传]
                 └─ Unit 5 (文档 + SKILL.md v5.7) [依赖 Unit 0-4 全部落地]
                      └─ Unit 6 (可选 UNKNOWN carve-out cleanup) [观察一次真实运行后再决定]
```

---

## Unit 0: Prompt 层酒店字段条件抽取

**Goal:** 在 `scripts/core/prompts.py` 的 OCR prompt 里加一段规则，让 LLM 在 PDF 原文不含明确酒店标签时，`arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` 字段保持 null。堵 `is_hotel_folio_by_fields` 3-choose-2 强特征路径被 Termius 类订阅发票的 "Nov 12, 2025 – Nov 12, 2026" 区间误触发。

**Requirements:** R5

**Dependencies:** 无（可最先落地）

**Files:**
- Modify: `scripts/core/prompts.py`（`get_ocr_prompt` 函数的 "酒店水单(Guest Folio)专用字段" 段落之后插入 rule）
- Modify: `scripts/core/__init__.py`（Modifications from source 清单追加一条）
- Test: `tests/test_postprocess.py`（新增 `TestPromptContract` 类）

### Steps

- [ ] **Step 0.1: 写 prompt 子串合约测试（先让它失败）**

  在 `tests/test_postprocess.py` 末尾追加：

  ```python
  class TestPromptContract:
      """Unit 0 R5: guard the hotel-field conditional extraction rule in prompts.py.

      The rule prevents LLM from filling arrival/departure/room fields from
      non-hotel contexts (subscription period, date due, etc.), which would
      otherwise trigger is_hotel_folio_by_fields 3-choose-2 on SaaS invoices.
      Keep these substrings synced with the rule text in prompts.py.
      """

      def test_hotel_conditional_rule_present(self):
          from core.prompts import get_ocr_prompt
          prompt = get_ocr_prompt()
          # Core rule phrasing — do not remove during snapshot sync with
          # reimbursement-helper without re-reading SKILL.md Lessons Learned v5.7.
          required_substrings = [
              "Hotel-specific field conditional extraction",
              "subscription period",
              "date due",
              "入离日期",
              "房号",
              "MUST remain `null`",
          ]
          for s in required_substrings:
              assert s in prompt, f"Prompt missing required substring: {s!r}"
  ```

- [ ] **Step 0.2: 跑测试验证失败**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestPromptContract::test_hotel_conditional_rule_present -v
  ```

  Expected: FAIL — `AssertionError: Prompt missing required substring: 'Hotel-specific field conditional extraction'`

- [ ] **Step 0.3: 把 rule 插入 prompt**

  打开 `scripts/core/prompts.py`，在 "## 酒店水单(Guest Folio)专用字段" 段落的 `**⚠️ transactionDate 取值规则（酒店水单）：** ... 若 departureDate 无法识别（水单残缺或信息缺失），则 transactionDate 填 null，不要猜测或用 arrivalDate 代替。` 之后、`## 网约车发票专用字段` 之前插入：

  ```python
  ## ⚠️ Hotel-specific field conditional extraction (v5.7)

  `arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` MUST be populated **only** when the source PDF contains explicit hotel-domain labels near the value, including any of:
  - English: `Arrival`, `Departure`, `Check-in`, `Check-out`, `Check in`, `Check out`, `Room No.`, `Room Number`
  - Chinese: `入住日期`, `抵店日期`, `离店日期`, `退房日期`, `到达日期`, `离开日期`, `入离日期`, `房号`, `房间号`, `房间号码`

  If a date or number appears only in non-hotel contexts — such as subscription period, service period, billing cycle, date of issue, date due, date paid, or payment history — these fields MUST remain `null`. Do NOT infer, guess, or transcribe subscription ranges (e.g. "Nov 12, 2025 – Nov 12, 2026") into arrivalDate/departureDate.

  Rationale: downstream classifier uses these fields to distinguish hotel folios from SaaS invoices. Filling them without hotel-domain textual evidence causes non-reimbursable SaaS receipts to be misrouted into the hotel matching pipeline.
  ```

- [ ] **Step 0.4: 跑测试验证通过**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestPromptContract -v
  ```

  Expected: PASS

- [ ] **Step 0.5: 回归跑整套 OCR 测试**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestProviderMatrix tests/test_postprocess.py::TestHallucinationDetection -v
  ```

  Expected: 现有测试全部 PASS（prompt rule 纯追加，不改字段语义 / OCR cache key 格式）

- [ ] **Step 0.6: 登记到 `scripts/core/__init__.py`**

  打开 `scripts/core/__init__.py`，把 "Modifications from source:" 段落的 `- prompts.py (v5.5): ...` 条目改为（追加 v5.7 一段）：

  ```python
  - prompts.py (v5.5): folio transactionDate=departureDate rule with null fallback;
    itinerary applicationDate field + rule with null fallback; two new JSON
    sample blocks (folio + itinerary) with common-field reminder captions.
    (v5.7): added "Hotel-specific field conditional extraction" rule requiring
    arrivalDate/departureDate/checkInDate/checkOutDate/roomNumber to remain
    null when no hotel-domain label appears near the value. Prevents SaaS
    subscription ranges (Nov 12, 2025 – Nov 12, 2026) from triggering
    is_hotel_folio_by_fields 3-choose-2 on Termius-style invoices.
    PENDING upstream sync to ~/reimbursement-helper/backend/agent/utils/prompts.py.
    See SKILL.md § Lessons Learned "v5.7 — IGNORED 白名单分类" for rationale.
  ```

- [ ] **Step 0.7: Commit**

  ```bash
  git add scripts/core/prompts.py scripts/core/__init__.py tests/test_postprocess.py
  git commit -m "feat(prompts): hotel-field conditional extraction rule (v5.7 Unit 0)

  Require LLM to leave arrivalDate/departureDate/checkInDate/checkOutDate/
  roomNumber null when PDF has no hotel-domain labels. Prevents Termius-style
  subscription-range invoices from triggering is_hotel_folio_by_fields
  3-choose-2. Downstream reimbursement-helper sees only filtered invoices
  post this change; local fork is pure win."
  ```

---

## Unit 1: classify.py — fallthrough 改 IGNORED + 水单 narrow gate

**Goal:** 把 `classify_invoice` 的默认出口从 `"UNKNOWN"` 换成 `"IGNORED"`，并把 `is_hotel_folio_by_doctype` 分支收紧为 `≥2 of {hotelName, confirmationNo, internalCodes, roomNumber}`。通过 `scripts/dev/replay_classify.py` 离线回放验证水单收紧不误踢历史合法样本。

**Requirements:** R1

**Dependencies:** Unit 0（OCR 字段语义跟上新 prompt；但 replay 脚本消费的是已有 cache，`--force` flag 处理在 Step 1.9）

**Files:**
- Modify: `scripts/core/classify.py`（两处：`:339-341` narrow gate + `:375-377` fallthrough + 文件头 MODIFIED 注释）
- Modify: `scripts/core/__init__.py`（Modifications from source 清单再追加一条）
- Create: `scripts/dev/replay_classify.py`（committed artifact，供未来 snapshot sync 复用）
- Create: `tests/fixtures/ocr/legitimate_folios/` 目录（空占位；差集非空时填充）
- Test: `tests/test_postprocess.py`（新增 `TestClassifyIgnored` + `TestHotelFolioNarrowGate`）

### Steps

- [ ] **Step 1.1: 写 IGNORED fallthrough 测试（先让它失败）**

  在 `tests/test_postprocess.py` 末尾追加：

  ```python
  class TestClassifyIgnored:
      """Unit 1 R1: classify_invoice fallthrough returns IGNORED.

      Termius-shape SaaS invoices (English docType, no Chinese tax ID,
      no hotel fields, no service type match) fall through all priority
      branches and must land on IGNORED, not UNKNOWN.
      """

      def test_empty_invoice_returns_ignored(self):
          from core.classify import classify_invoice
          assert classify_invoice({}) == "IGNORED"

      def test_termius_shape_returns_ignored(self):
          from core.classify import classify_invoice
          invoice = {
              "isChineseInvoice": False,
              "vendorTaxId": None,
              "docType": "Invoice",
              "serviceType": None,
              "vendorName": "Termius Corporation",
              # No hotel fields
          }
          assert classify_invoice(invoice) == "IGNORED"

      def test_stripe_like_with_balance_but_no_hotel_fields_ignored(self):
          """The balance field alone must not gate a record into HOTEL_FOLIO.

          Stripe/Termius invoices commonly surface "Amount due/Amount paid",
          which LLMs may spill into 'balance'. Narrow gate must not trigger
          on that single signal.
          """
          from core.classify import classify_invoice
          invoice = {
              "docType": "Statement",
              "balance": 120.0,
              "vendorName": "Stripe-like SaaS",
          }
          assert classify_invoice(invoice) == "IGNORED"

      def test_chinese_meal_invoice_still_classifies_correctly(self):
          """Regression: pre-existing MEAL path unaffected by fallthrough rename."""
          from core.classify import classify_invoice
          invoice = {
              "isChineseInvoice": True,
              "vendorTaxId": "91320214MA1XXXXXX",
              "serviceType": "*餐饮服务*餐饮费",
              "docType": "电子发票（普通发票）",
          }
          assert classify_invoice(invoice) == "MEAL"


  class TestHotelFolioNarrowGate:
      """Unit 1 R1: is_hotel_folio_by_doctype narrow gate requires >=2
      of {hotelName, confirmationNo, internalCodes, roomNumber}.

      balance is deliberately NOT in the set (SaaS "Amount due" conflict).
      arrivalDate/departureDate are deliberately NOT in the set
      (Unit 0 prompt layer handles subscription-range leakage).
      """

      def test_two_fields_pass_narrow_gate(self):
          from core.classify import classify_invoice
          invoice = {
              "docType": "Statement",
              "hotelName": "Marriott Shanghai",
              "confirmationNo": "86690506",
          }
          assert classify_invoice(invoice) == "HOTEL_FOLIO"

      def test_one_field_fails_narrow_gate(self):
          from core.classify import classify_invoice
          invoice = {
              "docType": "Statement",
              "hotelName": "Marriott Shanghai",
              # Only 1 field — should fall through to IGNORED
          }
          assert classify_invoice(invoice) == "IGNORED"

      def test_termius_single_field_hallucination_still_ignored(self):
          """LLM might fill hotelName with 'Termius Corporation'. Single
          field is not enough to pass the gate.
          """
          from core.classify import classify_invoice
          invoice = {
              "docType": "Invoice",
              "hotelName": "Termius Corporation",
          }
          assert classify_invoice(invoice) == "IGNORED"

      def test_guest_folio_with_balance_only_is_ignored(self):
          """balance is NOT in the narrow-gate field set. A folio that
          arrives with only balance filled falls through to IGNORED.
          Previous 5-field proposal (with balance) is deliberately rejected
          — see brainstorm 2026-05-02 for rationale.
          """
          from core.classify import classify_invoice
          invoice = {
              "docType": "Guest Folio",
              "balance": 1260.0,
          }
          assert classify_invoice(invoice) == "IGNORED"

      def test_room_and_confirmation_pass(self):
          from core.classify import classify_invoice
          invoice = {
              "docType": "Statement",
              "roomNumber": "1205",
              "confirmationNo": "86690506",
          }
          assert classify_invoice(invoice) == "HOTEL_FOLIO"

      def test_fields_path_still_works_unchanged(self):
          """is_hotel_folio_by_fields 3-choose-2 of roomNumber/arrivalDate/
          departureDate is untouched by Unit 1; legit folios go through this
          path unaffected.
          """
          from core.classify import classify_invoice
          invoice = {
              "docType": "any",  # docType narrow gate not reached
              "roomNumber": "1205",
              "arrivalDate": "2025-11-12",
              "departureDate": "2025-11-13",
          }
          assert classify_invoice(invoice) == "HOTEL_FOLIO"
  ```

- [ ] **Step 1.2: 跑测试验证失败**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestClassifyIgnored tests/test_postprocess.py::TestHotelFolioNarrowGate -v
  ```

  Expected: 多条 FAIL（`AssertionError: 'UNKNOWN' == 'IGNORED'` 等）。`test_fields_path_still_works_unchanged` 应该 PASS（现有行为未改）。

- [ ] **Step 1.3: 修改 `classify.py` 两处**

  打开 `scripts/core/classify.py`。

  **改动 A**（`:339-341` docType narrow gate）：

  找到：

  ```python
      # 1.4 Hotel folio by docType keywords
      if not category and is_hotel_folio_by_doctype(doc_type):
          category = 'HOTEL_FOLIO'
  ```

  替换为：

  ```python
      # 1.4 Hotel folio by docType keywords — narrowed to >=2 of 4 fields
      # (v5.7): protects against Termius-style SaaS invoices where docType
      # hallucinates to "Statement" or "Invoice" but no hotel-domain
      # structural fields exist. balance deliberately excluded (SaaS
      # "Amount due" conflict); arrivalDate/departureDate excluded because
      # the fields path (is_hotel_folio_by_fields, 3-choose-2) already
      # covers them — re-adding here would widen attack surface against
      # subscription-range leakage.
      if not category and is_hotel_folio_by_doctype(doc_type):
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

  **改动 B**（`:375-377` fallthrough rename）：

  找到：

  ```python
      # ========== Priority 4: UNKNOWN ==========
      if not category:
          category = 'UNKNOWN'
  ```

  替换为：

  ```python
      # ========== Priority 4: IGNORED ==========
      # (v5.7) fallthrough renamed from UNKNOWN. Non-invoice / non-folio /
      # non-itinerary documents (SaaS subscriptions, marketing receipts,
      # bank statements) land here and are filtered out of CSV/zip/
      # missing.json.items[] in downstream units.
      if not category:
          category = 'IGNORED'
  ```

  **改动 C**（文件头 `:13-18` MODIFIED 注释追加）：

  找到：

  ```python
  MODIFIED for gmail-invoice-downloader v5.3:
  - Removed COFFEE_KEYWORDS and MEAL_TYPES (non-deterministic random assignment
    was useful for Concur reimbursement, irrelevant for Gmail aggregation use case).
  - Removed is_coffee_vendor() and detect_meal_type() functions.
  - All meal-service invoices now classified as MEAL (no early/mid/late/coffee subtype).
  """
  ```

  替换为：

  ```python
  MODIFIED for gmail-invoice-downloader v5.3:
  - Removed COFFEE_KEYWORDS and MEAL_TYPES (non-deterministic random assignment
    was useful for Concur reimbursement, irrelevant for Gmail aggregation use case).
  - Removed is_coffee_vendor() and detect_meal_type() functions.
  - All meal-service invoices now classified as MEAL (no early/mid/late/coffee subtype).

  MODIFIED for gmail-invoice-downloader v5.7:
  - Fallthrough default changed from 'UNKNOWN' to 'IGNORED' (whitelist the three
    reimbursable formats; filter everything else).
  - is_hotel_folio_by_doctype narrow gate tightened: now requires >=2 of
    {hotelName, confirmationNo, internalCodes, roomNumber}. balance
    deliberately excluded (SaaS Amount-due conflict); arrival/departure
    deliberately excluded (covered by fields path + prompt-layer rule).
  - classify_invoice is confidence-blind by design — downstream validation
    flags from validate_ocr_plausibility are additive and do not feed
    classification. Any future upstream confidence-aware logic must update
    scripts/dev/replay_classify.py.
  """
  ```

- [ ] **Step 1.4: 跑测试验证新行为**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestClassifyIgnored tests/test_postprocess.py::TestHotelFolioNarrowGate -v
  ```

  Expected: 所有测试 PASS

- [ ] **Step 1.5: 跑既有 classifier + 匹配回归测试**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestHotelMatchingTiers tests/test_postprocess.py::TestCategoryConstants -v
  ```

  Expected: 全部 PASS（`TestCategoryConstants` 的 `UNPARSED: 99` invariant 不变；`CATEGORY_LABELS["UNKNOWN"]` 仍然在，未删除）

- [ ] **Step 1.6: 新建 `scripts/dev/replay_classify.py`**

  **先确认目录存在**：

  ```bash
  ls scripts/dev/ 2>&1 || mkdir -p scripts/dev/
  ```

  **新建 `scripts/dev/replay_classify.py`**，内容：

  ```python
  #!/usr/bin/env python3
  """Offline replay: compare old vs new classify_invoice on cached OCR results.

  Unit 1 Verification artifact (committed). Scans ~/.cache/gmail-invoice-downloader/
  ocr/*.json, runs both the legacy classify (fallthrough → 'UNKNOWN', 1-field
  narrow gate with balance) and the v5.7 classify (fallthrough → 'IGNORED',
  >=2-of-4 narrow gate without balance), prints the diff set with PDF paths
  resolved via sha256 reverse lookup against ~/invoices/**/pdfs/*.pdf.

  Usage:
      python3 scripts/dev/replay_classify.py
      python3 scripts/dev/replay_classify.py --force-rerun-ocr  # Unit 0 + 1

  Interpretation:
  - empty diff → no behavior change on cached samples; keep script for next
    snapshot sync.
  - diff contains HOTEL_FOLIO → IGNORED on legitimate folios → freeze those
    samples as pytest fixtures under tests/fixtures/ocr/legitimate_folios/
    and tighten narrow gate if needed.
  - diff contains HOTEL_FOLIO → IGNORED on SaaS invoices (Termius / Anthropic
    / OpenRouter etc.) → expected; Unit 0 + Unit 1 worked.
  """

  import argparse
  import glob
  import hashlib
  import json
  import os
  import pathlib
  import sys
  from typing import Any, Dict, List

  sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
  from core.classify import classify_invoice as classify_new  # noqa: E402


  def classify_legacy(invoice: Dict[str, Any]) -> str:
      """Snapshot of the pre-v5.7 classify_invoice (fallthrough = UNKNOWN,
      docType narrow gate used only >=1 of {hotelName, confirmationNo,
      internalCodes, roomNumber, balance}).

      Inlined so the replay runs against a known baseline even after the
      real classify.py has moved on. Do NOT import from core.classify.
      """
      from core.classify import (
          is_chinese_invoice_document,
          is_hotel_folio_by_doctype,
          is_hotel_folio_by_fields,
          is_hotel_service,
          is_meal_service,
          is_mobile_service,
          is_ridehailing_receipt,
          is_ridehailing_service,
          is_taxi_invoice_by_doctype,
          is_tolls_service,
          is_train_ticket,
      )
      service_type = invoice.get('serviceType', '') or ''
      doc_type = invoice.get('docType', '') or ''
      invoice_code = invoice.get('invoiceCode', '') or ''
      vendor_name = invoice.get('vendorName', '') or ''
      tax_id = invoice.get('vendorTaxId', '') or ''
      is_chinese_invoice = invoice.get('isChineseInvoice')

      category = None
      if is_ridehailing_receipt(doc_type):
          category = 'RIDEHAILING_RECEIPT'
      if not category and is_taxi_invoice_by_doctype(doc_type):
          category = 'TAXI'
      if not category and is_train_ticket(doc_type):
          category = 'TRAIN'
      if not category and is_hotel_folio_by_fields(invoice):
          category = 'HOTEL_FOLIO'
      # Legacy: docType-only (no narrow gate)
      if not category and is_hotel_folio_by_doctype(doc_type):
          category = 'HOTEL_FOLIO'
      if not category and invoice_code and len(invoice_code) == 12 and invoice_code.isdigit():
          category = 'TAXI'
      if not category and is_hotel_service(service_type):
          category = 'HOTEL_INVOICE' if is_chinese_invoice_document(is_chinese_invoice, tax_id) else 'HOTEL_FOLIO'
      if not category and is_ridehailing_service(service_type):
          category = 'RIDEHAILING_INVOICE' if is_chinese_invoice_document(is_chinese_invoice, tax_id) else 'RIDEHAILING_RECEIPT'
      if not category and is_meal_service(service_type):
          category = 'MEAL'
      if not category and is_mobile_service(service_type):
          category = 'MOBILE'
      if not category and is_tolls_service(service_type, vendor_name):
          category = 'TOLLS'
      if not category:
          category = 'UNKNOWN'  # legacy fallthrough
      return category


  def build_sha_lookup() -> Dict[str, List[str]]:
      """Scan ~/invoices/**/pdfs/*.pdf and build sha256[:16] -> [path] map.

      setdefault([]).append handles cross-quarter duplicate downloads
      (same PDF pulled in Q1 and Q2 batches).
      """
      sha_to_path: Dict[str, List[str]] = {}
      root = os.path.expanduser("~/invoices")
      if not os.path.exists(root):
          return sha_to_path
      for pdf in glob.glob(os.path.join(root, "**", "pdfs", "*.pdf"), recursive=True):
          try:
              with open(pdf, "rb") as f:
                  sha16 = hashlib.sha256(f.read()).hexdigest()[:16]
              sha_to_path.setdefault(sha16, []).append(pdf)
          except OSError:
              continue
      return sha_to_path


  def main() -> int:
      parser = argparse.ArgumentParser(description=__doc__)
      parser.add_argument(
          "--cache-dir",
          default=os.path.expanduser("~/.cache/gmail-invoice-downloader/ocr"),
      )
      args = parser.parse_args()

      if not os.path.exists(args.cache_dir):
          print(f"Cache dir not found: {args.cache_dir}", file=sys.stderr)
          return 2

      print("Building sha256 → pdf_path lookup from ~/invoices/**/pdfs/*.pdf ...",
            file=sys.stderr)
      sha_to_path = build_sha_lookup()
      print(f"  indexed {len(sha_to_path)} unique PDFs", file=sys.stderr)

      cache_files = sorted(glob.glob(os.path.join(args.cache_dir, "*.json")))
      print(f"Replaying {len(cache_files)} OCR cache entries ...", file=sys.stderr)

      diffs: List[tuple] = []
      for cache_file in cache_files:
          sha16 = pathlib.Path(cache_file).stem
          try:
              with open(cache_file, "r", encoding="utf-8") as f:
                  payload = json.load(f)
          except (OSError, json.JSONDecodeError):
              continue
          ocr = payload.get("ocr")
          if not ocr:
              continue
          old_cat = classify_legacy(ocr)
          new_cat = classify_new(ocr)
          if old_cat != new_cat:
              paths = sha_to_path.get(sha16, ["<orphan OCR cache (PDF not in ~/invoices)>"])
              diffs.append((old_cat, new_cat, sha16, paths))

      if not diffs:
          print("No classification diffs. Keeping replay script for next snapshot sync.")
          return 0

      # Group by (old, new) for readable output
      diffs.sort(key=lambda d: (d[0], d[1], d[2]))
      print(f"\n{len(diffs)} classification diffs:\n")
      for old_cat, new_cat, sha16, paths in diffs:
          print(f"{old_cat} → {new_cat}  (sha={sha16})")
          for p in paths:
              print(f"  {p}")
      return 0


  if __name__ == "__main__":
      sys.exit(main())
  ```

- [ ] **Step 1.7: 跑 replay 脚本（人工验证差集）**

  ```bash
  python3 scripts/dev/replay_classify.py
  ```

  **Expected** — 三种可能输出的处理：
  - **空差集** → 良好；cache 里无样本命中新规则改动。跳到 Step 1.8。
  - **差集全是 `HOTEL_FOLIO → IGNORED` 且 path 指向 SaaS PDF**（Termius / Anthropic / OpenRouter 等）→ 良好；Unit 0 + Unit 1 达到预期效果。跳到 Step 1.8。
  - **差集含 `HOTEL_FOLIO → IGNORED` 且 path 指向真实酒店 PDF** → **中止 Step 1.8**，先人工确认：如果这些样本真的是合法水单（可能因历史 OCR 只抽到 1 个 narrow-gate 字段），复制对应 `.json` 到 `tests/fixtures/ocr/legitimate_folios/<简短描述>.json`（脱敏：替换 `buyerName` / `sellerTaxId` 为占位符），并扩 `TestHotelFolioNarrowGate` 新增一组 fixture-based 参数化测试锁定。然后回到 Step 1.3 决定是否放宽 gate（如允许 `balance + 任一 date` 的联合条件），完成后重跑 Step 1.4-1.7。

- [ ] **Step 1.8: 新建 fixtures 目录占位**

  ```bash
  mkdir -p tests/fixtures/ocr/legitimate_folios
  touch tests/fixtures/ocr/legitimate_folios/.gitkeep
  ```

  这一步无论 replay 结果如何都要做——目录存在才能让未来的 sync 有地方放样本。

- [ ] **Step 1.9: 登记到 `scripts/core/__init__.py`**

  继续追加一条（在 Unit 0 已经改过的位置之后）：

  ```python
  - classify.py (v5.7): fallthrough changed from 'UNKNOWN' to 'IGNORED';
    is_hotel_folio_by_doctype narrow gate tightened to require >=2 of
    {hotelName, confirmationNo, internalCodes, roomNumber}. Paired with
    prompts.py v5.7 rule above. See SKILL.md § Lessons Learned
    "v5.7 — IGNORED 白名单分类" for full decision record.
  ```

- [ ] **Step 1.10: Commit**

  ```bash
  git add scripts/core/classify.py scripts/core/__init__.py \
          scripts/dev/replay_classify.py \
          tests/fixtures/ocr/legitimate_folios/.gitkeep \
          tests/test_postprocess.py
  git commit -m "feat(classify): IGNORED fallthrough + docType narrow gate (v5.7 Unit 1)

  - classify_invoice fallthrough default: UNKNOWN → IGNORED
  - is_hotel_folio_by_doctype narrow gate: require >=2 of {hotelName,
    confirmationNo, internalCodes, roomNumber}. balance and
    arrival/departure deliberately excluded (SaaS Amount-due conflict /
    subscription-range leakage, respectively).
  - scripts/dev/replay_classify.py committed for Unit 1 Verification and
    future snapshot-sync regression checks; builds sha256→pdf_path
    reverse lookup from ~/invoices/**/pdfs/*.pdf so diff entries resolve
    to original source files."
  ```

---

## Unit 2: sender 字段穿透到 download record

**Goal:** 让 `classify_email` 返回的 dict 新增 `sender_email`（bare lowercase）键，三个 `download_*` 函数构造的 record 里多带 `"sender"` 和 `"sender_email"` 两个字段，供 Unit 3 的 `rename_by_ocr` IGNORED 分支消费。

**Requirements:** R2（前置依赖）

**Dependencies:** 无——可与 Unit 1 并行。

**Files:**
- Modify: `scripts/invoice_helpers.py`（`classify_email` 返回 dict 扩字段）
- Modify: `scripts/download-invoices.py`（`download_attachment` / `download_zip` / `download_link` 三处 record 构造）
- Test: `tests/test_postprocess.py`（新增 `TestSenderEmailPassthrough`）

### Steps

- [ ] **Step 2.1: 写 `classify_email` sender_email 字段测试（先让它失败）**

  在 `tests/test_postprocess.py` 末尾追加：

  ```python
  class TestSenderEmailPassthrough:
      """Unit 2 R2 prerequisite: sender_email is exported in classify_email
      return dict and flows through to download records, so Unit 3's
      rename_by_ocr can compose IGNORED_{domain-label}_{base}.pdf.
      """

      def _msg(self, from_value: str) -> dict:
          return {
              "id": "msg-001",
              "payload": {
                  "headers": [
                      {"name": "From", "value": from_value},
                      {"name": "Subject", "value": "Invoice for Termius Pro"},
                  ],
                  "body": {"data": ""},
              },
              "internalDate": "1731600000000",
          }

      def test_sender_email_extracted_from_name_bracket_form(self):
          sys.path.insert(0, "scripts")
          from invoice_helpers import classify_email
          result = classify_email(self._msg("Billing <billing@termius.com>"))
          assert result.get("sender") == "Billing <billing@termius.com>"
          assert result.get("sender_email") == "billing@termius.com"

      def test_sender_email_extracted_from_bare_address(self):
          sys.path.insert(0, "scripts")
          from invoice_helpers import classify_email
          result = classify_email(self._msg("kent@example.com"))
          assert result.get("sender_email") == "kent@example.com"

      def test_sender_email_empty_when_sender_missing(self):
          sys.path.insert(0, "scripts")
          from invoice_helpers import classify_email
          msg = {
              "id": "x",
              "payload": {"headers": [], "body": {"data": ""}},
              "internalDate": "1731600000000",
          }
          result = classify_email(msg)
          assert result.get("sender_email") == ""
  ```

- [ ] **Step 2.2: 跑测试验证失败**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestSenderEmailPassthrough -v
  ```

  Expected: FAIL — `sender_email` 键不在返回 dict 里（`.get()` 返回 None，不等于 "billing@termius.com"）

- [ ] **Step 2.3: 在 `classify_email` 返回 dict 加 `sender_email`**

  打开 `scripts/invoice_helpers.py`，找到：

  ```python
      result = {
          "doc_type": "UNKNOWN",
          "method": "MANUAL",
          "pdf_attachments": pdf_atts,
          "download_url": None,
          "hotel_name": hotel_name,
          "merchant": hotel_name or extract_merchant_from_body(body),
          "invoice_date": extract_invoice_date_from_body(body),
          "subject": subject,
          "sender": sender,
          "zip_attachments": zip_atts,
      }
  ```

  替换为：

  ```python
      result = {
          "doc_type": "UNKNOWN",
          "method": "MANUAL",
          "pdf_attachments": pdf_atts,
          "download_url": None,
          "hotel_name": hotel_name,
          "merchant": hotel_name or extract_merchant_from_body(body),
          "invoice_date": extract_invoice_date_from_body(body),
          "subject": subject,
          "sender": sender,
          "sender_email": sender_email,  # v5.7 Unit 2: bare lowercase addr
          "zip_attachments": zip_atts,
      }
  ```

- [ ] **Step 2.4: 跑测试验证通过**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestSenderEmailPassthrough -v
  ```

  Expected: 3 个 test PASS

  注意：如果 `sender` 参数语义现有测试依赖于 "v5.7 新增字段不破坏现有行为"，先跑下面的完整 regression：

  ```bash
  python3 -m pytest tests/ -q 2>&1 | tail -20
  ```

  Expected: 所有原有测试 PASS（字段扩展是 addition-only，不破坏已有 dict 读取）

- [ ] **Step 2.5: 三个 download_* 函数的 record 构造加两个字段**

  打开 `scripts/download-invoices.py`。

  **A. `download_attachment`** — 找到 `:358-365`：

  ```python
          rec = {
              "path": out, "valid": ok, "info": info,
              "subject": entry.get("subject"), "method": "ATTACHMENT",
              "merchant": merchant, "date": date_str, "doc_type": actual_type,
              "message_id": msg_id,
              "attachment_part_id": att.get("attachmentId"),
              "internal_date": entry.get("internal_date"),
          }
  ```

  替换为：

  ```python
          rec = {
              "path": out, "valid": ok, "info": info,
              "subject": entry.get("subject"), "method": "ATTACHMENT",
              "merchant": merchant, "date": date_str, "doc_type": actual_type,
              "message_id": msg_id,
              "attachment_part_id": att.get("attachmentId"),
              "internal_date": entry.get("internal_date"),
              # v5.7 Unit 2: sender fields consumed by rename_by_ocr IGNORED
              # branch and the learned_exclusions CTA in write_report_md.
              "sender": entry.get("sender", ""),
              "sender_email": entry.get("sender_email", ""),
          }
  ```

  **B. `download_zip`** — 找到 `:397-404`：

  ```python
                  rec = {
                      "path": out, "valid": ok, "info": info,
                      "subject": entry.get("subject"), "method": "ATTACHMENT_ZIP",
                      "merchant": merchant, "date": date_str, "doc_type": entry["doc_type"],
                      "message_id": msg_id,
                      "attachment_part_id": f"{zid}:{os.path.basename(pdf)}",
                      "internal_date": entry.get("internal_date"),
                  }
  ```

  替换为：

  ```python
                  rec = {
                      "path": out, "valid": ok, "info": info,
                      "subject": entry.get("subject"), "method": "ATTACHMENT_ZIP",
                      "merchant": merchant, "date": date_str, "doc_type": entry["doc_type"],
                      "message_id": msg_id,
                      "attachment_part_id": f"{zid}:{os.path.basename(pdf)}",
                      "internal_date": entry.get("internal_date"),
                      # v5.7 Unit 2: sender fields for IGNORED rename + CTA
                      "sender": entry.get("sender", ""),
                      "sender_email": entry.get("sender_email", ""),
                  }
  ```

  **C. `download_link`** — 找到 `:472-480`：

  ```python
      rec = {
          "path": out, "valid": ok, "info": info,
          "subject": entry.get("subject"), "method": entry["method"],
          "merchant": merchant, "date": date_str, "doc_type": entry["doc_type"],
          "url": url,
          "message_id": entry.get("message_id", ""),
          "attachment_part_id": f"url:{url_key}",
          "internal_date": entry.get("internal_date"),
      }
  ```

  替换为：

  ```python
      rec = {
          "path": out, "valid": ok, "info": info,
          "subject": entry.get("subject"), "method": entry["method"],
          "merchant": merchant, "date": date_str, "doc_type": entry["doc_type"],
          "url": url,
          "message_id": entry.get("message_id", ""),
          "attachment_part_id": f"url:{url_key}",
          "internal_date": entry.get("internal_date"),
          # v5.7 Unit 2: sender fields for IGNORED rename + CTA
          "sender": entry.get("sender", ""),
          "sender_email": entry.get("sender_email", ""),
      }
  ```

- [ ] **Step 2.6: 跑整套测试确认无回归**

  ```bash
  python3 -m pytest tests/ -q 2>&1 | tail -10
  ```

  Expected: 全绿（字段扩展）

- [ ] **Step 2.7: Commit**

  ```bash
  git add scripts/invoice_helpers.py scripts/download-invoices.py tests/test_postprocess.py
  git commit -m "feat(download): pass sender/sender_email into download records (v5.7 Unit 2)

  classify_email now exports sender_email (bare lowercase addr) in its
  return dict. Three download_* functions propagate sender and sender_email
  into their record dicts so rename_by_ocr's IGNORED branch can compose
  IGNORED_{domain-label}_{base}.pdf and write_report_md can aggregate a
  learned_exclusions CTA by email domain."
  ```

---

## Unit 3: postprocess IGNORED 处理（rename + CATEGORY + matching 入口切分）

**Goal:** IGNORED 记录走 `rename_by_ocr` 第三条分支得到 `IGNORED_{sender_short}_{原名}.pdf` 文件名；`main()` 在 `valid_records` 构造后把 IGNORED 切出独立列表；matching 和 aggregation 只处理 reimbursable 记录；`build_aggregation` 入口加防御性断言。

**Requirements:** R2

**Dependencies:** Unit 1（IGNORED 分类存在） + Unit 2（`sender_email` 字段可读）

**Files:**
- Modify: `scripts/postprocess.py`（`CATEGORY_LABELS` 加 IGNORED；`rename_by_ocr` 新增 IGNORED 分支；`build_aggregation` 入口断言）
- Modify: `scripts/download-invoices.py::main`（`valid_records` 切分）
- Test: `tests/test_postprocess.py`（新增 `TestRenameIgnoredBranch`；扩 `TestCategoryConstants`）

### Steps

- [ ] **Step 3.1: 写 IGNORED rename 测试（先让它失败）**

  在 `tests/test_postprocess.py` 末尾追加：

  ```python
  class TestRenameIgnoredBranch:
      """Unit 3 R2: rename_by_ocr IGNORED branch produces
      IGNORED_{sender_short}_{base}.pdf. Reuses UNPARSED branch's
      sanitize + .pdf re-append pattern for safety.
      """

      def _setup(self, tmp_path, filename="original.pdf"):
          pdf = tmp_path / filename
          pdf.write_bytes(b"%PDF-1.4 dummy")
          return pdf

      def test_termius_renamed_with_domain_label(self, tmp_path):
          sys.path.insert(0, "scripts")
          from postprocess import rename_by_ocr
          pdf = self._setup(tmp_path, "Q9YJOO4I-0001.pdf")
          record = {
              "path": str(pdf),
              "message_id": "msg-termius-001",
              "sender": "Billing <billing@termius.com>",
              "sender_email": "billing@termius.com",
              "merchant": "Termius",
              "date": "20251112",
          }
          analysis = {"category": "IGNORED", "ocr": {"docType": "Invoice"}}
          rename_by_ocr(record, analysis, str(tmp_path))
          assert os.path.basename(record["path"]).startswith("IGNORED_termius_")
          assert record["path"].endswith(".pdf")
          assert os.path.exists(record["path"])

      def test_empty_sender_email_falls_back_to_unknown(self, tmp_path):
          sys.path.insert(0, "scripts")
          from postprocess import rename_by_ocr
          pdf = self._setup(tmp_path, "mystery.pdf")
          record = {
              "path": str(pdf),
              "message_id": "msg-mystery-001",
              "sender": "",
              "sender_email": "",
              "merchant": "",
              "date": "20250101",
          }
          analysis = {"category": "IGNORED", "ocr": {"docType": "whatever"}}
          rename_by_ocr(record, analysis, str(tmp_path))
          assert os.path.basename(record["path"]).startswith("IGNORED_unknown_")

      def test_long_domain_truncated(self, tmp_path):
          sys.path.insert(0, "scripts")
          from postprocess import rename_by_ocr
          pdf = self._setup(tmp_path, "x.pdf")
          record = {
              "path": str(pdf),
              "message_id": "msg-x-001",
              "sender_email": "ops@reallylongdomainname-with-suffix.co",
              "merchant": "X",
              "date": "20250101",
          }
          analysis = {"category": "IGNORED", "ocr": {}}
          rename_by_ocr(record, analysis, str(tmp_path))
          basename = os.path.basename(record["path"])
          # Domain label capped at 20 chars (between the two underscores).
          # Structure: IGNORED_{domain<=20}_{base}
          parts = basename.split("_", 2)
          assert parts[0] == "IGNORED"
          assert len(parts[1]) <= 20
          assert basename.endswith(".pdf")

      def test_weird_email_sanitized(self, tmp_path):
          """sender_email with unusual chars must not break os.rename
          (sanitize_filename handles path separators / NUL / etc.).
          """
          sys.path.insert(0, "scripts")
          from postprocess import rename_by_ocr
          pdf = self._setup(tmp_path, "z.pdf")
          record = {
              "path": str(pdf),
              "message_id": "msg-z-001",
              "sender_email": "foo@bar@baz.com",
              "merchant": "Z",
              "date": "20250101",
          }
          analysis = {"category": "IGNORED", "ocr": {}}
          rename_by_ocr(record, analysis, str(tmp_path))
          assert os.path.basename(record["path"]).endswith(".pdf")
          assert os.path.exists(record["path"])
  ```

- [ ] **Step 3.2: 写 CATEGORY_LABELS IGNORED 存在测试**

  找到已有 `class TestCategoryConstants:` 块（`tests/test_postprocess.py:2278`），在类末尾追加：

  ```python
      def test_ignored_label_registered(self):
          sys.path.insert(0, "scripts")
          from postprocess import CATEGORY_LABELS, CATEGORY_ORDER
          assert CATEGORY_LABELS.get("IGNORED") == "已忽略"
          # CATEGORY_ORDER deliberately NOT registering IGNORED: IGNORED
          # records do not enter aggregation rows, so the default fallback
          # get(..., 50) is enough. Registering IGNORED would risk
          # interacting with the UNPARSED=99 invariant.
          assert "IGNORED" not in CATEGORY_ORDER
  ```

- [ ] **Step 3.3: 跑测试验证失败**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestRenameIgnoredBranch tests/test_postprocess.py::TestCategoryConstants::test_ignored_label_registered -v
  ```

  Expected: FAIL — rename 测试因 IGNORED 分支不存在行为走 happy-path（文件名形如 `20251112_Termius_发票.pdf`）；label 测试因 IGNORED 未登记而断言失败

- [ ] **Step 3.4: 注册 IGNORED 到 CATEGORY_LABELS**

  打开 `scripts/postprocess.py`，找到 `CATEGORY_LABELS` 定义（`:62-76`）：

  ```python
  CATEGORY_LABELS: Dict[str, str] = {
      "HOTEL":               "酒店",
      "HOTEL_INVOICE":       "酒店发票",
      "HOTEL_FOLIO":         "水单",
      "RIDEHAILING":         "网约车",
      "RIDEHAILING_INVOICE": "网约车发票",
      "RIDEHAILING_RECEIPT": "行程单",
      "TAXI":                "出租车发票",
      "TRAIN":               "火车票",
      "MEAL":                "餐饮",
      "MOBILE":              "话费",
      "TOLLS":               "通行费",
      "UNKNOWN":             "发票",
      "UNPARSED":            "⚠️ 需人工核查",
  }
  ```

  替换为：

  ```python
  CATEGORY_LABELS: Dict[str, str] = {
      "HOTEL":               "酒店",
      "HOTEL_INVOICE":       "酒店发票",
      "HOTEL_FOLIO":         "水单",
      "RIDEHAILING":         "网约车",
      "RIDEHAILING_INVOICE": "网约车发票",
      "RIDEHAILING_RECEIPT": "行程单",
      "TAXI":                "出租车发票",
      "TRAIN":               "火车票",
      "MEAL":                "餐饮",
      "MOBILE":              "话费",
      "TOLLS":               "通行费",
      "UNKNOWN":             "发票",
      "UNPARSED":            "⚠️ 需人工核查",
      # v5.7 Unit 3: IGNORED records get IGNORED_{domain}_{base}.pdf
      # filenames but CATEGORY_ORDER is deliberately NOT extended — IGNORED
      # records do not enter aggregation rows, so the default get(..., 50)
      # fallback suffices. Registering CATEGORY_ORDER["IGNORED"] would risk
      # interacting with TestCategoryConstants's UNPARSED=99 invariant.
      "IGNORED":             "已忽略",
  }
  ```

- [ ] **Step 3.5: 在 `rename_by_ocr` 新增 IGNORED 分支**

  打开 `scripts/postprocess.py`，找到 `rename_by_ocr` 里的 UNPARSED 分支末尾（`:378-379` 的 `return record`）。在这个 `return record` 之后、happy path 开始之前，插入新的 IGNORED 分支：

  找到：

  ```python
          record["vendor_name"] = record.get("merchant") or "未知"
          record["transaction_date"] = record.get("date", "")
          return record

      # Happy path: {YYYYMMDD}_{vendor}_{label}.pdf
  ```

  替换为：

  ```python
          record["vendor_name"] = record.get("merchant") or "未知"
          record["transaction_date"] = record.get("date", "")
          return record

      # v5.7 Unit 3: IGNORED branch — non-reimbursable receipts (SaaS
      # subscriptions, marketing receipts) get IGNORED_{domain-label}_{base}.pdf
      # so the user can visually spot them in output_dir without them
      # polluting CSV/zip/missing.json.items[].
      if category == "IGNORED":
          sender_email = record.get("sender_email", "") or ""
          # Extract domain label: "billing@termius.com" → "termius"
          # Handle pathological "foo@bar@baz.com" by splitting on first '@'.
          after_at = sender_email.split("@", 1)[-1] if "@" in sender_email else sender_email
          domain_label = after_at.split(".")[0] or "unknown"
          sender_short = sanitize_filename(domain_label, max_len=20) or "unknown"

          base = os.path.basename(old_path)
          new_name = sanitize_filename(f"IGNORED_{sender_short}_{base}", max_len=200)
          if not new_name.endswith(".pdf"):
              new_name += ".pdf"
          new_path = make_unique_path(pdfs_dir, new_name)
          if new_path != old_path:
              try:
                  os.rename(old_path, new_path)
                  record["path"] = new_path
              except OSError as e:
                  # Hard-disk / permission error: degrade to UNPARSED so
                  # the record is still visible to the user through the
                  # UNPARSED pipeline. Keeps the three-deliverable set
                  # (report / CSV / zip) self-consistent.
                  record["category"] = "UNPARSED"
                  analysis["error"] = f"IGNORED rename failed: {e}"
                  # Re-run the UNPARSED logic by recursing won't work cleanly;
                  # just fall through to the bottom without further rename.
                  record["vendor_name"] = record.get("merchant") or "未知"
                  record["transaction_date"] = record.get("date", "")
                  return record
          record["vendor_name"] = record.get("merchant") or "未知"
          record["transaction_date"] = record.get("date", "")
          return record

      # Happy path: {YYYYMMDD}_{vendor}_{label}.pdf
  ```

- [ ] **Step 3.6: 跑 rename 测试验证通过**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestRenameIgnoredBranch tests/test_postprocess.py::TestCategoryConstants -v
  ```

  Expected: PASS

- [ ] **Step 3.7: 写 `build_aggregation` 防御性断言测试**

  在 `tests/test_postprocess.py::TestBuildAggregation` 类末尾追加：

  ```python
      def test_ignored_record_leaking_into_aggregation_raises(self):
          """Unit 3 guard: main() is the only place that filters out IGNORED
          records before they reach build_aggregation. If a future refactor
          silently lets one through, catch it here.
          """
          sys.path.insert(0, "scripts")
          from postprocess import build_aggregation
          matching_result = {
              "hotel": {"paired": [], "unmatched_invoices": [], "unmatched_folios": []},
              "ridehailing": {"paired": [], "unmatched_invoices": [], "unmatched_receipts": []},
              "other": [{
                  "category": "IGNORED",  # should never reach here
                  "path": "/tmp/bogus.pdf",
                  "transaction_date": "20250101",
                  "vendor_name": "Termius",
                  "ocr": {"transactionAmount": 120.0},
              }],
              "unparsed": [],
              "dedup_removed": [],
          }
          valid_records = matching_result["other"]
          with pytest.raises(AssertionError, match="IGNORED leaked"):
              build_aggregation(matching_result, valid_records)
  ```

- [ ] **Step 3.8: 跑测试验证失败（断言还不存在）**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestBuildAggregation::test_ignored_record_leaking_into_aggregation_raises -v
  ```

  Expected: FAIL — 当前 `build_aggregation` 不带该断言，测试得到的是 "didn't raise" 类型失败

- [ ] **Step 3.9: 在 `build_aggregation` 入口加断言**

  打开 `scripts/postprocess.py`，找到 `def build_aggregation` 定义（`:828`）。在函数体第一个有意义的代码块（读参数）**之前** / 一进入函数就加：

  先读一下函数签名了解参数：

  ```bash
  sed -n '828,850p' scripts/postprocess.py
  ```

  假设函数签名形如 `def build_aggregation(matching_result, valid_records):`，在其 docstring 之后、逻辑开始前插入：

  ```python
      # v5.7 Unit 3: Guard against silent regression — main() splits IGNORED
      # records out before calling build_aggregation. If any leak through,
      # aggregation completeness assertions later in the function will fire
      # from a confusing "accounted == len(valid_records)" mismatch; assert
      # here with a clearer message.
      assert not any(
          r.get("category") == "IGNORED"
          for r in valid_records
      ), "IGNORED leaked past main() split — Unit 3 filter broke"
  ```

  **重要**：如果函数签名里的第二个参数名和我假设的不同（例如 `rows`），相应调整。

- [ ] **Step 3.10: 跑测试验证通过**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestBuildAggregation -v
  ```

  Expected: 所有测试 PASS（新断言 test PASS；原有 aggregation 测试没有 IGNORED 数据，断言不触发）

- [ ] **Step 3.11: 在 `main()` 和 `_run_postprocess_only` 加 IGNORED 切分**

  打开 `scripts/download-invoices.py`，找到 `main()` 里 `valid_records` 构造处（`:1315-1319`）：

  ```python
      dedup_removed_ids = {id(r) for r in matching_result.get("dedup_removed", [])}
      valid_records = [
          d for d in downloaded_all
          if d.get("valid") and id(d) not in dedup_removed_ids
      ]
      aggregation = build_aggregation(matching_result, valid_records)
  ```

  替换为：

  ```python
      dedup_removed_ids = {id(r) for r in matching_result.get("dedup_removed", [])}
      valid_records = [
          d for d in downloaded_all
          if d.get("valid") and id(d) not in dedup_removed_ids
      ]
      # v5.7 Unit 3: split IGNORED out before aggregation. matching and
      # build_aggregation only see reimbursable records; ignored_records is
      # passed separately to the report writer and OpenClaw summary.
      ignored_records = [d for d in valid_records if d.get("category") == "IGNORED"]
      reimbursable_records = [d for d in valid_records if d.get("category") != "IGNORED"]
      aggregation = build_aggregation(matching_result, reimbursable_records)
  ```

  同样找 `_run_postprocess_only` 里的 `valid_records` 构造（`:898-902`）：

  ```python
      valid_records = [
          d for d in downloaded_all if d.get("valid")
      ]
      aggregation = build_aggregation(matching_result, valid_records)
  ```

  替换为：

  ```python
      valid_records = [
          d for d in downloaded_all if d.get("valid")
      ]
      # v5.7 Unit 3: same IGNORED split as main().
      ignored_records = [d for d in valid_records if d.get("category") == "IGNORED"]
      reimbursable_records = [d for d in valid_records if d.get("category") != "IGNORED"]
      aggregation = build_aggregation(matching_result, reimbursable_records)
  ```

  **注意**：两处 `ignored_records` / `reimbursable_records` 会在 Unit 4 被传给 `write_report_md` 和 `print_openclaw_summary`；这里只做切分，调用点修改留给 Unit 4。

- [ ] **Step 3.12: 跑所有 postprocess 测试确认无回归**

  ```bash
  python3 -m pytest tests/test_postprocess.py tests/test_agent_contract.py -q 2>&1 | tail -10
  ```

  Expected: 全绿。`TestMatchingTiersContract` / `TestZipManifestContract` / `TestMissingJsonSchemaContract` / `TestBuildAggregation` 无回归。

- [ ] **Step 3.13: Commit**

  ```bash
  git add scripts/postprocess.py scripts/download-invoices.py tests/test_postprocess.py
  git commit -m "feat(postprocess): IGNORED rename + main split (v5.7 Unit 3)

  - CATEGORY_LABELS['IGNORED'] = '已忽略'; CATEGORY_ORDER deliberately
    unchanged (IGNORED never enters aggregation rows)
  - rename_by_ocr adds third branch: IGNORED_{domain-label}_{base}.pdf
    with OSError degradation to UNPARSED branch for hard-disk failure
  - main() / _run_postprocess_only split IGNORED out before
    build_aggregation so matching pipeline only sees reimbursable records
  - build_aggregation input-guards against IGNORED leaks with clear
    assertion (catches silent regressions from future refactors)"
  ```

---

## Unit 4: 三路通知 — 报告节 + OpenClaw summary + zip 过滤 + learned_exclusions CTA

**Goal:** 下载报告末尾渲染「已忽略的非报销票据 (N)」章节 + `learned_exclusions.json` CTA 块（N ≥ 1 渲染，N = 0 整节省略）；`print_openclaw_summary` 追加「📭 已忽略 {N} 张」一行（N = 0 省略）；`zip_output` 排除 `IGNORED_*.pdf`。

**Requirements:** R3

**Dependencies:** Unit 3（`ignored_records` 列表已切分）

**Files:**
- Modify: `scripts/download-invoices.py::write_report_md`（加 `ignored_records=None` 关键字参数 + 渲染 + CTA；调用点传参）
- Modify: `scripts/postprocess.py::print_openclaw_summary`（加 `ignored_count: int = 0` 参数 + 渲染）
- Modify: `scripts/postprocess.py::zip_output`（加 `IGNORED_*.pdf` 前缀过滤）
- Test: `tests/test_postprocess.py`（新增 `TestIgnoredCtaRendering`；扩 `TestZipAtomic` / `TestPrintOpenClawSummary`）

### Steps

- [ ] **Step 4.1: 写 zip 过滤测试（先让它失败）**

  在 `tests/test_postprocess.py::TestZipAtomic` 末尾追加：

  ```python
      def test_ignored_prefix_excluded_from_zip(self, tmp_path):
          """Unit 4 R3: IGNORED_*.pdf is excluded from the deliverable zip.
          UNPARSED_*.pdf behavior is preserved (still zipped).
          """
          sys.path.insert(0, "scripts")
          from postprocess import zip_output
          out = tmp_path / "batch"
          out.mkdir()
          pdfs = out / "pdfs"
          pdfs.mkdir()
          (pdfs / "20251112_Marriott_水单.pdf").write_bytes(b"%PDF hotel")
          (pdfs / "IGNORED_termius_Q9YJOO4I.pdf").write_bytes(b"%PDF saas")
          (pdfs / "UNPARSED_abc_bad.pdf").write_bytes(b"%PDF broken")
          (out / "下载报告.md").write_text("report")
          (out / "发票汇总.csv").write_text("csv")

          zp = zip_output(str(out))
          import zipfile
          with zipfile.ZipFile(zp) as zf:
              names = {os.path.basename(n) for n in zf.namelist()}
          assert "20251112_Marriott_水单.pdf" in names
          assert "UNPARSED_abc_bad.pdf" in names  # preserved
          assert "IGNORED_termius_Q9YJOO4I.pdf" not in names  # excluded
  ```

- [ ] **Step 4.2: 写 OpenClaw summary IGNORED 行测试**

  在 `tests/test_postprocess.py::TestPrintOpenClawSummary` 末尾追加：

  ```python
      def test_ignored_count_line_rendered_when_nonzero(self):
          """Unit 4 R3: print_openclaw_summary emits '📭 已忽略 N 张非报销票据'
          when ignored_count > 0. The line is positioned after the 'next action'
          block and before the Deliverables block so the user sees it as
          post-summary context, not as a deliverable itself.
          """
          sys.path.insert(0, "scripts")
          from postprocess import print_openclaw_summary
          aggregation = {
              "rows": [],
              "subtotals": {},
              "unmatched": {"hotel_invoices": 0, "hotel_folios": 0,
                            "rh_invoices": 0, "rh_receipts": 0},
              "voucher_count": 1,
              "low_conf": {"count": 0, "amount": 0.0},
              "grand_total": 100.0,
          }
          lines = []
          print_openclaw_summary(
              aggregation,
              output_dir="/tmp/out",
              zip_path="/tmp/out/发票打包.zip",
              csv_path="/tmp/out/发票汇总.csv",
              md_path="/tmp/out/下载报告.md",
              log_path="/tmp/out/run.log",
              missing_status="stop",
              date_range=("2025/01/01", "2025/03/31"),
              writer=lines.append,
              ignored_count=3,  # v5.7 Unit 4
          )
          text = "\n".join(lines)
          assert "📭 已忽略 3 张非报销票据" in text

      def test_ignored_count_zero_omits_line(self):
          sys.path.insert(0, "scripts")
          from postprocess import print_openclaw_summary
          aggregation = {
              "rows": [],
              "subtotals": {},
              "unmatched": {"hotel_invoices": 0, "hotel_folios": 0,
                            "rh_invoices": 0, "rh_receipts": 0},
              "voucher_count": 1,
              "low_conf": {"count": 0, "amount": 0.0},
              "grand_total": 100.0,
          }
          lines = []
          print_openclaw_summary(
              aggregation,
              output_dir="/tmp/out",
              zip_path="/tmp/out/发票打包.zip",
              csv_path="/tmp/out/发票汇总.csv",
              md_path="/tmp/out/下载报告.md",
              log_path="/tmp/out/run.log",
              missing_status="stop",
              date_range=("2025/01/01", "2025/03/31"),
              writer=lines.append,
              ignored_count=0,
          )
          text = "\n".join(lines)
          assert "已忽略" not in text
  ```

- [ ] **Step 4.3: 写 report IGNORED 节 + CTA 测试**

  在 `tests/test_postprocess.py` 末尾追加：

  ```python
  class TestIgnoredCtaRendering:
      """Unit 4 R3: write_report_md renders '📭 已忽略的非报销票据 (N)' section
      plus a learned_exclusions.json CTA block listing -from:<domain> hints
      aggregated per sender domain.
      """

      def _base_args(self, tmp_path):
          return {
              "downloaded_all": [],
              "failed": [],
              "skipped": [],
              "matching_result": {
                  "hotel": {"paired": [], "unmatched_invoices": [],
                            "unmatched_folios": []},
                  "ridehailing": {"paired": [], "unmatched_invoices": [],
                                  "unmatched_receipts": []},
                  "other": [],
                  "unparsed": [],
                  "dedup_removed": [],
              },
              "date_range": ("2025/01/01", "2025/03/31"),
              "iteration": 1,
              "supplemental": False,
              "aggregation": None,
          }

      def test_three_distinct_senders_emit_three_cta_lines(self, tmp_path):
          sys.path.insert(0, "scripts")
          from download_invoices import write_report_md  # if import works; else adjust
          # If `scripts/download-invoices.py` can't be imported as a module
          # because of the dash in the filename, copy the write_report_md
          # assertion logic into this test via `importlib.util.spec_from_file_location`.
          # (See comment below for the loader fallback.)
          import importlib.util
          spec = importlib.util.spec_from_file_location(
              "download_invoices", "scripts/download-invoices.py")
          mod = importlib.util.module_from_spec(spec)
          spec.loader.exec_module(mod)
          write_report_md = mod.write_report_md

          ignored = [
              {"sender_email": "billing@termius.com", "ocr": {"transactionAmount": 120.0, "currency": "USD"}},
              {"sender_email": "receipts@openrouter.ai", "ocr": {"transactionAmount": 20.0}},
              {"sender_email": "invoice@anthropic.com", "ocr": {"transactionAmount": 10.0}},
          ]
          out = tmp_path / "report.md"
          write_report_md(str(out), **self._base_args(tmp_path), ignored_records=ignored)
          text = out.read_text()
          assert "📭 已忽略的非报销票据 (3)" in text
          # CTA block
          assert "learned_exclusions.json" in text
          assert "-from:termius.com" in text
          assert "-from:openrouter.ai" in text
          assert "-from:anthropic.com" in text
          # Sender listing
          assert "billing@termius.com" in text
          assert "120" in text

      def test_same_domain_multiple_records_aggregate_to_one_cta_line(self, tmp_path):
          sys.path.insert(0, "scripts")
          import importlib.util
          spec = importlib.util.spec_from_file_location(
              "download_invoices", "scripts/download-invoices.py")
          mod = importlib.util.module_from_spec(spec)
          spec.loader.exec_module(mod)
          write_report_md = mod.write_report_md

          ignored = [
              {"sender_email": "support@termius.com", "ocr": {"transactionAmount": 120.0}},
              {"sender_email": "billing@termius.com", "ocr": {"transactionAmount": 20.0}},
          ]
          out = tmp_path / "report.md"
          write_report_md(str(out), **self._base_args(tmp_path), ignored_records=ignored)
          text = out.read_text()
          assert text.count("-from:termius.com") == 1
          # Count aggregation
          assert "已过滤 2 次" in text

      def test_empty_ignored_omits_section(self, tmp_path):
          sys.path.insert(0, "scripts")
          import importlib.util
          spec = importlib.util.spec_from_file_location(
              "download_invoices", "scripts/download-invoices.py")
          mod = importlib.util.module_from_spec(spec)
          spec.loader.exec_module(mod)
          write_report_md = mod.write_report_md

          out = tmp_path / "report.md"
          write_report_md(str(out), **self._base_args(tmp_path), ignored_records=[])
          text = out.read_text()
          assert "已忽略的非报销票据" not in text
          assert "learned_exclusions" not in text

      def test_none_ignored_records_also_omits_section(self, tmp_path):
          """Default keyword value is None; existing callers that don't pass
          ignored_records must continue to work (no report section).
          """
          sys.path.insert(0, "scripts")
          import importlib.util
          spec = importlib.util.spec_from_file_location(
              "download_invoices", "scripts/download-invoices.py")
          mod = importlib.util.module_from_spec(spec)
          spec.loader.exec_module(mod)
          write_report_md = mod.write_report_md

          out = tmp_path / "report.md"
          write_report_md(str(out), **self._base_args(tmp_path))  # no ignored_records
          text = out.read_text()
          assert "已忽略的非报销票据" not in text

      def test_empty_sender_email_excluded_from_cta_only(self, tmp_path):
          """A record with empty sender_email still appears as "未知发件人"
          in the sender listing, but is NOT in the CTA aggregation
          (no domain to propose)."""
          sys.path.insert(0, "scripts")
          import importlib.util
          spec = importlib.util.spec_from_file_location(
              "download_invoices", "scripts/download-invoices.py")
          mod = importlib.util.module_from_spec(spec)
          spec.loader.exec_module(mod)
          write_report_md = mod.write_report_md

          ignored = [
              {"sender_email": "", "ocr": {"transactionAmount": 50.0}},
              {"sender_email": "billing@termius.com", "ocr": {"transactionAmount": 120.0}},
          ]
          out = tmp_path / "report.md"
          write_report_md(str(out), **self._base_args(tmp_path), ignored_records=ignored)
          text = out.read_text()
          assert "未知发件人" in text
          # Only termius.com in the CTA, not a -from: line with empty domain
          assert "-from:termius.com" in text
          assert "-from:\n" not in text  # no empty-domain line
          assert "-from: " not in text   # no empty-domain line w/ space
  ```

- [ ] **Step 4.4: 跑测试确认全部失败**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestZipAtomic::test_ignored_prefix_excluded_from_zip \
                    tests/test_postprocess.py::TestPrintOpenClawSummary::test_ignored_count_line_rendered_when_nonzero \
                    tests/test_postprocess.py::TestPrintOpenClawSummary::test_ignored_count_zero_omits_line \
                    tests/test_postprocess.py::TestIgnoredCtaRendering -v
  ```

  Expected: 全部 FAIL（各种 "section not found" / "unexpected keyword argument" 类型错误）

- [ ] **Step 4.5: zip 过滤 — 给 `zip_output` 加 IGNORED_ 前缀排除**

  打开 `scripts/postprocess.py`，找到 `zip_output` 里的 `if fn.startswith(ZIP_PREFIX) and fn.endswith(".zip"): continue`（`:1621-1622`）。

  在这一 `continue` 之后、`if os.path.islink(...)` 之前插入：

  ```python
                  # v5.7 Unit 4: IGNORED_ prefix files are non-reimbursable
                  # receipts (SaaS subscriptions, marketing). They stay in
                  # output_dir for the user's audit trail but don't enter
                  # the deliverable zip. UNPARSED_ files still zip (user
                  # needs to see failed-to-parse receipts).
                  if fn.startswith("IGNORED_"):
                      continue
  ```

- [ ] **Step 4.6: `print_openclaw_summary` 加 `ignored_count` 参数 + 渲染**

  打开 `scripts/postprocess.py`，找到 `print_openclaw_summary` 函数签名（`:1060-1071`）：

  ```python
  def print_openclaw_summary(
      aggregation: Dict[str, Any],
      *,
      output_dir: str,
      zip_path: Optional[str],
      csv_path: str,
      md_path: str,
      log_path: str,
      missing_status: str,
      date_range: Tuple[str, str],
      writer: Callable[[str], None] = print,
  ) -> None:
  ```

  替换为：

  ```python
  def print_openclaw_summary(
      aggregation: Dict[str, Any],
      *,
      output_dir: str,
      zip_path: Optional[str],
      csv_path: str,
      md_path: str,
      log_path: str,
      missing_status: str,
      date_range: Tuple[str, str],
      writer: Callable[[str], None] = print,
      ignored_count: int = 0,  # v5.7 Unit 4
  ) -> None:
  ```

  然后在 "9. Next action" 段（`:1160-1171`）之后、"10. Blank" (`:1173` `writer("")`) **之前**插入 IGNORED 行：

  找到：

  ```python
      else:  # ask_user
          writer(f"👉 下一步：需人工核查 — 见 {abs_md} 末尾「⚠️ 需人工核查」区")
      # 10. Blank
      writer("")
  ```

  替换为：

  ```python
      else:  # ask_user
          writer(f"👉 下一步：需人工核查 — 见 {abs_md} 末尾「⚠️ 需人工核查」区")
      # 9.5. v5.7 Unit 4: IGNORED count line (only if any were filtered)
      if ignored_count:
          writer(
              f"📭 已忽略 {ignored_count} 张非报销票据"
              f"（详见下载报告.md §已忽略的非报销票据）"
          )
      # 10. Blank
      writer("")
  ```

- [ ] **Step 4.7: `write_report_md` 加 `ignored_records` 参数 + 节 + CTA**

  打开 `scripts/download-invoices.py`，找到 `write_report_md` 签名（`:488-497`）：

  ```python
  def write_report_md(
      path, *,
      downloaded_all, failed, skipped,
      matching_result,
      date_range,
      iteration: int,
      supplemental: bool,
      aggregation=None,
      out_of_range_items=None,   # v5.5 — skipped cross-quarter items
  ):
  ```

  替换为：

  ```python
  def write_report_md(
      path, *,
      downloaded_all, failed, skipped,
      matching_result,
      date_range,
      iteration: int,
      supplemental: bool,
      aggregation=None,
      out_of_range_items=None,   # v5.5 — skipped cross-quarter items
      ignored_records=None,      # v5.7 Unit 4 — IGNORED records for §已忽略 + CTA
  ):
  ```

  然后找到 unparsed 节尾（`:719` `lines.append("")`）和 "补搜建议" 节前（`:739`）之间的位置。在 unparsed 节和 out_of_range 节之间，out_of_range 节之前插入 IGNORED 节（放这里，紧挨着 unparsed 的「⚠️ 需人工核查」— 两个"运营提示"节紧邻）：

  找到：

  ```python
      # ── Unparsed (LLM failed) ──
      unparsed = matching_result.get("unparsed", [])
      if unparsed:
          lines.append(f"## ⚠️ 需人工核查（LLM OCR 失败，{len(unparsed)} 份）\n")
          for rec in unparsed:
              err = rec.get("error") or "unknown"
              lines.append(f"- `{os.path.basename(rec.get('path',''))}` — {err[:80]}")
          lines.append("")

      # ── v5.5 跨季度边界项（无需补搜） ──
  ```

  替换为（在 unparsed 节之后、out_of_range 节之前插入新段）：

  ```python
      # ── Unparsed (LLM failed) ──
      unparsed = matching_result.get("unparsed", [])
      if unparsed:
          lines.append(f"## ⚠️ 需人工核查（LLM OCR 失败，{len(unparsed)} 份）\n")
          for rec in unparsed:
              err = rec.get("error") or "unknown"
              lines.append(f"- `{os.path.basename(rec.get('path',''))}` — {err[:80]}")
          lines.append("")

      # ── v5.7 IGNORED 非报销票据 + learned_exclusions CTA ──
      if ignored_records:
          lines.append(f"## 📭 已忽略的非报销票据 ({len(ignored_records)})\n")
          lines.append(
              "以下票据被识别为非发票 / 非水单 / 非行程单，已自动过滤，"
              "不进入 CSV / 打包 zip。文件仍保留在 PDFs 目录下以 `IGNORED_` "
              "前缀标记，可人工核查。\n"
          )
          # Sender listing (individual rows; empty sender → 未知发件人)
          for rec in ignored_records:
              sender_email = rec.get("sender_email") or ""
              label = sender_email if sender_email else "未知发件人"
              amount = (rec.get("ocr") or {}).get("transactionAmount")
              currency = (rec.get("ocr") or {}).get("currency") or ""
              if amount is not None:
                  prefix = "¥" if not currency or currency == "CNY" else ""
                  suffix = f" {currency}" if currency and currency != "CNY" else ""
                  lines.append(f"- {label}：{prefix}{amount:.2f}{suffix}")
              else:
                  lines.append(f"- {label}：金额未识别")
          lines.append("")

          # CTA: aggregate by email domain, render -from:<domain> lines
          domain_counts: Dict[str, int] = {}
          for rec in ignored_records:
              sender_email = rec.get("sender_email") or ""
              if "@" not in sender_email:
                  continue  # skip records with no parseable domain
              domain = sender_email.split("@", 1)[-1]
              if not domain:
                  continue
              domain_counts[domain] = domain_counts.get(domain, 0) + 1

          if domain_counts:
              lines.append(
                  "💡 下次避免 OCR 成本：可把这些 sender 加到 "
                  "`learned_exclusions.json`\n"
              )
              lines.append("```")
              for domain in sorted(domain_counts.keys()):
                  n = domain_counts[domain]
                  lines.append(f"-from:{domain}       # 已过滤 {n} 次")
              lines.append("```")
              lines.append("")

      # ── v5.5 跨季度边界项（无需补搜） ──
  ```

  **注意** `Dict` 类型导入：文件顶部已经 `from typing import ...` 过；如未导入 `Dict`，在顶部 typing import 行加上。

- [ ] **Step 4.8: 在 `main()` / `_run_postprocess_only` 调用点传 `ignored_records` + `ignored_count`**

  打开 `scripts/download-invoices.py`，找 `main()` 里 `write_report_md` 调用（`:1339-1348`）：

  ```python
      write_report_md(
          report_path,
          downloaded_all=downloaded_all, failed=failed, skipped=skipped,
          matching_result=matching_result,
          date_range=(args.start, args.end),
          iteration=iteration,
          supplemental=args.supplemental,
          aggregation=aggregation,
          out_of_range_items=missing_payload.get("out_of_range_items", []),
      )
  ```

  替换为：

  ```python
      write_report_md(
          report_path,
          downloaded_all=downloaded_all, failed=failed, skipped=skipped,
          matching_result=matching_result,
          date_range=(args.start, args.end),
          iteration=iteration,
          supplemental=args.supplemental,
          aggregation=aggregation,
          out_of_range_items=missing_payload.get("out_of_range_items", []),
          ignored_records=ignored_records,  # v5.7 Unit 4
      )
  ```

  同样找 `_run_postprocess_only` 里的 `write_report_md` 调用（`:922-928` 或附近）—— 如果该函数内也有此调用，同样加 `ignored_records=ignored_records`。**先 grep 确认**：

  ```bash
  grep -n "write_report_md(" scripts/download-invoices.py
  ```

  对每个调用点都加参数。

  然后找 `print_openclaw_summary` 调用（比 `write_report_md` 靠后几十行）：

  ```bash
  grep -n "print_openclaw_summary(" scripts/download-invoices.py
  ```

  对每个调用点加 `ignored_count=len(ignored_records)`：

  示例：

  ```python
      print_openclaw_summary(
          aggregation=aggregation,
          output_dir=output_dir,
          zip_path=zip_path,
          csv_path=csv_path,
          md_path=report_path,
          log_path=log_path,
          missing_status=missing_payload["recommended_next_action"],
          date_range=(args.start, args.end),
          writer=say,
          ignored_count=len(ignored_records),  # v5.7 Unit 4
      )
  ```

- [ ] **Step 4.9: 跑所有 Unit 4 测试验证通过**

  ```bash
  python3 -m pytest tests/test_postprocess.py::TestZipAtomic \
                    tests/test_postprocess.py::TestPrintOpenClawSummary \
                    tests/test_postprocess.py::TestIgnoredCtaRendering -v
  ```

  Expected: 全绿

- [ ] **Step 4.10: 跑 agent 合约测试确认无回归**

  ```bash
  python3 -m pytest tests/test_agent_contract.py::TestMatchingTiersContract \
                    tests/test_agent_contract.py::TestMissingJsonSchemaContract \
                    tests/test_agent_contract.py::TestZipManifestContract \
                    tests/test_agent_contract.py::TestChatSentinelContract -v
  ```

  Expected: 全绿（`ignored_records=None` 默认值保证现有调用点不破）

- [ ] **Step 4.11: （可选）手工跑一次 `--no-llm` 验证**

  如果本地 Gmail token 已经配置好，手工冒烟：

  ```bash
  python3 scripts/download-invoices.py --no-llm --start 2026/01/01 --end 2026/01/31 --output /tmp/v57-unit4-smoke 2>&1 | tail -30
  ```

  期望的肉眼检查：如果有 IGNORED 记录（无论是 `--no-llm` 下的 UNPARSED 还是 IGNORED），确认报告最后有「已忽略」节；没有也正常（区间内可能没有 IGNORED 样本）。

- [ ] **Step 4.12: Commit**

  ```bash
  git add scripts/postprocess.py scripts/download-invoices.py tests/test_postprocess.py
  git commit -m "feat(report): IGNORED section + learned_exclusions CTA (v5.7 Unit 4)

  Three user-facing notifications:
  1. write_report_md renders '📭 已忽略的非报销票据 (N)' section with
     per-record listing, plus a CTA block aggregating -from:<domain>
     hints by email domain. N=0 omits the entire section.
  2. print_openclaw_summary adds '📭 已忽略 N 张非报销票据' after the
     'next action' line. N=0 omits the line.
  3. zip_output excludes IGNORED_*.pdf (UNPARSED_*.pdf still zipped so
     user can see failed-to-parse receipts)."
  ```

---

## Unit 5: 文档登记 — SKILL.md v5.7 Lessons Learned

**Goal:** 把 prompt-层 + classifier-层的双层防御、IGNORED 记录处理规则、为什么拒绝 `missing.json.ignored_count` 字段，都登记到 `SKILL.md` 的 Lessons Learned，防止下次 snapshot 同步 reimbursement-helper 时误覆盖。

**Requirements:** R1 / R5 sub-requirement + 项目 CLAUDE.md 编辑规范

**Dependencies:** Unit 0-4 全部落地

**Files:**
- Modify: `SKILL.md`（`## Lessons Learned` 节，在 `🟢 v5.3 — scripts/core/ 是快照` 之后、`🔴 12306` 之前插入新条目）

### Steps

- [ ] **Step 5.1: 插入 SKILL.md v5.7 Lessons Learned 条目**

  打开 `SKILL.md`，找到：

  ```md
  ### 🟢 v5.3 — scripts/core/ 是快照，不会自动同步

  `scripts/core/` 从 `reimbursement-helper/backend/agent/utils/` 复制（commit `a0e8515`）。reimbursement-helper 未来新增酒店品牌 / 新类别 / 修 bug 时，**本目录不会自动跟**。

  **检测漂移**：`diff -r scripts/core/ ~/reimbursement-helper/backend/agent/utils/`
  **手动同步**：把变更逐文件 cherry-pick 过来（注意 `classify.py` 已删了 `detect_meal_type` 的随机逻辑，不要覆盖这个删除）。

  ### 🔴 12306 支付通知邮件无附件
  ```

  替换为（在 v5.3 条目和 12306 条目之间插入新 v5.7 条目）：

  ```md
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
  ```

- [ ] **Step 5.2: 验证插入位置**

  ```bash
  grep -n "IGNORED 白名单" SKILL.md
  grep -n "^### 🟢 v5" SKILL.md | head -5
  ```

  Expected: 有命中；v5.7 条目在 v5.3 之后、12306 条目之前。

- [ ] **Step 5.3: 验证 `scripts/core/__init__.py` 已登记（由 Unit 0 / 1 完成，此步只是 sanity check）**

  ```bash
  grep -n "v5.7" scripts/core/__init__.py
  ```

  Expected: 两处命中（prompts.py v5.7 rule + classify.py v5.7）

- [ ] **Step 5.4: Commit**

  ```bash
  git add SKILL.md
  git commit -m "docs(SKILL): Lessons Learned v5.7 — IGNORED whitelist classification

  Register the double-layer defense (prompt + classifier) for non-
  reimbursable receipts against snapshot-sync overwrites. Explicitly
  documents what NOT to do: don't re-add loose docType keyword logic,
  don't add balance/arrival/departure to narrow gate, don't add
  missing.json.ignored_count."
  ```

---

## Unit 6 (Optional / Deferred): UNKNOWN carve-out cleanup

**Goal:** Key Decision 承诺"UNKNOWN 合并进 IGNORED 后，下游 `category not in {"UNPARSED", "UNKNOWN"}` 这类 carve-out 可以逐步清掉"——本 Unit 把剩余的 UNKNOWN 相关死代码全部删除。

**Requirements:** Key Decision（非硬 R-要求）

**Dependencies:** Unit 1-5 全部落地 + 观察**至少一次**真实生产运行，确认 UNKNOWN 在新 fallthrough 下确实永不产生。

**Recommendation:** **本 Unit 推荐作为独立 issue 在 Unit 1-5 落地后观察一两次真实运行再启动**——除非实施阶段观察到明显可见残留（空节、空行）才立刻执行。如果决定跳过，整个 Plan 仍可 close；未来哪次运行看到 `"UNKNOWN"` 字样即可再开 Unit 6。

**Files (if executed):**
- Modify: `scripts/postprocess.py`（`CATEGORY_LABELS["UNKNOWN"]` 删除；`CATEGORY_ORDER["UNKNOWN"]` 删除；`build_aggregation` / `_single_row` 相关 UNKNOWN 分支清理）
- Modify: `scripts/download-invoices.py`（摘要表循环移除 `"UNKNOWN"`；「📄 其他发票」章节删除）

### Steps (deferred — only if taking action)

- [ ] **Step 6.1: grep 确认所有 UNKNOWN 引用都是死代码**

  ```bash
  grep -rn "\"UNKNOWN\"" scripts/ | grep -v invoice_helpers.py
  grep -rn "'UNKNOWN'" scripts/ | grep -v invoice_helpers.py
  ```

  审阅每处——看是否是 classify_invoice 返回值的消费者（post-v5.7 不会再有返回 UNKNOWN 的路径）。

- [ ] **Step 6.2: 删除 `postprocess.py` 里的 UNKNOWN 条目**

  （内容按 grep 结果定制，此处仅给框架；实施时按观察到的实际残留写具体 diff）

- [ ] **Step 6.3: 删除 `download-invoices.py` 里的 UNKNOWN 报告循环**

  （同上）

- [ ] **Step 6.4: 跑全套测试**

  ```bash
  python3 -m pytest tests/ -q 2>&1 | tail -10
  ```

  Expected: 全绿。`TestCategoryConstants` 的 `UNPARSED == 99` invariant 不受影响。

- [ ] **Step 6.5: Commit**

  ```bash
  git add scripts/postprocess.py scripts/download-invoices.py
  git commit -m "chore(cleanup): remove UNKNOWN dead code after v5.7 fallthrough rename"
  ```

---

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| Unit 0 prompt rule 被 LLM 违反（subscription 区间仍被填进 arrivalDate/departureDate） | 双层防御——Unit 1 narrow gate 是第二道闸；LLM 即使违反 prompt，只要返回字段里没 ≥2 个 hotel-specific 字段就仍然回退 IGNORED |
| Unit 1 水单 docType narrow gate 误踢合法历史水单 | Unit 1 Step 1.7 离线回放脚本扫 cache；差集含合法水单 → 固化 fixture + 回头放宽 |
| Unit 3 IGNORED 文件名截断导致 `.pdf` 扩展丢失 | 复用 UNPARSED 分支的 sanitize 后 re-append `.pdf` 模式（Unit 3 Step 3.5 中实现） |
| Unit 3 `rename_by_ocr` IGNORED 分支 `os.rename` 失败 | try/except OSError 降级为 UNPARSED 分支，保持三路交付一致 |
| Unit 4 `write_report_md` 现有调用点不传 `ignored_records` | 参数默认值 `None` 保证向后兼容；参数加进调用点后 `TestMatchingTiersContract` 仍绿 |
| `convergence_hash` 在 IGNORED 增长时假收敛（已知限制，显式接受） | UX 降级处理：报告 + OpenClaw 里「已忽略 N 张」计数 + CTA 让用户感知；不强改 Agent 合约。评审补充 #3（ignored_count 字段）显式拒绝 |
| 历史 OCR cache 里 SaaS 样本已经把 subscription 填进了 arrival/departure（Unit 0 只对新 OCR 生效） | 用户可选手动 `rm ~/.cache/gmail-invoice-downloader/ocr/*.json` 强制重跑 OCR；或者靠 Unit 1 的 narrow gate 兜底（即便 fields 路径因历史 cache 误触发 HOTEL_FOLIO，docType 路径不走此分支） |
| snapshot 同步 reimbursement-helper 时覆盖 prompts.py / classify.py 本地改动 | Unit 0 / 1 / 5 明确登记——每次 sync 前对照 `scripts/core/__init__.py` 的 Modifications from source 清单 + SKILL.md Lessons Learned v5.7 条目 + `TestPromptContract` 合约测试守护 |

---

## Documentation / Operational Notes

- `SKILL.md § Lessons Learned` 新增一条 `🟢 v5.7`（Unit 5）
- `scripts/core/__init__.py` 的 Modifications from source 清单新增两条（Unit 0 / 1）
- `scripts/dev/replay_classify.py` committed 作为 verification artifact（Unit 1 Step 1.6）
- `tests/fixtures/ocr/legitimate_folios/` 目录创建（Unit 1 Step 1.8）
- `missing.json` schema 不变（schema_version `"1.0"`），agent 合约完全透明
- 不需要 CHANGELOG 独立更新——项目 CLAUDE.md 要求 version label 只在 `SKILL.md` 第 10 行（当前 v5.6，release 时手动 bump 到 v5.7）
- **Release 时的手工步骤**：
  1. `SKILL.md:10` `# Gmail Invoice Downloader (v5.6)` → `(v5.7)`
  2. 编辑 `CHANGELOG.md` 或等价文档，总结本 Plan 的四单元
- 手工回归在 2026Q2 / Q3 季度烟雾测试里额外关注：
  - Termius / Anthropic / OpenRouter / GitHub / Figma 等 SaaS 供应商样例落成 `IGNORED_*.pdf`
  - 下载报告最后一节「已忽略的非报销票据」渲染且 CTA 块给出 `-from:` 行
  - 若新发现其他英文 SaaS 供应商仍落入 HOTEL_FOLIO / MEAL 等类，Unit 1 的 narrow gate 需进一步扩展到其他路径

---

## Sources & References

- **Origin document:** `docs/brainstorms/2026-05-02-ignore-non-reimbursable-receipts-review-integration-requirements.md`
- **Prior brainstorm:** `docs/brainstorms/2026-05-01-ignore-non-reimbursable-receipts-requirements.md`
- **Superseded plan (historical baseline):** `docs/plans/2026-05-01-003-feat-ignore-non-reimbursable-receipts-plan.md`
- **Bug report:** `docs/solutions/2026-05-01-termius-saas-misclassified-as-hotel-folio.md`（2025Q4 discovery）
- **External review:** 飞书文档 `GWf0dx50KoBiwsxH70lcBvwOnOh`（Agama, 2026-05-02）
- **Related plan (aggregated-summary):** `docs/plans/2026-05-01-002-feat-aggregated-summary-output-plan.md`（`print_openclaw_summary` 来源；Unit 4 依赖该函数）
- **Related code:**
  - `scripts/core/classify.py:296-379`（classify_invoice）
  - `scripts/core/classify.py:142-162`（is_hotel_folio_by_doctype）
  - `scripts/core/classify.py:60-88`（is_hotel_folio_by_fields — 本 Plan 不动）
  - `scripts/core/prompts.py:67-86`（酒店水单字段表 — Unit 0 在其后插入 rule）
  - `scripts/postprocess.py:339-405`（rename_by_ocr — Unit 3 加第三条分支）
  - `scripts/postprocess.py:608+`（do_all_matching — 本 Plan 不动）
  - `scripts/postprocess.py:828+`（build_aggregation — Unit 3 加防御性断言）
  - `scripts/postprocess.py:1060-1186`（print_openclaw_summary — Unit 4 加 ignored_count）
  - `scripts/postprocess.py:1587-1646`（zip_output — Unit 4 加 IGNORED_ 排除）
  - `scripts/download-invoices.py:488-760`（write_report_md — Unit 4 加节 + CTA）
  - `scripts/download-invoices.py:318-481`（三个 download_* 函数 — Unit 2 加 sender 字段）
  - `scripts/download-invoices.py:1310-1360`（main 调用点 — Unit 3 / 4 切分和传参）
  - `scripts/invoice_helpers.py:626-667`（classify_email — Unit 2 扩返回 dict）
- **Project conventions:** `CLAUDE.md` § Editing norms（scripts/core/ 是 snapshot，本地修改必须登记到 `__init__.py` + SKILL.md Lessons Learned）
