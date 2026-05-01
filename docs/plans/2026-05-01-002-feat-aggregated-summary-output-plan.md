---
title: "feat: Aggregated summary output (CSV + MD + OpenClaw chat)"
type: feat
status: completed
date: 2026-05-01
origin: docs/brainstorms/2026-05-01-aggregated-summary-output-requirements.md
---

# feat: Aggregated summary output (CSV + MD + OpenClaw chat)

## Overview

把三处面向用户的输出 —— `发票汇总.csv`、`下载报告.md` 开头的金额摘要、OpenClaw 聊天回传的总结 —— 统一为**同一个 `aggregation` 数据结构的消费者**。新增纯函数 `build_aggregation`（在 `scripts/postprocess.py`）把 `do_all_matching` 的结果转换为一个带 `rows / subtotals / grand_total / low_conf / unmatched / voucher_count` 的 dict；CSV 改为类别优先排序并把成对的酒店/网约车合并成 1 行，追加小计+总计；MD 报告前置新表 `## 💰 金额汇总`；新增 `print_openclaw_summary` 输出一屏内可读的聊天总结。

硬约束：**所有算术走 Python exec（`Decimal` + `sum` + `quantize`）**，LLM 不参与。本特性只消费 OCR 结果，不新增 LLM 调用，不动 `scripts/core/matching.py` 与 `do_all_matching` 的判定逻辑。

## Problem Frame

当前：
- `write_summary_csv`（`scripts/postprocess.py:484-576`）按 `(date, CATEGORY_ORDER)` 一张 PDF 一行平铺，**无小计/总计**。酒店一次入住的发票+水单被当两行渲染。
- `write_report_v53`（`scripts/download-invoices.py:483-679`）`## 📊 摘要` 只列每类**数量**，用户看不到"每类花了多少、总共多少"。
- 整条 run 结束时，OpenClaw 聊天窗口只有 `say(...)` 的零散日志。

见 origin: `docs/brainstorms/2026-05-01-aggregated-summary-output-requirements.md` Problem Frame。

## Requirements Trace

（来自 origin 文档 R1-R17，按实现单元聚合）

- R1. `build_aggregation(matching_result, valid_records)` 新增纯函数；`voucher_count`、`MergedRow.confidence` 规则（含 P3 降级）见 origin R1。
- R2. 酒店配对合并 1 行：`category="HOTEL"`，`amount = invoice.transactionAmount`（不 fallback 水单 balance）。
- R3. 网约车配对合并 1 行：`category="RIDEHAILING"`，同 R2 金额规则。
- R4. 未配对酒店/网约车票据保留原类别标签独立成行。
- R5. 其它类别沿用现有标签。UNPARSED 行 `amount=None`。
- R6. 聚合层用 `decimal.Decimal`，`ROUND_HALF_UP`，2 位小数。`None` 不入求和但保留行。
- R7. CSV 排序改为 `(CATEGORY_ORDER, date)`（类别为主键）。
- R8. CSV 列从 8 列改 9 列：`序号/开票日期/类别/金额/销售方/备注/主文件/配对凭证/数据可信度`。配对凭证内联格式 `水单: xxx.pdf` / `行程单: xxx.pdf`。
- R9. 明细后空 1 行 + 每类小计 + 总计。`None` 不入小计。
- R10. 小计/总计序号列填 `—`；数据可信度列留空。
- R11. 求和走 Decimal + `quantize('0.01')`，格式 `f"{amt:.2f}"`。
- R12. MD 报告在 `## 📊 摘要` 前插入 `## 💰 金额汇总`，列 `类别 / 数量 / 小计 / 总计`。
- R13. 低置信度脚注条件 `low_conf.count > 0`，与 R16.7 统一。
- R14. 不重写已有 MD 报告结构。
- R15. `print_openclaw_summary(aggregation, *, output_dir, zip_path, csv_path, md_path, log_path, missing_status, date_range, writer=print)` 新增，写 stdout，≤20 行。
- R16a/R16b. 非空/空模板，见 origin。
- R17. 金额 `¥{Decimal:.2f}`；交付物路径 `os.path.abspath`。

**Success Criteria**（origin）：
- CSV 中一次酒店入住占 1 行，发票+水单两个文件名都在行内。
- MD 金额汇总表让用户 3 秒内得到每类+总额。
- OpenClaw 总结一屏判断"补搜 vs 报销"。
- CSV 总计 = MD 总计 = OpenClaw 合计（**同一个 `aggregation["grand_total"]`**）。
- `tests/test_postprocess.py` 覆盖 6 个场景（见 origin "测试覆盖"）。

## Scope Boundaries

