"""Long-lived twins: started once, kept running, driven by hand.

A scenario run starts its twins and throws them away. This is the other mode —
``checkpoint twins start github`` leaves a twin up so you can point curl, a
notebook, an MCP client or a half-written agent at it and watch what your code
does to a service that remembers.

Each twin runs in its own detached process, so the command that started it can
exit without taking the twin with it. What is running is recorded in
``.checkpoint/cache/twins.json``:

    {
      "github": {
        "pid": 12345, "port": 18001, "host": "127.0.0.1",
        "started_at": "2026-09-19T05:10:00Z",
        "url": "http://127.0.0.1:18001",
        "mcp_url": "http://127.0.0.1:18001/mcp/",
        "token": "ghp_CHECKPOINTFAKE..."
      }
    }

That file is written, not trusted: a process can die without telling anyone, so
every read checks liveness and drops entries whose process is gone. Each
function takes the file's path so tests can point somewhere harmless.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from . import registry as twin_registry

SESSIONS_FILE = Path(".checkpoint/cache/twins.json")


class TwinNotRunning(RuntimeError):
    """No twin of that name is up.

    Raised with a message written for whoever asked — a person at a terminal or
    a dashboard user — so a caller can show it without deciding whether what it
    holds is safe to reveal.
    """


def _utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, sessions: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, indent=2))


def _wait_healthy(port: int, host: str = "127.0.0.1", timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://{host}:{port}/_health", timeout=1.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001, S110 — not up yet is the expected case
            pass
        time.sleep(0.15)
    return False


def _alive(pid: int) -> bool:
    """Whether a recorded twin process is still there. Never disturbs it."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # `os.kill(pid, 0)` is the POSIX way to ask, and on Windows it is not a
        # question: os.kill documents that any signal other than CTRL_C_EVENT
        # and CTRL_BREAK_EVENT calls TerminateProcess. CPython happens to spare
        # signal 0 today, but "asking whether the twin is alive" must not rest
        # on that, so ask the OS for the process instead of signalling it.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # gone, or not ours to look at
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # it exists; this process just may not signal it
    except ProcessLookupError:
        return False
    except OSError:
        return False


def start(
    twin: str,
    *,
    sessions_file: Path = SESSIONS_FILE,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> dict:
    """Start a twin and leave it running. Returns its session entry."""
    # Resolved per call, not at import: a project's own [twins.<name>] service
    # is registered while the command runs, long after this module loaded.
    spec = twin_registry.get(twin)

    sessions = _read(sessions_file)
    existing = sessions.get(spec.name)
    if existing:
        if _alive(existing.get("pid", -1)):
            raise RuntimeError(
                f"the {spec.name} twin is already running (pid {existing['pid']}, "
                f"{existing['url']}). Stop it first: checkpoint twins stop {spec.name}")
        del sessions[spec.name]

    chosen = port or _free_port()
    # Serving through uvicorn's CLI keeps the twin in a process of its own, so
    # a crash in one twin cannot take down the session that started it.
    env = dict(os.environ)
    if not spec.builtin:
        # A project-defined twin lives in the user's repository, which the child
        # interpreter would otherwise know nothing about.
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [os.getcwd(), env.get("PYTHONPATH", "")]))
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", spec.app,
         "--host", host, "--port", str(chosen), "--log-level", "warning"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )

    if not _wait_healthy(chosen, host):
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001, S110
                pass
        raise RuntimeError(f"the {spec.name} twin did not come up on port {chosen}")

    entry = {
        "pid": process.pid,
        "port": chosen,
        "host": host,
        "started_at": _utc_iso(),
        "url": f"http://{host}:{chosen}",
        "mcp_url": f"http://{host}:{chosen}/mcp/",
        "token": spec.token,
    }
    sessions[spec.name] = entry
    _write(sessions_file, sessions)
    return entry


def inspect(twin: str, *, sessions_file: Path = SESSIONS_FILE) -> dict | None:
    """The session entry plus what the twin currently holds, or None if unknown.

    A dead process comes back with ``alive=False`` and its entry is dropped, so
    the file heals itself rather than accumulating ghosts.
    """
    sessions = _read(sessions_file)
    entry = sessions.get(twin)
    if not entry:
        return None
    alive = _alive(entry.get("pid", -1))
    out = dict(entry, alive=alive)
    if not alive:
        del sessions[twin]
        _write(sessions_file, sessions)
        return out
    try:
        state = httpx.get(f"{entry['url']}/_state", timeout=2.0).json()
        out["state_keys"] = sorted(state.keys()) if isinstance(state, dict) else []
        out["state_size"] = len(json.dumps(state, default=str))
    except Exception as e:  # noqa: BLE001
        out["state_error"] = str(e)[:160]
        out["state_size"] = 0
        out["state_keys"] = []
    try:
        trace = httpx.get(f"{entry['url']}/_trace", timeout=2.0).json()
        out["request_count"] = len(trace) if isinstance(trace, list) else 0
    except Exception:  # noqa: BLE001
        out["request_count"] = 0
    return out


