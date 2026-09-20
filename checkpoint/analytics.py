"""Multi-run trend aggregation and flaky criterion detection."""
from __future__ import annotations

import json
from pathlib import Path


def load_runs_for_scenario(
    scenario_pattern: str,
    runs_dir: Path,
    limit: int = 100,
) -> list[dict]:
    """The most recent runs whose scenario name contains ``scenario_pattern``.

    Ordered by the timestamp each run recorded for itself, not by the file's
    mtime. Several runs finishing inside the same second write files whose
    mtimes tie, and a tie leaves the order to the filesystem — so "the last two
    runs" quietly became "two runs", which is the wrong answer to give anyone
    looking for a regression.
    """
    if not runs_dir.exists():
        return []
    found: list[tuple[str, float, dict]] = []
    pattern = scenario_pattern.lower()
    for path in runs_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if pattern and pattern not in (record.get("scenario") or "").lower():
            continue
        stamp = str((record.get("env") or {}).get("timestamp") or "")
        found.append((stamp, mtime, record))
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in found[:limit]]


def compute_trend(runs: list[dict]) -> dict:
    """Aggregate satisfaction trend and per-criterion pass rates across runs."""
    scores = [r.get("satisfaction", 0) for r in runs]
    avg = sum(scores) / len(scores) if scores else 0.0

    crit_stats: dict[str, dict] = {}
    for run in runs:
        for c in run.get("criteria") or []:
            t = c.get("text", "")
            if not t:
                continue
            if t not in crit_stats:
                crit_stats[t] = {"pass": 0, "fail": 0, "kind": c.get("kind", "?")}
            if c.get("passed"):
                crit_stats[t]["pass"] += 1
            else:
                crit_stats[t]["fail"] += 1

    for s in crit_stats.values():
        total = s["pass"] + s["fail"]
        s["pass_rate"] = round(s["pass"] / total, 3) if total else 0.0
        s["total"] = total

    history = [
        {
            "run_id": r.get("run_id", "?"),
            "score": r.get("satisfaction", 0),
            "timestamp": (r.get("env") or {}).get("timestamp", "?"),
        }
        for r in runs
    ]

    return {
        "run_count": len(runs),
        "avg_score": round(avg, 1),
        "min_score": min(scores) if scores else 0,
        "max_score": max(scores) if scores else 0,
        "criteria": crit_stats,
        "history": history,
    }


def detect_flaky(trend: dict, lo: float = 0.2, hi: float = 0.8) -> list[str]:
    """Return criterion texts that sometimes pass and sometimes fail.

    A criterion is flaky if it has >= 3 runs and its pass rate is between lo and hi.
    """
    return [
        t for t, s in trend["criteria"].items()
        if s["total"] >= 3 and lo < s["pass_rate"] < hi
    ]
