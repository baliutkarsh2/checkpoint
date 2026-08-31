"""Import the legacy JSON run-record cache into a RunStore."""
from __future__ import annotations

import json
from pathlib import Path


def migrate_json_runs(runs_dir: Path, store) -> int:
    """Load every ``<run-id>.json`` under ``runs_dir`` into ``store``.

    Idempotent — re-running just re-writes the same rows. Returns the count of
    records imported.
    """
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return 0
    count = 0
    for path in sorted(runs_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("run_id"):
            store.put_run(record)
            count += 1
    return count
