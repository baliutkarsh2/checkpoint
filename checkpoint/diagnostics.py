"""What ``checkpoint doctor`` checks, and why each check earns its place.

A check either blocks real work or it is advisory, and the two are never
confused: a red row means something you genuinely cannot do yet, and a judge
model with no key is not that, because scenarios whose criteria are all
assertions never call one.

The expensive checks are the honest ones: the intercept proxy is self-tested by
minting a CA and binding a listener, and the twins are checked by starting one
and reading its state back. Both are what a run does, so both fail here for the
same reasons a run would.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None
    required: bool = True
    """A failed advisory check is reported but never fails ``doctor``."""


def _python_version() -> Check:
    ok = sys.version_info >= (3, 11)
    return Check(
        name="Python 3.11 or newer",
        ok=ok,
        detail=sys.version.split()[0],
        fix=None if ok else "Install Python 3.11+ and reinstall Checkpoint into it.",
    )


def _intercept_proxy() -> Check:
    """Mint a CA and bind a loopback listener — the two things that break.

    This is what routes a real SDK's traffic into the twins. It fails on a
    broken cryptography/OpenSSL install and in sandboxes that forbid binding
    loopback ports, and in both cases every intercepted run would fail too.
    """
    name = "TLS interception"
    try:
        from .proxy.ca import CertificateAuthority
        from .proxy.server import EgressPolicy, InterceptProxy

        with tempfile.TemporaryDirectory(prefix="checkpoint-doctor-") as tmp:
            ca = CertificateAuthority.create(tmp)
            ca.server_context("api.github.com")
            with InterceptProxy([], EgressPolicy.open(), ca) as proxy:
                port = proxy.port
    except Exception as e:  # noqa: BLE001 — every failure mode is a user-facing row
        return Check(
            name=name, ok=False, detail=f"{type(e).__name__}: {e}"[:160],
            fix='pip install --force-reinstall "cryptography>=44.0" "h11>=0.16"',
        )
    return Check(name=name, ok=True,
                 detail=f"certificate authority and listener working (port {port})")


def _twins() -> Check:
    """Start a twin and read its state back, exactly as a run does."""
    name = "Twins"
    try:
        from .engine import Sandbox
        from .twins import registry

        with Sandbox(["github"], intercept=False, egress="none") as sandbox:
            sandbox.views()
        count = len(registry.names())
    except Exception as e:  # noqa: BLE001
        return Check(
            name=name, ok=False, detail=f"{type(e).__name__}: {e}"[:160],
            fix="Reinstall Checkpoint: pip install --force-reinstall checkpoint-agents",
        )
    return Check(name=name, ok=True, detail=f"{count} available, github starts and responds")


def _project(cwd: Path | None = None) -> list[Check]:
    """Whether this directory is set up, and whether the agent can be started."""
    from .project import CONFIG_NAME, ConfigError, Project

    cwd = cwd or Path.cwd()
    try:
        proj = Project.load(cwd)
    except ConfigError as e:
        return [Check(name="checkpoint.toml", ok=False, detail=str(e)[:200],
                      fix="Fix the file, or delete it and run `checkpoint init`.")]
    if proj.path is None:
        return [Check(
            name="checkpoint.toml", ok=True, required=False,
            detail=f"none in {cwd} — every command needs --command until there is one",
            fix='checkpoint init --command "python my_agent.py"',
        )]

    checks = [Check(name=CONFIG_NAME, ok=True, detail=_short(proj.path, cwd))]
    command = proj.agent.get("command")
    if isinstance(command, str) and command.strip():
        program = command.split()[0]
        found = shutil.which(program)
        checks.append(Check(
            name="Agent command",
            ok=found is not None,
            detail=command if found else f"{program!r} is not on PATH",
            fix=None if found else f"Install {program!r}, or fix [agent] command in {CONFIG_NAME}.",
        ))
    scenarios = [p for p in proj.scenario_paths() if p.exists()]
    checks.append(Check(
        name="Scenarios", ok=bool(scenarios), required=False,
        detail=", ".join(_short(p, cwd) for p in scenarios) if scenarios
        else "none yet — nothing to run",
        fix=None if scenarios else 'checkpoint new "<what the agent should do>"',
    ))
    return checks


def _short(path: Path, cwd: Path) -> str:
    """A path the reader can place at a glance, absolute only when it must be."""
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


#: Where each provider expects its key. A judge is needed only for `[P]`
#: criteria, so a missing key is advisory: assertion-only scenarios run without
#: one, and the gate refuses up front rather than scoring a judged criterion as
#: a failure.
_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _judge(cwd: Path | None = None) -> Check:
    from .llm import provider_for
    from .project import ConfigError, Project

    try:
        model = Project.load(cwd or Path.cwd()).judge_model()
    except ConfigError:
        from .llm import DEFAULT_MODEL
        model = DEFAULT_MODEL
    if os.environ.get("CHECKPOINT_LLM_BASE_URL"):
        return Check(name="Judge model", ok=True, required=False,
                     detail=f"{model} via CHECKPOINT_LLM_BASE_URL")
    keys = _PROVIDER_KEYS.get(provider_for(model), ())
    present = [k for k in keys if os.environ.get(k, "").strip()]
    if not keys:
        return Check(name="Judge model", ok=True, required=False, detail=model)
    return Check(
        name="Judge model", ok=bool(present), required=False,
        detail=f"{model}, {present[0]} set" if present
        else f"{model} needs {' or '.join(keys)}",
        fix=None if present else f"export {keys[0]}=...  (only needed for [P] criteria)",
    )


def run_checks(*, cwd: Path | None = None, include_twins: bool = True) -> list[Check]:
    """Every check, in a stable order so the output is comparable between runs."""
    checks = [_python_version(), _intercept_proxy()]
    if include_twins:
        checks.append(_twins())
    checks.extend(_project(cwd))
    checks.append(_judge(cwd))
    return checks


def all_passed(checks: list[Check]) -> bool:
    """Whether anything required failed. Advisory rows never decide this."""
    return all(c.ok for c in checks if c.required)
