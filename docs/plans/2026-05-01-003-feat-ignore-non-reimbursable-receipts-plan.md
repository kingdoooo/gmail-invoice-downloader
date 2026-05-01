---
title: 非「发票 / 水单 / 行程单」票据一律过滤（IGNORED 分类）
type: feat
status: active
date: 2026-05-01
origin: docs/brainstorms/2026-05-01-ignore-non-reimbursable-receipts-requirements.md
---

# 非「发票 / 水单 / 行程单」票据一律过滤（IGNORED 分类）

## Overview

`classify_invoice` 的默认 fallthrough 出口从 `"UNKNOWN"` 改为 `"IGNORED"`，同时把 `is_hotel_folio_by_doctype` 单独命中的水单分支收窄——必须同时出现至少一个结构字段（`hotelName / confirmationNo / internalCodes / roomNumber`）才算水单。IGNORED 记录加 `IGNORED_` 文件名前缀保留在输出目录，不进入 matching / CSV / zip / `missing.json.items[]`，只在下载报告末尾的「已忽略」章节和 OpenClaw 汇总一行里对用户可见。Agent 合约（`missing.json` schema / `convergence_hash` / `status` / `recommended_next_action`）**完全不动**。

这是对 2025Q4 烟雾测试中 Termius 英文 SaaS 发票被误判为 `HOTEL_FOLIO` 问题的最小实现——用户明确选择白名单化而非 per-type sanity check 的策略，但也明确"不要设计太复杂"，所以放弃了 schema 升级 / 三路通知 / Agent 消费 `ignored[]` 等扩展。

## Problem Frame

2025Q4 烟雾测试发现 Termius（$120 USD Stripe 订阅发票）被 LLM 分类为 `HOTEL_FOLIO`，进入酒店匹配管道后永远不会匹配，落到 `missing.json.items[]` 作为一条永远修不好的 `hotel_invoice` 缺口。Agent loop 通过 `convergence_hash` 走到 `status=converged`，但这是假收敛——该记录根本不属于可报销范围。下一个英文 SaaS 供应商（Notion / Figma / GitHub / Linear ...）必然会再踩同一个坑。

根因在 `scripts/core/classify.py:339-341`：`is_hotel_folio_by_doctype` 的关键字列表包含非常宽松的 "Statement" / "INFORMATION INVOICE"，Stripe 英文模板很容易命中；只要 LLM 把 docType 写成这类模糊值，记录就被塞进 HOTEL_FOLIO。

用户框架：**发票 / 水单 / 行程单都是格式相对固定的票据，不符合就直接过滤**。不需要分"五道门"、不需要为未来假想供应商做架构。

见 origin: `docs/brainstorms/2026-05-01-ignore-non-reimbursable-receipts-requirements.md`

## Requirements Trace

- **R1.** `classify_invoice` 新增合法返回值 `"IGNORED"`，fallthrough 默认从 `"UNKNOWN"` 改为 `"IGNORED"`；`is_hotel_folio_by_doctype` 分支收窄（需结构字段兜底）。
- **R2.** `rename_by_ocr` 对 IGNORED 记录改名 `IGNORED_{sender_short}_{原名}.pdf`；`do_all_matching` 的 `valid_records` 进入前按 category 切分独立的 `ignored_records` 列表；`CATEGORY_LABELS` / `CATEGORY_ORDER` 登记 IGNORED。
- **R3.** 下载报告末尾新增「已忽略的非报销票据 (N)」章节（N=0 省略）；`print_openclaw_summary` 追加「📭 已忽略 {N} 张」一行（若 N>0）；`zip_output` 排除 `IGNORED_*.pdf`（保留 `UNPARSED_*.pdf` 现有行为）。
- **R4.** `missing.json` schema 不动。IGNORED 记录**不**进 `items[]`、**不**影响 `convergence_hash` / `status` / `recommended_next_action`。schema_version 保持 `"1.0"`。

## Scope Boundaries

- **不**改 `scripts/core/prompts.py`（OCR prompt 是与 reimbursement-helper 的共享契约）。
- **不**引 `learned_exclusions.json` 的新规则（两者正交保留）。
- **不**新增 LLM 调用（IGNORED 判定纯粹消费现有 OCR 字段）。
- **不**改 `scripts/invoice_helpers.py::classify_email` 的 Gmail-level 邮件分类逻辑（只扩返回 dict 的字段）。
- **不**动 Agent 合约：`status / recommended_next_action / convergence_hash / schema_version` 全部保持当前语义和字面量。
- **不**做"自动学习哪些 sender 应忽略"、"根据 IGNORED 计数触发 supplemental 暂停"等未来向自动化。

## Context & Research

### Relevant Code and Patterns

