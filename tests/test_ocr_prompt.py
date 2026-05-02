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

    def test_new_rules_include_null_fallback_guidance(self):
        """v5.5: both new rules must explicitly say what to do when the
        key field (departureDate / applicationDate) is unreadable.
        Prevents LLM hallucination when the rule can't be applied."""
        # Folio null fallback
        assert "departureDate 无法识别" in PROMPT, \
            "folio rule must tell LLM what to do when departureDate missing"
        assert "transactionDate 填 null" in PROMPT, \
            "folio rule must instruct null, not guess"

        # Itinerary null fallback — same pattern
        assert "applicationDate 无法识别" in PROMPT, \
            "itinerary rule must tell LLM what to do when applicationDate missing"

    def test_new_samples_include_common_field_reminder(self):
        """v5.5: the folio and itinerary JSON samples are intentionally
        concise (showing only the new rule). A caption above each sample
        must remind the LLM to still extract common fields."""
        assert "仍需按通用字段表提取" in PROMPT, \
            "new samples must carry a caption reminding LLM to extract common fields"
