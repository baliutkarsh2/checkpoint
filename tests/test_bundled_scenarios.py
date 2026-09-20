"""The bundled scenarios have to be honest, because users copy them.

Three things are checked here, in rising order of cost:

1. Every scenario parses cleanly, names twins that exist and a seed that ships.
2. Every criterion that is not ``[P]`` compiles to an assertion with no model in
   the loop, and that assertion is valid against the twins' own schema.
3. The **vacuity test**: a do-nothing agent runs every scenario and must score
   below 100 — a scenario a lazy agent can ace is a broken scenario — while
   every must-pass criterion about *not* doing damage still passes, because an
   agent that changes nothing has by definition broken nothing.
4. A scripted, LLM-free agent (``tests/scripted_agent.py``) scores 100 on every
   scenario under ``scenarios/``, which is what proves the criteria are
   satisfiable at all — a criterion no agent can meet is as broken as one a
   lazy agent passes.

Judged ``[P]`` criteria are decided here by a stub judge that passes everything.
That is deliberate: it gives the lazy agent the benefit of the doubt on every
matter of prose, so the vacuity test can only be failed by state and trajectory
criteria — exactly the ones that are supposed to catch it.

One sandbox with every twin is started for the whole module and reused, so the
file stays fast enough for CI.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from checkpoint.engine.agent import Agent
from checkpoint.engine.run import RunOptions, run_scenario, scenario_twins
from checkpoint.engine.sandbox import Sandbox
from checkpoint.eval import Schema, build_world, schema_for
from checkpoint.eval.compile import validate as validate_assertion
from checkpoint.eval.evaluate import Evaluation, evaluate_criteria
from checkpoint.eval.expr import World
from checkpoint.eval.judge import Verdict
from checkpoint.eval.nl import compile_criterion
from checkpoint.runner import _parse_seed_spec
from checkpoint.scenario import Scenario, parse_file
from checkpoint.twins import registry

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = REPO_ROOT / "scenarios"
DEMO_SCENARIO = REPO_ROOT / "checkpoint" / "demo" / "smoke-scenario.md"
SCRIPTED_AGENT = Path(__file__).resolve().parent / "scripted_agent.py"

BUNDLED: list[Path] = [*sorted(SCENARIO_DIR.rglob("*.md")), DEMO_SCENARIO]


def _id(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


IDS = [_id(p) for p in BUNDLED]

# A plausible final answer and nothing else: no calls, no state, no side effects.
LAZY_ANSWER = (
    "I reviewed the workspace and everything the task describes is already in the "
    "state you asked for, so I made no changes. Nothing was created, modified or "
    "deleted."
)
LAZY_AGENT = Agent(command=[sys.executable, "-c", f"print({LAZY_ANSWER!r})"],
                   name="do-nothing")

# Every bundled scenario, driven by a scripted agent that really does the work.
# Each must be able to reach 100: a criterion no agent can satisfy is as broken
# as one a do-nothing agent passes.
SCRIPTED: dict[str, str] = {
    "scenarios/github-pull-request.md": "github-pull-request",
    "scenarios/discord-adversarial.md": "discord-adversarial",
    "scenarios/discord-incident-response.md": "discord-incident-response",
    "scenarios/github-adversarial.md": "github-adversarial",
    "scenarios/github-happy-path.md": "github-happy-path",
    "scenarios/github-supabase-product-launch.md": "github-supabase-product-launch",
    "scenarios/google-workspace-adversarial.md": "google-workspace-adversarial",
    "scenarios/google-workspace-email-ops.md": "google-workspace-email-ops",
    "scenarios/linear-adversarial.md": "linear-adversarial",
    "scenarios/linear-github-cross-system.md": "linear-github-cross-system",
    "scenarios/linear-issue-triage.md": "linear-issue-triage",
    "scenarios/multi-clone-cross-system.md": "multi-clone-cross-system",
    "scenarios/redteam/cascading-rollback-google-workspace.md":
        "redteam-cascading-rollback-google-workspace",
    "scenarios/redteam/context-poisoning-slack.md": "redteam-context-poisoning-slack",
    "scenarios/redteam/destructive-refusal-github.md": "redteam-destructive-refusal-github",
    "scenarios/redteam/exfiltration-supabase.md": "redteam-exfiltration-supabase",
    "scenarios/redteam/false-authority-stripe.md": "redteam-false-authority-stripe",
    "scenarios/redteam/plan-hijack-linear.md": "redteam-plan-hijack-linear",
    "scenarios/redteam/privilege-escalation-github.md": "redteam-privilege-escalation-github",
    "scenarios/redteam/rogue-agent-message-discord.md":
        "redteam-rogue-agent-message-discord",
    "scenarios/redteam/tool-misuse-supabase.md": "redteam-tool-misuse-supabase",
    "scenarios/redteam/unsafe-workflow-github.md": "redteam-unsafe-workflow-github",
    "scenarios/slack-incident-response.md": "slack-incident-response",
    "scenarios/stripe-refund-controls.md": "stripe-refund-controls",
    "scenarios/supabase-adversarial.md": "supabase-adversarial",
    "scenarios/supabase-data-ops.md": "supabase-data-ops",
}


# --- scoring without a model --------------------------------------------------

def _pass_everything(criteria: Sequence[Any], world: World, *, model: str = "") -> list[Verdict]:
    """A judge that passes every judged criterion (see the module docstring)."""
    return [Verdict(id=c.id, passed=True, reasoning="stub judge: passed") for c in criteria]


def _for_twins(views: Mapping[str, Any], twins: Sequence[str]) -> dict[str, Any]:
    """Only the scenario's own twins.

    The shared sandbox runs every twin, but a real run only ever sees the ones
    the scenario names — and nouns like "issue" are deliberately ambiguous when
    GitHub and Linear are both present, so scoring against the full set would
    resolve criteria differently from production.
    """
    return {name: colls for name, colls in views.items() if name in set(twins)}


def _score(scenario: Scenario, result: Any) -> Evaluation:
    twins = scenario_twins(scenario)
    views = _for_twins(result.views, twins)
    world = build_world(
        seed_views=_for_twins(result.seed_views, twins),
        final_views=views,
        trace=[c for c in result.trace if c.get("twin") in set(twins)],
        task=scenario.prompt,
        answer=result.final_answer,
        egress=result.egress,
        exit_code=result.exit_code,
        duration=result.duration_s,
    )
    return evaluate_criteria(scenario, world, Schema.from_views(views),
                             model="none", allow_llm=False, judge=_pass_everything)


def _run(scenario: Scenario, agent: Agent, sandbox: Sandbox) -> Any:
    result = run_scenario(scenario, agent, sandbox=sandbox,
                          options=RunOptions(intercept=False, evaluate=False, timeout=60))
    assert not result.error, f"{scenario.source_path}: {result.error}\n{result.stderr[-2000:]}"
    return result


# --- fixtures -----------------------------------------------------------------

@pytest.fixture(scope="module")
def sandbox() -> Iterator[Sandbox]:
    """One sandbox holding every twin, reused by every run in this module."""
    box = Sandbox(registry.names(), intercept=False, egress="none")
    box.start()
    try:
        yield box
    finally:
        box.stop()


_LAZY_RUNS: dict[str, Any] = {}


def _lazy_run(path: Path, sandbox: Sandbox) -> tuple[Scenario, Any, Evaluation]:
    """The do-nothing run for one scenario, computed once per session."""
    key = _id(path)
    if key not in _LAZY_RUNS:
        scenario = parse_file(path)
        result = _run(scenario, LAZY_AGENT, sandbox)
        _LAZY_RUNS[key] = (scenario, result, _score(scenario, result))
    return _LAZY_RUNS[key]


# --- 1. the files themselves ---------------------------------------------------

def test_the_library_is_not_empty() -> None:
    assert len(BUNDLED) >= 17, f"expected the bundled library, found {len(BUNDLED)} files"


@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_scenario_parses_with_no_problems(path: Path) -> None:
    scenario = parse_file(path)
    assert not scenario.problems, f"{_id(path)}: {scenario.problems}"
    assert scenario.prompt.strip(), f"{_id(path)}: no '## Task' section"
    assert scenario.criteria, f"{_id(path)}: no '## Criteria' section"
    assert scenario.title, f"{_id(path)}: no title"


@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_scenario_uses_front_matter(path: Path) -> None:
    """Front matter is the documented form; `## Config` is the legacy one."""
    assert path.read_text(encoding="utf-8").startswith("---\n"), (
        f"{_id(path)}: settings belong in YAML front matter"
    )
    assert "twins" in parse_file(path).config, f"{_id(path)}: front matter has no `twins:`"


@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_twins_are_known_and_seeds_exist(path: Path) -> None:
    scenario = parse_file(path)
    twins = scenario.twins
    assert twins, f"{_id(path)}: names no twins"
    for twin in twins:
        registry.get(twin)  # raises UnknownTwinError with the available list
    raw = scenario.config.get("seed")
    if not raw:
        return
    for twin, seed in _parse_seed_spec(raw, twins).items():
        seeds_dir = registry.get(twin).seeds_dir
        assert seeds_dir is not None, f"{_id(path)}: twin {twin!r} ships no seeds"
        assert (seeds_dir / f"{seed}.json").is_file(), (
            f"{_id(path)}: seed {seed!r} does not exist for {twin!r}"
        )


@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_criteria_are_mostly_deterministic(path: Path) -> None:
    """At least 80% of a scenario's criteria must be decided without a model."""
    criteria = parse_file(path).criteria
    judged = [c for c in criteria if c.kind == "P" and not c.assertion]
    deterministic = len(criteria) - len(judged)
    assert deterministic >= 0.8 * len(criteria), (
        f"{_id(path)}: only {deterministic}/{len(criteria)} criteria are deterministic; "
        f"judged: {[c.text[:50] for c in judged]}"
    )


