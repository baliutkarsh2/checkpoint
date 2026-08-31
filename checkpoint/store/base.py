"""The RunStore interface every backend implements."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RunStore(Protocol):
    """Persist and query run records and gate results.

    A record is the same dict `checkpoint.run_record.build_record` produces, so
    a backend needs no schema knowledge beyond a few indexed fields (run_id,
    scenario, score, timestamp) it can pull from the blob.
    """

    def put_run(self, record: dict) -> str:
        """Store a run record; return its run_id."""
        ...

    def get_run(self, run_id: str) -> dict | None:
        """Return the full run record, or None."""
        ...

    def list_runs(self, *, scenario: str | None = None, limit: int = 50) -> list[dict]:
        """Return newest-first run summaries (run_id, scenario, score, timestamp)."""
        ...

    def put_gate(self, gate: dict) -> str:
        """Store a gate result; return its gate_id."""
        ...

    def list_gates(self, *, target: str | None = None, limit: int = 50) -> list[dict]:
        """Return newest-first gate summaries (gate_id, target, verdict, created_at)."""
        ...

    def close(self) -> None:
        ...