- **Classifier fallthrough**：`scripts/core/classify.py:375-377` 是唯一的 `category = 'UNKNOWN'` 赋值点；`classify.py:339-341` 是 docType-only 的水单分支。3-choose-2 的 `is_hotel_folio_by_fields`（`classify.py:335-337`）精度足够高、**不收窄**。
- **CATEGORY registries**：`scripts/postprocess.py:61-94`（`CATEGORY_LABELS` / `CATEGORY_ORDER`）——新类别必须两处都登记，否则 `rename_by_ocr` 会用 `get(category, "发票")` 兜底成「IGNORED_xxx_发票.pdf」。
- **`rename_by_ocr` 现有双分支**：`scripts/postprocess.py:314-369`，happy-path（`{date}_{vendor}_{label}.pdf`）+ UNPARSED-path（`UNPARSED_{msgid[:12]}_{base}.pdf`，带 `.pdf` re-append）。新增 IGNORED 分支可复用后者的 sanitize + re-append 套路。
- **Download record shape**：`scripts/download-invoices.py:358-365, 398-404, 470-478` 三个 `download_*` 函数构造的 dict **不含 `sender`**；`classified` 列表（`:916`）有 `sender`。`skipped[]` 同样带 `sender`（`:914`）——该字段存在于 classified 层但未穿过 download 层。
- **`classify_email` 返回 dict**：`scripts/invoice_helpers.py:613-781`。`sender` 键是完整 "Name \<email\>" 原始 header（`:631, :652`）；`sender_email` bare lowercase 在本地算出但不导出（`:638-639`）。
- **`do_all_matching` / `build_aggregation` 完整性断言**：`scripts/postprocess.py:756-761` `assert accounted == len(valid_records)`。IGNORED 切分最干净的位置在 `download-invoices.py:1012`（`valid_records` 构造行），matching 和 aggregation 只看 `reimbursable_records`——断言不需要修改。
- **`write_report_v53` 章节顺序**：`scripts/download-invoices.py:486-719`，现有 10 节。`## ⚠️ 需人工核查（LLM OCR 失败）`（`:685-692`）渲染 unparsed；新「已忽略」章节紧随其后最合适（N=0 整节省略，对齐 `if unparsed:` 的 gating）。
- **`print_openclaw_summary` 末尾结构**：`scripts/postprocess.py:828-944`，末尾依次「下一步」（`:921-934`）+ Deliverables（`:937-944`）。追加的「📭 已忽略」行插在「下一步」节尾 / Deliverables 前，N=0 省略。
- **`zip_output` allowlist**：`scripts/postprocess.py:1262-1321`，已有 `startswith("发票打包_") and endswith(".zip")` 的自排除（`:1296-1297`）；同位置加一行 `if fn.startswith("IGNORED_"): continue` 就够。
- **UNPARSED_ zip 行为**：目前 `.pdf` suffix 命中 allowlist、无前缀过滤，UNPARSED_ 进 zip——**保持不变**（brainstorm 明确：解析失败的票据用户需要看到）。
- **已知相关测试**：
  - `tests/test_postprocess.py::TestCategoryConstants`（`:1336-1356`）——category 常量 invariants
  - `tests/test_postprocess.py::TestRenameHappyPath`（`:298-334`）+ `TestPathTraversal`（`:74-123`）——rename 分支 + 路径安全
  - `tests/test_postprocess.py::TestZipAtomic`（`:341-387`）+ `tests/test_agent_contract.py::TestZipManifestContract`（`:767-843`）——zip 合约
  - `tests/test_postprocess.py::TestHotelMatchingTiers`（`:795+`）+ `TestMissingJsonStateMachine`（`:1059+`）+ `tests/test_agent_contract.py::TestConvergenceHashContract` / `TestStateMachineContract` / `TestMissingJsonSchemaContract`（`:499+`）——**全部必须绿且不需改**（R4 明确 schema 不动）
  - `tests/test_postprocess.py::TestBuildAggregation.test_row_count_matches_valid_records`（`:1576`）——完整性断言
  - `tests/test_agent_contract.py::TestMatchingTiersContract`（`:336-493`）——报告格式合约

### Institutional Learnings

- `docs/solutions/2026-05-01-termius-saas-misclassified-as-hotel-folio.md`（本 bug 原始记录）提出了 4 个候选，推荐 Option 3（HOTEL_FOLIO 专用 sanity check，若 date 字段全空则降级）。**本计划没走 Option 3**——brainstorm 阶段用户明确选择了更激进的白名单化方案（fallthrough → IGNORED），理由是 Option 3 只挡一条滑入路径。
- solution 文档里的测试建议「Termius-shaped fixture（英文、USD、'Corporation' in name、无 hotel-date 字段）→ 应路由到降级桶而非 HOTEL_FOLIO」直接适用于本计划的 `TestClassifyIgnored`。
- 收窄 `is_hotel_folio_by_doctype` 被原 solution 列为 Option 1（prompt 收窄）的兄弟方案；Option 1 因动 prompt 被否决，本计划只动 classify 不动 prompt，规避了上游 reimbursement-helper 的漂移风险。

### External References

无。纯 Python、无新 SDK、本地 pattern 充足（rename_by_ocr UNPARSED 分支、zip filter、report section gating 都已有先例）。

## Key Technical Decisions

- **fallthrough `UNKNOWN` → `IGNORED`（单点改动）**：brainstorm Key Decision 之一。不重写 classify 架构，只把最后一档默认从 UNKNOWN 换成 IGNORED；逐步把下游 `category not in {"UNPARSED", "UNKNOWN"}` 这类 carve-out 清掉作为**可选**收尾单元（非阻塞）。
- **水单收窄只改 `classify.py:339-341` 一行**：`is_hotel_folio_by_doctype(doc_type)` 命中后额外判断至少一个 `hotelName / confirmationNo / internalCodes / roomNumber / balance`。3-choose-2 的 `is_hotel_folio_by_fields` 路径**不动**（强特征路径本身精度足够高）。`balance` 加入白名单兜底字段，防止 LLM 只抽到一个日期字段的 Marriott 式折页（旧版本会通过 docType-only 捕获）被误判为 IGNORED；Stripe 类英文发票一般不返回 `balance` 这个水单特有字段。
- **IGNORED 切分在 `download-invoices.py:1012`（valid_records 构造处）**：brainstorm Outstanding Q 给了两个选项（`do_all_matching` 内部 vs 入口切分），选"入口切分" —— 不动 `do_all_matching` / `build_aggregation` / 完整性断言代码，最低侵入。`ignored_records` 作为独立列表传给 `write_report_v53` 和 `print_openclaw_summary`。
- **`sender_short` 从 email domain 取（`termius.com` → `termius`），硬上限 20 字符**：brainstorm 明确 sender 取邮件 `from` 头不走 LLM；文件名总长走 `IGNORED_{sender_short ≤20}_{原名 ≤140}.pdf`，总字节预算内。空 sender → `unknown`。
- **sender 字段在 `classify_email` 返回 dict 里扩字段**：新增 `"sender_email"`（bare lowercase，正则已经在 `invoice_helpers.py:638-639` 算了但未导出）；保持现有 `"sender"`（原 header value）。三个 `download_*` 函数把 `entry.get("sender")` 和 `entry.get("sender_email")` 放进 record。rename_by_ocr 的 IGNORED 分支**只读 `sender_email`**（domain label 形态），`sender` 传递下来仅供未来报告/调试用。语义注解：`sender` = 原始 header `"Name <addr>"`；`sender_email` = bare lowercase `addr`；`sender_short` = 文件名片段 = `sender_email.split("@",1)[-1].split(".")[0]`（如 "termius"）。
- **备选方案（记录不采纳理由）**：scope-guardian 建议改用 `IGNORED_{msgid[:12]}_{base}.pdf` 对称 UNPARSED 可完全去掉 Unit 2。不采纳理由：(a) msgid 对用户不可读，扫一眼 output_dir 认不出"Termius 这类是什么来的"，而报告里的 sender 行虽可查但切换上下文成本高；(b) classify_email 早已算出 sender_email，Unit 2 实际改动量仅 1 行 classify_email 返回 + 3 行 download_* record。保留 Unit 2 性价比可接受。
- **`CATEGORY_ORDER["IGNORED"]` 不赋值**：IGNORED 不进 CSV / aggregation rows，`_sort_key` 用不到它；留空让默认 `get(category, 50)` 返回 50 即可，避免和 UNPARSED=99 的 invariant 测试（`TestCategoryConstants`）产生互动。`CATEGORY_LABELS["IGNORED"] = "已忽略"` 必须登记（`rename_by_ocr` 虽走独立分支但防御性保留）。
- **UNKNOWN carve-out 清理作独立单元（可选）**：Key Decision 承诺"逐步清掉"，但本次只做最小必要：保留 `CATEGORY_LABELS["UNKNOWN"]` / `CATEGORY_ORDER["UNKNOWN"]` 和下游所有 `UNKNOWN` 分支不动。因为合并是 brainstorm 的意图而非硬要求，且 `download-invoices.py:558, 670-672` 的报告节假如实际永不渲染则为无害代码。单独一个 Unit 5 做统一清理，但标为**选做**。
- **水单收窄的回归验证通过离线回放而非新单元测试保证**：brainstorm Outstanding Q1。规划阶段在 Unit 1 的 Verification 里做一次 `~/.cache/gmail-invoice-downloader/ocr/*.json` 回放，统计新规则下 `HOTEL_FOLIO → IGNORED` 的差集并人工核对。差集非空且含合法水单 → 再放宽条件（例：允许 `balance + arrivalDate` 组合）或加 pytest fixture 锁定。