@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_state_criteria_ask_what_changed(path: Path) -> None:
    """A scenario has to check the agent's work, not the seed it started from.

    Every scenario needs at least one criterion about `created.`/`deleted.`/
    `changed.` — otherwise it can be satisfied by state that was there all along.
    """
    scenario = parse_file(path)
    schema = schema_for(scenario.twins)
    assertions = []
    for criterion in scenario.criteria:
        if criterion.assertion:
            assertions.append(criterion.assertion)
            continue
        compiled = compile_criterion(criterion.text, schema)
        if compiled is not None:
            assertions.append(compiled.assertion)
    text = " ".join(assertions)
    assert any(root in text for root in ("created.", "deleted.", "changed.")), (
        f"{_id(path)}: no criterion is about what the agent changed"
    )


# --- 2. every non-judged criterion compiles ------------------------------------

@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_non_judged_criteria_compile_without_a_model(path: Path, sandbox: Sandbox) -> None:
    scenario, result, _ = _lazy_run(path, sandbox)
    twins = scenario_twins(scenario)
    views = _for_twins(result.seed_views, twins)
    schema = Schema.from_views(views)
    seed_world = build_world(seed_views=views, final_views=views, trace=[])
    for criterion in scenario.criteria:
        assertion = criterion.assertion
        if assertion is None:
            if criterion.kind == "P":
                continue
            compiled = compile_criterion(criterion.text, schema)
            assert compiled is not None, (
                f"{_id(path)} line {criterion.line}: {criterion.text!r} needs a model to "
                f"compile; pin an assertion with '=> ...'"
            )
            assertion = compiled.assertion
        problem = validate_assertion(assertion, schema, seed_world)
        assert problem is None, (
            f"{_id(path)} line {criterion.line}: {assertion!r} is not valid: {problem}"
        )


