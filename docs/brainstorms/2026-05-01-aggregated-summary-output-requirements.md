---
date: 2026-05-01
topic: aggregated-summary-output
---

# 按类别汇总的 CSV / MD / OpenClaw 总结输出

## Problem Frame

现在 `发票汇总.csv` 仅按 `(日期, 类别)` 平铺明细，每张 PDF 一行；`下载报告.md` 只给每类**数量**而无金额合计；运行结束时 OpenClaw 回到聊天窗口只有零散的 `say(...)` 日志，没有给用户的结构化总结。

对财务报销这个核心场景，这三点都不够：
1. 一次酒店入住产生 **2 张票据（发票 + 水单）**，金额一致，平铺时会在 Excel 里肉眼去重，手工容易错。
2. 用户/老板要的第一个数字永远是**每类花了多少 + 总共多少**；现在得手工在 Excel 里透视。
3. OpenClaw 跑完后用户需要一屏读完"这一趟下来了啥、金额多少、有没有缺票、交付物在哪"，而不是从几百行日志里倒找。

硬约束：**所有数值计算必须是 Python exec（`sum` / `round`）**，不走 LLM — LLM 容易在金额/百分比上输出看似合理的错数。LLM 只做已完成的 OCR 字段抽取（`scripts/core/llm_ocr.py`）；本特性只消费 OCR 结果，不新增 LLM 调用。

## User Flow

```
┌───────────────────────────────┐
│ download-invoices.py 主循环   │
│ Step 8 do_all_matching 完成   │  ← 已有
└───────────────┬───────────────┘
                │ matching_result（含 hotel.matched / ridehailing.matched / meal / 各单票据桶 / unparsed）
                ▼
┌───────────────────────────────┐
│ build_aggregation(records)    │  ← 新增 (postprocess.py 中的纯函数)
│  • 生成 merged_rows[]         │
│  • 每行 {category, date,       │
│    amount, vendor, primary_    │
│    file, paired_file, paired_  │
│    kind, confidence, flags}    │
│  • 一对酒店/网约车 → 1 行      │
│  • 未配对单票据 → 1 行         │
│  • 用 decimal.Decimal 算       │
│    per_category_subtotal[]     │
│    grand_total                 │
│    low_conf_count/amount       │
└───────────────┬───────────────┘
                │ aggregation
    ┌───────────┼───────────────────────────────┐
    ▼           ▼                               ▼
write_summary_csv     write_report_v53    print_openclaw_summary
(改签名：接受 aggregation)  (在摘要前置      (新函数，stdout
                         「按类别汇总」表)   紧凑模板；run 末尾调用)
```

**模块归属约定**：`write_report_v53` 继续留在 `scripts/download-invoices.py`（已被 `test_agent_contract.py` 通过 `cli_module.write_report_v53` 钉住导入路径，禁止移动）。`build_aggregation` 与 `print_openclaw_summary` 新增到 `scripts/postprocess.py`，由 `download-invoices.py` 以 `from postprocess import build_aggregation, print_openclaw_summary` 导入，延续现有 `do_all_matching` 的导入模式，避免循环依赖。

## Requirements

**汇总数据层 (build_aggregation)**

