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

Sync: this directory is a snapshot and does NOT auto-update when
reimbursement-helper changes. To check for drift:
    diff -r scripts/core/ ~/reimbursement-helper/backend/agent/utils/
"""
