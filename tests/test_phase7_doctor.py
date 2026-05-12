"""CLI-03: checkpoint doctor."""
from __future__ import annotations

import socket
from pathlib import Path

from click.testing import CliRunner

from checkpoint import diagnostics
from checkpoint.cli import main


def test_run_checks_returns_stable_order(tmp_path):
    checks = diagnostics.run_checks(ports=(8000,), cwd=tmp_path)
    names = [c.name for c in checks]
    assert names[0] == "Python >= 3.11"
    # docker check, ports, openai, mitmproxy, .checkpoint.json
    assert "Docker daemon reachable" in names or "docker SDK importable" in names
    assert "Port 8000 free" in names
    assert "OPENAI_API_KEY set" in names
    assert "mitmproxy importable" in names
    assert ".checkpoint.json present" in names


def test_port_check_detects_occupied_port(tmp_path):
    """Bind a socket, then ensure the port check reports it occupied."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy_port = s.getsockname()[1]
    try:
        checks = diagnostics.run_checks(ports=(busy_port,), cwd=tmp_path)
        port_check = next(c for c in checks if c.name.startswith(f"Port {busy_port}"))
        assert not port_check.ok
        assert port_check.fix is not None
    finally:
        s.close()


def test_doctor_cli_succeeds_when_all_pass(tmp_path, monkeypatch):
    # Force OPENAI_API_KEY present so the gate passes on a CI box too.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    # We don't insist on exit 0 (CI may have no docker daemon), but we DO
    # insist that the table renders and that failures show a fix.
    assert "Check" in result.output
    if result.exit_code != 0:
        assert "Fix the following" in result.output


def test_checkpoint_json_present_check(tmp_path):
    """Informational check: present or absent both report ok=True."""
    chk = diagnostics._check_checkpoint_config(tmp_path)
    assert chk.ok is True
    (tmp_path / ".checkpoint.json").write_text("{}")
    chk2 = diagnostics._check_checkpoint_config(tmp_path)
    assert chk2.ok is True
    assert ".checkpoint.json" in chk2.detail
