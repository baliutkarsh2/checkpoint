"""Typed failures for every LLM call Checkpoint makes.

They subclass ``RuntimeError`` because the call sites that predate this module
catch ``RuntimeError``; new code can catch the precise class instead. The
messages are written for the person running ``checkpoint``, not for a log: a
missing credential says which variable to set and which flags change the model,
because that is the single most common way a judged run fails.
"""
from __future__ import annotations


class LLMError(RuntimeError):
    """An LLM call failed in a way the caller cannot retry away."""


class LLMCredentialError(LLMError):
    """No usable credential is configured for the requested model."""


class LLMResponseError(LLMError):
    """The model answered, but not with the JSON the caller asked for."""


def missing_credential(env_var: str, model: str | None, example: str) -> LLMCredentialError:
    """The error a user sees when a judged run has nowhere to send its prompt."""
    return LLMCredentialError(
        f"No API key for model {model or '(default)'!r}: set {env_var} in your environment. "
        f"Or judge with another provider — pass --model {example}, or set model = "
        f'"{example}" under [judge] in checkpoint.toml. Or point at a local or other '
        f"OpenAI-compatible server with CHECKPOINT_LLM_BASE_URL=http://localhost:1234/v1."
    )
