"""The run index is a cache; the JSON files are the record.

`checkpoint runs` has no migrate step, because a migration somebody has to
remember to run is one nobody runs. These tests pin both halves of that
promise: the index catches itself up from the files on its own, and a database
that is missing, stale or corrupt never hides a run that was written.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.store import RunStore, SqliteRunStore, default_db_path, migrate_json_runs


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


def _project_with_runs(tmp_path, monkeypatch, *records):
    """A working directory holding JSON run records and no index yet."""
    monkeypatch.delenv("CHECKPOINT_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / ".checkpoint" / "cache" / "runs"
    runs_dir.mkdir(parents=True)
    for record in records:
        (runs_dir / f"{record['run_id']}.json").write_text(json.dumps(record))
    return runs_dir


def test_runs_list_indexes_the_records_with_no_migrate_step(tmp_path, monkeypatch):
    _project_with_runs(tmp_path, monkeypatch,
                       _record("r1", "a.md", 100.0, "2026-08-31T00:00:01"))

    listed = CliRunner().invoke(main, ["runs", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert [r["run_id"] for r in rows] == ["r1"]
    assert rows[0]["score"] == 100.0

    # The index now holds the run, without anyone having asked it to.
    store = SqliteRunStore(default_db_path())
    assert store.get_run("r1")["scenario"] == "a.md"
    store.close()


def test_runs_list_picks_up_a_record_written_after_the_index_was_built(tmp_path, monkeypatch):
    """A stale index must not make a finished run invisible."""
    runs_dir = _project_with_runs(tmp_path, monkeypatch,
                                  _record("r1", "a.md", 100.0, "2026-08-31T00:00:01"))
    CliRunner().invoke(main, ["runs", "list", "--json"])  # builds the index

    (runs_dir / "r2.json").write_text(
        json.dumps(_record("r2", "b.md", 40.0, "2026-08-31T00:00:02")))
    rows = json.loads(CliRunner().invoke(main, ["runs", "list", "--json"]).output)
    assert {r["run_id"] for r in rows} == {"r1", "r2"}


def test_runs_list_falls_back_to_the_files_when_the_index_is_unusable(tmp_path, monkeypatch):
    """SQLite is a convenience. Losing it must not lose the evidence."""
    _project_with_runs(tmp_path, monkeypatch,
                       _record("r1", "a.md", 100.0, "2026-08-31T00:00:01"))
    db = default_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a database")

    listed = CliRunner().invoke(main, ["runs", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert [r["run_id"] for r in json.loads(listed.output)] == ["r1"]
