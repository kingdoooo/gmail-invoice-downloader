---
title: "refactor: Skill compliance cleanup + Agent contract evals"
type: refactor
status: completed
date: 2026-05-01
completed: 2026-05-01
origin: docs/brainstorms/2026-05-01-skill-compliance-and-evals-requirements.md
---

# refactor: Skill compliance cleanup + Agent contract evals

## Overview

Four-part cleanup before tagging the project as publishable:

1. **SKILL.md 合规微调**（Unit 1）— 瘦身 `description`、给 `references/platforms.md` 加 ToC、顶部加 OpenClaw 说明。不拆 Lessons Learned（用户已选择最温和方案）。
2. **`scripts/v53_pipeline.py` → `scripts/postprocess.py` 去版本化重命名**（Unit 2）— 纯文件名重构，不改一行 pipeline 逻辑。
3. **新增 `tests/test_agent_contract.py` — Mock 驱动的 Agent 契约 evals**（Unit 3）— 覆盖 CI 可自动跑的 R8-R12 合约面。
4. **新增 `references/seasonal-smoke.md` — 真实 Gmail + LLM 的季度手动 runbook**（Unit 4）— 与 Unit 3 互补，捕真实 API/LLM 漂移。

零业务功能变更。目标是把 v5.3 推到"可以交给陌生开发者或 Agent 审核者看也不尴尬"的状态。

## Problem Frame

v5.3 功能完整（94/94 单元测试通过、6 个 LLM provider、10 步 pipeline 跑通），但作为 OpenClaw Skill 还有两类卫生欠债：

- **元数据 / 导航问题**：`description` 180 词超出 skill-creator 100 词指引；`references/platforms.md` 686 行无目录；非标 frontmatter 键（`display_name`、`icon`）缺乏"这是 OpenClaw 特化"的说明。
- **Agent 合约无端到端防线**：exit 码、`REMEDIATION:` stderr、`missing.json` schema v1.0、zip manifest 四项是 Agent loop 的生死线，但目前只有组件级单元测试，没有"从 CLI 入口到交付物"的集成断言——未来 refactor 会静默打破 Agent 合同。
- **命名腐烂第一个信号**：`scripts/v53_pipeline.py` 把版本号写进文件名。v5.3 只是它首次落地的版本，它**职能上**是"下载后的后处理模块"——v5.4/v6.0 来临时要么扩散重命名，要么留作误导性化石。

Origin doc: `docs/brainstorms/2026-05-01-skill-compliance-and-evals-requirements.md`

## Requirements Trace

**SKILL.md 合规（Unit 1）**
- R1. `description` 精简到 ~100 词，保留所有触发关键词，移平台枚举到正文
- R2. SKILL.md 顶部加 OpenClaw Skill 特化说明段
- R4. `references/platforms.md` 顶部加 ToC

（R3 "不动大段落"已移入 Scope Boundaries——它是非目标，不是交付项，保留历史编号避免下游引用错位）

**命名去版本化（Unit 2）**
- R5. `scripts/v53_pipeline.py` → `scripts/postprocess.py`，所有 import/doc 点跟进
- R6. `tests/test_v53_pipeline.py` → `tests/test_postprocess.py`
- R7. 函数签名与行为不变；Unit 2 完成时测试仍 94 全绿（Unit 3 之后升至 ≥ 105）

**Agent 契约 evals（Unit 3，Mock 驱动）**
- R8. Exit 码 + REMEDIATION 契约（exit 2/3/4/5 + stderr `REMEDIATION:` 前缀行）
- R9. 匹配三层（P1 remark / P2 date+amount / P3 同日兜底 + `⚠️` 标记）
- R10. 状态机（R10a convergence_hash 可复现 + R10b 状态跳转 converged/max_iterations_reached/user_action_required）
- R11. missing.json schema v1.0 的 enum 值严格断言
- R12. zip manifest allowlist（只含 pdf/md/csv，不含 run.log/step*_*.json/嵌套 zip）

**季度 smoke（Unit 4，真实 Gmail + LLM）**
- R13. Runbook 含 "When to run / Prerequisites / Command / Expected timing / Pass criteria"
- R14. 结果记录位置 + 失败排查流程
- R15. 不做 CI 集成，季度人工触发

## Scope Boundaries

