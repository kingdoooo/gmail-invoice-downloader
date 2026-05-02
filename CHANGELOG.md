# Changelog

本项目的版本声明仅在 `SKILL.md` 第 1 行（`# Gmail Invoice Downloader (vX.Y)`）。下方记录每个版本的发布说明；最新版本在最上面。

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
