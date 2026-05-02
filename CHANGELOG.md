# Changelog

本项目的版本声明仅在 `SKILL.md` 第 1 行（`# Gmail Invoice Downloader (vX.Y)`）。下方记录每个版本的发布说明；最新版本在最上面。

## v5.6 — Agent-delivered chat attachments + 可信消息原文 (2026-05-02)

- **feat(postprocess):** `print_openclaw_summary` 现在在 stdout 每次产出 `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END` 两条裸锚点行，包住给用户看的完整中文摘要（包括结尾的 "💡 发现不该报销的…" 提示）。Agent 必须原文转发两者之间的内容 — 不增、不删、不翻译、不挑重点。修复 v5.5 前"结尾提示常被 Agent 当装饰文字砍掉"的问题。
- **feat(postprocess):** R16a 非空路径在 `CHAT_MESSAGE_END` 之后额外输出单行 JSON `CHAT_ATTACHMENTS: {...}`，声明本次产出的三份交付物（zip + MD + CSV）。Agent 按 `files[]` 顺序（报销包 → 报告 → 明细）用当前 channel 原生消息工具（飞书 / Slack / Discord / WhatsApp / iMessage）作为附件上传到当前会话。Skill 本身 channel-agnostic，不依赖任何特定 IM SDK。
- **feat(postprocess):** `zip_output` 失败时（DEC-6 降级）`CHAT_ATTACHMENTS` JSON 会跳过 zip 条目，但仍声明 MD + CSV — 附件发送不会因打包失败整体塌陷。
- **docs(SKILL):** 新增 `## Presenting Results to the User` 顶层章节，文档化两条 sentinel 契约 + Agent Playbook（先转发摘要后上传附件；单个上传失败补警告行不中断；channel 不支持附件时降级为路径文本）。
- **test(agent-contract):** 新增 R18 契约测试类 `TestChatSentinelContract`（11 个测试），锁定锚点格式、顺序不变量（START < END < ATTACHMENTS）、JSON schema（`{files: [{path, caption}]}`）、caption 合法取值（报销包 / 报告 / 明细）、zip 失败降级、单行 payload 不变量、路径不做 shell quote。镜像 R8 `REMEDIATION:` 契约的结构。
- **test(postprocess):** `TestPrintOpenClawSummary` 新增 10 个测试覆盖 R16a / R16b / zip 失败 / ValueError 各路径下的锚点行为。

### 不变量

- 每条 sentinel 在单次 Skill 运行中各自至多出现一次。
- 严格顺序：`CHAT_MESSAGE_START` → `CHAT_MESSAGE_END` → `CHAT_ATTACHMENTS:`（后两者可缺席）。
- `CHAT_ATTACHMENTS:` 存在 ⇒ `CHAT_MESSAGE_START` / `END` 一定存在。
- R16b 空结果路径：只发两条锚点，不发 attachments。
- 早期错误路径（Gmail auth / LLM 配置）：两条 sentinel 都不发，Agent 走 `REMEDIATION:` 路径。

### 升级备注

- **无 schema 破坏性改动：** `missing.json` v1.0 schema、`REMEDIATION:` stderr 行、exit codes 的既有语义全部保持不变。`print_openclaw_summary` 签名未改。
- **现有 OpenClaw 部署：** 老版 Agent（不识别 sentinel）运行本版 Skill 不会崩 — 锚点和 JSON 行会作为普通输出出现在 stdout 里，看着像多了两行"杂讯"，但不影响 Skill 本身的功能。要获得"附件直接出现在聊天里"的体验，需要让 Agent 按本版 SKILL.md 新增章节的 Playbook 消费 sentinel。

## v5.5 — Agent-ready polish + OCR 校准 (2026-05-02)