- 不改任何业务逻辑（`invoice_helpers.py`、`scripts/core/*` 均不动，后者还是显式快照）
- 不新增 LLM provider、不新增平台解析器、不改匹配算法、不改 OCR prompt
- 不做 CI/CD 配置（`.github/workflows` 等）
- 不做 `run_loop.py` 的 description trigger 优化
- **不拆 SKILL.md 大段落**：Lessons Learned / Workflow 10-step / Handling Unknown Platforms 三大段保留原文（原 R3 归属于此）
- **测试基础设施不算"新增业务功能"**：`test_agent_contract.py` 只在 CLI 边界外加断言，不改 `download-invoices.py` 的一行行为

## Context & Research

### Relevant Code and Patterns

**Exit codes + REMEDIATION 契约（Unit 3 mock 要打的面）**
- `scripts/download-invoices.py:84-89` — `EXIT_OK / EXIT_UNKNOWN / EXIT_AUTH / EXIT_LLM_CONFIG / EXIT_GMAIL_QUOTA / EXIT_PARTIAL` 常量
- `scripts/download-invoices.py:92-95` — `GmailQuotaError` 异常类
- `scripts/download-invoices.py:794,803,883,1001,1007` — 所有 `REMEDIATION:` stderr 写入点
- `scripts/download-invoices.py:733,799,807,885,987,988,1003,1011` — 所有 `sys.exit(EXIT_*)` 调用点

**Mock seam（最窄的 mock 注入点）**
- `scripts/download-invoices.py:111-197` — `GmailClient` 类，核心是 `_api_get()` (line 134)
- `GmailClient.search / get_full_message / get_attachment_bytes` 三个方法全部走 `_api_get`
- 结论：monkeypatch `GmailClient._api_get` 是最窄的 mock 面；返回预设 JSON 即可覆盖整个下载链。LLM 侧 monkeypatch `core.llm_ocr.extract_from_bytes` 或直接 `--llm-provider=none`

**missing.json 状态机（Unit 3 R10 要打的面）**
- `scripts/v53_pipeline.py:667` — `_compute_convergence_hash(items)` 纯函数，最容易单元测
- `scripts/v53_pipeline.py:682-822` — `write_missing_json(...)` 含 `status` / `recommended_next_action` 判定
- `scripts/download-invoices.py:654,666` — `_previous_convergence_hash` / `_previous_iteration` 读上一轮状态的辅助

**现有测试命名风格（Unit 3 新测试要镜像这套）**
- `tests/test_v53_pipeline.py` 里所有类：`TestPathTraversal / TestHallucinationDetection / TestRetry / TestOCRCache / TestRenameHappyPath / TestZipAtomic / TestSummaryCSV / TestHotelMatchingTiers / TestProviderMatrix`
- 一致的 "Test<FunctionalArea>" 命名，method 都是 `test_<specific_behavior>`

**Fixtures 已就位**
- `tests/conftest.py:19-48` — `hotel_invoice_pdf / hotel_folio_pdf / didi_invoice_pdf / didi_receipt_pdf` fixture，指向 `/Users/kentpeng/Documents/agent Test/` 的真实 PDF
- `pytest.skip` 在缺失时跳过，所以 CI 里如果 fixture 缺失不会假失败，但会降覆盖——这是可接受状态

**v53_pipeline 全部引用点（Unit 2 重命名必须覆盖）**
- `scripts/download-invoices.py:70` — `from v53_pipeline import (...)` 主 import
- `scripts/download-invoices.py:451` — `do_all_matching in v53_pipeline and write_report_v53` 注释
- `scripts/v53_pipeline.py:137` — `# that already import v53_pipeline don't need a second import line.` 自引用
- `tests/conftest.py:9` — `# Add scripts/ so tests can `import v53_pipeline` etc.` 注释
- `tests/test_v53_pipeline.py:48` — `from v53_pipeline import (...)` 主 import
- `tests/test_v53_pipeline.py:1223-1225` — `import v53_pipeline; monkeypatch.setattr(v53_pipeline, "get_client", raising_get_client)` 动态引用（关键：**除了静态 import 还有一处动态 monkeypatch 目标**）
- `SKILL.md:144,631` — Architecture 图 + Scripts 索引
- `CLAUDE.md:25,26,51,78,81,82` — 6 处命令/路径/讨论
- `scripts/core/__init__.py` — 无引用（已查过）

### Institutional Learnings

仓库暂无 `docs/solutions/`。本次产出的经验（mock seam 选 `_api_get`、文件名去版本化原则）可在未来写入。

### External References

无需。全是仓库内部的命名、文档、测试基础设施清理，无框架决策。

## Key Technical Decisions