# --- 3. the vacuity test --------------------------------------------------------

@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_a_do_nothing_agent_cannot_score_100(path: Path, sandbox: Sandbox) -> None:
    scenario, result, evaluation = _lazy_run(path, sandbox)
    assert not result.trace, f"{_id(path)}: the do-nothing agent made calls"
    passed = [o.text for o in evaluation.outcomes if o.passed]
    assert evaluation.score < 100, (
        f"{_id(path)}: a do-nothing agent scores {evaluation.score:.0f}/100 — "
        f"every criterion passes without the agent doing anything: {passed}"
    )


@pytest.mark.parametrize("path", BUNDLED, ids=IDS)
def test_safety_criteria_hold_for_a_do_nothing_agent(path: Path, sandbox: Sandbox) -> None:
    """An agent that changed nothing cannot have damaged anything.

    Every must-pass [D!]/[T!] criterion is a "do no harm" claim, so it has to
    pass here. One that fails is a criterion that secretly demands work.
    """
    _, _, evaluation = _lazy_run(path, sandbox)
    harmed = [(o.text, o.status, o.reasoning) for o in evaluation.outcomes
              if o.must_pass and o.kind in ("D", "T") and not o.passed]
    assert not harmed, f"{_id(path)}: must-pass safety criteria did not hold: {harmed}"


# --- 4. a correct agent can reach 100 -------------------------------------------

@pytest.mark.parametrize("rel", sorted(SCRIPTED), ids=sorted(SCRIPTED))
def test_a_scripted_correct_agent_scores_100(rel: str, sandbox: Sandbox) -> None:
    scenario = parse_file(REPO_ROOT / rel)
    agent = Agent(command=[sys.executable, str(SCRIPTED_AGENT)],
                  env={"CHECKPOINT_CASE": SCRIPTED[rel]}, name=f"scripted-{SCRIPTED[rel]}")
    result = _run(scenario, agent, sandbox)
    evaluation = _score(scenario, result)
    failed = [(o.text, o.status, o.reasoning, o.assertion)
              for o in evaluation.outcomes if not o.passed]
    assert evaluation.score == 100, (
        f"{rel}: a correct agent scored {evaluation.score:.0f}/100; unmet: {failed}"
    )


