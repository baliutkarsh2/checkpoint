"""The sandbox: the simulated world an agent runs in.

A sandbox is a set of twins served from one local process, plus — when
interception is on — a TLS proxy that routes the agent's calls to production
hostnames (``https://api.github.com``) into those twins and applies an egress
policy to everything else. Build it once and reuse it across runs: ``prepare()``
resets every twin, so each run starts from exactly the scenario's seed.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from checkpoint.twins import registry

from .agent import kill_tree

Egress = Literal["open", "llm", "none"]
"""What the agent may reach outside the sandbox: anything, only LLM providers
(plus ``allow_hosts``), or nothing but ``allow_hosts``. Twins are always reachable."""

_STARTUP_TIMEOUT = 60.0


class SandboxError(RuntimeError):
    """The sandbox could not be built or prepared (never an agent failure)."""


@dataclass
class TwinSetup:
    """How to prepare one twin before a run."""

    seed: str | None = None
    """Name of a bundled seed (``small-project``)."""
    seed_data: dict | None = None
    """An inline seed ``{"state": {...}, "config": {...}}`` (e.g. from a seed file)."""
    config: dict = field(default_factory=dict)
    """Knobs and faults for ``/_config`` (``{"rate_limit": 5}``)."""


@dataclass
class Sandbox:
    """Twins + interception for a set of services. Use as a context manager."""

    twins: Sequence[str]
    intercept: bool = True
    egress: Egress = "llm"
    allow_hosts: Sequence[str] = ()

    _host: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _ports: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _proxy: Any = field(default=None, init=False, repr=False)
    _workdir: Path | None = field(default=None, init=False, repr=False)
    _stderr_log: Path | None = field(default=None, init=False, repr=False)
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        names: list[str] = []
        for name in self.twins:
            spec = registry.get(name)  # raises UnknownTwinError with the available list
            if spec.name not in names:
                names.append(spec.name)
        # A sandbox with no twins is valid: agents that only talk to an LLM still
        # get egress control and a record of every external host they reached.
        self.twins = tuple(names)
        if self.egress not in ("open", "llm", "none"):
            raise SandboxError(f"egress must be open, llm or none, not {self.egress!r}")

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> Sandbox:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def started(self) -> bool:
        return self._client is not None

    def start(self) -> None:
        if self.started:
            return
        self._workdir = Path(tempfile.mkdtemp(prefix="checkpoint-sandbox-"))
        self._client = httpx.Client(timeout=15.0, trust_env=False)
        try:
            self._start_twins()
            if self.intercept:
                self._start_proxy()
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        if self._proxy is not None:
            try:
                self._proxy.stop()
            finally:
                self._proxy = None
        if self._host is not None:
            kill_tree(self._host)
            self._host = None
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    def _start_twins(self) -> None:
        self._ports = {name: _free_port() for name in self.twins}
        if not self._ports:
            return
        assert self._workdir is not None
        self._stderr_log = self._workdir / "twins.log"
        log = self._stderr_log.open("w", encoding="utf-8")
        try:
            self._host = subprocess.Popen(
                [sys.executable, "-m", "checkpoint.twins.host",
                 *(f"{name}={port}" for name, port in self._ports.items())],
                stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        finally:
            log.close()
        ready = _read_line(self._host, _STARTUP_TIMEOUT)
        if ready is None or '"ready"' not in ready:
            detail = self._stderr_log.read_text(encoding="utf-8", errors="replace")[-3000:]
            raise SandboxError(
                f"twins ({', '.join(self.twins)}) failed to start"
                + (f":\n{detail.strip()}" if detail.strip() else " (no output)")
            )

    def _start_proxy(self) -> None:
        try:
            from checkpoint.proxy import CertificateAuthority, EgressPolicy, InterceptProxy
        except ImportError as e:
            raise SandboxError(f"TLS interception is unavailable in this install: {e}") from e

        assert self._workdir is not None
        ca = CertificateAuthority.create(self._workdir / "ca")
        self._proxy = InterceptProxy(routes=self._routes(), policy=self._policy(EgressPolicy), ca=ca)
        self._proxy.start()

    def _routes(self) -> list:
        from checkpoint.proxy import Route

        routes = []
        for name in self.twins:
            for domain in registry.get(name).domains:
                routes.append(Route(domain, self.twin_url(name)))
        return routes

    def _policy(self, policy_cls: Any) -> Any:
        from checkpoint.proxy import LLM_PROVIDER_HOSTS

        if self.egress == "open":
            return policy_cls.open()
        allowed = list(self.allow_hosts)
        if self.egress == "llm":
            allowed = [*LLM_PROVIDER_HOSTS, *allowed]
        return policy_cls.allowlist(allowed)

    # -- addressing ------------------------------------------------------------

    def twin_url(self, name: str) -> str:
        spec = registry.get(name)
        if spec.name not in self._ports:
            raise SandboxError(f"twin {spec.name!r} is not part of this sandbox ({', '.join(self.twins)})")
        return f"http://127.0.0.1:{self._ports[spec.name]}"

    @property
    def urls(self) -> dict[str, str]:
        return {name: self.twin_url(name) for name in self.twins}

    def agent_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment an agent process needs to live inside this sandbox.

        Starts from ``base`` (normally the caller's environment, so the agent
        keeps its PATH, virtualenv and LLM keys), then overrides every credential
        for a sandboxed service with its fake — a real production token in the
        caller's shell can never reach a real API through the agent.
        """
        env = dict(os.environ if base is None else base)
        intercepting = self._proxy is not None
        for name in self.twins:
            env.update(registry.get(name).agent_env(self.twin_url(name), intercepted=intercepting))
        env["CHECKPOINT_TWINS"] = ",".join(self.twins)
        env["CHECKPOINT_SANDBOX"] = "1"
        if intercepting:
            env.update(self._proxy.client_env())
        return env

    # -- per-run preparation and inspection -----------------------------------

    def reset(self) -> None:
        for name in self.twins:
            self._post(name, "/_reset")
        if self._proxy is not None:
            self._proxy.clear_events()

    def prepare(self, setups: Mapping[str, TwinSetup] | None = None) -> None:
        """Reset every twin, then apply each twin's seed and config."""
        self.reset()
        for name, setup in (setups or {}).items():
            twin = registry.get(name).name
            if twin not in self._ports:
                raise SandboxError(f"setup given for {twin!r}, which is not in this sandbox")
            if setup.seed:
                self._post(twin, f"/_seed/{setup.seed}", what=f"seed {setup.seed!r}")
            if setup.seed_data is not None:
                self._post(twin, "/_seed-file", json=setup.seed_data, what="seed file")
            if setup.config:
                self._post(twin, "/_config", json=setup.config, what="config")

    def views(self) -> dict[str, dict[str, dict]]:
        """Each twin's normalized collections: ``{twin: {collection: {key, tombstone, nouns, items}}}``."""
        return {name: self._get(name, "/_views")["collections"] for name in self.twins}

    def state(self) -> dict[str, dict]:
        return {name: self._get(name, "/_state") for name in self.twins}

    def trace(self) -> list[dict]:
        """Every API call the agent made, across twins, in order."""
        calls: list[dict] = []
        for name in self.twins:
            for entry in self._get(name, "/_trace"):
                calls.append({"twin": name, **entry})
        calls.sort(key=lambda e: e.get("ts", ""))
        return calls

    def egress_events(self) -> list[dict]:
        """Connections the agent made to hosts outside the twins."""
        if self._proxy is None:
            return []
        return [_event_dict(e) for e in self._proxy.events() if not getattr(e, "routed", False)]

    # -- http helpers ----------------------------------------------------------

    def _post(self, twin: str, path: str, *, json: Any = None, what: str = "") -> None:
        assert self._client is not None, "sandbox is not started"
        try:
            r = self._client.post(self.twin_url(twin) + path, json=json)
        except httpx.HTTPError as e:
            raise SandboxError(f"{twin} twin did not respond to {path}: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json()
            except ValueError:
                detail = {"error": r.text[:300]}
            message = detail.get("error") or detail
            hint = ""
            if "available" in detail:
                hint = f" (available: {', '.join(detail['available']) or 'none'})"
            raise SandboxError(f"{twin}: could not apply {what or path}: {message}{hint}")

    def _get(self, twin: str, path: str) -> Any:
        assert self._client is not None, "sandbox is not started"
        try:
            r = self._client.get(self.twin_url(twin) + path)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SandboxError(f"{twin} twin did not answer {path}: {e}") from e


def load_seed_file(path: str | Path) -> dict:
    """Read a seed file: ``{"state": ..., "config": ...}`` or a bare state object."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SandboxError(f"seed file not found: {p}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise SandboxError(f"seed file {p} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SandboxError(f"seed file {p} must contain a JSON object")
    return data if "state" in data or "config" in data else {"state": data}


def _event_dict(event: Any) -> dict:
    if isinstance(event, dict):
        return event
    fields = ("ts", "host", "port", "method", "path", "status", "routed", "allowed")
    return {f: getattr(event, f, None) for f in fields}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_line(proc: subprocess.Popen, timeout: float) -> str | None:
    """Read one stdout line from ``proc`` within ``timeout`` seconds (None on timeout/exit)."""
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        lines.put(proc.stdout.readline() or None)

    threading.Thread(target=pump, daemon=True).start()
    try:
        return lines.get(timeout=timeout)
    except queue.Empty:
        return None


def twins_for(names: Iterable[str]) -> list[str]:
    """Normalize user-supplied twin names (aliases, case) and drop duplicates."""
    seen: list[str] = []
    for name in names:
        spec = registry.get(name)
        if spec.name not in seen:
            seen.append(spec.name)
    return seen
