"""What `checkpoint init` puts in a repository, and what it refuses to touch.

Init is the first thing anyone runs, on a repository full of work that is not
ours. Three guarantees matter more than the rest and are each pinned below: it
writes four files at most and never a line of code; it never overwrites
anything; and what it writes is usable — the config round-trips through
:class:`Project`, the starter scenario parses, and its criteria cannot be
satisfied by an agent that does nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from checkpoint.init import CI_WORKFLOW, SKILL_FILE
from checkpoint.project import CONFIG_NAME, Project

STARTER = "scenarios/quickstart.md"


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty repository to set up, with nothing for init to collide with."""
    return tmp_path


# -- what it writes ------------------------------------------------------------


def test_init_writes_the_config_the_starter_scenario_and_a_gitignore_line(repo, run_cli):
    result = run_cli("init", repo, "--command", "python my_agent.py")

    assert result.exit_code == 0, result.output
    assert _files(repo) == {CONFIG_NAME, STARTER, ".gitignore"}
    assert ".checkpoint/" in (repo / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_init_writes_no_code_into_the_repository(repo, run_cli):
    """Checkpoint starts the command you already have; it never adds a harness."""
    run_cli("init", repo, "--command", "python my_agent.py")

    assert [p for p in repo.rglob("*.py")] == []


def test_init_guesses_the_command_from_what_the_repository_looks_like(repo, run_cli):
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")

    result = run_cli("init", repo)

    assert result.exit_code == 0, result.output
    assert Project.load(repo).agent["command"] == "python main.py"


def test_init_without_a_command_and_nothing_to_guess_from_says_what_to_pass(repo, run_cli):
    result = run_cli("init", repo)

    assert result.exit_code == 2
    assert "--command" in result.output
    assert _files(repo) == set()


# -- what it never touches -----------------------------------------------------


def test_init_never_overwrites_an_existing_scenario(repo, run_cli):
    (repo / "scenarios").mkdir()
    (repo / STARTER).write_text("# mine\n", encoding="utf-8")

    result = run_cli("init", repo, "--command", "python my_agent.py")

    assert result.exit_code == 0, result.output
    assert (repo / STARTER).read_text(encoding="utf-8") == "# mine\n"
    assert "kept" in result.output


def test_init_never_overwrites_an_existing_config(repo, run_cli):
    mine = '[agent]\ncommand = "python mine.py"\n'
    (repo / CONFIG_NAME).write_text(mine, encoding="utf-8")

    run_cli("init", repo, "--command", "python other.py")

    assert (repo / CONFIG_NAME).read_text(encoding="utf-8") == mine


def test_running_init_twice_adds_nothing(repo, run_cli):
    run_cli("init", repo, "--command", "python my_agent.py")
    before = {path: (repo / path).read_text(encoding="utf-8") for path in _files(repo)}

    result = run_cli("init", repo, "--command", "python my_agent.py")

    assert result.exit_code == 0, result.output
    assert {path: (repo / path).read_text(encoding="utf-8") for path in _files(repo)} == before


# -- the config it generates ---------------------------------------------------


def test_the_generated_config_round_trips_through_project_load(repo, run_cli):
    run_cli("init", repo, "--command", "python my_agent.py", "--model", "gpt-5.6-luna")

    proj = Project.load(repo)
    assert proj.path == repo / CONFIG_NAME
    assert proj.judge_model() == "gpt-5.6-luna"
    assert proj.sandbox_setting("egress") == "llm"
    assert proj.gate_setting("runs") == 16
    agent = proj.build_agent()
    assert agent is not None and agent.command == "python my_agent.py"


def test_an_agent_that_takes_the_task_as_a_flag_is_written_down_as_such(repo, run_cli):
    run_cli("init", repo, "--command", "node agent.js",
            "--task-via", "arg", "--task-arg", "--prompt")

    agent = Project.load(repo).build_agent()
    assert agent is not None
    assert (agent.task_via, agent.task_arg) == ("arg", "--prompt")


# -- the optional files --------------------------------------------------------


def test_the_ci_workflow_is_not_written_into_a_repository_without_github(repo, run_cli):
    run_cli("init", repo, "--command", "python my_agent.py")

    assert not (repo / CI_WORKFLOW).exists()


def test_the_ci_workflow_is_written_when_asked_for(repo, run_cli):
    run_cli("init", repo, "--command", "python my_agent.py", "--ci")

    assert (repo / CI_WORKFLOW).is_file()


def test_the_ci_workflow_is_written_when_the_repository_already_uses_actions(repo, run_cli):
    (repo / ".github").mkdir()

    run_cli("init", repo, "--command", "python my_agent.py")

    assert (repo / CI_WORKFLOW).is_file()


def test_no_ci_overrides_a_repository_that_already_uses_actions(repo, run_cli):
    (repo / ".github").mkdir()

    run_cli("init", repo, "--command", "python my_agent.py", "--no-ci")

    assert not (repo / CI_WORKFLOW).exists()


def test_the_ci_workflow_gates_every_pull_request(repo, run_cli):
    run_cli("init", repo, "--command", "python my_agent.py", "--ci")

    workflow = yaml.safe_load((repo / CI_WORKFLOW).read_text(encoding="utf-8"))
    # YAML 1.1 reads a bare `on:` as the boolean true, which is what PyYAML
    # hands back for a workflow's trigger block.
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    steps = workflow["jobs"]["gate"]["steps"]
    gate = next(s for s in steps if "checkpoint gate" in (s.get("run") or ""))
    assert "OPENAI_API_KEY" in gate["env"]


def test_the_claude_skill_is_written_only_when_the_repository_wants_one(repo, run_cli):
    run_cli("init", repo, "--command", "python my_agent.py")
    assert not (repo / SKILL_FILE).exists()

    run_cli("init", repo, "--command", "python my_agent.py", "--skill")
    assert (repo / SKILL_FILE).is_file()


# -- the starter scenario ------------------------------------------------------


def test_the_starter_scenario_parses_with_no_problems(repo, run_cli):
    from checkpoint.scenario import parse_file

    run_cli("init", repo, "--command", "python my_agent.py")

    scenario = parse_file(repo / STARTER)
    assert scenario.runnable
    assert scenario.twins == ["github"]
    assert scenario.problems == []
    assert scenario.criteria


def test_the_starter_scenario_cannot_be_passed_by_an_agent_that_does_nothing(repo, run_cli):
    """Every check is deterministic, and at least one asks what *changed*.

    "An issue exists" is already true of the seed the scenario starts from, so
    a starter written that way would greet every new user with a green run from
    an agent that never ran.
    """
    from checkpoint.eval import schema_for
    from checkpoint.eval.nl import compile_criterion
    from checkpoint.scenario import parse_file

    run_cli("init", repo, "--command", "python my_agent.py")
    scenario = parse_file(repo / STARTER)
    schema = schema_for(scenario.twins)

    assertions = []
    for criterion in scenario.criteria:
        if criterion.kind == "P":
            continue  # judged by a model, so there is no assertion to inspect
        compiled = criterion.assertion or getattr(
            compile_criterion(criterion.text, schema), "assertion", None)
        assert compiled, f"{criterion.text!r} has no deterministic check"
        assertions.append(compiled)

    assert any(a.startswith("count(created.") for a in assertions), assertions
