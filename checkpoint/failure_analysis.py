"""Post-run failure analysis: explain WHY criteria failed and suggest fixes.

Called from ``runner._evaluate()`` after scoring, only when there are failures.
Result is stored in ``RunResult.failure_analysis`` and persisted to the run
record so developers can read it from ``checkpoint replay`` or the run JSON.

Returns a plain ``dict[criterion_text, why_text]`` so the runner doesn't have
to import the full analysis shape — and so callers that don't need it can
skip the LLM call by not importing this module.
"""
from __future__ import annotations

import json

SYSTEM = """You are helping a developer debug why an AI agent failed specific evaluation criteria.

You are given:
- The task the agent was asked to perform.
- The criteria that were NOT satisfied.
- The HTTP trace of API calls the agent made.
- The final state of the system after the run.

For each failed criterion, explain concisely (2–3 sentences):
1. What the agent DID do (cite specific trace calls or state values).
2. What was MISSING or wrong to satisfy the criterion.
3. One concrete suggestion to fix the agent behavior.

Return strict JSON:
{
  "analyses": [
    {"criterion": "<criterion text>", "why_failed": "<2-3 sentences>", "suggestion": "<one-line fix>"}
  ]
}
"""


def analyze_failures(
    task: str,
    failed_criteria: list[str],
    trace: list,
    state: dict,
    model: str = "gpt-4o-mini",
    *,
    _client_factory=None,
) -> dict[str, str]:
    """Return ``{criterion_text: why_failed_text}`` for each failed criterion.

    Non-fatal: callers should catch all exceptions and soft-skip on failure.
    ``_client_factory`` is a test seam (same pattern as ``checker_llm``).
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
        raise RuntimeError(f"OpenAI client init failed: {e}") from e

    # Truncate trace to keep prompt size sane.
    short_trace = trace[:100] if len(trace) > 100 else trace

    payload = {
        "task": task,
        "failed_criteria": failed_criteria,
        "trace": short_trace,
        "state_keys": list(state.keys()) if isinstance(state, dict) else [],
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

    raw = (resp.choices[0].message.content or "{}").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    out: dict[str, str] = {}
    for item in parsed.get("analyses") or []:
        crit = (item.get("criterion") or "").strip()
        why = item.get("why_failed") or ""
        suggestion = item.get("suggestion") or ""
        if crit:
            out[crit] = why + (f" Suggestion: {suggestion}" if suggestion else "")
    return out
