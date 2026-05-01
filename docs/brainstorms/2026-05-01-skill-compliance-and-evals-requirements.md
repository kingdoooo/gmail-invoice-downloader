---
date: 2026-05-01
topic: skill-compliance-and-evals
---

# Skill Compliance Cleanup + Agent Contract Evals

## Problem Frame

`gmail-invoice-downloader` v5.3 功能层面已完成：10 步 pipeline 全部接好，6 个 LLM provider 全覆盖，94/94 单元测试通过。但项目要以 OpenClaw Skill 形式被 Agent 调用，还有两类卫生欠债阻碍"可发布"判定：

1. **Skill 元数据 / 导航合规度**：`description` 过长含平台枚举，`references/platforms.md` 686 行无目录（skill-creator 指南推荐 >300 行加 ToC），非标准 frontmatter 键（`display_name`、`icon`）缺乏"OpenClaw Skill 特化"的说明，读者/审核者会疑惑。
2. **Agent 运行时契约缺端到端防线**：exit 码、`REMEDIATION:` stderr、`missing.json` schema v1.0、zip manifest 这四项是 Agent loop 的生死线，但目前只有单元测试覆盖组件，没有"从 CLI 入口一路跑到交付物"的集成断言——未来 refactor 会静默打破 Agent 合同。
3. **命名腐烂的第一个信号**：`scripts/v53_pipeline.py` + `tests/test_v53_pipeline.py` 把版本号写进文件名。v5.3 是它首次落地的版本，但它**职能上**是"下载后的后处理模块"（postprocess），而非某个特定版本的代码。等到 v5.4/v6.0 要么重命名扩散修改，要么留作误导性化石。

**术语约定**：文中"pipeline"一律指"download-invoices.py 串起的 10 步总流程"（包括下载前 + 下载后），"postprocess"指"下载后的 OCR + 命名 + 匹配 + 打包那一段"。重命名后的 `scripts/postprocess.py` 只覆盖后半段，不覆盖 Gmail 搜索/下载。

本次工作的目标：在**不改任何业务逻辑**的前提下，把 Skill 推到"可以交给陌生开发者或 Agent 审核者看也不尴尬"的状态。

## Requirements

**SKILL.md 合规（最小改动版）**

- R1. 精简 `description` 字段到 ~100 词（当前 ~180）。保留所有中/英触发关键词（发票/invoice/水单/报销/收据）和"use when"三条场景，移除 9 个平台的枚举列表（改放正文）。
- R2. 在 SKILL.md 顶部加 2-3 句"This skill targets OpenClaw Agents"说明，解释 `display_name` 和 `icon` 两个非标准 frontmatter 键的由来，避免陌生读者误判为 bug。
- R3. 保留 `Lessons Learned`、`Workflow 10-step`、`Handling Unknown Platforms` 在 SKILL.md 主文（用户已明确选择"温和瘦身"之外的"只动 description + ToC"方案，不挪动大段落）。
- R4. `references/platforms.md` 顶部加一段 Table of Contents，列出所有平台（百望云 3 模板、诺诺网、fapiao.com、xforceplus、云票、百旺金穗云、金财数科、克如云、12306、Marriott 等）+ 跳转 anchor，便于 Agent 按需跳读而非全文加载。

**命名去版本化**

- R5. `scripts/v53_pipeline.py` → `scripts/postprocess.py`。导入点、`download-invoices.py` 的 `from v53_pipeline import ...`、测试文件名、CLAUDE.md 与 SKILL.md 的 Architecture 图、`scripts/core/__init__.py` 的文档注释全部跟进。
- R6. `tests/test_v53_pipeline.py` → `tests/test_postprocess.py`。内部 class 名如 `TestHotelMatchingTiers`、`TestProviderMatrix` 保持不变（它们是按功能命名的，是健康的）。
- R7. 函数签名与行为不变。这是纯文件名重构，不触碰任何 pipeline 逻辑，测试数量应保持 94 条且全绿。

**A 类 evals — Mock 驱动的 Agent 契约自动化测试**

新增 `tests/test_agent_contract.py`，作为已有 94 条单元测试的扩展。使用 pytest-mock monkeypatch 屏蔽 Gmail API 和 LLM 调用，断言以 `download-invoices.py` CLI 为边界的端到端行为：

