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


def _gate_option_names() -> set[str]:
    from checkpoint.cli import gate

    names: set[str] = set()
    for param in gate.params:
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
    for flag in ("--harness", "--pass-threshold", "--strict", "--certificate",
                 "--judge-model", "--agent", "--no-baseline"):
        assert flag in result.output, f"{flag} missing from `checkpoint gate --help`"