- 不改 `missing.json` schema v1.0。
- 不改 Agent Loop Playbook / Exit Codes。
- 不改 `scripts/core/matching.py` 与 `do_all_matching` 判定逻辑。
- 不重写 `write_report_v53` 的 P1/P2/P3 配对表、未匹配项列表、补搜建议区。
- 不做"按月/按项目"二级分组。不改 CSV 为 xlsx 多 sheet。LLM 不参与任何算术。
- **SKILL.md 同步不在本计划内** —— plan 落地后另起一个 `/document-release` 流程同步（不作为 implementation unit）。

## Context & Research

### Relevant Code and Patterns

- `scripts/postprocess.py:58-84` — `CATEGORY_LABELS` / `CATEGORY_ORDER` 常量；无 `HOTEL` / `RIDEHAILING` 合并键。
- `scripts/postprocess.py:366-373` — `_to_float`：`None | "" | 不可转 → None`，否则 `float(val)`。调用端 `if x is None`，不用 `if not x`（保护 0.0）。
- `scripts/postprocess.py:376-395` — `_to_matching_input` 设置 `"_record": record`；合并行取 `matched[i]["invoice"]["_record"]`。
- `scripts/postprocess.py:398-474` — `do_all_matching` 输出形状。`matched` 条目含 `_record`；`meal/train/taxi/...` 走 `_raw()` 是**原始 record 字典**（非 `_to_matching_input` 形状）。**不对称必须在 aggregation 中处理。**
- `scripts/postprocess.py:440` — **P3 分支唯一处设 `"confidence": "low"`**；P1/P2 matched 条目无 `confidence` 键。`MergedRow.confidence` demote 规则的数据来源。
- `scripts/postprocess.py:481` — `CSV_COLUMNS` 常量。
- `scripts/postprocess.py:484-576` — `write_summary_csv` 现状：排序键 `(date, CATEGORY_ORDER)`、UTF-8 BOM（`:571`）、金额按类别取 `balance | totalAmount | transactionAmount`（`:511-520`）、confidence 派生自 OCR 侧 flags（`:549-558`）。
- `scripts/download-invoices.py:483-679` — `write_report_v53`。插入点：**第 513 行** `## 📊 摘要` 之前。
- `scripts/download-invoices.py:785-787` — `say()` 是 `main()` 闭包，同时写 stdout + `run.log`。`print_openclaw_summary` 新增 `writer` callable 参数，由 `main()` 传 `say` 进去（`say` 无法 import，refactor 出闭包风险过大，不在本 plan 范围）。
- `scripts/download-invoices.py:971-1015` — 主流程尾部：write_report_v53 → write_summary_csv → write_missing_json → zip_output → log.close() → sys.exit()。`print_openclaw_summary` 必须在 **`log.close()` 之前**调用（否则 `writer=say` 的 log 侧会写已关闭文件）。
- `scripts/postprocess.py:682-813` — `write_missing_json` 返回 `missing_payload`（`recommended_next_action ∈ {stop, run_supplemental, ask_user}`）。
- `scripts/download-invoices.py:720-771` — `--no-llm`：主管线 931-937 强制所有记录 `category=UNPARSED, ocr=None`；`do_all_matching` 路由到 `unparsed` 桶；`write_missing_json` 走 `user_action_required / ask_user` 分支（`postprocess.py:789`）。

**调用点清单（实现时需同步改）**：
- `write_summary_csv(...)` 共 **5 处**：`scripts/download-invoices.py:985`、`tests/test_postprocess.py:399, 408, 422, 513`。
- `write_report_v53(...)` 1 处：`scripts/download-invoices.py:973`。`test_agent_contract.py::TestMatchingTiersContract._report_for` 辅助同步更新。

**测试回归闸**：
- `tests/test_postprocess.py::TestHotelMatchingTiers`（712-814；v5.3 headline 回归）
- `tests/test_postprocess.py::TestSummaryCSV::test_unparsed_sorts_last`（CATEGORY_ORDER 重编号不破）
- `tests/test_postprocess.py::TestE2E::test_no_llm_full_pipeline`（`--no-llm` 全路径）
- `tests/test_agent_contract.py::TestMatchingTiersContract::test_p3_date_only_low_confidence_marker`（P3 ⚠️ 报告标记）
- `tests/test_agent_contract.py`：**无用例断言 stdout 内容**，`print_openclaw_summary` 是 greenfield。

### Institutional Learnings

`docs/solutions/` 无直接相关条目。CLAUDE.md 约束：
- **silent-fallback 反模式** —— amount 缺失标 `⚠️`，不替换 0。`None` 不入求和但保留行。
- **`scripts/core/` 是快照** —— 不改 `core/matching.py`。
- **`_to_float` 语义** —— `None` 与 `0.0` 必须可区分。
- **`missing.json` 枚举** —— 三值必须全分支覆盖。

## Key Technical Decisions

