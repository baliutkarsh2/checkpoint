"""Run records: one JSON file per agent run.

Every run is written to ``.checkpoint/cache/runs/<run-id>.json`` and
``.checkpoint/cache/last-run.json`` points at the newest one. These files feed
the dashboard, every `checkpoint runs` subcommand, and CI artifacts.
"""
from __future__ import annotations

import json
import platform
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CACHE_ROOT = Path(".checkpoint/cache")
RUNS_DIR = CACHE_ROOT / "runs"
LAST_RUN_POINTER = CACHE_ROOT / "last-run.json"


def _utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_id() -> str:
    """A fresh, unique run id (runs of one scenario can start in the same second)."""
    return uuid.uuid4().hex[:12]


def _cli_version() -> str:
    from checkpoint import __version__

    return __version__


def _truncate_state_for_record(state: dict, max_chars: int = 100_000) -> dict:
    """Run records may grow large with multi-clone state. Cap at 100KB raw."""
    raw = json.dumps(state, default=str)
    if len(raw) <= max_chars:
        return state
    keys = list(state.keys())[:50]
    return {"_truncated": True, "_size": len(raw), "_max": max_chars, "_keys": keys}


def _serialize_criterion(c: Any) -> dict:
    """A criterion in the shape the record stores.

    A plain dict passes through unchanged. It used to fall to the last branch
    and be stored as `{"raw": "<the dict, stringified>"}` — a silent data loss
    that no caller could see until it read the record back.
    """
    if isinstance(c, dict):
        return dict(c)
    if is_dataclass(c):
        return asdict(c)
    if hasattr(c, "__dict__"):
        return dict(c.__dict__)
    return {"raw": str(c)}


def build_record(
    *,
    scenario_name: str,
    scenario_path: str | None,
    satisfaction: float,
    criteria: list,
    evaluator_model: str,
    evaluator_model_source: str,
    final_answer: str,
    stdout: str | None = None,
    stderr: str | None = None,
    trace: list,
    state: dict,
    error: str | None = None,
    exit_code: int = 0,
    metrics: dict | None = None,
    agent_trace: Any = None,
    failure_analysis: dict[str, str] | None = None,
    run_id: str | None = None,
    timestamp: str | None = None,
    agent: dict | None = None,
    duration_ms: float | None = None,
    warnings: list[str] | None = None,
    egress: list[dict] | None = None,
    twins: list[str] | None = None,
    gate_id: str | None = None,
) -> dict:
    ts = timestamp or _utc_iso()
    rid = run_id or make_run_id()
    record: dict = {
        "run_id": rid,
        "scenario": scenario_name,
        "scenario_path": scenario_path,
        "satisfaction": satisfaction,
        "criteria": [_serialize_criterion(c) for c in criteria],
        "evaluator_model": evaluator_model,
        "evaluator_model_source": evaluator_model_source,
        "failure_analysis": failure_analysis or None,
        "final_answer": final_answer,
        "stdout": stdout,
        "stderr": stderr,
        "trace": trace,
        "state": _truncate_state_for_record(state),
        "error": error,
        "exit_code": exit_code,
        # {name, cmd} — what was run. Still written under the old key as well,
        # so a records directory written before the rename keeps opening.
        "agent": agent,
        "harness": agent,
        "duration_ms": duration_ms,
        "twins": twins or [],
        "warnings": warnings or [],
        "egress": egress or [],
        "gate_id": gate_id,
        "env": {
            "timestamp": ts,
            "host": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cli_version": _cli_version(),
        },
    }
    if metrics is not None:
        record["metrics"] = metrics
    if agent_trace is not None:
        record["agent_trace"] = agent_trace
    return record


def write_record(record: dict, *, root: Path | None = None, pointer: bool = True) -> Path:
    """Persist ``record``, and by default point "the last run" at it.

    ``pointer=False`` for runs that arrive in bulk. A gate writes sixteen runs
    per scenario, often from several threads at once: moving the pointer for
    each would race on one file, and "the last run" would end up meaning an
    arbitrary member of the batch rather than the run somebody just did by hand.

    Returns the absolute path of the written record.
    """
    cache_root = (root or CACHE_ROOT).resolve()
    runs_dir = cache_root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rid = record["run_id"]
    path = runs_dir / f"{rid}.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    if pointer:
        last = cache_root / "last-run.json"
        last.write_text(json.dumps({"run_id": rid, "path": str(path)}, indent=2),
                        encoding="utf-8")
    return path


def load_last_run(root: Path | None = None) -> dict | None:
    cache_root = (root or CACHE_ROOT).resolve()
    pointer = cache_root / "last-run.json"
    if not pointer.exists():
        return None
    try:
        ptr = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rid = ptr.get("run_id")
    if not rid:
        return None
    record_path = cache_root / "runs" / f"{rid}.json"
    if not record_path.exists():
        return None
    try:
        return json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
