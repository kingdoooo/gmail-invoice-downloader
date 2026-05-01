---
date: 2026-05-01
topic: exclusions-self-service-invite
---

# OpenClaw 总结末尾追加"自助新增排除规则"邀请语

## Problem Frame

现在 `learned_exclusions.json` 是单一事实源（40+ 条规则，覆盖银行/券商/订阅/营销/IHG 反馈问卷等），**完全靠用户手动维护**。代码里唯一间接提示用户去动这个文件的地方，是空运行 R16b 模板的「过滤过严」提示（`scripts/postprocess.py:870-871`）—— 方向是**减规则**。

反方向的需求一直没被产品化：跑完一次后，用户大概率会肉眼看到交付物里混进了**不该报销的凭证**（Apple 订阅 / SaaS 发票 / 个人账单邮件偶然带了 PDF 等），但当前的 OpenClaw 总结没有任何一处提醒用户"可以直接告诉我，我加进 exclusions"。用户要不就自己去读 `learned_exclusions.json` 的格式手动改，要不就在下次跑时一次次看到同样的噪音。

这次只做**最轻的干预**：在 `print_openclaw_summary` 的 R16a 非空模板末尾追加一行对话式邀请语，把"加规则"的操作入口从隐藏文档里提出来，变成每次跑完都会被看到的一行邀请。Agent 看到用户回复（「Apple 那条加上」之类）后，按已有的约定去改 `learned_exclusions.json` 即可。

**明确不做的事**：不聚合 `skipped` 数组、不分析 sender 频次、不打阈值、不做交互式 Y/N —— 那些都是更早对话轮评估过的方向，最终结论是"对新用户首跑场景过度工程化，且对 `method=MANUAL` / `failed` / `UNPARSED` 有误杀真发票的风险"。

## Requirements

- R1. 在 `scripts/postprocess.py::print_openclaw_summary` 的 R16a 非空模板中，在**现有最后一个 writer 调用 `writer(f"  明细: {abs_csv}   |   报告: {abs_md}")`**（参考位置 `scripts/postprocess.py:944`）**之后**，追加 3 个新的 writer 调用，分别为：
  1. 空 writer（`writer("")`）：与交付物块用空行隔开，不要挤成一坨。
  2. `writer("💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？")`
  3. `writer("   直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。")`

  文案**完全照抄**，视觉效果：
  ```
  💡 发现不该报销的（SaaS 订阅 / 个人账单 / 营销邮件）？
     直接在聊天里告诉我，我会加到 learned_exclusions.json，下次自动排除。
  ```

  第二行以 3 个半角空格缩进（为与 💡 emoji 在等宽字体下视觉对齐：emoji 占 2 列 + 1 空格 = 3 列）。此值**在本需求中固定为 3 空格**，除非 Plan 阶段在真实终端渲染中发现明显错位，否则不调整。注意此插入点位于 R16b 早退返回分支之后（R16b 在 `scripts/postprocess.py:864-874` 以 `return` 结束），因此 5 行追加天然只作用于 R16a，无需额外分支判断 —— 这同时满足 R2 的"仅 R16a"约束。

- R2. **仅出现在 R16a 非空模板**。R16b 空模板（`voucher_count == 0` 且全无 unmatched/UNPARSED/failed）**不追加**这段邀请 —— 空运行时"啥都没下来"，追问"有没有噪音要排除"是答非所问，且 R16b 自己已经用「可能原因：... learned_exclusions.json 过滤过严」覆盖了反方向提示，两边混在一起会让用户困惑。

- R3. 邀请语是**无条件的**（只要走 R16a 就一定打印），**不**根据本次运行的凭证数量、unmatched 数量、skipped 大小、`method=IGNORE` 频次做任何动态开关。理由在 Key Decisions DEC-1 展开。

- R4. 不新增任何聚合/统计函数，不读取 `skipped[]`、`step4_downloaded.json`、`learned_exclusions.json`，`print_openclaw_summary` 的现有函数签名**保持不变**。本特性就是 ~5 行 writer 调用的追加。

- R5. 更新 R16a 的总行数上限约束：原文在 `print_openclaw_summary` docstring 写的是 "≤20 行"。加完后非低置信场景的实际行数：标题 1 + 空 1 + 合计 1 + 每类 1 行（典型 2-6 行）+ 空 1 + 未配对 0-4 行 + low_conf 脚注 0-1 行 + 空 1 + 下一步 1-2 行 + 空 1 + 交付物 2 行 + 空 1 + 邀请 2 行 ≈ 16-24 行。**将 docstring 上限从 "≤20" 改为 "≤24"**（保留数字作为 code review 触警值，比"≤ 一屏"这种定性描述更有约束力，在未来有人无意扩张模板时能立刻引起注意）。

- R6. 测试覆盖（`tests/test_postprocess.py` 现有 `TestPrintOpenClawSummary` 类，位于 `tests/test_postprocess.py:1668`，注意类名是 **`OpenClaw`** CamelCase 不是 `Openclaw`；该类已使用 `writer=lambda s: sink.append(s)` 模式，新测试沿用同一模式）中新增：
  - (a) R16a 非空模板下，邀请语两行**严格匹配**（包括 emoji、换行、缩进、`learned_exclusions.json` 文件名）。用 `assert "💡 发现不该报销的" in output` 和 `assert "learned_exclusions.json，下次自动排除" in output`。
  - (b) R16b 空模板下，邀请语**必须不出现**。`assert "💡 发现不该报销的" not in output`。
  - (c) 邀请语出现在"交付物"块之后（即输出中 `📦 报销包` 或 `明细:` 行出现的位置早于 `💡 发现不该报销的` 行）。用 `output.index("💡") > output.index("明细:")` 或等价断言。**同时**断言两行的顺序完整性（防止只贴了第一行却漏了第二行）：`assert output.index("💡 发现不该报销的") < output.index("下次自动排除")`。
  - (d) 没有额外的 I/O、没有新函数导入，保持 `test_postprocess.py` 现有 mock 结构不变。

