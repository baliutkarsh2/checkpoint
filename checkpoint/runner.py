"""Orchestrate a single scenario run."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

import httpx

from .checker import check
from .judge import judge
from .scenario import Criterion, Scenario


@dataclass
class CriterionResult:
    text: str
    kind: str
    passed: bool
    reasoning: str
    evaluator: str  # "deterministic" or "llm"


@dataclass
class RunResult:
    final_answer: str
    stderr: str
    exit_code: int
    trace: list
    state: dict
    criteria: list[CriterionResult] = field(default_factory=list)
    error: str | None = None

    @property
    def score(self) -> float:
        if not self.criteria:
            return 0.0
        return 100.0 * sum(1 for c in self.criteria if c.passed) / len(self.criteria)

    @property
    def complete(self) -> bool:
        return self.error is None and self.exit_code == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TWIN_APPS = {
    "github": "checkpoint.twins.github:app",
    "slack": "checkpoint.twins.slack:app",
    "stripe": "checkpoint.twins.stripe:app",
}


def _start_twin(clone: str, port: int) -> subprocess.Popen:
    app = TWIN_APPS.get(clone)
    if app is None:
        raise ValueError(f"unsupported clone={clone!r}; known: {sorted(TWIN_APPS)}")
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            app,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_healthy(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/_health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _extract_final_answer(stdout: str) -> str:
    stdout = stdout.strip()
    if not stdout:
        return ""
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            if "text" in obj:
                return str(obj["text"])
            if isinstance(obj.get("payloads"), list):
                return "\n".join(str(p) for p in obj["payloads"])
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "text" in obj:
                    return str(obj["text"])
            except json.JSONDecodeError:
                continue
    return "\n".join(l for l in stdout.splitlines() if l.strip())


def _fetch_trace(port: int) -> list:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/_trace", timeout=5).json()
    except Exception:
        return []


def _fetch_state(port: int) -> dict:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/_state", timeout=5).json()
    except Exception:
        return {}


def run_once(
    scenario: Scenario,
    harness_cmd: list[str],
    cwd: str | None = None,
    judge_model: str = "gpt-4o-mini",
) -> RunResult:
    clones = scenario.clones or ["github"]
    if len(clones) > 1:
        return RunResult("", "", -1, [], {}, error=f"Multi-clone not supported in v0: {clones}")
    clone = clones[0]
    port = _free_port()
    twin = _start_twin(clone, port)

    try:
        if not _wait_healthy(port):
            return RunResult("", "", -1, [], {}, error="Twin failed to start")

        base_url = f"http://127.0.0.1:{port}"
        env = dict(os.environ)
        env["CHECKPOINT_TASK"] = scenario.prompt
        env["CHECKPOINT_BASE_URL"] = base_url
        env[f"CHECKPOINT_{clone.upper()}_URL"] = base_url
        env["ARCHAL_ENGINE_TASK"] = scenario.prompt
        env["ARCHAL_ENGINE_MODE"] = "local"
        # Inject per-twin bootstrap tokens so harnesses talking directly to
        # the local twin (non-Docker, no TLS sidecar) authenticate cleanly.
        if clone == "github":
            env["GITHUB_TOKEN"] = "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt"
        elif clone == "slack":
            env["SLACK_TOKEN"] = "xoxb-123456789012-234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
        elif clone == "stripe":
            env["STRIPE_API_KEY"] = "sk_live_51Abc123DefGhiJklMnoPqrStUvWxYz0123456789"

        try:
            proc = subprocess.run(
                harness_cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=scenario.timeout,
            )
        except subprocess.TimeoutExpired as e:
            return RunResult(
                final_answer="",
                stderr=(e.stderr or "")[-4000:] if isinstance(e.stderr, (bytes, str)) else "",
                exit_code=-1,
                trace=_fetch_trace(port),
                state=_fetch_state(port),
                error=f"Harness exceeded timeout of {scenario.timeout}s",
            )

        result = RunResult(
            final_answer=_extract_final_answer(proc.stdout),
            stderr=(proc.stderr or "")[-4000:],
            exit_code=proc.returncode,
            trace=_fetch_trace(port),
            state=_fetch_state(port),
        )

        if proc.returncode != 0:
            result.error = f"Harness exited {proc.returncode}"
            return result

        _evaluate(scenario, result, judge_model)
        return result
    finally:
        twin.terminate()
        try:
            twin.wait(timeout=3)
        except subprocess.TimeoutExpired:
            twin.kill()


def _evaluate(scenario: Scenario, result: RunResult, judge_model: str) -> None:
    deferred: list[Criterion] = []
    for c in scenario.criteria:
        if c.kind == "D":
            cr = check(c.text, result.state, result.trace)
            if cr.handled:
                result.criteria.append(CriterionResult(
                    text=c.text, kind="D", passed=cr.passed,
                    reasoning=cr.reasoning, evaluator="deterministic",
                ))
                continue
            deferred.append(c)
        else:
            deferred.append(c)

    if not deferred:
        return

    try:
        verdicts = judge(
            task=scenario.prompt,
            final_answer=result.final_answer,
            trace=result.trace,
            state=result.state,
            criteria=[c.text for c in deferred],
            model=judge_model,
        )
    except Exception as e:
        for c in deferred:
            result.criteria.append(CriterionResult(
                text=c.text, kind=c.kind, passed=False,
                reasoning=f"Judge failed: {e}", evaluator="llm",
            ))
        return

    for c, v in zip(deferred, verdicts):
        result.criteria.append(CriterionResult(
            text=c.text, kind=c.kind, passed=v.passed,
            reasoning=v.reasoning, evaluator="llm",
        ))
