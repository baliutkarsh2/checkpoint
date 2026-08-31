"""Drive a simulated user through a multi-turn conversation with the agent.

Twins are started once and kept alive for the whole conversation, so the agent's
actions accumulate turn over turn (the whole point of multi-turn testing). Each
turn re-invokes the harness with the running transcript as its task.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from ..runner import (
    _CLONE_BOOTSTRAP_TOKEN_ENV,
    TWIN_APPS,
    RunResult,
    _apply_named_seed,
    _apply_seed_file,
    _evaluate,
    _extract_final_answer,
    _fetch_state,
    _fetch_trace,
    _free_port,
    _merge_state_for_clones,
    _merge_trace_for_clones,
    _parse_seed_spec,
    _start_twin,
    _wait_healthy,
)
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


def _render_task(transcript: list[dict]) -> str:
    lines = [
        "You are in an ongoing conversation with a user. Continue it: read the "
        "history and respond to the user's most recent message.",
        "",
    ]
    for turn in transcript:
        who = "USER" if turn["role"] == "user" else "ASSISTANT"
        lines.append(f"{who}: {turn['content']}")
    return "\n".join(lines)


def _run_harness_turn(twins, harness_cmd, task, timeout, cwd) -> tuple[str, str | None]:
    env = dict(os.environ)
    env["CHECKPOINT_TASK"] = task
    env["ARCHAL_ENGINE_TASK"] = task
    env["ARCHAL_ENGINE_MODE"] = "local"
    env["CHECKPOINT_BASE_URL"] = f"http://127.0.0.1:{twins[0][1]}"
    for clone, port, _ in twins:
        env[f"CHECKPOINT_{clone.upper()}_URL"] = f"http://127.0.0.1:{port}"
        tok = _CLONE_BOOTSTRAP_TOKEN_ENV.get(clone)
        if tok:
            env[tok[0]] = tok[1]
    try:
        proc = subprocess.run(
            list(harness_cmd), cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        return "", f"harness not found: {e}"
    except subprocess.TimeoutExpired:
        return "", f"harness exceeded timeout of {timeout}s"
    if proc.returncode != 0:
        return _extract_final_answer(proc.stdout), f"harness exited {proc.returncode}"
    return _extract_final_answer(proc.stdout), None


def simulate(
    scenario,
    harness_cmd: list[str],
    persona: Persona,
    *,
    max_turns: int = 6,
    judge_model: str = "gpt-4o-mini",
    user=None,
    cwd: str | None = None,
) -> SimResult:
    """Run a multi-turn conversation and evaluate the final state."""
    clones = scenario.clones or ["github"]
    unknown = [c for c in clones if c not in TWIN_APPS]
    if unknown:
        return SimResult(persona.name, 0, error=f"Unknown clones: {unknown}")

    user = user or LLMSimulatedUser(judge_model)
    twins: list[tuple[str, int, subprocess.Popen]] = []
    transcript: list[dict] = []
    turns = 0
    satisfied = gave_up = False

    try:
        for clone in clones:
            port = _free_port()
            twins.append((clone, port, _start_twin(clone, port)))
        for clone, port, _ in twins:
            if not _wait_healthy(port):
                return SimResult(persona.name, 0, error=f"Twin {clone!r} failed to start")

        # Seed once, up front (state then persists across turns).
        seed_map = _parse_seed_spec(scenario.config.get("seed") or scenario.config.get("seed_name"), clones)
        seed_file_map = _parse_seed_spec(scenario.config.get("seed-file") or scenario.config.get("seed_file"), clones)
        for clone, port, _ in twins:
            if seed_file_map.get(clone):
                _apply_seed_file(port, seed_file_map[clone], scenario.source_path)
            elif seed_map.get(clone):
                _apply_named_seed(port, seed_map[clone])

        current_msg = persona.goal or getattr(scenario, "prompt", "") or ""
        turn_error: str | None = None

        while turns < max_turns:
            turns += 1
            transcript.append({"role": "user", "content": current_msg})
            answer, err = _run_harness_turn(twins, harness_cmd, _render_task(transcript), scenario.timeout, cwd)
            transcript.append({"role": "assistant", "content": answer})
            if err:
                turn_error = err
                break

            decision: UserTurn = user.next(transcript, persona)
            if decision.satisfied:
                satisfied = True
                if decision.message:
                    transcript.append({"role": "user", "content": decision.message})
                break
            if decision.gave_up or turns >= persona.patience:
                gave_up = True
                break
            if not decision.message:
                break
            current_msg = decision.message

        # Final evaluation against accumulated state + trace.
        per_state = {clone: _fetch_state(port) for clone, port, _ in twins}
        per_trace = {clone: _fetch_trace(port) for clone, port, _ in twins}
        last_answer = next((t["content"] for t in reversed(transcript) if t["role"] == "assistant"), "")
        result = RunResult(
            final_answer=last_answer,
            stderr="",
            exit_code=0,
            trace=_merge_trace_for_clones(per_trace),
            state=_merge_state_for_clones(per_state),
        )
        if turn_error:
            result.error = turn_error
        else:
            _evaluate(scenario, result, judge_model)

        calibration = compute_calibration(turns, max_turns, persona, satisfied, gave_up)
        return SimResult(
            persona_name=persona.name,
            turns=turns,
            transcript=transcript,
            satisfied=satisfied,
            gave_up=gave_up,
            result=result,
            calibration=calibration,
            error=turn_error,
        )
    finally:
        for _clone, _port, proc in twins:
            try:
                proc.terminate()
            except Exception:
                pass