- **DEC-1 Decimal 转换内联**：在 `build_aggregation` 内部 2-3 个调用点直接写 `Decimal(str(f)) if (f := _to_float(val)) is not None else None`，**不新增 `_to_decimal` helper**。`_to_float` 是全项目唯一数值强转入口，不引入第二条并行 API。使用 `Decimal(str(float))` 而非 `Decimal(float)` 避免二进制残差。
- **DEC-2 `localcontext` 作用域限定**：`build_aggregation` 顶部用 `with localcontext() as ctx: ctx.rounding = ROUND_HALF_UP; ctx.prec = 28` 包住整个函数体，**不**修改全局 `getcontext()`。量化发生在 `subtotal.quantize(Decimal("0.01"))`；格式化 `f"{x:.2f}"` 在 quantize 之后是 no-op，不再受 rounding mode 影响 —— 保证三处 writer 的字符串表示一致。
- **DEC-3 `--no-llm` 走 R16a**：所有 valid 记录进 `unparsed` 桶、`voucher_count==0` 但 UNPARSED 行存在，按 origin R16b 规则走 R16a。`missing_status` 由 `write_missing_json` 路由为 `user_action_required/ask_user`（已验证 `postprocess.py:789` 分支命中）。
- **DEC-4 `write_summary_csv` 签名改为 `(path, aggregation)`**：删掉 `all_valid_records` 参数。5 处调用点同 PR 全改（见 "调用点清单"）。
- **DEC-5 `print_openclaw_summary` 接受 `writer` 参数**：`writer=print` 默认；`main()` 传 `say` 让总结同时进 `run.log`。必须在 `log.close()`（`download-invoices.py:1015`）之前调用。
- **DEC-6 `zip_output` 失败降级**：`zip_path = None` sentinel + `try: zip_path = zip_output(...)`。`print_openclaw_summary` 接受 `Optional[str] zip_path`；`None` 时第 11 步输出 `📦 报销包：未生成（打包失败，见 run.log 末尾）`。CSV/MD 行仍正常。
- **DEC-7 `confidence` 枚举显式白名单 + fail-fast**（修 adversarial P1 风险）：定义模块级常量 `VALID_CONFIDENCES = frozenset({"high", "low", "failed"})`；合并行的 `confidence` 计算用显式 `worst_of(a, b)` 函数，对不在白名单的值 `raise ValueError`，**而非静默降 "high"**。这样若 `matching.py` 将来新增 `"medium"` 等值，测试会立即红。
- **MergedRow 字段**（dataclass；`Aggregation` 则保持 plain dict 与 `do_all_matching` 返回风格一致）：`category / date / amount / vendor / primary_file / paired_file / paired_kind / confidence / remark_flags`。
  - `vendor`: 合并行取发票侧 `ocr.vendorName → record.merchant → "—"`，沿用 `write_summary_csv:565`。不读 folio.hotelName（保持"金额/日期/销售方全取发票"通则；Deferred to Implementation 中给 P3 脏 OCR 的 case 留一条 follow-up test）。
  - `paired_file`: `os.path.basename`，与现有 CSV 风格一致。
- **CATEGORY_ORDER 重编号**：新增 `HOTEL=0`、`RIDEHAILING=3`。`MEAL=3` 冲突，整体后移：`HOTEL:0, HOTEL_FOLIO:1, HOTEL_INVOICE:2, RIDEHAILING:3, RIDEHAILING_INVOICE:4, RIDEHAILING_RECEIPT:5, MEAL:6, TRAIN:7, TAXI:8, MOBILE:9, TOLLS:10, UNKNOWN:11, UNPARSED:99`。相对顺序不变。研究确认测试无人断言整数值。
- **小计/总计行序号列填 `"—"`**：tombstone，Excel 不误判数据；类别列 `"{CATEGORY_LABELS[cat]} 小计"` 自描述。

## Open Questions

### Resolved During Planning

- Decimal 精度污染全局？→ DEC-2 `localcontext`。
- `CATEGORY_ORDER` 重编号破测试？→ 研究确认无断言整数值；安全。
- stdout 总结如何走 `run.log`？→ DEC-5 `writer=say`。
- `write_summary_csv` 有多少调用方？→ 5 处，清单已列。
- zip 失败时总结如何处理？→ DEC-6 降级。
- `confidence` 未知枚举静默降级？→ DEC-7 fail-fast。

### Deferred to Implementation

- `TestAggregation` fixture：能复用 `TestHotelMatchingTiers::_make_invoice/_make_folio` 则复用，否则新建。
- MD pipe 表格在 iOS Files.app / OpenClaw 渲染器里的对齐 —— 人工渲染检查，不在 plan 里预设 fallback。
- P3 脏 OCR case 的 vendor 兜底（`—` vs `folio.hotelName`）：Unit 2 test 覆盖 `—` 情形；若用户反馈需要 hotelName 再另起一个 follow-up。
- `build_aggregation` 是否需要二次遍历 `valid_records`：**不需要** —— `do_all_matching` 已把 `valid=True but ocr=None` 路由到 `matching_result["unparsed"]`。plan 仅遍历 `matching_result` 各桶；`valid_records` 参数保留做 count 断言（断言 `len(all rows) == len(valid_records)`，防漏记录）。

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification.*

