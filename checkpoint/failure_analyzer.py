"""Why a criterion failed, in a sentence a reader can act on.

When a run scores <100, for every failed criterion we ask the LLM to produce
a 3-5 sentence paragraph explaining *why* it failed, citing the offending
trace entry where possible. Batched into ONE LLM call regardless of failure
count (cost predictability).

Analyses are matched back to criteria by id, like the judge's verdicts: an
explanation attached to the wrong criterion is worse than no explanation, and
this module used to fall back to positional alignment. Unlike the judge this is
enrichment, so a missing or unmatched analysis is simply left out.

All tests use the ``_client_factory`` seam; no live API call required.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .llm import DEFAULT_MODEL, LLMError, complete_json

log = logging.getLogger("checkpoint.failure_analyzer")


SYSTEM = """You are a debugger. The user will give you:
- The original task an AI agent was asked to do.
- The agent's final answer (its stdout).
- A truncated trace of HTTP/tool calls the agent made.
- The final state of the system.
- A list of success criteria the agent FAILED, each with an id.

For each failed criterion, write ONE paragraph of 3 to 5 sentences explaining
why it failed. Name the offending trace entry by index when possible
(e.g. "at trace entry 12 the agent called create_issue with title 'Foo' but
the criterion required 'Bar'"). Be concrete, cite specific evidence. Do not
hedge.

Return one analysis per id, reusing each id exactly as given, as strict JSON:
{"analyses": [{"id": "<the id>", "why": "<paragraph>"}]}
"""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["analyses"],
    "properties": {
        "analyses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "why"],
                "properties": {
                    "id": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        }
    },
}


def _truncate_trace(trace: list, max_entries: int = 200) -> list:
    if not isinstance(trace, list):
        return []
    if len(trace) <= max_entries:
        return trace
    return trace[:max_entries] + [{"_truncated": f"...{len(trace) - max_entries} more"}]


def _truncate_state(state: dict, max_chars: int = 30000) -> dict:
    raw = json.dumps(state, default=str)
    if len(raw) <= max_chars:
        return state
    return {"_truncated": True, "_size": len(raw), "_max": max_chars,
            "_keys": list(state.keys())[:50]}


def analyze(
    failed_criteria: list[str],
    *,
    task: str,
    final_answer: str,
    trace: list,
    state: dict,
    model: str = DEFAULT_MODEL,
    _client_factory=None,
) -> dict[str, str]:
    """Return ``{criterion_text: paragraph}``.

    Empty dict on no failures or on any error (we never raise — failure
    analysis is best-effort enrichment).
    """
    if not failed_criteria:
        return {}

    by_id = {f"c{i}": text for i, text in enumerate(failed_criteria)}
    payload = {
        "task": task,
        "final_answer": final_answer,
        "trace": _truncate_trace(trace),
        "state": _truncate_state(state),
        "failed_criteria": [{"id": cid, "criterion": text} for cid, text in by_id.items()],
    }

    try:
        obj = complete_json(
            system=SYSTEM,
            user=payload,
            model=model,
            schema=_SCHEMA,
            schema_name="checkpoint_failure_analyses",
            client=_client_factory() if _client_factory else None,
        )
    except LLMError as e:
        log.info("failure_analyzer: %s", e)
        return {}

    if not isinstance(obj, dict):
        return {}
    out: dict[str, str] = {}
    for item in obj.get("analyses") or []:
        if not isinstance(item, dict):
            continue
        text = by_id.get(str(item.get("id") or ""))
        why = str(item.get("why") or "").strip()
        if text and why:
            out[text] = why
    return out
