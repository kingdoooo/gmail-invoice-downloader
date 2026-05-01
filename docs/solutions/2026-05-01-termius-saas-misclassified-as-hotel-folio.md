---
date: 2026-05-01
topic: llm-classifier-saas-misclassification
status: open
discovered_in: 2025Q4 seasonal smoke (Bedrock + claude-opus-4-7)
---

# Termius SaaS invoice misclassified as HOTEL_FOLIO

## Problem

During the 2025Q4 seasonal smoke run, a **Termius Corporation** subscription
invoice ($120 USD SSH client Pro subscription, English-language Stripe
invoice) was classified by the LLM as a hotel folio and routed to the hotel
matching pipeline.  Result: it ended up in `missing.json` as an unmatched
`hotel_invoice` that no amount of `--supplemental` searching can resolve —
there is no corresponding hotel invoice because it is not a hotel stay.

```
needed_for: 20251112_Termius Corporation_水单.pdf
expected_merchant: Termius Corporation
expected_amount: 120.0
```

Agent loop convergence is blocked for this batch: iter 2 → 3 will produce
identical items, eventually landing in `status=converged` via the
convergence_hash stability check, but the "converged" state is misleading —
the item was never a real gap.

## What the LLM saw

The actual PDF (via `pdftotext`):

```
Invoice
Invoice number Q9YJOO4I-0001
Date of issue    November 12, 2025
Date due         November 12, 2025

Termius Corporation
2261 Market Street #4981
San Francisco, California 94114
United States
support@termius.com

Bill to  user@example.com

US EIN 38-4097286

$120.00 USD due November 12, 2025

Description                        Qty   Unit price   Amount
Termius Pro Subscription
```

## Why the LLM slipped

The classifier prompt (`scripts/core/prompts.py`) and the classification
logic in `scripts/core/classify.py` were designed for Chinese
reimbursement-in-scope documents.  Signals the LLM likely weighted as
"hotel-like":

- `"Corporation"` in the merchant name
- Single-line-item invoice format with `Qty / Unit price / Amount`
- Billing address / entity block resembling a hotel billing summary
- $120 is plausible as a hotel night rate
- English-language invoices are rare enough in our corpus that nearest
  neighbours in the prompt may bias toward the dominant English hotel
  template (Marriott / Courtyard folios)

## Why it hurts

1. **Wrong category** means the record is piped into hotel matching, where
   it stays permanently unmatched.
2. **Agent loop wastes effort** — every iteration will suggest searching
   Gmail for "more Termius", which finds nothing useful.
3. **`下载报告.md` hotel section** shows a confusing entry that is neither
   a real hotel nor a real gap.
4. **Finance review noise** — the file ships in the zip labelled as a
   hotel folio.

## Scope boundary

This is **not** a matching-logic bug.  P1/P2/P3 matching tiers work
correctly: they correctly reject a hotel_invoice with no matching
hotel_folio.  The bug is upstream at the LLM classification step.

This is **not** a new platform to add to `references/platforms.md` —
Termius is a straight Stripe invoice, not a Chinese invoice platform.

## Possible fixes (ideas — not yet validated)

1. **Tighten classifier prompt** to require at least one of `{checkInDate,
   checkOutDate, roomNo, nights, guestName}` for a HOTEL_FOLIO
   classification.  If none present → OTHER_RECEIPT or IGNORE.
2. **Non-CNY currency detection** — if `$USD / €EUR / £GBP` is the
   declared currency AND the merchant is not a known hotel brand, default
   to OTHER_RECEIPT.  (Risk: foreign-travel hotel bookings would need
   an allowlist.)
3. **Post-classification sanity check** — if category == HOTEL_FOLIO but
   OCR returned `checkInDate=null AND checkOutDate=null AND
   departureDate=null`, downgrade to UNKNOWN and write a "suspect"
   marker to missing.json rather than letting it poison the matching
   pipeline.
4. **Exclude at source** — add `-from:support@termius.com` to
   `learned_exclusions.json`.  Fast fix, but treats the symptom; the next
   English-language SaaS vendor (Notion, Figma, GitHub, Linear...) will
   hit the same trap.

## Recommended next step

Option 3 (post-classification sanity check) is the highest-value + lowest-
risk fix: it treats the root cause ("no hotel-ish date fields" → "not a
hotel folio") without requiring currency allowlists or per-vendor
exclusions.  It also produces a clearer debugging signal — "LLM said
HOTEL_FOLIO but OCR fields contradict that" is an actionable anomaly for
future prompt tuning.

This is out of scope for the 2026-05-01 Skill compliance work.  Suggest a
separate `/ce:brainstorm` session to design the sanity-check logic.

## Context

- Discovered: 2026-05-01
- Commit at discovery: ca249f5 (Skill compliance + Agent contract evals
  series complete)
- Sample PDF: `~/tmp/smoke-2025Q4/pdfs/20251112_Termius Corporation_水单.pdf`
  (do not commit — contains real Gmail sender address)
- OCR cache entry: `~/.cache/gmail-invoice-downloader/ocr/<sha16>.json`
  (where `<sha16>` is the SHA-256 prefix of the PDF bytes)