**数据流**：

```
do_all_matching → matching_result
                    │
                    ▼
   build_aggregation(matching_result, valid_records)
                    │
                    │ aggregation (plain dict)
                    ▼
   ┌────────────────┼─────────────────┐
   ▼                ▼                 ▼
write_summary_csv  write_report_v53  print_openclaw_summary
(rows 按类别+日期    (前置 💰          (R16a / R16b 模板，
 排序；追加小计+     金额汇总表)        writer=say)
 总计)
```

**MergedRow 合并规则（sketch）**：

```
for pair in matching_result["hotel"]["matched"]:
  inv_rec = pair["invoice"]["_record"]
  fol_rec = pair["folio"]["_record"]
  amount = (lambda f: Decimal(str(f)) if f is not None else None)(
              _to_float(pair["invoice"].get("transactionAmount")))  # 不 fallback 水单
  row = MergedRow(
    category = "HOTEL",
    date = pair["folio"].get("checkOutDate") or pair["folio"].get("departureDate"),
    amount = amount,
    vendor = inv_rec["ocr"].get("vendorName") or inv_rec.get("merchant") or "—",
    primary_file = os.path.basename(inv_rec["path"]),
    paired_file = os.path.basename(fol_rec["path"]),
    paired_kind = "水单",
    confidence = worst_of(                            # DEC-7 fail-fast
      invoice_ocr_confidence(inv_rec),                # 现有 write_summary_csv:549-558
      "low" if pair.get("confidence") == "low" else "high",  # P3 demote
    ),
    remark_flags = collect_flags(inv_rec["ocr"]) + (
      ["⚠️发票金额缺失"] if amount is None else []
    ),
  )
```

## Implementation Units

- [x] **Unit 1: 聚合核心 — `build_aggregation` + `MergedRow` + 类别注册**

**Goal**: 一口气做完聚合层的所有基础：`MergedRow` dataclass、`VALID_CONFIDENCES` + `worst_of`、`CATEGORY_LABELS/ORDER` 新增键、`build_aggregation` 主函数。CSV/MD/消息三个 writer 的单一数据源。

**Requirements**: R1, R2, R3, R4, R5, R6。

**Dependencies**: 无。

**Files**:
- Modify: `scripts/postprocess.py`（顶部 import `from decimal import Decimal, ROUND_HALF_UP, localcontext`；常量表扩展；在 `do_all_matching` 之后、`write_summary_csv` 之前新增 `MergedRow` dataclass + `worst_of` + `build_aggregation`）
- Test: `tests/test_postprocess.py`（新增 `TestBuildAggregation` 类，含 `TestCategoryConstants` 小组）

**Execution note**: Test-first —— 先写覆盖 origin Success Criteria 六项（a-f）的失败断言，再写实现。

**Approach**:
- 常量扩展：`CATEGORY_LABELS["HOTEL"]="酒店"`、`CATEGORY_LABELS["RIDEHAILING"]="网约车"`；`CATEGORY_ORDER` 按 DEC-8 整体重编号。
- `VALID_CONFIDENCES = frozenset({"high", "low", "failed"})`（模块级常量）。
- `worst_of(*values: str) -> str`：遍历 values，不在 `VALID_CONFIDENCES` raise ValueError；返回最劣（`failed` > `low` > `high`）。
- `@dataclass class MergedRow`（字段见 Key Decisions "MergedRow 字段"）。
- `build_aggregation(matching_result, valid_records)` 返回 `dict`：`{rows, subtotals, grand_total, low_conf: {count, amount}, unmatched: {hotel_invoices, hotel_folios, rh_invoices, rh_receipts}, voucher_count}`。
  - 顶部 `with localcontext() as ctx: ctx.rounding = ROUND_HALF_UP; ctx.prec = 28`。
  - Decimal 转换内联（DEC-1）。
  - 遍历顺序：`hotel.matched → ridehailing.matched → unmatched_{invoices,folios,receipts} → meal/train/taxi/mobile/tolls/unknown → unparsed`。注意 `meal/...` 桶是原始 record 字典。
  - Rows 排序 `(CATEGORY_ORDER[category], date or "99999999")`。
  - `subtotals[cat] = sum(...).quantize(Decimal("0.01"))`；`None` 跳过。
  - `grand_total = sum(subtotals.values()).quantize(Decimal("0.01"))`（空时 `Decimal("0.00")`）。
  - `low_conf.count / .amount`：含 P3 demote 后的 low 行。
  - `voucher_count = len([r for r in rows if r.category != "UNPARSED"])`。
  - **完整性断言**：`assert len(rows) == sum(全桶总数, including unparsed)`；防止遗漏 record。