## Open Questions

### Resolved During Planning

- **Q: IGNORED 记录切分位置**（brainstorm Outstanding Q2）——选"`download-invoices.py` 入口切分 `valid_records`"，不动 `do_all_matching` 返回字典结构。
- **Q: sender 取哪一层**——从 `classify_email` 返回 dict 新增 `sender_email`（bare lowercase）字段，经三个 download_* 函数传递到 record，rename_by_ocr 读结构化字段。
- **Q: `CATEGORY_ORDER["IGNORED"]` 赋值**——不赋值，让 `get(..., 50)` fallback 即可（IGNORED 不进 aggregation rows，排序无关）。

### Deferred to Implementation

- **水单收窄差集具体样本**（brainstorm Outstanding Q1）：实施时跑离线回放脚本才能看到真实差集；若差集含合法水单，Unit 1 需回头放宽条件。此步写在 Unit 1 Verification 里，规划阶段不预测。
- **合并 UNKNOWN → IGNORED 的完整 carve-out 清理范围**：Unit 5 选做。如果实施时发现 `download-invoices.py:558, 670-672` 对空列表的渲染确实产生了"其他发票"空节或空行，就顺手清掉；如果本来就是 `if bucket:` gating 无副作用，可不动。
- **`print_openclaw_summary` 是否已具备 `ignored_count` 入参**：研究阶段确认函数已落地（`postprocess.py:828-944`），但没预留参数。实现时一起加 `ignored_count: int = 0` 关键字参数。

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
Gmail search → classify_email → download_{attachment,zip,link} → analyze_pdf_batch → rename_by_ocr → main loop split → matching → aggregation → writers
     (A)           (B+sender)           (C+sender)                    (D)                 (E+IGNORED)     (F)             (G)         (H)           (I)

(A) 不变
(B) classify_email 返回 dict 增加 "sender_email" 字段（bare lowercase）
(C) download_{attachment,zip,link} 构造的 record 里加 "sender" 和 "sender_email" 两个字段
(D) analyze_pdf_batch 不变
(E) rename_by_ocr 新增第三条分支：
      if category == "IGNORED":
          # 从 bare-email 取 domain label: "billing@termius.com" → "termius"
          sender_short = sender_email.split("@", 1)[-1].split(".")[0] or "unknown"
          new_name = sanitize(f"IGNORED_{sender_short[:20]}_{base}", max_len=200)
          ensure .pdf suffix after sanitize
          try/except OSError：rename 失败 → 降级为 UNPARSED 分支（见 Unit 3 Risks）
(F) 在 download-invoices.py main() 里 valid_records 构造之后切分：
      ignored_records = [d for d in valid_records if d.get("category") == "IGNORED"]
      reimbursable_records = [d for d in valid_records if d.get("category") != "IGNORED"]
    matching_result = do_all_matching(reimbursable_records)
    aggregation = build_aggregation(matching_result, reimbursable_records)
(G+H) 不变
(I) write_report_v53(ignored_records=...)  → 新增「已忽略」节 (N=0 省略)
    print_openclaw_summary(ignored_count=...)  → 追加「📭 已忽略 N 张」(N=0 省略)
    zip_output → 前缀过滤 IGNORED_*.pdf
    write_missing_json → 不变，IGNORED 完全不参与
```

classify.py 决策改动只有两处（两条等价的增量）：

```
Priority 1.4 (classify.py:339-341):
  OLD:  if not category and is_hotel_folio_by_doctype(doc_type):
            category = 'HOTEL_FOLIO'
  NEW:  if not category and is_hotel_folio_by_doctype(doc_type):
            if invoice.get('hotelName') or invoice.get('confirmationNo') \
               or invoice.get('internalCodes') or invoice.get('roomNumber') \
               or invoice.get('balance') is not None:
                category = 'HOTEL_FOLIO'
            # else: fall through to IGNORED

Priority 4 (classify.py:375-377):
  OLD:  if not category:
            category = 'UNKNOWN'
  NEW:  if not category:
            category = 'IGNORED'
