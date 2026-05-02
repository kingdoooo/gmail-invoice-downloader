# CLAUDE.md

Guidance for Claude Code sessions in this repo. `SKILL.md` is the authoritative user + Agent spec — read it before inferring CLI flags, exit codes, or Loop Playbook semantics.

## Agent/Task model policy

All `Agent` tool calls in this repo MUST use Opus 4.7. Pass `model: "opus"` explicitly on every spawn (any `subagent_type`, including `general-purpose`, `Explore`, `Plan`, `code-reviewer`, and all `compound-engineering:*` / `superpowers:*` agents). Do not rely on the agent definition's default or parent inheritance.

## Project

`gmail-invoice-downloader` is an OpenClaw Skill that batch-downloads invoices / receipts / hotel folios / ride-hailing itineraries from Gmail, OCRs them with an LLM, pairs folio↔invoice + itinerary↔invoice, and emits `下载报告.md` + `发票汇总.csv` + `missing.json` + a zip bundle. Current version is declared on `SKILL.md` line 1.

## Common commands

```bash
# End-to-end run (preflight → search → download → OCR → match → 3 deliverables + zip)
python3 scripts/download-invoices.py --start 2026/01/01 --end 2026/05/01 --output ~/invoices/2026-Q1

# Preflight — Gmail creds, pdftotext, LLM provider, cache dir, scripts/core/ package
python3 scripts/doctor.py                 # exit 0 = green, exit 2 = some check failed

# First-time Gmail OAuth (browser flow → writes token.json)
python3 scripts/gmail-auth.py

# Test suite — 195 tests, offline (mocked Gmail + LLM), ~4s
python3 -m pytest tests/ -q
python3 -m pytest tests/test_postprocess.py::TestHotelMatchingTiers -v   # component
python3 -m pytest tests/test_agent_contract.py -v                        # CLI-boundary Agent evals
python3 -m pytest tests/test_postprocess.py -k "p3_date_only" -v         # single test

# Reverse-engineer a new invoice platform URL
python3 scripts/probe-platform.py "https://新平台.com/xxx"

# Agent supplemental loop — triggered by missing.json.recommended_next_action == "run_supplemental"
python3 scripts/download-invoices.py --supplemental --start ... --end ... \
    --output <same-dir> --query "水单 OR folio 希尔顿"
```

## Architecture

10-step pipeline split across two layers. v5.3 grafted an OCR-driven post-processing chain onto the v5.2 Gmail-download chain; v5.4 layered aggregation + business-key dedup on top. Keep the layer boundary in mind — it tells you which file owns which concern.

### Layer 1 — Gmail download (v5.2, stable)

**`scripts/download-invoices.py`** orchestrates the full run. Steps 1–5 are "find + fetch":
1. Build Gmail query (`INVOICE_KEYWORDS` + `learned_exclusions.json`)
2. Paginated `messages.list`
3. `classify_email()` → `doc_type` + `method` decision tree (direct attachment / ZIP / 9 Chinese platform link types / MANUAL / IGNORE)
4. Download (attachment / ZIP extract / link via `curl` with 9 platform-specific URL resolvers)
5. `%PDF` header validation

**`scripts/invoice_helpers.py`** (~1000 lines of pure functions) owns classify, URL extraction, platform short-link resolution (百望云, 诺诺网, fapiao.com, xforceplus, 云票, 百旺金穗云, 金财数科, 克如云, 12306), PDF header validation, date parsing, and the 4-layer hotel-name fallback chain. **Touch this file only to add a new platform extractor or fix the download chain** — it is the stable v5.2 core and its functions are re-exported through the CLI.

**Gmail transport** (`GmailClient._api_get`): raw HTTPS + hand-rolled OAuth token refresh — `google-api-python-client` is deliberately NOT a dependency. Transient network errors (SSL EOF, timeouts) are retried with exponential backoff since commit `67ce58e`; don't collapse retries back into a single attempt (see `docs/solutions/` future entry and memory `project_gmail_transient_retry.md`).

### Layer 2 — LLM post-processing (`scripts/postprocess.py` + `scripts/core/`)

