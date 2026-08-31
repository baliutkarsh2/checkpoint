"""SQLite-backed RunStore: one indexed file, no server."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path


def default_db_path() -> Path:
    override = os.environ.get("CHECKPOINT_HOME")
    base = Path(override) if override else Path.cwd() / ".checkpoint"
    return base / "checkpoint.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id    TEXT PRIMARY KEY,
    scenario  TEXT,
    score     REAL,
    timestamp TEXT,
    blob      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON runs(scenario);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(timestamp);

CREATE TABLE IF NOT EXISTS gates (
    gate_id    TEXT PRIMARY KEY,
    target     TEXT,
    verdict    TEXT,
    created_at TEXT,
    blob       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gates_target ON gates(target);
"""


class SqliteRunStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- runs ---------------------------------------------------------------

    def put_run(self, record: dict) -> str:
        rid = record.get("run_id") or uuid.uuid4().hex[:12]
        ts = (record.get("env") or {}).get("timestamp")
        self._conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, scenario, score, timestamp, blob) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, record.get("scenario"), record.get("satisfaction"), ts, json.dumps(record)),
        )
        self._conn.commit()
        return rid

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT blob FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self, *, scenario: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT run_id, scenario, score, timestamp FROM runs"
        params: list = []
        if scenario:
            q += " WHERE scenario = ?"
            params.append(scenario)
        q += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(q, params).fetchall()
        return [
            {"run_id": r[0], "scenario": r[1], "score": r[2], "timestamp": r[3]}
            for r in rows
        ]

    # -- gates --------------------------------------------------------------

    def put_gate(self, gate: dict) -> str:
        gid = gate.get("gate_id") or uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT OR REPLACE INTO gates (gate_id, target, verdict, created_at, blob) "
            "VALUES (?, ?, ?, ?, ?)",
            (gid, gate.get("target"), gate.get("verdict"), gate.get("created_at"), json.dumps(gate)),
        )
        self._conn.commit()
        return gid

    def list_gates(self, *, target: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT gate_id, target, verdict, created_at FROM gates"
        params: list = []
        if target:
            q += " WHERE target = ?"
            params.append(target)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(q, params).fetchall()
        return [
            {"gate_id": r[0], "target": r[1], "verdict": r[2], "created_at": r[3]}
            for r in rows
        ]

    def count_runs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
