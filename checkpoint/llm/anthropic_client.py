"""Anthropic adapter presenting the OpenAI client surface Checkpoint calls.

Only ``chat.completions.create`` with an optional ``response_format`` of
``{"type": "json_object"}`` is used, so that's all we translate. JSON mode is
implemented by prefilling the assistant turn with ``{`` — the standard, reliable
way to force a JSON object out of a Claude model.
"""
from __future__ import annotations

from types import SimpleNamespace


def _shaped(text: str):
    """Wrap raw text in the OpenAI response shape callers expect."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class _Completions:
    def __init__(self, anthropic_client):
        self._c = anthropic_client

    def create(self, *, model, messages, response_format=None, temperature=0,
               max_tokens=4096, **_ignored):
        system = "\n\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        convo = [
            {"role": m["role"], "content": m["content"]}
            for m in messages if m.get("role") in ("user", "assistant")
        ]
        want_json = bool(response_format) and response_format.get("type") == "json_object"
        if want_json:
            # Prefill forces the model to continue a JSON object.
            convo.append({"role": "assistant", "content": "{"})

        resp = self._c.messages.create(
            model=model,
            system=system or None,
            messages=convo,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = ""
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                text += block.text
        if want_json:
            text = "{" + text
        return _shaped(text)


class _Chat:
    def __init__(self, anthropic_client):
        self.completions = _Completions(anthropic_client)


class AnthropicClient:
    """OpenAI-shaped facade over ``anthropic.Anthropic``."""

    def __init__(self):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "Claude models require the Anthropic SDK. Install it with "
                "`pip install anthropic` (or `pip install checkpoint-agents[anthropic]`)."
            ) from e
        self._client = anthropic.Anthropic()
        self.chat = _Chat(self._client)
