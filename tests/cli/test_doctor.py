"""What `checkpoint doctor` calls a problem, and what it only mentions.

The distinction is the whole command. A red row has to mean something you
genuinely cannot do yet; a judge model with no key is not that, because a
scenario whose criteria are all assertions never calls one. Get that wrong in
either direction and doctor becomes either a liar or noise, so the tests here
are mostly about which rows decide the exit code.
"""
from __future__ import annotations

import pytest

from checkpoint import diagnostics
from checkpoint.diagnostics import Check

CONFIG = "checkpoint.toml"


@pytest.fixture
def checks_recorder(monkeypatch):
    """Answer with fixed rows, and remember how doctor asked for them."""

    class Recorder:
        def __init__(self) -> None:
            self.kwargs: dict = {}
            self.rows: list[Check] = [Check(name="Python 3.11 or newer", ok=True, detail="3.12")]

        def __call__(self, **kwargs):
            self.kwargs = kwargs
            return self.rows

    recorder = Recorder()
    monkeypatch.setattr(diagnostics, "run_checks", recorder)
    return recorder


# -- which rows decide the exit code -------------------------------------------


def test_a_failing_advisory_row_does_not_fail_the_command(checks_recorder, run_cli):
    checks_recorder.rows.append(Check(
        name="Judge model", ok=False, required=False,
        detail="gpt-5.6-luna needs OPENAI_API_KEY", fix="export OPENAI_API_KEY=..."))

    result = run_cli("doctor")

    assert result.exit_code == 0, result.output
    assert "Ready." in result.output
    assert "optional" in result.output


def test_a_failing_required_row_fails_the_command(checks_recorder, run_cli):
    checks_recorder.rows.append(Check(
        name="Agent command", ok=False, detail="'my-agent' is not on PATH",
        fix="Install 'my-agent', or fix [agent] command in checkpoint.toml"))

    result = run_cli("doctor")

    assert result.exit_code == 1
    assert "not on PATH" in result.output
    assert "Install 'my-agent'" in result.output


def test_all_passed_ignores_advisory_rows(checks_recorder):
    advisory_failure = [Check(name="Judge model", ok=False, detail="", required=False)]
    required_failure = [Check(name="Twins", ok=False, detail="")]

    assert diagnostics.all_passed(advisory_failure) is True
    assert diagnostics.all_passed(required_failure) is False


def test_quick_skips_starting_a_twin(checks_recorder, run_cli):
    """Starting one is the honest check and also the slow one, so it is opt-out."""
    run_cli("doctor", "--quick")
    assert checks_recorder.kwargs["include_twins"] is False

    run_cli("doctor")
    assert checks_recorder.kwargs["include_twins"] is True


# -- the checks themselves -----------------------------------------------------


def test_the_checks_come_back_in_a_stable_order(tmp_path):
    """The output is meant to be comparable between two machines and two days."""
    first = diagnostics.run_checks(cwd=tmp_path, include_twins=False)
    second = diagnostics.run_checks(cwd=tmp_path, include_twins=False)

    assert [c.name for c in first] == [c.name for c in second]
    assert first[0].name.startswith("Python")


def test_this_machine_can_intercept_tls(tmp_path):
    """Minting a CA and binding a listener is what every intercepted run does."""
    proxy = next(c for c in diagnostics.run_checks(cwd=tmp_path, include_twins=False)
                 if c.name == "TLS interception")

    assert proxy.ok, proxy.detail
    assert proxy.required is True


def test_a_directory_with_no_config_is_reported_without_failing(tmp_path):
    checks = diagnostics.run_checks(cwd=tmp_path, include_twins=False)

    config = next(c for c in checks if c.name == CONFIG)
    assert config.required is False
    assert "checkpoint init" in (config.fix or "")
    assert diagnostics.all_passed(checks) is True


def test_a_config_that_cannot_be_used_is_a_failure(tmp_path):
    (tmp_path / CONFIG).write_text("[nonsense]\nkey = 1\n", encoding="utf-8")

    checks = diagnostics.run_checks(cwd=tmp_path, include_twins=False)

    config = next(c for c in checks if c.name == CONFIG)
    assert config.ok is False and config.required is True
    assert diagnostics.all_passed(checks) is False


def test_an_agent_command_that_is_not_installed_is_a_failure(tmp_path):
    (tmp_path / CONFIG).write_text(
        '[agent]\ncommand = "definitely-not-installed-xyz run"\n', encoding="utf-8")

    checks = diagnostics.run_checks(cwd=tmp_path, include_twins=False)

    command = next(c for c in checks if c.name == "Agent command")
    assert command.ok is False and command.required is True
    assert "definitely-not-installed-xyz" in command.detail


def test_a_missing_judge_key_is_only_advisory(tmp_path, monkeypatch):
    """Assertion-only scenarios never call a judge, so this cannot block a run."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHECKPOINT_LLM_BASE_URL", raising=False)
    (tmp_path / CONFIG).write_text('[judge]\nmodel = "gpt-5.6-luna"\n', encoding="utf-8")

    checks = diagnostics.run_checks(cwd=tmp_path, include_twins=False)

    judge = next(c for c in checks if c.name == "Judge model")
    assert judge.ok is False and judge.required is False
    assert "OPENAI_API_KEY" in judge.detail
    assert diagnostics.all_passed(checks) is True


def test_doctor_is_happy_with_a_set_up_project_and_no_judge_key(project, run_cli, monkeypatch):
    """The end-to-end case: a real project, a real proxy self-test, no API key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHECKPOINT_LLM_BASE_URL", raising=False)

    result = run_cli("doctor", "--quick")

    assert result.exit_code == 0, result.output
    assert "Ready." in result.output
