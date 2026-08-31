"""CLI-07: long-lived local twin sessions (clone start/stop/inspect).

A tiny daemon registry at ``.checkpoint/cache/clones.json`` tracks running
twin processes started by ``checkpoint clone start <id>``. Each clone runs
in its own detached subprocess (``start_new_session=True``) so the CLI
exits without orphaning the child.

The registry is a JSON object keyed by clone id:

    {
      "github": {
        "pid": 12345,
        "port": 18001,
        "host": "127.0.0.1",
        "started_at": "2026-05-12T05:10:00Z",
        "url": "http://127.0.0.1:18001",
        "mcp_url": "http://127.0.0.1:18001/mcp/",
        "token": "ghp_CHECKPOINTFAKE..."
      }
    }

The functions in this module are pure-ish — they take the registry path as
an argument so tests can point at ``tmp_path``.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN, FAKE_SLACK_TOKEN, FAKE_STRIPE_KEY, FAKE_LINEAR_TOKEN, FAKE_SUPABASE_TOKEN, FAKE_DISCORD_TOKEN, FAKE_GOOGLE_WORKSPACE_TOKEN


DEFAULT_REGISTRY = Path(".checkpoint/cache/clones.json")

# Source of truth for which clones we can start, mirroring runner.TWIN_APPS.
TWIN_APPS = {
    "github": "checkpoint.twins.github:app",
    "slack": "checkpoint.twins.slack:app",
    "stripe": "checkpoint.twins.stripe:app",
    "linear": "checkpoint.twins.linear:app",
    "supabase": "checkpoint.twins.supabase:app",
    "discord": "checkpoint.twins.discord:app",
    "google-workspace": "checkpoint.twins.google_workspace:app",
}

_CLONE_TOKEN = {
    "github": FAKE_GITHUB_TOKEN,
    "slack": FAKE_SLACK_TOKEN,
    "stripe": FAKE_STRIPE_KEY,
    "linear": FAKE_LINEAR_TOKEN,
    "supabase": FAKE_SUPABASE_TOKEN,
    "discord": FAKE_DISCORD_TOKEN,
    "google-workspace": FAKE_GOOGLE_WORKSPACE_TOKEN,
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_registry(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_registry(path: Path, registry: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2))


def _wait_healthy(port: int, host: str = "127.0.0.1", timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://{host}:{port}/_health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but we can't signal it.
        return True
    except OSError:
        # ProcessLookupError (Unix ESRCH) or WinError 87 (Windows: no such
        # process / invalid parameter) — treat as dead.
        return False


def start(
    clone_id: str,
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> dict:
    """Start a long-lived twin process. Returns the registry entry."""
    if clone_id not in TWIN_APPS:
        raise ValueError(f"unsupported clone={clone_id!r}; known: {sorted(TWIN_APPS)}")

    registry = _read_registry(registry_path)
    if clone_id in registry:
        existing = registry[clone_id]
        if _process_alive(existing.get("pid", -1)):
            raise RuntimeError(
                f"clone {clone_id!r} already running (pid={existing['pid']}, "
                f"url={existing['url']}). Run `clone stop {clone_id}` first."
            )
        # stale entry — drop it and continue
        del registry[clone_id]

    p = port or _free_port()
    app = TWIN_APPS[clone_id]

    # start_new_session=True detaches from this terminal so the CLI exiting
    # doesn't kill the twin. stdout/stderr go to DEVNULL — long-running
    # daemons don't deserve a log file in v1, and the twin's /_trace
    # endpoint covers debugging.
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            app,
            "--host", host,
            "--port", str(p),
            "--log-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    if not _wait_healthy(p, host):
        # Failed to come up — kill the process and raise.
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        raise RuntimeError(f"clone {clone_id!r} failed health check on :{p}")

    entry = {
        "pid": proc.pid,
        "port": p,
        "host": host,
        "started_at": _utc_iso(),
        "url": f"http://{host}:{p}",
        "mcp_url": f"http://{host}:{p}/mcp/",
        "token": _CLONE_TOKEN.get(clone_id, ""),
    }
    registry[clone_id] = entry
    _write_registry(registry_path, registry)
    return entry


def inspect(
    clone_id: str,
    *,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict | None:
    """Return registry entry enriched with live state/trace counts.

    Returns ``None`` if not registered. If the process is dead, the
    returned dict has ``alive=False`` and the registry entry is purged.
    """
    registry = _read_registry(registry_path)
    entry = registry.get(clone_id)
    if not entry:
        return None
    alive = _process_alive(entry.get("pid", -1))
    out = dict(entry)
    out["alive"] = alive
    if not alive:
        # purge stale entries on inspect so the registry self-heals.
        del registry[clone_id]
        _write_registry(registry_path, registry)
        return out
    url = entry["url"]
    try:
        state = httpx.get(f"{url}/_state", timeout=2.0).json()
        out["state_keys"] = sorted(state.keys()) if isinstance(state, dict) else []
        out["state_size"] = len(json.dumps(state, default=str))
    except Exception as e:
        out["state_error"] = str(e)[:160]
        out["state_size"] = 0
        out["state_keys"] = []
    try:
        trace = httpx.get(f"{url}/_trace", timeout=2.0).json()
        out["request_count"] = len(trace) if isinstance(trace, list) else 0
    except Exception:
        out["request_count"] = 0
    return out


def stop(
    clone_id: str,
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    timeout: float = 5.0,
) -> bool:
    """Stop a running clone. Returns True if it was running, False otherwise."""
    registry = _read_registry(registry_path)
    entry = registry.get(clone_id)
    if not entry:
        return False
    pid = entry.get("pid", -1)
    was_alive = _process_alive(pid)
    if was_alive:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            was_alive = False
        # Wait for graceful exit.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _process_alive(pid):
                break
            time.sleep(0.1)
        else:
            # signal.SIGKILL does not exist on Windows; fall back to SIGTERM
            # (which calls TerminateProcess there — effectively a force-kill).
            _force_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
            try:
                os.kill(pid, _force_sig)
            except OSError:
                pass
    del registry[clone_id]
    _write_registry(registry_path, registry)
    return was_alive


# ---------------------------------------------------------------------------
# Sprint A extensions: list / status / renew / seed / reset / tools.
# All operate on the same DEFAULT_REGISTRY and never start new processes.
# ---------------------------------------------------------------------------

def list_all(*, registry_path: Path = DEFAULT_REGISTRY) -> list[dict]:
    """Return every registered clone enriched with liveness and TTL state.

    Stale entries (process gone) are auto-purged from the registry — this
    self-heals the file when a daemon dies behind the CLI's back.
    """
    registry = _read_registry(registry_path)
    out: list[dict] = []
    purged = False
    for clone_id, entry in list(registry.items()):
        alive = _process_alive(entry.get("pid", -1))
        rec = dict(entry)
        rec["id"] = clone_id
        rec["alive"] = alive
        if not alive:
            del registry[clone_id]
            purged = True
        out.append(rec)
    if purged:
        _write_registry(registry_path, registry)
    return out


def reset(clone_id: str, *, registry_path: Path = DEFAULT_REGISTRY) -> dict:
    """POST `/_reset` to a running clone. Returns ``{"ok": bool, ...}``."""
    entry = _registry_get_alive(clone_id, registry_path)
    try:
        r = httpx.post(f"{entry['url']}/_reset", timeout=5.0)
        return {"ok": r.status_code < 400, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def seed(clone_id: str, name: str, *, registry_path: Path = DEFAULT_REGISTRY) -> dict:
    """Apply a named seed to a running clone via `/_seed/<name>`."""
    entry = _registry_get_alive(clone_id, registry_path)
    try:
        r = httpx.post(f"{entry['url']}/_seed/{name}", timeout=10.0)
        ok = r.status_code < 400
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        return {"ok": ok, "status": r.status_code, "body": body}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def tools(clone_id: str, *, registry_path: Path = DEFAULT_REGISTRY) -> dict:
    """List MCP tools exposed by a running clone.

    Calls the MCP `tools/list` JSON-RPC method on the clone's `/mcp/`
    endpoint. Falls back to an empty list if the twin doesn't speak MCP.
    """
    entry = _registry_get_alive(clone_id, registry_path)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    try:
        r = httpx.post(
            entry["mcp_url"],
            json=payload,
            timeout=5.0,
            headers={"accept": "application/json, text/event-stream"},
        )
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "tools": []}
        # Streamable-HTTP MCP returns SSE-shaped frames; the JSON-RPC
        # response is the first `data: {...}` line.
        body = r.text
        for line in body.splitlines():
            if line.startswith("data:"):
                obj = json.loads(line[5:].strip())
                if obj.get("id") == 1 and isinstance(obj.get("result"), dict):
                    return {"ok": True, "tools": obj["result"].get("tools", [])}
        # Plain JSON fallback (some MCP implementations).
        try:
            obj = json.loads(body)
            return {"ok": True, "tools": (obj.get("result") or {}).get("tools", [])}
        except json.JSONDecodeError:
            return {"ok": True, "tools": []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "tools": []}


def renew(
    clone_id: str,
    *,
    ttl_seconds: int,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict:
    """Set / extend the clone's `expires_at` metadata.

    Note: TTL is *advisory* metadata for the dashboard and `clone list`.
    Checkpoint does not auto-kill expired clones — there is no daemon to
    enforce it. ``clone stop`` is always a manual decision.
    """
    registry = _read_registry(registry_path)
    if clone_id not in registry:
        raise KeyError(clone_id)
    expires_at = time.time() + max(60, int(ttl_seconds))
    registry[clone_id]["ttl_seconds"] = int(ttl_seconds)
    registry[clone_id]["expires_at"] = expires_at
    registry[clone_id]["expires_at_iso"] = datetime.fromtimestamp(
        expires_at, tz=timezone.utc
    ).isoformat()
    _write_registry(registry_path, registry)
    return registry[clone_id]


def configure(
    clone_id: str,
    *,
    rate_limit: int | None = None,
    permissions_denied: bool | None = None,
    read_only: bool | None = None,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict:
    """POST `/_config` to a running clone (rate_limit + read_only knobs).

    Twins that don't recognize a knob silently ignore it. ``rate_limit`` is
    currently enforced by the GitHub twin; ``read_only`` is enforced by all
    twins via the runner's pre/post state-snapshot diff.
    """
    entry = _registry_get_alive(clone_id, registry_path)
    body: dict = {}
    if rate_limit is not None:
        body["rate_limit"] = int(rate_limit)
    if permissions_denied is not None:
        body["permissions_denied"] = bool(permissions_denied)
    if read_only is not None:
        body["read_only"] = bool(read_only)
    if not body:
        return {"ok": True, "config": {}}
    try:
        r = httpx.post(f"{entry['url']}/_config", json=body, timeout=5.0)
        return {"ok": r.status_code < 400, "status": r.status_code, "applied": body}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _registry_get_alive(clone_id: str, registry_path: Path) -> dict:
    registry = _read_registry(registry_path)
    entry = registry.get(clone_id)
    if not entry:
        raise KeyError(clone_id)
    if not _process_alive(entry.get("pid", -1)):
        # Auto-purge stale entry so the next list reflects truth.
        del registry[clone_id]
        _write_registry(registry_path, registry)
        raise RuntimeError(f"clone {clone_id!r} is registered but the process is gone")
    return entry
