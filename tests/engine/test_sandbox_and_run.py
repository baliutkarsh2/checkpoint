"""The sandbox lifecycle and end-to-end scenario runs on the engine."""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from checkpoint.engine import Agent, RunOptions, Sandbox, SandboxError, TwinSetup, run_scenario
from checkpoint.scenario import parse, parse_file
from checkpoint.twins import registry

PY = sys.executable

# A deterministic agent: creates a GitHub issue named after the task, via the
# twin URL the sandbox exports, then reports the issue number.
AGENT = textwrap.dedent('''
    import json, os, sys, urllib.request
    base = os.environ["CHECKPOINT_GITHUB_URL"]
    title = os.environ["CHECKPOINT_TASK"].split(":", 1)[-1].strip()
    req = urllib.request.Request(
        base + "/repos/acme/webapp/issues", method="POST",
        data=json.dumps({"title": title}).encode(),
        headers={"Authorization": "token " + os.environ["GITHUB_TOKEN"],
                 "Content-Type": "application/json"})
    issue = json.load(urllib.request.urlopen(req))
    mode = os.environ.get("AGENT_MODE", "")
    if mode == "crash":
        sys.exit(2)
    print(json.dumps({"text": f"Opened issue #{issue['number']}"}))
''')

SCENARIO = textwrap.dedent('''
    # File a bug

    ## Prompt
    File an issue: Login broken

    ## Success Criteria
    - [D] An issue titled "Login broken" exists

    ## Config
    twins: github
    seed: small-project
''')


@pytest.fixture
def agent_path(tmp_path: Path) -> Path:
    path = tmp_path / "agent.py"
    path.write_text(AGENT, encoding="utf-8")
    return path


# -- sandbox -------------------------------------------------------------------

def test_sandbox_lifecycle_and_env():
    with Sandbox(["github", "slack"], intercept=False) as box:
        assert box.twins == ("github", "slack")
        for name, url in box.urls.items():
            assert httpx.get(f"{url}/_health").json()["twin"] == name
        env = box.agent_env({"PATH": "x", "GITHUB_TOKEN": "a-real-token"})
        assert env["GITHUB_TOKEN"] == registry.get("github").token, "real tokens are replaced"
        assert env["CHECKPOINT_GITHUB_URL"] == box.twin_url("github")
        assert env["CHECKPOINT_TWINS"] == "github,slack"
        assert env["PATH"] == "x"
    assert not box.started


def test_prepare_resets_between_runs():
    with Sandbox(["github"], intercept=False) as box:
        box.prepare({"github": TwinSetup(seed="small-project")})
        seeded = len(box.views()["github"]["issues"]["items"])
        assert seeded > 0
        httpx.post(f"{box.twin_url('github')}/user/repos", json={"name": "extra"},
                   headers={"Authorization": "token x"})
        assert box.trace()
        box.prepare({})
        assert box.trace() == []
        assert box.views()["github"]["issues"]["items"] == []


def test_bad_seed_names_the_alternatives():
    with Sandbox(["github"], intercept=False) as box:
        with pytest.raises(SandboxError) as exc:
            box.prepare({"github": TwinSetup(seed="smal-project")})
    assert "small-project" in str(exc.value)


def test_bad_fault_config_is_reported():
    with Sandbox(["github"], intercept=False) as box:
        with pytest.raises(SandboxError) as exc:
            box.prepare({"github": TwinSetup(config={"rate_limt": 1})})
    assert "rate_limt" in str(exc.value)


def test_unknown_twin_is_rejected_up_front():
    with pytest.raises(KeyError) as exc:
        Sandbox(["jira"])
    assert "available" in str(exc.value)


def test_twinless_sandbox_starts():
    with Sandbox([], intercept=False) as box:
        assert box.urls == {}
        assert box.trace() == [] and box.views() == {}


# -- end-to-end runs --------------------------------------------------------------

def test_run_scenario_scores_a_real_agent(agent_path):
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False))
    assert result.error is None, result.stderr
    assert result.score == 100.0
    assert result.final_answer.startswith("Opened issue #")
    assert result.twins == ["github"] and result.run_id
    created = [c for c in result.trace if c["op"] == "create"]
    assert created and created[0]["resource"] == "issues"
    key = result.views["github"]["issues"]["key"]
    seeded = {i[key] for i in result.seed_views["github"]["issues"]["items"]}
    final = {i[key] for i in result.views["github"]["issues"]["items"]}
    assert len(final - seeded) == 1


