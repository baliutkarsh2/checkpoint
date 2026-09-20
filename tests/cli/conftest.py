"""What every command-line test needs, built once here.

Three things, and no test builds any of them again:

``project``     a directory that looks like a real repository — a
                ``checkpoint.toml``, a scenario, and an agent that can actually
                be started — with the working directory moved into it, because
                every command finds its project by walking up from there.
``run_cli``     invokes the real ``checkpoint`` group, so argument parsing,
                exit codes and rendering are the ones users get.
``stub_runs``   stands in for the engine.

The stub is the one that needs justifying. ``checkpoint run`` without it starts
a twin per run and calls a judge model, which no unit test should pay for. It
replaces exactly one function, :func:`checkpoint.engine.run_scenario`, so
everything around the run stays real: the project is loaded, the scenario is
parsed, every flag is resolved into a :class:`RunOptions`, and the result is
rendered and written to the run records. What the stub records is therefore
evidence about the command, not about itself.
"""
from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from checkpoint.cli import main
from checkpoint.engine import Agent, RunOptions
from checkpoint.runner import CriterionResult, RunResult
from checkpoint.scenario import Scenario

#: An agent that answers and touches nothing. Standard library only: it has to
#: start on any machine that can run the tests, with no install step.
AGENT_SOURCE = '''\
"""A stand-in for the agent under test: it answers, and changes nothing."""
import json
import os
import sys

sys.stdout.write(json.dumps({"text": "did nothing: " + os.environ.get("CHECKPOINT_TASK", "")}))
'''

_CONFIG = """\
[agent]
command = {command}

[judge]
model = "stub-judge"

[sandbox]
egress = "llm"
"""


@dataclass
class ProjectDir:
    """The directory a CLI test runs in, and the files that were put in it."""

    root: Path
    agent: Path
    scenarios: Path

    def write_scenario(
        self,
        name: str,
        *,
        task: str = "File an issue in acme/webapp titled \"Add login button\".",
        criteria: Sequence[str] = ("[D] Exactly 1 issue was created",),
        twins: Sequence[str] = ("github",),
        tags: Sequence[str] = (),
        settings: Sequence[str] = (),
    ) -> Path:
        """Write one scenario into the project and return its path."""
        front = [f"twins: [{', '.join(twins)}]"]
        if tags:
            front.append(f"tags: [{', '.join(tags)}]")
        front.extend(settings)
        body = (
            "---\n" + "\n".join(front) + "\n---\n"
            f"# {name.replace('-', ' ').capitalize()}\n\n"
            f"## Task\n\n{task}\n\n"
            "## Criteria\n\n" + "".join(f"- {c}\n" for c in criteria)
        )
        path = self.scenarios / f"{name}.md"
        path.write_text(body, encoding="utf-8")
        return path


@pytest.fixture(autouse=True)
def _stable_environment(monkeypatch):
    """Keep the output and the settings independent of the machine running them.

    Rich sizes its tables to the terminal, so a narrow window would fold an
    assertion across lines and break an output assertion for no real reason.
    A judge model named in the environment outranks ``checkpoint.toml``, which
    would quietly change what the project fixture means.
    """
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("LINES", "50")
    monkeypatch.delenv("CHECKPOINT_JUDGE_MODEL", raising=False)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> ProjectDir:
    """A set-up project, with the working directory inside it."""
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT_SOURCE, encoding="utf-8")
    # json.dumps produces a TOML basic string, backslashes and all, which is
    # what an interpreter path on Windows needs.
    (tmp_path / "checkpoint.toml").write_text(
        _CONFIG.format(command=json.dumps(f"{sys.executable} agent.py")), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    proj = ProjectDir(root=tmp_path, agent=agent, scenarios=scenarios)
    proj.write_scenario("starter", tags=("smoke",))
    return proj


@pytest.fixture
def run_cli():
    """Invoke the real ``checkpoint`` command and return click's result.

    ``catch_exceptions=False`` by default so a crash in a command arrives as
    that crash, rather than as an exit code some assertion then misreads.
    """
    runner = CliRunner()

    def invoke(*args: object, catch_exceptions: bool = False, **kwargs):
        return runner.invoke(main, [str(a) for a in args],
                             catch_exceptions=catch_exceptions, **kwargs)

    return invoke


@dataclass
class Invocation:
    """One run the command asked the engine for."""

    scenario: Scenario
    agent: Agent
    options: RunOptions


class StubbedEngine:
    """``run_scenario`` without a twin, a subprocess or a judge.

    ``verdict`` decides what every run comes back as: ``"pass"``, ``"fail"``
    (one criterion did not hold) or ``"unscoreable"`` (the run happened but no
    verdict could be reached), which are the three cases the exit code of
    ``checkpoint run`` distinguishes.
    """

    def __init__(self) -> None:
        self.calls: list[Invocation] = []
        self.verdict = "pass"

    def __call__(self, scenario, agent, *, sandbox=None, options=None) -> RunResult:
        self.calls.append(Invocation(scenario=scenario, agent=agent, options=options))
        return self._result(scenario)

    @property
    def scenarios_run(self) -> list[str]:
        """The title of each scenario that was run, in order."""
        return [call.scenario.title for call in self.calls]

    def _result(self, scenario: Scenario) -> RunResult:
        # A scenario with no criteria (`run --task`) is scored by nothing, and
        # the stub has to say so rather than invent a verdict.
        criteria = []
        if scenario.criteria:
            first = scenario.criteria[0]
            criteria.append(CriterionResult(
                text=first.text, kind=first.kind, passed=self.verdict == "pass",
                reasoning="stubbed engine", evaluator="assertion:pinned",
                assertion="count(created.github.issues) == 1",
                must_pass=first.must_pass,
            ))
        result = RunResult(
            final_answer="did nothing", stderr="", exit_code=0, trace=[], state={},
            criteria=criteria, run_id=uuid.uuid4().hex[:12], agent="stub",
            twins=list(scenario.twins), duration_s=0.01,
        )
        if self.verdict == "unscoreable":
            result.eval_errors.append("the judge model could not be reached")
        return result


@pytest.fixture
def stub_runs(monkeypatch) -> StubbedEngine:
    """Replace the engine, and record what each command asked it to do."""
    stub = StubbedEngine()
    # `checkpoint run` imports run_scenario inside the call, so patching the
    # engine module is what reaches it.
    monkeypatch.setattr("checkpoint.engine.run_scenario", stub)
    return stub
