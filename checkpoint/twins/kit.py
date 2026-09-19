"""Shared runtime for twins: one control plane and one fault model for every service.

A twin is a FastAPI app that keeps a service's state in memory. The engine drives
every twin through the same control plane, so built-in and user-written twins are
interchangeable:

    GET  /_health        liveness probe
    GET  /_state         current state (public keys plus ``_config``)
    GET  /_trace         every API call the agent made, in order
    POST /_reset         fresh state, empty trace, default config
    GET  /_config        current knobs and faults
    POST /_config        update knobs and faults (unknown keys are rejected)
    GET  /_seeds         bundled seed names
    GET  /_views         state as normalized collections, for assertions
    POST /_seed/{name}   reset, then load a bundled seed
    POST /_seed-file     reset, then load an inline seed ``{"state": {...}, "config": {...}}``

Faults are uniform across twins so a scenario can inject the same failure into any
service. Each twin shapes the resulting error the way the real API does, because
SDKs parse those shapes into typed exceptions and retry decisions:

    rate_limit          requests allowed before every call returns 429
    read_only           writes are refused
    permissions_denied  writes are refused as a permissions error
    latency_ms          delay added to every call
    error_rate          fraction of calls that fail with 500 (seeded, reproducible)
    fail                targeted rules: [{"method": "POST", "path": "/issues", "status": 503, "times": 1}]
    fault_seed          seed for ``error_rate``
    strict_auth         accept only the twin's own fake credential (default: any non-empty one)

Every trace entry is classified as a ``create``/``read``/``update``/``delete`` of a
named resource, so trajectory checks can say "never deleted a message" even for
APIs that delete with a POST. Calls arriving through a twin's MCP server are
recorded with ``"via": "mcp"`` so a trajectory shows which surface was used.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

CONTROL_PREFIX = "/_"
MCP_PREFIX = "/mcp"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Header the MCP shim stamps on the REST calls it makes, so the trace can tell
# MCP tool calls apart from direct REST calls.
VIA_HEADER = "x-checkpoint-via"

FaultKind = Literal[
    "unauthorized", "forbidden", "read_only", "rate_limited", "server_error", "injected",
]

DEFAULT_FAULTS: dict[str, Any] = {
    "rate_limit": None,
    "read_only": False,
    "permissions_denied": False,
    "latency_ms": 0,
    "error_rate": 0.0,
    "fail": [],
    "fault_seed": 0,
    # Accept only the twin's own fake credential (by default any non-empty
    # credential works, so an agent's usual token handling runs unchanged).
    "strict_auth": False,
}

Op = Literal["create", "read", "update", "delete", "other"]

ErrorFactory = Callable[[FaultKind, int, str], Response]
Authenticator = Callable[[Request], Response | None]
ResponseHook = Callable[[Request, Response], None]
SeedHook = Callable[[dict], None]
Classifier = Callable[[str, str, Any], "tuple[Op, str] | None"]
FailureCheck = Callable[[int, Any], bool]


@dataclass
class View:
    """A normalized collection of records that assertions can query.

    ``key`` identifies a record across the seed and final snapshots, which is how
    created/deleted/changed are computed. ``tombstone`` names the field a
    soft-delete sets (a record whose tombstone becomes truthy counts as deleted).
    """

    items: list[dict]
    key: str = "id"
    tombstone: str | None = None
    nouns: tuple[str, ...] = ()
    """How people refer to a record ("issue", "pull request"), for plain-English criteria."""

    def to_json(self) -> dict:
        return {"key": self.key, "tombstone": self.tombstone, "nouns": list(self.nouns),
                "items": self.items}


ViewBuilder = Callable[[dict], "dict[str, View]"]

_METHOD_OPS: dict[str, Op] = {
    "GET": "read", "HEAD": "read", "OPTIONS": "read",
    "POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete",
}
_ID_SEGMENT = re.compile(r"^(?:\d+|[0-9a-f-]{16,}|[A-Za-z]{1,4}_[A-Za-z0-9]+|@me|[A-Z]+-\d+)$")
_VERSION_SEGMENT = re.compile(r"^(?:v\d+(?:\.\d+)?|api|rest|graphql)$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def default_error(kind: FaultKind, status: int, message: str) -> Response:
    """Plain JSON error for twins without a service-specific error shape."""
    return JSONResponse(status_code=status, content={"error": {"type": kind, "message": message}})


class ConfigError(ValueError):
    """An invalid ``/_config`` or seed ``config`` payload."""


@dataclass
class FailRule:
    method: str
    path: re.Pattern[str]
    status: int
    times: int | None
    message: str

    @classmethod
    def parse(cls, raw: Any) -> FailRule:
        if not isinstance(raw, dict):
            raise ConfigError(f"fail rule must be an object, got {raw!r}")
        unknown = set(raw) - {"method", "path", "status", "times", "message"}
        if unknown:
            raise ConfigError(f"fail rule has unknown keys {sorted(unknown)}")
        try:
            status = int(raw.get("status", 500))
            times = raw.get("times")
            times = None if times is None else int(times)
            pattern = re.compile(str(raw.get("path", ".*")))
        except (TypeError, ValueError, re.error) as e:
            raise ConfigError(f"invalid fail rule {raw!r}: {e}") from e
        if not 400 <= status <= 599:
            raise ConfigError(f"fail rule status must be 4xx/5xx, got {status}")
        return cls(
            method=str(raw.get("method", "*")).upper(),
            path=pattern,
            status=status,
            times=times,
            message=str(raw.get("message") or f"Injected failure ({status})"),
        )

    def matches(self, method: str, path: str) -> bool:
        if self.times is not None and self.times <= 0:
            return False
        return self.method in ("*", method) and self.path.search(path) is not None


@dataclass
class Twin:
    """The shared runtime behind one twin app.

    ``state`` and ``trace`` are the twin module's own ``STATE`` dict and ``TRACE``
    list: the kit mutates them in place, so route handlers keep using their
    module-level names.
    """

    name: str
    state: dict
    trace: list
    fresh_state: Callable[[], dict]
    seeds_dir: Path | None = None
    error: ErrorFactory = default_error
    authenticate: Authenticator | None = None
    on_response: ResponseHook | None = None
    after_seed: SeedHook | None = None
    views: ViewBuilder | None = None
    """Builds the twin's normalized collections; the default derives them from state."""
    classify: Classifier | None = None
    """Maps (method, path, body) to (op, resource); ``None`` falls back to HTTP semantics."""
    failed: FailureCheck | None = None
    """Whether a response is an application failure (default: HTTP status >= 400)."""
    # Twin-specific knobs accepted by /_config beyond the uniform faults.
    knobs: dict[str, Any] = field(default_factory=dict)

    requests: int = field(default=0, init=False)
    _rules: list[FailRule] = field(default_factory=list, init=False)
    _rng: random.Random = field(default_factory=random.Random, init=False)

    # -- lifecycle -----------------------------------------------------------

    def reset(self) -> None:
        self.state.clear()
        self.state.update(self.fresh_state())
        self.trace.clear()
        self.requests = 0
        self._rules = []
        self._rng = random.Random(self.config.get("fault_seed", 0))

    @property
    def config(self) -> dict:
        # Tolerate state replaced wholesale (tests, legacy seeds) without _config.
        config = self.state.setdefault("_config", {})
        for key, value in {**DEFAULT_FAULTS, **self.knobs}.items():
            if key not in config:
                config[key] = _copy(value)
        return config

    def configure(self, updates: dict) -> dict:
        """Validate and apply config updates; raise ConfigError on bad input."""
        if not isinstance(updates, dict):
            raise ConfigError("config must be a JSON object")
        allowed = set(DEFAULT_FAULTS) | set(self.knobs) | set(self.config)
        unknown = sorted(set(updates) - allowed)
        if unknown:
            raise ConfigError(
                f"unknown config key(s) {unknown} for the {self.name} twin; "
                f"supported: {sorted(allowed)}"
            )
        staged = dict(self.config)
        staged.update(updates)
        rules = [FailRule.parse(r) for r in staged.get("fail") or []]
        _validate_faults(staged)
        self.config.clear()
        self.config.update(staged)
        self._rules = rules
        if "fault_seed" in updates:
            self._rng = random.Random(self.config.get("fault_seed", 0))
        return self.config

    def load_seed(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ConfigError("seed must be a JSON object")
        self.reset()
        for key, value in (data.get("state") or {}).items():
            if isinstance(value, dict) and isinstance(self.state.get(key), dict):
                self.state[key].update(value)
            else:
                self.state[key] = value
        if self.after_seed is not None:
            self.after_seed(self.state)
        if data.get("config"):
            self.configure(data["config"])

    def seed_names(self) -> list[str]:
        if self.seeds_dir is None or not self.seeds_dir.is_dir():
            return []
        return sorted(p.stem for p in self.seeds_dir.glob("*.json"))

    def public_state(self) -> dict:
        return {k: v for k, v in self.state.items() if not k.startswith("_") or k == "_config"}

    def collection_views(self) -> dict[str, View]:
        if self.views is not None:
            return self.views(self.state)
        return default_views(self.state)

    def classify_call(self, method: str, path: str, body: Any) -> tuple[Op, str]:
        if self.classify is not None:
            result = self.classify(method, path, body)
            if result is not None:
                return result
        return default_classify(method, path)

    # -- request pipeline ----------------------------------------------------

    async def _fault(self, request: Request) -> Response | None:
        """Return an injected failure for this request, or None to serve it."""
        method, path, cfg = request.method, request.url.path, self.config
        is_write = method in WRITE_METHODS

        if cfg.get("latency_ms"):
            await asyncio.sleep(float(cfg["latency_ms"]) / 1000)
        if is_write and cfg.get("permissions_denied"):
            return self.error("forbidden", 403, "Resource not accessible with these credentials.")
        if is_write and cfg.get("read_only"):
            return self.error("read_only", 403, "Writes are disabled in this sandbox (read_only).")

        self.requests += 1
        limit = cfg.get("rate_limit")
        if limit is not None and self.requests > int(limit):
            return self.error("rate_limited", 429, "API rate limit exceeded.")

        for rule in self._rules:
            if rule.matches(method, path):
                if rule.times is not None:
                    rule.times -= 1
                kind: FaultKind = "rate_limited" if rule.status == 429 else "injected"
                return self.error(kind, rule.status, rule.message)

        rate = float(cfg.get("error_rate") or 0)
        if rate and self._rng.random() < rate:
            return self.error("server_error", 500, "Internal server error (injected).")
        return None

    async def handle(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        started = time.perf_counter()
        body_bytes = await request.body()

        async def receive() -> dict:
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request._receive = receive  # type: ignore[attr-defined]  # replay the consumed body

        response = self.authenticate(request) if self.authenticate else None
        fault: str | None = None
        if response is None:
            response = await self._fault(request)
            if response is not None:
                fault = "injected"
        if response is None:
            response = await call_next(request)
        if self.on_response is not None:
            self.on_response(request, response)

        if hasattr(response, "body_iterator"):  # streamed by the route handler
            resp_bytes = b"".join([chunk async for chunk in response.body_iterator])
        else:  # built here (auth failure or injected fault)
            resp_bytes = bytes(response.body)
        body = _decode(body_bytes)
        resp_body = _decode(resp_bytes)
        op, resource = self.classify_call(request.method, request.url.path, body)
        failed = (self.failed(response.status_code, resp_body) if self.failed
                  else response.status_code >= 400)
        entry: dict[str, Any] = {
            "ts": _now_iso(),
            "twin": self.name,
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "body": body,
            "status": response.status_code,
            "ok": not failed,
            "op": op,
            "resource": resource,
            "response": resp_body,
            "via": "mcp" if request.headers.get(VIA_HEADER) == "mcp" else "rest",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        if fault:
            entry["fault"] = True
        self.trace.append(entry)
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(
            content=resp_bytes,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )


def install(app: FastAPI, twin: Twin) -> Twin:
    """Mount the control plane and the trace/fault/auth pipeline on ``app``."""
    twin.reset()

    @app.middleware("http")
    async def _pipeline(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path.startswith(CONTROL_PREFIX) or path.startswith(MCP_PREFIX):
            return await call_next(request)
        return await twin.handle(request, call_next)

    @app.get("/_health", include_in_schema=False)
    def _health() -> dict:
        return {"ok": True, "twin": twin.name}

    @app.get("/_state", include_in_schema=False)
    def _state() -> dict:
        return twin.public_state()

    @app.get("/_trace", include_in_schema=False)
    def _trace() -> list:
        return twin.trace

    @app.post("/_reset", include_in_schema=False)
    def _reset() -> dict:
        twin.reset()
        return {"ok": True}

    @app.get("/_config", include_in_schema=False)
    def _get_config() -> dict:
        return twin.config

    @app.post("/_config", include_in_schema=False)
    async def _set_config(request: Request) -> Response:
        try:
            config = twin.configure(await _json(request))
        except ConfigError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "config": config})

    @app.get("/_seeds", include_in_schema=False)
    def _seeds() -> dict:
        return {"seeds": twin.seed_names()}

    @app.get("/_views", include_in_schema=False)
    def _views() -> dict:
        return {"collections": {name: view.to_json() for name, view in twin.collection_views().items()}}

    @app.post("/_seed/{name}", include_in_schema=False)
    def _seed(name: str) -> Response:
        path = (twin.seeds_dir / f"{name}.json") if twin.seeds_dir else None
        if path is None or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not path.is_file():
            return JSONResponse(status_code=404, content={
                "ok": False,
                "error": f"seed {name!r} not found for the {twin.name} twin",
                "available": twin.seed_names(),
            })
        try:
            twin.load_seed(json.loads(path.read_text(encoding="utf-8")))
        except ConfigError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "seed": name, "config": twin.config})

    @app.post("/_seed-file", include_in_schema=False)
    async def _seed_file(request: Request) -> Response:
        try:
            twin.load_seed(await _json(request))
        except ConfigError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "config": twin.config})

    return twin