- R8. **退出码信号**：构造 4 种错误场景，断言正确的 `EXIT_*` 常量和 stderr 必含 `REMEDIATION:` 前缀行：
  - Gmail token 失效 → `exit 2`
  - LLM provider 配置缺失 → `exit 3`
  - Gmail 模拟 429 / quotaExceeded → `exit 4`
  - 有 UNPARSED 或 failed 下载项 → `exit 5`
- R9. **匹配三层可复现**：用 `conftest.py` 里已有的 4 个真实 PDF fixture + mock Gmail 邮件元数据，构造三组输入分别恰好命中 P1 / P2 / P3 路径；断言 `下载报告.md` 中每种匹配类型的行数符合预期，且 P3 行带 `⚠️` 低置信标记。
- R10. **Agent loop 收敛逻辑**：分两部分断言，不做两轮全流程 mock：
  - R10a. **convergence_hash 可复现**：同一组 `items`（同 type + needed_for 集合）任意顺序，`convergence_hash` 恒等（即按 CLAUDE.md 定义的 `sha256(sorted((item.type, item.needed_for)))[:16]`）。
  - R10b. **状态机跳转**：构造 iteration=3 且 items 非空的场景，断言 `status == max_iterations_reached` + `recommended_next_action == ask_user`；构造连续两次 hash 相同的场景，断言 `status == converged` + `recommended_next_action == stop`。不真跑 `--supplemental`，直接调用 `write_missing_json` 的状态决策函数。
- R11. **missing.json schema v1.0 形变防御**：对生成的 `missing.json` 用显式 schema validator 断言：`schema_version == "1.0"`、`status ∈ {converged, needs_retry, max_iterations_reached, user_action_required}`、`recommended_next_action ∈ {stop, run_supplemental, ask_user}`、`items[].type ∈ {hotel_folio, hotel_invoice, ridehailing_receipt, ridehailing_invoice, extraction_failed}`。任何 enum 值新增/改名 → 测试失败，强制文档同步更新。
- R12. **zip manifest allowlist**：构造一份完整输出目录，断言 `发票打包_*.zip` 内只含 `.pdf/.md/.csv` 三种扩展名、至少 1 份 CSV + 1 份 MD、不含 `run.log` / `step*_*.json` / 嵌套旧 zip。

**B 类 evals — 真实 Gmail + 真实 LLM 的季度 smoke test**

新增 `evals/seasonal_smoke_test.md`（runbook 格式，非 pytest），作为季度手动校验：

- R13. runbook 包含：固定时间窗（建议"过去 3 个月"滚动窗口，不是绝对日期，避免文档腐烂）、预期跑完时间（90-120 秒）、必过断言清单：
  - exit code ∈ {0, 5}（5 是可接受的"部分成功"）
  - `发票打包_*.zip` 生成且非空
  - `下载报告.md` 存在且至少含"搜到 N 封邮件"摘要行（即 pipeline 成功跑到 Step 9）
  - `missing.json` 可被 JSON parse 且 `schema_version == "1.0"`
  - **不断言匹配数量**。真实邮箱可能刚好这 3 个月没出差、没打车，强行断言 "P1/P2/P3 至少 1 行" 会造成合法季度误报。匹配正确性由 A 类 R9 用固定 fixture 保护。
- R14. runbook 记录每次运行结果的位置（建议 `evals/seasonal_results/YYYY-QN.md`），含跑的命令、实际 exit 码、邮件数、配对率、发现的 anomaly。后续可以拿这份结果作为经验库。
- R15. 不做 CI 集成。季度 smoke 只在人工触发（如重大重构后、每季度发报销前）跑一次。

## Success Criteria

- **测试面积**：完成 R5-R12 后，pytest 条数从 94 增至 ≥ 105（至少 11 条 A 类契约 eval）。全部 < 5s。
- **SKILL.md 质量**：`description` 从 ~180 词降到 ≤ 110 词，三条触发场景保留。`references/platforms.md` 第一屏能看到全平台跳转 ToC。
- **命名健康度**：`grep -rn "v53_pipeline" scripts/ tests/ *.md` 返回 0。SKILL.md 的 Architecture 图刷新为 `postprocess.py`。
- **可发布感**：新开发者从零 clone 后 30 分钟内能：(a) 读 SKILL.md 知道这是什么；(b) 跑通测试；(c) 读懂 Agent loop 合约；(d) 知道怎么在 v5.4 加 feature 时不破坏合约（因为 A 类 evals 会保护）。

