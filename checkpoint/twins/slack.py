"""Slack twin: a stateful, in-memory Slack Web API.

The Slack Web API answers application errors with HTTP 200 and
``{"ok": false, "error": "..."}``; clients check ``ok``. The twin mirrors that
exactly so unmodified SDKs (``slack_sdk``, ``@slack/web-api``) read it as Slack.
The control plane and fault model come from :mod:`checkpoint.twins.kit`.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from checkpoint.fake_credentials import FAKE_SLACK_TOKEN
from checkpoint.twins import kit

app = FastAPI(title="checkpoint slack twin")

DEFAULT_BOOTSTRAP_TOKEN = FAKE_SLACK_TOKEN

SEEDS_DIR = Path(__file__).parent / "slack_seeds"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


# --- runtime: auth, faults, trace, control plane --------------------------

def _request_token(request: Request) -> str | None:
    """Slack accepts the token as a Bearer header, a query param, or a form field."""
    token = _extract_token(request.headers.get("authorization")) or request.query_params.get("token")
    if token:
        return token
    body = getattr(request, "_body", b"") or b""
    if b"token=" in body:
        from urllib.parse import parse_qs
        values = parse_qs(body.decode("utf-8", errors="replace")).get("token")
        return values[0] if values else None
    return None


def _authenticate(request: Request) -> Response | None:
    token = _request_token(request)
    if not token:
        return slack_error("not_authed")
    if TWIN.config.get("strict_auth") and token != _bootstrap_token():
        return slack_error("invalid_auth")
    return None


_FAULT_ERRORS = {
    "forbidden": "missing_scope",
    "read_only": "restricted_action",
    "server_error": "internal_error",
    "unauthorized": "invalid_auth",
}


def _error(kind: str, status: int, message: str) -> Response:
    if kind == "rate_limited":
        return JSONResponse(status_code=429, content={"ok": False, "error": "ratelimited"},
                            headers={"Retry-After": "30"})
    if kind in ("forbidden", "read_only"):
        # Slack reports permission problems as application errors on HTTP 200.
        return slack_error(_FAULT_ERRORS[kind])
    code = _FAULT_ERRORS.get(kind, "service_unavailable" if status >= 500 else "fatal_error")
    return slack_error(code, status=status)


def _stamp_headers(request: Request, response: Response) -> None:
    for k, v in _slack_headers().items():
        response.headers.setdefault(k, v)


_SLACK_FAMILIES = {"chat": "messages", "conversations": "channels", "reactions": "reactions",
                   "users": "users", "files": "files", "pins": "pins", "bookmarks": "bookmarks"}
_READ_VERBS = ("list", "info", "history", "replies", "get", "lookup", "test", "members", "search")
_DELETE_VERBS = ("delete", "remove", "kick", "leave", "archive")
_UPDATE_VERBS = ("update", "set", "rename", "unarchive", "mark")


def _classify(method: str, path: str, body: object) -> tuple[kit.Op, str] | None:
    """Slack is RPC over HTTP: the method name, not the HTTP verb, says what happened."""
    name = path.rsplit("/", 1)[-1]
    family, _, verb = name.partition(".")
    if not verb:
        return None
    resource = _SLACK_FAMILIES.get(family, family)
    verb = verb.lower()
    if verb.startswith(_READ_VERBS):
        return "read", resource
    if verb.startswith(_DELETE_VERBS):
        return "delete", resource
    if verb.startswith(_UPDATE_VERBS):
        return "update", resource
    return "create", resource


def _failed(status: int, body: object) -> bool:
    # Slack answers application errors with HTTP 200 and {"ok": false}.
    return status >= 400 or (isinstance(body, dict) and body.get("ok") is False)


TWIN = kit.install(app, kit.Twin(
    name="slack",
    classify=_classify,
    failed=_failed,
    state=STATE,
    trace=TRACE,
    fresh_state=_fresh_state,
    seeds_dir=SEEDS_DIR,
    error=_error,
    authenticate=_authenticate,
    on_response=_stamp_headers,
))


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
