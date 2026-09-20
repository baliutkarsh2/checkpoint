"""One call path for every LLM request Checkpoint makes.

Before this module each caller wrote its own ``chat.completions.create``, and
each one carried the same three bugs: a hard-coded ``temperature=0`` that
current reasoning models reject outright, a ``provider:model`` string handed to
an SDK that has never heard of the prefix, and no retry — so a single 429 made a
criterion fail closed and moved the gate's verdict. Fixing that in six places
means fixing it in six places again next time, so there is one path now:

    complete_json(system=..., user=..., model=..., schema=...) -> parsed JSON
    complete_text(system=..., user=..., model=...)             -> str

Both speak the OpenAI client shape (``client.chat.completions.create``), which
is the internal contract every provider adapter translates to and every test
seam fakes.

What the path guarantees:

* **Structured output where it exists.** With a ``schema`` the request uses
  OpenAI's strict ``json_schema`` response format (and Claude's
  ``output_config``, via the adapter). If the endpoint rejects the parameter —
  an older proxy, a local server — the call degrades to JSON mode and then to
  prompt-only JSON rather than failing.
* **No ``temperature`` unless the caller asked for one.** Current models default
  to their own sampling and 400 on an explicit value.
* **Bounded retries with jittered backoff** on 429s, 5xx, timeouts and
  connection errors, so transient infrastructure is not scored as agent failure.
* **A typed error that says what to do** when there is no credential.
"""
from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Mapping
from typing import Any

from .errors import LLMError, LLMResponseError
from .models import DEFAULT_MODEL
from .resolve import bare_model, get_client

log = logging.getLogger("checkpoint.llm")

#: Retries after the first attempt. Four calls in total by default.
DEFAULT_RETRIES = 3
_BACKOFF_BASE_S = 0.5
_BACKOFF_CAP_S = 8.0

# Substrings that identify the two parameters an endpoint may reject outright.
_JSON_PARAM_HINTS = ("response_format", "json_schema", "output_config", "json mode", "strict")
_TRANSIENT_NAMES = ("timeout", "connection", "ratelimit", "internalserver",
                    "serviceunavailable", "overloaded")

# Seam: tests replace this so backoff costs no wall-clock time.
_sleep = time.sleep


def complete_json(
    *,
    system: str,
    user: str | Mapping[str, Any],
    model: str | None = None,
    schema: Mapping[str, Any] | None = None,
    schema_name: str = "response",
    samples: int = 1,
    temperature: float | None = None,
    retries: int = DEFAULT_RETRIES,
    client: Any | None = None,
) -> Any:
    """Ask ``model`` for JSON and return it parsed.

    ``schema`` is a JSON Schema the provider enforces where it can; it is not
    re-validated here, because the caller knows what a violation means for it
    (a judge turns one into an ERROR verdict, a parser falls through).

    With the default ``samples=1`` the parsed value is returned. With
    ``samples=k`` the call is made ``k`` independent times and a list of ``k``
    parsed values is returned — the raw material for self-consistency, where a
    caller requires the samples to agree before it trusts them.

    ``client`` is the test seam every call site in this repo already uses: an
    object exposing ``chat.completions.create``.
    """
    if samples < 1:
        raise ValueError(f"samples must be at least 1, got {samples}")
    response_format: dict[str, Any] = (
        {"type": "json_schema",
         "json_schema": {"name": schema_name, "schema": dict(schema), "strict": True}}
        if schema is not None else {"type": "json_object"}
    )
    resolved = model or DEFAULT_MODEL
    client = client if client is not None else get_client(resolved)
    parsed = [
        _parse(_complete(system=system, user=user, model=resolved,
                         response_format=response_format, temperature=temperature,
                         retries=retries, client=client))
        for _ in range(samples)
    ]
    return parsed if samples > 1 else parsed[0]


def complete_text(
    *,
    system: str,
    user: str | Mapping[str, Any],
    model: str | None = None,
    temperature: float | None = None,
    retries: int = DEFAULT_RETRIES,
    client: Any | None = None,
) -> str:
    """Ask ``model`` for prose (a generated scenario, an explanation) and return it."""
    return _complete(system=system, user=user, model=model, response_format=None,
                     temperature=temperature, retries=retries, client=client).strip()


# ---------------------------------------------------------------------------
# The single request
# ---------------------------------------------------------------------------

def _complete(
    *,
    system: str,
    user: str | Mapping[str, Any],
    model: str | None,
    response_format: dict[str, Any] | None,
    temperature: float | None,
    retries: int,
    client: Any | None,
) -> str:
    resolved = model or DEFAULT_MODEL
    client = client if client is not None else get_client(resolved)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user if isinstance(user, str)
         else json.dumps(user, default=str)},
    ]
    resp = _send(client, bare_model(resolved), messages, response_format, temperature,
                 retries, resolved)
    return _content(resp)


def _send(client, model: str, messages: list[dict], response_format: dict[str, Any] | None,
          temperature: float | None, retries: int, label: str):
    """Send one completion, retrying transients and degrading rejected parameters."""
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(
                **_kwargs(model, messages, response_format, temperature)
            )
        except Exception as e:
            if _is_transient(e) and attempt < retries:
                attempt += 1
                delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (attempt - 1))
                # Full jitter: retries from parallel runs must not re-collide.
                _sleep(random.uniform(0, delay))
                log.info("llm: retrying %s after %s (attempt %d/%d)",
                         label, type(e).__name__, attempt, retries)
                continue
            simpler = _degrade(e, response_format, temperature)
            if simpler is not None:
                # A rejected parameter is not a transient failure: retrying the
                # same request cannot help, but a simpler one can, and it does
                # not consume the transient budget.
                response_format, temperature = simpler
                log.info("llm: %s rejected a parameter, retrying without it", label)
                continue
            if isinstance(e, LLMError):
                raise
            raise LLMError(f"LLM call to {label!r} failed: {e}") from e


def _kwargs(model: str, messages: list[dict], response_format: dict[str, Any] | None,
            temperature: float | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if response_format is not None:
        kwargs["response_format"] = response_format
    # Absent by default: current reasoning models answer a 400 to any explicit
    # temperature, so sending one to "make the judge deterministic" would in
    # fact fail every criterion it judged.
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


def _degrade(exc: Exception, response_format: dict[str, Any] | None,
             temperature: float | None) -> tuple[dict[str, Any] | None, float | None] | None:
    """Return a strictly simpler request when the endpoint named a parameter.

    Each step removes something, so the ladder terminates: strict schema → JSON
    mode → prompt-only, and an explicit temperature → none.
    """
    status = _status_of(exc)
    if status is not None and status != 400:
        return None
    text = str(exc).lower()
    if temperature is not None and "temperature" in text:
        return response_format, None
    if response_format is not None and any(hint in text for hint in _JSON_PARAM_HINTS):
        if response_format.get("type") == "json_schema":
            return {"type": "json_object"}, temperature
        return None, temperature
    return None


def _is_transient(exc: Exception) -> bool:
    """Whether the failure is infrastructure rather than a bad request."""
    status = _status_of(exc)
    if status is not None:
        return status == 429 or status >= 500
    name = type(exc).__name__.lower()
    return any(hint in name for hint in _TRANSIENT_NAMES)


def _status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    return value if isinstance(value, int) else None


def _content(resp: Any) -> str:
    try:
        text = resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as e:
        raise LLMResponseError(f"the model returned an unreadable response: {e}") from e
    return text or ""


def _parse(raw: str) -> Any:
    if not raw.strip():
        raise LLMResponseError("the model returned an empty response")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMResponseError(f"{e} (first 200 chars: {raw[:200]!r})") from e