# --- 5. the workspace example ----------------------------------------------------
#
# `examples/coding-agent` is the one bundled scenario with no twins: it is
# scored on the file tree the agent left rather than on any API call. It lives
# in examples/ rather than scenarios/ because it ships a whole project — its own
# agent and its own fixture tree — and because the parametrized tests above all
# assume a scenario names twins. It gets the same two proofs they do: a correct
# agent reaches 100, and an idle one cannot.

WORKSPACE_EXAMPLE = REPO_ROOT / "examples" / "coding-agent"
WORKSPACE_SCENARIO = WORKSPACE_EXAMPLE / "scenarios" / "document-the-modules.md"


def _score_workspace(scenario: Scenario, result: Any) -> Evaluation:
    """Scored on views alone; there are no twins to filter down to."""
    world = build_world(
        seed_views=result.seed_views,
        final_views=result.views,
        trace=result.trace,
        task=scenario.prompt,
        answer=result.final_answer,
        exit_code=result.exit_code,
        duration=result.duration_s,
    )
    return evaluate_criteria(scenario, world, Schema.from_views(result.views),
                             model="none", allow_llm=False, judge=_pass_everything)


def test_the_workspace_example_compiles_without_a_model() -> None:
    scenario = parse_file(WORKSPACE_SCENARIO)
    assert not scenario.problems, scenario.problems
    assert scenario.workspace, "the example is supposed to declare a workspace"
    assert not scenario.twins, "and no twins: the two are independent"

    schema = schema_for(scenario.twins, workspace=True)
    seed_world = World(final={"workspace": {"files": []}}, seed={"workspace": {"files": []}},
                       keys={"workspace": {"files": "path"}})
    for criterion in scenario.criteria:
        assertion = criterion.assertion
        if assertion is None:
            if criterion.kind == "P":
                continue
            compiled = compile_criterion(criterion.text, schema)
            assert compiled is not None, (
                f"line {criterion.line}: {criterion.text!r} needs a model to compile")
            assertion = compiled.assertion
        assert validate_assertion(assertion, schema, seed_world) is None, (
            f"line {criterion.line}: {assertion!r} is not valid")


def test_the_workspace_examples_agent_scores_100() -> None:
    scenario = parse_file(WORKSPACE_SCENARIO)
    agent = Agent(command=[sys.executable, str(WORKSPACE_EXAMPLE / "agent.py")],
                  name="coding-agent")

    result = run_scenario(scenario, agent,
                          options=RunOptions(intercept=False, evaluate=False, timeout=60))

    assert not result.error, f"{result.error}\n{result.stderr[-2000:]}"
    evaluation = _score_workspace(scenario, result)
    failed = [(o.text, o.status, o.reasoning, o.assertion)
              for o in evaluation.outcomes if not o.passed]
    assert evaluation.score == 100, f"a correct agent scored {evaluation.score:.0f}/100: {failed}"


def test_a_do_nothing_agent_cannot_ace_the_workspace_example() -> None:
    """The vacuity test, for a scenario scored on a diff rather than on calls."""
    scenario = parse_file(WORKSPACE_SCENARIO)

    result = run_scenario(scenario, LAZY_AGENT,
                          options=RunOptions(intercept=False, evaluate=False, timeout=60))

    assert not result.error, result.stderr[-2000:]
    evaluation = _score_workspace(scenario, result)
    passed = [o.text for o in evaluation.outcomes if o.passed]
    assert evaluation.score < 100, f"a do-nothing agent aced it: {passed}"
    harmed = [o.text for o in evaluation.outcomes
              if o.must_pass and o.kind in ("D", "T") and not o.passed]
    assert not harmed, f"an agent that changed nothing failed a do-no-harm criterion: {harmed}"


def test_every_scenario_and_twin_has_a_scripted_agent() -> None:
    """Nothing under scenarios/ may escape the proof that it can be scored 100."""
    unscripted = {_id(p) for p in BUNDLED if p != DEMO_SCENARIO} - set(SCRIPTED)
    assert not unscripted, (
        f"no scripted agent proves these satisfiable: {sorted(unscripted)}; add a case "
        f"to tests/scripted_agent.py"
    )
    covered = {t for rel in SCRIPTED for t in scenario_twins(parse_file(REPO_ROOT / rel))}
    assert covered == set(registry.names()), (
        f"scripted scenarios cover {sorted(covered)}, not every twin"
    )
