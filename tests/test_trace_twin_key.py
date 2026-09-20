"""Every consumer of a trace has to agree on which twin served a call.

A trace event says which twin answered it. The engine writes that under `twin`;
older records carry `_clone` or `clone`, from before the rename. That is fine as
long as everything that reads a trace accepts all of them — and for a while
nothing did. `checkpoint runs trace` read `twin` first and printed the right
thing; the dashboard read only `_clone`, so its Twin column was empty for every
run the current engine produced, and the telemetry report agreed with the
dashboard rather than with the run.

That is the same drift that once made `replay --clone` match nothing. These pin
the two halves: a record written today is readable, and a record written before
the rename still is.
"""
from __future__ import annotations

import pytest

from checkpoint.telemetry import build_telemetry_report

WIRE_NAMES = ["twin", "_twin", "clone", "_clone"]


def _record(event: dict) -> dict:
    return {
        "run_id": "r1",
        "scenario": "file-a-bug.md",
        "trace": [{"method": "POST", "path": "/repos/acme/webapp/issues",
                   "status": 201, **event}],
    }


@pytest.mark.parametrize("key", WIRE_NAMES)
def test_the_twin_survives_into_the_telemetry_report(key: str) -> None:
    """Whichever name the writer used, the reader finds it."""
    report = build_telemetry_report(_record({key: "github"}))

    call = report["api_calls"][0]
    assert call["twin"] == "github", (
        f"a trace event carrying {key!r} lost its twin on the way to the "
        f"dashboard: {call}")


def test_an_event_with_no_twin_is_not_invented() -> None:
    """Absent is absent — a made-up twin is worse than an empty column."""
    call = build_telemetry_report(_record({}))["api_calls"][0]
    assert call["twin"] is None


def test_the_current_engine_writes_the_name_the_readers_prefer() -> None:
    """The producer side of the same agreement, so the two cannot drift apart.

    `Sandbox.trace()` tags each call with the twin that served it. If that key
    ever changes, this fails here rather than silently emptying a column in the
    dashboard.
    """
    import inspect

    from checkpoint.engine import sandbox

    source = inspect.getsource(sandbox)
    assert '"twin": name' in source, (
        "the sandbox no longer tags trace entries with `twin`; every reader "
        "prefers that key, so they need changing together")
