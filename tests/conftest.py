"""Pytest config: put scripts/ on sys.path and provide shared fixtures."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# Add scripts/ so tests can `import v53_pipeline` etc.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest


FIXTURE_PDFS = Path("/Users/kentpeng/Documents/agent Test")


@pytest.fixture
def hotel_invoice_pdf():
    p = FIXTURE_PDFS / "dzfp_25322000000531608417_鲁能泰山度假俱乐部管理有限公司无锡万豪酒店_20251113000928.pdf"
    if not p.exists():
        pytest.skip(f"Fixture not available: {p}")
    return p


@pytest.fixture
def hotel_folio_pdf():
    p = FIXTURE_PDFS / "nkgak_folio_ef_sj_gc547945017.pdf"
    if not p.exists():
        pytest.skip(f"Fixture not available: {p}")
    return p


@pytest.fixture
def didi_invoice_pdf():
    p = FIXTURE_PDFS / "滴滴电子发票 (1).pdf"
    if not p.exists():
        pytest.skip(f"Fixture not available: {p}")
    return p


@pytest.fixture
def didi_receipt_pdf():
    p = FIXTURE_PDFS / "滴滴出行行程报销单 (1).pdf"
    if not p.exists():
        pytest.skip(f"Fixture not available: {p}")
    return p
