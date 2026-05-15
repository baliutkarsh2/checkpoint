"""User-level config at ``~/.checkpoint/config.json``.

This is *separate* from the project-level ``.checkpoint.json`` (handled by
:mod:`checkpoint.config`).  Project config is per-repo, lives in source
control, and is what your team agrees on.  *User* config is per-machine,
lives in your home directory, and holds personal defaults — your preferred
judge model, your preferred scenarios directory, machine-local overrides.

Lookup precedence everywhere in the CLI is:

    explicit flag  >  project ``.checkpoint.json``  >  user config  >  env  >  built-in default

The ``env:NAME`` indirection syntax lets you keep secrets out of the file:

    {"engine": {"openai_api_key": "env:OPENAI_API_KEY"}}

Any string starting with ``env:`` is resolved at read-time against the
process environment.  Useful for CI where the key lands as a secret env var.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def home_dir() -> Path:
    """Resolve the checkpoint home dir.

    Honors the ``CHECKPOINT_HOME`` env var (so CI / containers can override),
    falls back to ``~/.checkpoint`` on every platform.
    """
    override = os.environ.get("CHECKPOINT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".checkpoint"


def config_path() -> Path:
    return home_dir() / "config.json"


# A whitelist of keys we recognize. Unknown keys are accepted (forward-compat)
# but warned about in `config show --strict` / `doctor`.
KNOWN_KEYS: dict[str, str] = {
    "defaults.judge_model": "Default model used for [P] LLM-judged criteria.",
    "defaults.agent_model": "Default model used by harnesses that read CHECKPOINT_AGENT_MODEL.",
    "defaults.scenarios_dir": "Where the dashboard / `scenario list` looks for .md scenarios.",
    "defaults.pass_threshold": "Minimum satisfaction score (0-100) required to exit 0 on `run --pass-threshold`.",
    "defaults.runs": "Default number of runs per scenario when neither scenario nor --runs sets it.",
    "engine.openai_api_key": "OpenAI key for the judge. Use `env:OPENAI_API_KEY` to indirect.",
    "engine.anthropic_api_key": "Anthropic key (used by harnesses that read CHECKPOINT_ENGINE_API_KEY).",
    "engine.gemini_api_key": "Gemini key (used by harnesses that read CHECKPOINT_ENGINE_API_KEY).",
    "telemetry.enabled": "Reserved. Always false today — Checkpoint never phones home.",
    "dashboard.port": "Default port for `checkpoint serve`.",
    "dashboard.host": "Default host for `checkpoint serve`.",
}


@dataclass
class UserConfig:
    """A flat key/value store backed by the JSON file.

    Internally we keep the data nested (so it round-trips through JSON
    naturally), but the public API is ``get/set/unset`` over dotted paths
    like ``"defaults.judge_model"``.
    """

    data: dict[str, Any]
    path: Path

    # ---- file IO ---------------------------------------------------------

    @classmethod
    def load(cls) -> "UserConfig":
        p = config_path()
        if not p.exists():
            return cls(data={}, path=p)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(data={}, path=p)
        if not isinstance(raw, dict):
            return cls(data={}, path=p)
        return cls(data=raw, path=p)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # ---- dotted-path access ---------------------------------------------

    def get(self, key: str, *, resolve_env: bool = True) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        if resolve_env and isinstance(node, str) and node.startswith("env:"):
            return os.environ.get(node[4:])
        return node

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = _coerce_value(value)

    def unset(self, key: str) -> bool:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        if isinstance(node, dict) and parts[-1] in node:
            del node[parts[-1]]
            self._prune()
            return True
        return False

    def flatten(self) -> dict[str, Any]:
        """Yield ``{"defaults.judge_model": ..., ...}``.  Stable order."""
        out: dict[str, Any] = {}
        _flatten("", self.data, out)
        return dict(sorted(out.items()))

    # ---- helpers ---------------------------------------------------------

    def _prune(self) -> None:
        """Remove now-empty dict branches after an unset."""
        def walk(d: dict) -> bool:
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, dict):
                    if walk(v):
                        del d[k]
            return len(d) == 0
        walk(self.data)


def _flatten(prefix: str, node: Any, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else k
            _flatten(key, v, out)
    else:
        out[prefix] = node


def _coerce_value(value: Any) -> Any:
    """``checkpoint config set`` always passes strings; coerce when sensible."""
    if not isinstance(value, str):
        return value
    if value.startswith("env:"):
        return value
    lo = value.lower()
    if lo in ("true", "false"):
        return lo == "true"
    if lo in ("none", "null"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
