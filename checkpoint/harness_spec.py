"""Declarative harness spec — the "zero user code" contract.

A user with an existing agent shouldn't have to write any Python to use
Checkpoint. They describe *how to invoke* their agent in a tiny manifest, and
Checkpoint does the rest:

    {
      "name": "my-agent",
      "command": "python my_agent.py --task",
      "task_via": "arg",          // or "env" | "stdin" | "none"
      "env": {
        "OPENAI_API_KEY": "$OPENAI_API_KEY"
      },
      "stdout_format": "json"     // expects {"text": "..."}; alternative is "text"
    }

The spec drives :class:`HarnessSpec` which our runner turns into:
  - an argv list to spawn
  - an env dict (with ``$VAR`` expanded against the parent process env)
  - a strategy for injecting the scenario prompt (env / arg / stdin)
  - a parser for stdout (JSON or plain text)

Backwards-compatible: a v1 ``harness.json`` with just ``{"path": "..."}`` is
treated as ``{"command": "python <path>", "task_via": "env"}``.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

TaskVia = Literal["env", "arg", "stdin", "none"]
StdoutFormat = Literal["json", "text"]

DEFAULT_TASK_ENV = "CHECKPOINT_TASK"


@dataclass
class HarnessSpec:
    """Normalized representation of a harness invocation contract."""

    argv: list[str]
    """The command to spawn, as an argv list. Always non-empty."""

    task_via: TaskVia = "env"
    task_env: str = DEFAULT_TASK_ENV
    task_arg: str | None = None  # only used when task_via == "arg"

    env: dict[str, str] = field(default_factory=dict)
    """Extra env vars; ``$VAR`` already expanded against the parent process env."""

    working_dir: str | None = None
    stdout_format: StdoutFormat = "json"
    name: str | None = None

    # Optional Docker mode.
    dockerfile: str | None = None
    image: str | None = None
    harness_dir: str | None = None  # the dir containing the Dockerfile

    source_path: str | None = None  # path of the manifest this came from

    # ---- post-init helpers ----------------------------------------------

    def is_docker(self) -> bool:
        return bool(self.dockerfile or self.image)

    def build_invocation(self, task: str) -> tuple[list[str], dict[str, str], str | None]:
        """Compose the (argv, env, stdin) for a particular scenario task.

        Returns a 3-tuple suitable for subprocess.run / Popen:
          - argv: list of command-line tokens including any --task arg
          - env: complete env dict (process env + spec env + task env)
          - stdin: str to pipe to stdin, or None
        """
        argv = list(self.argv)
        env = {**os.environ, **self.env}
        stdin: str | None = None
        if self.task_via == "env":
            env[self.task_env] = task
        elif self.task_via == "arg":
            if self.task_arg:
                argv.extend([self.task_arg, task])
            else:
                argv.append(task)
        elif self.task_via == "stdin":
            stdin = task
        # "none" — caller must have wired the task in some other way.
        return argv, env, stdin


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_inline(
    command: str,
    *,
    task_via: TaskVia = "env",
    task_env: str = DEFAULT_TASK_ENV,
    task_arg: str | None = None,
    env: dict[str, str] | None = None,
    working_dir: str | None = None,
    stdout_format: StdoutFormat = "json",
    name: str | None = None,
) -> HarnessSpec:
    """Build a spec from a single shell-style command string.

    Used by ``checkpoint run --command "python my_agent.py"`` and by tests.
    """
    argv = shlex.split(command, posix=sys.platform != "win32")
    if not argv:
        raise ValueError("command is empty")
    return HarnessSpec(
        argv=argv,
        task_via=task_via,
        task_env=task_env,
        task_arg=task_arg,
        env=_expand_env(env or {}),
        working_dir=working_dir,
        stdout_format=stdout_format,
        name=name,
    )


def parse_manifest(data: dict[str, Any], *, source_path: str | None = None) -> HarnessSpec:
    """Parse a harness.json dict into a normalized spec.

    Accepts both v1 ({"path": "..."}) and v2 ({"command": "..." | "argv": [...]})
    shapes. Unknown keys are ignored (forward-compat).
    """
    # v2: explicit argv array.
    if isinstance(data.get("argv"), list) and data["argv"]:
        argv = [str(x) for x in data["argv"]]
    # v2: shell-style command string.
    elif isinstance(data.get("command"), str) and data["command"].strip():
        argv = shlex.split(data["command"], posix=sys.platform != "win32")
    # v1 legacy: {"path": "harness.py"} → python harness.py
    elif isinstance(data.get("path"), str):
        argv = [sys.executable, data["path"]]
    else:
        raise ValueError(
            "harness manifest must specify one of: command, argv, path"
        )
    task_via: TaskVia = data.get("task_via", "env")
    if task_via not in ("env", "arg", "stdin", "none"):
        raise ValueError(f"task_via must be one of env/arg/stdin/none, got {task_via!r}")

    docker_cfg = data.get("docker") or {}
    return HarnessSpec(
        argv=argv,
        task_via=task_via,
        task_env=data.get("task_env", DEFAULT_TASK_ENV),
        task_arg=data.get("task_arg"),
        env=_expand_env(data.get("env") or {}),
        working_dir=data.get("working_dir"),
        stdout_format=data.get("stdout_format", "json"),
        name=data.get("name"),
        dockerfile=docker_cfg.get("dockerfile") or data.get("dockerfile"),
        image=docker_cfg.get("image"),
        harness_dir=docker_cfg.get("dir") or data.get("dir"),
        source_path=source_path,
    )


def load_manifest(path: Path | str) -> HarnessSpec:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    return parse_manifest(raw, source_path=str(p.resolve()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expand_env(env: dict[str, Any]) -> dict[str, str]:
    """Expand ``$VAR`` and ``${VAR}`` references against the process env.

    Anything that doesn't expand cleanly is dropped to ``""`` rather than
    leaving a literal ``$VAR`` in the child env, which would silently break.
    """
    out: dict[str, str] = {}
    for k, v in env.items():
        if not isinstance(v, str):
            out[str(k)] = "" if v is None else str(v)
            continue
        out[str(k)] = os.path.expandvars(v)
    return out


def template_manifest(
    *,
    command: str,
    task_via: TaskVia = "env",
    task_env: str = DEFAULT_TASK_ENV,
    task_arg: str | None = None,
    env: dict[str, str] | None = None,
    name: str | None = None,
    dockerfile: str | None = None,
) -> dict[str, Any]:
    """Build a *new* harness.json dict from the user's high-level inputs.

    Returns a dict ready for json.dumps. Includes inline comments via a
    ``$schema`` hint so users opening the file see what's available.
    """
    out: dict[str, Any] = {
        "$schema": "https://docs.checkpoint.dev/harness-schema-v2.json",
        "version": 2,
    }
    if name:
        out["name"] = name
    out["command"] = command
    if task_via != "env":
        out["task_via"] = task_via
    if task_env != DEFAULT_TASK_ENV:
        out["task_env"] = task_env
    if task_arg:
        out["task_arg"] = task_arg
    if env:
        out["env"] = env
    if dockerfile:
        out["docker"] = {"dockerfile": dockerfile}
    return out
