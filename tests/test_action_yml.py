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


def test_ci_still_defines_every_check_main_requires():
    """A renamed job silently blocks every merge to main.

    Branch protection names its required checks as strings. Rename a job and
    the check it reported simply stops existing: the pull request waits forever
    for a status nobody will ever send, with no error anywhere to explain it.
    This happened — `Build SPA + run pytest + verify wheel` was renamed to put
    the steps in a truthful order, and main became unmergeable.

    The names are listed here rather than fetched so the test needs no network
    and no token. If protection changes, this list changes with it.
    """
    import yaml

    required = {
        "Build SPA + run pytest + verify wheel",
        "Validate the GitHub Action",
        "Lint (ruff)",
    }
    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/checkpoint-ci.yml").read_text(
        encoding="utf-8"))
    defined = {spec.get("name", job) for job, spec in ci["jobs"].items()}

    missing = sorted(required - defined)
    assert not missing, (
        f"branch protection on main requires these checks and CI no longer "
        f"defines them, so no pull request can merge: {missing}")

    # gitleaks is required too, and lives in its own workflow.
    secrets = yaml.safe_load((REPO_ROOT / ".github/workflows/gitleaks.yml").read_text(
        encoding="utf-8"))
    assert "gitleaks" in {job for job in secrets["jobs"]}, (
        "main requires a `gitleaks` check; the secret-scan workflow no longer has one")


def test_the_image_builds_the_spa_on_the_node_ci_tests():
    """Three places name a Node major, and they have to be the same one.

    The Dockerfile builds the dashboard from source rather than trusting the
    committed bundle, which is right — and useless if it builds on a different
    major than CI verifies, because then the image ships a bundle nobody
    tested. They had drifted to 22 and 24, with a comment still saying 20.
    """
    import re

    import yaml

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    image = re.search(r"FROM node:(\d+)-", dockerfile)
    assert image, "the Dockerfile no longer builds the SPA on a pinned node image"

    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/checkpoint-ci.yml").read_text(
        encoding="utf-8"))
    setups = [step["with"]["node-version"]
              for job in ci["jobs"].values()
              for step in job.get("steps", [])
              if "setup-node" in str(step.get("uses", "")) and step.get("with", {}).get(
                  "node-version")]
    assert setups, "CI no longer sets up Node anywhere"

    majors = {str(v).split(".")[0] for v in setups} | {image.group(1)}
    assert len(majors) == 1, (
        f"the Docker image and CI build the SPA on different Node majors "
        f"({sorted(majors)}), so the image ships a bundle CI never tested")

    # The header comment names it too, and a stale comment is how this drifted.
    assert f"node:{image.group(1)} builds the SPA" in dockerfile, (
        f"the Dockerfile's header comment does not say node:{image.group(1)}")
