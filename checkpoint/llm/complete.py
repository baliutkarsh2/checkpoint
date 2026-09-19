"""One way to ask a model for JSON, whichever vendor answers.

Call sites differ in what they want back (a verdict, an assertion, a seed), but
they all want the same thing from the transport: strict JSON, no creative
sampling, and a clear error when no credential is configured. Structured output
is requested where the provider supports it and degraded gracefully where it
does not, and ``temperature`` is only ever sent when a caller sets it — current
reasoning models reject it outright.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

from .resolve import bare_model, get_client

DEFAULT_MODEL = "gpt-5.6-luna"
"""Cheap, current, and good enough to judge. Override per run with --judge-model."""

_RETRIES = 3
_BACKOFF = 1.5
_TRANSIENT = ("429", "500", "502", "503", "504", "timeout", "timed out", "connection", "overloaded")


class LLMError(RuntimeError):
    """The model could not be reached, or did not answer with usable JSON."""


def complete_json(
    *,
    model: str,
    system: str,
    user: str,
    schema: dict | None = None,
    temperature: float | None = None,
    client: Any = None,
) -> Any:
    """Ask ``model`` for a JSON object and return it parsed."""
    name = bare_model(model) or DEFAULT_MODEL
    try:
        client = client if client is not None else get_client(model)
    except Exception as e:  # noqa: BLE001 — turn a missing key into an actionable message
        raise LLMError(str(e)) from e

    request: dict[str, Any] = {
        "model": name,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if temperature is not None:
        request["temperature"] = temperature
    formats = _formats(schema)

    last: Exception | None = None
    for attempt in range(_RETRIES):
        for index, response_format in enumerate(formats):
            try:
                payload = dict(request)
                if response_format is not None:
                    payload["response_format"] = response_format
                raw = client.chat.completions.create(**payload)
                return _parse(raw)
            except Exception as e:  # noqa: BLE001 — classified below
                last = e
                if _rejects_parameter(e) and index + 1 < len(formats):
                    continue  # this provider/model does not take that format; try a simpler one
                break
        if not _transient(last):
            break
        time.sleep(_BACKOFF ** attempt + random.random() * 0.2)
    raise LLMError(f"{name}: {last}") from last


def _formats(schema: dict | None) -> list[dict | None]:
    """Response-format attempts, most strict first."""
    if schema is None:
        return [{"type": "json_object"}, None]
    return [
        {"type": "json_schema",
         "json_schema": {"name": "result", "schema": schema, "strict": True}},
        {"type": "json_object"},
        None,
    ]


def _parse(raw: Any) -> Any:
    try:
        content = raw.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as e:
        raise LLMError(f"unreadable response: {raw!r}") from e
    if content is None:
        raise LLMError("the model returned no content")
    text = content.strip()
    if text.startswith("```"):  # some models fence JSON despite the instruction
        text = text.strip("`")
        text = text.partition("\n")[2] if text[:4].lower().startswith("json") else text
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"the model did not return JSON: {text[:200]}") from e


def _transient(error: Exception | None) -> bool:
    return error is not None and any(m in str(error).lower() for m in _TRANSIENT)


def _rejects_parameter(error: Exception) -> bool:
    message = str(error).lower()
    return any(m in message for m in
               ("response_format", "json_schema", "unsupported", "not supported", "invalid_request"))
