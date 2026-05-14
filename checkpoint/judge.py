"""[P] LLM judge using OpenAI structured output. Single batched call per run."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class JudgeResult:
    criterion: str
    passed: bool
    reasoning: str


SYSTEM = """You are evaluating whether an AI agent met specific success criteria during a task.

You will be given:
- The original task the agent was asked to do.
- The agent's final answer (its stdout).
- The full trace of HTTP calls the agent made to a stateful clone of a SaaS API.
- The final state of the clone after the run.
- A list of criteria to judge.

For each criterion, decide pass/fail and explain why in one or two sentences, citing specific
evidence from the trace or state. Be strict: if the trace shows the agent did not actually
do what the criterion requires, fail it even if the final answer claims success.

Return strict JSON of the form:
{"results": [{"criterion": "<exact criterion text>", "passed": true/false, "reasoning": "<1-2 sentences>"}]}
"""


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace for fuzzy criterion matching."""
    return re.sub(r"\s+", " ", s.strip().lower())


def judge(
    task: str,
    final_answer: str,
    trace: list,
    state: dict,
    criteria: list[str],
    model: str = "gpt-4o-mini",
) -> list[JudgeResult]:
    if not criteria:
        return []
    from openai import OpenAI

    client = OpenAI()
    payload = {
        "task": task,
        "final_answer": final_answer,
        "trace": _truncate_trace(trace),
        "state": _truncate_state(state),
        "criteria": criteria,
    }

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    items = parsed.get("results") or []

    aligned: list[JudgeResult] = []
    used_idx: set[int] = set()
    for c in criteria:
        match: JudgeResult | None = None

        # Pass 1: exact text match
        for j, item in enumerate(items):
            if j in used_idx:
                continue
            if (item.get("criterion") or "").strip() == c.strip():
                match = JudgeResult(c, bool(item.get("passed")), item.get("reasoning") or "")
                used_idx.add(j)
                break

        # Pass 2: normalized substring match (handles LLM paraphrasing)
        if match is None:
            c_norm = _normalize(c)
            for j, item in enumerate(items):
                if j in used_idx:
                    continue
                item_norm = _normalize(item.get("criterion") or "")
                if item_norm and (item_norm in c_norm or c_norm in item_norm):
                    match = JudgeResult(c, bool(item.get("passed")), item.get("reasoning") or "")
                    used_idx.add(j)
                    break

        # Pass 3: positional fallback (LLM returned criteria in different order)
        if match is None:
            idx = len(aligned)
            if idx < len(items) and idx not in used_idx:
                item = items[idx]
                match = JudgeResult(c, bool(item.get("passed")), item.get("reasoning") or "")
                used_idx.add(idx)
            else:
                match = JudgeResult(c, False, "Judge returned no verdict for this criterion.")

        aligned.append(match)
    return aligned


def _truncate_trace(trace: list, max_entries: int = 200) -> list:
    if len(trace) <= max_entries:
        return trace
    return trace[:max_entries] + [{"_truncated": f"...{len(trace) - max_entries} more"}]


def _truncate_state(state: dict, max_chars: int = 30000) -> dict:
    raw = json.dumps(state, default=str)
    if len(raw) <= max_chars:
        return state

    # Generic twin-agnostic summary: top-level key → item count (works for any twin).
    summary: dict = {}
    for k, v in list(state.items())[:40]:
        if isinstance(v, dict):
            summary[k] = len(v)
        elif isinstance(v, list):
            summary[k] = len(v)
        else:
            summary[k] = v  # scalars (strings, ints) pass through as-is

    # Include a sample of items from the largest collection so the judge
    # has concrete evidence to cite, not just counts.
    biggest_key: str | None = None
    biggest_size = 0
    for k, v in state.items():
        if isinstance(v, (dict, list)) and len(v) > biggest_size:
            biggest_key = k
            biggest_size = len(v)

    if biggest_key:
        coll = state[biggest_key]
        if isinstance(coll, dict):
            sample = list(coll.values())[:20]
        else:
            sample = list(coll)[:20]
        summary[f"{biggest_key}_sample"] = sample

    return {"_note": "state truncated for context window", "_summary": summary}
