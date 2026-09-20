"""The published GitHub Action must stay in sync with the gate CLI.

action.yml is the README's headline CI integration, but it runs on the *user's*
runner against the released package — so a renamed `checkpoint gate` option
would break every consumer with nothing failing in this repo. These tests pin
the contract.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from checkpoint.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION = REPO_ROOT / "action.yml"


@pytest.fixture(scope="module")
def action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def _gate_command():
    """The gate command as the CLI resolves it, not as a module import."""
    import click

    command = main.get_command(click.Context(main), "gate")
    assert command is not None, "`checkpoint gate` is missing from the command table"
    return command


def _gate_option_names() -> set[str]:
    names: set[str] = set()
    for param in _gate_command().params:
        names.update(getattr(param, "opts", []) or [])
        names.update(getattr(param, "secondary_opts", []) or [])
    return names


def test_action_is_well_formed(action):
    assert action["runs"]["using"] == "composite"
    assert "verdict" in action["outputs"]
    # Every declared input must be referenced somewhere in the steps.
    body = ACTION.read_text(encoding="utf-8")
    for name in action["inputs"]:
        assert f"inputs.{name}" in body, f"input '{name}' is declared but never used"


def test_every_gate_flag_in_the_action_exists_in_the_cli():
    """Flags the action passes must be real `checkpoint gate` options."""
    body = ACTION.read_text(encoding="utf-8")
    # Long options appearing inside the args array / gate invocation.
    used = set(re.findall(r"(--[a-z][a-z0-9-]+)", body))
    # Options belonging to other tools in the same file, not to the gate:
    # pip's --upgrade and `checkpoint --version` in the install step.
    ignore = {"--upgrade", "--version"}
    valid = _gate_option_names()
    unknown = {f for f in used - ignore if f not in valid}
    assert not unknown, (
        f"action.yml passes flags that `checkpoint gate` does not define: {sorted(unknown)}"
    )


def test_action_does_not_interpolate_inputs_into_the_shell():
    """Inputs must reach bash via env:, never `${{ }}` inside run: (injection)."""
    body = ACTION.read_text(encoding="utf-8")
    # Collect the `run:` blocks and assert none contain an inputs expression.
    offenders = [
        line.strip()
        for line in body.splitlines()
        if "${{" in line and "inputs." in line and (
            "$(" in line or '"$' in line or line.strip().startswith("checkpoint ")
        )
    ]
    assert not offenders, f"inputs interpolated into shell text: {offenders}"


def test_gate_accepts_the_action_invocation_shape():
    """`checkpoint gate --help` works and exposes the options the action needs."""
    result = CliRunner().invoke(main, ["gate", "--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--command", "--pass-threshold", "--allow-conditional", "--strict",
                 "--certificate", "--model", "--name", "--no-baseline", "--json"):
        assert flag in result.output, f"{flag} missing from `checkpoint gate --help`"


def test_every_action_input_reaches_the_gate_as_a_flag():
    """A renamed action input that nothing passes on is a setting that vanishes.

    Each input either names a gate flag the run step builds, or is one of the
    few that configure the runner itself rather than the gate.
    """
    body = ACTION.read_text(encoding="utf-8")
    action_inputs = set(yaml.safe_load(body)["inputs"])
    # These set up the job, not the verdict: the scenarios to gate, which
    # checkpoint to install, and which interpreter to install it with.
    runner_only = {"target", "version", "python-version"}
    for name in sorted(action_inputs - runner_only):
        flag = f"--{name}" if name != "runs" else "-n"
        assert flag in body, f"input '{name}' never reaches `checkpoint gate` as {flag}"
        assert flag in _gate_option_names(), f"{flag} is not a `checkpoint gate` option"
