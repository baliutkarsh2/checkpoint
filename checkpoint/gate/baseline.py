"""Persist per-scenario pass rates so the gate can tell a flake from a
regression.

`classify_stability` can flag a *regression* — a real drop versus a known
baseline — but only if it has a baseline to compare against. We keep a small
JSON ledger of the last pass rate per scenario (per gate target), read it before
a run, and update it after. First run of a scenario has no baseline, so it can
only be stable/flaky; once history exists, a sudden drop reads as a regression.

This is deliberately a flat file behind a tiny interface. A hosted, multi-tenant
store is a drop-in replacement for `load`/`save` later.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path


def _store_dir() -> Path:
    # Project-local, next to run records; overridable for tests/CI.
    override = os.environ.get("CHECKPOINT_HOME")
    base = Path(override) if override else Path.cwd() / ".checkpoint"
    return base


def baseline_path() -> Path:
    return _store_dir() / "baselines.json"


def _target_key(target: Path) -> str:
    # A stable, filesystem-independent key for a gate target.
    return hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:16]


def _read_all() -> dict:
    path = baseline_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(target: Path) -> dict[str, float]:
    """Return ``{scenario_name: pass_rate}`` recorded for this target, if any."""
    section = _read_all().get(_target_key(target), {})
    out: dict[str, float] = {}
    for name, rec in section.items():
        if isinstance(rec, dict) and isinstance(rec.get("pass_rate"), (int, float)):
            out[name] = float(rec["pass_rate"])
    return out


def save(target: Path, stats) -> None:
    """Record the pass rate of each ScenarioStat as the new baseline."""
    all_data = _read_all()
    now = datetime.datetime.now(datetime.UTC).isoformat()
    section = all_data.setdefault(_target_key(target), {})
    for s in stats:
        section[s.scenario] = {
            "pass_rate": round(s.pass_rate, 4),
            "n": s.n,
            "classification": s.classification,
            "updated_at": now,
        }
    path = baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(all_data, indent=2, sort_keys=True), encoding="utf-8")
