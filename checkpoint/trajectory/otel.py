"""Ingest OpenTelemetry GenAI spans into the canonical Trajectory.

Agents instrumented with the OpenTelemetry GenAI semantic conventions emit spans
for model calls (`chat`) and tool calls (`execute_tool`), with `gen_ai.*`
attributes. We map those into trajectory steps so `[T]` criteria and the path
metrics work on an externally-traced agent, not just one that ran against our
twins. We deliberately read a small, stable subset and keep our own schema, since
the conventions are still marked experimental.

Accepts either a list of spans as simple dicts (`{"name", "attributes": {...},
"status": {...}}`) or OTLP-JSON attributes (a list of `{"key", "value": {...}}`).
"""
from __future__ import annotations

from .model import Trajectory, TrajectoryStep


def _attr_value(v):
    if isinstance(v, dict):
        for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if k in v:
                return v[k]
        return next(iter(v.values()), None)
    return v


def _attrs(span: dict) -> dict:
    raw = span.get("attributes")
    if isinstance(raw, dict):
        return raw
    out: dict = {}
    if isinstance(raw, list):  # OTLP-JSON key/value list
        for kv in raw:
            if isinstance(kv, dict) and "key" in kv:
                out[kv["key"]] = _attr_value(kv.get("value"))
    return out


def _is_error(span: dict) -> bool:
    status = span.get("status")
    if isinstance(status, dict):
        code = str(status.get("code", "")).upper()
        return code in ("ERROR", "STATUS_CODE_ERROR", "2")
    return False


def from_otel_spans(spans: list[dict]) -> Trajectory:
    """Map GenAI tool/model spans to trajectory steps (non-GenAI spans ignored)."""
    steps: list[TrajectoryStep] = []
    for i, span in enumerate(spans):
        if not isinstance(span, dict):
            continue
        attrs = _attrs(span)
        op = str(attrs.get("gen_ai.operation.name") or span.get("name", "")).lower()
        tool = attrs.get("gen_ai.tool.name")
        model = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model")

        if tool or "execute_tool" in op:
            method, path = "TOOL", str(tool or op)
        elif model or op in ("chat", "text_completion", "generate_content"):
            method, path = "LLM", str(model or op)
        else:
            continue  # not a GenAI span

        steps.append(TrajectoryStep(
            index=i, method=method, path=path,
            status=500 if _is_error(span) else 200,
        ))
    return Trajectory(steps=steps)
