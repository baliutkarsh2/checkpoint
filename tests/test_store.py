"""SQLite RunStore: roundtrip, queries, migration, and the `db` CLI."""
from __future__ import annotations

import json

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.store import RunStore, SqliteRunStore, migrate_json_runs


def _record(run_id, scenario, score, ts):
    return {"run_id": run_id, "scenario": scenario, "satisfaction": score,
            "criteria": [], "env": {"timestamp": ts}}


def test_sqlite_store_run_roundtrip(tmp_path):
    store = SqliteRunStore(tmp_path / "s.db")
    store.put_run(_record("r1", "a.md", 100.0, "2026-08-31T00:00:01"))
    store.put_run(_record("r2", "a.md", 50.0, "2026-08-31T00:00:02"))
    store.put_run(_record("r3", "b.md", 80.0, "2026-08-31T00:00:03"))

    got = store.get_run("r1")
    assert got["scenario"] == "a.md" and got["satisfaction"] == 100.0
    assert store.get_run("missing") is None

    # Newest first, filtered.
    a_runs = store.list_runs(scenario="a.md")
    assert [r["run_id"] for r in a_runs] == ["r2", "r1"]
    assert store.count_runs() == 3
    store.close()


def test_sqlite_store_gates(tmp_path):
    store = SqliteRunStore(tmp_path / "s.db")
    store.put_gate({"gate_id": "g1", "target": "scenarios/", "verdict": "SHIP",
                    "created_at": "2026-08-31T00:00:01"})
    gates = store.list_gates(target="scenarios/")
    assert gates[0]["verdict"] == "SHIP"
    store.close()


def test_store_satisfies_protocol(tmp_path):
    assert isinstance(SqliteRunStore(tmp_path / "s.db"), RunStore)


def test_migrate_json_runs(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "r1.json").write_text(json.dumps(_record("r1", "a.md", 90.0, "2026-08-31T00:00:01")))
    (runs_dir / "bad.json").write_text("{not json")
    (runs_dir / "noid.json").write_text(json.dumps({"scenario": "x"}))
    store = SqliteRunStore(tmp_path / "s.db")
    n = migrate_json_runs(runs_dir, store)
    assert n == 1
    assert store.get_run("r1")["scenario"] == "a.md"
    store.close()


def test_db_cli_migrate_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    runs_dir = tmp_path / "cache" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "r1.json").write_text(json.dumps(_record("r1", "a.md", 100.0, "2026-08-31T00:00:01")))
    monkeypatch.setattr("checkpoint.run_record.RUNS_DIR", runs_dir)

    mig = CliRunner().invoke(main, ["db", "migrate"])
    assert mig.exit_code == 0, mig.output
    assert "Imported 1" in mig.output

    listed = CliRunner().invoke(main, ["db", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert rows[0]["run_id"] == "r1" and rows[0]["score"] == 100.0


def test_db_cli_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_HOME", str(tmp_path))
    r = CliRunner().invoke(main, ["db", "path"])
    assert r.exit_code == 0
    assert "checkpoint.db" in r.output
