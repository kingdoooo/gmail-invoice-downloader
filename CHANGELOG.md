# Changelog

本项目的版本声明仅在 `SKILL.md` 第 1 行（`# Gmail Invoice Downloader (vX.Y)`）。下方记录每个版本的发布说明；最新版本在最上面。

## v5.7.2 — output_dir preflight + Agent 新目录指引 (2026-05-03)

**动机**：v5.7.1 解决了 zip **包含** 跨批次残留的问题（白名单过滤），但 `pdfs/` 目录本身仍会因反复复用 `--output` 而累积文件——用户开 Finder 看到满目"(1)""(2)"后缀仍然困惑。正确的解法是从**上游**阻止这种复用：Agent 每次新跑构造新目录。

- **feat(cli):** `main()` 在 Step 1 之前做 output_dir preflight。新助手 `_inspect_existing_output_dir(output_dir)` 检测 `pdfs/` 下非 IGNORED_ 的 PDF 数 + `step4_downloaded.json` / `missing.json` / `下载报告.md` / `发票汇总.csv` 的存在。非 supplemental 运行 + 非空目录 → 打印 `⚠️ output_dir 不是空的：...` 一行（stdout + run.log），提示用户首次跑应该用新目录。`--supplemental` 模式下静默（复用目录是设计行为）。**纯提示，不阻断**——Agent headless 场景不能交互；兜底给运行看，决策层在 SKILL.md。
- **docs(SKILL):** `§ Agent First-Run Procedure § 2. 选 output 目录` 重写为强指令："初次跑一个日期区间 MUST 用全新目录"。给了命名约定（`~/invoices/{YYYY-QN}` 首选 + `-2/-3` 碰撞自增）+ Agent 伪代码 + 补搜例外（`--supplemental` MUST 复用旧目录）。运行期兜底提示跟这段文档说明对齐。
- **test(postprocess):** 新增 `TestOutputDirPreflight`（6 个测试）：empty / nonexistent / prior pdfs detected / IGNORED_ 不计 / step4 state detected / report csv 信号。
- **test(suite):** 290 passed（v5.7.1 为 284，+6 新测试）。

### 不受影响

- `_inspect_existing_output_dir` 是纯函数，不 mutate output_dir；实际目录创建仍在 `os.makedirs(pdfs_dir, exist_ok=True)` 处。
- `missing.json` schema / `CHAT_MESSAGE_*` / `CHAT_ATTACHMENTS` / zip 白名单语义全部不变。既有调用方 / Agent 契约测试 100% 兼容。
- 即使用户无视提示继续跑，v5.7.1 zip 白名单仍保证交付 zip 只有本 run 内容。v5.7.2 纯粹是 UX 层提示——消除"先跑完才发现污染"的尴尬。

### 升级备注

- **已经累积的残留目录**：不会被自动清理。用户可以 `rm -rf ~/invoices/2025-Q2/pdfs/*.pdf` 手动清空，或者忽略——下次 Agent 按 SKILL.md 新指引会建 `~/invoices/2025-Q2-2/` 新目录，老目录保留做 forensics。
- **自定义 Agent/脚本**：如果有外部工具调用 `download-invoices.py`，**建议也遵循 SKILL.md 新目录约定**以获得最干净的 zip 输出。运行期兜底提示保证即使遗忘也不会出错。

## v5.7.1 — zip 跨批次残留修复 (2026-05-03)

**问题**：用户报告 `pdfs/` 里几乎每张票都有一份 `(1)` 后缀的"重复"；zip 包里也有。SHA 对比显示 182 份文件中 89 份与本 run 的 step4 records byte-identical —— 这些是**前几次 run 在同一个 output_dir 下累积的残留文件**。`zip_output` 之前直接 `os.walk(output_dir)` 把所有 `.pdf` 都打包，不区分本 run 产物和跨批次残留，导致 zip 膨胀 + 用户体验困惑。dedup (`_dedup_by_ocr_business_key`) 没有 bug —— 它按设计只在单 run 的 `downloaded` list 内工作，磁盘上的前 run 残留本来就不在它的 scope 里。

