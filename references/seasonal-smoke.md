# Quarterly Smoke Test Runbook

Real Gmail + real LLM smoke test.  Complements `tests/test_agent_contract.py`
(which runs fully mocked on every CI pass): this runbook catches the
**real-world drift** that mocks can't — new Gmail API quirks, LLM provider
output drift, platform format changes.

## When to run

- End of every quarter (2026-Q1 wrap-up, 2026-Q2 wrap-up, ...).
- After every major refactor that touches the download pipeline, the LLM
  provider, or `scripts/core/`.
- After adding a new LLM provider backend.
- After importing a fresh `scripts/core/` snapshot from
  `~/reimbursement-helper/backend/agent/utils/`.

## Prerequisites

1. Gmail OAuth token is valid (`~/.openclaw/credentials/gmail/token.json`
   exists and the refresh token hasn't been revoked).  If unsure, run
   `python3 scripts/doctor.py` first.
2. At least one LLM provider is configured.  `LLM_PROVIDER=bedrock`
   (default) works with IAM roles; otherwise export an explicit
   `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / LiteLLM `*_BASE_URL` + key
   pair.  See SKILL.md § "LLM Provider".
3. `pdftotext` installed (used by the hallucination-detection
   cross-check in `validation.py`, even when LLM is live).

## Command

```bash
# Use a rolling 3-month window so the runbook doesn't rot with absolute
# dates.  ~/tmp is throwaway — pick a fresh folder each quarter.
python3 scripts/download-invoices.py \
    --start "$(date -v-90d '+%Y/%m/%d')" \
    --end   "$(date '+%Y/%m/%d')" \
    --output "$HOME/tmp/smoke-$(date +%Y%m%d)"
```

(Linux: replace `date -v-90d` with `date --date='90 days ago'`.)

## Expected timing

- First run: 90–180 seconds (OCR cache is cold).
- Repeat run on same date range: 30–60 seconds (cache at
  `~/.cache/gmail-invoice-downloader/ocr/` is warm).
- Anything >5 minutes is abnormal — check `run.log` for retry storms
  or stuck LLM calls.

## Pass criteria

These are the **minimum health checks** — they do NOT assert a specific
match count, because your real inbox may legitimately have zero hotel
stays or zero ride-hailing trips in the quarter.  Match correctness is
protected by `tests/test_agent_contract.py::TestMatchingTiersContract`,
not here.

1. **Exit code ∈ {0, 5}.**  0 means full success, 5 means partial (some
   PDFs became UNPARSED); either is a successful smoke.  Any other code
   is a failure — see § If fails.
2. **Zip is produced.**  `~/tmp/smoke-<stamp>/../发票打包_*.zip` exists
   and is non-empty.
3. **Report markdown is produced.**  `~/tmp/smoke-<stamp>/下载报告.md`
   exists and contains a "搜到 N 封邮件" summary line (confirms pipeline
   reached Step 9).
4. **missing.json parses and has the declared schema version.**
   ```bash
   python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["schema_version"]=="1.0", d; print("ok")' \
       "$HOME/tmp/smoke-$(date +%Y%m%d)/missing.json"
   ```

## If fails

| Exit code | Likely cause | First thing to try |
|-----------|--------------|--------------------|
| 2 (auth)  | Token expired or revoked | `python3 scripts/gmail-auth.py` to re-authorize |
| 3 (LLM config) | Missing / malformed credentials for the active provider | Check env vars; `python3 scripts/doctor.py` surfaces the exact gap |
| 4 (quota) | Gmail API quota momentarily exceeded | Wait 60 seconds, retry; if recurring, reduce `--max-results` |
| 5 + many UNPARSED | LLM provider drift (rate limits, output format change, prompt needs refresh) | Pick 2–3 UNPARSED PDFs from `pdfs/` and inspect them manually; if legitimate invoices, the prompt in `scripts/core/prompts.py` may need updating |
| 1 (unknown) | Uncaught exception | Read `run.log` tail — full traceback is there |

## Record the result

When the smoke passes, log the outcome so future-you (or your team) can
spot trends.  Suggested location: `references/seasonal-results/YYYY-QN.md`
(create the directory the first time you save a result — no pre-built
scaffolding is shipped).

Template:

```markdown
# 2026-Q2 smoke (2026-06-30)

- Command: (as above)
- Exit code: 0
- Duration: 94s
- Emails matched: 37
- PDFs downloaded: 42 (3 from ZIP extraction)
- Hotel pairs matched: 4 (3 P1, 1 P2, 0 P3)
- Ride-hailing pairs matched: 6
- UNPARSED: 0
- Anomalies: none

LLM provider: bedrock / us-east-1 / claude-sonnet-4-5
```

Over a year of quarters, this gives you a real regression corpus that
no mock can provide.

## Explicit non-scope

- **Not a CI job.**  This is a human-triggered runbook.  Automating it
  would either leak a Gmail token into CI (bad) or require a test
  account whose contents would need maintenance (worse).
- **Not a strict regression harness.**  Real inboxes change.  A
  quarter with zero hotel stays should not cause a failure — the
  regression safety net lives in `tests/test_agent_contract.py`.
- **Not an LLM cost-control mechanism.**  Each smoke run costs a few
  cents in LLM calls.  The OCR cache amortizes this across repeat
  runs on the same date range.