- **Mock seam 选 `GmailClient._api_get`，而非 `search/get_full_message/get_attachment_bytes` 三个公开方法**
  - *理由*：`_api_get` 是所有**数据拉取**的出口，三个公开方法都走它。在这一层 monkeypatch 一次覆盖所有数据读取。
  - *覆盖边界*：**`GmailClient._refresh()` 不在本 mock 之内**——它直接 `urllib.request.urlopen` 访问 Google 的 token 端点，不走 `_api_get`。因此 R8 的 "exit 2 真实产生路径"（token 刷新失败）不被本 mock 覆盖。接受这一测试盲区：Unit 3 的 R8 exit 2 测试会 mock `_api_get` 抛 `GmailClient` 无法捕获的异常（模拟 auth 全失败的下游效果），而非真实走 `_refresh`。未来如有 `_refresh` 回归风险，单独加 `test_refresh` 单元测，不扩散到 `test_agent_contract.py`。

- **LLM 侧按测试目的分两档 mock**
  - **`monkeypatch core.llm_ocr.extract_from_bytes`（R9 默认 + R11 部分）**：R9 匹配三层测试**必须**用这一层——因为 `--llm-provider=none` 会让所有 PDF 走 `UNPARSED` 分支，而 `do_all_matching` 对 UNPARSED 记录直接 skip（`scripts/v53_pipeline.py:411-415` 检查 `if cat == "UNPARSED" or not d.get("ocr"): by_cat["UNPARSED"].append(d); continue`），P1/P2/P3 分支永远不会被触发。Unit 3 R9 为每个 PDF fixture 返回受控 OCR dict：`{confirmationNo, transactionDate, transactionAmount, hotelName, remark, vendorName}`。
  - **`--llm-provider=none`（R8 部分 + R11 extraction_failed 分支 + R12）**：这些测试需要的正是 "全部 UNPARSED" 状态，直接用 `none` 是最干净的路径。
  - *否定选项*：不用真实 litellm / Anthropic / Bedrock。真实 LLM 留给 Unit 4 季度 smoke 验证。Unit 3 必须零网络、零 key、可重现。

- **R10 拆成 R10a + R10b，分别针对纯函数 `_compute_convergence_hash` 和状态判定逻辑**
  - *理由*：origin doc P1 findings 指出"两轮 mock 全流程"过重。`_compute_convergence_hash` 是纯函数（input→hash），状态判定是（hash + iteration）→（status, recommended_next_action）的有限状态机。两者都能用纯单元测覆盖，零 Gmail/LLM mock 依赖。

- **Unit 2 重命名采用 "shim 并存 + 一次性切换"而非渐进式别名**
  - *理由*：本地化仓库，无外部 import 风险，没必要保留 `v53_pipeline` 别名。一次性 `git mv` + grep 替换最干净。保留兼容 shim 反而会让"为啥两个文件"成为下一波技术债。

- **`test_agent_contract.py` 做成独立文件而非并入 `test_postprocess.py`**
  - *理由*：`test_postprocess.py` 是单元测试（组件级），`test_agent_contract.py` 是集成测试（CLI 边界 + stderr/exit 断言）。边界不同，运行方式也略不同（一个用 `subprocess.run` 或 `runpy`，一个直接 import 函数）。放一起会让 fixture 和意图都混乱。

## Open Questions

### Resolved During Planning

- **Q（origin deferred）：`from v53_pipeline import ...` 是否有动态导入 / importlib 形式的引用？**
  - *Resolution*：grep 确认。静态 import 两处（`download-invoices.py:70`、`test_v53_pipeline.py:48`），动态引用一处（`test_v53_pipeline.py:1223-1225` 的 `import v53_pipeline; monkeypatch.setattr(v53_pipeline, ...)`）。Unit 2 的 files 清单已经全覆盖。

- **Q（origin deferred）：Mock 应该放在 `_api_get` 还是三个公开方法？**
  - *Resolution*：`_api_get`。见上节 Key Technical Decisions。

### Deferred to Implementation

- **Q：`subprocess.run([sys.executable, "scripts/download-invoices.py", ...])` vs `runpy.run_module` 调用 CLI 做集成测试？**
  - *Why deferred*：`subprocess` 给真实 stderr/exit code，但每次 fork 慢（~200ms×8 个 eval ≈ 1.6s 增加）；`runpy` 快但要自己捕获 `SystemExit` 和 stderr。实现时量一下性能差异再决定。

- **Q：zip manifest 断言里，`os.replace` 在 APFS vs ext4 vs NTFS 原子性是否一致？**
  - *Why deferred*：Origin doc 提到的问题。本地 Mac APFS 够用；部署到 Linux 容器时再按实际文件系统验证。这次测试先只断言最终 manifest 内容，不断言原子性时序。

