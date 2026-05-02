"""String-level assertions on scripts/core/prompts.py::get_ocr_prompt().

v5.5 adds two rules (folio transactionDate = departureDate; itinerary
applicationDate field + rule). These tests lock in the prompt text so
silent regressions (e.g., prompt edit that drops a rule) break CI.
"""
import json
import re

from core.prompts import get_ocr_prompt


PROMPT = get_ocr_prompt()


class TestOCRPromptV55Rules:
    def test_folio_transaction_date_rule_present(self):
        assert "酒店水单" in PROMPT or "Guest Folio" in PROMPT
        assert "departureDate" in PROMPT
        # Look for the explicit rule text:
        rule_found = (
            re.search(r"transactionDate.*取值规则.*水单", PROMPT, re.DOTALL)
            or "统一使用" in PROMPT
        )
        assert rule_found, "folio transactionDate = departureDate rule missing"

    def test_application_date_field_defined_for_itinerary(self):
        # Field appears in the ride-hailing itinerary section.
        assert "applicationDate" in PROMPT
        assert "申请日期" in PROMPT

    def test_itinerary_transaction_date_rule_present(self):
        # transactionDate should equal applicationDate for itineraries.
        assert re.search(
            r"applicationDate.*transactionDate",
            PROMPT,
            re.DOTALL,
        ), "itinerary transactionDate = applicationDate rule missing"

    def test_sample_json_blocks_parseable(self):
        # Every fenced ```json block should parse.
        blocks = re.findall(r"```json\n(.*?)\n```", PROMPT, re.DOTALL)
        assert len(blocks) >= 1, "prompt must have at least one JSON sample"
        for b in blocks:
            try:
                json.loads(b)
            except json.JSONDecodeError as e:
                raise AssertionError(f"Unparseable sample JSON: {e}\n{b}")
