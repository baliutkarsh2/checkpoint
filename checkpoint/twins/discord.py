"""Discord twin: stateful in-memory clone of the Discord REST API v10.

Implements the primary Discord REST surfaces used by agents:
  Guilds      — metadata, members, roles
  Channels    — text channels, categories, CRUD
  Messages    — send, edit, delete, bulk-delete, reactions
  Webhooks    — create + execute

Authentication mirrors Discord bot token format:
  Authorization: Bot <token>

Introspection at /_health, /_trace, /_state, /_reset, /_seed/<name>, /_seed-file.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="checkpoint discord twin")

DEFAULT_BOOTSTRAP_TOKEN = "Bot checkpoint.discord.twin.token.aabbccddeeff0011"
INTROSPECTION_PREFIX = "/_"

SEEDS_DIR = Path(__file__).parent / "discord_seeds"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snowflake() -> str:
    """Monotonically increasing Discord-style snowflake ID (simplified)."""
    STATE["_counters"]["snowflake_seq"] += 1
    return str(int(time.time() * 1000) * 1000 + STATE["_counters"]["snowflake_seq"])


def _fresh_state() -> dict:
    return {
        "guilds": {},    # guild_id -> guild dict
        "channels": {},  # channel_id -> channel dict (global, all guilds)
        "messages": {},  # channel_id -> [message dict]
        "members": {},   # guild_id -> {user_id -> member dict}
        "roles": {},     # guild_id -> {role_id -> role dict}
        "webhooks": {},  # webhook_id -> webhook dict
        "users": {},     # user_id -> user dict
        "_counters": {
            "snowflake_seq": 0,
            "requests": 0,
        },
        "_config": {
            "rate_limit": None,
            "bot_user_id": "bot-000001",
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers -----------------------------------------------------------------

def discord_error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def _bootstrap_token() -> str:
    return os.environ.get("DISCORD_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    for prefix in ("Bot ", "Bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return auth_header.strip()


def _check_auth(request: Request) -> bool:
    if request.url.path.startswith(INTROSPECTION_PREFIX):
        return True
    expected = _bootstrap_token()
    raw = expected[4:] if expected.startswith("Bot ") else expected
    token = _extract_token(request.headers.get("Authorization"))
    return token == raw or f"Bot {token}" == expected or token == expected


def _build_bot_user() -> dict:
    return {
        "id": STATE["_config"]["bot_user_id"],
        "username": "checkpoint-bot",
        "discriminator": "0001",
        "avatar": None,
        "bot": True,
    }


# --- middleware / tracing ----------------------------------------------------

@app.middleware("http")
async def _middleware(request: Request, call_next):
    path = request.url.path
    is_introspection = path.startswith(INTROSPECTION_PREFIX)
    is_mcp = path.startswith("/mcp")
    if not is_introspection and not is_mcp:
        STATE["_counters"]["requests"] += 1
        if not _check_auth(request):
            return JSONResponse(status_code=401, content={"code": 0, "message": "401: Unauthorized"})
    response = await call_next(request)
    if not is_introspection and not is_mcp:
        TRACE.append({
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "ts": _now(),
        })
    return response


# --- introspection -----------------------------------------------------------

@app.get("/_health")
def health():
    return {"ok": True, "twin": "discord"}


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
async def configure(request: Request):
    body = await request.json()
    if "rate_limit" in body:
        STATE["_config"]["rate_limit"] = body["rate_limit"]
    return {"ok": True, "config": STATE["_config"]}


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
    for ck, cv in (data.get("config") or {}).items():
        STATE["_config"][ck] = cv
    return {"ok": True, "seed": name}


@app.post("/_seed-file")
async def load_seed_file(request: Request):
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
    for ck, cv in (data.get("config") or {}).items():
        STATE["_config"][ck] = cv
    return {"ok": True}


# --- Gateway / current user --------------------------------------------------

@app.get("/api/v10/gateway/bot")
def gateway_bot():
    return {"url": "wss://gateway.discord.gg", "shards": 1, "session_start_limit": {"total": 1000, "remaining": 999}}


@app.get("/api/v10/users/@me")
def get_current_user():
    return _build_bot_user()


@app.get("/api/v10/users/{user_id}")
def get_user(user_id: str):
    if user_id == "@me":
        return _build_bot_user()
    u = STATE["users"].get(user_id)
    if not u:
        return discord_error(404, 10013, "Unknown User")
    return u


# --- Guilds ------------------------------------------------------------------

@app.get("/api/v10/guilds/{guild_id}")
def get_guild(guild_id: str):
    g = STATE["guilds"].get(guild_id)
    if not g:
        return discord_error(404, 10004, "Unknown Guild")
    return g


@app.get("/api/v10/guilds/{guild_id}/channels")
def list_guild_channels(guild_id: str):
    if guild_id not in STATE["guilds"]:
        return discord_error(404, 10004, "Unknown Guild")
    return [c for c in STATE["channels"].values() if c.get("guild_id") == guild_id]


@app.post("/api/v10/guilds/{guild_id}/channels")
async def create_channel(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return discord_error(404, 10004, "Unknown Guild")
    body = await request.json()
    name = body.get("name")
    if not name:
        return discord_error(400, 50035, "name is required")
    cid = _snowflake()
    channel: dict[str, Any] = {
        "id": cid,
        "guild_id": guild_id,
        "name": name,
        "type": body.get("type", 0),  # 0=text, 4=category
        "position": body.get("position", 0),
        "topic": body.get("topic"),
        "nsfw": bool(body.get("nsfw", False)),
        "parent_id": body.get("parent_id"),
        "permission_overwrites": body.get("permission_overwrites", []),
        "last_message_id": None,
    }
    STATE["channels"][cid] = channel
    STATE["messages"][cid] = []
    STATE["guilds"][guild_id]["approximate_member_count"] = len(STATE["members"].get(guild_id, {}))
    return channel


# --- Channels ----------------------------------------------------------------

@app.get("/api/v10/channels/{channel_id}")
def get_channel(channel_id: str):
    c = STATE["channels"].get(channel_id)
    if not c:
        return discord_error(404, 10003, "Unknown Channel")
    return c


@app.patch("/api/v10/channels/{channel_id}")
async def modify_channel(channel_id: str, request: Request):
    c = STATE["channels"].get(channel_id)
    if not c:
        return discord_error(404, 10003, "Unknown Channel")
    body = await request.json()
    for field in ("name", "topic", "nsfw", "position", "parent_id", "rate_limit_per_user"):
        if field in body:
            c[field] = body[field]
    return c


@app.delete("/api/v10/channels/{channel_id}")
def delete_channel(channel_id: str):
    c = STATE["channels"].pop(channel_id, None)
    if not c:
        return discord_error(404, 10003, "Unknown Channel")
    STATE["messages"].pop(channel_id, None)
    return c


# --- Messages ----------------------------------------------------------------

@app.get("/api/v10/channels/{channel_id}/messages")
async def list_messages(channel_id: str, request: Request):
    if channel_id not in STATE["channels"]:
        return discord_error(404, 10003, "Unknown Channel")
    params = dict(request.query_params)
    limit = min(int(params.get("limit", 50)), 100)
    msgs = list(STATE["messages"].get(channel_id, []))
    # before / after / around filters (by message id)
    before = params.get("before")
    after = params.get("after")
    if before:
        msgs = [m for m in msgs if m["id"] < before]
    if after:
        msgs = [m for m in msgs if m["id"] > after]
    return msgs[-limit:][::-1]  # newest first


@app.post("/api/v10/channels/{channel_id}/messages")
async def create_message(channel_id: str, request: Request):
    if channel_id not in STATE["channels"]:
        return discord_error(404, 10003, "Unknown Channel")
    body = await request.json()
    content = body.get("content", "")
    if not content and not body.get("embeds") and not body.get("attachments"):
        return discord_error(400, 50006, "Cannot send an empty message")
    mid = _snowflake()
    msg: dict[str, Any] = {
        "id": mid,
        "channel_id": channel_id,
        "author": _build_bot_user(),
        "content": content,
        "timestamp": _now(),
        "edited_timestamp": None,
        "tts": bool(body.get("tts", False)),
        "mention_everyone": "@everyone" in content,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": body.get("embeds", []),
        "reactions": [],
        "pinned": False,
        "type": 0,
        "message_reference": body.get("message_reference"),
    }
    if body.get("message_reference"):
        msg["referenced_message"] = None
    STATE["messages"].setdefault(channel_id, []).append(msg)
    STATE["channels"][channel_id]["last_message_id"] = mid
    return msg


@app.patch("/api/v10/channels/{channel_id}/messages/{message_id}")
async def edit_message(channel_id: str, message_id: str, request: Request):
    msgs = STATE["messages"].get(channel_id, [])
    msg = next((m for m in msgs if m["id"] == message_id), None)
    if not msg:
        return discord_error(404, 10008, "Unknown Message")
    body = await request.json()
    if "content" in body:
        msg["content"] = body["content"]
    if "embeds" in body:
        msg["embeds"] = body["embeds"]
    msg["edited_timestamp"] = _now()
    return msg


@app.delete("/api/v10/channels/{channel_id}/messages/{message_id}")
def delete_message(channel_id: str, message_id: str):
    msgs = STATE["messages"].get(channel_id, [])
    idx = next((i for i, m in enumerate(msgs) if m["id"] == message_id), None)
    if idx is None:
        return discord_error(404, 10008, "Unknown Message")
    msgs.pop(idx)
    return Response(status_code=204)


@app.post("/api/v10/channels/{channel_id}/messages/bulk-delete")
async def bulk_delete_messages(channel_id: str, request: Request):
    if channel_id not in STATE["channels"]:
        return discord_error(404, 10003, "Unknown Channel")
    body = await request.json()
    ids_to_delete = set(body.get("messages", []))
    if not ids_to_delete:
        return discord_error(400, 50016, "Provided too few messages to delete")
    STATE["messages"][channel_id] = [
        m for m in STATE["messages"].get(channel_id, [])
        if m["id"] not in ids_to_delete
    ]
    return Response(status_code=204)


# --- Reactions ---------------------------------------------------------------

@app.put("/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me")
def add_reaction(channel_id: str, message_id: str, emoji: str):
    msgs = STATE["messages"].get(channel_id, [])
    msg = next((m for m in msgs if m["id"] == message_id), None)
    if not msg:
        return discord_error(404, 10008, "Unknown Message")
    reactions = msg.setdefault("reactions", [])
    existing = next((r for r in reactions if r["emoji"]["name"] == emoji), None)
    if existing:
        existing["count"] += 1
        existing["me"] = True
    else:
        reactions.append({"emoji": {"id": None, "name": emoji}, "count": 1, "me": True})
    return Response(status_code=204)


@app.delete("/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me")
def remove_reaction(channel_id: str, message_id: str, emoji: str):
    msgs = STATE["messages"].get(channel_id, [])
    msg = next((m for m in msgs if m["id"] == message_id), None)
    if not msg:
        return discord_error(404, 10008, "Unknown Message")
    reactions = msg.get("reactions", [])
    r = next((x for x in reactions if x["emoji"]["name"] == emoji), None)
    if r:
        r["count"] -= 1
        r["me"] = False
        if r["count"] <= 0:
            msg["reactions"] = [x for x in reactions if x["emoji"]["name"] != emoji]
    return Response(status_code=204)


@app.get("/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}")
def get_reactions(channel_id: str, message_id: str, emoji: str):
    msgs = STATE["messages"].get(channel_id, [])
    msg = next((m for m in msgs if m["id"] == message_id), None)
    if not msg:
        return discord_error(404, 10008, "Unknown Message")
    r = next((x for x in msg.get("reactions", []) if x["emoji"]["name"] == emoji), None)
    if not r:
        return []
    return [{"id": str(i), "username": f"user-{i}", "discriminator": "0000", "avatar": None}
            for i in range(r["count"])]


# --- Guild Members -----------------------------------------------------------

@app.get("/api/v10/guilds/{guild_id}/members")
async def list_members(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return discord_error(404, 10004, "Unknown Guild")
    params = dict(request.query_params)
    limit = min(int(params.get("limit", 100)), 1000)
    members = list((STATE["members"].get(guild_id) or {}).values())
    return members[:limit]


@app.get("/api/v10/guilds/{guild_id}/members/{user_id}")
def get_member(guild_id: str, user_id: str):
    if guild_id not in STATE["guilds"]:
        return discord_error(404, 10004, "Unknown Guild")
    m = (STATE["members"].get(guild_id) or {}).get(user_id)
    if not m:
        return discord_error(404, 10007, "Unknown Member")
    return m


@app.put("/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
def assign_role(guild_id: str, user_id: str, role_id: str):
    members = STATE["members"].setdefault(guild_id, {})
    m = members.get(user_id)
    if not m:
        return discord_error(404, 10007, "Unknown Member")
    if role_id not in m.get("roles", []):
        m.setdefault("roles", []).append(role_id)
    return Response(status_code=204)


@app.delete("/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
def remove_role_from_member(guild_id: str, user_id: str, role_id: str):
    members = STATE["members"].get(guild_id) or {}
    m = members.get(user_id)
    if not m:
        return discord_error(404, 10007, "Unknown Member")
    m["roles"] = [r for r in m.get("roles", []) if r != role_id]
    return Response(status_code=204)


# --- Roles -------------------------------------------------------------------

@app.get("/api/v10/guilds/{guild_id}/roles")
def list_roles(guild_id: str):
    if guild_id not in STATE["guilds"]:
        return discord_error(404, 10004, "Unknown Guild")
    return list((STATE["roles"].get(guild_id) or {}).values())


@app.post("/api/v10/guilds/{guild_id}/roles")
async def create_role(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return discord_error(404, 10004, "Unknown Guild")
    body = await request.json()
    rid = _snowflake()
    role: dict[str, Any] = {
        "id": rid,
        "name": body.get("name", "new role"),
        "color": body.get("color", 0),
        "hoist": bool(body.get("hoist", False)),
        "permissions": str(body.get("permissions", 0)),
        "mentionable": bool(body.get("mentionable", False)),
        "position": body.get("position", 1),
        "managed": False,
    }
    STATE["roles"].setdefault(guild_id, {})[rid] = role
    return role


@app.patch("/api/v10/guilds/{guild_id}/roles/{role_id}")
async def modify_role(guild_id: str, role_id: str, request: Request):
    roles = STATE["roles"].get(guild_id) or {}
    role = roles.get(role_id)
    if not role:
        return discord_error(404, 10011, "Unknown Role")
    body = await request.json()
    for field in ("name", "color", "hoist", "permissions", "mentionable", "position"):
        if field in body:
            role[field] = body[field]
    return role


@app.delete("/api/v10/guilds/{guild_id}/roles/{role_id}")
def delete_role(guild_id: str, role_id: str):
    roles = STATE["roles"].get(guild_id) or {}
    if role_id not in roles:
        return discord_error(404, 10011, "Unknown Role")
    del roles[role_id]
    return Response(status_code=204)


# --- Webhooks ----------------------------------------------------------------

@app.post("/api/v10/channels/{channel_id}/webhooks")
async def create_webhook(channel_id: str, request: Request):
    if channel_id not in STATE["channels"]:
        return discord_error(404, 10003, "Unknown Channel")
    body = await request.json()
    wid = _snowflake()
    token = str(uuid.uuid4()).replace("-", "")
    webhook: dict[str, Any] = {
        "id": wid,
        "type": 1,
        "channel_id": channel_id,
        "guild_id": STATE["channels"][channel_id].get("guild_id"),
        "name": body.get("name", "Webhook"),
        "avatar": body.get("avatar"),
        "token": token,
        "url": f"https://discord.com/api/webhooks/{wid}/{token}",
    }
    STATE["webhooks"][wid] = webhook
    return webhook


@app.get("/api/v10/channels/{channel_id}/webhooks")
def list_channel_webhooks(channel_id: str):
    if channel_id not in STATE["channels"]:
        return discord_error(404, 10003, "Unknown Channel")
    return [w for w in STATE["webhooks"].values() if w.get("channel_id") == channel_id]


@app.post("/api/v10/webhooks/{webhook_id}/{webhook_token}")
async def execute_webhook(webhook_id: str, webhook_token: str, request: Request):
    w = STATE["webhooks"].get(webhook_id)
    if not w or w.get("token") != webhook_token:
        return discord_error(404, 10015, "Unknown Webhook")
    body = await request.json()
    channel_id = w["channel_id"]
    content = body.get("content", "")
    mid = _snowflake()
    msg: dict[str, Any] = {
        "id": mid,
        "channel_id": channel_id,
        "author": {"id": webhook_id, "username": w["name"], "discriminator": "0000", "bot": True, "webhook_id": webhook_id},
        "content": content,
        "timestamp": _now(),
        "edited_timestamp": None,
        "embeds": body.get("embeds", []),
        "attachments": [],
        "reactions": [],
        "type": 0,
    }
    STATE["messages"].setdefault(channel_id, []).append(msg)
    STATE["channels"][channel_id]["last_message_id"] = mid
    return msg


@app.delete("/api/v10/webhooks/{webhook_id}")
def delete_webhook(webhook_id: str):
    if webhook_id not in STATE["webhooks"]:
        return discord_error(404, 10015, "Unknown Webhook")
    del STATE["webhooks"][webhook_id]
    return Response(status_code=204)


# --- Search / pins -----------------------------------------------------------

@app.get("/api/v10/channels/{channel_id}/pins")
def get_pins(channel_id: str):
    if channel_id not in STATE["channels"]:
        return discord_error(404, 10003, "Unknown Channel")
    return [m for m in STATE["messages"].get(channel_id, []) if m.get("pinned")]


@app.put("/api/v10/channels/{channel_id}/pins/{message_id}")
def pin_message(channel_id: str, message_id: str):
    msgs = STATE["messages"].get(channel_id, [])
    msg = next((m for m in msgs if m["id"] == message_id), None)
    if not msg:
        return discord_error(404, 10008, "Unknown Message")
    msg["pinned"] = True
    return Response(status_code=204)


@app.delete("/api/v10/channels/{channel_id}/pins/{message_id}")
def unpin_message(channel_id: str, message_id: str):
    msgs = STATE["messages"].get(channel_id, [])
    msg = next((m for m in msgs if m["id"] == message_id), None)
    if not msg:
        return discord_error(404, 10008, "Unknown Message")
    msg["pinned"] = False
    return Response(status_code=204)


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.discord_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