- **Q：Gmail token 季度 smoke 失效时的标准排查流程？**
  - *Why deferred*：Origin doc 提到的问题。runbook 先写"遇到 exit 2 → 跑 `scripts/gmail-auth.py` 重新授权"，后续季度跑多了再补充详细诊断。

## Implementation Units

- [x] **Unit 1: SKILL.md + references 合规微调**

**Goal:** 精简 `description` 到 ~100 词、给 `platforms.md` 加 ToC、在 SKILL.md 顶部加 OpenClaw 特化说明。不动 SKILL.md 大段落。

**Requirements:** R1, R2, R3, R4

**Dependencies:** 无

**Files:**
- Modify: `SKILL.md`
- Modify: `references/platforms.md`

**Approach:**
- `SKILL.md` frontmatter `description`：保留 3 个 "Use when" 场景 + 触发关键词（发票/invoice/水单/报销/收据），移除"9+ Chinese invoice platforms"枚举 list 到 body（§Architecture 下有地方可塞）。目标 ≤ 110 词
- `SKILL.md` 正文第一段之前插一个 ~3 句的"This skill targets OpenClaw Agents"说明块。解释 `display_name` / `icon` 两个非标 frontmatter 键来自 OpenClaw runtime，标准 Anthropic Skill spec 没有
- `references/platforms.md` 顶部加 Table of Contents block：列所有已支持平台（百望云 bwfp/bwmg/u.baiwang 3 模板、诺诺网、fapiao.com、xforceplus、云票、百旺金穗云、金财数科、克如云、12306、Marriott 等）+ 跳 anchor，便于 Agent 按需跳读
- **不** 拆 Lessons Learned / Workflow / Handling Unknown Platforms。用户已明确温和方案

**Patterns to follow:**
- SKILL.md 现有的 H2/H3 层级和 `##` 风格，新增段落直接融入
- `references/setup.md` 的简洁文件头风格

**Test scenarios:**
- Manual verification（无自动测试文件）：
  - `head -c 200 SKILL.md` 肉眼检查 description 长度 ≤ 110 词
  - 开 `references/platforms.md`，第一屏能看到全部平台 ToC
  - `grep -c "Lessons Learned\|Workflow\|Handling Unknown" SKILL.md` 返回原有值（未被删除）
- Test expectation: none -- 纯文档改动，无行为变更

**Verification:**
- `description` 词数 ≤ 110
- SKILL.md 顶部有 OpenClaw 说明段
- `references/platforms.md` 有 ToC 且所有 anchor 可跳
- `python3 -m pytest tests/ -q` 仍 94 passed（确认文档改动没碰逻辑）

---

- [x] **Unit 2: `v53_pipeline.py` → `postprocess.py` 去版本化重命名**

**Goal:** 纯文件名重构，所有引用点同步更新，测试数保持 94 且全绿。不改任何函数签名或行为。

**Requirements:** R5, R6, R7

**Dependencies:** 无（可与 Unit 1 并行，但建议按顺序做避免同屏 diff 大）

**Files:**
- Rename: `scripts/v53_pipeline.py` → `scripts/postprocess.py`
- Rename: `tests/test_v53_pipeline.py` → `tests/test_postprocess.py`
- Modify: `scripts/download-invoices.py` (pre-rename line 70 import; pre-rename line 451 comment)
- Modify: `scripts/postprocess.py` (ex-`v53_pipeline.py` line 137 self-referential comment — line number preserved through `git mv`)
- Modify: `tests/conftest.py` (pre-rename line 9 comment)
- Modify: `tests/test_postprocess.py` (ex-`test_v53_pipeline.py` line 48 import; lines 1223-1225 dynamic import + monkeypatch target)
- Modify: `SKILL.md` (pre-rename lines 141, 144, 631 — Architecture 图 `v53_pipeline.py` 框 + 相邻 `write_report_v53 (new)` 标签 + Scripts 索引；注意 `write_report_v53` 实际定义在 `scripts/download-invoices.py:453`，若标签位置造成误读可顺手调整为 "postprocess.py" 框外的独立引用)
- Modify: `CLAUDE.md` (pre-rename lines 25, 26, 51, 78, 81, 82 — 6 处命令/路径/讨论)
- Modify: `scripts/core/__init__.py` (字符串 "v5.3 — bedrock_ocr.py → split into llm_client.py + llm_ocr.py + prompts.py" 注释保持不变，那是历史事实；不需要改)

