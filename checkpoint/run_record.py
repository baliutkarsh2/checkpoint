"""EV-06: Run-record persistence.

After every ``checkpoint run`` invocation we write:
  - ``.checkpoint/cache/runs/<run-id>.json`` — full run record.
  - ``.checkpoint/cache/last-run.json`` — pointer ``{"run_id": "..."}``.

``<run-id>`` = ``sha256(scenario_path + iso_timestamp)[:12]``.

Schema is documented at the top of ``Plan 05-03``; see that file for the
canonical shape. The cache lives under ``.checkpoint/`` which is in
``.gitignore``.
"""
from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_ROOT = Path(".checkpoint/cache")
RUNS_DIR = CACHE_ROOT / "runs"
LAST_RUN_POINTER = CACHE_ROOT / "last-run.json"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_id(scenario_path: str | None, timestamp: str | None = None) -> str:
    """Stable per-invocation id. ``scenario_path`` may be None (inline task)."""
    src = (scenario_path or "<inline>") + "|" + (timestamp or _utc_iso())
    return hashlib.sha256(src.encode()).hexdigest()[:12]


def _cli_version() -> str:
    try:
        from importlib.metadata import version

        return version("checkpoint")
    except Exception:
        return "0.0.0"


def _truncate_state_for_record(state: dict, max_chars: int = 100_000) -> dict:
    """Run records may grow large with multi-clone state. Cap at 100KB raw."""
    raw = json.dumps(state, default=str)
    if len(raw) <= max_chars:
        return state
    keys = list(state.keys())[:50]
    return {"_truncated": True, "_size": len(raw), "_max": max_chars, "_keys": keys}


def _serialize_criterion(c: Any) -> dict:
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
    trace: list,
    state: dict,
    error: str | None = None,
    exit_code: int = 0,
    metrics: dict | None = None,
    agent_trace: Any = None,
    failure_analysis: dict[str, str] | None = None,
    run_id: str | None = None,
    timestamp: str | None = None,
) -> dict:
    ts = timestamp or _utc_iso()
    rid = run_id or make_run_id(scenario_path, ts)
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
        "trace": trace,
        "state": _truncate_state_for_record(state),
        "error": error,
        "exit_code": exit_code,
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


def write_record(record: dict, *, root: Path | None = None) -> Path:
    """Persist ``record`` and update the last-run pointer.

    Returns the absolute path of the written record.
    """
    cache_root = (root or CACHE_ROOT).resolve()
    runs_dir = cache_root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rid = record["run_id"]
    path = runs_dir / f"{rid}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    pointer = cache_root / "last-run.json"
    pointer.write_text(json.dumps({"run_id": rid, "path": str(path)}, indent=2))
    return path


def load_last_run(root: Path | None = None) -> dict | None:
    cache_root = (root or CACHE_ROOT).resolve()
    pointer = cache_root / "last-run.json"
    if not pointer.exists():
        return None
    try:
        ptr = json.loads(pointer.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rid = ptr.get("run_id")
    if not rid:
        return None
    record_path = cache_root / "runs" / f"{rid}.json"
    if not record_path.exists():
        return None
    try:
        return json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