- confidence 派生：抽 `_confidence_for_invoice_record(rec)`，复用 `write_summary_csv:549-558` 现有逻辑。

**Patterns to follow**:
- `do_all_matching`（`scripts/postprocess.py:398-474`）结构风格。
- `_to_matching_input` 里 `"_record"` 读取模式。
- `_to_float` 的 None-safe 语义（镜像但不复制）。

**Test scenarios**:
- Happy path (a): 酒店 P1 配对 → 1 行，`category=="HOTEL"`，`primary_file=发票 basename`，`paired_file=水单 basename`，`paired_kind=="水单"`。
- Happy path (b): 网约车配对 → 1 行，`category=="RIDEHAILING"`，`paired_kind=="行程单"`。
- Happy path (c): 酒店仅发票（无水单）→ 1 行 `category=="HOTEL_INVOICE"`，`paired_file is None`。
- Happy path (d): `None` amount 行保留在 rows，**不入** `subtotals["MEAL"]`，但 `voucher_count` 照数。
- Happy path (e): low_conf 行（OCR `_amountConfidence=="low"`）→ `aggregation["low_conf"]["count"]==1`，`amount` 含该行金额。
- Happy path (f): `Decimal("0.1") + Decimal("0.2") + Decimal("0.3") == Decimal("0.6")`；不出现 `0.6000000000000001`。
- Edge case: `--no-llm` 全 UNPARSED → `voucher_count==0`，`grand_total==Decimal("0.00")`，`rows` 全保留。
- Edge case: P3 兜底（`matched[i]["confidence"]=="low"`）→ 即使发票 OCR 全 high，合并行 `confidence=="low"`；`low_conf.count` 计入。
- Edge case: Gmail 完全空（`matching_result` 各桶全空）→ rows=[]、subtotals={}、grand_total=0.00、unmatched 全 0。
- Edge case: 部分 OCR 失败（5 个 valid、1 个 `ocr=None`）→ 恰好 5 行 MergedRow，1 行 UNPARSED，`voucher_count==4`；`len(rows)==len(valid_records)` 断言通过。
- Edge case: CATEGORY_ORDER 重编号后 `HOTEL<HOTEL_FOLIO<HOTEL_INVOICE<RIDEHAILING<RIDEHAILING_INVOICE<...<UNPARSED(99)`。
- Edge case: `worst_of("high", "low") == "low"`；`worst_of("low", "failed") == "failed"`；`worst_of("medium", "high")` **raises ValueError**（DEC-7 fail-fast）。
- Edge case: `localcontext` 退出后 `getcontext().rounding == ROUND_HALF_EVEN`（全局未污染）。
- Integration: aggregation dict 是单一实例；CSV/MD/消息三处读到的 `grand_total` 是同一 Decimal 对象。
- Error path: `matching_result` 结构残缺（`"hotel"` 键缺失）→ KeyError（不防御性默认值）。

**Verification**:
- `python3 -m pytest tests/test_postprocess.py::TestBuildAggregation tests/test_postprocess.py::TestCategoryConstants -v` 全绿。
- `python3 -m pytest tests/test_postprocess.py::TestHotelMatchingTiers tests/test_postprocess.py::TestSummaryCSV::test_unparsed_sorts_last -v` **仍全绿**（回归闸）。

---

- [x] **Unit 2: 改写 `write_summary_csv` + `write_report_v53` 消费 `aggregation`**

**Goal**: 两个现有 writer 同步切到 aggregation 输入。CSV 变 9 列 + 类别优先排序 + 小计+总计；MD 报告在 `## 📊 摘要` 前追加 `## 💰 金额汇总` 表。

**Requirements**: R7, R8, R9, R10, R11, R12, R13, R14。

**Dependencies**: Unit 1。

**Files**:
- Modify: `scripts/postprocess.py`（`CSV_COLUMNS` 常量 `:481`；`write_summary_csv` `:484-576` 函数签名与实现）
- Modify: `scripts/download-invoices.py`（`write_report_v53` `:483-679` 在 `:513` `## 📊 摘要` 之前插入 `## 💰 金额汇总` 块；函数签名追加 `aggregation` kwarg）
- Test: `tests/test_postprocess.py`（扩展 `TestSummaryCSV`）
- Test: `tests/test_agent_contract.py`（更新 `_report_for` 辅助；新增 `test_finance_summary_table_contract`）

**Approach**:

