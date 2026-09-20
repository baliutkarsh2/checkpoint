"""Twins that outlive the command that started them.

A scenario run starts its twins and throws them away; `checkpoint twins start`
leaves one up to build against by hand. The record of what is running is a file
that is written but never trusted — a process can die without telling anyone —
so these tests cover the whole lifecycle, including the two ways it goes wrong:
starting a twin that is already up, and finding an entry whose process is gone.

Starting a twin costs a process and a health check, so the tests that only read
from a live one share a single twin.
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN
from checkpoint.twins import registry, sessions


@pytest.fixture(scope="module")
def running_twin(tmp_path_factory):
    """One github twin, up for as long as this module needs it."""
    sessions_file = tmp_path_factory.mktemp("sessions") / "twins.json"
    entry = sessions.start("github", sessions_file=sessions_file)
    try:
        yield entry, sessions_file
    finally:
        sessions.stop("github", sessions_file=sessions_file)


def test_a_started_twin_serves_its_api_and_is_written_down(running_twin):
    entry, sessions_file = running_twin

    assert entry["pid"] > 0
    assert entry["url"].startswith("http://127.0.0.1:")
    assert entry["mcp_url"].endswith("/mcp/")
    assert entry["token"] == FAKE_GITHUB_TOKEN
    assert "github" in json.loads(sessions_file.read_text())


def test_inspecting_a_running_twin_reports_what_it_holds(running_twin):
    _, sessions_file = running_twin

    info = sessions.inspect("github", sessions_file=sessions_file)

    assert info is not None
    assert info["alive"] is True
    assert "issues" in info["state_keys"]
    assert info["request_count"] >= 0


def test_a_twin_cannot_be_started_twice(tmp_path):
    """Two twins on two ports would leave the second one holding all the state.

    The recorded process is this one, which is certainly alive, so the refusal
    is reached without depending on a second twin having survived. That matters:
    if the check ever reads "not running" the call falls through to spawning a
    real server, and this test would then be measuring process startup on
    whatever machine it happens to run on instead of the rule it is about.
    """
    sessions_file = tmp_path / "twins.json"
    sessions_file.write_text(json.dumps({"github": {
        "pid": os.getpid(),
        "port": 9999,
        "host": "127.0.0.1",
        "started_at": "2020-01-01T00:00:00Z",
        "url": "http://127.0.0.1:9999",
        "mcp_url": "http://127.0.0.1:9999/mcp/",
        "token": FAKE_GITHUB_TOKEN,
    }}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="already running"):
        sessions.start("github", sessions_file=sessions_file)


def test_a_live_process_reads_as_alive_and_a_dead_one_does_not():
    """The check the refusal rests on, and it must never disturb what it asks about."""
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert sessions._alive(child.pid) is True
        assert sessions._alive(os.getpid()) is True
        # Asking did not kill it — on Windows os.kill(pid, 0) would have.
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=30)
    assert sessions._alive(child.pid) is False
    assert sessions._alive(-1) is False


def test_starting_an_unknown_twin_names_the_ones_that_exist(tmp_path):
    with pytest.raises(registry.UnknownTwinError) as raised:
        sessions.start("salesforce", sessions_file=tmp_path / "twins.json")

    assert "github" in str(raised.value)


def test_an_entry_whose_process_is_gone_is_dropped(tmp_path):
    """The file heals itself instead of accumulating ghosts."""
    sessions_file = tmp_path / "twins.json"
    sessions_file.write_text(json.dumps({
        "github": {
            "pid": 999999,  # no such process, so the entry is a leftover
            "port": 9999,
            "host": "127.0.0.1",
            "started_at": "2020-01-01T00:00:00Z",
            "url": "http://127.0.0.1:9999",
            "mcp_url": "http://127.0.0.1:9999/mcp/",
            "token": FAKE_GITHUB_TOKEN,
        }
    }))

    info = sessions.inspect("github", sessions_file=sessions_file)

    assert info is not None and info["alive"] is False
    assert json.loads(sessions_file.read_text()) == {}


def test_stopping_a_twin_that_was_not_running_says_so(tmp_path):
    assert sessions.stop("github", sessions_file=tmp_path / "twins.json") is False


# -- through the command line --------------------------------------------------


def test_the_cli_starts_a_twin_prints_where_it_is_and_stops_it(tmp_path, monkeypatch):
    """The URL and the credential are the reason to start one by hand."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    started = runner.invoke(main, ["twins", "start", "github"])
    try:
        assert started.exit_code == 0, started.output
        assert "http://127.0.0.1:" in started.output
        assert "/mcp/" in started.output
        assert FAKE_GITHUB_TOKEN in started.output

        status = runner.invoke(main, ["twins", "status", "github"])
        assert status.exit_code == 0, status.output
        assert "requests" in status.output
    finally:
        stopped = runner.invoke(main, ["twins", "stop", "github"])

    assert stopped.exit_code == 0, stopped.output
    assert "Stopped github" in stopped.output
    assert sessions.inspect("github", sessions_file=sessions.SESSIONS_FILE) is None


def test_asking_about_a_twin_that_is_not_running_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["twins", "status", "github"])

    assert result.exit_code == 1
    assert "not running" in result.output