- R1. 新增纯函数 `build_aggregation(matching_result, valid_records)`，返回 `{rows: List[MergedRow], subtotals: Dict[cat, Decimal], grand_total: Decimal, low_conf: {count, amount}, unmatched: {hotel_invoices: int, hotel_folios: int, rh_invoices: int, rh_receipts: int}, voucher_count: int, empty_reason: Optional[str]}`。签名稳定，CSV/MD/消息三个 writer 都消费这个结构，保证三处数字永远一致。
  - **行计数语义**：`len(rows)` 是 CSV 明细的行数（包含 UNPARSED 行，用于在 CSV 底部保留未解析凭证让人工核查）。`voucher_count = len([r for r in rows if r.category != "UNPARSED"])` 是 OpenClaw 消息 `共 N 份凭证` 的 `N`（不含 UNPARSED，因为 UNPARSED 不是"凭证"）。CSV/MD 汇总表中 UNPARSED 同样不计入 grand_total（因其 amount=None，R6 已定）。
  - **MergedRow.confidence 规则**：配对合并行的 `confidence` 取以下任一条件的最劣值：
    1. 发票侧 OCR 字段中任一为 low/invalid（`_amountConfidence=="low"` / `_dateConfidence=="low"` / `_vendorNameInvalid`）；
    2. **配对本身的 match_type**：`do_all_matching` 返回的 `matched[i]` 中若有 `confidence=="low"` 字段（= P3 date-only 兜底），合并行也必须 demote 到 low。这保证 P3 兜底匹配不会被 OCR-only 的 confidence 视角吞掉。
    - 水单 OCR 的 low 标记**不**影响合并行的 confidence（水单主要作为附件存在，R2/R3 已决定金额/日期/销售方均取发票侧；这条是对水单侧 OCR 噪音的有意忽略，不是对配对质量的忽略）。
- R2. 酒店 **P1/P2/P3 匹配上**的 `(invoice, folio)` 合并为 **1 行**。`category="HOTEL"`（合并后），`date = folio.checkOutDate ∥ folio.departureDate`。
  - **金额规则**：`amount = invoice.transactionAmount`（这是 LLM 抽取的含税总额，由 prompt 规则 `transactionAmountNoVAT + VAT = transactionAmount` 保证）。**不使用** `folio.balance` 作为 fallback —— 水单的 balance 可能含发票外明细（小费、城市税、私人消费），不是报销金额。
    - **边缘场景说明**：P2 配对本身以 `is_amount_match(invoice, folio)` 为 gate，invoice amount 是 None 时根本进不了 P2；所以"配对成功但发票 amount 缺失"只可能发生在 P1（remark 匹配，不查金额）或 P3（日期-only，confidence 已标 low）。P1 下 OCR 能抽到 remark 却漏掉 amount 是罕见偏执情形；出现时 `amount=None` 进入 R6 的 "None 不入小计但保留明细" 路径，CSV 备注标 `⚠️发票金额缺失`，人工打开 zip 中的水单核对后手工回填。不做隐式 fallback 换取罕见情形的"正确"显示。
  - **类别注册**：新增合并类别 `HOTEL` 必须在 `scripts/postprocess.py` 的 `CATEGORY_LABELS` 中注册（`"HOTEL": "酒店"`），并在 `CATEGORY_ORDER` 中赋予排序值 `0`（排在现有 `HOTEL_FOLIO=1` / `HOTEL_INVOICE=2` 之前），否则下游 `CATEGORY_LABELS.get("HOTEL", "发票")` 会回退到"发票"，`CATEGORY_ORDER.get("HOTEL", 50)` 会把合并行排到中段。
- R3. 网约车匹配上的 `(invoice, receipt)` 合并为 **1 行**；`category="RIDEHAILING"`。
  - **金额规则**：与 R2 同样通则 —— `amount = invoice.transactionAmount`（发票内的含税总额）。**不** fallback 到 `receipt.totalAmount`。`invoice.transactionAmount` 为 `None` 时 `amount=None`、CSV 备注标 `⚠️发票金额缺失`，进入 R6 的 None-safe 路径。发票是唯一报销数据源。
  - **类别注册**：新增合并类别 `RIDEHAILING` 必须注册 `CATEGORY_LABELS["RIDEHAILING"]="网约车"` 与 `CATEGORY_ORDER["RIDEHAILING"]=3`（排在 `RIDEHAILING_INVOICE=4` / `RIDEHAILING_RECEIPT=5` 之前）。
