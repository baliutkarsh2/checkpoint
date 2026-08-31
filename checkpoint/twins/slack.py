"""Slack twin: stateful in-memory clone of Slack Web API.

Phase 3 plan 01 (+F2): 10 MCP-tool-equivalent REST endpoints (incl.
conversations.create / conversations.info), bootstrap-token auth,
Slack-shape `{ok: false, error: "..."}` envelope, introspection endpoints.

The Slack Web API uses HTTP 200 even for application errors — clients
check the `ok` boolean. We mirror that exactly so any unmodified Slack SDK
(`slack_sdk`, `@slack/web-api`) reads our twin as if it were Slack.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from checkpoint.fake_credentials import FAKE_SLACK_TOKEN

app = FastAPI(title="checkpoint slack twin")

# Per SCOPE §3.4 / REQUIREMENTS.md SL-02.
DEFAULT_BOOTSTRAP_TOKEN = FAKE_SLACK_TOKEN
INTROSPECTION_PREFIX = "/_"

SEEDS_DIR = Path(__file__).parent / "slack_seeds"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> str:
    """Slack-style monotonic timestamp `seconds.microseconds`."""
    STATE["_counters"]["ts_seq"] += 1
    base = int(time.time())
    return f"{base}.{STATE['_counters']['ts_seq']:06d}"


def _fresh_state() -> dict:
    return {
        "channels": {},   # channel_id -> {id, name, is_channel, num_members, topic, ...}
        "users": {},      # user_id -> {id, name, real_name, profile: {...}}
        "messages": {},   # channel_id -> [message dicts]
        "_counters": {
            "channel_id": 0,
            "user_id": 0,
            "ts_seq": 0,
            "requests": 0,
        },
        "_config": {
            "page_size": 100,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers -------------------------------------------------------------

def slack_error(error: str, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": error})


def slack_ok(**fields: Any) -> dict:
    body: dict[str, Any] = {"ok": True}
    body.update(fields)
    return body


def _bootstrap_token() -> str:
    return os.environ.get("SLACK_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    auth_header = auth_header.strip()
    for prefix in ("Bearer ", "bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return None


def _new_channel_id() -> str:
    STATE["_counters"]["channel_id"] += 1
    return f"C{STATE['_counters']['channel_id']:08d}"


def _new_user_id() -> str:
    STATE["_counters"]["user_id"] += 1
    return f"U{STATE['_counters']['user_id']:08d}"


def _slack_headers() -> dict:
    return {
        "X-Slack-Req-Id": uuid.uuid4().hex[:16],
    }


# --- middlewares ---------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Introspection and the mounted MCP transport bypass token auth.
    # MCP tool bodies stamp the bootstrap token back on when shimming
    # into the REST surface.
    if path.startswith(INTROSPECTION_PREFIX) or path.startswith("/mcp"):
        return await call_next(request)

    token = _extract_token(request.headers.get("authorization"))
    if not token:
        return slack_error("not_authed")
    if token != _bootstrap_token():
        return slack_error("invalid_auth")

    STATE["_counters"]["requests"] += 1
    response = await call_next(request)
    for k, v in _slack_headers().items():
        response.headers[k] = v
    return response


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(INTROSPECTION_PREFIX) or path.startswith("/mcp"):
        return await call_next(request)

    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else None
    except Exception:
        # Slack also accepts form-encoded — record raw.
        body = body_bytes.decode("utf-8", errors="replace") if body_bytes else None

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    request._receive = receive  # type: ignore[attr-defined]
    response = await call_next(request)

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    resp_bytes = b"".join(chunks)
    try:
        resp_body = json.loads(resp_bytes) if resp_bytes else None
    except Exception:
        resp_body = resp_bytes.decode("utf-8", errors="replace") if resp_bytes else None

    TRACE.append({
        "ts": _now_iso(),
        "method": request.method,
        "path": path,
        "query": dict(request.query_params),
        "body": body,
        "status": response.status_code,
        "response": resp_body,
    })

    return Response(
        content=resp_bytes,
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
        media_type=response.media_type,
    )


# --- introspection -------------------------------------------------------

@app.get("/_health")
def health():
    return {"ok": True}


@app.get("/_trace")
def get_trace():
    return TRACE


@app.get("/_state")
def get_state():
    return {k: v for k, v in STATE.items() if not k.startswith("_") or k == "_config"}


@app.post("/_reset")
def reset():
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()
    return {"ok": True}


@app.post("/_config")
async def set_config(request: Request):
    body = await request.json()
    cfg = STATE["_config"]
    if "page_size" in body:
        cfg["page_size"] = int(body["page_size"])
    return {"ok": True, "config": cfg}


@app.post("/_seed/{name}")
def load_seed(name: str):
    path = SEEDS_DIR / f"{name}.json"
    if not path.exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": f"seed {name!r} not found"})
    data = json.loads(path.read_text())
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()
    for k, v in (data.get("state") or {}).items():
        if isinstance(v, dict) and isinstance(STATE.get(k), dict):
            STATE[k].update(v)
        else:
            STATE[k] = v
    cfg = data.get("config") or {}
    for ck, cv in cfg.items():
        STATE["_config"][ck] = cv
    return {"ok": True, "seed": name, "config": STATE["_config"]}


@app.post("/_seed-file")
async def load_seed_file(request: Request):
    """Apply an inline JSON seed payload (same shape as slack_seeds/*.json)."""
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "body must be a JSON object"})
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()
    for k, v in (data.get("state") or {}).items():
        if isinstance(v, dict) and isinstance(STATE.get(k), dict):
            STATE[k].update(v)
        else:
            STATE[k] = v
    cfg = data.get("config") or {}
    for ck, cv in cfg.items():
        STATE["_config"][ck] = cv
    return {"ok": True, "config": STATE["_config"]}


# --- chat.postMessage / reply_to_thread ---------------------------------

@app.post("/api/chat.postMessage")
async def chat_post_message(request: Request):
    try:
        body = await request.json()
    except Exception:
        # Try form
        form = await request.form()
        body = dict(form)
    channel = body.get("channel")
    text = body.get("text")
    if not channel:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: channel"})
    if text is None or text == "":
        return JSONResponse(content={"ok": False, "error": "missing required arguments: text"})

    # Resolve channel by id or name
    ch = _find_channel(channel)
    if ch is None:
        return JSONResponse(content={"ok": False, "error": "channel_not_found"})

    thread_ts = body.get("thread_ts")
    ts = _ts()
    msg = {
        "type": "message",
        "user": body.get("user") or "U00000001",
        "text": text,
        "ts": ts,
        "channel": ch["id"],
    }
    if thread_ts:
        msg["thread_ts"] = thread_ts
        # bump parent reply_count
        parent = _find_message(ch["id"], thread_ts)
        if parent is not None:
            parent["reply_count"] = parent.get("reply_count", 0) + 1
            parent["latest_reply"] = ts

    STATE["messages"].setdefault(ch["id"], []).append(msg)
    return slack_ok(channel=ch["id"], ts=ts, message=msg)


# --- conversations.history ----------------------------------------------

@app.get("/api/conversations.history")
def conversations_history(channel: str = "", limit: int = 100, cursor: str = ""):
    if not channel:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: channel"})
    ch = _find_channel(channel)
    if ch is None:
        return JSONResponse(content={"ok": False, "error": "channel_not_found"})
    msgs = list(STATE["messages"].get(ch["id"], []))
    # Only top-level messages (no thread replies) — Slack semantics.
    msgs = [m for m in msgs if not m.get("thread_ts") or m.get("thread_ts") == m.get("ts")]
    msgs.sort(key=lambda m: m["ts"], reverse=True)
    start = int(cursor) if cursor.isdigit() else 0
    page = msgs[start:start + limit]
    has_more = (start + limit) < len(msgs)
    next_cursor = str(start + limit) if has_more else ""
    return slack_ok(
        messages=page,
        has_more=has_more,
        response_metadata={"next_cursor": next_cursor},
    )


# --- conversations.replies ----------------------------------------------

@app.get("/api/conversations.replies")
def conversations_replies(channel: str = "", ts: str = ""):
    if not channel:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: channel"})
    if not ts:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: ts"})
    ch = _find_channel(channel)
    if ch is None:
        return JSONResponse(content={"ok": False, "error": "channel_not_found"})
    parent = _find_message(ch["id"], ts)
    if parent is None:
        return JSONResponse(content={"ok": False, "error": "thread_not_found"})
    msgs = STATE["messages"].get(ch["id"], [])
    replies = [m for m in msgs if m.get("thread_ts") == ts and m.get("ts") != ts]
    out = [parent] + sorted(replies, key=lambda m: m["ts"])
    return slack_ok(messages=out, has_more=False)


# --- conversations.create -----------------------------------------------

# Slack: lowercase letters, numbers, hyphens, underscores, periods; max 80 chars.
_CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _valid_channel_name(name: str) -> bool:
    return bool(_CHANNEL_NAME_RE.match(name))


@app.post("/api/conversations.create")
async def conversations_create(request: Request):
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)
    name = body.get("name")
    if not name:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: name"})
    name = str(name).lstrip("#").strip().lower()
    if not _valid_channel_name(name):
        return JSONResponse(content={"ok": False, "error": "invalid_name"})
    if any(ch.get("name") == name for ch in STATE["channels"].values()):
        return JSONResponse(content={"ok": False, "error": "name_taken"})

    is_private = bool(body.get("is_private"))
    channel_id = _new_channel_id()
    channel = {
        "id": channel_id,
        "name": name,
        "name_normalized": name,
        "is_channel": not is_private,
        "is_group": is_private,
        "is_private": is_private,
        "is_im": False,
        "is_archived": False,
        "is_general": False,
        "created": int(time.time()),
        "creator": body.get("user") or "U00000001",
        "num_members": 1,
        "topic": {"value": "", "creator": "", "last_set": 0},
        "purpose": {"value": "", "creator": "", "last_set": 0},
    }
    STATE["channels"][channel_id] = channel
    STATE["messages"].setdefault(channel_id, [])
    return slack_ok(channel=channel)


# --- conversations.info -------------------------------------------------

@app.get("/api/conversations.info")
def conversations_info(channel: str = ""):
    if not channel:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: channel"})
    ch = _find_channel(channel)
    if ch is None:
        return JSONResponse(content={"ok": False, "error": "channel_not_found"})
    return slack_ok(channel=ch)


# --- conversations.list -------------------------------------------------

@app.get("/api/conversations.list")
def conversations_list(cursor: str = "", limit: int = 100, types: str = "public_channel"):
    all_channels = list(STATE["channels"].values())
    all_channels.sort(key=lambda c: c["id"])
    start = int(cursor) if cursor.isdigit() else 0
    page = all_channels[start:start + limit]
    has_more = (start + limit) < len(all_channels)
    next_cursor = str(start + limit) if has_more else ""
    return slack_ok(
        channels=page,
        response_metadata={"next_cursor": next_cursor},
    )


# --- reactions.add ------------------------------------------------------

@app.post("/api/reactions.add")
async def reactions_add(request: Request):
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)
    channel = body.get("channel")
    timestamp = body.get("timestamp")
    name = body.get("name")
    if not channel:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: channel"})
    if not timestamp:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: timestamp"})
    if not name:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: name"})
    ch = _find_channel(channel)
    if ch is None:
        return JSONResponse(content={"ok": False, "error": "channel_not_found"})
    msg = _find_message(ch["id"], timestamp)
    if msg is None:
        return JSONResponse(content={"ok": False, "error": "message_not_found"})
    reactions = msg.setdefault("reactions", [])
    found = next((r for r in reactions if r["name"] == name), None)
    user = body.get("user") or "U00000001"
    if found:
        if user not in found["users"]:
            found["users"].append(user)
            found["count"] = len(found["users"])
    else:
        reactions.append({"name": name, "users": [user], "count": 1})
    return slack_ok()


# --- users.list ---------------------------------------------------------

@app.get("/api/users.list")
def users_list(cursor: str = "", limit: int = 100):
    members = list(STATE["users"].values())
    members.sort(key=lambda u: u["id"])
    start = int(cursor) if cursor.isdigit() else 0
    page = members[start:start + limit]
    has_more = (start + limit) < len(members)
    next_cursor = str(start + limit) if has_more else ""
    return slack_ok(
        members=page,
        response_metadata={"next_cursor": next_cursor},
    )


# --- users.profile.get --------------------------------------------------

@app.get("/api/users.profile.get")
def users_profile_get(user: str = ""):
    if not user:
        return JSONResponse(content={"ok": False, "error": "missing required arguments: user"})
    u = STATE["users"].get(user)
    if u is None:
        # Try by name
        u = next((x for x in STATE["users"].values() if x.get("name") == user), None)
    if u is None:
        return JSONResponse(content={"ok": False, "error": "user_not_found"})
    profile = u.get("profile") or {
        "real_name": u.get("real_name", u.get("name", "")),
        "display_name": u.get("name", ""),
        "email": u.get("email", ""),
    }
    return slack_ok(profile=profile)


# --- internal lookup helpers --------------------------------------------

def _find_channel(ident: str) -> dict | None:
    if not ident:
        return None
    if ident in STATE["channels"]:
        return STATE["channels"][ident]
    # Allow lookup by name (with or without leading #)
    name = ident.lstrip("#")
    for ch in STATE["channels"].values():
        if ch.get("name") == name:
            return ch
    return None


def _find_message(channel_id: str, ts: str) -> dict | None:
    for m in STATE["messages"].get(channel_id, []):
        if m["ts"] == ts:
            return m
    return None


# --- MCP transport -------------------------------------------------------
# Mount the Slack MCP server at /mcp on this same FastAPI app so REST and
# MCP share the same STATE dict (Phase 6, MCP-01/MCP-02).

from checkpoint.mcp_servers.slack_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