**Approach:**
- `git mv scripts/v53_pipeline.py scripts/postprocess.py && git mv tests/test_v53_pipeline.py tests/test_postprocess.py`（保留 git 历史）
- 用 `sed` 或 Edit 批量替换：
  - `from v53_pipeline import` → `from postprocess import`
  - `import v53_pipeline` → `import postprocess`
  - `monkeypatch.setattr(v53_pipeline, ...)` → `monkeypatch.setattr(postprocess, ...)`
- 文档引用替换：
  - `scripts/v53_pipeline.py` → `scripts/postprocess.py`
  - `tests/test_v53_pipeline.py` → `tests/test_postprocess.py`
  - `v53_pipeline.do_all_matching` → `postprocess.do_all_matching` (CLAUDE.md:81)
  - `test_v53_pipeline.py::TestHotelMatchingTiers` → `test_postprocess.py::TestHotelMatchingTiers` (CLAUDE.md:25)
- 内部 class 名（`TestHotelMatchingTiers / TestProviderMatrix / TestDoctorLLMMatrix`）保持不变——它们按功能命名，健康

**Patterns to follow:**
- 已有 `scripts/invoice_helpers.py`（下载前的纯函数库）与新的 `scripts/postprocess.py`（下载后的处理模块）形成对称命名

**Test scenarios:**
- Happy path: `grep -rn "v53_pipeline" scripts/ tests/ *.md` 应返回 0 条
- Happy path: `python3 -m pytest tests/ -q` 仍 94 passed，零新增、零失败、零跳过变化
- Happy path: `python3 -m pytest tests/test_postprocess.py::TestHotelMatchingTiers -v` 能找到并跑通（确认 CLAUDE.md 里的命令示例跟上了）
- Edge case: `python3 scripts/doctor.py` 退出码仍为 0（确认运行时没残留 `v53_pipeline` 动态加载）

**Verification:**
- `grep -rn "v53_pipeline" .` 只在 docs/brainstorms/ 历史文档里出现（那是历史记录，不动）
- `grep -rn "v53_pipeline" scripts/core/` 返回 0 条
- 测试全绿，94 passed
- **关键 smoke**：`python3 scripts/download-invoices.py --help` 正常 exit 0（这是唯一能触发 `from postprocess import ...` 顶层 import 的 gate——pytest 不会 import `download-invoices.py`，所以纯 `pytest -q` 不捕捉 import 错误）
- `python3 scripts/doctor.py` exit 0（二级 smoke）

---

- [x] **Unit 3: `tests/test_agent_contract.py` — Mock 驱动的 Agent 契约 evals**

**Goal:** 新增集成测试文件，覆盖 R8-R12 的五类 Agent 合约。用 monkeypatch `GmailClient._api_get` + `--llm-provider=none`（或局部 patch LLM）作 mock 面，不依赖任何网络、Gmail token 或 LLM key。

**Requirements:** R8, R9, R10, R11, R12

**Dependencies:** Unit 2（import 路径已改为 `postprocess`）

**Files:**
- Create: `tests/test_agent_contract.py`
- Modify: `CLAUDE.md` (在 "Common commands" 段补一行 `python3 -m pytest tests/test_agent_contract.py -v`)

**Approach:**

`test_agent_contract.py` 按 R8-R12 拆成五个 `class`，每个 class 对应一个合约面：

- `class TestExitCodeContract`（R8）：
  - 构造 mock `GmailClient._api_get` 按场景返回 401 → exit 2
  - 构造环境变量清空 `LLM_PROVIDER=anthropic` + 无 `ANTHROPIC_API_KEY` → exit 3
  - mock `_api_get` 返回 429 状态码 + JSON body → exit 4
  - mock 下载路径产出 0 个成功 PDF 或 1 个 UNPARSED → exit 5
  - 每个断言同时 check stderr 含 `"REMEDIATION:"` 前缀行
- `class TestMatchingTiersContract`（R9）：
  - **mock 策略**：monkeypatch `core.llm_ocr.extract_from_bytes` 返回每个 fixture PDF 对应的受控 OCR dict（不走 `--llm-provider=none`，否则所有 PDF 被 skip，见 Key Technical Decisions）。Gmail 侧 `_api_get` 返回预设邮件 metadata
  - 用 `conftest.py` 现有 4 个真实 PDF fixture + 构造的 OCR return value，构造三组输入恰好命中 P1/P2/P3：
    - P1 用例：OCR 返回 `{remark: "HT-XYZ"}`（发票）+ `{confirmationNo: "HT-XYZ"}`（folio）→ 同 confirmationNo 触发 P1
    - P2 用例：OCR 返回同日期 + 同金额（差值 < 0.01）→ P2 date+amount
    - P3 用例：OCR 返回 remark ≠ confirmationNo 且金额不等 → 仅 date 回退到 P3
  - 断言生成的 `下载报告.md` 里各类匹配 row 数等于预期
  - 断言 P3 row 含 `⚠️` 字符标记（已在 `write_report_v53` 实现）
