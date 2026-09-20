"""``checkpoint.toml``: one file that says how to run your agent and how to test it.

    [agent]
    command = "python my_agent.py"

    [judge]
    model = "gpt-5.6-luna"

    [sandbox]
    egress = "llm"

    [gate]
    runs = 16

Settings were previously spread over ``.checkpoint.json``, ``harness.json`` and
``~/.checkpoint/config.json``, each read by a different command — several keys
were documented but read by nothing at all. There is one file now, found by
walking up from the working directory, and one precedence rule everywhere:

    command-line flag  >  environment variable  >  checkpoint.toml  >  default

Unknown keys and wrong types are errors, not silence: a setting that looks
applied but is not is worse than no setting.
"""
from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import Agent
from .llm import DEFAULT_MODEL

CONFIG_NAME = "checkpoint.toml"
MAX_PARENTS = 6

_AGENT_KEYS = {"command", "url", "task_via", "task_env", "task_arg", "cwd", "env", "timeout", "name"}
_JUDGE_KEYS = {"model", "samples"}
_SANDBOX_KEYS = {"intercept", "egress", "allow_hosts"}
_GATE_KEYS = {"runs", "pass_threshold", "ship_min", "block_max", "confidence", "strict",
              "allow_conditional", "regression_drop", "concurrency"}
_SCENARIO_KEYS = {"path", "paths"}
_TWIN_KEYS = {"app", "domains", "title", "token", "token_env", "auth_scheme",
              "production_url", "extra_env", "docs"}
_SECTIONS = {"agent": _AGENT_KEYS, "judge": _JUDGE_KEYS, "sandbox": _SANDBOX_KEYS,
             "gate": _GATE_KEYS, "scenarios": _SCENARIO_KEYS}


class ConfigError(ValueError):
    """``checkpoint.toml`` cannot be used as written."""


