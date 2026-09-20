"""Ingest OpenTelemetry GenAI spans into the canonical Trajectory.

Agents instrumented with the OpenTelemetry GenAI semantic conventions emit spans
for model calls (`chat`) and tool calls (`execute_tool`), with `gen_ai.*`
attributes. We map those into trajectory steps so `[T]` criteria and the path
metrics work on an externally-traced agent, not just one that ran against our
twins. We deliberately read a small, stable subset and keep our own schema, since
the conventions are still marked experimental.

Accepts either a list of spans as simple dicts (`{"name", "attributes": {...},
"status": {...}}`) or OTLP-JSON attributes (a list of `{"key", "value": {...}}`).
Use :func:`spans_from_export` first when what you have is a whole exported file,
which wraps its spans in resource and scope envelopes.
"""
from __future__ import annotations

from typing import Any

from .model import Trajectory, TrajectoryStep


def spans_from_export(data: Any) -> list[dict]:
    """The spans inside whatever an OTLP exporter wrote.

    A collector writes ``{"resourceSpans": [{"scopeSpans": [{"spans": [...]}]}]}``
    — nested twice so one file can carry several services and instrumentation
    libraries. Neither envelope means anything to a single agent's trajectory,
    so they are flattened away here. A bare list, or ``{"spans": [...]}``, is
    passed through, because both are what people hand-write.
    """
    if isinstance(data, list):
        return [span for span in data if isinstance(span, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("spans"), list):
        return [span for span in data["spans"] if isinstance(span, dict)]
    spans: list[dict] = []
    for resource in data.get("resourceSpans") or []:
        if not isinstance(resource, dict):
            continue
        # The field was renamed between OTLP versions; exporters in the wild
        # still write the old one.
        scopes = resource.get("scopeSpans") or resource.get("instrumentationLibrarySpans") or []
        for scope in scopes:
            if isinstance(scope, dict):
                spans.extend(s for s in (scope.get("spans") or []) if isinstance(s, dict))
    return spans


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
