---
date: 2026-05-01
topic: ignore-non-reimbursable-receipts
---

# 非「发票 / 水单 / 行程单」的票据一律过滤

## Problem Frame

2025Q4 烟雾测试里，一张 **Termius** 英文 SaaS 订阅发票（$120 USD Stripe 模板）被 LLM 分类为 `HOTEL_FOLIO`，进了酒店匹配管道永不匹配，落到 `missing.json` 里作为一条永远修不好的 `hotel_invoice` 缺口。Agent loop 会走 `convergence_hash` 兜底假收敛，但收敛本身是谎——它压根不是报销票据。

用户的框架很简单：**发票 / 水单 / 行程单都是格式相对固定的票据，不符合的直接过滤**。不需要猜、不需要分"五道门"、不需要为未来的假想供应商做架构准备。

## User Flow

```
classify_invoice (classify.py)
   ├─ 中国发票 特征？    → MEAL / HOTEL_INVOICE / RIDEHAILING_INVOICE / TAXI / TRAIN / MOBILE / TOLLS
   ├─ 水单 特征？        → HOTEL_FOLIO
   ├─ 行程单 特征？      → RIDEHAILING_RECEIPT
   └─ 都不符合           → IGNORED
                              ↓
                         加 IGNORED_ 前缀保留文件
                         不进 matching / CSV / zip
                         下载报告.md 末尾一节列出
                         OpenClaw 汇总加一行"已忽略 N 张"
```

## Requirements

- R1. `scripts/core/classify.py::classify_invoice` 新增合法返回值 `"IGNORED"`。判定顺序保留现有逻辑，只在最后一条默认出口做改动：
  - 当前 fallthrough 到 `"UNKNOWN"` 的那一支，改为返回 `"IGNORED"`。
  - 酒店水单分支收窄一点：`is_hotel_folio_by_doctype()` 单独命中不够，需同时至少出现 `hotelName / confirmationNo / internalCodes / roomNumber` 之一。`is_hotel_folio_by_fields()`（3 选 2）路径保留不动。这条是为关掉 Termius 那条具体的滑入路径，不做更多。
  - 改动务必登记在 `scripts/core/__init__.py` 的「Modifications from source」列表，并在 `SKILL.md` Lessons Learned 里写一条，避免下次 snapshot 同步被无声覆盖。
- R2. IGNORED 记录处理（`scripts/postprocess.py`）：
  - `rename_by_ocr` 对 `category == "IGNORED"` 的记录改名为 `IGNORED_{sender_short}_{原名}.pdf` 保留在 output_dir。`sender_short` 从邮件 `from` 头取（不走 LLM 抽的 vendorName），空时 fallback 为 `unknown`。文件名总长保持在现有 sanitize 预算内。
  - `do_all_matching` 的 `valid_records` 进入前按 `category == "IGNORED"` 过滤成独立列表 `ignored_records`；既有的 `accounted == len(valid_records)` 完整性断言同步改为 `accounted + len(ignored_records) == len(valid_records)`，保留对"偷偷丢记录"的防御。
  - `CATEGORY_LABELS` / `CATEGORY_ORDER` 里登记 `IGNORED`，避免兜底成 `发票` 导致文件名被拼成 `IGNORED_xxx_发票.pdf`。
- R3. 用户通知（最少量）：
  - `下载报告.md` 末尾新增一节「已忽略的非报销票据 (N)」：每行 `- {sender}：{金额 或「金额未识别」}`。N=0 时整节省略。
  - `print_openclaw_summary`（聚合汇总特性）的末尾追加一句 `📭 已忽略 {N} 张非报销票据`。若该函数尚未落地，退化为在现有 `say(...)` 流里加一行同样内容，不阻塞。
  - `发票打包_*.zip` 不打包 `IGNORED_*.pdf`（文件名前缀过滤即可；`UNPARSED_*.pdf` 的 zip 行为保持不变——用户需要看到解析失败的票据）。
- R4. **不**动 `missing.json` schema：
  - IGNORED 记录**不**进 `items[]`、**不**影响 `convergence_hash`、**不**影响 `status / recommended_next_action`。
  - 不新增 `ignored[]` 顶层字段，不升版。保留 schema_version `"1.0"` 不变——Agent 合约对"已忽略"完全透明，本就无需知道。需要回头看忽略详情的是用户（在报告里看到），不是 Agent。

## Success Criteria