def test_sandbox_is_reused_across_runs(agent_path):
    agent = Agent(command=[PY, str(agent_path)])
    with Sandbox(["github"], intercept=False) as box:
        first = run_scenario(parse(SCENARIO), agent, sandbox=box)
        second = run_scenario(parse(SCENARIO), agent, sandbox=box)
    assert first.score == second.score == 100.0
    # Each run starts from the seed: the new issue gets the same number both times.
    assert first.final_answer == second.final_answer


def test_agent_crash_is_an_error_not_a_score(agent_path, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "crash")
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False))
    assert result.error == "agent exited with code 2"
    assert result.criteria == [] and not result.complete and not result.setup_error


def test_setup_failure_is_flagged(agent_path):
    scenario = parse(SCENARIO.replace("seed: small-project", "seed: nope"))
    result = run_scenario(scenario, Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False))
    assert result.setup_error and "sandbox setup failed" in result.error


def test_no_calls_to_the_sandbox_is_called_out(tmp_path):
    lazy = tmp_path / "lazy.py"
    lazy.write_text('print("I did it, trust me")', encoding="utf-8")
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(lazy)]),
                          options=RunOptions(intercept=False))
    assert any("made no calls" in w for w in result.warnings)
    assert result.score < 100.0


def test_read_only_fails_a_writing_agent(agent_path):
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False, read_only=True))
    assert any(c.evaluator == "read-only-guard" and not c.passed for c in result.criteria)


# -- interception ------------------------------------------------------------------

INTERCEPT_AGENT = textwrap.dedent('''
    import json, urllib.request
    req = urllib.request.Request(
        "https://api.github.com/repos/acme/webapp/issues", method="POST",
        data=json.dumps({"title": "Login broken"}).encode(),
        headers={"Authorization": "token whatever-the-agent-has",
                 "Content-Type": "application/json"})
    issue = json.load(urllib.request.urlopen(req))
    print(json.dumps({"text": f"Opened issue #{issue['number']}"}))
''')


@pytest.fixture
def production_agent(tmp_path: Path) -> Path:
    path = tmp_path / "production_agent.py"
    path.write_text(INTERCEPT_AGENT, encoding="utf-8")
    return path


def test_calls_to_production_urls_reach_the_twin(production_agent):
    # The claim the product rests on: the agent's own code, its own URLs, no edits.
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(production_agent)]),
                          options=RunOptions(intercept=True))
    assert result.error is None, result.stderr
    assert result.score == 100.0
    assert [(c["method"], c["path"]) for c in result.trace] == [
        ("POST", "/repos/acme/webapp/issues")]
    assert not result.warnings


def test_without_interception_the_agent_gets_no_proxy():
    # With interception off the agent is on its own: no proxy, no rerouting, and
    # calls to production hostnames go wherever DNS says. The twin URLs are still
    # exported for agents that read them.
    with Sandbox(["github"], intercept=False) as box:
        env = box.agent_env({"PATH": ""})
    assert "HTTPS_PROXY" not in env and "SSL_CERT_FILE" not in env
    assert env["CHECKPOINT_GITHUB_URL"].startswith("http://127.0.0.1:")
    assert env["GITHUB_API_URL"] == env["CHECKPOINT_GITHUB_URL"]


def test_egress_outside_the_sandbox_is_blocked_and_reported(tmp_path):
    reacher = tmp_path / "reacher.py"
    reacher.write_text(textwrap.dedent('''
        import json, urllib.request
        try:
            urllib.request.urlopen("https://api.tavily.com/search", timeout=10)
            note = "reached it"
        except Exception as e:
            note = f"blocked: {type(e).__name__}"
        print(json.dumps({"text": note}))
    '''), encoding="utf-8")
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, str(reacher)]),
                          options=RunOptions(intercept=True, egress="llm"))
    blocked = [e for e in result.egress if e.get("allowed") is False]
    assert [e["host"] for e in blocked] == ["api.tavily.com"]
    assert any("api.tavily.com" in w and "--allow-host" in w for w in result.warnings)


def test_an_allowed_host_is_let_through(tmp_path):
    result = run_scenario(parse(SCENARIO), Agent(command=[PY, "-c", "print('{}')"]),
                          options=RunOptions(intercept=True, egress="llm",
                                             allow_hosts=("api.tavily.com",)))
    assert result.error is None


# -- workspaces ---------------------------------------------------------------------
#
# A workspace is the other half of what an agent can be tested on: the tree it
# edits, rather than the APIs it calls. It is orthogonal to twins — a run may
# have either, both, or neither.

WORKSPACE_SCENARIO = textwrap.dedent('''
    ---
    workspace: repo
    ---
    # Fix the bug

    ## Task
    Fix the bug in src/app.py and note it in the changelog.

    ## Criteria
    - [D] Exactly 1 file was created
    - [D] src/app.py was changed
    - [D] No files were deleted
''')