- **fix(postprocess):** `zip_output` 新增 keyword `include_pdf_paths: set[str] | None = None`。传入 set 时，只有绝对路径在白名单里的 PDF 会进 zip；其他（跨批次残留）留在磁盘上供审计。`None` 保持原有全扫描行为（向后兼容既有 contract test 和第三方工具）。`IGNORED_` / `发票打包_` 前缀排除仍然独立生效（纵深防御）。CSV / MD **不**被白名单管辖——它们是本 run 现写的产物，白名单化它们会逼 caller 复制 writer 路径毫无意义。
- **feat(cli):** `main()` 和 `_run_postprocess_only` 现在从 `downloaded_all` / `records` 收集本 run 的 PDF path set 传给 `zip_output(..., include_pdf_paths=...)`。同时新增 `_count_leftover_pdfs` 辅助函数，在 zip 之前输出 `ℹ️ 跨批次残留：pdfs/ 目录里有 N 份 PDF 来自之前的批次，未打包进本次 zip（仍保留在磁盘上供审计）...` 一行，让用户知情并给清理建议。
- **test(postprocess):** `TestZipAtomic` 新增 4 个测试：白名单生效排除跨 run 残留 / 白名单为 None 保留 legacy 全扫描 / 白名单仍尊重 `IGNORED_` 前缀过滤 / 白名单为空 set 时仅打包 CSV+MD。
- **test(suite):** 284 passed（v5.7 为 280，+4 新测试）。

### 不受影响

- `missing.json` schema 仍为 `"1.0"`；`CHAT_MESSAGE_*` / `CHAT_ATTACHMENTS` sentinel 契约不变；`_dedup_by_ocr_business_key` 逻辑不变（单 run scope 去重仍按 Pass 1 业务键 + Pass 2 SHA256 正常工作）；`rename_by_ocr` / `build_aggregation` / CATEGORY 登记不变。
- 既有 `zip_output` 调用者（`TestZipManifestContract` 等）传 `dest_dir` 而不传 `include_pdf_paths`，走 `None`-legacy 分支，行为与 v5.7 完全一致。

### 升级备注

- **已存在的跨批次残留不会自动清理**。用户如果想让 `pdfs/` 也干净，提示行里已经给了命令：`rm {pdfs_dir}/*.pdf` 然后重跑。zip 本身从 v5.7.1 开始保证干净。
- **未来设计方向**：如果需要"run 开始时自动清空 pdfs/" 的激进行为，单独开议题讨论（涉及到用户手动放 PDF 进 pdfs/ 等副作用场景）。

## v5.7 — IGNORED 白名单分类 + 非报销票据过滤（双层防御） (2026-05-02)

**动机**：2025Q4 smoke 里 Termius 订阅发票（Stripe 模板 + 英文 docType "Invoice"）被 `is_hotel_folio_by_doctype` 的 "Statement"/"Invoice" 关键字命中，滑入 HOTEL_FOLIO 管道永不匹配，在 `missing.json` 里变成永远修不好的 `hotel_invoice` 缺口；`convergence_hash` 兜底判为 `converged` 是**假收敛**。下一个英文 SaaS 供应商必然再踩同一坑。v5.7 用双层防御从根上切断这条路径。

- **feat(prompts):** `scripts/core/prompts.py` 新增 "Hotel-specific field conditional extraction" 规则（Unit 0）：`arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` 要求 PDF 原文含明确酒店标签（`Arrival / Departure / Check-in / Check-out / 入住日期 / 抵店日期 / 离店日期 / 退房日期 / 入离日期 / Room No. / 房号` 等），否则保持 null。堵 `is_hotel_folio_by_fields` 3-choose-2 路径被订阅区间（"Nov 12, 2025 – Nov 12, 2026"）、账单周期、开票到期日误触发。
- **feat(classify):** `scripts/core/classify.py::classify_invoice` 双改（Unit 1）：fallthrough 出口从 `'UNKNOWN'` 改为 `'IGNORED'`；`is_hotel_folio_by_doctype` 命中后 narrow gate 要求 **≥2 of {hotelName, confirmationNo, internalCodes, roomNumber}** — 故意**不含** `balance`（Stripe / Termius "Amount due / Amount paid" 语义冲突）、**不含** `arrivalDate / departureDate`（由 prompt 层约束）。`is_hotel_folio_by_fields` 3-choose-2 强特征路径不动。
- **feat(download):** `classify_email` 返回 dict 新增 `sender_email`（bare lowercase）；三个 `download_*` 函数构造的 record 传递 `sender` + `sender_email`（Unit 2），供下游 IGNORED 重命名 + CTA 消费。
- **feat(postprocess):** `rename_by_ocr` 新增第三条 IGNORED 分支（Unit 3）：`IGNORED_{sender_short}_{原名}.pdf`，`sender_short` 从邮件 `from` 域名取（`billing@termius.com` → `termius`，上限 20 字符），空时 `unknown`。`os.rename` OSError 时递归降级到 UNPARSED 分支，保持三路交付自洽。`CATEGORY_LABELS["IGNORED"] = "已忽略"`（`CATEGORY_ORDER` 故意不扩展，避免扰动 UNPARSED=99 不变量）。`main()` 和 `_run_postprocess_only` 在 `valid_records` 构造后切分 `ignored_records` / `reimbursable_records`，matching 和 aggregation 只看可报销记录。`build_aggregation` 入口新增防御性断言防止 IGNORED 泄漏。
- **feat(report):** 下载报告末尾新增 `## 📭 已忽略的非报销票据 (N)` 节（Unit 4），逐行列出 sender + 金额；附加 `learned_exclusions.json` CTA 代码块，按 domain 聚合 `-from:xxx.com  # 已过滤 N 次`，sorted alphabetically。`print_openclaw_summary` 追加 `📭 已忽略 N 张非报销票据` 行（N=0 省略）。`zip_output` 排除 `IGNORED_*.pdf`（`UNPARSED_*.pdf` 仍然打包）。金额字段经 `float()` coerce，LLM 返回字符串（"120.00"）或 garbage 不会崩溃，不可识别值降级为"金额未识别"。
- **docs(SKILL):** 新增 `## Lessons Learned § v5.7` 条目（Unit 5）登记双层防御决策 + 明确「不要再做」清单 + 验证工具指引，防止下次 snapshot 同步 `~/reimbursement-helper/backend/agent/utils/` 时无声覆盖本地修改。
- **chore(dev):** `scripts/dev/replay_classify.py` committed 作为回归工具——扫 `~/.cache/gmail-invoice-downloader/ocr/*.json` 跑旧 / 新 classify 差集，附带 sha256→pdf_path 反查（扫 `~/invoices/**/pdfs/*.pdf`）。差集含合法水单 → 固化到 `tests/fixtures/ocr/legitimate_folios/*.json` + 扩 `TestHotelFolioNarrowGate` 锁定。差集含 SaaS → 预期。
- **test(suite):** 280 passed（v5.6 为 261，+19 新测试）：`TestPromptContract` / `TestClassifyIgnored` / `TestHotelFolioNarrowGate` / `TestSenderEmailPassthrough` / `TestRenameIgnoredBranch`（含 OSError 降级）/ `TestIgnoredCtaRendering`（含字符串金额崩溃回归）。