CSV 侧：
- `CSV_COLUMNS = ["序号", "开票日期", "类别", "金额", "销售方", "备注", "主文件", "配对凭证", "数据可信度"]`（9 列）。
- `def write_summary_csv(path: str, aggregation: dict) -> int`：
  1. UTF-8 BOM 开文件（现有 `:571`）。
  2. 写表头。
  3. 遍历 `aggregation["rows"]` 写 9 列；`金额` `f"{row.amount:.2f}"` if not None else `""`；`配对凭证` `f"{row.paired_kind}: {row.paired_file}"` if paired else `""`；`备注` `"; ".join(row.remark_flags)`。
  4. 明细后空行（`[""]*9`）。
  5. 按 CATEGORY_ORDER 遍历 `aggregation["subtotals"]`：每类一行 `["—", "", f"{CATEGORY_LABELS[cat]} 小计", f"{subtotal:.2f}", "", "", "", "", ""]`。
  6. 末行 `["—", "", "总计", f"{aggregation['grand_total']:.2f}", "", "", "", "", ""]`。
  7. `return len(aggregation["rows"])`。
- 5 处调用点全改（生产 1 + 测试 4）。

MD 报告侧：
- `write_report_v53(..., aggregation)` —— 追加 kwarg，保留现有 P1/P2/P3 表格、未匹配项、补搜建议、UNPARSED 区所有逻辑（R14 只追加不重写）。
- 在 `lines.append("## 📊 摘要\n")` 之前插入：
  ```
  ## 💰 金额汇总
  | 类别 | 数量 | 小计 |
  |------|------|------|
  | {CATEGORY_LABELS[cat]} | {count} | ¥{subtotal:.2f} |
  ... (仅 subtotals 中的类别，按 CATEGORY_ORDER 迭代)
  | **总计** | {voucher_count} | **¥{grand_total:.2f}** |
  ```
- 若 `aggregation["low_conf"]["count"] > 0`，表下追加一行脚注：`† 其中 {count} 项金额存疑（可信度=low，合计 ¥{amount:.2f}），见末尾「⚠️ 需人工核查」区`。条件与 R16.7 严格相同。

**Patterns to follow**:
- UTF-8 BOM、`f"{x:.2f}"` 金额风格（现有 `write_summary_csv`）。
- `lines.append(...)` + `"\n".join(lines)` 风格（现有 `write_report_v53`）。
- 数量摘要 pipe 表格式（`:514-515`）。

**Test scenarios**:

CSV:
- Happy path: fixture 3 条（酒店配对 + 网约车配对 + 餐饮）→ 3 明细 + 1 空行 + 3 小计 + 1 总计 = 8 数据行（表头外）。
- Happy path: 酒店合并行 `配对凭证` == `"水单: folio_20260102_国金丽思.pdf"`。
- Happy path: UNPARSED 行 `金额` 为空，`数据可信度=="failed"`，不入小计。
- Edge case: 只有 1 类 → 1 小计行 + 1 总计行。
- Edge case: `rows==[]` → 写表头 + 空行 + 总计 0.00；`return 0`。
- Edge case: `Decimal("1.235").quantize(...)` == `Decimal("1.24")`（ROUND_HALF_UP）。
- Integration: BOM 字节 `\xef\xbb\xbf` 在文件头。
- Integration: UNPARSED 行排最后（沿用 `test_unparsed_sorts_last` 断言风格，但按新排序 key 调整）。

MD:
- Happy path: fixture → 报告含 `## 💰 金额汇总` 标题、`| 酒店 | 1 | ¥1280.00 |` 行、`| **总计** | 3 | **¥1350.00** |` 行。
- Happy path: P1/P2/P3 配对表、UNPARSED 区仍原样（`TestMatchingTiersContract` 全绿）。
- Edge case: `low_conf.count==0` → 无脚注；`>0` → 脚注含金额。
- Edge case: `rows==[]` → 只有表头 + 总计 `¥0.00` 行。
- Integration: MD 总计字符串 == CSV 总计字符串（同源 `aggregation["grand_total"]`）。
- Integration: `test_p3_date_only_low_confidence_marker` 继续绿（新表不取代 P3 报告 ⚠️ 标记）。

**Verification**:
- `python3 -m pytest tests/test_postprocess.py::TestSummaryCSV tests/test_agent_contract.py -v` 全绿。

---

- [x] **Unit 3: `print_openclaw_summary` + 主管线接线**

**Goal**: OpenClaw 聊天总结；非空/空两模板；主管线顺序调整（zip 失败降级 + log.close 之前调用）。

**Requirements**: R15, R16a, R16b, R17。

**Dependencies**: Unit 1, Unit 2。

**Files**:
- Modify: `scripts/postprocess.py`（顶部 `import shlex`；新增 `print_openclaw_summary`）
- Modify: `scripts/download-invoices.py`（import 新函数；`build_aggregation` 先构造；改 `write_summary_csv` 和 `write_report_v53` 调用；zip 降级；`print_openclaw_summary` 在 `log.close()` 之前）
- Test: `tests/test_postprocess.py`（新增 `TestPrintOpenClawSummary`）