- R4. 未配对的酒店发票、酒店水单、网约车发票、网约车行程单各自独立成行，保留原类别标签（`HOTEL_INVOICE` / `HOTEL_FOLIO` / `RIDEHAILING_INVOICE` / `RIDEHAILING_RECEIPT`），便于 finance 一眼看到"孤儿票据"。
- R5. 其它类别（餐饮/火车/出租/话费/通行费/其它）一张票据一行，沿用现有类别标签。UNPARSED 行 `amount` 为 `None`。
- R6. 所有金额字段在聚合层用 `decimal.Decimal`（`ROUND_HALF_UP`，2 位小数），避免浮点漂移；输出层再转成字符串。`None` 金额不参与求和，但行本身保留在明细中。

**CSV 输出 (write_summary_csv)**

- R7. 排序规则：**类别按 `CATEGORY_ORDER` 常量升序**（酒店 → 网约车 → 餐饮 → 火车 → 出租 → 话费 → 通行费 → 其它 → UNPARSED 垫底）；组内按 `date` 升序。
- R8. 表头改为 `序号 / 开票日期 / 类别 / 金额 / 销售方 / 备注 / 主文件 / 配对凭证 / 数据可信度`（**9 列，是当前 `CSV_COLUMNS`（8 列，`postprocess.py:481`）的修改版** —— 将原 `文件名` 拆为 `主文件` + `配对凭证`；实现时需同步更新 `CSV_COLUMNS` 常量）。
  - **主文件 vs 配对凭证 的分配规则**：匹配上的 HOTEL 行 `主文件 = invoice PDF basename`（增值税发票是可抵扣、报销系统要的主要凭证）、`配对凭证 = folio PDF basename`；匹配上的 RIDEHAILING 行 `主文件 = invoice PDF basename`、`配对凭证 = receipt/trip PDF basename`。未配对的单票据：`主文件 = 该票据 basename`，`配对凭证` 留空。
  - **配对凭证单元格内联格式**：酒店行填 `水单: {folio_basename}`，网约车行填 `行程单: {trip_basename}`，未配对时留空。一眼能在一个单元格里读完"这是什么类型的附件 + 文件名"，不用扫到备注列。
  - **备注列不再承载"配对类型"信息**：备注沿用当前 `write_summary_csv` 的语义（`confNo=...` / `phone=...` / `period=...` / `trips=...` / `⚠️金额可疑` / `⚠️日期异常` / `⚠️销售方未识别` / 新增 `⚠️发票金额缺失`），保持作为"每行告警与元数据"的专职列。
- R9. 明细区结束后空 1 行，然后按 `CATEGORY_ORDER` 输出 `{CATEGORY_LABEL} 小计` 行 × N（只输出有金额行的类别），每行只填"类别"与"金额"两列；最后 1 行 `总计`。`None` 金额不入小计。
- R10. 小计/总计**不生成序号**；`数据可信度` 列为空。保持 UTF-8 BOM、Excel 直开。
- R11. 所有求和使用 `decimal.Decimal`，`str(quantize('0.01'))`，不使用 `format(x, ',.2f')` 的千分位（沿用现有 `f"{amt:.2f}"` 风格），避免 Excel 对千分位字符串的误判。

**MD 报告 (write_report_v53) 调整**

- R12. 在现有 `## 📊 摘要` 表前方插入新表 `## 💰 金额汇总`，列：`类别 / 数量 / 小计`。不含"占比"列（Problem Frame 只要"每类多少、总共多少"，百分比不在痛点中，且引入后会有"分母含不含 UNPARSED"的边界规则负担）。
- R13. 金额汇总表末尾追加一行 `总计`；若 `low_conf.count > 0`（条件与 R16.7 OpenClaw 消息**完全一致**，不再用"有金额行"做额外门槛），表下方加一行脚注 `† 其中 {low_conf.count} 项金额存疑（可信度=low，合计 ¥{low_conf.amount}），见末尾「⚠️ 需人工核查」区`。
- R14. 现有数量摘要、P1/P2/P3 配对表、未匹配项、补搜建议、UNPARSED 区块保持不变 —— 本次只**追加**金额汇总表，不重写已稳定的报告结构。

