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


_CLONE_BOOTSTRAP_TOKEN_ENV = {
    "github": ("GITHUB_TOKEN", "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt"),
    "slack": ("SLACK_TOKEN", "xoxb-123456789012-234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    "stripe": ("STRIPE_API_KEY", "sk_live_51Abc123DefGhiJklMnoPqrStUvWxYz0123456789"),
}


def _merge_state_for_clones(per_clone_state: dict[str, dict]) -> dict:
    """Build the `state` field on RunResult for multi-clone runs.

    Single-clone runs keep the legacy flat shape (top-level keys are twin state
    keys like `repositories`, `pull_requests`, etc.) so deterministic checks
    against existing scenarios keep working. Multi-clone runs use a nested
    `{clone_id: state}` shape and the deterministic checker walks both.
    """
    if len(per_clone_state) == 1:
        return next(iter(per_clone_state.values()))
    return dict(per_clone_state)


def _merge_trace_for_clones(per_clone_trace: dict[str, list]) -> list:
    """Concatenate per-clone traces. Each entry is tagged with `_clone` so
    callers can filter when needed."""
    if len(per_clone_trace) == 1:
        return next(iter(per_clone_trace.values()))
    out: list = []
    for clone, entries in per_clone_trace.items():
        for e in entries:
            if isinstance(e, dict) and "_clone" not in e:
                e = {**e, "_clone": clone}
            out.append(e)
    return out


def run_once(
    scenario: Scenario,
    harness_cmd: list[str],
    cwd: str | None = None,
    judge_model: str = "gpt-4o-mini",
) -> RunResult:
    clones = scenario.clones or ["github"]
    unknown = [c for c in clones if c not in TWIN_APPS]
    if unknown:
        return RunResult("", "", -1, [], {}, error=f"Unknown clones: {unknown}")

    # Phase-4: start one twin per clone, on its own free port.
    twins: list[tuple[str, int, subprocess.Popen]] = []
    try:
        for clone in clones:
            port = _free_port()
            proc = _start_twin(clone, port)
            twins.append((clone, port, proc))

        for clone, port, _ in twins:
            if not _wait_healthy(port):
                return RunResult("", "", -1, [], {}, error=f"Twin {clone!r} failed to start on :{port}")

        # Apply seeds. Format options:
        #   seed: small-project                  -> applies to first clone only (legacy)
        #   seed: github=small-project, slack=engineering-team
        #   seed-file: ./seed.json               -> applies to first clone (raw state)
        #   seed-file: github=./gh.json, slack=./sl.json
        # `seed:` and `seed-file:` may both be present; seed-file wins per-clone.
        seed_map = _parse_seed_spec(scenario.config.get("seed") or scenario.config.get("seed_name"), clones)
        seed_file_map = _parse_seed_spec(scenario.config.get("seed-file") or scenario.config.get("seed_file"), clones)

        for clone, port, _ in twins:
            sf = seed_file_map.get(clone)
            sn = seed_map.get(clone)
            if sf:
                err = _apply_seed_file(port, sf, scenario.source_path)
                if err:
                    return RunResult("", "", -1, [], {}, error=err)
            elif sn:
                err = _apply_named_seed(port, sn)
                if err:
                    return RunResult("", "", -1, [], {}, error=err)
            elif _setup_seed_enabled(scenario) and scenario.setup and scenario.setup.strip():
                # SCN-08: derive a JSON seed from the `## Setup` prose. Soft-fail
                # if OPENAI_API_KEY is missing — twin keeps its default fresh state.
                # Opt-in via `setup-seed: true` in the scenario config (or
                # `setup-seed: auto`) so existing Phase 1-3 scenarios with
                # descriptive ## Setup prose don't suddenly get LLM-generated state.
                err = _apply_setup_derived_seed(port, clone, scenario.setup)
                if err:
                    # Log but don't abort: a missing key is the most common case.
                    print(f"[checkpoint] seed-from-setup skipped for {clone}: {err}", flush=True)

        env = dict(os.environ)
        env["CHECKPOINT_TASK"] = scenario.prompt
        env["ARCHAL_ENGINE_TASK"] = scenario.prompt
        env["ARCHAL_ENGINE_MODE"] = "local"
        first_clone, first_port, _ = twins[0]
        env["CHECKPOINT_BASE_URL"] = f"http://127.0.0.1:{first_port}"
        for clone, port, _ in twins:
            env[f"CHECKPOINT_{clone.upper()}_URL"] = f"http://127.0.0.1:{port}"
            tok = _CLONE_BOOTSTRAP_TOKEN_ENV.get(clone)
            if tok:
                env[tok[0]] = tok[1]

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
            per_state = {clone: _fetch_state(port) for clone, port, _ in twins}
            per_trace = {clone: _fetch_trace(port) for clone, port, _ in twins}
            return RunResult(
                final_answer="",
                stderr=(e.stderr or "")[-4000:] if isinstance(e.stderr, (bytes, str)) else "",
                exit_code=-1,
                trace=_merge_trace_for_clones(per_trace),
                state=_merge_state_for_clones(per_state),
                error=f"Harness exceeded timeout of {scenario.timeout}s",
            )

        per_state = {clone: _fetch_state(port) for clone, port, _ in twins}
        per_trace = {clone: _fetch_trace(port) for clone, port, _ in twins}
        result = RunResult(
            final_answer=_extract_final_answer(proc.stdout),
            stderr=(proc.stderr or "")[-4000:],
            exit_code=proc.returncode,
            trace=_merge_trace_for_clones(per_trace),
            state=_merge_state_for_clones(per_state),
        )

        if proc.returncode != 0:
            result.error = f"Harness exited {proc.returncode}"
            return result

        _evaluate(scenario, result, judge_model)
        return result
    finally:
        for _, _, proc in twins:
            proc.terminate()
        for _, _, proc in twins:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def _parse_seed_spec(raw: str | None, clones: list[str]) -> dict[str, str]:
    """Parse `seed:` / `seed-file:` config into a {clone: value} map.

    Single value (no `=`) applies to the first clone only (legacy v0 behavior).
    Comma-separated `clone=value` pairs apply per-clone. Unknown clones are
    ignored silently — they may be intentionally excluded from a run.
    """
    if not raw:
        return {}
    raw = raw.strip()
    if "=" not in raw:
        # Single value: apply to first clone (legacy behavior).
        return {clones[0]: raw} if clones else {}
    out: dict[str, str] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if k and v:
            out[k] = v
    return out


def _apply_named_seed(port: int, name: str) -> str | None:
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/_seed/{name}", timeout=5)
    except Exception as e:
        return f"Seed {name!r} request failed on :{port}: {e}"
    if r.status_code != 200:
        return f"Seed {name!r} failed on :{port}: {r.status_code} {r.text[:200]}"
    return None


_SETUP_SEED_TRUTHY = {"true", "1", "yes", "auto", "on"}


def _setup_seed_enabled(scenario: Scenario) -> bool:
    """SCN-08 opt-in flag: `setup-seed: true` in `## Config`."""
    val = (scenario.config.get("setup-seed") or scenario.config.get("setup_seed") or "").strip().lower()
    return val in _SETUP_SEED_TRUTHY


def _apply_setup_derived_seed(port: int, clone: str, setup_text: str) -> str | None:
    """SCN-08: generate a seed from `## Setup` prose and POST to /_seed-file.

    Returns an error string on failure (caller decides whether to abort or
    soft-skip). Cache hits avoid any LLM call.
    """
    try:
        # Fetch twin's current state so the LLM has a schema sample.
        twin_state = _fetch_state(port)
        from .scenario_seed_gen import generate_seed

        seed = generate_seed(clone, setup_text, twin_state)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"seed-from-setup failed for {clone}: {e}"
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/_seed-file", json=seed, timeout=5)
    except Exception as e:
        return f"seed-from-setup POST failed on :{port}: {e}"
    if r.status_code != 200:
        return f"seed-from-setup POST rejected on :{port}: {r.status_code} {r.text[:200]}"
    return None


def _apply_seed_file(port: int, file_path: str, scenario_source: str | None) -> str | None:
    """Load a JSON file and POST it to the twin.

    File format options:
      1. `{"state": {...}}` — same shape `/_seed/<name>` JSON files use.
      2. `{...}` — treated as a raw state replacement.

    Relative paths resolve against the scenario file's directory if known,
    else against cwd.
    """
    from pathlib import Path

    p = Path(file_path)
    if not p.is_absolute() and scenario_source:
        base = Path(scenario_source).parent
        candidate = (base / p).resolve()
        if candidate.exists():
            p = candidate
    if not p.exists():
        return f"seed-file not found: {file_path}"
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return f"seed-file {file_path}: {e}"
    payload = data if "state" in data else {"state": data}
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/_seed-file", json=payload, timeout=5)
    except Exception as e:
        return f"seed-file POST failed on :{port}: {e}"
    if r.status_code != 200:
        return f"seed-file POST failed on :{port}: {r.status_code} {r.text[:200]}"
    return None


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