- Termius 样例下一次跑：落成 `IGNORED_termius_*.pdf`、不在 CSV、不在 zip、出现在下载报告「已忽略」一节和 OpenClaw 汇总的一行。
- 既有测试不回归：`test_postprocess.py` / `test_agent_contract.py` 全绿。特别地，`missing.json.schema_version == "1.0"` 的硬检查不改。
- 新增最少量测试：(a) IGNORED 样本走白名单路径落入 IGNORED；(b) IGNORED 文件名以 `IGNORED_` 开头且不进 zip；(c) 报告「已忽略」节 N=0 时不渲染。
- 下一个英文 SaaS 样本（GitHub / Figma 等）走同一条路径，代码零改动。

## Scope Boundaries

- **不**改 `scripts/core/prompts.py`——OCR prompt 是与 reimbursement-helper 的共享契约。
- **不**引 `learned_exclusions.json` 的新规则，两者正交保留。
- **不**新增 LLM 调用。IGNORED 判定纯粹依赖现有 OCR 字段。
- **不**动 `status / recommended_next_action / convergence_hash / schema_version`。Agent 状态机原封不动。
- **不**做"自动学习哪些 sender 应该忽略"、"根据 ignored 数据触发报警"等未来向功能。统计稳定后再另开议题。
- **不**改 `scripts/invoice_helpers.py`。

## Key Decisions

- **只把默认出口换成 IGNORED，不重写分类架构**：用户明确说"格式相对确认，不符合就过滤"。原本的 classify.py 已经是"正向匹配 → 命中类别，fallthrough → UNKNOWN"，只需把最后一档默认从 UNKNOWN 换成 IGNORED，附带把水单分支里最容易被 LLM 幻觉骗进的 `docType-only` 路径收窄。不搞"五道门"的复杂分层叙述。
- **UNKNOWN 合并进 IGNORED**：现有 UNKNOWN 语义是"OCR 成功但服务类型未命中"。在白名单化后，它和 IGNORED 事实上已等价——当前代码路径里 UNKNOWN 还是会被塞进 CSV/报告，但对用户没有行动意义。合并让规则更简单、路径更少、下游 `category not in {UNPARSED, UNKNOWN}` 这类 carve-out 可以逐步清掉。
- **保留 IGNORED_ 前缀文件不删**：对称 UNPARSED_ 的写法，保留审计线索。若未来跨季度积累过多再考虑子目录化——现在不提前抽象。
- **不升 missing.json schema**：避免为无 Agent 消费者的字段做契约迁移。Agent 只关心"还有多少真实缺口"，IGNORED 对它透明。

## Dependencies / Assumptions

- 依赖并行进行中的聚合汇总特性（`print_openclaw_summary`）。若它先落地，R3 的 OpenClaw 行直接进；若它未落地，R3 降级为在既有 `say(...)` 流里加一行——不阻塞。
- 假设 LLM 不会同时幻觉出 `hotelName + 日期字段 + 确认号` 三件套。这个假设不完美（LLM 可以任意填字段），但比当前"只要 docType 含 Invoice 就进 HOTEL_FOLIO"强得多。若未来出现新绕过案例，再落 sanity check，不提前防御。

## Alternatives Considered

| 方案 | 覆盖面 | 改动面 | 备注 |
|---|---|---|---|
| Option 3 solutions 文档推荐: HOTEL_FOLIO 专用 sanity check | 只挡"误判进 HOTEL_FOLIO" | 极小，classify.py 一处 | 下次误判进别处还要再补 |
| Option 4: `learned_exclusions.json` 加 `-from:termius.com` | 单点止血 | 一行 JSON | 治标不治本，保留作为第二道防线 |
| **本方案**: fallthrough → IGNORED + 水单分支收窄 | 覆盖所有未命中类别 | classify.py + postprocess.py 少量改动 | **选中**，用户意图的最小实现 |
| 五道门 + schema v1.1 + 三路通知 + ignored[] Agent 消费 | 覆盖全部 + Agent 自动化 | 大 | **放弃**，对单一数据点过度设计 |

## Outstanding Questions

### Deferred to Planning

- [Affects R1][Technical, evidence-first] 水单分支收窄（docType-only 路径要求至少一个结构字段）会不会踢掉历史合法水单？planning 阶段跑一遍 `~/.cache/gmail-invoice-downloader/ocr/*.json` 离线回放确认。差集非空就继续放宽条件、或加 fixture 锁定。
- [Affects R2] IGNORED 记录在 `do_all_matching` 返回字典里是放进新增的 `"ignored"` 键、还是直接丢弃只保留到 `records[]`？两种都能让 R3 工作，planning 阶段择一即可。

## Next Steps

→ `/ce:plan` for structured implementation planning