**OpenClaw 聊天总结模板 (print_openclaw_summary)**

- R15. 新增函数 `print_openclaw_summary(aggregation, output_dir, zip_path, missing_status)`，写 stdout（不是 MD），在 `download-invoices.py` 主流程末尾、zip 生成后调用。`missing_status` 传 `missing.json` 的 `recommended_next_action`（`stop` / `run_supplemental` / `ask_user`）。单次调用输出 ≤ 20 行。
- R16a. **非空模板**（`voucher_count > 0` 或 `unmatched.* > 0` 或有失败下载；即本次有可报告内容）固定结构（按顺序）：
  1. 标题行：`📄 发票报销包 — {start_date} → {end_date}`
  2. 空行
  3. 合计行：`✅ 共 {N} 份凭证，合计 ¥{grand_total}{dagger}`。`N = aggregation.voucher_count`（R1 已定义为 CSV 非-UNPARSED 行数，合并行算 1 份）。`{dagger}` = `" †"` 仅当 `low_conf.count > 0`。
  4. 每类一行：`  • {label} {count} 份    ¥{subtotal}`（无金额类别隐藏）。**单位统一为「份」**，覆盖所有类别（餐饮/火车/出租/话费/通行费/其它/UNPARSED 均用"份"），未来新增类别不必扩映射表。`label` 用 `CATEGORY_LABELS[cat]`（如"酒店"、"网约车"、"餐饮"、"UNPARSED"）。
  5. 空行
  6. 未配对提醒（仅当有未配对）：`⚠️ {N1} 张酒店发票无对应水单` / `⚠️ {N2} 份水单无对应发票` / `⚠️ {N3} 张网约车发票无行程单` / `⚠️ {N4} 份行程单无发票`。每种只在 N>0 时出现。
  7. 可信度脚注（仅当 low_conf.count > 0）：`† 其中 {low_conf.count} 项金额存疑（可信度=low，合计 ¥{low_conf.amount}），请人工复核`。**注意**：与 R13 MD 脚注条件**统一为 `low_conf.count > 0`**（不再仅检查"有金额行"），且两处都连带展示金额，保证 CSV/MD/消息三处同步。
  8. 空行
  9. **下一步行动**（必须出现）：根据 `missing_status` 渲染：
     - `stop` → `👉 下一步：可以提交报销 — 打开上面 zip`
     - `run_supplemental` → 两行：`👉 下一步：建议补搜 —` 换行后缩进 `python3 scripts/download-invoices.py --supplemental --start {start} --end {end} --output {output_dir}`
     - `ask_user` → `👉 下一步：需人工核查 — 见 {md_path} 末尾「⚠️ 需人工核查」区`
  10. 空行
  11. **交付物（主次分级）**：第一行 `📦 报销包（提交这个）: {abs_zip_path}`；第二行缩进 `  明细: {abs_csv_path}   |   报告: {abs_md_path}`。zip 独占醒目一行，CSV/MD 作为次要引用挤在一行。
- R16b. **空模板**（`voucher_count == 0` AND 所有 `unmatched.* == 0` AND 无失败下载；即 Gmail 搜索完全空）覆盖 R16a 的 1-11：
  ```
  ℹ️ 本次未下载到凭证 — 日期范围：{start_date} → {end_date}
     可能原因：关键词未覆盖 / 日期区间无邮件 / learned_exclusions.json 过滤过严
     检查：{log_path}
  ```
  不输出 zip/csv/md 路径（此时它们可能不存在或为空壳），不输出"下一步"（Gmail 完全空时 supplemental 也救不了）。3 行完事。UNPARSED 行存在即视为有内容，走 R16a（因为这正是需要人工核查的场景）。
- R17. 所有金额展示格式 `¥{Decimal:.2f}`；份数直接 `int`。所有数字来自 R1 `aggregation` 结构，不在模板层做任何算术。所有交付物路径使用**绝对路径**（`os.path.abspath(...)`），便于用户在终端/Finder 直接点开。