### Agent 合约不变

- `missing.json` schema 保持 `"1.0"`；IGNORED 记录**不**进 `items[]`、**不**影响 `convergence_hash` / `status` / `recommended_next_action`。
- **显式拒绝** 在 `missing.json` 加 `ignored_count` 顶层字段——对无 Agent 消费者的字段做契约迁移属 YAGNI。未来若真有 supervisor agent 需要可观察性，向后兼容的 optional 字段随时可加，非 blocking。
- `ALLOWED_ITEM_TYPES`、`CATEGORY_LABELS["UNPARSED"]`、`CATEGORY_ORDER["UNPARSED"]==99`、`is_hotel_folio_by_fields` 3-choose-2 路径全部不变。
- `CHAT_MESSAGE_START` / `CHAT_MESSAGE_END` / `CHAT_ATTACHMENTS:` 三个 sentinel 格式和顺序不变；OpenClaw summary 新增的 `📭 已忽略` 行位于 `CHAT_MESSAGE_START` → `CHAT_MESSAGE_END` 之间，属于用户可见摘要的一部分。

### 升级备注

- **OCR 缓存对 Unit 0 规则是惰性生效的。** 历史 cache 是用 v5.5 prompt 抽取的，可能已把 SaaS 订阅区间写进了 `arrivalDate / departureDate`。这类历史记录在 v5.7 下走 `is_hotel_folio_by_fields` 3-choose-2 路径仍会命中 HOTEL_FOLIO，narrow gate 兜底不到。想让 prompt 规则对历史数据也生效：

      rm -rf ~/.cache/gmail-invoice-downloader/ocr

  不清缓存也安全——v5.7 只会「增量生效」（新 OCR 的 SaaS 会正确落到 IGNORED，历史缓存的 SaaS 可能仍被误判 HOTEL_FOLIO，但 convergence_hash 仍按原来的机制收敛）。

- **上游同步（reimbursement-helper）**：`scripts/core/prompts.py` 的 v5.7 rule 和 `scripts/core/classify.py` 的 fallthrough + narrow gate 是本地 fork。下游 reimbursement-helper 消费经 IGNORED 过滤的 clean 发票，本改动对下游 **pure win**。未来 snapshot sync 把两条改动一起推上游即可。每次 sync 前参考 `scripts/core/__init__.py § Modifications from source` 清单 + 本条 Lessons Learned（SKILL.md line 675+），不要覆盖本地修改。

- **测试固化**：如果 `scripts/dev/replay_classify.py` 在差集里报了合法水单 → IGNORED 的案例，脱敏后固化到 `tests/fixtures/ocr/legitimate_folios/{sample}.json`，扩 `TestHotelFolioNarrowGate` 锁定样本，并回头评估 narrow gate 阈值是否需要放宽。差集全是 SaaS（Termius / Anthropic / OpenRouter / ...）→ 预期效果。

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