def default_views(state: dict) -> dict[str, View]:
    """Derive collections from state: every public dict-of-records or list-of-records."""
    views: dict[str, View] = {}
    for name, value in state.items():
        if name.startswith("_"):
            continue
        # Empty collections count too: "no messages were created" must be checkable
        # against a twin that started with none.
        if isinstance(value, dict) and all(isinstance(v, dict) for v in value.values()):
            items = [record if "id" in record else {"id": key, **record} for key, record in value.items()]
            views[name] = View(items)
        elif isinstance(value, list) and all(isinstance(v, dict) for v in value):
            views[name] = View(list(value))
    return views


def default_classify(method: str, path: str) -> tuple[Op, str]:
    """HTTP-semantics classification: the verb decides the op, the path the resource."""
    segments = [s for s in path.split("/") if s and not _VERSION_SEGMENT.match(s)]
    resource = next((s for s in reversed(segments) if not _ID_SEGMENT.match(s)),
                    segments[-1] if segments else "")
    return _METHOD_OPS.get(method.upper(), "other"), resource


def _validate_faults(cfg: dict) -> None:
    def _num(key: str, lo: float, hi: float | None = None) -> None:
        value = cfg.get(key)
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{key} must be a number, got {value!r}") from e
        if v < lo or (hi is not None and v > hi):
            raise ConfigError(f"{key} must be in [{lo}, {hi if hi is not None else 'inf'}], got {v}")

    _num("rate_limit", 0)
    _num("latency_ms", 0, 60_000)
    _num("error_rate", 0, 1)
    if not isinstance(cfg.get("fail") or [], list):
        raise ConfigError("fail must be a list of rules")


async def _json(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"body is not valid JSON: {e}") from e


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value)) if isinstance(value, (dict, list)) else value
