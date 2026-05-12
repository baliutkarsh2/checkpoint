"""[P] LLM judge using OpenAI structured output. Single batched call per run."""
from __future__ import annotations

import json
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
        for j, item in enumerate(items):
            if j in used_idx:
                continue
            if (item.get("criterion") or "").strip() == c.strip():
                match = JudgeResult(c, bool(item.get("passed")), item.get("reasoning") or "")
                used_idx.add(j)
                break
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
    raw = json.dumps(state)
    if len(raw) <= max_chars:
        return state
    return {
        "_note": "state truncated for context",
        "summary": {
            "n_repos": len(state.get("repos", {})),
            "n_issues": len(state.get("issues", {})),
            "n_comments": len(state.get("comments", {})),
            "n_labels": len(state.get("labels", {})),
            "issues_preview": [
                {
                    "title": i.get("title"),
                    "state": i.get("state"),
                    "labels": [lab.get("name") for lab in i.get("labels", [])],
                    "comments": i.get("comments"),
                }
                for i in list(state.get("issues", {}).values())[:30]
            ],
        },
    }