## Scope Boundaries

- **不新增业务功能**。不加 Ollama provider、不加新平台解析器、不改匹配算法、不改 OCR prompt。如果这些在 review 过程里浮现成必要，另起一轮 brainstorm。**注意**：新增 `tests/test_agent_contract.py` 属于"新增测试基础设施"而非"新增业务功能"——它不改变 `download-invoices.py` 的任何行为，只是在已有 CLI 边界外加断言。
- **不 refactor `scripts/invoice_helpers.py` 或 `scripts/core/*.py`**。后者是显式快照，改动成本在别处。
- **不改 Agent 自身逻辑**。这个 Skill 是被 Agent 调用的工具，Agent 侧代码不归本 repo。
- **不做"完整 Lessons Learned 外移"**。用户已明确选择温和方案，不强拆。
- **不做 description trigger 优化器跑 `run_loop.py`**。等到 A/B 需求真实出现再做。
- **不写 CI 配置**。测试本地能跑、season smoke 手动跑即可；GitHub Actions/GitLab CI 等工作流属于下一轮 scope。

## Key Decisions

- **Mock + 真实两层并存**：Mock 做 CI/refactor 保护（覆盖错误路径、enum 合约、zip manifest 这类"真实数据很难触发"的场景），真实做季度抽检（验证真实 Gmail 的新 quirk 和 LLM 漂移）。**理由**：真实 Gmail 数据内容会变，无法当 regression 基线；mock 无法发现真实 API 新 quirk。两者互补，各取所长。
- **命名选 `postprocess.py` 而非 `pipeline.py`**：与已有 `invoice_helpers.py`（下载前）形成对称命名，职能边界清晰——helpers 找+下载，postprocess OCR+命名+匹配+打包。"pipeline" 太宽泛，会和 `core/` 里的 OCR 模块混淆。
- **SKILL.md 主体保持不动**：用户明确了温和瘦身偏好。skill-creator 的 500 行是 guideline 不是硬指标，644 行用户可接受；改 description 和加 ToC 已经把最大触发收益拿到。

## Dependencies / Assumptions

- **真实 Gmail 季度 smoke 依赖**：`~/.openclaw/credentials/gmail/token.json` 长期有效（Google refresh token 默认无过期，除非 scope 改变或 6 个月无活动）。B 类 eval runbook 里需要注明"token 失效时先跑 scripts/gmail-auth.py"。
- **真实 PDF fixtures 已存在**：`~/Documents/agent Test/` 下的 ≥ 6 份 PDF 是 A 类 eval 的匹配场景输入来源（可用 `GMAIL_INVOICE_FIXTURES` env var 覆盖到别的目录）。若未来迁移开发机，该路径需要同步。`conftest.py` 已用 `pytest.skip` 处理缺失情况，所以 fixture 缺失不会导致假失败，但会掉测试覆盖——这是目前已接受的状态。
- **假设 mock Gmail 的响应 JSON 能精准复现真实 API**：messages.list 分页、messages.get 全文、attachments.get base64 三个端点的响应 schema 稳定，Google 不会在近期做破坏性改动。这是 `GmailClient._api_get` 手写而非用 google-api-python-client 带来的风险——mock 的准确度靠手写 JSON 保证。

## Outstanding Questions

### Resolve Before Planning

无。所有产品决策已对齐。

### Deferred to Planning

- [Affects R5][Technical] `download-invoices.py` 的 `from v53_pipeline import ...` 和 `from scripts.v53_pipeline import ...` 两种写法都需要 grep 确认覆盖全；有没有 `importlib.import_module("v53_pipeline")` 式的动态导入。
- [Affects R8-R11][Technical] A 类 eval 的 mock 层应该放在哪——monkeypatch `GmailClient._api_get`（最接近边界），还是 monkeypatch `search/get_message/get_attachment_bytes`（更易读）？需要规划期看代码决定。
- [Affects R12][Needs research] zip 在不同文件系统（APFS vs ext4 vs NTFS）下 `os.replace` 的原子性保证是否都成立？本地 Mac 环境够用，但真部署到 Linux 容器有没有额外测试需求？
- [Affects R13][Needs research] Gmail token 真实过期 / refresh failure 的季度 smoke 失败应该用什么样的错误消息提示用户？runbook 里要不要给出标准排查流程？

## Next Steps

→ `/ce:plan` for structured implementation planning
