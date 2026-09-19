"""Persist per-scenario pass rates so the gate can tell a flake from a
regression.

`classify_stability` can flag a *regression* — a real drop versus a known
baseline — but only if it has a baseline worth comparing against. Three rules
keep this honest, and each one fixes a way the old ledger lied:

1. **Only a SHIP updates a baseline.** Saving a "flaky" rate let the ledger
   follow a degrading agent downward — 17/20, then 14/20, then 11/20, each step
   too small to trip the drop threshold and each step rewriting history — so a
   collapsing agent never read as a regression. A baseline is the last rate we
   were *confident* about, or nothing.
2. **Keyed by path relative to the gate target**, not by file name, so
   ``github/smoke.md`` and ``slack/smoke.md`` stop overwriting each other.
3. **Fingerprinted by the scenario's criteria.** Rewrite what "passing" means
   and the old rate is discarded instead of being reported as a regression.

Where the file lives: ``.checkpoint/baselines.json`` under the working
directory, or ``$CHECKPOINT_HOME/baselines.json`` when that is set. The section
key is the gate target's path *relative to the working directory*, so a CI
checkout at a different absolute path still finds its own baselines — the old
scheme hashed the absolute path and therefore never matched in CI. Commit the
file, or restore it with your CI cache; without it every run is a first run.

This is deliberately a flat file behind a tiny interface. A hosted, multi-tenant
store is a drop-in replacement for `load`/`save` later.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..scenario import Scenario
    from .verdict import ScenarioStat


@dataclass(frozen=True)
class Baseline:
    """A recorded pass rate plus what it was measured against."""

    pass_rate: float
    criteria_hash: str = ""
    """Fingerprint of the criteria in force when the rate was recorded. Empty
    means "unknown" (a file written by an older version): still usable, because
    refusing to compare would silently disable regression detection."""
    n: int = 0
    updated_at: str = ""

    def applies_to(self, criteria_hash: str) -> bool:
        """True when this baseline measured the same success criteria."""
        return not self.criteria_hash or not criteria_hash or self.criteria_hash == criteria_hash


def criteria_hash(scenario: Scenario) -> str:
    """Fingerprint of what "passing" means for a scenario.

    Kind and whitespace-normalized text of every criterion, in order. Reflowing
    a criterion does not change the fingerprint; changing, adding or removing
    one does — and that invalidates the baseline, because a rewritten scenario
    measures something else and its drop is not a regression.
    """
    payload = "\n".join(f"{c.kind}:{' '.join(c.text.split())}" for c in scenario.criteria)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def scenario_key(target: Path, path: Path) -> str:
    """Stable per-scenario key: POSIX path relative to the gate target."""
    target, path = Path(target), Path(path)
    base = target if target.is_dir() else target.parent
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        # Outside the target (only reachable if a caller hands us a stray path):
        # the file name is the best key available.
        return path.name


def _store_dir() -> Path:
    # Project-local, next to run records; overridable for tests/CI.
    override = os.environ.get("CHECKPOINT_HOME")
    return Path(override) if override else Path.cwd() / ".checkpoint"


def baseline_path() -> Path:
    return _store_dir() / "baselines.json"


def _target_key(target: Path) -> str:
    """Section key for a gate target: its path relative to the working directory.

    Readable and reproducible across machines. A target outside the working
    directory falls back to its name — good enough for a project-local ledger,
    and still independent of where the repository happens to be checked out.
    """
    resolved = Path(target).resolve()
    try:
        rel = resolved.relative_to(Path.cwd().resolve())
    except ValueError:
        return resolved.name
    return rel.as_posix() or "."


def _read_all() -> dict:
    path = baseline_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(target: Path) -> dict[str, Baseline]:
    """Return ``{scenario_key: Baseline}`` recorded for this target, if any."""
    section = _read_all().get(_target_key(target), {})
    out: dict[str, Baseline] = {}
    if not isinstance(section, dict):
        return out
    for key, rec in section.items():
        if not isinstance(rec, dict) or not isinstance(rec.get("pass_rate"), (int, float)):
            continue
        out[key] = Baseline(
            pass_rate=float(rec["pass_rate"]),
            criteria_hash=str(rec.get("criteria_hash") or ""),
            n=int(rec.get("n") or 0),
            updated_at=str(rec.get("updated_at") or ""),
        )
    return out


def save(target: Path, stats: Iterable[ScenarioStat]) -> list[str]:
    """Record the pass rate of every *confidently passing* scenario.

    Returns the keys written. Anything that is not a ``stable_pass`` keeps its
    previous baseline: a ledger that follows a degrading agent downward can
    never detect the degradation it exists to catch. Callers additionally only
    reach here on a SHIP verdict — this filter is the second lock on the door.
    """
    keep = [s for s in stats if s.classification == "stable_pass"]
    if not keep:
        return []
    all_data = _read_all()
    now = datetime.datetime.now(datetime.UTC).isoformat()
    section = all_data.setdefault(_target_key(target), {})
    for s in keep:
        section[s.scenario] = {
            "pass_rate": round(s.pass_rate, 4),
            "n": s.n,
            "criteria_hash": s.criteria_hash,
            "classification": s.classification,
            "updated_at": now,
        }
    path = baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(all_data, indent=2, sort_keys=True), encoding="utf-8")
    return [s.scenario for s in keep]