- `class TestConvergenceHashAndStateMachine`（R10）：
  - R10a: 调用 `postprocess._compute_convergence_hash(items)`，对相同 items 不同顺序验结果相等；hash 长度 == 16 字符
  - R10b 必须用**非空 items**，否则 `write_missing_json` 第一个 `if not items: status = "converged"` 分支会短路让测试通过而不检查状态机。三个 R10b 测试用例：
    - 案例 A：items 非空 + prev_hash == current_hash → `status="converged"` + `recommended_next_action="stop"` （覆盖 hash 匹配分支）
    - 案例 B：items 非空 + iteration == cap + prev_hash != current_hash → `status="max_iterations_reached"` + `recommended_next_action="ask_user"`
    - 案例 C：items 非空 + iteration < cap + prev_hash != current_hash → `status="needs_retry"` + `recommended_next_action="run_supplemental"`
- `class TestMissingJsonSchema`（R11）：
  - 跑最小 pipeline 产出 `missing.json`，读回后断言 `schema_version == "1.0"`
  - 用 pytest 参数化穷举所有 `status` 可达值 ∈ `{converged, needs_retry, max_iterations_reached, user_action_required}`
  - 用 pytest 参数化穷举所有 `recommended_next_action` 可达值 ∈ `{stop, run_supplemental, ask_user}`
  - 对 `items[].type` 同样穷举断言在枚举 `{hotel_folio, hotel_invoice, ridehailing_receipt, ridehailing_invoice, extraction_failed}` 内
- `class TestZipManifestContract`（R12）：
  - 在 tmp_path 里搭出完整 output 目录（pdfs/ + .md + .csv + run.log + step*_*.json + 旧 `发票打包_*.zip`）
  - 调用 `postprocess.zip_output(...)`
  - 读取生成的 zip，断言 `names` 所有后缀 ∈ `{.pdf, .md, .csv}`，不含 `.log / .json`，不含 `发票打包_`
  - 断言至少 1 份 CSV + 1 份 MD

**Execution note:** Execution target: default。集成测试，优先用 `runpy.run_path("scripts/download-invoices.py", run_name="__main__")`（hyphen 使 `run_module` 不可用）配合 `pytest.raises(SystemExit)` + `capsys` 捕 stderr；真正需要 fork 的 R12 端到端 zip 测用 `subprocess.run`。不做 test-first（这是对已存在行为加 safety net）。

**Patterns to follow:**
- 测试 class 命名镜像 `tests/test_postprocess.py` 已有的 `TestPathTraversal / TestZipAtomic` 等风格
- Fixture 复用 `conftest.py` 现有 4 个 PDF fixture
- Mock 风格对齐 `test_postprocess.py:1223-1225` 的 `monkeypatch.setattr(postprocess, ...)` 做法

**Test scenarios:**

*Happy path*
- 退出码 0 场景（全部 PDF 成功下载 + 匹配）→ stderr 不含 `REMEDIATION:`
- P1 匹配：input 发票 `remark` == folio `confirmationNo` → 报告里该行标 `P1 remark`
- P2 匹配：input 发票日期 + 金额（0.01 容差）匹配 folio → 报告里该行标 `P2 date+amount`
- P3 匹配：P1/P2 均失败但同日 → 报告里该行标 `P3 (仅日期)⚠️`
- `_compute_convergence_hash([{type:"hotel_folio", needed_for:"a.pdf"}, {type:"hotel_invoice", needed_for:"b.pdf"}])` == `_compute_convergence_hash([{type:"hotel_invoice", needed_for:"b.pdf"}, {type:"hotel_folio", needed_for:"a.pdf"}])` （顺序无关）

*Edge cases*
- `_compute_convergence_hash([])` 返回合法 16 字符 hash（空 items 不崩）(R10a)
- `iteration == iteration_cap` 且 `items == []` → `status == "converged"`（非 `max_iterations_reached`，确认空 items 短路优先于 cap 检查）(R10b)
- SKILL.md 的 Chinese `发票打包_20260501-120000.zip` 文件名自排除（不嵌套进新 zip）(R12)

