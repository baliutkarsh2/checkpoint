"""The sandbox: the simulated world an agent runs in.

A sandbox is a set of twins served from one local process, plus — when
interception is on — a TLS proxy that routes the agent's calls to production
hostnames (``https://api.github.com``) into those twins and applies an egress
policy to everything else. Build it once and reuse it across runs: ``prepare()``
resets every twin, so each run starts from exactly the scenario's seed.

A sandbox may also hold a **workspace**: a temporary file tree the agent edits,
for agents whose work is a diff rather than a sequence of API calls (see
:mod:`checkpoint.workspace`). The two are orthogonal — a run may have twins, a
workspace, or both — and the workspace shares the twins' lifecycle exactly: made
on ``start()``, refilled from its seed on ``prepare()``, read back by ``views()``
and ``state()`` under the key ``workspace``, removed on ``stop()``.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from checkpoint.twins import registry
from checkpoint.workspace import NAMESPACE as WORKSPACE
from checkpoint.workspace import Workspace, WorkspaceError

from .agent import kill_tree, own_process_group

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
    workspace: bool = False
    """Give this sandbox a file tree for the agent to edit. Declared here rather
    than passed to ``prepare()`` because a reused sandbox has to be built for it,
    the same way it is built for a set of twins; the *seed* comes per run."""

    _host: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _ports: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _proxy: Any = field(default=None, init=False, repr=False)
    _workdir: Path | None = field(default=None, init=False, repr=False)
    _stderr_log: Path | None = field(default=None, init=False, repr=False)
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _workspace: Workspace | None = field(default=None, init=False, repr=False)

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
            if self.workspace:
                self._workspace = Workspace()
                self._workspace.start()
            self._start_twins()
            if self.intercept:
                self._start_proxy()
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        if self._workspace is not None:
            try:
                self._workspace.stop()
            finally:
                self._workspace = None
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
        if not self.twins:
            self._ports = {}
            return
        # Port 0 for every twin: the host binds, then tells us what it got.
        # Choosing here instead meant binding a socket, reading its number and
        # closing it before the host bound the same one -- a window two sandboxes
        # starting together land in, which `-j 4` made the common case:
        # [Errno 98], four dead runs, and an INCONCLUSIVE gate.
        requested = dict.fromkeys(self.twins, 0)
        assert self._workdir is not None
        self._stderr_log = self._workdir / "twins.log"
        log = self._stderr_log.open("w", encoding="utf-8")
        try:
            self._host = subprocess.Popen(
                [sys.executable, "-m", "checkpoint.twins.host",
                 *(f"{name}={port}" for name, port in requested.items())],
                stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                **own_process_group(),
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
        self._ports = _ready_ports(ready, self.twins)

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
        from checkpoint.proxy import proxy_routes

        upstreams = {
            domain: self.twin_url(name)
            for name in self.twins
            for domain in registry.get(name).domains
        }
        return proxy_routes(upstreams)

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
        if self._workspace is not None:
            env.update(self._workspace.agent_env())
        if intercepting:
            env.update(self._proxy.client_env())
        return env

    @property
    def workspace_root(self) -> Path | None:
        """Where the agent's file tree lives, or None if this sandbox has no workspace."""
        return self._workspace.root if self._workspace is not None else None

    # -- per-run preparation and inspection -----------------------------------

    def reset(self) -> None:
        for name in self.twins:
            self._post(name, "/_reset")
        if self._proxy is not None:
            self._proxy.clear_events()

    def prepare(self, setups: Mapping[str, TwinSetup] | None = None,
                *, workspace_seed: str | Path | None = None) -> None:
        """Reset every twin, then apply each twin's seed and config.

        ``workspace_seed`` refills the file tree from that directory. It is
        wiped first, so a reused sandbox never shows one run the previous run's
        edits — the same guarantee ``reset()`` gives the twins.
        """
        self.reset()
        if self._workspace is not None:
            try:
                self._workspace.prepare(workspace_seed)
            except WorkspaceError as e:
                raise SandboxError(str(e)) from e
        elif workspace_seed is not None:
            raise SandboxError(
                "a workspace seed was given, but this sandbox was built without a "
                "workspace (Sandbox(..., workspace=True))"
            )
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
        """Each namespace's normalized collections.

        ``{twin: {collection: {key, tombstone, nouns, items}}}``, plus
        ``workspace: {"files": ...}`` when this sandbox has a workspace — which
        is the whole trick: the assertion language resolves ``workspace.files``
        by exactly the lookup it uses for ``github.issues``.
        """
        views = {name: self._get(name, "/_views")["collections"] for name in self.twins}
        if self._workspace is not None:
            # A tree too big to hold is a sandbox failure, not a score of zero:
            # an agent that left 100,000 files behind cannot be scored at all, and
            # this must be reported on the run rather than raised past everyone.
            try:
                files = self._workspace.views()
            except WorkspaceError as e:
                raise SandboxError(str(e)) from e
            views[WORKSPACE] = {name: view.to_json() for name, view in files.items()}
        return views

    def state(self) -> dict[str, dict]:
        state = {name: self._get(name, "/_state") for name in self.twins}
        if self._workspace is not None:
            try:
                state[WORKSPACE] = self._workspace.state()
            except WorkspaceError as e:
                raise SandboxError(str(e)) from e
        return state

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


def _ready_ports(line: str, twins: Sequence[str]) -> dict[str, int]:
    """The ports the twin host reported, validated rather than assumed.

    A port the host never confirmed would be routed to anyway, and every call
    to that twin would then fail with a connection error that reads like a
    broken agent rather than a broken sandbox.
    """
    try:
        ready = json.loads(line).get("ready")
    except (TypeError, ValueError) as e:
        raise SandboxError(f"twins reported an unreadable ready line: {line!r}") from e
    if not isinstance(ready, dict):
        raise SandboxError(f"twins reported no port map: {line!r}")
    ports: dict[str, int] = {}
    for name in twins:
        port = ready.get(name)
        if not isinstance(port, int) or port <= 0:
            raise SandboxError(f"the {name} twin reported no usable port ({port!r})")
        ports[name] = port
    return ports

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
