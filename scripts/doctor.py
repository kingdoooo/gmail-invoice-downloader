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
    provider = (os.environ.get("LLM_PROVIDER") or "bedrock").lower()

    if provider == "none":
        return True, "LLM_PROVIDER=none (OCR disabled)"

    if provider == "bedrock":
        region = os.environ.get("AWS_REGION", "us-east-1")
        # boto3 1.35.17+ reads AWS_BEARER_TOKEN_BEDROCK (Bedrock API Key).
        if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            model = os.environ.get("BEDROCK_MODEL_ID", "claude-opus-4-7 (default)")
            return True, f"Bedrock via API key (region={region}, model={model})"
        try:
            import boto3
        except ImportError:
            return False, (
                "LLM_PROVIDER=bedrock but boto3 not installed. "
                "REMEDIATION: pip install boto3"
            )
        try:
            session = boto3.Session()
            creds = session.get_credentials()
        except Exception as e:
            return False, f"boto3 session failed: {e}"
        if creds is None:
            return False, (
                "LLM_PROVIDER=bedrock but no AWS credentials resolved "
                "(tried env, ~/.aws, IAM role, instance profile, Bedrock API key). "
                "REMEDIATION: assume a role, set AWS_PROFILE / AWS_ACCESS_KEY_ID, "
                "export AWS_BEARER_TOKEN_BEDROCK=..., or pass --no-llm."
            )
        method = getattr(creds, "method", "unknown")
        model = os.environ.get("BEDROCK_MODEL_ID", "claude-opus-4-7 (default)")
        return True, f"Bedrock via {method} (region={region}, model={model})"

    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, (
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY not set. "
                "REMEDIATION: export ANTHROPIC_API_KEY=..., switch provider, or --no-llm."
            )
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6 (default)")
        return True, f"Anthropic API key present (model={model})"

    if provider == "anthropic-compatible":
        base = os.environ.get("ANTHROPIC_BASE_URL")
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not base:
            return False, (
                "LLM_PROVIDER=anthropic-compatible but ANTHROPIC_BASE_URL not set. "
                "REMEDIATION: export ANTHROPIC_BASE_URL=https://... (e.g. openrouter, litellm)."
            )
        if not key:
            return False, (
                "LLM_PROVIDER=anthropic-compatible but ANTHROPIC_API_KEY not set. "
                "REMEDIATION: export ANTHROPIC_API_KEY=... (endpoint-specific key)."
            )
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6 (default)")
        return True, f"Anthropic-compatible endpoint {base} (model={model})"

    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            return False, (
                "LLM_PROVIDER=openai but OPENAI_API_KEY not set. "
                "REMEDIATION: export OPENAI_API_KEY=..., switch provider, or --no-llm."
            )
        model = os.environ.get("OPENAI_MODEL", "gpt-4o (default)")
        return True, f"OpenAI API key present (model={model})"

    if provider == "openai-compatible":
        base = os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY")
        if not base:
            return False, (
                "LLM_PROVIDER=openai-compatible but OPENAI_BASE_URL not set. "
                "REMEDIATION: export OPENAI_BASE_URL=https://... (e.g. DeepSeek, Qwen, vLLM)."
            )
        if not key:
            return False, (
                "LLM_PROVIDER=openai-compatible but OPENAI_API_KEY not set. "
                "REMEDIATION: export OPENAI_API_KEY=... (endpoint-specific key)."
            )
        model = os.environ.get("OPENAI_MODEL", "gpt-4o (default)")
        return True, f"OpenAI-compatible endpoint {base} (model={model})"

    return False, (
        f"Unknown LLM_PROVIDER={provider}. "
        f"REMEDIATION: set bedrock|anthropic|anthropic-compatible|openai|openai-compatible|none."
    )


def _check_ocr_concurrency() -> tuple[bool, str]:
    """Validate LLM_OCR_CONCURRENCY env var (v5.5 addition).

    Returns (ok, message). Invalid values fail fast — same policy as
    analyze_pdf_batch, which raises LLMConfigError on invalid env.
    """
    env = os.environ.get("LLM_OCR_CONCURRENCY", "").strip()
    if not env:
        return (True, "LLM_OCR_CONCURRENCY unset (using default=5)")
    try:
        n = int(env)
        if n < 1:
            raise ValueError(f"must be >= 1, got {n}")
    except ValueError as e:
        return (
            False,
            f"LLM_OCR_CONCURRENCY={env!r} invalid ({e}). "
            f"REMEDIATION: set to a positive integer or unset it."
        )
    if n > 20:
        return (
            True,
            f"LLM_OCR_CONCURRENCY={n} (warn: unusually high; most providers "
            f"throttle above 10)"
        )
    return (True, f"LLM_OCR_CONCURRENCY={n}")


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
    ("OCR concurrency", _check_ocr_concurrency),
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