**Approach**:
- `def print_openclaw_summary(aggregation, *, output_dir, zip_path, csv_path, md_path, log_path, missing_status, date_range, writer=print)`。
  - 空模板（R16b）条件：`voucher_count==0 AND all(unmatched.* == 0) AND 无失败下载`。3 行。
  - 非空模板（R16a）按 origin 11 步渲染。路径走 `os.path.abspath(...)`。
  - `missing_status` 分支：`stop / run_supplemental / ask_user`，非枚举值 raise ValueError（DEC-7 风格显式失败）。
  - `run_supplemental` 的命令行用 `shlex.quote(output_dir)`（需 `import shlex`）。
  - `zip_path is None` 时第 11 步输出 `📦 报销包：未生成（打包失败，见 run.log 末尾）` 并仍列 csv/md。

主管线接线（`scripts/download-invoices.py` 尾部严格顺序）：
1. `aggregation = build_aggregation(matching_result, [d for d in downloaded_all if d.get("valid")])`
2. `write_report_v53(..., aggregation=aggregation)`
3. `n_csv = write_summary_csv(csv_path, aggregation)`
4. `missing_payload = write_missing_json(...)`
5. `zip_path = None`；`try: zip_path = zip_output(...) except RuntimeError: ...`
6. `print_openclaw_summary(aggregation, output_dir=out_dir, zip_path=zip_path, csv_path=csv_path, md_path=report_path, log_path=log_path, missing_status=missing_payload["recommended_next_action"], date_range=(start_date, end_date), writer=say)` —— **log 仍打开**
7. `log.close()`（现有 `:1015`）
8. `sys.exit(...)`

**Patterns to follow**:
- `say()` 双写（`:785-787`）。
- 现有 emoji 用法（`:513, 531, 577, 608`）。

**Test scenarios** （**内容断言，不钉 writer 调用次数**）:
- Happy path: 非空 aggregation 3 类 → 输出含 `"📄 发票报销包"`、`"✅ 共 3 份凭证"`、`"¥" + grand_total`。
- Happy path: `missing_status=="stop"` → 输出含 `"可以提交报销"`。
- Happy path: `missing_status=="run_supplemental"` → 含 `"--supplemental"` + `shlex.quote(output_dir)`。
- Happy path: `missing_status=="ask_user"` → 含 `"需人工核查"` 引用 md_path。
- Edge case: 部分 OCR 失败（5 valid / 1 ocr=None）→ 非空模板，类别行有 `⚠️ 需人工核查 1 份`，`voucher_count==4`。
- Edge case: zip 失败降级（`zip_path=None`）→ 输出含 `"📦 报销包：未生成（打包失败"`；csv/md 行仍渲染。
- Edge case: 空模板（全部 0）→ 仅 3 行；不含 `"✅"`、`"📦"`。
- Edge case: `--no-llm` UNPARSED-only run → 非空模板，类别行 `"⚠️ 需人工核查 N 份"`；下一步 `ask_user`。
- Edge case: `low_conf.count > 0` → 合计行末尾 `" †"`；脚注含 `low_conf.amount`；**条件与 MD R13 脚注同步**（同一 aggregation 字段判定）。
- Edge case: 所有 4 类 unmatched > 0 → 4 行 `⚠️` 未配对提醒都出现；N==0 的不出现。
- Integration: `writer=print` 时 `capsys.readouterr().out` 捕获预期行；`writer=lambda lines.append(x)` 时可断言每行精确字符串（测试传 lambda 是工具，不是接口要求）。
- Integration: 所有路径 `os.path.isabs(extracted_path)` 为真。
- Integration: CSV 总计 == MD 总计 == stdout 合计（三个字符串严格相等；`TestAggregationConsistency` 专项）。
- Error path: `missing_status` 不在枚举 → ValueError（显式失败）。

**Verification**:
- `python3 -m pytest tests/test_postprocess.py::TestPrintOpenClawSummary -v` 全绿。
- 完整 offline suite 全绿：`python3 -m pytest tests/ -q`。
- 人工一轮小范围 `--no-llm` 验空模板；一轮真实运行核对"三处数字永远相等"。

## System-Wide Impact

- **Interaction graph**: `download-invoices.py` 主流程新增构造 + 1 个 stdout 调用；`write_summary_csv` / `write_report_v53` 签名扩展；`scripts/core/matching.py` / `do_all_matching` 判定逻辑零改动（硬约束）。
- **Error propagation**:
  - `build_aggregation` 残缺 `matching_result` → KeyError。
  - `worst_of` / `print_openclaw_summary` 对未知枚举 → ValueError（DEC-7 显式失败，对齐 CLAUDE.md "silent-fallback 反模式"）。
  - Decimal 内联转换 None-safe：求和前必须 `if x is not None`。
