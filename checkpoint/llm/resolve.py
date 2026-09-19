"""Pick the right LLM client from a model name (or an explicit provider prefix)."""
from __future__ import annotations

import os

from .errors import LLMCredentialError, missing_credential
from .models import DEFAULT_ANTHROPIC_MODEL, DEFAULT_MODEL

# Gemini ships an OpenAI-compatible endpoint, so we reach it with the OpenAI
# client + a base_url — no extra SDK required.
_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

_PREFIXES = ("openai", "anthropic", "gemini", "google", "compat", "local")


def provider_for(model: str | None) -> str:
    """Infer the provider from a model name.

    Accepts an explicit ``provider:model`` prefix (e.g. ``anthropic:claude-...``);
    otherwise infers from well-known name prefixes. Defaults to OpenAI, which is
    where the default model lives.
    """
    m = (model or "").strip().lower()
    if ":" in m:
        head = m.split(":", 1)[0]
        if head in _PREFIXES:
            return "gemini" if head == "google" else ("compat" if head == "local" else head)
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini"):
        return "gemini"
    return "openai"


def bare_model(model: str | None) -> str:
    """Strip any ``provider:`` prefix, returning the model id the SDK expects.

    Every call path runs a model name through this before it reaches an SDK: a
    provider prefix is Checkpoint's routing syntax, and no vendor knows it.
    """
    m = (model or DEFAULT_MODEL).strip()
    if ":" in m:
        head, rest = m.split(":", 1)
        if head.lower() in _PREFIXES:
            return rest.strip()
    return m


def get_client(model: str | None = None):
    """Return an OpenAI-client-shaped object for the given model's provider.

    Env overrides:
      - ``CHECKPOINT_LLM_BASE_URL`` forces every call through an
        OpenAI-compatible endpoint (local models, vLLM, OpenRouter, …).
      - Provider keys: ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` /
        ``GEMINI_API_KEY``.
    """
    base_override = os.environ.get("CHECKPOINT_LLM_BASE_URL")
    if base_override:
        from openai import OpenAI
        return OpenAI(base_url=base_override, api_key=os.environ.get("OPENAI_API_KEY", "not-needed"))

    provider = provider_for(model)

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model)

    if provider == "gemini":
        from openai import OpenAI
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise missing_credential("GEMINI_API_KEY", model, DEFAULT_MODEL)
        return OpenAI(base_url=_GEMINI_OPENAI_BASE, api_key=key)

    if provider == "compat":
        from openai import OpenAI
        base = os.environ.get("CHECKPOINT_LLM_BASE_URL")
        return OpenAI(base_url=base, api_key=os.environ.get("OPENAI_API_KEY", "not-needed"))

    # Default: OpenAI (patched globally in tests via openai.OpenAI). The SDK
    # constructor is the only thing that knows how it resolves a key, so we let
    # it decide and translate its one failure mode into an actionable message.
    from openai import OpenAI
    try:
        return OpenAI()
    except Exception as e:
        if _is_missing_key(e):
            raise missing_credential("OPENAI_API_KEY", model, DEFAULT_ANTHROPIC_MODEL) from e
        raise


def _is_missing_key(exc: Exception) -> bool:
    """Whether an SDK constructor failed for a missing credential, not a real bug."""
    if isinstance(exc, LLMCredentialError):
        return True
    text = str(exc).lower()
    return "api_key" in text or "api key" in text
