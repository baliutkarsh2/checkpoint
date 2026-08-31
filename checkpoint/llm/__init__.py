"""Vendor-neutral LLM client resolution.

Checkpoint is neutral across model vendors — the judge, the schema parser, and
the scenario generators all work with OpenAI, Anthropic, Gemini, or any
OpenAI-compatible endpoint, chosen purely by the model name you pass.

The internal contract is the OpenAI client shape: every client exposes
``client.chat.completions.create(model=..., messages=[...],
response_format={"type": "json_object"}, temperature=...)`` and returns an object
with ``.choices[0].message.content``. Non-OpenAI providers are thin adapters that
translate to and from that shape, so the call sites — and every existing test
seam that injects an OpenAI-shaped fake — stay unchanged.
"""
from .resolve import get_client, provider_for

__all__ = ["get_client", "provider_for"]