@dataclass
class Project:
    """What ``checkpoint.toml`` says, plus where it was found."""

    root: Path = field(default_factory=Path.cwd)
    path: Path | None = None
    agent: dict[str, Any] = field(default_factory=dict)
    judge: dict[str, Any] = field(default_factory=dict)
    sandbox: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)
    scenarios: dict[str, Any] = field(default_factory=dict)
    twins: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Services of your own, declared as ``[twins.<name>]``."""

    # -- loading ---------------------------------------------------------------

    @classmethod
    def find(cls, start: str | Path | None = None) -> Path | None:
        """The nearest ``checkpoint.toml`` at or above ``start``."""
        here = Path(start or Path.cwd()).resolve()
        for directory in (here, *here.parents)[: MAX_PARENTS + 1]:
            candidate = directory / CONFIG_NAME
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def load(cls, start: str | Path | None = None) -> Project:
        """Load the project config, or an empty one when there is none."""
        path = cls.find(start)
        if path is None:
            return cls(root=Path(start or Path.cwd()).resolve())
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path} is not valid TOML: {e}") from None
        except OSError as e:
            raise ConfigError(f"{path} cannot be read: {e}") from None
        _check(data, path)
        return cls(
            root=path.parent,
            path=path,
            agent=dict(data.get("agent") or {}),
            judge=dict(data.get("judge") or {}),
            sandbox=dict(data.get("sandbox") or {}),
            gate=dict(data.get("gate") or {}),
            scenarios=dict(data.get("scenarios") or {}),
            twins={name: dict(spec) for name, spec in (data.get("twins") or {}).items()},
        )

    # -- what the commands ask for ----------------------------------------------

    def build_agent(self, command: str | None = None, **overrides: Any) -> Agent | None:
        """The agent to run: ``command`` if given, else what the config describes."""
        spec: dict[str, Any] = {**self.agent, **{k: v for k, v in overrides.items() if v is not None}}
        if command:
            spec["command"] = command
            spec.pop("url", None)
        if not spec.get("command") and not spec.get("url"):
            return None
        cwd = spec.get("cwd")
        env = spec.get("env") or {}
        if not isinstance(env, Mapping):
            raise ConfigError("[agent] env must be a table of name = \"value\" pairs")
        return Agent(
            command=self._resolve_command(spec.get("command") or ()),
            url=spec.get("url"),
            task_via=spec.get("task_via", "env"),
            task_env=spec.get("task_env", "CHECKPOINT_TASK"),
            task_arg=spec.get("task_arg"),
            cwd=str(self.resolve(cwd)) if cwd else None,
            env={str(k): str(v) for k, v in env.items()},
            name=spec.get("name", ""),
        )

    def _resolve_command(self, command: str | Sequence[str]) -> str | list[str]:
        """Make file paths in the command absolute, relative to checkpoint.toml.

        Every other path in this file is relative to the file itself, and the
        command has to be too — because the directory the agent runs *in* is not
        always the directory its code lives in. A scenario with a ``workspace:``
        starts the agent inside a throwaway copy of a fixture tree, and there
        ``python agent.py`` finds no ``agent.py``: the script is back in the
        project. Resolving it here means the same line works either way.

        Only tokens that really name a file are touched, so a flag, a literal
        argument or a program found on PATH is passed through untouched.
        """
        if not command:
            return command if isinstance(command, str) else list(command)
        from .engine import split_command

        argv = split_command(command) if isinstance(command, str) else list(command)
        resolved = [str((self.root / token).resolve()) if (self.root / token).is_file()
                    else token for token in argv]
        return resolved if resolved != argv else command

    def judge_model(self, flag: str | None = None) -> str:
        return (flag or os.environ.get("CHECKPOINT_JUDGE_MODEL")
                or self.judge.get("model") or DEFAULT_MODEL)

    def judge_samples(self, flag: int | None = None) -> int:
        """How many times to ask the judge about each criterion.

        More than one costs more and disagrees less: the samples have to agree
        before a criterion passes, and a judge that flips between them reports
        `unknown` rather than picking a side.
        """
        if flag is not None:
            return flag
        value = self.judge.get("samples")
        return int(value) if value is not None else 1

    def agent_timeout(self, flag: float | None = None) -> float | None:
        return flag if flag is not None else self.agent.get("timeout")

    def sandbox_setting(self, name: str, flag: Any = None, default: Any = None) -> Any:
        if flag is not None:
            return flag
        return self.sandbox.get(name, default)

    def gate_setting(self, name: str, flag: Any = None, default: Any = None) -> Any:
        if flag is not None:
            return flag
        return self.gate.get(name, default)

    def scenario_paths(self) -> list[Path]:
        """Where this project keeps its scenarios."""
        raw = self.scenarios.get("paths") or self.scenarios.get("path") or "scenarios"
        items = [raw] if isinstance(raw, str) else list(raw)
        return [self.resolve(item) for item in items]

    def resolve(self, value: str | Path) -> Path:
        """A path from the config, relative to the file that declared it."""
        path = Path(value)
        return path if path.is_absolute() else (self.root / path)

    # -- twins of your own -------------------------------------------------------

    def register_twins(self) -> list[str]:
        """Add this project's ``[twins.<name>]`` services to the registry.

        The seven bundled twins cover the services most agents touch, but not
        yours. Point Checkpoint at an ASGI app in your own repository and it
        becomes a twin like any other: scenarios name it, the sandbox starts it,
        the proxy routes its production hostnames into it.

            [twins.billing]
            app = "mycompany.testing.billing_twin:app"
            domains = ["api.billing.internal"]
            token_env = ["BILLING_API_KEY"]

        Returns the names registered. Called once per command, before anything
        resolves a twin name.
        """
        if not self.twins:
            return []
        from checkpoint.twins.registry import TwinSpec, register

        # The app lives in the project, not in Checkpoint's own package, and the
        # console script's sys.path does not include the working directory.
        root = str(self.root)
        if root not in sys.path:
            sys.path.insert(0, root)

        registered = []
        for name, spec in self.twins.items():
            app = spec.get("app")
            if not isinstance(app, str) or ":" not in app:
                raise ConfigError(
                    f'[twins.{name}] needs app = "package.module:attribute" naming '
                    "the ASGI app that serves this service")
            register(TwinSpec(
                name=name,
                title=str(spec.get("title") or name.replace("-", " ").title()),
                app=app,
                domains=tuple(spec.get("domains") or ()),
                token=str(spec.get("token") or f"cptk_CHECKPOINTFAKE_{name.upper()}"),
                token_env=tuple(spec.get("token_env") or ()),
                auth_scheme=str(spec.get("auth_scheme", "Bearer")),
                production_url=str(spec.get("production_url") or ""),
                extra_env=dict(spec.get("extra_env") or {}),
                docs=str(spec.get("docs") or ""),
                builtin=False,
            ))
            registered.append(name)
        return registered


def _check(data: Mapping[str, Any], path: Path) -> None:
    """Reject unknown sections and keys, naming the nearest real one."""
    unknown_sections = [k for k in data if k not in _SECTIONS and k != "twins"]
    if unknown_sections:
        raise ConfigError(
            f"{path}: unknown section(s) {_quoted(unknown_sections)}; "
            f"supported: {_quoted(sorted(_SECTIONS))}"
        )
    for section, keys in _SECTIONS.items():
        table = data.get(section)
        if table is None:
            continue
        if not isinstance(table, Mapping):
            raise ConfigError(f"{path}: [{section}] must be a table")
        unknown = [k for k in table if k not in keys]
        if unknown:
            raise ConfigError(
                f"{path}: [{section}] has unknown key(s) {_quoted(unknown)}; "
                f"supported: {_quoted(sorted(keys))}"
            )
    agent = data.get("agent") or {}
    via = agent.get("task_via")
    if via is not None and via not in ("env", "arg", "stdin"):
        raise ConfigError(f"{path}: [agent] task_via must be env, arg or stdin, not {via!r}")
    twins = data.get("twins") or {}
    if not isinstance(twins, Mapping):
        raise ConfigError(f"{path}: declare a twin as [twins.<name>], one table per service")
    for name, spec in twins.items():
        if not isinstance(spec, Mapping):
            raise ConfigError(f"{path}: [twins.{name}] must be a table")
        unknown = [k for k in spec if k not in _TWIN_KEYS]
        if unknown:
            raise ConfigError(
                f"{path}: [twins.{name}] has unknown key(s) {_quoted(unknown)}; "
                f"supported: {_quoted(sorted(_TWIN_KEYS))}")
    egress = (data.get("sandbox") or {}).get("egress")
    if egress is not None and egress not in ("open", "llm", "none"):
        raise ConfigError(f"{path}: [sandbox] egress must be open, llm or none, not {egress!r}")


def _quoted(values: list[str]) -> str:
    return ", ".join(repr(v) for v in values)


TEMPLATE = '''\
# How Checkpoint runs and tests this agent. https://github.com/baliutkarsh2/checkpoint

[agent]
# The command that already runs your agent. Checkpoint never edits your code:
# it sets $CHECKPOINT_TASK to the scenario's task and reads the final answer
# from stdout (plain text or JSON), or from $CHECKPOINT_ANSWER_FILE.
command = {command}
{extra}
[judge]
# Used only for [P] criteria, which need judgement rather than a state check.
model = {model}

[sandbox]
# Route the agent's calls to production hostnames into the twins, so the code
# path under test is the one you ship.
intercept = true
# What the agent may reach beyond the twins: "open", "llm", or "none".
egress = "llm"
# allow_hosts = ["api.example.com"]

[gate]
# Runs per scenario. A perfect run of fewer than 16 cannot clear the default
# ship threshold, so the gate reports INCONCLUSIVE rather than a green build.
runs = 16
'''


def render_template(command: str, *, task_via: str = "env", task_arg: str | None = None,
                    model: str = DEFAULT_MODEL) -> str:
    """The starter ``checkpoint.toml`` written by ``checkpoint init``."""
    extra = ""
    if task_via != "env":
        extra += f"task_via = {toml_string(task_via)}\n"
    if task_arg:
        extra += f"task_arg = {toml_string(task_arg)}\n"
    return TEMPLATE.format(command=toml_string(command), extra=extra,
                           model=toml_string(model))


def toml_string(value: str) -> str:
    """``value`` as a TOML basic string.

    Not ``repr``: Python's escaping is not TOML's, and the two disagree exactly
    where it hurts. ``repr`` of a Windows command produces a single-quoted
    literal whose doubled backslashes TOML then keeps as doubled backslashes,
    and a command containing an apostrophe ends the literal early.
    """
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))
    return f'"{escaped}"'
