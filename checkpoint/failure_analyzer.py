"""EV-05: Per-criterion failure analysis.

When a run scores <100, for every failed criterion we ask the LLM to produce
a 3-5 sentence paragraph explaining *why* it failed, citing the offending
trace entry where possible. Batched into ONE LLM call regardless of failure
count (cost predictability).

All tests use the ``_client_factory`` seam; no live API call required.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("checkpoint.failure_analyzer")


SYSTEM = """You are a debugger. The user will give you:
- The original task an AI agent was asked to do.
- The agent's final answer (its stdout).
- A truncated trace of HTTP/tool calls the agent made.
- The final state of the system.
- A list of success criteria the agent FAILED.

For each failed criterion, write ONE paragraph of 3 to 5 sentences explaining
why it failed. Name the offending trace entry by index when possible
(e.g. "at trace entry 12 the agent called create_issue with title 'Foo' but
the criterion required 'Bar'"). Be concrete, cite specific evidence. Do not
hedge.

Return strict JSON of the form:
{"analyses": [{"criterion": "<exact text>", "why": "<paragraph>"}]}
"""


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
    model: str = "gpt-4o-mini",
    _client_factory=None,
) -> dict[str, str]:
    """Return ``{criterion_text: paragraph}``.

    Empty dict on no failures or on any error (we never raise — failure
    analysis is best-effort enrichment).
    """
    if not failed_criteria:
        return {}

    try:
        if _client_factory is None:
            from openai import OpenAI

            client = OpenAI()
        else:
            client = _client_factory()
    except Exception as e:
        log.info("failure_analyzer: openai client unavailable (%s)", e)
        return {}

    payload: dict[str, Any] = {
        "task": task,
        "final_answer": final_answer,
        "trace": _truncate_trace(trace),
        "state": _truncate_state(state),
        "failed_criteria": failed_criteria,
    }

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception as e:
        log.info("failure_analyzer: openai call failed (%s)", e)
        return {}

    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        log.info("failure_analyzer: bad JSON (%s)", e)
        return {}

    items = obj.get("analyses") or []
    out: dict[str, str] = {}
    used_idx: set[int] = set()
    for c in failed_criteria:
        match = None
        for j, item in enumerate(items):
            if j in used_idx:
                continue
            if (item.get("criterion") or "").strip() == c.strip():
                match = item.get("why")
                used_idx.add(j)
                break
        if match is None:
            # Fallback: positional alignment.
            idx = len(out)
            if idx < len(items) and idx not in used_idx:
                item = items[idx]
                match = item.get("why")
                used_idx.add(idx)
        if match:
            out[c] = str(match).strip()
    return out
