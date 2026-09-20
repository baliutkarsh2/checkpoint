"""The bundled examples are the first code anyone copies.

A missing file or a stale flag here is a confusing failure five minutes into
somebody's first day with Checkpoint, so the cheap checks run at PR time: the
files exist, the Python parses, the config loads, and the scenarios are
scenarios that an idle agent would fail. Nothing here runs an agent — that
needs a model key, and these have to pass on a fork.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from checkpoint.project import Project
from checkpoint.scenario import parse_file
from checkpoint.twins import registry

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
AGENTS = sorted(p.name for p in EXAMPLES.iterdir()
                if p.is_dir() and (p / "checkpoint.toml").is_file())


def test_there_are_examples_to_copy():
    assert AGENTS, "examples/ has no example agent"


@pytest.mark.parametrize("agent", AGENTS)
def test_an_example_is_complete(agent: str):
    directory = EXAMPLES / agent
    for name in ("agent.py", "checkpoint.toml", "requirements.txt", "README.md"):
        assert (directory / name).is_file(), f"{agent}/{name} is missing"
    assert list((directory / "scenarios").glob("*.md")), f"{agent} ships no scenario"


@pytest.mark.parametrize("agent", AGENTS)
def test_the_agent_is_valid_python(agent: str):
    ast.parse((EXAMPLES / agent / "agent.py").read_text(encoding="utf-8"))


@pytest.mark.parametrize("agent", AGENTS)
def test_the_config_loads_and_names_a_command(agent: str):
    """A config Checkpoint rejects would fail before the example ran at all."""
    project = Project.load(EXAMPLES / agent)
    assert project.path == EXAMPLES / agent / "checkpoint.toml"
    built = project.build_agent()
    assert built is not None and built.command


@pytest.mark.parametrize("agent", AGENTS)
def test_the_agent_reads_the_task_the_way_its_config_says(agent: str):
    """`task_via` and the agent's own code have to agree, or every run is empty."""
    project = Project.load(EXAMPLES / agent)
    source = (EXAMPLES / agent / "agent.py").read_text(encoding="utf-8")
    via = project.agent.get("task_via", "env")
    if via == "env":
        variable = project.agent.get("task_env", "CHECKPOINT_TASK")
        assert variable in source, f"{agent} never reads ${variable}"
    else:
        assert "stdin" in source or "argv" in source, f"{agent} never reads the task"


def _scenarios() -> list[tuple[str, Path]]:
    return [(agent, path) for agent in AGENTS
            for path in sorted((EXAMPLES / agent / "scenarios").glob("*.md"))]


@pytest.mark.parametrize(("agent", "path"), _scenarios(), ids=lambda v: getattr(v, "name", v))
def test_an_example_scenario_is_runnable(agent: str, path: Path):
    scenario = parse_file(path)
    assert scenario.problems == [], f"{path.name}: {scenario.problems}"
    assert scenario.prompt.strip(), f"{path.name} has no task"
    assert scenario.criteria, f"{path.name} has no criteria"
    known = set(registry.names())
    unknown = [t for t in scenario.twins if t.lower() not in known]
    assert not unknown, f"{path.name} names unknown twins: {unknown}"


@pytest.mark.parametrize(("agent", "path"), _scenarios(), ids=lambda v: getattr(v, "name", v))
def test_an_example_scenario_asks_what_changed(agent: str, path: Path):
    """The examples are what people imitate, so they must not teach a check that
    an agent doing nothing would pass."""
    scenario = parse_file(path)
    deltas = ("created", "deleted", "changed", "was created", "were created",
              "was deleted", "were deleted", "trace", "answer")
    text = " ".join(
        f"{c.text} {c.assertion or ''}".lower()
        for c in scenario.criteria if c.kind != "P")
    assert any(word in text for word in deltas), (
        f"{path.name} has no criterion about what the agent changed or did")


@pytest.mark.parametrize("agent", AGENTS)
def test_the_readme_only_names_commands_that_exist(agent: str):
    import re

    from checkpoint.cli import SECTIONS

    commands = {entry.name for _, entries in SECTIONS for entry in entries}
    text = (EXAMPLES / agent / "README.md").read_text(encoding="utf-8")
    named = set(re.findall(r"(?<!from )checkpoint ([a-z][a-z-]*)", text)) - {"import"}
    assert named <= commands, f"{agent}/README.md names {sorted(named - commands)}"