## Success Criteria

- CSV 打开后，一次酒店入住（匹配成功）占 1 行，发票+水单两个文件名都在行内；"总计"单元格能直接抄给老板。
- MD 报告前置的金额汇总表让用户 **3 秒**内答出"酒店花了多少 / 总共花了多少"，不需要打开 CSV。
- OpenClaw 跑完后，聊天窗口最后那段 ≤ 20 行的总结，即使不点开任何文件，用户也能判断"是否需要 supplemental 补搜 / 是否可以走报销流程"。
- **三处数字永远相等**：CSV 总计行、MD 金额汇总表总计、OpenClaw 消息合计行读取的都是同一个 `aggregation.grand_total` Decimal 值。
- 测试覆盖：`tests/test_postprocess.py` 新增一组用例覆盖 (a) 酒店配对合并 1 行、(b) 网约车配对合并 1 行、(c) 未配对单票据独立成行、(d) `None` 金额不入小计但保留在明细、(e) low_conf 的合计旁标数字、(f) 金额用 Decimal 求和的精度（避免 `0.1 + 0.2`）。全部沿用现有 mock fixtures，不引入新的 LLM 调用。

## Scope Boundaries

- **不改 `missing.json` schema v1.0**。这个 schema 是 Agent 契约的一部分，任何改动需要单独的 brainstorm。
- **不改 Agent Loop Playbook / Exit Codes**。OpenClaw 总结只是 stdout 里新增的输出段，不走非零退出码、不影响 Agent 决策。
- **不做"按月 / 按项目"二级分组**。财务报销场景是按"日期区间"一次性跑完，二级分组是 YAGNI。
- **不给 CSV 做多 sheet / xlsx 输出**。UTF-8 BOM CSV 已是当前交付约定；改成 xlsx 会引入 `openpyxl` 依赖，这次不值得。
- **LLM 不参与任何算术**。即使 prompt 看起来能算，也必须走 Python。违反这条的 PR 直接驳回。
- **不重写 `write_report_v53` 已有的 P1/P2/P3 表格、未匹配项列表、补搜建议区**。本次纯追加一张新表，不动已经在生产跑的逻辑。
- **不给单票据合并行引入跨类别合并**（例如"酒店发票 + 餐饮发票同日同商户"这种）。只对 matching_result 里已经成对的 hotel / ridehailing 合并。
- **不改动 `scripts/core/matching.py` 与 `do_all_matching` 的配对判定逻辑**（P1 remark / P2 日期+金额 / P3 date-only fallback）。本特性是 matching 的**下游消费者**，不是 matching 的修订。

## Key Decisions

- **DEC-1 合并行而不是分组号**：CSV 用 1 行 1 对，而不是两行 + "住宿编号"分组列。理由：Excel 数据透视/筛选场景下，分组号列会让 SUM 翻倍；合并行天然去重，finance 直接 SUM 金额列即可。
- **DEC-2 "配对凭证"单列而不是双列**：CSV 不拆"水单文件"+"行程单文件"两列（会多一列空白），统一叫「配对凭证」，`备注` 列注明种类。理由：对未来新增的"同类配对"场景（如其它类别也出现双票据）不用再扩列。
- **DEC-3 low_conf 照样计入总计 + 旁标提醒**：不采用"已核 / 待核"双数字方案。理由：财务报销常见流程是"先汇总给老板看，疑似项后补"，拆两个数字反而让人追问"那到底要不要报"。旁标把决定权留给用户。
- **DEC-4 `decimal.Decimal` 而不是 `float`**：聚合层全部 Decimal。理由：报销金额是钱，`0.1 + 0.2 ≠ 0.3` 会让 SUM 列出现 `2852.0000000001`，老板看见一次就记住你不靠谱。
- **DEC-5 追加而不是重写 MD 报告结构**：金额汇总表作为新 `## 💰 金额汇总` 区块前置插入，不改 P1/P2/P3 表格。理由：v5.3 的 report 结构已被 `tests/test_agent_contract.py` 钉住，重写风险远大于追加。
- **DEC-6 OpenClaw 总结走 stdout 而不是新文件**：消息模板由 `print_openclaw_summary` 直接 print 到 stdout。理由：OpenClaw 聊天回传的本来就是 stdout 最后一段，新增文件只会让 Agent 多一次读盘 + 用户多一个东西删。

