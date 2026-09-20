"""An externally traced agent must be readable as a trajectory.

Not every agent runs against Checkpoint's twins; many are already instrumented
with the OpenTelemetry GenAI conventions. These tests pin the small, stable
subset of that vocabulary Checkpoint reads — which spans become steps, which are
ignored, and how an errored span reaches the path metrics — so `[T]` criteria
mean the same thing for a traced agent as for a sandboxed one.
"""
from __future__ import annotations

from checkpoint.trajectory import compute_metrics, from_otel_spans

_SPANS = [
    {"name": "chat", "attributes": {"gen_ai.request.model": "gpt-4o"}},
    {"name": "execute_tool", "attributes": {"gen_ai.tool.name": "get_order"}},
    {"name": "execute_tool", "attributes": {"gen_ai.tool.name": "get_order"},
     "status": {"code": "ERROR"}},   # redundant + error
    {"name": "GET /health"},          # non-GenAI span, ignored
]


def test_model_and_tool_spans_become_steps_and_other_spans_do_not():
    traj = from_otel_spans(_SPANS)
    assert len(traj) == 3  # the non-GenAI span is dropped
    assert traj.steps[0].method == "LLM" and traj.steps[0].path == "gpt-4o"
    assert traj.steps[1].method == "TOOL" and traj.steps[1].path == "get_order"


def test_metrics_count_an_errored_span_and_a_repeated_tool_call():
    m = compute_metrics(from_otel_spans(_SPANS))
    assert m.total_calls == 3
    assert m.error_calls == 1        # the ERROR-status tool span
    assert m.redundant_calls == 1    # get_order twice


def test_otlp_json_key_value_attributes_read_the_same_as_plain_ones():
    """Exported spans carry attributes as a key/value list, not a mapping."""
    span = {"name": "execute_tool", "attributes": [
        {"key": "gen_ai.tool.name", "value": {"stringValue": "issue_refund"}},
    ]}
    traj = from_otel_spans([span])
    assert traj.steps[0].method == "TOOL"
    assert traj.steps[0].path == "issue_refund"


def test_operation_name_identifies_a_span_the_span_name_does_not():
    """Span names are free-form; `gen_ai.operation.name` is the conventional one."""
    spans = [
        {"name": "anthropic.messages.create",
         "attributes": {"gen_ai.operation.name": "chat",
                        "gen_ai.response.model": "claude-sonnet-4"}},
        {"name": "run_tool", "attributes": {"gen_ai.operation.name": "execute_tool"}},
    ]
    traj = from_otel_spans(spans)
    assert [(s.method, s.path) for s in traj.steps] == [
        ("LLM", "claude-sonnet-4"), ("TOOL", "execute_tool")]


def test_an_errored_span_is_never_read_as_a_successful_call():
    """Every spelling OTLP uses for a failed span has to count as one."""
    for code in ("ERROR", "STATUS_CODE_ERROR", "2"):
        span = {"name": "execute_tool", "attributes": {"gen_ai.tool.name": "pay"},
                "status": {"code": code}}
        assert compute_metrics(from_otel_spans([span])).error_calls == 1, code
    ok = {"name": "execute_tool", "attributes": {"gen_ai.tool.name": "pay"},
          "status": {"code": "OK"}}
    assert compute_metrics(from_otel_spans([ok])).error_calls == 0


def test_malformed_spans_are_skipped_rather_than_crashing_the_read():
    """A trace from somebody else's exporter is data, not a contract."""
    traj = from_otel_spans(["not a span", {}, {"attributes": None},
                            {"name": "chat", "attributes": {"gen_ai.request.model": "gpt-4o"}}])
    assert [(s.method, s.path) for s in traj.steps] == [("LLM", "gpt-4o")]
