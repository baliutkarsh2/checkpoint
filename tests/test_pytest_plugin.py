"""The pytest plugin has to work with no conftest, no imports and no setup.

It is registered through the ``pytest11`` entry point, so after
``pip install checkpoint-agents`` a test can ask for ``checkpoint_sandbox`` and
get running twins. That promise is only worth testing the way a user meets it,
so every test here writes a test file and runs pytest on it through the
``pytester`` fixture: what is under test is the fixture a real suite receives,
not a function this file imported from the plugin module.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_SCENARIO = REPO_ROOT / "checkpoint" / "demo" / "smoke-scenario.md"
DEMO_AGENT = REPO_ROOT / "checkpoint" / "demo" / "harness_fake.py"

_HAS_DEMO = DEMO_SCENARIO.is_file() and DEMO_AGENT.is_file()


def _run_isolated(pytester):
    """Run the written test in its own interpreter.

    `checkpoint_run` starts the intercepting proxy, which mints a certificate
    through cryptography's Rust bindings. Those cannot be re-initialized, and
    pytester restores ``sys.modules`` after every in-process run — so a second
    in-process run of this fixture meets half-reloaded modules. The isolation is
    the point of a subprocess here, not the speed cost.
    """
    return pytester.runpytest_subprocess()


# -- the marker ----------------------------------------------------------------


def test_the_marker_is_registered_so_strict_markers_accepts_it(pytester):
    """A suite running --strict-markers would otherwise fail on our own marker."""
    pytester.makepyfile("""
        import pytest

        @pytest.mark.checkpoint(twins=["github"], seed="small-project",
                                intercept=False, egress="none")
        def test_marked_but_asks_for_nothing():
            pass
    """)
    pytester.runpytest("--strict-markers").assert_outcomes(passed=1)


def test_the_fixtures_are_there_without_a_conftest(pytester):
    """The entry point is the whole installation story: no plugin line to add."""
    pytester.makepyfile("def test_nothing(): pass")
    result = pytester.runpytest("--fixtures")
    result.stdout.fnmatch_lines(["*checkpoint_sandbox*"])
    result.stdout.fnmatch_lines(["*checkpoint_run*"])
    result.stdout.fnmatch_lines(["*checkpoint_twins*"])
    assert not (pytester.path / "conftest.py").exists()


# -- checkpoint_sandbox --------------------------------------------------------


@pytest.mark.integration
def test_checkpoint_sandbox_serves_the_twins_the_marker_names(pytester):
    """The fixture hands back a real Sandbox with the twins already answering."""
    pytester.makepyfile("""
        import httpx
        import pytest

        from checkpoint.twins import registry

        @pytest.mark.checkpoint(twins=["github"], seed="small-project")
        def test_the_seeded_repository_is_served(checkpoint_sandbox):
            assert tuple(checkpoint_sandbox.twins) == ("github",)
            spec = registry.get("github")
            response = httpx.get(
                f"{checkpoint_sandbox.twin_url('github')}/repos/acme/webapp",
                headers={"Authorization": f"{spec.auth_scheme} {spec.token}"},
                timeout=15, trust_env=False)
            assert response.status_code == 200
            assert response.json()["name"] == "webapp"
    """)
    pytester.runpytest().assert_outcomes(passed=1)


@pytest.mark.integration
def test_checkpoint_sandbox_defaults_to_github_with_no_marker(pytester):
    """The commonest case needs no marker at all."""
    pytester.makepyfile("""
        def test_a_twin_is_running(checkpoint_sandbox):
            assert tuple(checkpoint_sandbox.twins) == ("github",)
            assert checkpoint_sandbox.twin_url("github").startswith("http://127.0.0.1:")
    """)
    pytester.runpytest().assert_outcomes(passed=1)


@pytest.mark.integration
def test_checkpoint_sandbox_starts_every_twin_the_marker_asks_for(pytester):
    pytester.makepyfile("""
        import pytest

        @pytest.mark.checkpoint(twins=["github", "slack"])
        def test_two_twins(checkpoint_sandbox):
            assert set(checkpoint_sandbox.twins) == {"github", "slack"}
            urls = {checkpoint_sandbox.twin_url(n) for n in checkpoint_sandbox.twins}
            assert len(urls) == 2, "each twin needs its own port"
    """)
    pytester.runpytest().assert_outcomes(passed=1)


@pytest.mark.integration
def test_checkpoint_sandbox_is_stopped_when_the_test_ends(pytester):
    """A suite that leaked a twin per test would run out of ports and memory.

    Asserted on the sandbox itself rather than on its port: a freed port is
    fair game for the next test's twin, so "nothing answers there" would be a
    flake waiting to happen.
    """
    pytester.makepyfile("""
        SANDBOXES = []

        def test_first_uses_the_sandbox(checkpoint_sandbox):
            assert checkpoint_sandbox.started
            SANDBOXES.append(checkpoint_sandbox)

        def test_it_was_torn_down_afterwards():
            assert SANDBOXES, "the first test never received a sandbox"
            assert not SANDBOXES[0].started
    """)
    pytester.runpytest().assert_outcomes(passed=2)


# -- checkpoint_run ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_DEMO, reason="demo assets missing")
def test_checkpoint_run_scores_a_scenario_like_the_cli_does(pytester):
    """A scenario becomes an ordinary assertion, with the same score the gate uses."""
    pytester.makepyfile(f"""
        SCENARIO = {str(DEMO_SCENARIO)!r}
        COMMAND = {f"{sys.executable} {DEMO_AGENT}"!r}

        def test_the_agent_files_the_issue(checkpoint_run):
            result = checkpoint_run(SCENARIO, command=COMMAND)
            assert result.score == 100, [c.text for c in result.criteria if not c.passed]
            assert result.complete
            assert [c.kind for c in result.criteria]
    """)
    _run_isolated(pytester).assert_outcomes(passed=1)


@pytest.mark.skipif(not _HAS_DEMO, reason="demo assets missing")
def test_checkpoint_run_takes_the_command_from_checkpoint_toml(pytester):
    """The point of the fixture: the suite does not restate how to start the agent."""
    pytester.makefile(".toml", checkpoint=f"""
        [agent]
        command = {f"{sys.executable} {DEMO_AGENT}"!r}
    """.replace("\n        ", "\n"))
    pytester.makepyfile(f"""
        SCENARIO = {str(DEMO_SCENARIO)!r}

        def test_no_command_needed(checkpoint_run):
            assert checkpoint_run(SCENARIO).score == 100
    """)
    _run_isolated(pytester).assert_outcomes(passed=1)


def test_checkpoint_run_says_how_to_point_it_at_an_agent(pytester):
    """With nothing configured the failure has to name the fix, not the traceback."""
    pytester.makepyfile("""
        import pytest

        def test_without_an_agent(checkpoint_run):
            with pytest.raises(pytest.UsageError) as raised:
                checkpoint_run("does-not-matter.md")
            message = str(raised.value)
            assert "checkpoint.toml" in message
            assert "command=" in message
    """)
    pytester.runpytest().assert_outcomes(passed=1)


# -- checkpoint_twins ----------------------------------------------------------


@pytest.mark.integration
def test_checkpoint_twins_reuses_one_sandbox_across_the_session(pytester):
    """The factory exists to pay the startup cost once, not once per test."""
    pytester.makepyfile("""
        URLS = []

        def test_first(checkpoint_twins):
            URLS.append(checkpoint_twins(["github"]).twin_url("github"))

        def test_second(checkpoint_twins):
            assert checkpoint_twins(["github"]).twin_url("github") == URLS[0]
    """)
    pytester.runpytest().assert_outcomes(passed=2)


@pytest.mark.integration
def test_checkpoint_twins_resets_between_tests_so_order_does_not_matter(pytester):
    """A shared sandbox that kept the last test's writes would leak between tests."""
    pytester.makepyfile("""
        import httpx

        from checkpoint.twins import registry

        def _headers():
            spec = registry.get("github")
            return {"Authorization": f"{spec.auth_scheme} {spec.token}"}

        def _issues(sandbox):
            return sandbox.views()["github"]["issues"]["items"]

        def test_writes_an_issue(checkpoint_twins):
            sandbox = checkpoint_twins(["github"], seed="small-project")
            before = len(_issues(sandbox))
            created = httpx.post(f"{sandbox.twin_url('github')}/repos/acme/webapp/issues",
                                 json={"title": "left behind"}, headers=_headers(),
                                 timeout=15, trust_env=False)
            assert created.status_code == 201
            assert len(_issues(sandbox)) == before + 1

        def test_does_not_see_it(checkpoint_twins):
            sandbox = checkpoint_twins(["github"], seed="small-project")
            titles = [i.get("title") for i in _issues(sandbox)]
            assert "left behind" not in titles
    """)
    pytester.runpytest().assert_outcomes(passed=2)


@pytest.mark.integration
def test_checkpoint_twins_can_keep_state_when_a_test_asks_for_it(pytester):
    """Some suites build a fixture up across tests on purpose."""
    pytester.makepyfile("""
        import httpx

        from checkpoint.twins import registry

        def _headers():
            spec = registry.get("github")
            return {"Authorization": f"{spec.auth_scheme} {spec.token}"}

        def _titles(sandbox):
            return [i.get("title") for i in sandbox.views()["github"]["issues"]["items"]]

        def test_writes(checkpoint_twins):
            sandbox = checkpoint_twins(["github"], seed="small-project")
            httpx.post(f"{sandbox.twin_url('github')}/repos/acme/webapp/issues",
                       json={"title": "kept"}, headers=_headers(), timeout=15,
                       trust_env=False)

        def test_still_there(checkpoint_twins):
            sandbox = checkpoint_twins(["github"], reset=False)
            assert "kept" in _titles(sandbox)
    """)
    pytester.runpytest().assert_outcomes(passed=2)
