"""[D] deterministic checker — stage 2: schema-validated LLM-JSON parser.

This is the "we did it better" wedge vs Archal's chatty stage 2:

  - The LLM is constrained to produce a single JSON object conforming to
    the `Assertion` pydantic schema.
  - If parsing or validation fails, we do NOT silently retry or guess —
    we explicitly fall through to the `[P]` judge with the original text.
  - On schema-valid output we evaluate programmatically against twin state;
    no further LLM call.

The runner wires this between stage-1 regex (``checker.check``) and the
stage-3 `[P]` judge (``judge.judge``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .checker import (
    CheckResult,
    _collect,
    _matches_state,
    _has_title,
    _has_label,
    _resource_lookup,
)

log = logging.getLogger("checkpoint.checker_llm")


Operator = Literal[
    "count_eq",
    "count_gte",
    "count_lte",
    "exists",
    "not_exists",
    "all_have",
    "none_have",
]


class Assertion(BaseModel):
    """Strict schema. Extra keys, prose, or omitted required fields fail
    validation and force fall-through to the `[P]` judge."""

    model_config = {"extra": "forbid"}

    resource: str = Field(..., description="Singular or plural noun from the criterion")
    selector: dict[str, Any] | None = Field(
        default=None,
        description="Optional filter: keys may include state/label/title/name/status",
    )
    operator: Operator
    value: int | str | bool | None = None


@dataclass
class ParseOutcome:
    assertion: Assertion | None
    reason: str  # "ok" on success; otherwise why we fell through


SYSTEM_PROMPT = """You translate a single English success criterion about a
software system's state into one JSON object conforming exactly to this schema:

{
  "resource":  string,                                   // required, e.g. "issues", "channels", "refunds"
  "selector":  object | null,                            // optional filter; keys include "state", "label", "title", "name", "status"
  "operator":  one of ["count_eq","count_gte","count_lte","exists","not_exists","all_have","none_have"],
  "value":     integer | string | boolean | null
}

Rules:
  - Output one JSON object. No prose, no markdown, no code fences.
  - Use plural lowercase for `resource` when the criterion is countable
    ("issues", "refunds"); singular only for `exists`/`not_exists`.
  - For 'all closed issues have a comment', use operator "all_have",
    resource "issues", selector {"state": "closed"}, value "comment".
  - For 'no new disputes', use "count_eq" with value 0.
  - For 'an issue titled "Foo" exists', use "exists" with selector
    {"title": "Foo"}.
  - If the criterion isn't a state assertion (e.g. tone/quality), still
    produce JSON but use operator "exists" with selector null — caller
    will catch wrong calls. Do not refuse.