*Error paths*
- Gmail 401 Unauthorized → `sys.exit(2)` + stderr 含 `"REMEDIATION: run `python3 scripts/gmail-auth.py`"` 子串
- `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` 空字符串 → `sys.exit(3)` + stderr 含 `REMEDIATION:` 且含对应 provider 的提示
- Gmail 429 Retry-After → `sys.exit(4)` + stderr 含 `"wait 60s"` 子串
- 任一 PDF 被命名为 `UNPARSED_*.pdf` → `sys.exit(5)`，`missing.json` items 里出现 `type: "extraction_failed"`

*Integration*
- 跑完整 `main()`（mock Gmail，`--llm-provider=none`）产出真实 `下载报告.md` + `发票汇总.csv` + `missing.json` + zip，四者齐全且 parse 干净

**Verification:**
- `python3 -m pytest tests/test_agent_contract.py -v` 全绿，≥ 11 条 test
- `python3 -m pytest tests/ -q` 总计 ≥ 105 passed
- 运行时间：每个单测 < 500ms，`test_agent_contract.py` 新增累加 < 3s。全量 `tests/` 总时间 < 8s。不强求单一硬上限；若突破则用 `pytest.mark.slow` 把 `subprocess.run` 型的 R12 端到端测与快速 in-process 测分开

---

- [x] **Unit 4: `evals/seasonal_smoke_test.md` 季度手动 smoke runbook**

**Goal:** 真实 Gmail + 真实 LLM 的人工校验 runbook。季度跑一次，验证真实 API 没变 quirk、LLM 没漂移，基本健康指标未退化。

**Requirements:** R13, R14, R15

**Dependencies:** 无（可与其他 unit 并行）

**Files:**
- Create: `references/seasonal-smoke.md`（与 `references/setup.md`、`references/platforms.md` 同层，沿用现有 runbook 惯例）
- Modify: `CLAUDE.md`（新加一小节指向 `references/seasonal-smoke.md`，说明季度节点）
- 不预创建 `seasonal_results/` 目录——runbook 里写 "记录到你平时存 ops 笔记的地方；若想入 git，建议 `references/seasonal-results/YYYY-QN.md`"。第一次真跑出结果时再 mkdir。

**Approach:**

`references/seasonal-smoke.md` 按如下结构：

1. **When to run**: 每季度末 / v5.X 大重构完成 / 新 LLM provider 上线后
2. **Prerequisites**: `token.json` 有效、`LLM_PROVIDER` 已 export、`/Users/kentpeng/Documents/agent Test/` 存在
3. **Command**: `python3 scripts/download-invoices.py --start <今天-90 天> --end <今天> --output ~/tmp/smoke-$(date +%Y%m%d)`
4. **Expected timing**: 90-120 秒
5. **Pass criteria**（必过 assertions，全是"基本健康"不碰具体匹配数）:
   - exit code ∈ {0, 5}
   - `~/tmp/smoke-*/../发票打包_*.zip` 生成且非空
   - `~/tmp/smoke-*/下载报告.md` 存在且含 "搜到 N 封邮件" 摘要行
   - `~/tmp/smoke-*/missing.json` JSON parse OK 且 `schema_version == "1.0"`
   - **不断言匹配数量**（真实邮箱可能没出差、没打车；匹配正确性由 Unit 3 R9 fixture 保护）
6. **If fails**:
   - exit 2 → 跑 `scripts/gmail-auth.py` 重新授权
   - exit 3 → 检查 LLM provider env vars + `python3 scripts/doctor.py`
   - exit 4 → Gmail 配额未恢复，等 60 秒重试
   - exit 5 + 大量 UNPARSED → LLM provider 可能漂移，手动抽几份 UNPARSED PDF 看
7. **Record location**: 用户自选。建议 `references/seasonal-results/YYYY-QN.md`（第一次跑出结果时再 mkdir），记录跑的日期、实际 exit code、邮件数、配对率、发现的 anomaly

**Patterns to follow:**
- `references/setup.md` 的简洁 runbook 风格
- 文件开头 `date:`、`owner:` frontmatter 可选，不强求

**Test scenarios:**
- Test expectation: none -- 这是人工 runbook，没有可自动化的断言

**Verification:**
- `references/seasonal-smoke.md` 存在且按模板写完
- CLAUDE.md 引用了 `references/seasonal-smoke.md`
- `python3 -m pytest tests/ -q` 仍全绿（这个 unit 不改 test 基础设施）

## System-Wide Impact