- **State lifecycle**:
  - aggregation 单一构造；三个 writer 只读。
  - `print_openclaw_summary` 必须在 `log.close()` 之前（DEC-5）；`missing.json` 必须先落盘。
- **API surface**:
  - `write_summary_csv` 签名变；5 处调用点已列。
  - `write_report_v53` 追加 kwarg；`test_agent_contract.py::TestMatchingTiersContract._report_for` 同步。
- **Integration coverage**: "三处数字永远相等"不变量 —— 需要一条 integration test 同时断言 CSV/MD/stdout 总计字符串严格相等（新增 `TestAggregationConsistency`，置于 `tests/test_postprocess.py`）。
- **Unchanged invariants**: `scripts/core/matching.py`、`missing.json` schema v1.0、exit codes 表、`stderr REMEDIATION:` 契约、UNPARSED=99。

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `CATEGORY_ORDER` 重编号破隐式 sort | `test_unparsed_sorts_last` 作回归闸；全 suite 运行。 |
| `write_summary_csv` 签名改动破 5 处调用点 | DEC-4 清单同 PR 全改；`grep -n "write_summary_csv(" tests/ scripts/` 对齐。 |
| Decimal 引入后 float/Decimal 双轨混乱 | DEC-1 内联转换、`_to_float` 仍是唯一数值强转入口；Decimal 只活在 `build_aggregation` 内部。 |
| `localcontext` 污染全局 | `getcontext().rounding == ROUND_HALF_EVEN` 出入断言。 |
| P3 confidence demote 规则误伤 | DEC-7 `worst_of` + `VALID_CONFIDENCES` 白名单 fail-fast；`TestHotelMatchingTiers` 全绿。若 matching.py 未来加 `"medium"`，测试立即红、不静默降级。 |
| `say` 闭包不可导入 | DEC-5 `writer` 参数；main 显式传 `say`。 |
| `zip_output` 失败吞掉总结 | DEC-6 sentinel + 降级；`print_openclaw_summary` 一定执行。 |
| `--no-llm` `voucher_count==0` 边界 | DEC-3 明确走 R16a；Unit 1 fixture + `TestE2E.test_no_llm_full_pipeline` 回归。 |
| 三处数字字符串不一致 | 同源 `aggregation["grand_total"]`；quantize 后 `:.2f` no-op；`TestAggregationConsistency` 专项断言。 |

## Documentation / Operational Notes

- Plan 落地后另起 `/document-release` 同步 SKILL.md（"交付物"节 + stdout 总结说明）。**不在本 plan 单元内**。
- `references/seasonal-smoke.md` 下次烟雾测试可手工验"三处数字永远相等"作为 checklist 项。
- 不涉及 Gmail OAuth / LLM provider / migrations / 外部 API / rollout。

## Sources & References

- **Origin**: [docs/brainstorms/2026-05-01-aggregated-summary-output-requirements.md](docs/brainstorms/2026-05-01-aggregated-summary-output-requirements.md)
- **Code**:
  - `scripts/postprocess.py:58-84`（CATEGORY constants）
  - `scripts/postprocess.py:366-395`（`_to_float`, `_to_matching_input`）
  - `scripts/postprocess.py:398-474`（`do_all_matching` 输出）
  - `scripts/postprocess.py:440`（P3 `confidence="low"` 唯一 set 点）
  - `scripts/postprocess.py:481-576`（`CSV_COLUMNS`, `write_summary_csv`）
  - `scripts/postprocess.py:549-558`（confidence 派生逻辑，抽出复用）
  - `scripts/postprocess.py:682-813`（`write_missing_json` 枚举 + `:789` --no-llm 分支）
  - `scripts/download-invoices.py:483-679`（`write_report_v53`；`:513` 插入点）
  - `scripts/download-invoices.py:785-787`（`say()`）
  - `scripts/download-invoices.py:971-1015`（主管线尾部；`:1015` `log.close()`）
  - `scripts/core/matching.py:268-354`（`match_hotel_pairs`；零改动）
- **Tests**:
  - `tests/test_postprocess.py::TestSummaryCSV`（388-427）
  - `tests/test_postprocess.py::TestHotelMatchingTiers`（712-814；回归闸）
  - `tests/test_postprocess.py::TestE2E::test_no_llm_full_pipeline`（488-544）
  - `tests/test_agent_contract.py::TestMatchingTiersContract`（338-432；P3 ⚠️）
  - `tests/test_agent_contract.py::TestMissingJsonSchemaContract`（591-613；枚举）
- **CLAUDE.md** 约束：silent-fallback 反模式、`scripts/core/` 快照、`_to_float` None 语义、`missing.json` 枚举契约。
