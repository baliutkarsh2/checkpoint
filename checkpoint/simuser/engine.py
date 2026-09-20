"""Drive a simulated user through a multi-turn conversation with the agent.

One sandbox lives for the whole conversation, so the agent's actions accumulate
turn over turn — the point of multi-turn testing. Each turn the agent receives
the user's latest message as its task and the full conversation as
``$CHECKPOINT_MESSAGES`` (JSON); HTTP agents receive both in the request body
with a stable ``session_id``, so a stateful service can keep its own memory.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace

from ..engine import (
    Agent,
    RunOptions,
    Sandbox,
    SandboxError,
    scenario_setups,
    scenario_twins,
    scenario_workspace,
)
from ..engine.run import run_state
from ..llm import DEFAULT_MODEL
from ..runner import RunResult, _evaluate
from .calibration import compute_calibration
from .persona import Persona, UserTurn
from .user import LLMSimulatedUser


@dataclass
class SimResult:
    persona_name: str
    turns: int
    transcript: list[dict] = field(default_factory=list)
    satisfied: bool = False
    gave_up: bool = False
    result: RunResult | None = None
    calibration: float = 0.0
    error: str | None = None

    @property
    def score(self) -> float:
        return self.result.score if self.result else 0.0


def simulate(
    scenario,
    command: list[str] | str | None,
    persona: Persona,
    *,
    max_turns: int = 6,
    judge_model: str = DEFAULT_MODEL,
    user=None,
    cwd: str | None = None,
    agent: Agent | None = None,
    options: RunOptions | None = None,
) -> SimResult:
    """Run a multi-turn conversation, then score the final state and last answer."""
    opts = options or RunOptions(judge_model=judge_model)
    agent = agent or Agent(command=command or (), cwd=cwd)
    user = user or LLMSimulatedUser(opts.judge_model)
    session_id = uuid.uuid4().hex[:12]
    transcript: list[dict] = []
    turns = 0
    satisfied = gave_up = False
    turn_error: str | None = None
    started = time.perf_counter()

    try:
        twins = scenario_twins(scenario)
        setups = scenario_setups(scenario, twins)
        workspace_seed = scenario_workspace(scenario)
        sandbox = Sandbox(twins, intercept=opts.intercept, egress=opts.egress,
                          allow_hosts=opts.allow_hosts,
                          workspace=workspace_seed is not None)
    except (SandboxError, KeyError) as e:
        return SimResult(persona.name, 0, error=f"sandbox setup failed: {e}")

    try:
        with sandbox:
            sandbox.prepare(setups, workspace_seed=workspace_seed)
            seed_views = sandbox.views()
            env = sandbox.agent_env()
            if sandbox.workspace_root is not None and not agent.cwd:
                agent = replace(agent, cwd=str(sandbox.workspace_root))
            timeout = opts.timeout or float(scenario.timeout)
            message = persona.goal or getattr(scenario, "prompt", "") or ""

            while turns < max_turns:
                turns += 1
                transcript.append({"role": "user", "content": message})
                output = agent.invoke(message, env, timeout, messages=list(transcript),
                                      session_id=session_id, on_line=opts.on_line)
                transcript.append({"role": "assistant", "content": output.answer})
                if not output.ok:
                    turn_error = output.error or (
                        f"agent timed out after {timeout:.0f}s" if output.timed_out
                        else f"agent exited with code {output.exit_code}"
                    )
                    break
                decision: UserTurn = user.next(transcript, persona)
                if decision.satisfied:
                    satisfied = True
                    if decision.message:
                        transcript.append({"role": "user", "content": decision.message})
                    break
                if decision.gave_up or turns >= persona.patience or not decision.message:
                    gave_up = decision.gave_up or turns >= persona.patience
                    break
                message = decision.message

            final_views = sandbox.views()
            final_state = sandbox.state()
            trace = sandbox.trace()
            egress = sandbox.egress_events()
    except SandboxError as e:
        return SimResult(persona.name, turns, transcript, error=f"sandbox setup failed: {e}")

    last_answer = next((t["content"] for t in reversed(transcript) if t["role"] == "assistant"), "")
    result = RunResult(
        final_answer=last_answer,
        stderr="",
        exit_code=0,
        trace=trace,
        state=run_state(final_state),
        run_id=session_id,
        agent=agent.display_name,
        twins=list(twins),
        seed_views=seed_views,
        views=final_views,
        egress=egress,
        duration_s=round(time.perf_counter() - started, 3),
    )
    if turn_error:
        result.error = turn_error
    else:
        _evaluate(scenario, result, opts.judge_model)

    return SimResult(
        persona_name=persona.name,
        turns=turns,
        transcript=transcript,
        satisfied=satisfied,
        gave_up=gave_up,
        result=result,
        calibration=compute_calibration(turns, max_turns, persona, satisfied, gave_up),
        error=turn_error,
    )
