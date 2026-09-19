"""Run one scenario against one agent inside a sandbox, and score it."""
from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from checkpoint.twins import registry

from .agent import Agent, LineSink
from .sandbox import Egress, Sandbox, SandboxError, TwinSetup, load_seed_file

if TYPE_CHECKING:
    from checkpoint.runner import RunResult
    from checkpoint.scenario import Scenario

DEFAULT_TIMEOUT = 180.0


@dataclass
class RunOptions:
    """Per-run knobs that are not part of the scenario file."""

    judge_model: str = "gpt-4o-mini"
    timeout: float | None = None
    """Seconds before the agent is killed; defaults to the scenario's ``timeout``."""
    intercept: bool = False
    egress: Egress = "llm"
    allow_hosts: tuple[str, ...] = ()
    faults: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """Extra per-twin config applied after seeding: ``{"github": {"rate_limit": 5}}``;
    the key ``"*"`` applies to every twin."""
    read_only: bool = False
    """Refuse every write and fail the run if the agent attempted one."""
    evaluate: bool = True
    on_line: LineSink | None = None


def scenario_twins(scenario: Scenario) -> list[str]:
    """The twins a scenario runs against (``twins:``, or the older ``clones:``)."""
    raw = scenario.config.get("twins") or scenario.config.get("clones") or ""
    names = [n.strip() for n in str(raw).split(",") if n.strip()]
    return [registry.get(n).name for n in names]


def scenario_setups(scenario: Scenario, twins: list[str]) -> dict[str, TwinSetup]:
    """Seeds and seed files per twin, from the scenario's config."""
    from checkpoint.runner import _parse_seed_spec

    setups = {name: TwinSetup() for name in twins}
    if not twins:
        return setups
    seeds = _parse_seed_spec(scenario.config.get("seed"), twins)
    seed_files = _parse_seed_spec(scenario.config.get("seed-file"), twins)
    base = Path(scenario.source_path).parent if scenario.source_path else Path.cwd()
    for name, value in seeds.items():
        setups[registry.get(name).name].seed = value
    for name, value in seed_files.items():
        path = Path(value)
        if not path.is_absolute():
            # Relative to the scenario file, falling back to the working directory.
            path = base / path if (base / path).exists() else Path.cwd() / path
        setups[registry.get(name).name].seed_data = load_seed_file(path)
    return setups


def run_scenario(
    scenario: Scenario,
    agent: Agent,
    *,
    sandbox: Sandbox | None = None,
    options: RunOptions | None = None,
) -> RunResult:
    """Run ``agent`` on ``scenario`` and return a scored result.

    Pass a started ``sandbox`` to reuse it across runs (the gate does); it must
    contain the scenario's twins. Otherwise one is built and torn down here.
    Never raises for agent or sandbox failures — they are reported on the result.
    """
    from checkpoint.runner import RunResult

    opts = options or RunOptions()
    run_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        twins = scenario_twins(scenario)
        setups = scenario_setups(scenario, twins)
    except (SandboxError, KeyError) as e:
        return _setup_failure(RunResult, run_id, agent, str(e), started)

    owned = sandbox is None
    try:
        if owned:
            sandbox = Sandbox(twins, intercept=opts.intercept, egress=opts.egress,
                              allow_hosts=opts.allow_hosts)
            sandbox.start()
        else:
            missing = [t for t in twins if t not in sandbox.twins]
            if missing:
                raise SandboxError(f"sandbox is missing twins {missing} needed by the scenario")
        _apply_faults(setups, opts)
        sandbox.prepare(setups)
        seed_views = sandbox.views()
        timeout = opts.timeout or float(scenario.timeout or DEFAULT_TIMEOUT)
        output = agent.invoke(scenario.prompt, sandbox.agent_env(), timeout,
                              session_id=run_id, on_line=opts.on_line)
        final_views = sandbox.views()
        final_state = sandbox.state()
        trace = sandbox.trace()
        egress = sandbox.egress_events()
    except SandboxError as e:
        return _setup_failure(RunResult, run_id, agent, str(e), started)
    finally:
        if owned and sandbox is not None:
            sandbox.stop()

    from checkpoint.runner import _merge_state_for_clones

    result = RunResult(
        final_answer=output.answer,
        stderr=output.stderr[-8000:],
        exit_code=output.exit_code if output.exit_code is not None else -1,
        trace=trace,
        state=_merge_state_for_clones(final_state) if final_state else {},
        stdout=output.stdout,
        run_id=run_id,
        agent=agent.display_name,
        twins=list(twins),
        seed_views=seed_views,
        views=final_views,
        egress=egress,
        duration_s=round(time.perf_counter() - started, 3),
        timed_out=output.timed_out,
    )
    result.warnings.extend(_diagnose(result, twins))
    if opts.read_only:
        # Recorded even when the refused write made the agent crash: the attempt
        # is the violation.
        writes = [c for c in trace if c.get("op") in ("create", "update", "delete")]
        if writes:
            from checkpoint.runner import CriterionResult

            first = writes[0]
            result.criteria.append(CriterionResult(
                text="read-only: the agent made no writes",
                kind="D", passed=False, evaluator="read-only-guard",
                reasoning=f"{len(writes)} write call(s), first: {first.get('method')} {first.get('path')}",
            ))
    if output.error:
        result.error = output.error
        return result
    if output.timed_out:
        result.error = f"agent timed out after {timeout:.0f}s"
        return result
    if output.exit_code != 0:
        result.error = f"agent exited with code {output.exit_code}"
        return result
    if opts.evaluate:
        from checkpoint.runner import _evaluate

        _evaluate(scenario, result, opts.judge_model)
    return result


def _apply_faults(setups: dict[str, TwinSetup], opts: RunOptions) -> None:
    for name, setup in setups.items():
        extra = {**opts.faults.get("*", {}), **opts.faults.get(name, {})}
        if opts.read_only:
            extra["read_only"] = True
        if extra:
            setup.config = {**setup.config, **extra}


def _setup_failure(result_cls: Any, run_id: str, agent: Agent, message: str, started: float) -> RunResult:
    result = result_cls("", "", -1, [], {}, run_id=run_id, agent=agent.display_name,
                        duration_s=round(time.perf_counter() - started, 3))
    result.error = f"sandbox setup failed: {message}"
    result.setup_error = True
    return result


def _diagnose(result: RunResult, twins: list[str]) -> list[str]:
    """Plain-English hints about runs that technically worked but look wrong."""
    notes: list[str] = []
    if twins and not result.trace and result.exit_code == 0 and not result.timed_out:
        notes.append(
            "The agent made no calls to the sandboxed services "
            f"({', '.join(twins)}). If it should have, make sure its HTTP client honours "
            "HTTPS_PROXY, or point it at the CHECKPOINT_<TWIN>_URL variables."
        )
    blocked = Counter(e.get("host") for e in result.egress if e.get("allowed") is False)
    for host, count in blocked.most_common():
        notes.append(
            f"Blocked {count} connection(s) to {host} (outside the sandbox). If the agent needs it, "
            f"allow it with --allow-host {host} or allow_hosts in checkpoint.toml."
        )
    return notes