Steps 6–10, added in v5.3 and extended in v5.4:
6. `analyze_pdf_batch` — `ThreadPoolExecutor` runs `core.llm_ocr.extract_from_bytes` + `core.validation.validate_ocr_plausibility` per PDF. Default concurrency is 5; override via `LLM_OCR_CONCURRENCY` env var (v5.5 raised default from 2 to 5). Anthropic tier-1 users may need `LLM_OCR_CONCURRENCY=2`.
7. `rename_by_ocr` — rewrite filename to `{YYYYMMDD}_{vendor}_{category}.pdf` using LLM fields; `sanitize_filename` guards against path traversal.
8. `_dedup_by_ocr_business_key` then `do_all_matching`:
   - **Dedup** (v5.4): collapse duplicate PDFs by OCR business key (invoice number, itinerary file number, or SHA256 for `RIDEHAILING_RECEIPT` where no stable ID exists). Runs before matching and again after supplemental merge.
   - **Hotel matching**: **P1** `remark == confirmationNo`, **P2** `date + amount` with 0.01 tolerance, **P3** same-day v5.2 fallback (`match_type = "date_only (v5.2 fallback)"`, `confidence: "low"`).
   - **Ride-hailing**: by amount with file-number tiebreaker.
9. Emit three deliverables: `下载报告.md` (with aggregation table per v5.4), `发票汇总.csv` (UTF-8 BOM, Excel-compatible, subtotals + grand total), `missing.json` (schema v1.0). Aggregation is computed once in `build_aggregation` and shared by the MD report, the CSV, and the stdout OpenClaw summary so the three stay consistent.
10. Atomic `.tmp → os.replace` zip with allowlist (pdf / md / csv only).

**`scripts/core/`** is a **snapshot** of `~/reimbursement-helper/backend/agent/utils/` (source commit `a0e8515`) augmented with v5.3 modules:
- `llm_client.py` — provider-agnostic adapter over 6 backends (`bedrock` default, `anthropic`, `anthropic-compatible`, `openai`, `openai-compatible`, `none`). Singleton per process. `_reraise_as_llm_error` classifies by exception type first (boto3 `ClientError` codes, Anthropic/OpenAI typed exceptions), falls back to substring matching. `extract_with_retry` does exponential backoff with jitter for `LLMRateLimitError` / `LLMServerError` only; auth and config errors raise immediately.
- `llm_ocr.py` — `extract_from_bytes(pdf_bytes)` with SHA256-indexed on-disk cache at `~/.cache/gmail-invoice-downloader/ocr/`. Same PDF re-run = $0 LLM spend.
- `prompts.py` — OCR prompt template. Changes here must be mirrored upstream.
- `validation.py` — `validate_ocr_plausibility` cross-checks LLM amount against `pdftotext` output (flags `_amountConfidence: "low"` when mismatch >10%) and dates against email `internalDate ± 90 days`.
- `classify.py` / `matching.py` / `location.py` — snapshot. **`classify.py` has one local modification**: `detect_meal_type` + `COFFEE_KEYWORDS` removed (non-deterministic meal assignment is irrelevant here). Don't re-add during future snapshot sync.
- `helpers.py` from upstream is deliberately **NOT copied** (Concur-only).

### Agent-facing runtime contract

The Skill is driven by AI Agents in a loop (see SKILL.md "Agent First-Run Procedure" + "Loop Playbook"). Three contracts must not be casually broken:

1. **Exit codes** (`download-invoices.py` `EXIT_*` constants): 0=ok, 1=unknown+REMEDIATION on stderr, 2=Gmail auth, 3=LLM config, 4=Gmail quota (raises `GmailQuotaError` from `_api_get`), 5=partial (UNPARSED or failed downloads present).
2. **stderr `REMEDIATION:` line** must print on every non-zero exit so Agents can pattern-match and recover.
3. **`missing.json` schema v1.0** — `status ∈ {converged, needs_retry, max_iterations_reached, user_action_required}`, `recommended_next_action ∈ {stop, run_supplemental, ask_user}`. `convergence_hash = sha256(sorted((item.type, item.needed_for)))[:16]`. Agents re-read this file between iterations — do not invent new state machines.

## Editing norms