## Dependencies / Assumptions

- `decimal.Decimal` 是标准库，不需要新增依赖。
- 依赖 `matching_result` 结构中 `hotel.matched[*].invoice` 和 `.folio` 都携带 `_record` 的既有约定（`postprocess.do_all_matching` 已如此返回）。
- 假设所有 `ocr.transactionAmount` / `ocr.balance` 字段在 LLM 抽取时都以 RMB 为单位；目前代码库里只处理中国发票，这条假设成立。若未来支持外币，本聚合层要加 currency 维度 — 本次不做。
- **业务约束：酒店发票与水单金额一致是成熟业务假设**。现行配对规则是 P1 remark 匹配（`invoice.remark == folio.confirmationNo`，不查金额，因为 remark 即订单号）、P2 日期+金额匹配（`is_amount_match`，0.01 容差）、P3 日期-only 兜底（标 `confidence="low"`）。这三层已在生产稳定运行并被 `TestHotelMatchingTiers` 钉住。**本 brainstorm 明确不触碰 `scripts/core/matching.py::match_hotel_pairs` 与 `scripts/postprocess.py::do_all_matching` 的任何判定逻辑**；合并为 1 行是基于"配对成功即金额一致"这条业务约束的安全操作，不需要 cross-check 金额或在 P3 下显示两个金额。

## Outstanding Questions

### Resolve Before Planning

（无 — 所有产品决策已落定，下面的项均可交给 /ce:plan 在实现中决定）

### Deferred to Planning

- [Affects R2][Technical] 匹配上的酒店行，"销售方"取发票 `vendorName` 还是水单 `hotelName`？现有 `write_summary_csv:565` 的优先级是 `ocr.vendorName → r.merchant → "—"`；合并后建议沿用同一优先级（发票侧），与 R2 金额/日期都取发票侧的通则一致。Plan 阶段用真实 fixture 验证。
- [Affects R8][Technical] `配对凭证` 列值的文件名部分用 `basename`（与现有 CSV 一致）。Plan 阶段确认即可。
- [Affects R15][Technical] `print_openclaw_summary` 在 `--no-llm` 模式下的行为（此时金额为 None 或退化值）— 走空模板（R16b）还是仍按 R16 打印并全部显示 `¥0.00` ？规划时看 `download-invoices.py` 现有 flag 语义决定。
- [Affects R6][Technical] `Decimal` 转换入口点：`Decimal(str(float_val))` vs `Decimal(float_val)` — 前者避免二进制残差。Plan 阶段统一在 `build_aggregation` 边界做 `str()` 转换。
- [Affects R11][Technical] `Decimal` 的 rounding mode 默认 `ROUND_HALF_UP`（与 R6 DEC-4 一致，财务习惯）。Plan 阶段在 `build_aggregation` 顶部 `getcontext().rounding = ROUND_HALF_UP` 一次性设置即可。
- [Affects R7][Needs research] 若决定在 `CATEGORY_ORDER` 中新增 `HOTEL=0` / `RIDEHAILING=3`，需 grep `tests/test_postprocess.py` 与 `tests/test_agent_contract.py` 中任何隐式依赖当前排序值的断言。Plan 阶段先扫描再改。
- [Affects R9][Technical] CSV 小计/总计行的 `序号` 列值：用 `—`（与数据可信度列留空风格一致）还是字面值 `小计` / `总计` ？Plan 阶段定。

## Next Steps

→ `/ce:plan` for structured implementation planning
