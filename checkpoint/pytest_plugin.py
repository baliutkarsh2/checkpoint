"""Checkpoint's pytest plugin — twins and scenario runs as ordinary fixtures.

Registered automatically through the ``pytest11`` entry point, so after
``pip install checkpoint-agents`` these are available with no conftest:

``checkpoint_sandbox``
    Twins for one test, started from the ``@pytest.mark.checkpoint`` marker and
    torn down afterwards. Gives you the real :class:`checkpoint.Sandbox` — URLs,
    credentials, request traces, and the state the twins ended in::

        @pytest.mark.checkpoint(twins=["github"], seed="small-project")
        def test_twin_serves_the_seeded_repository(checkpoint_sandbox):
            url = checkpoint_sandbox.twin_url("github")
            response = httpx.get(f"{url}/repos/acme/webapp")
            assert response.status_code == 200

``checkpoint_run``
    Runs a scenario against your agent and returns the scored result, so a
    scenario can be asserted on like any other test. The command comes from
    ``checkpoint.toml`` unless you pass one::

        def test_refund_scenario(checkpoint_run):
            result = checkpoint_run("scenarios/refund.md")
            assert result.score == 100, [c.text for c in result.criteria if not c.passed]

``checkpoint_twins``
    A session-scoped factory for suites that would rather pay the startup cost
    once. Twins are reset between tests that ask for it, not restarted.

Everything heavy is imported inside the fixtures: this module is loaded at
startup in every environment where Checkpoint is installed, including suites
that never touch it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from checkpoint import RunResult, Sandbox

MARKER = "checkpoint"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "checkpoint(twins, seed, intercept, egress): the twins a test needs, the "
        "named dataset they start from, and how the sandbox exposes them",
    )


def _marker_options(request: pytest.FixtureRequest) -> dict[str, Any]:
    marker = request.node.get_closest_marker(MARKER)
    options = dict(marker.kwargs) if marker else {}
    # `clones` is what twins were called before; scenarios still accept it.
    twins = options.pop("twins", None) or options.pop("clones", None) or ["github"]
    options["twins"] = [twins] if isinstance(twins, str) else list(twins)
    return options


@pytest.fixture
def checkpoint_sandbox(request: pytest.FixtureRequest) -> Sandbox:
    """Twins for this test, seeded as the marker asks, stopped afterwards."""
    from checkpoint import Sandbox
    from checkpoint.engine import TwinSetup

    options = _marker_options(request)
    seed = options.pop("seed", None)
    sandbox = Sandbox(
        options["twins"],
        intercept=options.get("intercept", False),
        egress=options.get("egress", "none"),
    )
    with sandbox:
        if seed:
            sandbox.prepare({name: TwinSetup(seed=seed) for name in sandbox.twins})
        yield sandbox


@pytest.fixture
def checkpoint_run(request: pytest.FixtureRequest):
    """Run a scenario against your agent and hand back the scored result.

    ``checkpoint_run(path, command=None, **options)`` — ``command`` defaults to
    ``[agent]`` in the nearest ``checkpoint.toml``, and any other keyword is a
    :class:`checkpoint.RunOptions` field.
    """
    from checkpoint import Project, RunOptions, parse_file, run_scenario

    def run(scenario_path: str, command: str | None = None, **options: Any) -> RunResult:
        project = Project.load()
        agent = project.build_agent(command)
        if agent is None:
            raise pytest.UsageError(
                "checkpoint_run needs to know how to start your agent: pass "
                'command="python my_agent.py", or add [agent] to checkpoint.toml'
            )
        judge_model = options.pop("judge_model", None) or project.judge_model()
        return run_scenario(parse_file(scenario_path), agent,
                            options=RunOptions(judge_model=judge_model, **options))

    return run


class _TwinFactory:
    """Keeps one sandbox per twin combination alive for the whole session."""

    def __init__(self) -> None:
        self._sandboxes: dict[tuple[str, ...], Sandbox] = {}

    def __call__(self, twins: list[str] | str, *, seed: str | None = None,
                 reset: bool = True) -> Sandbox:
        from checkpoint import Sandbox
        from checkpoint.engine import TwinSetup

        names = tuple(sorted([twins] if isinstance(twins, str) else twins))
        sandbox = self._sandboxes.get(names)
        if sandbox is None:
            sandbox = Sandbox(list(names), intercept=False, egress="none")
            sandbox.start()
            self._sandboxes[names] = sandbox
        elif reset:
            # A shared sandbox that keeps the previous test's writes makes the
            # next test's assertions depend on execution order.
            sandbox.reset()
        if seed:
            sandbox.prepare({name: TwinSetup(seed=seed) for name in names})
        return sandbox

    def close(self) -> None:
        for sandbox in self._sandboxes.values():
            sandbox.stop()
        self._sandboxes.clear()


@pytest.fixture(scope="session")
def checkpoint_twins() -> _TwinFactory:
    """Session-scoped twins: ``checkpoint_twins(["github"], seed="small-project")``."""
    factory = _TwinFactory()
    yield factory
    factory.close()