```

## Implementation Units

- [ ] **Unit 1: classify.py — fallthrough 改 IGNORED + 水单分支收窄**

**Goal:** 把 `classify_invoice` 的默认出口从 `"UNKNOWN"` 换成 `"IGNORED"`，并收窄 docType-only 水单分支（必须至少有一个结构字段）。离线回放验证水单收窄不误踢历史合法水单。

**Requirements:** R1

**Dependencies:** 无。此单元独立，可先落地。

**Files:**
- Modify: `scripts/core/classify.py`（两处：`:339-341` docType narrow + `:375-377` fallthrough rename）
- Modify: `scripts/core/__init__.py`（Modifications from source 清单新增一条）
- Test: `tests/test_postprocess.py`（新增 `TestClassifyIgnored` 类）
- Create: `scripts/dev/replay_classify.py`（一次性离线回放脚本，用于 Verification，committed 而非临时）
- Create: `tests/fixtures/ocr/`（把回放发现的合法水单样本固化为 pytest fixture）

**Approach:**
- `classify.py:339-341` 改成：命中 `is_hotel_folio_by_doctype` 后，必须 `invoice.get('hotelName') or invoice.get('confirmationNo') or invoice.get('internalCodes') or invoice.get('roomNumber') or invoice.get('balance') is not None` 才赋 `HOTEL_FOLIO`；否则继续 fall through。**新增 `balance` 作第 5 个兜底字段**：防止 LLM 只抽到一个日期字段的 Marriott 式折页（旧版本会通过 docType-only 捕获）被误判为 IGNORED；Stripe 类英文发票一般不返回 `balance` 这个水单特有字段。
- `classify.py:375-377` 的 `category = 'UNKNOWN'` 改为 `category = 'IGNORED'`。
- `classify.py` 文件头的 MODIFIED 注释（`:13-18`）追加一条说明"fallthrough UNKNOWN→IGNORED + docType-only folio narrowed (requires hotelName/confirmationNo/internalCodes/roomNumber/balance)"。
- `scripts/core/__init__.py` 的 Modifications from source 清单新增第 7 条对称格式（见研究结果 §11）。**附加一行**：`classify_invoice is confidence-blind by design — downstream validation flags from validate_ocr_plausibility are additive and do not feed classification`（保证未来 upstream 若加 confidence-aware 逻辑时提醒回放脚本失效）。

**Execution note:** 测试先行（characterization-first 对分类器有价值）——先写 `TestClassifyIgnored` 里 Termius-shape 的 fixture（英文 docType "Invoice"、无 hotel 字段），观察当前代码返回 `HOTEL_FOLIO`，再改 classify.py 让它返回 IGNORED。收窄分支的测试同理：先构造 `docType="Statement"` 无结构字段的输入，断言应得 IGNORED。

**Patterns to follow:**
- `classify.py:13-18` 已有的 `MODIFIED for gmail-invoice-downloader v5.3` 注释格式
- `scripts/core/__init__.py` 现有第 2 条（`classify.py: 移除 detect_meal_type`）的登记格式
- `tests/test_postprocess.py::TestHotelMatchingTiers` 纯函数测试风格（构造 dict、调用 classify_invoice、断言返回值）

**Test scenarios:**
- Happy path — Termius 形状（`isChineseInvoice=False / vendorTaxId=None / docType="Invoice" / serviceType=None / vendorName="Termius Corporation" / 无 hotel 字段`）→ `classify_invoice(...) == "IGNORED"`
- Happy path — 合法中国发票（`isChineseInvoice=True / vendorTaxId=有效 18 位 / serviceType="*餐饮服务*餐饮费"`）→ `"MEAL"`（既有行为不回归）
- Happy path — 合法酒店水单 3-choose-2 路径（`roomNumber="1205" / arrivalDate="2025-11-10" / docType 任意`）→ `"HOTEL_FOLIO"`（强特征路径不受收窄影响）
- Edge case — 收窄分支命中（`docType="Statement" / hotelName="Marriott Shanghai"` / 其余字段空）→ `"HOTEL_FOLIO"`（结构字段兜底）
- Edge case — 收窄分支命中（`docType="Guest Folio" / balance=1260.0` / 其余空）→ `"HOTEL_FOLIO"`（`balance` 兜底；防止 Marriott 式单日期折页误杀）
- Edge case — 收窄分支不命中（`docType="Statement"` / `hotelName=None / confirmationNo=None / internalCodes=None / roomNumber=None / balance=None`）→ `"IGNORED"`（Termius 路径）
- Edge case — 空 invoice dict → `"IGNORED"`（原先 `"UNKNOWN"`，行为变更）
- Regression — 历史合法水单（从 OCR cache 回放固化到 `tests/fixtures/ocr/legitimate_folios/*.json` 的样本）→ 新规则下仍分类为 `"HOTEL_FOLIO"`（差集为空则本测试可空；差集非空则逐个样本锁定）

**Verification:**
- `pytest tests/test_postprocess.py::TestClassifyIgnored -v` 全绿
- `pytest tests/test_postprocess.py::TestHotelMatchingTiers -v` 全绿（水单既有路径无回归）
- **离线回放（artifact）**：`scripts/dev/replay_classify.py` committed 到仓库，遍历 `~/.cache/gmail-invoice-downloader/ocr/*.json`，对每个 ocr dict 运行旧 classify 和新 classify，输出 `(path, 旧分类, 新分类)` 差集。特别关注 `HOTEL_FOLIO → IGNORED` 的记录。人工核查这些记录的 PDF 是否真实水单。
  - 差集为空 → 正常完成，保留脚本供未来同步 reimbursement-helper 时再跑一次
  - 差集非空且含合法水单 → 把样本（脱敏后）固化到 `tests/fixtures/ocr/legitimate_folios/*.json` + 新增 `TestHotelFolioNarrowing` 测试类，断言这些样本新规则下仍分类为 `HOTEL_FOLIO`；若规则无法覆盖再回头放宽（e.g. 允许 `transactionAmountNoVAT + arrivalDate` 组合）
  - 差集全是非水单（Stripe/SaaS 等）→ 预期结果，直接接受
- 脚本本身极短（~30 行），提交到 `scripts/dev/` 而非一次性删除，便于下次 reimbursement-helper snapshot sync 时快速验证 classify.py 本地修改未破坏历史样本

---

- [ ] **Unit 2: 邮件层 sender 字段穿透到 download record**

**Goal:** 让 download 层构造的 record 带上 `sender` 和 `sender_email` 字段，供 Unit 3 的 `rename_by_ocr` 消费。

**Requirements:** R2（前置依赖）

**Dependencies:** Unit 1 可以并行或先行——此单元不读分类结果。

**Files:**
- Modify: `scripts/invoice_helpers.py`（`classify_email` 返回 dict 扩一个 `sender_email` 键）
- Modify: `scripts/download-invoices.py`（`download_attachment`、`download_zip`、`download_link` 三处 record 构造点加 `"sender"` 和 `"sender_email"` 字段）
- Test: `tests/test_postprocess.py` 或 `tests/test_invoice_helpers.py`（若存在；否则加在 test_postprocess.py 里）

**Approach:**
- `invoice_helpers.py::classify_email` 在 `:638-639` 已经算出 `sender_email = bare lowercase`，在返回 dict（`:652` 附近）把它加进去。保持 `"sender"` 为完整 header 原值（向后兼容）。
- 三个 `download_*` 函数的 record 构造处加：
  ```
  "sender": entry.get("sender", ""),
  "sender_email": entry.get("sender_email", ""),
  ```
- `main()` 调用处已经直接 iterate 从 `classified`（含 sender）走到 download，不需要额外传参。

**Patterns to follow:**
- `download-invoices.py:358-365` 已有的 record dict 构造写法（extend，不重写）
- `invoice_helpers.py:916` 的 skipped[] 附加 sender 的现有做法

**Test scenarios:**
- Happy path — 构造 `entry = {"sender": "Billing <billing@termius.com>", ...}`，调用 `classify_email`，断言返回 dict 含 `sender_email == "billing@termius.com"`。
- Edge case — sender 里没有 `<email>` 格式（纯 email `kent@example.com`）→ `sender_email == "kent@example.com"`
- Edge case — 空 sender → `sender_email == ""`

**Verification:**
- 上述单元测试全绿
- `pytest tests/ -q` 全绿（不得破坏任何现有 download / classification 测试）

---

- [ ] **Unit 3: postprocess IGNORED 记录处理（rename + CATEGORY 登记 + matching 入口切分）**

**Goal:** IGNORED 记录进 `rename_by_ocr` 的第三条分支得到 `IGNORED_{sender_short}_{原名}.pdf` 文件名；`main()` 在 `valid_records` 构造后把 IGNORED 切出独立列表，matching 和 aggregation 只处理 reimbursable 记录。

**Requirements:** R2

**Dependencies:** Unit 1（IGNORED 分类存在）+ Unit 2（sender 字段可读）

**Files:**
- Modify: `scripts/postprocess.py`（`CATEGORY_LABELS` 新增 `"IGNORED": "已忽略"`；`rename_by_ocr` 新增 IGNORED 分支）
- Modify: `scripts/download-invoices.py::main`（`valid_records` 切分 → `ignored_records` + `reimbursable_records`；do_all_matching / build_aggregation 调用改用 reimbursable）
- Test: `tests/test_postprocess.py`（扩 `TestRenameHappyPath` 或新增 `TestRenameIgnoredBranch`）

**Approach:**
- `CATEGORY_LABELS["IGNORED"] = "已忽略"`；`CATEGORY_ORDER` **不**登记 IGNORED（让 `get(..., 50)` fallback 兜底，符合 TestCategoryConstants invariant）。
- **防御性断言**：在 `build_aggregation` 函数入口或 `_sort_key` 之前加 `assert all(r.category != "IGNORED" for r in rows), "IGNORED leaked past main() split — Unit 3 filter broke"`。main() 切分是 IGNORED 不进 aggregation 的唯一屏障，没有该断言则未来重构静默回归无法发现。
- **rename_by_ocr IGNORED 分支 `os.rename` 失败处理**：包在 `try/except OSError`；失败时降级为 UNPARSED 分支——把 `category="UNPARSED"`、`analysis["error"]=f"IGNORED rename failed: {e}"`，让它走 UNPARSED_ 前缀路径进 zip。这样三份交付（报告 / CSV / zip）仍然自洽：记录是 UNPARSED，用户能看到。
- **清理 `postprocess.py:706` 的 UNKNOWN carve-out**：把 `category not in {"UNPARSED", "UNKNOWN"}` 改为 `category not in {"UNPARSED", "IGNORED"}`——Unit 3 split 走通后 UNKNOWN 永不再产生，该处碰巧不踩只是因为 IGNORED 根本进不到 `_single_row`；改成 IGNORED 让语义跟上当下路由。
- `rename_by_ocr` 在现有 UNPARSED 分支（`:340-354`）和 happy 分支（`:356-368`）之间插入 IGNORED 分支：
  - 读 `record.get("sender_email", "")`
  - 提取 domain label：`sender_email.split("@", 1)[-1].split(".")[0] or "unknown"`，截到 20 字符
  - `new_name = sanitize_filename(f"IGNORED_{sender_short}_{base}", max_len=200)`
  - 若 sanitize 后不以 `.pdf` 结尾则 re-append（同 UNPARSED 分支 `:345-347` 的套路）
  - `os.rename` + `record["path"]` 更新 + `record["vendor_name"]` / `record["transaction_date"]` 填 fallback（同 UNPARSED 分支）
- `main()` 在 `valid_records = [d for d in downloaded_all if d.get("valid")]` 后加：
  ```
  ignored_records = [d for d in valid_records if d.get("category") == "IGNORED"]
  reimbursable_records = [d for d in valid_records if d.get("category") != "IGNORED"]
  ```
  后续 `do_all_matching(...)` 和 `build_aggregation(...)` 全部传 `reimbursable_records`，并把 `ignored_records` 传给 writers（Unit 4 会用）。

**Patterns to follow:**
- `postprocess.py:340-354` UNPARSED rename 分支的 sanitize + re-append .pdf 模式
- `postprocess.py:421-426` `do_all_matching` 内部 `cat = d.get("category") or "UNPARSED"` 的 category 读取约定

**Test scenarios:**
- Happy path — Termius record（`category="IGNORED"`, `sender_email="billing@termius.com"`, path="/tmp/x.pdf"）→ 改名为 `IGNORED_termius_x.pdf`
- Happy path — 空 sender_email → `IGNORED_unknown_{原名}.pdf`
- Edge case — sender_email 是超长 company name（伪造 email `foo@reallylongdomainname-with-suffix.co`）→ domain label 截到 20 字符以内，文件名总长 ≤200
- Edge case — sender 里带 `@` 的奇怪 domain（`foo@bar@baz.com`）→ sanitize 干净，不爆 `os.rename`
- Integration — `main()` 级别：构造 10 条 valid_records（5 reimbursable + 5 IGNORED），确认 `do_all_matching` 接收的 records 数 == 5；`build_aggregation` 的完整性断言不 fire（`accounted == len(reimbursable_records) == 5`）
- Edge case — `valid_records` 全是 IGNORED → `reimbursable_records` 为空，`do_all_matching` 正常返回空 buckets，断言不 fire

**Verification:**
- `pytest tests/test_postprocess.py::TestRenameIgnoredBranch -v` 绿
- `pytest tests/test_postprocess.py::TestRenameHappyPath tests/test_postprocess.py::TestPathTraversal -v` 无回归
- `pytest tests/test_postprocess.py::TestBuildAggregation -v` 无回归（完整性断言仍成立）

---

- [ ] **Unit 4: 用户通知三路——报告节 + OpenClaw summary + zip 过滤**

**Goal:** 下载报告末尾渲染「已忽略的非报销票据 (N)」章节（N=0 省略）；`print_openclaw_summary` 追加「📭 已忽略 {N} 张」一行（N>0 时）；`zip_output` 的输出包里排除 `IGNORED_*.pdf`。

**Requirements:** R3

**Dependencies:** Unit 3（`ignored_records` 列表已可传）

**Files:**
- Modify: `scripts/download-invoices.py::write_report_v53`（新增 `ignored_records=None` 关键字参数——**默认 None 是必须的**，否则 `tests/test_agent_contract.py::TestMatchingTiersContract._report_for` 现有调用点不传该参数会破坏合约测试 + 「已忽略」章节渲染）
- Modify: `scripts/postprocess.py::print_openclaw_summary`（新增 `ignored_count: int = 0` 关键字参数 + 末尾一行渲染，N=0 省略）
- Modify: `scripts/postprocess.py::zip_output`（basename 前缀过滤 `IGNORED_`）
- Modify: `scripts/download-invoices.py::main`（两个 writer 调用点传 `ignored_records` / `ignored_count`）
- Test: `tests/test_postprocess.py`（zip 过滤）+ `tests/test_agent_contract.py`（报告章节 + OpenClaw summary 合约）

**Approach:**
- **报告节**：渲染位置在 `download-invoices.py:685-692`（unparsed 节）之后、`:694-709`（补搜建议节）之前。格式：
  ```
  ## 📭 已忽略的非报销票据 (N)

  以下票据被识别为非发票 / 非水单 / 非行程单，已自动过滤，不进入 CSV / 打包 zip。文件仍保留在 PDFs 目录下以 `IGNORED_` 前缀标记，可人工核查。

  - {sender_email 或 "未知发件人"}：{金额+币种 或 "金额未识别"}
  ```
  金额：优先 `record.get("ocr", {}).get("transactionAmount")`，无则「金额未识别」；币种若 OCR 无则省略。N=0 整节省略（`if ignored_records:`）。
- **OpenClaw summary 行**：在 `postprocess.py:936-944` 的 Deliverables 行之前加：
  ```
  if ignored_count:
      say(f"📭 已忽略 {ignored_count} 张非报销票据（详见下载报告.md）")
  ```
- **zip 过滤**：`postprocess.py:1296-1297` 的现有 `if fn.startswith(ZIP_PREFIX)...` 下加：
  ```
  if fn.startswith("IGNORED_"):
      continue
  ```
- `main()` 调用点：`write_report_v53(..., ignored_records=ignored_records)`；`print_openclaw_summary(..., ignored_count=len(ignored_records))`。

**Patterns to follow:**
- `download-invoices.py:685-692` 「⚠️ 需人工核查」节的 `if unparsed:` gating + per-record 循环渲染
- `postprocess.py:1296-1297` 的文件名前缀排除写法
- `postprocess.py:831-839` `print_openclaw_summary` 的 keyword arg 添加方式

**Test scenarios:**
- Happy path — `write_report_v53` 传 `ignored_records=[{"sender_email": "billing@termius.com", "ocr": {"transactionAmount": 120.0}}]` → 报告 Markdown 含 `## 📭 已忽略的非报销票据 (1)` 和 `billing@termius.com` + `120`
- Edge case — `ignored_records=[]` 或 `None` → 报告 Markdown **不包含**字串 `已忽略的非报销票据`
- Edge case — 缺失 ocr amount → 条目显示「金额未识别」
- Integration — 构造一条 IGNORED record（文件名 `IGNORED_termius_Q9YJOO4I.pdf` 放入 pdfs_dir）+ 一条正常 PDF，运行 `zip_output` → zip 清单只含正常 PDF，IGNORED_*.pdf 不在
- Edge case — `UNPARSED_xxx.pdf` 同时存在 → 仍然进 zip（保持现有行为）
- Happy path — `print_openclaw_summary(..., ignored_count=3)` 的 stdout 含 `📭 已忽略 3 张非报销票据`
- Edge case — `ignored_count=0` → stdout **不**含「已忽略」字串

**Verification:**
- `pytest tests/test_postprocess.py::TestZipAtomic tests/test_agent_contract.py::TestZipManifestContract -v` 全绿（含新增「IGNORED 不进 zip」）
- `pytest tests/test_agent_contract.py::TestMatchingTiersContract -v` 全绿（现有节不回归）
- 手工运行一次 `python3 scripts/download-invoices.py --no-llm --query ...` 或拿已有 Termius fixture 跑一遍，肉眼检查报告、zip 内容、OpenClaw 输出

---

- [ ] **Unit 5: 文档登记（Lessons Learned + Modifications 清单完善）**

**Goal:** 把 classify.py 的改动登记到 CLAUDE.md 规范要求的两处（`scripts/core/__init__.py` 的 Modifications from source + `SKILL.md` 的 Lessons Learned），避免下次 snapshot 同步时被无声覆盖。

**Requirements:** R1（sub-requirement）+ 项目 CLAUDE.md 编辑规范

**Dependencies:** Unit 1 完成。可与 Unit 4 并行或最后做。

**Files:**
- Modify: `scripts/core/__init__.py`（Unit 1 已加了新 modification，这里复核 + 补条目间的分隔/指针）
- Modify: `SKILL.md`（§ Lessons Learned 新增一条 `🟢 v5.4 — 白名单分类`）

**Approach:**
- `scripts/core/__init__.py` 末尾 `Sync:` 段之上加一行 `See SKILL.md § Lessons Learned "IGNORED 白名单分类" for rationale.`
- `SKILL.md:510` 的 `## Lessons Learned` 节，在 `🟢 v5.3 — scripts/core/ 是快照` 之后、下一条 `🔴` 之前，插入以日期而非版本号标记的条目（下方示例使用 `2026-05`，避免与 aggregated-summary plan 争用 v5.x 版号）：

  ```md
  ### 🟢 2026-05 — IGNORED 白名单分类：未命中即过滤

  **背景**：2025Q4 smoke 里 Termius 订阅发票（Stripe 模板 + 英文 docType "Invoice"）被 `is_hotel_folio_by_doctype` 的 "Statement"/"Invoice" 类关键字命中，滑入 HOTEL_FOLIO 管道永不匹配，成了 `missing.json` 里永远修不好的 hotel_invoice 缺口。

  **决策**：只处理「中国发票 / 酒店水单 / 行程单」三类格式相对固定的票据，其他一律过滤——`classify.py::classify_invoice` 的 fallthrough 出口从 `UNKNOWN` 改为 `IGNORED`，同时水单 docType-only 分支（`is_hotel_folio_by_doctype`）要求至少一个结构字段（`hotelName / confirmationNo / internalCodes / roomNumber`）。3-choose-2 的 `is_hotel_folio_by_fields` 强特征路径不动。

  **IGNORED 记录处理**：文件加 `IGNORED_` 前缀保留在 output_dir，不进 CSV / zip / `missing.json.items[]`，只在下载报告末尾「已忽略」节和 OpenClaw 汇总一行里可见。`missing.json` schema 保持 `"1.0"`，Agent 对「已忽略」完全透明（`convergence_hash` / `status` / `recommended_next_action` 全部无感知）。

  **不要再做**：回加「docType 含 Invoice/Statement 就是 HOTEL_FOLIO」的宽松逻辑——这是 Termius bug 的根因。同步 `~/reimbursement-helper/backend/agent/utils/classify.py` 时，注意保留本地 fallthrough + docType 收窄修改；同时参考 `scripts/core/__init__.py` 的 Modifications from source 清单。
  ```

**Patterns to follow:**
- `SKILL.md:510+` 的 Lessons Learned 既有条目格式（emoji 标签 + 背景 + 决策 + 不要再做）
- `scripts/core/__init__.py:7-18` 现有 Modifications from source 清单格式

**Test scenarios:**
- Test expectation: none —— 纯文档改动，无可执行行为

**Verification:**
- `grep -n "IGNORED 白名单" SKILL.md` 至少一处命中
- `grep -n "IGNORED" scripts/core/__init__.py` 至少一处命中
- 肉眼确认 Lessons Learned 节的插入位置保持时序（v5.3 之后、后续条目之前）

---

- [ ] **Unit 6（可选 / 推荐作为后续 issue）: UNKNOWN 完整 carve-out 清理**

**Goal:** Key Decision 承诺"UNKNOWN 合并进 IGNORED 后，下游 `category not in {"UNPARSED", "UNKNOWN"}` 这类 carve-out 可以逐步清掉"——本单元把剩余的 UNKNOWN 相关代码全部删除（bucket 字面量、CATEGORY_LABELS/ORDER、download-invoices.py 报告循环）。**注意**：Unit 3 已在 Approach 里把 `postprocess.py:706` 的 carve-out 改到 `{UNPARSED, IGNORED}`；Unit 6 是其余死代码的扫荡。**推荐作为独立 issue 在 Unit 1-5 落地后观察一两次真实运行再启动**——除非实施阶段观察到明显可见残留（空节、空行）才立刻执行。

**Requirements:** Key Decision（非硬 R-要求）

**Dependencies:** Unit 1-4 全部落地 + 手工回归测试观察到具体残留。

**Files（若做）:**
- Modify: `scripts/postprocess.py`（`:73` `CATEGORY_LABELS["UNKNOWN"]` 删除；`:92` `CATEGORY_ORDER["UNKNOWN"]` 删除；`:482, 744` 的 `"unknown"` bucket 循环清理；`:706` 的 `category not in {"UNPARSED", "UNKNOWN"}` 改成 `{"UNPARSED"}`）
- Modify: `scripts/download-invoices.py`（`:558` 摘要表循环移除 `"UNKNOWN"`；`:670-672` 「📄 其他发票」章节删除）

**Approach:**
- 不急于清理；先让 Unit 1-4 在生产环境跑一两次，观察 UNKNOWN 是否真的消失（理论上 IGNORED 替代后 classify_invoice 永不返回 UNKNOWN）。确认后再批量删除 UNKNOWN 相关代码路径。
- 每处改动都是删减，不引入新行为。

**Execution note:** 先在本地 grep 确认所有 UNKNOWN 引用都是死代码再删；保留 `tests/test_postprocess.py::TestCategoryConstants` 的 invariants。

**Test scenarios:**
- 所有既有测试必须保持绿。无新增测试——删除死代码不需要新测。

**Verification:**
- `pytest tests/ -q` 全绿
- `grep -rn "UNKNOWN" scripts/ | grep -v invoice_helpers.py` 应显著减少（invoice_helpers.py 的 UNKNOWN 是邮件级 doc_type，不动）

## System-Wide Impact

- **Interaction graph：** `classify_invoice` 的返回值被 `postprocess.rename_by_ocr` / `do_all_matching` / `build_aggregation` / `write_summary_csv` / `write_report_v53` / `write_missing_json` / `print_openclaw_summary` 七处消费。本计划改动覆盖前六处；`write_missing_json` 明确不动（R4）——它的 items 循环当前按 `category != "IGNORED"` 过滤不需要改（因为 IGNORED 记录根本不会进 matching，也就不会进 unmatched_invoices/unmatched_folios/unmatched_receipts，`write_missing_json` 只从这三个源头读）。
- **错误传播：** IGNORED 是正常分类路径的终点，不触发任何 exception。若 `rename_by_ocr` 的 IGNORED 分支 `os.rename` 失败，record 保持原 path，下游 zip 过滤用 basename startswith 仍然过滤不到——此为极低概率硬盘错误，允许留到 UNPARSED 机制捕获（rename 失败通常伴随磁盘满/权限）。
- **State lifecycle：** Agent loop 跨 iteration 的 `convergence_hash` 基于 `(item.type, item.needed_for)` 元组的 sorted tuple（`postprocess.py:1105-1117`）。IGNORED 不参与，hash 不变；既有 Agent 状态机对本特性完全透明。**已知限制**（brainstorm Outstanding Q 里提到）：若 iter1 和 iter2 的 `items[]` 完全相同但 `ignored[]` 增长，`convergence_hash` 仍相同会判为 `converged`——用户借报告和 OpenClaw 的「已忽略 N 张」计数发现 N 在增长，这是可接受的 UX 降级，不强改。
- **API surface parity：** 无——`classify_invoice` 返回值枚举扩展（`UNKNOWN` + `IGNORED`），是向下兼容的新增；CSV / zip / 报告格式对外稳定（报告只多一节、zip 文件数可能少）。
- **Integration coverage：** Unit 3 的 `main()` 切分 + Unit 4 的 writers 传参必须端到端跑通——单元测试 mock 得到的 records 不能证明 `main()` 里的字段流转，推荐 Unit 3/4 用已有的 Termius OCR 缓存跑一次 `python3 scripts/download-invoices.py --no-llm ...` 目测输出。
- **Unchanged invariants：**
  - `missing.json` schema_version `"1.0"`、`status` / `recommended_next_action` 枚举、`convergence_hash` 算法全部不动
  - `ALLOWED_ITEM_TYPES` = `{hotel_folio, hotel_invoice, ridehailing_receipt, ridehailing_invoice, extraction_failed}`——IGNORED **不**加入该枚举
  - `UNPARSED_*.pdf` 的 zip 行为（继续进 zip）
  - `CATEGORY_LABELS["UNPARSED"]` / `CATEGORY_ORDER["UNPARSED"]==99` invariant
  - `is_hotel_folio_by_fields` 3-choose-2 路径（强特征不收窄）

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Unit 1 水单 docType 收窄误踢合法历史水单 | Unit 1 Verification 步骤做离线回放（`~/.cache/gmail-invoice-downloader/ocr/*.json`），差集非空即回头放宽条件或加 fixture 锁定 |
| Unit 3 文件名截断导致 `.pdf` 扩展丢失 | 复用 UNPARSED 分支现有的 sanitize 后 re-append `.pdf` 模式（`postprocess.py:345-347`） |
| Unit 2 `classify_email` 扩字段破坏现有测试 | 扩字段是 addition-only，不改 `sender` 原 header 语义；先跑全套测试再改 |
| `convergence_hash` 在 IGNORED 增长时假收敛（已知限制） | UX 降级处理：报告 + OpenClaw 里的「已忽略 N 张」计数让用户感知；不强改 Agent 合约 |
| Unit 5 SKILL.md 插入位置错乱 | 参照既有 Lessons Learned 条目的时序规律（v5.3 之后、先 🟢 后 🔴🟡），lint 时肉眼 diff |
| snapshot 同步 reimbursement-helper 时覆盖 classify.py 改动 | Unit 5 明确登记——每次 sync 前对照 `scripts/core/__init__.py` 的 Modifications from source 清单 + SKILL.md Lessons Learned v5.4 条目 |

## Documentation / Operational Notes

- SKILL.md § Lessons Learned 新增一条（Unit 5）
- `scripts/core/__init__.py` 的 Modifications from source 清单新增一条（Unit 1/5）
- 不需要 CHANGELOG——项目没有正式版本分发流程
- 手工回归在 2026Q1 / Q2 季度烟雾测试里额外关注：
  - Termius 样例落成 `IGNORED_termius_*.pdf`
  - 下载报告最后一节有「已忽略的非报销票据 (N)」
  - 若新发现其他英文 SaaS 供应商也落入 IGNORED，本设计意图已达成；若仍有英文 SaaS 误判入 HOTEL_FOLIO / MEAL 等类，Unit 1 的水单收窄需要进一步扩展到 MEAL / RIDEHAILING 等路径

## Sources & References

- **Origin document:** `docs/brainstorms/2026-05-01-ignore-non-reimbursable-receipts-requirements.md`
- **Bug report:** `docs/solutions/2026-05-01-termius-saas-misclassified-as-hotel-folio.md`（2025Q4 discovery）
- **Related plan (aggregated-summary):** `docs/plans/2026-05-01-002-feat-aggregated-summary-output-plan.md`（`print_openclaw_summary` 的来源；Unit 4 依赖该函数已落地）
- **Related code:**
  - `scripts/core/classify.py:296-379`（classify_invoice）
  - `scripts/core/classify.py:142-162`（is_hotel_folio_by_doctype）
  - `scripts/postprocess.py:314-369`（rename_by_ocr）
  - `scripts/postprocess.py:408-484`（do_all_matching）
  - `scripts/postprocess.py:828-944`（print_openclaw_summary）
  - `scripts/postprocess.py:1262-1321`（zip_output）
  - `scripts/download-invoices.py:486-719`（write_report_v53）
  - `scripts/download-invoices.py:358-365, 398-404, 470-478`（download record shapes）
  - `scripts/invoice_helpers.py:613-781`（classify_email）
- **Project conventions:** `CLAUDE.md` § Editing norms（scripts/core/ 是 snapshot，本地修改必须登记到 `__init__.py` + SKILL.md Lessons Learned）