# Creates one file and edits another, entirely through relative paths — the way
# a coding agent works when it believes it is sitting in a checkout.
CODING_AGENT = textwrap.dedent('''
    import pathlib
    pathlib.Path("CHANGELOG.md").write_text("Fixed the bug.\\n", encoding="utf-8")
    app = pathlib.Path("src/app.py")
    app.write_text(app.read_text(encoding="utf-8").replace("1 / 0", "1"), encoding="utf-8")
    print("edited the tree")
''')


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scenario file with a small fixture tree beside it."""
    seed = tmp_path / "repo"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "app.py").write_text("def main():\n    return 1 / 0\n", encoding="utf-8")
    (seed / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "fix.md").write_text(WORKSPACE_SCENARIO, encoding="utf-8")
    return tmp_path


@pytest.fixture
def coding_agent(tmp_path: Path) -> Path:
    path = tmp_path / "coding_agent.py"
    path.write_text(CODING_AGENT, encoding="utf-8")
    return path


def test_a_workspace_exports_its_root_and_holds_the_seed():
    with Sandbox([], intercept=False, workspace=True) as box:
        box.prepare(workspace_seed=None)
        root = Path(box.agent_env({})["CHECKPOINT_WORKSPACE"])

        assert root == box.workspace_root and root.is_dir()
        assert box.views()["workspace"]["files"]["items"] == []


def test_the_workspace_collection_is_keyed_by_path(repo):
    with Sandbox([], intercept=False, workspace=True) as box:
        box.prepare(workspace_seed=repo / "repo")
        files = box.views()["workspace"]["files"]

        assert files["key"] == "path"
        assert sorted(i["path"] for i in files["items"]) == ["README.md", "src/app.py"]
        assert (box.workspace_root / "src" / "app.py").is_file(), "the tree is really there"


def test_a_sandbox_without_a_workspace_says_so_rather_than_silently_skipping(repo):
    with Sandbox([], intercept=False) as box:
        assert box.workspace_root is None
        with pytest.raises(SandboxError) as exc:
            box.prepare(workspace_seed=repo / "repo")
    assert "workspace=True" in str(exc.value)


def test_the_agent_runs_inside_the_workspace(repo, coding_agent):
    result = run_scenario(parse_file(repo / "fix.md"), Agent(command=[PY, str(coding_agent)]),
                          options=RunOptions(intercept=False))

    assert result.error is None, result.stderr
    assert result.score == 100.0
    files = {f["path"]: f for f in result.views["workspace"]["files"]["items"]}
    assert "1 / 0" not in files["src/app.py"]["content"]
    assert files["CHANGELOG.md"]["content"] == "Fixed the bug.\n"


def test_an_explicit_cwd_wins_but_the_workspace_is_still_reachable(repo, tmp_path):
    """Someone who set `[agent] cwd` meant it; the tree stays addressable by env var."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    reporter = tmp_path / "reporter.py"
    # Reported from inside the run: the sandbox removes the tree on the way out,
    # so whether the agent could reach it is only answerable while it is running.
    reporter.write_text(textwrap.dedent('''
        import json, os, pathlib
        ws = pathlib.Path(os.environ["CHECKPOINT_WORKSPACE"])
        print(json.dumps({"text": json.dumps({
            "cwd": str(pathlib.Path.cwd()),
            "reached": (ws / "src" / "app.py").is_file(),
        })}))
    '''), encoding="utf-8")

    result = run_scenario(parse_file(repo / "fix.md"),
                          Agent(command=[PY, str(reporter)], cwd=str(elsewhere)),
                          options=RunOptions(intercept=False, evaluate=False))

    reported = json.loads(result.final_answer)
    assert Path(reported["cwd"]) == elsewhere.resolve()
    assert reported["reached"] is True


def test_each_run_gets_a_fresh_copy_of_the_tree(repo, coding_agent):
    """What the gate relies on: run two must not inherit run one's edits."""
    scenario = parse_file(repo / "fix.md")
    agent = Agent(command=[PY, str(coding_agent)])
    with Sandbox([], intercept=False, workspace=True) as box:
        first = run_scenario(scenario, agent, sandbox=box)
        seed_paths = sorted(i["path"] for i in first.seed_views["workspace"]["files"]["items"])
        second = run_scenario(scenario, agent, sandbox=box)

    assert first.error is None and second.error is None, second.stderr
    assert first.score == second.score == 100.0
    assert seed_paths == ["README.md", "src/app.py"], "the seed has no CHANGELOG.md"
    assert sorted(i["path"] for i in second.seed_views["workspace"]["files"]["items"]) == (
        seed_paths), "run two started from the seed, not from run one's tree"