"""


def parse_assertion(
    criterion: str,
    resources: list[str] | None = None,
    *,
    model: str = "gpt-4o-mini",
    _client_factory=None,
) -> ParseOutcome:
    """Call the LLM and validate. Returns ``ParseOutcome``.

    ``_client_factory`` is a test seam: when set, the returned object must
    expose a ``.chat.completions.create(...)`` method matching the OpenAI
    SDK (the real factory just instantiates ``OpenAI()``).
    """
    try:
        if _client_factory is None:
            from .llm import get_client

            client = get_client(model)
        else:
            client = _client_factory()
    except Exception as e:  # pragma: no cover — defensive
        return ParseOutcome(None, f"llm client init failed: {e}")

    user_msg = {
        "criterion": criterion,
        "known_resources": resources or [],
    }

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_msg)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception as e:
        return ParseOutcome(None, f"openai call failed: {e}")

    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        return ParseOutcome(None, "empty response")

    # Reject anything that isn't a single JSON object (markdown fences, prose,
    # multi-object output, etc.). pydantic itself will reject extra keys.
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return ParseOutcome(None, f"not valid JSON: {e}")
    if not isinstance(obj, dict):
        return ParseOutcome(None, f"JSON is {type(obj).__name__}, not object")

    try:
        assertion = Assertion(**obj)
    except ValidationError as e:
        return ParseOutcome(None, f"schema validation failed: {e.errors()[:2]}")

    return ParseOutcome(assertion, "ok")


# ---------------------------------------------------------------------------
# Programmatic evaluator
# ---------------------------------------------------------------------------

def _apply_selector(items: list, selector: dict[str, Any] | None) -> list:
    if not selector:
        return items
    out = items
    if "state" in selector:
        sval = selector["state"]
        out = [i for i in out if isinstance(i, dict) and _matches_state(i, str(sval))]
    if "status" in selector:
        sval = str(selector["status"]).lower()
        out = [i for i in out if isinstance(i, dict)
               and str(i.get("status", "")).lower() == sval]
    if "label" in selector:
        lab = str(selector["label"])
        out = [i for i in out if isinstance(i, dict) and _has_label(i, lab)]
    for key in ("title", "name"):
        if key in selector:
            needle = str(selector[key])
            out = [i for i in out if isinstance(i, dict) and _has_title(i, needle)]
    return out


def evaluate(assertion: Assertion, state: dict, trace: list | None = None) -> CheckResult:
    """Run an assertion against the merged twin state."""
    res = _resource_lookup(assertion.resource)
    if not res:
        return CheckResult(
            False, f"Unknown resource '{assertion.resource}'", handled=False
        )
    twin, key = res
    items = _collect(state, twin, key)
    filtered = _apply_selector(items, assertion.selector)
    n = len(filtered)
    op = assertion.operator
    val = assertion.value

    if op == "count_eq":
        target = int(val) if isinstance(val, (int, str)) and str(val).isdigit() else 0
        return CheckResult(n == target, f"Found {n} {assertion.resource}; expected exactly {target}.", True)

    if op == "count_gte":
        target = int(val) if isinstance(val, (int, str)) and str(val).isdigit() else 0
        return CheckResult(n >= target, f"Found {n} {assertion.resource}; expected at least {target}.", True)

    if op == "count_lte":
        target = int(val) if isinstance(val, (int, str)) and str(val).isdigit() else 0
        return CheckResult(n <= target, f"Found {n} {assertion.resource}; expected at most {target}.", True)

    if op == "exists":
        return CheckResult(n >= 1, f"Found {n} matching {assertion.resource}.", True)

    if op == "not_exists":
        return CheckResult(n == 0, f"Found {n} matching {assertion.resource}; expected none.", True)

    if op == "all_have":
        # value is the property required of every filtered item, e.g. "comment".
        prop = str(val or "")
        if not filtered:
            return CheckResult(False, "No items matched selector.", True)
        if prop == "comment":
            missing = [i for i in filtered if int(i.get("comments") or 0) < 1]
            return CheckResult(
                len(missing) == 0,
                f"{len(filtered) - len(missing)}/{len(filtered)} have a comment.",
                True,
            )
        # Generic case: property is a truthy key on each item.
        missing = [i for i in filtered if not i.get(prop)]
        return CheckResult(
            len(missing) == 0,
            f"{len(filtered) - len(missing)}/{len(filtered)} have '{prop}'.",
            True,
        )

    if op == "none_have":
        prop = str(val or "")
        if prop == "comment":
            have = [i for i in filtered if int(i.get("comments") or 0) >= 1]
        else:
            have = [i for i in filtered if i.get(prop)]
        return CheckResult(
            len(have) == 0,
            f"{len(have)}/{len(filtered)} have '{prop}'; expected none.",
            True,
        )

    return CheckResult(False, f"Unknown operator '{op}'", handled=False)


# ---------------------------------------------------------------------------
# Glue: try stage-2 on a single criterion
# ---------------------------------------------------------------------------

def try_stage2(
    criterion: str,
    state: dict,
    trace: list | None = None,
    *,
    model: str = "gpt-4o-mini",
    _client_factory=None,
) -> tuple[CheckResult | None, str]:
    """Returns ``(result_or_none, reason)``.

    - ``(CheckResult, "ok")`` on schema-valid happy path.
    - ``(None, "<reason>")`` on any fall-through.
    """
    outcome = parse_assertion(criterion, model=model, _client_factory=_client_factory)
    if outcome.assertion is None:
        log.info("stage-2 fall-through (%s) for criterion: %r", outcome.reason, criterion)
        return None, outcome.reason
    result = evaluate(outcome.assertion, state, trace or [])
    if not result.handled:
        log.info("stage-2 evaluator un-handled (%s) for criterion: %r", result.reasoning, criterion)
        return None, result.reasoning
    return result, "ok"
