"""
Provider-agnostic LLM adapter for invoice OCR.

Exposes a uniform interface (LLMClient.extract_from_pdf) over:
- Anthropic API via the `anthropic` SDK (default)
- AWS Bedrock via `boto3` (when LLM_PROVIDER=bedrock or AWS_PROFILE is set)
- Disabled mode ("none") which raises LLMDisabledError — used by --no-llm

Singleton: one client instance per process (see get_client()). Thread-safe
via a module-level lock. Callers share the same underlying SDK client, which
the Anthropic and boto3 clients both guarantee is thread-safe for concurrent
calls.

Retry: extract_with_retry() wraps provider calls with exponential backoff on
retryable errors (429, 5xx, 529 overloaded). Max 3 attempts by default.
"""

from __future__ import annotations

import base64
import os
import random
import threading
import time
from typing import Optional


# =============================================================================
# Exceptions
# =============================================================================

class LLMError(Exception):
    """Base class for LLM-related failures."""


class LLMAuthError(LLMError):
    """Missing API key / AWS credentials."""


class LLMConfigError(LLMError):
    """Bad configuration — unknown provider, missing env."""


class LLMDisabledError(LLMError):
    """LLM_PROVIDER=none or --no-llm was passed; caller should use offline fallback."""


class LLMSizeLimitError(LLMError):
    """PDF too large for the model."""


class LLMRateLimitError(LLMError):
    """Retryable: 429 or 529 overloaded."""


class LLMServerError(LLMError):
    """Retryable: 5xx from provider."""


# =============================================================================
# Client implementations
# =============================================================================

class LLMClient:
    """Uniform interface: extract_from_pdf(pdf_bytes, prompt) -> response text."""

    provider_name: str = "base"

    def extract_from_pdf(self, pdf_bytes: bytes, prompt: str) -> str:
        raise NotImplementedError


class AnthropicClient(LLMClient):
    """Anthropic API client. Requires ANTHROPIC_API_KEY."""

    provider_name = "anthropic"

    def __init__(self, model: Optional[str] = None):
        try:
            import anthropic
        except ImportError as e:
            raise LLMConfigError(
                "anthropic SDK not installed. Run: pip install anthropic"
            ) from e

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMAuthError(
                "ANTHROPIC_API_KEY not set. Either export it, or set "
                "LLM_PROVIDER=bedrock to use AWS Bedrock instead."
            )

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get(
            "ANTHROPIC_MODEL", "claude-sonnet-4-5"
        )

    def extract_from_pdf(self, pdf_bytes: bytes, prompt: str) -> str:
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        except Exception as e:
            _reraise_as_llm_error(e)
            raise  # unreachable, but keeps type checker happy

        for block in resp.content:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", "")
        raise LLMError(
            f"Anthropic returned no text block. stop_reason={resp.stop_reason}"
        )


class BedrockClient(LLMClient):
    """AWS Bedrock client. Requires AWS credentials (profile or keys)."""

    provider_name = "bedrock"

    def __init__(self, model_id: Optional[str] = None):
        try:
            import boto3
        except ImportError as e:
            raise LLMConfigError(
                "boto3 not installed. Run: pip install boto3"
            ) from e

        region = os.environ.get("AWS_REGION", "us-east-1")
        try:
            self.client = boto3.client("bedrock-runtime", region_name=region)
        except Exception as e:
            raise LLMAuthError(
                f"Failed to create Bedrock client in region {region}: {e}. "
                f"Check AWS_PROFILE or AWS_ACCESS_KEY_ID."
            ) from e

        self.model_id = model_id or os.environ.get(
            "BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5"
        )

    def extract_from_pdf(self, pdf_bytes: bytes, prompt: str) -> str:
        import json as _json

        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2000,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        try:
            resp = self.client.invoke_model(
                modelId=self.model_id, body=_json.dumps(request_body)
            )
        except Exception as e:
            _reraise_as_llm_error(e)
            raise

        body = _json.loads(resp["body"].read())
        if not body.get("content") or not body["content"][0].get("text"):
            raise LLMError(
                f"Bedrock returned empty content. stop_reason={body.get('stop_reason')}"
            )
        return body["content"][0]["text"]