- **`scripts/core/` is a snapshot, not a workspace.** Before editing, check whether the change belongs upstream in `~/reimbursement-helper/backend/agent/utils/`. If a local-only tweak is genuinely needed (like the `detect_meal_type` removal), update the "Modifications from source" list in `scripts/core/__init__.py` and flag it in SKILL.md's "Lessons Learned".
- **`scripts/invoice_helpers.py` is the v5.2 stable core.** Touch it only to add new Chinese platform URL extractors or fix the download chain. Rename logic and classification live in `postprocess.py` + `scripts/core/classify.py`.
- **v5.2 pdftotext extractors are dead in the LLM path.** `extract_seller_from_pdf` and `extract_hotel_from_folio_pdf` are retained as library functions, but `download-invoices.py` no longer calls them at Step 4 — `rename_by_ocr` in Step 7 overwrites the filename anyway. Don't reintroduce pre-rename pdftotext calls.
- **Silent fallback is a recurring anti-pattern.** When a helper can't produce a clean result, mark it explicitly: `_amountConfidence='low'`, `_dateConfidence='low'`, `_vendorNameInvalid=True`, or route the record to `UNPARSED`. Don't collapse missing values to 0 or a neutral default — `0.0` and `None` must stay distinguishable (`_to_float` returns `None` on missing; call sites use `if x is None`, never `x or default`).
- **Matching logic:** P1/P2 primary in `scripts/core/matching.py`; P3 fallback in `postprocess.do_all_matching`. Multi-folio same-day hotel is the headline v5.2→v5.3 regression risk — any refactor must keep `TestHotelMatchingTiers` green.
- **Dedup (v5.4):** `_dedup_by_ocr_business_key` runs twice for `--supplemental` flow — once in `do_all_matching`, again inside `merge_supplemental_downloads` after merging. `RIDEHAILING_RECEIPT` has no stable business key, so it falls back to SHA256 collapse (fix in commit `79dd8b0`). Don't regress this tie-breaker.
- **Aggregation is single-source.** `build_aggregation` produces the rows; the MD report, CSV, and stdout OpenClaw summary all read from it. If you change category totals or the row schema, update all three consumers (tests in `TestBuildAggregation` + `TestAggregationConsistency` + `TestPrintOpenClawSummary`).
- **LLM provider additions:** subclass `LLMClient` in `llm_client.py`, add an enum branch in `get_client()`, add a doctor check in `doctor.py::_check_llm_config`, add a test class in `test_postprocess.py::TestProviderMatrix` + `TestDoctorLLMMatrix`. The `*-compatible` subclasses are thin factories requiring a `*_BASE_URL` env var; keep that pattern.
- **Never hardcode proxy URLs or API keys in SKILL.md or test files.** Use `<your-proxy-base-url>` / `<your-key>` placeholders. Test fixtures use `dummy-key` to avoid secret-scanner false positives.
- **Version label lives only in `SKILL.md` line 1.** On release, bump that line. Don't add parenthesized version suffixes to section headings, module docstrings, argparse descriptions, or runtime banners. Historical labels ("v5.2 rule", "NEW for v5.3", `"date_only (v5.2 fallback)"`) that describe *when a concept was introduced* are fine — they're lineage, not a current-version claim.

## Dependencies

- **Hard**: Python 3.10+, `boto3>=1.35.17` (Bedrock default), standard library, `curl`, `pdftotext` (poppler-utils) — the last is needed by `validation.py` even in `--no-llm` mode.
- **Optional**: `anthropic>=0.34` (`LLM_PROVIDER=anthropic[-compatible]`), `openai>=1.50` (`LLM_PROVIDER=openai[-compatible]`).
- **Test-only**: `pytest`, `pytest-mock`.

## Test fixtures

Integration tests use real anonymized PDFs from `~/Documents/agent Test/` by default; override with `GMAIL_INVOICE_FIXTURES` env var (see `tests/conftest.py`). Tests are `pytest.skip`ped when the directory is absent so the suite stays portable.

## Docs & references

- `SKILL.md` — authoritative user + Agent spec. Read first.
- `README.md` — public landing page (Chinese); short and points at SKILL.md.
- `references/setup.md` — Gmail API OAuth setup walkthrough.
- `references/platforms.md` — per-platform download details (百望云 3 templates, 诺诺网, fapiao.com, xforceplus, 12306, Marriott, etc.) and the 5-step "add a new platform" playbook.
- `references/seasonal-smoke.md` — quarterly real-Gmail + real-LLM smoke runbook. Complements `tests/test_agent_contract.py` (fully mocked). Run at quarter end, after major refactors, and after LLM provider additions.
- `learned_exclusions.json` — single source of truth for Gmail `-from:` / `-subject:` exclusion rules. User-editable.
- `docs/brainstorms/` — per-feature requirements notes (dated; origin for plans).
- `docs/plans/` — numbered implementation plans (`NNN-<slug>-plan.md`). Historical record of what shipped in each bundle.
- `docs/solutions/` — postmortem / issue writeups (e.g., `2026-05-01-termius-saas-misclassified-as-hotel-folio.md`). Link new entries from `MEMORY.md` when saving project memories.
