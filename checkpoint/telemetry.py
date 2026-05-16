"""Run telemetry normalization.

The run record is the durable source of truth. This module builds a stable,
dashboard-friendly report from records of different ages and harness styles.
It is deliberately tolerant of arbitrary ``agent_trace`` shapes: harnesses can
write whatever they know, and Checkpoint will surface the useful chat/tool
fragments without throwing away the original raw payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Found:
    path: str
    value: Any


_MESSAGE_KEYS = {"messages", "conversation", "chat", "transcript"}
_EVENT_KEYS = {"events", "steps", "spans", "trace", "timeline"}
_TOOL_KEYS = {"tool_calls", "toolCalls", "tools", "calls"}


def build_telemetry_report(record: dict) -> dict:
    """Return a normalized full-process report for a persisted run record."""
    trace = _as_list(record.get("trace"))
    criteria = _as_list(record.get("criteria"))
    metrics = record.get("metrics") or {}
    agent_trace = record.get("agent_trace")
    stdout = record.get("stdout")
    stderr = record.get("stderr")
    if stdout is None:
        stdout = record.get("final_answer") or ""
    if stderr is None:
        stderr = ""

    chat_messages = _extract_messages(agent_trace)
    agent_steps = _extract_agent_steps(agent_trace)
    agent_tool_calls = _extract_tool_calls(agent_trace)
    api_calls = [_normalize_api_call(i, ev) for i, ev in enumerate(trace)]
    judge_steps = [_normalize_judge_step(i, c) for i, c in enumerate(criteria)]

    timeline = _build_timeline(
        record=record,
        api_calls=api_calls,
        chat_messages=chat_messages,
        agent_steps=agent_steps,
        agent_tool_calls=agent_tool_calls,
        judge_steps=judge_steps,
    )

    return {
        "run_id": record.get("run_id"),
        "summary": _summary(record, trace, criteria, chat_messages, agent_steps, agent_tool_calls),
        "cli": _cli_commands(record),
        "chat": {
            "messages": chat_messages,
            "raw": agent_trace,
            "capture_note": (
                "Agent chat and model reasoning are shown when the harness writes "
                "ARCHAL_AGENT_TRACE_FILE. Hidden provider internals are not present "
                "unless the agent explicitly emits a summary or trace event."
            ),
        },
        "transcript": {
            "stdout": stdout,
            "stderr": stderr,
            "final_answer": record.get("final_answer") or "",
            "error": record.get("error"),
            "exit_code": record.get("exit_code"),
        },
        "steps": agent_steps,
        "tool_calls": agent_tool_calls,
        "api_calls": api_calls,
        "judge": {
            "model": record.get("evaluator_model"),
            "model_source": record.get("evaluator_model_source"),
            "criteria": judge_steps,
            "failure_analysis": record.get("failure_analysis") or {},
        },
        "metrics": _normalize_metrics(metrics, record, api_calls, agent_tool_calls),
        "timeline": timeline,
        "state": record.get("state") or {},
        "raw": {
            "record": record,
            "agent_trace": agent_trace,
        },
    }


def _summary(
    record: dict,
    trace: list,
    criteria: list,
    chat_messages: list[dict],
    agent_steps: list[dict],
    agent_tool_calls: list[dict],
) -> dict:
    passed = sum(1 for c in criteria if isinstance(c, dict) and c.get("passed"))
    return {
        "scenario": record.get("scenario"),
        "scenario_path": record.get("scenario_path"),
        "satisfaction": float(record.get("satisfaction") or 0),
        "criteria_passed": passed,
        "criteria_total": len(criteria),
        "api_call_count": len(trace),
        "agent_message_count": len(chat_messages),
        "agent_step_count": len(agent_steps),
        "tool_call_count": len(agent_tool_calls),
        "duration_ms": record.get("duration_ms"),
        "timestamp": (record.get("env") or {}).get("timestamp"),
        "exit_code": record.get("exit_code"),
        "harness": record.get("harness") or {},
    }


def _cli_commands(record: dict) -> dict:
    run_id = record.get("run_id") or "<run-id>"
    scenario_path = record.get("scenario_path") or "<scenario.md>"
    harness = record.get("harness") or {}
    replay_base = f"checkpoint replay {run_id}"
    commands = {
        "detail": f"checkpoint traces detail {run_id}",
        "telemetry": f"checkpoint traces telemetry {run_id}",
        "export": f"checkpoint traces export {run_id} --output {run_id}.json",
        "replay": replay_base,
        "replay_json": f"{replay_base} --json",
        "serve": "checkpoint serve",
    }
    run_parts = ["checkpoint", "run", scenario_path]
    if harness.get("mode") == "docker":
        run_parts.append("--docker")
        if harness.get("dir"):
            run_parts.extend(["--harness-dir", str(harness["dir"])])
    elif harness.get("cmd"):
        run_parts.extend(["--harness", str(harness["cmd"]), "--no-docker"])
    commands["rerun"] = " ".join(run_parts)
    return commands


def _normalize_metrics(metrics: dict, record: dict, api_calls: list, tool_calls: list) -> dict:
    duration_ms = record.get("duration_ms")
    prompt_tokens = _first_number(metrics, "inputTokens", "input_tokens", "prompt_tokens")
    completion_tokens = _first_number(metrics, "outputTokens", "output_tokens", "completion_tokens")
    total_tokens = _first_number(metrics, "totalTokens", "total_tokens")
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    return {
        "raw": metrics,
        "duration_ms": duration_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "llm_call_count": _first_number(metrics, "llmCallCount", "llm_call_count", "model_calls"),
        "tool_call_count": _first_number(metrics, "toolCallCount", "tool_call_count") or len(tool_calls),
        "api_call_count": len(api_calls),
        "error_count": sum(1 for call in api_calls if int(call.get("status") or 0) >= 400),
    }


def _normalize_api_call(index: int, event: Any) -> dict:
    ev = event if isinstance(event, dict) else {"raw": event}
    return {
        "index": index,
        "clone": ev.get("_clone") or ev.get("clone"),
        "method": ev.get("method") or ev.get("type") or "UNKNOWN",
        "path": ev.get("path") or ev.get("url") or "",
        "status": ev.get("status") or ev.get("status_code"),
        "timestamp": ev.get("timestamp") or ev.get("ts"),
        "duration_ms": ev.get("duration_ms") or ev.get("elapsed_ms"),
        "request_body": ev.get("request_body"),
        "response_body": ev.get("response_body"),
        "raw": ev,
    }


def _normalize_judge_step(index: int, criterion: Any) -> dict:
    c = criterion if isinstance(criterion, dict) else {"raw": criterion}
    return {
        "index": index,
        "kind": c.get("kind"),
        "text": c.get("text") or "",
        "passed": bool(c.get("passed")),
        "evaluator": c.get("evaluator"),
        "reasoning": c.get("reasoning"),
        "raw": c,
    }


def _build_timeline(
    *,
    record: dict,
    api_calls: list[dict],
    chat_messages: list[dict],
    agent_steps: list[dict],
    agent_tool_calls: list[dict],
    judge_steps: list[dict],
) -> list[dict]:
    out: list[dict] = []
    ts = (record.get("env") or {}).get("timestamp")
    out.append({
        "kind": "run",
        "label": "Run started",
        "timestamp": ts,
        "status": "ok" if not record.get("error") else "error",
        "detail": record.get("scenario"),
    })
    for msg in chat_messages:
        out.append({
            "kind": "chat",
            "label": f"{msg.get('role') or 'message'}",
            "timestamp": msg.get("timestamp"),
            "status": "ok",
            "detail": _brief(msg.get("content") or msg.get("text") or ""),
            "ref": {"section": "chat", "index": msg.get("index")},
        })
    for step in agent_steps:
        out.append({
            "kind": "agent",
            "label": step.get("name") or step.get("type") or "Agent step",
            "timestamp": step.get("timestamp"),
            "status": step.get("status") or "ok",
            "detail": _brief(step.get("summary") or step.get("content") or ""),
            "ref": {"section": "steps", "index": step.get("index")},
        })
    for call in agent_tool_calls:
        out.append({
            "kind": "tool",
            "label": call.get("name") or "Tool call",
            "timestamp": call.get("timestamp"),
            "status": call.get("status") or "ok",
            "detail": _brief(call.get("summary") or call.get("input") or ""),
            "ref": {"section": "tool_calls", "index": call.get("index")},
        })
    for call in api_calls:
        status = call.get("status")
        out.append({
            "kind": "api",
            "label": f"{call.get('method')} {call.get('path')}",
            "timestamp": call.get("timestamp"),
            "status": "error" if isinstance(status, int) and status >= 400 else "ok",
            "detail": f"{status or '-'} {call.get('clone') or ''}".strip(),
            "ref": {"section": "api_calls", "index": call.get("index")},
        })
    for step in judge_steps:
        out.append({
            "kind": "judge",
            "label": f"[{step.get('kind')}] {step.get('text')}",
            "timestamp": None,
            "status": "ok" if step.get("passed") else "error",
            "detail": _brief(step.get("reasoning") or ""),
            "ref": {"section": "judge", "index": step.get("index")},
        })
    out.append({
        "kind": "run",
        "label": "Run ended",
        "timestamp": None,
        "status": "ok" if record.get("exit_code") == 0 and not record.get("error") else "error",
        "detail": f"exit {record.get('exit_code')}",
    })
    return out


def _extract_messages(agent_trace: Any) -> list[dict]:
    found = _find_keyed_lists(agent_trace, _MESSAGE_KEYS)
    messages: list[dict] = []
    for item in found:
        for value in _as_list(item.value):
            msg = _message_from_value(value)
            if msg:
                msg["source_path"] = item.path
                msg["index"] = len(messages)
                messages.append(msg)
    if messages:
        return messages
    # Fallback: scan any event that looks like a chat message.
    for item in _find_event_like(agent_trace):
        msg = _message_from_value(item.value)
        if msg:
            msg["source_path"] = item.path
            msg["index"] = len(messages)
            messages.append(msg)
    return messages


def _extract_agent_steps(agent_trace: Any) -> list[dict]:
    steps: list[dict] = []
    for found in _find_keyed_lists(agent_trace, _EVENT_KEYS):
        for value in _as_list(found.value):
            if not isinstance(value, dict):
                continue
            if _message_from_value(value):
                continue
            steps.append({
                "index": len(steps),
                "source_path": found.path,
                "type": value.get("type") or value.get("event") or value.get("kind"),
                "name": value.get("name") or value.get("title"),
                "status": value.get("status"),
                "timestamp": value.get("timestamp") or value.get("ts"),
                "summary": value.get("summary") or value.get("reasoning") or value.get("content") or value.get("text"),
                "raw": value,
            })
    return steps


def _extract_tool_calls(agent_trace: Any) -> list[dict]:
    calls: list[dict] = []
    for found in _find_keyed_lists(agent_trace, _TOOL_KEYS):
        for value in _as_list(found.value):
            call = _tool_call_from_value(value, found.path, len(calls))
            if call:
                calls.append(call)
    for found in _find_event_like(agent_trace):
        if len(calls) > 5000:
            break
        call = _tool_call_from_value(found.value, found.path, len(calls))
        if call and not _already_has_tool(calls, call):
            calls.append(call)
    return calls


def _message_from_value(value: Any) -> dict | None:
    if isinstance(value, str):
        return {"role": "message", "content": value}
    if not isinstance(value, dict):
        return None
    role = value.get("role") or value.get("speaker") or value.get("author")
    content = value.get("content")
    if content is None:
        content = value.get("text") or value.get("message")
    if role is None and content is None:
        return None
    return {
        "role": role or value.get("type") or "message",
        "content": _content_to_text(content),
        "timestamp": value.get("timestamp") or value.get("ts"),
        "name": value.get("name"),
        "raw": value,
    }


def _tool_call_from_value(value: Any, path: str, index: int) -> dict | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("type") or value.get("event") or value.get("kind") or "").lower()
    fn = value.get("function")
    fn_name = fn.get("name") if isinstance(fn, dict) else None
    name = (
        value.get("tool")
        or value.get("tool_name")
        or value.get("name")
        or fn_name
    )
    if not name and "tool" not in kind and "function" not in kind:
        return None
    return {
        "index": index,
        "source_path": path,
        "name": name or value.get("id") or "tool",
        "type": value.get("type") or value.get("event") or value.get("kind"),
        "status": value.get("status"),
        "timestamp": value.get("timestamp") or value.get("ts"),
        "input": value.get("input") or value.get("arguments") or value.get("args"),
        "output": value.get("output") or value.get("result") or value.get("response"),
        "summary": value.get("summary") or value.get("text"),
        "raw": value,
    }


def _find_keyed_lists(value: Any, keys: set[str], path: str = "$", depth: int = 0) -> list[_Found]:
    if depth > 8:
        return []
    out: list[_Found] = []
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{path}.{k}"
            if k in keys and isinstance(v, list):
                out.append(_Found(child, v))
            out.extend(_find_keyed_lists(v, keys, child, depth + 1))
    elif isinstance(value, list):
        for i, v in enumerate(value[:5000]):
            out.extend(_find_keyed_lists(v, keys, f"{path}[{i}]", depth + 1))
    return out


def _find_event_like(value: Any, path: str = "$", depth: int = 0) -> list[_Found]:
    if depth > 8:
        return []
    out: list[_Found] = []
    if isinstance(value, dict):
        if any(k in value for k in ("role", "content", "tool", "tool_name", "function")):
            out.append(_Found(path, value))
        for k, v in value.items():
            out.extend(_find_event_like(v, f"{path}.{k}", depth + 1))
    elif isinstance(value, list):
        for i, v in enumerate(value[:5000]):
            out.extend(_find_event_like(v, f"{path}[{i}]", depth + 1))
    return out


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        out: list = []
        for clone, events in value.items():
            for ev in _as_list(events):
                if isinstance(ev, dict) and "_clone" not in ev:
                    out.append({**ev, "_clone": clone})
                else:
                    out.append(ev)
        return out
    return [value]


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _first_number(d: dict, *keys: str) -> int | float | None:
    for key in keys:
        value = d.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _brief(value: Any, limit: int = 220) -> str:
    text = _content_to_text(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _already_has_tool(calls: list[dict], call: dict) -> bool:
    raw = call.get("raw")
    return any(c.get("raw") is raw for c in calls)