class DisabledClient(LLMClient):
    """No-op client for --no-llm mode. Every call raises LLMDisabledError."""

    provider_name = "none"

    def extract_from_pdf(self, pdf_bytes: bytes, prompt: str) -> str:
        raise LLMDisabledError("LLM is disabled (--no-llm or LLM_PROVIDER=none)")


# =============================================================================
# Error classification
# =============================================================================

def _reraise_as_llm_error(e: Exception) -> None:
    """Map provider SDK exceptions to our LLM* hierarchy."""
    msg = str(e).lower()

    if any(h in msg for h in ("too large", "exceeds", "size limit")):
        raise LLMSizeLimitError(f"PDF exceeds model limit: {e}") from e
    if any(h in msg for h in ("401", "unauthorized", "forbidden", "credential")):
        raise LLMAuthError(f"Auth failed: {e}") from e
    if any(h in msg for h in ("429", "rate_limit", "rate limit", "throttl", "overloaded", "529")):
        raise LLMRateLimitError(f"Rate limited: {e}") from e
    if any(h in msg for h in ("500", "502", "503", "504", "server error", "timeout")):
        raise LLMServerError(f"Server error: {e}") from e

    # Default: treat as plain LLMError so callers can decide
    raise LLMError(f"LLM call failed: {e}") from e


# =============================================================================
# Singleton factory
# =============================================================================

_client_lock = threading.Lock()
_client: Optional[LLMClient] = None


def get_client(provider_override: Optional[str] = None) -> LLMClient:
    """
    Return a process-wide LLM client singleton.

    Provider selection order:
      1. provider_override arg (CLI --llm-provider)
      2. LLM_PROVIDER env var
      3. Default: "anthropic"

    Values: "anthropic", "bedrock", "none"

    Thread-safe. First call constructs the client; subsequent calls reuse it
    even across threads.
    """
    global _client

    with _client_lock:
        if _client is not None and provider_override is None:
            return _client

        provider = (
            provider_override
            or os.environ.get("LLM_PROVIDER")
            or "anthropic"
        ).lower()

        if provider == "anthropic":
            client: LLMClient = AnthropicClient()
        elif provider == "bedrock":
            client = BedrockClient()
        elif provider == "none":
            client = DisabledClient()
        else:
            raise LLMConfigError(
                f"Unknown LLM_PROVIDER={provider!r}. "
                f"Valid: anthropic, bedrock, none."
            )

        _client = client
        return _client


def reset_client() -> None:
    """Drop the singleton. Exposed for tests only."""
    global _client
    with _client_lock:
        _client = None


# =============================================================================
# Retry wrapper
# =============================================================================

def extract_with_retry(
    pdf_bytes: bytes,
    prompt: str,
    *,
    client: Optional[LLMClient] = None,
    max_attempts: int = 3,
    base_delay: float = 2.0,
) -> str:
    """
    Call the LLM with exponential backoff on retryable errors.

    Retryable: LLMRateLimitError, LLMServerError.
    Non-retryable (raised immediately): LLMAuthError, LLMSizeLimitError,
    LLMDisabledError, LLMConfigError, LLMError.

    Backoff: base_delay * 2^attempt with ±20% jitter.
    Attempt 1: immediate, attempt 2: ~2s wait, attempt 3: ~4s wait.
    """
    if client is None:
        client = get_client()

    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return client.extract_from_pdf(pdf_bytes, prompt)
        except (LLMRateLimitError, LLMServerError) as e:
            last_err = e
            if attempt == max_attempts - 1:
                break
            sleep_s = base_delay * (2 ** attempt)
            sleep_s *= 1 + random.uniform(-0.2, 0.2)
            time.sleep(sleep_s)
        except LLMError:
            # Non-retryable — re-raise immediately
            raise

    assert last_err is not None
    raise last_err
