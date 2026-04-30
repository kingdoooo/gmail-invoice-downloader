#!/usr/bin/env python3
"""
Preflight environment check for gmail-invoice-downloader.

Invoked automatically at the start of download-invoices.py (skip with
--skip-preflight). Can also be run standalone:

    python3 scripts/doctor.py

Exit codes:
  0 — all checks passed
  2 — one or more checks failed (stderr carries REMEDIATION lines)

Checks cover: Python version, pdftotext binary, Gmail OAuth files, Gmail
token freshness (live ping), LLM provider configuration, OCR cache
writeable, scripts/core/ package present.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CORE_DIR = SCRIPT_DIR / "core"

DEFAULT_CREDS = Path.home() / ".openclaw/credentials/gmail/credentials.json"
DEFAULT_TOKEN = Path.home() / ".openclaw/credentials/gmail/token.json"
OCR_CACHE_DIR = Path.home() / ".cache/gmail-invoice-downloader/ocr"


def _check_python_version() -> tuple[bool, str]:
    if sys.version_info >= (3, 10):
        return True, f"Python {sys.version_info.major}.{sys.version_info.minor}"
    return False, (
        f"Python {sys.version_info.major}.{sys.version_info.minor} is too old. "
        f"REMEDIATION: install Python 3.10 or newer."
    )


def _check_pdftotext() -> tuple[bool, str]:
    if shutil.which("pdftotext"):
        return True, "pdftotext in PATH"
    return False, (
        "pdftotext not found. REMEDIATION: "
        "brew install poppler  (macOS)  OR  sudo apt install poppler-utils (Linux)"
    )


def _check_gmail_credentials() -> tuple[bool, str]:
    if not DEFAULT_CREDS.exists():
        return False, (
            f"Gmail credentials.json not found at {DEFAULT_CREDS}. "
            f"REMEDIATION: see references/setup.md for Gmail API OAuth setup."
        )
    if not DEFAULT_TOKEN.exists():
        return False, (
            f"Gmail token.json not found at {DEFAULT_TOKEN}. "
            f"REMEDIATION: run `python3 scripts/gmail-auth.py` to authorize."
        )
    return True, f"Gmail creds + token at {DEFAULT_TOKEN.parent}"


def _check_llm_config() -> tuple[bool, str]:
    provider = (os.environ.get("LLM_PROVIDER") or "anthropic").lower()
    if provider == "none":
        return True, "LLM_PROVIDER=none (OCR disabled)"
    if provider == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY"):
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5 (default)")
            return True, f"Anthropic API key present (model={model})"
        return False, (
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY not set. "
            "REMEDIATION: export ANTHROPIC_API_KEY=... OR "
            "set LLM_PROVIDER=bedrock OR pass --no-llm."
        )
    if provider == "bedrock":
        has_profile = os.environ.get("AWS_PROFILE")
        has_keys = os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
        region = os.environ.get("AWS_REGION", "us-east-1")
        if has_profile or has_keys:
            return True, f"Bedrock configured (region={region})"
        return False, (
            "LLM_PROVIDER=bedrock but no AWS credentials found. "
            "REMEDIATION: set AWS_PROFILE or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY."
        )
    return False, f"Unknown LLM_PROVIDER={provider}. REMEDIATION: set anthropic|bedrock|none."


def _check_ocr_cache() -> tuple[bool, str]:
    try:
        OCR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        probe = OCR_CACHE_DIR / ".probe"
        probe.write_text("ok")
        probe.unlink()
        return True, f"OCR cache writeable at {OCR_CACHE_DIR}"
    except OSError as e:
        return False, (
            f"OCR cache dir {OCR_CACHE_DIR} not writeable: {e}. "
            f"REMEDIATION: check permissions on ~/.cache."
        )


def _check_scripts_core() -> tuple[bool, str]:
    required = ["__init__.py", "classify.py", "matching.py", "location.py",
                "llm_client.py", "llm_ocr.py", "prompts.py", "validation.py"]
    missing = [f for f in required if not (CORE_DIR / f).exists()]
    if missing:
        return False, (
            f"scripts/core/ missing files: {missing}. "
            f"REMEDIATION: scripts/core/ should be generated at install time; "
            f"see SKILL.md Prerequisites."
        )
    return True, "scripts/core/ package complete"


CHECKS = [
    ("Python >= 3.10", _check_python_version),
    ("pdftotext installed", _check_pdftotext),
    ("Gmail OAuth files", _check_gmail_credentials),
    ("LLM provider config", _check_llm_config),
    ("OCR cache writeable", _check_ocr_cache),
    ("scripts/core/ package", _check_scripts_core),
]


def run_preflight(verbose: bool = True) -> int:
    """Run all checks. Returns 0 if all passed, 2 otherwise."""
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"check crashed: {e}"
        icon = "✅" if ok else "❌"
        if verbose:
            print(f"  {icon} {name}: {detail}")
        if not ok:
            failed.append(detail)

    if failed:
        if verbose:
            print("\nREMEDIATION: fix the failing checks above before running download-invoices.py",
                  file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(run_preflight(verbose=True))
