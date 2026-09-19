"""Vendor-neutral LLM access: one call path, one default model.

Checkpoint is neutral across model vendors — the judge, the criterion parser,
and the generators all work with OpenAI, Anthropic, Gemini, or any
OpenAI-compatible endpoint, chosen purely by the model name you pass
(optionally with a ``provider:`` prefix).

Call :func:`complete_json` or :func:`complete_text`; they own parameter choice,
structured output, retries and error messages, so no caller has to get those
right again. :func:`get_client` stays public because it is the seam the
provider adapters and the tests use.

The internal contract is the OpenAI client shape: every client exposes
``client.chat.completions.create(model=..., messages=[...], ...)`` and returns
an object with ``.choices[0].message.content``. Non-OpenAI providers are thin
adapters that translate to and from that shape.
"""
from .call import complete_json, complete_text
from .errors import LLMCredentialError, LLMError, LLMResponseError
from .models import DEFAULT_MODEL
from .resolve import bare_model, get_client, provider_for

__all__ = [
    "DEFAULT_MODEL",
    "LLMCredentialError",
    "LLMError",
    "LLMResponseError",
    "bare_model",
    "complete_json",
    "complete_text",
    "get_client",
    "provider_for",
]