- **fix(nuonuo):** 短链提取正则排除 `/allow` QR 图片 URL + `/invoice /scan /sapi /scan-invoice`。修复「雨花台区程旭餐饮店」等诺诺网发票「failed to resolve」假阳性。
- **feat(agent):** exit 5 新增 auto-probe 循环。`run.log` 中 `failed to resolve …` 条目依次过 `probe-platform.py` 诊断 → 成功则 curl + `--postprocess-only` 重跑；失败则调 `scripts/record-unknown-platform.py` 把 `type=unknown_platform` 写回 `missing.json.items[]`，`status` 变 `user_action_required`。详见 SKILL.md「Failed-link auto-probe」子节。
- **feat(cli):** 新增 `--postprocess-only` flag — 跳过 Gmail 搜索 / 下载（Step 1-5），直接从已有 `pdfs/` 重跑 Step 6-10（OCR、匹配、三份交付物、zip）。适合 auto-probe 补救或人工补下载后的对账。
- **feat(report):** P3 同日兜底匹配行现在独立成「P3 同日兜底匹配（低可信度）」子节，显示 `入住 / 退房 (OCR)` 列。解决审核时看到文件名日期不一致以为是错配的误解。
- **feat(rename):** `HOTEL_FOLIO` 文件名优先用 OCR `departureDate`（退房日）而非邮件 `internalDate`。同日入住的水单和发票文件名从此对齐。
- **feat(missing):** `missing.json` 新增 `out_of_range_items[]` 顶层数组（对 v1.0 schema 可选扩展，向后兼容）。业务日期（folio `departureDate` / invoice `transactionDate`）在 `--start`/`--end` 范围外的条目落入此数组，不参与 `convergence_hash` / `status` 计算，不触发 `run_supplemental`。跨季度项目不再污染季度批次。
- **perf(ocr):** `analyze_pdf_batch` 默认并发 2 → 5，支持 `LLM_OCR_CONCURRENCY` 环境变量覆盖（docstring 原本承诺但未连线）。非正整数抛 `LLMConfigError` (exit 3)，不静默回退。`doctor` 新增 `_check_ocr_concurrency` 检查。Anthropic tier-1 用户：`export LLM_OCR_CONCURRENCY=2` 保持原行为。
- **feat(ocr):** Bedrock 默认模型 Opus 4.7 → Sonnet 4.6 (`global.anthropic.claude-sonnet-4-6`)。95-PDF benchmark 显示 Sonnet 4.6 快 42%、便宜 ~5×、金额抽取零漂移。保留 `BEDROCK_MODEL_ID=global.anthropic.claude-opus-4-7` 覆盖。
- **feat(prompt):** OCR prompt 新增两条规则澄清 `transactionDate` 的歧义：
  - 酒店水单 `transactionDate == departureDate`（退房日），退房日不可识别时填 null。
  - 网约车行程单新增 `applicationDate` 字段（申请日期），`transactionDate == applicationDate`，申请日期不可识别时两者都填 null。
  解决了同一张 PDF 在 Opus/Sonnet 之间日期取舍不一致的 8/95 案例。
- **chore(docs):** 版本号仅在 `SKILL.md` line 1 声明；`CLAUDE.md`、`download-invoices.py` 不再重复宣称版本。CLAUDE.md 新增编辑规范条款。

### 升级备注

- **OCR 缓存:** 本版 prompt 有改动。v5.4 已缓存的 PDF 会继续返回老版字段（正确，只是 `transactionDate` 对水单/行程单可能还没命中新规则）。想让已缓存的 PDF 吐出新字段：

      rm -rf ~/.cache/gmail-invoice-downloader/ocr

  不清缓存也安全，部分水单 P2 匹配可能暂时回退到 P3 低可信度直到 OCR 重跑。
- **文件名:** 老批次里曾命名为 `20250607_…` 的水单在 v5.5 重跑后会变成 `20250508_…`。按文件名日期排序的目录列表会重排。
- **上游同步:** `scripts/core/prompts.py` 的 prompt 改动需要同步回 `~/reimbursement-helper/backend/agent/utils/prompts.py`（见 `scripts/core/__init__.py` snapshot 规则）。
- **Anthropic tier-1 用户:** 默认并发从 2 → 5。如果遇到 rate limit，`export LLM_OCR_CONCURRENCY=2`。