def test_a_missing_workspace_directory_is_a_setup_error_not_a_zero(repo, coding_agent):
    """A typo'd path must be reported, never scored as an agent that failed."""
    (repo / "fix.md").write_text(
        WORKSPACE_SCENARIO.replace("workspace: repo", "workspace: no-such-tree"),
        encoding="utf-8")

    result = run_scenario(parse_file(repo / "fix.md"), Agent(command=[PY, str(coding_agent)]),
                          options=RunOptions(intercept=False))

    assert result.setup_error
    assert "workspace directory not found" in result.error
    assert result.criteria == []


def test_an_idle_agent_fails_what_it_should_and_passes_what_it_should(repo):
    """The negative case that decides whether file criteria are worth anything.

    `exists(workspace.files[...])` is about the seed as much as the agent: a file
    that shipped in the fixture exists whether or not the agent ran. Only the
    delta roots can catch an agent that did nothing, which is why the docs tell
    authors to write `count(created.workspace.files) == 1`.
    """
    (repo / "fix.md").write_text(WORKSPACE_SCENARIO.replace(
        "- [D] Exactly 1 file was created",
        '- [D] Exactly 1 file was created\n'
        '- [D] README.md is present  =>  exists(workspace.files[path == "README.md"])',
    ), encoding="utf-8")
    idle = repo / "idle.py"
    idle.write_text('print("I had a look and everything seemed fine")', encoding="utf-8")

    result = run_scenario(parse_file(repo / "fix.md"), Agent(command=[PY, str(idle)]),
                          options=RunOptions(intercept=False))

    verdicts = {c.assertion: c.passed for c in result.criteria}
    assert verdicts['count(created.workspace.files) == 1'] is False
    assert verdicts['exists(workspace.files[path == "README.md"])'] is True, (
        "a seeded file exists no matter what the agent did — which is the trap")
    assert verdicts['exists(changed.workspace.files[path == "src/app.py"])'] is False
    assert result.score < 100.0


def test_a_tree_too_big_to_hold_is_reported_not_raised(repo, tmp_path, monkeypatch):
    """An agent that floods the tree cannot be scored, so the run says so."""
    monkeypatch.setattr("checkpoint.workspace.MAX_FILES", 3)
    flooder = tmp_path / "flooder.py"
    flooder.write_text(textwrap.dedent('''
        import pathlib
        for i in range(10):
            pathlib.Path(f"junk{i}.txt").write_text("x", encoding="utf-8")
        print("flooded")
    '''), encoding="utf-8")

    result = run_scenario(parse_file(repo / "fix.md"), Agent(command=[PY, str(flooder)]),
                          options=RunOptions(intercept=False))

    assert result.setup_error
    assert "more than the limit of 3" in result.error


def test_twins_and_a_workspace_coexist(repo, agent_path):
    """Neither implies the other, and a single-twin run keeps its flat state shape."""
    scenario = parse(SCENARIO + "\nworkspace: " + str(repo / "repo").replace("\\", "/") + "\n")

    result = run_scenario(scenario, Agent(command=[PY, str(agent_path)]),
                          options=RunOptions(intercept=False))

    assert result.error is None, result.stderr
    assert result.score == 100.0
    assert sorted(i["path"] for i in result.views["workspace"]["files"]["items"]) == [
        "README.md", "src/app.py"]
    assert "issues" in result.state, "the twin's state is still flat, not nested per clone"
    assert result.state["workspace"]["files"]


def test_a_twins_mcp_surface_is_reachable_through_interception():
    """An MCP agent should not have to know it is being intercepted either.

    The MCP server's DNS-rebinding guard only accepts a localhost `Host`
    header, and an intercepted request carries the production hostname — so the
    twin answered 421 to exactly the agents that needed no modification, and an
    MCP client had to be rewritten to use $CHECKPOINT_<TWIN>_URL instead.
    """
    handshake = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "checkpoint-tests", "version": "1"}},
    }
    with Sandbox(["github"], intercept=True, egress="none") as sandbox:
        env = sandbox.agent_env()
        with httpx.Client(proxy=env["HTTPS_PROXY"], verify=env["SSL_CERT_FILE"],
                          trust_env=False, timeout=20) as client:
            response = client.post(
                "https://api.github.com/mcp/", json=handshake,
                headers={"accept": "application/json, text/event-stream"})
    assert response.status_code == 200, response.text[:300]
    assert "serverInfo" in response.text