## Success Criteria

- 每次正常跑完（有凭证下载）的 OpenClaw 聊天最后，用户都能读到那两行邀请语，并且能在同一个聊天会话里直接回复"把 Apple 那条加进去" —— Agent 根据现有 `learned_exclusions.json` 格式（`{rule, reason, confirmed}` 三字段）追加条目即可，不需要新的 Agent 工具或约定。
- 空运行不出现邀请语（验证方式：跑一次肯定空的日期区间，看 stdout）。
- `print_openclaw_summary` 总行数即便在类别最多的情况下仍可在一屏（24 行以内）读完。
- 零回归：`tests/test_postprocess.py` 和 `tests/test_agent_contract.py` 全部仍通过。

## Scope Boundaries

**数据与聚合**
- **不新增 `skipped` 聚合** —— 不做 "sender 域名 / subject 关键词 ≥ N 次" 的候选名单生成。原始想法评估后弃用，理由在 Key Decisions DEC-2。
- **不做跨运行累计统计**。没有 history store，也不新增。

**文件与契约**
- **不碰 `learned_exclusions.json` 的读写代码** —— 文件加载路径（`download-invoices.py:237`）、格式（`{exclusions: [{rule, reason, confirmed}]}`）、Agent 如何追加条目，这些都是已有稳定契约，不改。
- **不改 MD 报告 / CSV / missing.json** 的任何输出。这条邀请是聊天层独占。

**模板结构**
- **不改 R16a 的既有 11 步结构与顺序**（标题/合计/分类/空行/未配对/脚注/下一步/交付物），纯追加 1 个新步块。
- **不改 R16b 空模板**。

**交互形态与本地化**
- **不做交互式多选确认**（"你要屏蔽下列哪些 sender？[y/N]"）。Agent Loop 走 stdin/stdout 的轮换，塞交互问卷会破坏当前的 exit code + REMEDIATION 契约。
- **不做 i18n** —— Skill 面向中文用户，文案固定中文。

## Key Decisions

- **DEC-1 无条件打印，不做智能触发**：邀请语每次 R16a 都出现，不根据 skipped 频次、没下载到已知品牌 SaaS 邮件等信号条件化。理由：(a) 条件化一定要读 `skipped[]`，会引入聚合层和分类学判断（`IGNORE` 安全、`MANUAL` 危险、`failed` 多半是真发票），复杂度远超邀请语本身；(b) 新用户首跑场景就是主要价值场景，而新用户最容易漏看的恰恰是偶发的 1 条噪音 —— 阈值会把这条关掉；(c) 老用户会把邀请语当作"背景文案"自动忽略，干扰极小；两行的信噪比是可接受的。
- **DEC-2 放弃 sender/subject 频次聚合方案**：最初用户提议对 `skipped[]` 按 sender 域名 / subject 关键词聚合出现次数 ≥ N 的条目。评估后有三个硬伤：(1) `skipped[]` 混了 `method=IGNORE`（安全）与 `method=MANUAL`（多半是没自动化的真发票平台），不加"真发票保护名单"就会建议用户屏蔽百望云/诺诺等关键平台；(2) 新用户首跑时 `learned_exclusions.json` 为空，一次跑下来一堆银行/券商邮件先是进 classify 决策树的非发票分支，但不一定落进 `skipped[]` —— 降级到"建议增加 skipped 聚合"实际上只覆盖了已经 IGNORE 的一小部分；(3) 纯产品层面，"一句话邀请"已经把用户大脑里的那个想法说出来了，剩下的识别工作人类比机器准。
- **DEC-3 空运行不追加邀请语**：R16b 的业务语义是"Gmail 搜索空集"，此时谈"排除"是反方向的，容易让用户误以为"刚才少下的就是被排除的"。把 R16b 保持 3 行不动，是对"空运行 = 最少信息" 这条 R16b 原设计意图的尊重。
- **DEC-4 放在交付物之后而非"下一步"之前**：第 9 步「下一步」已经是动作指引（`stop` / `run_supplemental` / `ask_user`）；把"加排除规则"塞到「下一步」里会让 3 种 missing_status 分支各自再扩一种变体。放在交付物之后、模板末尾，语义上是"交付完毕之后的增量改进建议"，与「下一步」的"本次流程怎么收尾"正交，互不干扰。

## Dependencies / Assumptions

- 假设 Agent（OpenClaw / Claude Code / 其它宿主）在用户对 stdout 邀请语做出自然语言回复后，能够自行找到 `learned_exclusions.json`、读懂现有三字段格式（`{rule, reason, confirmed}`）、追加一条新规则。这是既有约定，本特性不新增 Agent 侧工具。
- 假设 `learned_exclusions.json` 的 `confirmed` 字段值用 `YYYY-MM-DD`（现有所有 40+ 条都是这格式）。Agent 追加时沿用。
- 无代码依赖变化。标准库够用。
- 假设终端与 OpenClaw 面板都能渲染 emoji `💡`；当前 R16a 已经大量使用 `📄/✅/⚠️/📦/👉/†` 等，这条假设已成立。

## Outstanding Questions

### Resolve Before Planning

（无）

### Deferred to Planning

- [Affects R6][Technical] 测试文件里，断言邀请语顺序位置（在交付物之后）是用 `output.index(...)` 比较还是把 writer 的 call list 拆成行再 index 比较。现有 `TestPrintOpenClawSummary` 测试风格 Plan 阶段 grep 一下现有断言沿用同一风格。

## Next Steps

→ `/ce:plan` for structured implementation planning