def stop(twin: str, *, sessions_file: Path = SESSIONS_FILE, timeout: float = 5.0) -> bool:
    """Stop a running twin. True if it was running, False if it was not."""
    sessions = _read(sessions_file)
    entry = sessions.get(twin)
    if not entry:
        return False
    pid = entry.get("pid", -1)
    was_alive = _alive(pid)
    if was_alive:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            was_alive = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _alive(pid):
                break
            time.sleep(0.1)
        else:
            # Windows has no SIGKILL; there SIGTERM already means TerminateProcess.
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                # It exited between the liveness check and this signal, which is
                # the outcome we wanted anyway.
                pass
    del sessions[twin]
    _write(sessions_file, sessions)
    return was_alive


def list_all(*, sessions_file: Path = SESSIONS_FILE) -> list[dict]:
    """Every recorded twin, with liveness. Dead ones are dropped as we go."""
    sessions = _read(sessions_file)
    out: list[dict] = []
    purged = False
    for name, entry in list(sessions.items()):
        alive = _alive(entry.get("pid", -1))
        out.append(dict(entry, id=name, alive=alive))
        if not alive:
            del sessions[name]
            purged = True
    if purged:
        _write(sessions_file, sessions)
    return out


def reset(twin: str, *, sessions_file: Path = SESSIONS_FILE) -> dict:
    """Put a running twin back to its factory state."""
    entry = _running(twin, sessions_file)
    try:
        response = httpx.post(f"{entry['url']}/_reset", timeout=5.0)
        return {"ok": response.status_code < 400, "status": response.status_code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def seed(twin: str, name: str, *, sessions_file: Path = SESSIONS_FILE) -> dict:
    """Load a named dataset into a running twin."""
    entry = _running(twin, sessions_file)
    try:
        # The name is user input: encode it so it cannot climb out of the
        # path and address some other endpoint on the twin.
        response = httpx.post(f"{entry['url']}/_seed/{quote(name, safe='')}",
                              timeout=10.0)
        is_json = response.headers.get("content-type", "").startswith("application/json")
        return {"ok": response.status_code < 400, "status": response.status_code,
                "body": response.json() if is_json else None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def tools(twin: str, *, sessions_file: Path = SESSIONS_FILE) -> dict:
    """The MCP tools a running twin exposes, for agents that speak MCP."""
    entry = _running(twin, sessions_file)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        response = httpx.post(entry["mcp_url"], json=payload, timeout=5.0,
                              headers={"accept": "application/json, text/event-stream"})
        if response.status_code != 200:
            return {"ok": False, "status": response.status_code, "tools": []}
        # Streamable HTTP frames the reply as SSE; the answer is the first
        # `data:` line. Some servers answer with plain JSON instead.
        for line in response.text.splitlines():
            if line.startswith("data:"):
                message = json.loads(line[5:].strip())
                if message.get("id") == 1 and isinstance(message.get("result"), dict):
                    return {"ok": True, "tools": message["result"].get("tools", [])}
        try:
            message = json.loads(response.text)
            return {"ok": True, "tools": (message.get("result") or {}).get("tools", [])}
        except json.JSONDecodeError:
            return {"ok": True, "tools": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "tools": []}


def renew(twin: str, *, ttl_seconds: int, sessions_file: Path = SESSIONS_FILE) -> dict:
    """Record when a twin is meant to expire.

    Advisory only, and deliberately so: nothing here kills an expired twin.
    Stopping one is always somebody's decision, never a timer's.
    """
    sessions = _read(sessions_file)
    if twin not in sessions:
        raise KeyError(twin)
    expires_at = time.time() + max(60, int(ttl_seconds))
    sessions[twin].update(
        ttl_seconds=int(ttl_seconds),
        expires_at=expires_at,
        expires_at_iso=datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
    )
    _write(sessions_file, sessions)
    return sessions[twin]


def configure(
    twin: str,
    *,
    rate_limit: int | None = None,
    permissions_denied: bool | None = None,
    read_only: bool | None = None,
    sessions_file: Path = SESSIONS_FILE,
) -> dict:
    """Make a running twin misbehave the way the real service sometimes does."""
    entry = _running(twin, sessions_file)
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
        response = httpx.post(f"{entry['url']}/_config", json=body, timeout=5.0)
        return {"ok": response.status_code < 400, "status": response.status_code,
                "applied": body}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def _running(twin: str, sessions_file: Path) -> dict:
    """The session entry for a twin that is actually up, or an explanation."""
    sessions = _read(sessions_file)
    entry = sessions.get(twin)
    if not entry:
        raise TwinNotRunning(
            f"no {twin} twin is running. Start one: checkpoint twins start {twin}")
    if not _alive(entry.get("pid", -1)):
        del sessions[twin]
        _write(sessions_file, sessions)
        raise TwinNotRunning(
            f"the {twin} twin was recorded as running, but its process is gone. "
            f"Start it again: checkpoint twins start {twin}")
    return entry
