"""Anthropic adapter presenting the OpenAI client surface Checkpoint calls.

Only ``chat.completions.create`` is used, so that's all we translate. Two things
this adapter must get right, because getting them wrong silently corrupts a
judged run:

* JSON is requested with ``output_config.format`` (Claude's structured outputs),
  never by prefilling the assistant turn with ``{``. Current Claude models reject
  a prefill alongside extended thinking, and a prefill only *encourages* JSON —
  ``output_config`` constrains it.
* ``temperature`` is forwarded only when the caller set one. Sending a default
  is a 400 on current models, which would fail every criterion closed.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .errors import LLMError, missing_credential
from .models import DEFAULT_MODEL
from .resolve import bare_model

# Claude requires an output cap; the API has no "unlimited" default. Verdicts
# for a whole criteria list fit well inside this.
_MAX_TOKENS = 8192


def _shaped(text: str):
    """Wrap raw text in the OpenAI response shape callers expect."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _output_config(response_format: dict | None) -> dict | None:
    """Translate OpenAI's ``response_format`` into Claude's ``output_config``.

    ``json_object`` has no Claude equivalent: the caller's system prompt already
    demands a JSON object, so we send nothing rather than inventing a constraint.
    """
    if not response_format:
        return None
    if response_format.get("type") != "json_schema":
        return None
    schema = (response_format.get("json_schema") or {}).get("schema")
    if not schema:
        return None
    return {"format": {"type": "json_schema", "schema": schema}}


class _Completions:
    def __init__(self, anthropic_client):
        self._c = anthropic_client

    def create(self, *, model, messages, response_format=None,
               max_tokens=_MAX_TOKENS, **kw):
        system = "\n\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        convo = [
            {"role": m["role"], "content": m["content"]}
            for m in messages if m.get("role") in ("user", "assistant")
        ]
        params: dict[str, Any] = {
            "model": bare_model(model),
            "system": system or None,
            "messages": convo,
            "max_tokens": max_tokens,
        }
        if "temperature" in kw and kw["temperature"] is not None:
            params["temperature"] = kw["temperature"]
        output_config = _output_config(response_format)
        if output_config is not None:
            params["output_config"] = output_config

        resp = self._c.messages.create(**params)
        text = ""
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                text += block.text
        return _shaped(text)


class _Chat:
    def __init__(self, anthropic_client):
        self.completions = _Completions(anthropic_client)


class AnthropicClient:
    """OpenAI-shaped facade over ``anthropic.Anthropic``."""

    def __init__(self, model: str | None = None):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on optional dep
            raise LLMError(
                "Claude models require the Anthropic SDK. Install it with "
                "`pip install anthropic` (or `pip install checkpoint-agents[anthropic]`)."
            ) from e
        try:
            self._client = anthropic.Anthropic()
        except Exception as e:
            if "api_key" in str(e).lower() or "api key" in str(e).lower():
                raise missing_credential("ANTHROPIC_API_KEY", model, DEFAULT_MODEL) from e
            raise
        self.chat = _Chat(self._client)
