"""Pick the right LLM client from a model name (or an explicit provider prefix)."""
from __future__ import annotations

import os

# Gemini ships an OpenAI-compatible endpoint, so we reach it with the OpenAI
# client + a base_url — no extra SDK required.
_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def provider_for(model: str | None) -> str:
    """Infer the provider from a model name.

    Accepts an explicit ``provider:model`` prefix (e.g. ``anthropic:claude-...``);
    otherwise infers from well-known name prefixes. Defaults to OpenAI so existing
    ``gpt-4o-mini`` config keeps working unchanged.
    """
    m = (model or "").strip().lower()
    if ":" in m:
        head = m.split(":", 1)[0]
        if head in ("openai", "anthropic", "gemini", "google", "compat", "local"):
            return "gemini" if head == "google" else ("compat" if head == "local" else head)
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini"):
        return "gemini"
    return "openai"


def bare_model(model: str | None) -> str:
    """Strip any ``provider:`` prefix, returning the model id the SDK expects."""
    m = (model or "").strip()
    if ":" in m:
        head, rest = m.split(":", 1)
        if head.lower() in ("openai", "anthropic", "gemini", "google", "compat", "local"):
            return rest
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
        return AnthropicClient()

    if provider == "gemini":
        from openai import OpenAI
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set (required for gemini-* models)."
            )
        return OpenAI(base_url=_GEMINI_OPENAI_BASE, api_key=key)

    if provider == "compat":
        from openai import OpenAI
        base = os.environ.get("CHECKPOINT_LLM_BASE_URL")
        return OpenAI(base_url=base, api_key=os.environ.get("OPENAI_API_KEY", "not-needed"))

    # Default: OpenAI (patched globally in tests via openai.OpenAI).
    from openai import OpenAI
    return OpenAI()
