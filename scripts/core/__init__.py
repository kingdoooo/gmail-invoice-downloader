"""
scripts/core/ — snapshot of reimbursement-helper/backend/agent/utils/

Source commit: a0e8515b267ca4fc6e886bd91b65c2de3a959e43
Copied on: 2026-05-01

Modifications from source:
- bedrock_ocr.py → split into llm_client.py + llm_ocr.py + prompts.py
  (provider-agnostic adapter, S3 path removed, SHA256 cache added)
- classify.py: removed detect_meal_type random assignment + COFFEE_KEYWORDS
  (gmail-invoice-downloader v5.3 — all meals classified as MEAL, no random
  early/mid/late assignment)
- matching.py: unchanged
- location.py: unchanged
- validation.py: new (validate_ocr_plausibility anti-hallucination)
- helpers.py: NOT copied — contains Concur reimbursement formatters
  (meal-type GUIDs, expense validators) irrelevant to the Gmail aggregation
  use case.
- prompts.py (v5.5): folio transactionDate=departureDate rule with null fallback;
  itinerary applicationDate field + rule with null fallback; two new JSON
  sample blocks (folio + itinerary) with common-field reminder captions.
  (v5.7): added "Hotel-specific field conditional extraction" rule requiring
  arrivalDate/departureDate/checkInDate/checkOutDate/roomNumber to remain
  null when no hotel-domain label appears near the value. Prevents SaaS
  subscription ranges (Nov 12, 2025 – Nov 12, 2026) from triggering
  is_hotel_folio_by_fields 3-choose-2 on Termius-style invoices.
  PENDING upstream sync to ~/reimbursement-helper/backend/agent/utils/prompts.py.
  See SKILL.md § Lessons Learned "v5.7 — IGNORED 白名单分类" for rationale.

Sync: this directory is a snapshot and does NOT auto-update when
reimbursement-helper changes. To check for drift:
    diff -r scripts/core/ ~/reimbursement-helper/backend/agent/utils/
(expect helpers.py difference — see above)
"""
