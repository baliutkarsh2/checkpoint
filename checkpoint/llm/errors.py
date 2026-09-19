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
        f"No API key for model {model or '(default)'!r}: set {env_var} in your environment.\n"
        f"  - to judge with a different provider instead, pass --judge-model "
        f"(e.g. --judge-model {example});\n"
        f"  - to use a local or other OpenAI-compatible server, set "
        f"CHECKPOINT_LLM_BASE_URL (e.g. http://localhost:1234/v1)."
    )