- **Interaction graph**: Unit 2 重命名触达 `download-invoices.py` / `tests/conftest.py` / 2 个 doc 文件，但只改 import 字符串和注释。`scripts/postprocess.py` 暴露的函数集不变，所有下游调用点二级不受影响
- **Error propagation**: Unit 3 断言所有错误路径（exit 2/3/4/5）都带 `REMEDIATION:` 前缀行，严格守住 Agent 的错误识别合约。任何未来改错误处理的 PR 如果漏了 `REMEDIATION:` 会被测试捕获
- **State lifecycle risks**: Unit 3 R10 锁住 convergence_hash 纯函数语义。未来如果想改 hash 算法（例如加盐或扩字段），会被测试打断 → 必须同步更新 SKILL.md / CLAUDE.md 的 schema 文档
- **API surface parity**: `missing.json` schema v1.0 被 Unit 3 R11 严格穷举断言，任何 enum 扩容都会触发测试失败 → 强制开发者同步更新 SKILL.md §Loop Playbook 章节
- **Integration coverage**: Unit 3 R12 用 tmp_path 搭真实输出目录调 `zip_output`，覆盖了"多文件类型混杂 + 嵌套旧 zip"场景，比 `test_postprocess.py::TestZipAtomic` 的单文件测试更接近真实交付物
- **Unchanged invariants**:
  - `scripts/core/*` 完全不动（显式快照）
  - `scripts/invoice_helpers.py` 完全不动（v5.2 stable core）
  - 六个 LLM provider 的行为不改
  - 9 个平台解析器的行为不改
  - SKILL.md 里 Lessons Learned / Workflow 10-step / Handling Unknown Platforms 三大段保留原文
  - `learned_exclusions.json` 不动

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Unit 2 重命名漏掉某个动态引用，CI 通过但运行时崩 | Unit 2 Verification 里强制 `python3 scripts/download-invoices.py --help` 和 `scripts/doctor.py` 跑一遍（exit 0 = import 全通）。另外 `grep -rn "v53_pipeline"` 返回 0 条 |
| Unit 3 Mock seam 选错，测试和真实行为偏离 | 选 `_api_get` 是最窄切面，且 `test_postprocess.py:1223-1225` 已经验证过 `monkeypatch.setattr(postprocess, ...)` 工作。Unit 3 的 R12 还会真跑一遍 `zip_output` 到 tmp_path，集成层再过一道 |
| Unit 1 description 精简过头，Agent 触发率下降 | R1 要求保留**所有**触发关键词（发票/invoice/水单/报销/收据）和三条 Use when 场景。只移平台枚举。如果不放心，未来可跑 `run_loop.py` description optimizer 做 A/B 量化 |
| Unit 4 runbook 没人真的跑 | 放到 CLAUDE.md 的"Common commands"段里，加日历提醒。可接受"跑或不跑是用户选择"——runbook 的价值是失败时有 checklist，而非强制执行 |
| 季度 smoke 依赖 `~/.openclaw/credentials/gmail/token.json` 长期有效 | Google refresh token 默认无过期，除非 6 个月无活动或 scope 改变。runbook 已列 exit 2 → `gmail-auth.py` 排查流程 |

## Documentation / Operational Notes

- Unit 1 完成后，SKILL.md / `references/platforms.md` 即"可发布态"
- Unit 2 完成后，所有 `docs/brainstorms/` 里的历史文档仍保留 `v53_pipeline` 字样（那是历史记录，不动）
- Unit 3 完成后，`python3 -m pytest tests/ -q` 总测试数从 94 升至 ≥ 105，CLAUDE.md 里的 Common commands 段补一行 `pytest tests/test_agent_contract.py -v`
- Unit 4 完成后，`evals/` 成为新的顶层目录，记得把 `evals/seasonal_results/` 的 `.gitignore` 规则想清（建议：runbook 本身入 git，`seasonal_results/*.md` 入 git——有价值的历史）

## Sources & References

- **Origin document**: [docs/brainstorms/2026-05-01-skill-compliance-and-evals-requirements.md](../brainstorms/2026-05-01-skill-compliance-and-evals-requirements.md)
- Related code:
  - `scripts/download-invoices.py` (exit codes 84-89, mock seam 111-197, entry 678)
  - `scripts/v53_pipeline.py` (state machine 667-822, naming target)
  - `tests/test_v53_pipeline.py` (naming target + existing test class patterns)
  - `tests/conftest.py` (PDF fixtures)
  - `SKILL.md` (compliance target)
  - `references/platforms.md` (ToC target)
  - `CLAUDE.md` (6-location doc update)
- Related PRs/issues: 无（本地仓库，非协作项目）
- External docs: 无需
