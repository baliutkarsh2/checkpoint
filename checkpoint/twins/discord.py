"""Discord twin: a stateful, in-memory Discord REST API (v10).

Implements the REST surface agents actually drive, in the shapes the official
SDKs parse (discord.py and discord.js decode every id as an integer snowflake,
so every id this twin invents or seeds is one):

  Application — ``/oauth2/applications/@me`` (discord.py's login does this call),
                slash-command registration
  Guilds      — metadata, members, roles, bans, active threads
  Channels    — text/voice/category CRUD, threads, DMs, typing, permissions
  Messages    — send (JSON or multipart uploads), reply, edit, delete,
                bulk-delete, reactions, pins, pagination
  Webhooks    — create, execute (with and without a bot token), edit, delete

Authentication mirrors Discord bot tokens (``Authorization: Bot <token>``).
Webhook routes that carry their own token need no bot credential, exactly as
the real API, because that is how webhook URLs are meant to be used.

The control plane and fault model come from :mod:`checkpoint.twins.kit`.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from checkpoint.fake_credentials import FAKE_DISCORD_TOKEN
from checkpoint.twins import kit

app = FastAPI(title="checkpoint discord twin")

# Discord serves the same API under /api, /api/v9 and /api/v10; SDKs pick one
# and webhook URLs are unversioned, so every route is mounted under all three.
api = APIRouter()
API_PREFIXES = ("/api/v10", "/api/v9", "/api")

DEFAULT_BOOTSTRAP_TOKEN = FAKE_DISCORD_TOKEN

SEEDS_DIR = Path(__file__).parent / "discord_seeds"

# Snowflakes are (ms since 2015-01-01) << 22 plus worker/sequence bits. SDKs
# decode the timestamp back out (discord.py dates every object that way), so
# generated ids have to keep the layout, not just be numeric.
DISCORD_EPOCH_MS = 1420070400000
# The bot's own user id, which is also its application id — as for real bots.
BOT_USER_ID = "1196242344345600000"
BOT_PERMISSIONS = "8"  # Administrator: the sandbox bot is never the bottleneck
EVERYONE_PERMISSIONS = "1071698660929"

TEXT_CHANNEL, DM_CHANNEL, VOICE_CHANNEL, CATEGORY_CHANNEL = 0, 1, 2, 4
ANNOUNCEMENT_CHANNEL, FORUM_CHANNEL = 5, 15
THREAD_TYPES = (10, 11, 12)  # announcement, public and private threads


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _snowflake_time(snowflake: str) -> str:
    """The creation timestamp encoded in a snowflake, as Discord reports it."""
    value = _as_snowflake(snowflake)
    if not value:
        return _now()
    return datetime.fromtimestamp(((value >> 22) + DISCORD_EPOCH_MS) / 1000, UTC).isoformat()


def _as_snowflake(value: Any) -> int:
    """Snowflakes travel as strings but order numerically; 0 for anything else."""
    text = str(value or "")
    return int(text) if text.isdigit() else 0


def _snowflake() -> str:
    """The next id: Discord's layout, monotonic, always after seeded ids."""
    counters = STATE.setdefault("_counters", {})
    counters["snowflake_seq"] = seq = int(counters.get("snowflake_seq") or 0) + 1
    value = ((int(time.time() * 1000) - DISCORD_EPOCH_MS) << 22) | (seq & 0x3FFFFF)
    value = max(value, int(counters.get("last_snowflake") or 0) + 1)
    counters["last_snowflake"] = str(value)
    return str(value)


def _fresh_state() -> dict:
    return {
        "guilds": {},      # guild_id -> guild dict
        "channels": {},    # channel_id -> channel dict (guild channels, threads, DMs)
        "messages": {},    # channel_id -> [message dict]
        "members": {},     # guild_id -> {user_id -> member dict}
        "roles": {},       # guild_id -> {role_id -> role dict}
        "bans": {},        # guild_id -> {user_id -> ban dict}
        "webhooks": {},    # webhook_id -> webhook dict
        "users": {},       # user_id -> user dict
        "commands": {},    # command_id -> application command dict
        # Uploaded file bytes, served back from the CDN path attachments point at.
        "_attachments": {},
        "_counters": {"snowflake_seq": 0, "last_snowflake": ""},
        "_config": {
            "rate_limit": None,
            "bot_user_id": BOT_USER_ID,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- error envelopes ---------------------------------------------------------

def discord_error(status: int, code: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message, **extra})


def _unknown_channel() -> JSONResponse:
    return discord_error(404, 10003, "Unknown Channel")


def _unknown_guild() -> JSONResponse:
    return discord_error(404, 10004, "Unknown Guild")


def _unknown_message() -> JSONResponse:
    return discord_error(404, 10008, "Unknown Message")


def _unknown_member() -> JSONResponse:
    return discord_error(404, 10007, "Unknown Member")


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """Unrouted paths answer like Discord ({code, message}), not like FastAPI."""
    detail = exc.detail if isinstance(exc.detail, str) else "Error"
    return discord_error(exc.status_code, 0, f"{exc.status_code}: {detail}")


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    return discord_error(400, 50035, "Invalid Form Body")


# --- users, members, roles ---------------------------------------------------

def _bot_user_id(state: dict | None = None) -> str:
    config = (state or STATE).get("_config") or {}
    return str(config.get("bot_user_id") or BOT_USER_ID)


def _build_bot_user(state: dict | None = None) -> dict:
    return {
        "id": _bot_user_id(state),
        "username": "checkpoint-bot",
        "discriminator": "0000",
        "global_name": "Checkpoint",
        "avatar": None,
        "bot": True,
        "system": False,
        "public_flags": 0,
    }


def _user(user_id: str) -> dict:
    """A full user object; unknown ids get a placeholder, as ids always resolve."""
    stored = (STATE.get("users") or {}).get(user_id)
    if user_id == _bot_user_id():
        return {**_build_bot_user(), **(stored or {})}
    base = {"id": user_id, "username": f"user-{user_id}", "discriminator": "0",
            "global_name": None, "avatar": None, "bot": False}
    return {**base, **(stored or {})}


def _member(guild_id: str, user_id: str) -> dict | None:
    stored = (STATE["members"].get(guild_id) or {}).get(user_id)
    if stored is None:
        return None
    return _render_member(user_id, stored)


def _render_member(user_id: str, member: dict) -> dict:
    """Fill the fields discord.py reads unconditionally (``user``, ``roles``, ``flags``)."""
    return {
        "avatar": None,
        "nick": None,
        "premium_since": None,
        "deaf": False,
        "mute": False,
        "pending": False,
        "communication_disabled_until": None,
        **{k: v for k, v in member.items() if not k.startswith("_")},
        "user": {**_user(user_id), **(member.get("user") or {})},
        "roles": [str(r) for r in member.get("roles", [])],
        "joined_at": member.get("joined_at") or _snowflake_time(user_id),
        "flags": int(member.get("flags") or 0),
    }


def _everyone_role(guild_id: str) -> dict:
    """Every guild has an @everyone role whose id is the guild id."""
    return {
        "id": guild_id,
        "name": "@everyone",
        "description": None,
        "color": 0,
        "colors": {"primary_color": 0, "secondary_color": None, "tertiary_color": None},
        "hoist": False,
        "icon": None,
        "unicode_emoji": None,
        "position": 0,
        "permissions": EVERYONE_PERMISSIONS,
        "managed": False,
        "mentionable": False,
        "flags": 0,
    }


# --- payload rendering -------------------------------------------------------

_GUILD_DEFAULTS: dict[str, Any] = {
    "icon": None,
    "splash": None,
    "discovery_splash": None,
    "banner": None,
    "description": None,
    "features": [],
    "emojis": [],
    "stickers": [],
    "verification_level": 0,
    "default_message_notifications": 0,
    "explicit_content_filter": 0,
    "mfa_level": 0,
    "nsfw_level": 0,
    "premium_tier": 0,
    "premium_subscription_count": 0,
    "preferred_locale": "en-US",
    "afk_channel_id": None,
    "afk_timeout": 300,
    "system_channel_id": None,
    "system_channel_flags": 0,
    "rules_channel_id": None,
    "public_updates_channel_id": None,
    "vanity_url_code": None,
}


def _render_guild(guild: dict, *, with_counts: bool = False) -> dict:
    guild_id = guild["id"]
    out = {**_GUILD_DEFAULTS, **{k: v for k, v in guild.items() if not k.startswith("_")}}
    out["roles"] = list((STATE["roles"].get(guild_id) or {}).values())
    if with_counts:
        members = STATE["members"].get(guild_id) or {}
        out["approximate_member_count"] = len(members)
        out["approximate_presence_count"] = sum(
            1 for m in members.values() if not (m.get("user") or {}).get("bot")
        )
    return out


def _partial_guild(guild: dict) -> dict:
    """The shape ``GET /users/@me/guilds`` returns: no roles, plus permissions."""
    members = STATE["members"].get(guild["id"]) or {}
    return {
        "id": guild["id"],
        "name": guild.get("name", ""),
        "icon": guild.get("icon"),
        "banner": guild.get("banner"),
        "owner": guild.get("owner_id") == _bot_user_id(),
        "permissions": BOT_PERMISSIONS,
        "features": guild.get("features", []),
        "approximate_member_count": len(members),
        "approximate_presence_count": len(members),
    }


def _render_channel(channel: dict) -> dict:
    out = {k: v for k, v in channel.items() if not k.startswith("_")}
    if channel.get("type") in THREAD_TYPES:
        count = len(STATE["messages"].get(channel["id"]) or [])
        out["message_count"] = count
        out["total_message_sent"] = count
    return out


def _render_message(msg: dict) -> dict:
    out = {k: v for k, v in msg.items() if not k.startswith("_")}
    ref = msg.get("message_reference")
    if ref:
        # The real API resolves the reply target inline, and null means it is gone.
        target = _find_message(ref.get("channel_id"), ref.get("message_id"))
        out["referenced_message"] = (
            {k: v for k, v in target.items() if not k.startswith("_") and k != "referenced_message"}
            if target else None
        )
    # A thread started from a message shares its id, and rides along on the message.
    thread = STATE["channels"].get(msg["id"])
    if thread and thread.get("type") in THREAD_TYPES:
        out["thread"] = _render_channel(thread)
    return out


# --- lookups -----------------------------------------------------------------

def _channel(channel_id: str) -> dict | None:
    return STATE["channels"].get(channel_id)


def _messages(channel_id: str) -> list[dict]:
    return STATE["messages"].setdefault(channel_id, [])


def _find_message(channel_id: Any, message_id: Any) -> dict | None:
    for msg in STATE["messages"].get(str(channel_id or ""), []):
        if msg["id"] == str(message_id or ""):
            return msg
    return None


def _resolve_user_id(user_id: str) -> str:
    return _bot_user_id() if user_id == "@me" else user_id


# --- runtime: auth, faults, trace, control plane ----------------------------

def _bootstrap_token() -> str:
    return os.environ.get("DISCORD_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    """The bare token: agents, SDKs and the sandbox proxy all prefix differently.

    discord.py prepends ``Bot`` itself, the proxy rewrites to ``Bearer``, so a
    header can arrive as ``Bot <token>``, ``Bearer Bot <token>`` or bare.
    """
    if not auth_header:
        return None
    token = auth_header.strip()
    while True:
        scheme, _, rest = token.partition(" ")
        if scheme in ("Bot", "Bearer") and rest.strip():
            token = rest.strip()
            continue
        return token or None


# Webhook URLs carry their own credential and CDN links carry none, so neither
# route may demand a bot token — that is the whole point of a webhook URL.
_NO_AUTH = re.compile(r"^(?:/api(?:/v\d+)?)?/webhooks/[^/]+/[^/]+|^/attachments/")


def _authenticate(request: Request) -> Response | None:
    if _NO_AUTH.match(request.url.path):
        return None
    token = _extract_token(request.headers.get("authorization"))
    if not token:
        return discord_error(401, 0, "401: Unauthorized")
    if TWIN.config.get("strict_auth") and token != _extract_token(_bootstrap_token()):
        return discord_error(401, 0, "401: Unauthorized")
    return None


def _error(kind: str, status: int, message: str) -> Response:
    if kind == "rate_limited":
        return JSONResponse(
            status_code=429,
            content={"message": "You are being rate limited.", "retry_after": 1.0, "global": False},
            headers={
                "Retry-After": "1",
                "X-RateLimit-Limit": "1",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": f"{time.time() + 1:.3f}",
                "X-RateLimit-Reset-After": "1",
                "X-RateLimit-Bucket": "checkpoint",
                "X-RateLimit-Scope": "user",
                # discord.py treats a 429 without Via as a Cloudflare ban and
                # refuses to retry; real 429s come through Google's edge.
                "Via": "1.1 google",
            },
        )
    if kind in ("forbidden", "read_only"):
        return discord_error(403, 50013, "Missing Permissions")
    if kind == "unauthorized":
        return discord_error(401, 0, "401: Unauthorized")
    return discord_error(status, 0, f"{status}: {message}")


_OPS: dict[str, kit.Op] = {"POST": "create", "PUT": "create", "PATCH": "update",
                           "DELETE": "delete"}


def _op(method: str) -> kit.Op:
    return _OPS.get(method, "read")


def _classify(method: str, path: str, body: Any) -> tuple[kit.Op, str] | None:
    """Map Discord's routes onto (op, resource); several lie about both."""
    method = method.upper()
    if path.endswith("/messages/bulk-delete"):
        return "delete", "messages"  # a POST that deletes
    if "/reactions" in path:
        return _op(method), "reactions"
    if "/pins" in path:
        return _op(method), "pins"
    if re.search(r"/members/[^/]+/roles/[^/]+$", path):
        return "update", "members"  # a role assignment changes the member
    if path.endswith("/members/search"):
        return "read", "members"
    if method == "POST" and re.search(r"/(?:messages/[^/]+)?/?threads$", path):
        return "create", "threads"
    if "/thread-members" in path:
        return _op(method), "thread_members"
    if path.endswith("/typing"):
        return "other", "typing"
    if re.search(r"/webhooks/[^/]+/[^/]+", path) and ("/messages" in path or method == "POST"):
        # Executing a webhook posts a message; editing one edits that message.
        return _op(method), "messages"
    if "/bans" in path or path.endswith("/bulk-ban"):
        return _op(method), "bans"
    return None


def _after_seed(state: dict) -> None:
    """Make a seed self-consistent: the bot is a member, ids stay monotonic."""
    bot = _build_bot_user(state)
    state.setdefault("users", {}).setdefault(bot["id"], bot)
    for guild_id in list(state.get("guilds") or {}):
        roles = state.setdefault("roles", {}).setdefault(guild_id, {})
        roles.setdefault(guild_id, _everyone_role(guild_id))
        members = state.setdefault("members", {}).setdefault(guild_id, {})
        members.setdefault(bot["id"], {
            "user": bot, "nick": None, "roles": [], "joined_at": _snowflake_time(bot["id"]),
            "deaf": False, "mute": False, "flags": 0,
        })
    for channel_id in (state.get("channels") or {}):
        state.setdefault("messages", {}).setdefault(channel_id, [])
    highest = 0
    for collection in ("guilds", "channels", "users", "webhooks", "commands"):
        for key in (state.get(collection) or {}):
            highest = max(highest, _as_snowflake(key))
    for per_guild in (state.get("roles") or {}).values():
        for key in per_guild:
            highest = max(highest, _as_snowflake(key))
    for msgs in (state.get("messages") or {}).values():
        for msg in msgs or []:
            highest = max(highest, _as_snowflake(msg.get("id")))
    counters = state.setdefault("_counters", {"snowflake_seq": 0})
    counters["last_snowflake"] = str(max(highest, _as_snowflake(counters.get("last_snowflake"))))


_CHANNEL_KINDS = {
    TEXT_CHANNEL: "text", DM_CHANNEL: "dm", VOICE_CHANNEL: "voice", 3: "group_dm",
    CATEGORY_CHANNEL: "category", ANNOUNCEMENT_CHANNEL: "announcement", 13: "stage",
    FORUM_CHANNEL: "forum", 10: "thread", 11: "thread", 12: "thread",
}


def _views(state: dict) -> dict[str, kit.View]:
    """Flat, denormalized collections: what a scenario asserts against."""
    guilds = state.get("guilds") or {}
    channels = state.get("channels") or {}
    roles = state.get("roles") or {}
    guild_name = {gid: g.get("name", "") for gid, g in guilds.items()}
    channel_name = {cid: c.get("name") or "direct message" for cid, c in channels.items()}
    role_name = {rid: r.get("name", "") for per in roles.values() for rid, r in per.items()}
    user_name = {uid: u.get("username", "") for uid, u in (state.get("users") or {}).items()}

    def _author(msg: dict) -> dict:
        author = msg.get("author") or {}
        return {"author_id": author.get("id", ""),
                "author": author.get("username") or user_name.get(author.get("id", ""), ""),
                "author_is_bot": bool(author.get("bot"))}

    channel_items, thread_items = [], []
    for cid, channel in channels.items():
        guild_id = channel.get("guild_id") or ""
        record = {
            "id": cid,
            "name": channel.get("name"),
            "kind": _CHANNEL_KINDS.get(channel.get("type"), "text"),
            "type": channel.get("type"),
            "guild_id": guild_id,
            "guild": guild_name.get(guild_id, ""),
            "topic": channel.get("topic"),
            "nsfw": bool(channel.get("nsfw")),
            "parent_id": channel.get("parent_id"),
            "parent": channel_name.get(channel.get("parent_id") or "", ""),
            "message_count": len((state.get("messages") or {}).get(cid) or []),
        }
        if channel.get("type") in THREAD_TYPES:
            metadata = channel.get("thread_metadata") or {}
            thread_items.append({**record, "archived": bool(metadata.get("archived")),
                                 "locked": bool(metadata.get("locked")),
                                 "owner_id": channel.get("owner_id")})
        else:
            channel_items.append(record)

    message_items = []
    for cid, msgs in (state.get("messages") or {}).items():
        for msg in msgs or []:
            reference = msg.get("message_reference") or {}
            message_items.append({
                "id": msg.get("id"),
                "channel_id": cid,
                "channel": channel_name.get(cid, ""),
                "guild_id": (channels.get(cid) or {}).get("guild_id", ""),
                "guild": guild_name.get((channels.get(cid) or {}).get("guild_id", ""), ""),
                **_author(msg),
                "content": msg.get("content", ""),
                "pinned": bool(msg.get("pinned")),
                "edited": bool(msg.get("edited_timestamp")),
                "reply_to": reference.get("message_id"),
                "reactions": [(r.get("emoji") or {}).get("name")
                              for r in msg.get("reactions") or []],
                "attachments": [a.get("filename") for a in msg.get("attachments") or []],
                "embed_titles": [e.get("title") for e in msg.get("embeds") or []],
                "via_webhook": msg.get("webhook_id"),
                "timestamp": msg.get("timestamp"),
            })

    member_items = []
    for guild_id, members in (state.get("members") or {}).items():
        for uid, member in members.items():
            user = member.get("user") or {}
            member_items.append({
                "id": f"{guild_id}:{uid}",
                "guild_id": guild_id,
                "guild": guild_name.get(guild_id, ""),
                "user_id": uid,
                "username": user.get("username") or user_name.get(uid, ""),
                "display_name": member.get("nick") or user.get("global_name")
                or user.get("username") or user_name.get(uid, ""),
                "nick": member.get("nick"),
                "roles": [role_name.get(str(r), str(r)) for r in member.get("roles") or []],
                "role_ids": [str(r) for r in member.get("roles") or []],
                "joined_at": member.get("joined_at"),
                "timed_out_until": member.get("communication_disabled_until"),
                "bot": bool(user.get("bot")),
            })

    role_items = []
    for guild_id, per_guild in roles.items():
        members = (state.get("members") or {}).get(guild_id) or {}
        for rid, role in per_guild.items():
            role_items.append({
                "id": rid,
                "guild_id": guild_id,
                "guild": guild_name.get(guild_id, ""),
                "name": role.get("name"),
                "color": role.get("color", 0),
                "position": role.get("position", 0),
                "permissions": role.get("permissions", "0"),
                "mentionable": bool(role.get("mentionable")),
                "member_count": sum(1 for m in members.values()
                                    if str(rid) in [str(r) for r in m.get("roles") or []]),
            })

    ban_items = [
        {"id": f"{guild_id}:{uid}", "guild_id": guild_id, "guild": guild_name.get(guild_id, ""),
         "user_id": uid, "username": (ban.get("user") or {}).get("username", ""),
         "reason": ban.get("reason")}
        for guild_id, bans in (state.get("bans") or {}).items() for uid, ban in bans.items()
    ]

    webhook_items = [
        {"id": wid, "name": hook.get("name"), "channel_id": hook.get("channel_id"),
         "channel": channel_name.get(hook.get("channel_id") or "", ""),
         "guild_id": hook.get("guild_id"), "guild": guild_name.get(hook.get("guild_id") or "", ""),
         "url": hook.get("url")}
        for wid, hook in (state.get("webhooks") or {}).items()
    ]

    guild_items = [
        {"id": gid, "name": guild.get("name"), "description": guild.get("description"),
         "owner_id": guild.get("owner_id"),
         "member_count": len((state.get("members") or {}).get(gid) or {}),
         "channel_count": sum(1 for c in channels.values() if c.get("guild_id") == gid)}
        for gid, guild in guilds.items()
    ]

    user_items = [
        {"id": uid, "username": user.get("username"), "global_name": user.get("global_name"),
         "bot": bool(user.get("bot"))}
        for uid, user in (state.get("users") or {}).items()
    ]

    command_items = [
        {"id": cid, "name": command.get("name"), "description": command.get("description"),
         "guild_id": command.get("guild_id"),
         "guild": guild_name.get(command.get("guild_id") or "", "")}
        for cid, command in (state.get("commands") or {}).items()
    ]

    return {
        "guilds": kit.View(guild_items, nouns=("guild", "guilds", "server", "servers")),
        "channels": kit.View(channel_items, nouns=("channel", "channels")),
        "threads": kit.View(thread_items, nouns=("thread", "threads")),
        "messages": kit.View(message_items, nouns=("message", "messages")),
        "members": kit.View(member_items, nouns=("member", "members")),
        "roles": kit.View(role_items, nouns=("role", "roles")),
        "bans": kit.View(ban_items, nouns=("ban", "bans")),
        "webhooks": kit.View(webhook_items, nouns=("webhook", "webhooks")),
        "users": kit.View(user_items, nouns=("user", "users")),
        "commands": kit.View(command_items, nouns=("slash command", "slash commands")),
    }


TWIN = kit.install(app, kit.Twin(
    name="discord",
    state=STATE,
    trace=TRACE,
    fresh_state=_fresh_state,
    seeds_dir=SEEDS_DIR,
    error=_error,
    authenticate=_authenticate,
    after_seed=_after_seed,
    views=_views,
    classify=_classify,
    knobs={"bot_user_id": BOT_USER_ID},
))


# --- gateway, application, current user --------------------------------------

@api.get("/gateway")
def gateway():
    return {"url": "wss://gateway.discord.gg"}


@api.get("/gateway/bot")
def gateway_bot():
    return {
        "url": "wss://gateway.discord.gg",
        "shards": 1,
        "session_start_limit": {"total": 1000, "remaining": 999, "reset_after": 86400,
                                "max_concurrency": 1},
    }


def _application() -> dict:
    bot = _build_bot_user()
    return {
        "id": bot["id"],
        "name": bot["username"],
        "icon": None,
        "description": "Checkpoint twin application",
        "bot_public": False,
        "bot_require_code_grant": False,
        "verify_key": "0" * 64,
        "owner": bot,
        "team": None,
        "flags": 0,
        "tags": [],
        "install_params": {"scopes": ["bot"], "permissions": BOT_PERMISSIONS},
        "integration_types_config": {},
        "approximate_guild_count": len(STATE["guilds"]),
        "bot": bot,
    }


@api.get("/oauth2/applications/@me")
def oauth2_application():
    """discord.py's ``login()`` calls this right after ``/users/@me``."""
    return _application()


@api.get("/applications/@me")
def current_application():
    return _application()


@api.get("/users/@me")
def get_current_user():
    return _build_bot_user()


@api.patch("/users/@me")
async def modify_current_user(request: Request):
    body = await _json_body(request)
    bot = STATE["users"].setdefault(_bot_user_id(), _build_bot_user())
    for field in ("username", "avatar", "banner"):
        if field in body:
            bot[field] = body[field]
    return {**_build_bot_user(), **bot}


@api.get("/users/@me/guilds")
async def get_current_user_guilds(request: Request):
    """The guilds the bot is in — in a sandbox, every guild it knows about."""
    params = request.query_params
    guilds = sorted(STATE["guilds"].values(), key=lambda g: _as_snowflake(g["id"]))
    if params.get("after"):
        guilds = [g for g in guilds if _as_snowflake(g["id"]) > _as_snowflake(params["after"])]
    if params.get("before"):
        guilds = [g for g in guilds if _as_snowflake(g["id"]) < _as_snowflake(params["before"])]
    limit = _clamp(params.get("limit"), default=200, high=200)
    return [_partial_guild(g) for g in guilds[:limit]]


@api.post("/users/@me/channels")
async def create_dm(request: Request):
    body = await _json_body(request)
    recipient_id = str(body.get("recipient_id") or "")
    if not recipient_id:
        return discord_error(400, 50035, "Invalid Form Body")
    existing = next(
        (c for c in STATE["channels"].values()
         if c.get("type") == DM_CHANNEL
         and recipient_id in [r["id"] for r in c.get("recipients", [])]),
        None,
    )
    if existing:
        return existing
    channel = {
        "id": _snowflake(),
        "type": DM_CHANNEL,
        "last_message_id": None,
        "recipients": [_user(recipient_id)],
        "flags": 0,
    }
    STATE["channels"][channel["id"]] = channel
    STATE["messages"][channel["id"]] = []
    return channel


@api.get("/users/{user_id}")
def get_user(user_id: str):
    if user_id in ("@me", _bot_user_id()):
        return _build_bot_user()
    if user_id not in (STATE.get("users") or {}):
        return discord_error(404, 10013, "Unknown User")
    return _user(user_id)


# --- guilds ------------------------------------------------------------------

@api.post("/guilds", status_code=201)
async def create_guild(request: Request):
    """Bots in fewer than ten guilds may create one; a twin bot always is."""
    body = await _json_body(request)
    if not body.get("name"):
        return discord_error(400, 50035, "Invalid Form Body")
    bot = _build_bot_user()
    guild_id = _snowflake()
    guild = {**_GUILD_DEFAULTS, "id": guild_id, "name": body["name"],
             "icon": body.get("icon"), "owner_id": bot["id"]}
    STATE["guilds"][guild_id] = guild
    STATE["roles"][guild_id] = {guild_id: _everyone_role(guild_id)}
    STATE["members"][guild_id] = {bot["id"]: {
        "user": bot, "nick": None, "roles": [], "joined_at": _now(),
        "deaf": False, "mute": False, "flags": 0,
    }}
    STATE["users"].setdefault(bot["id"], bot)
    for entry in body.get("channels") or [{"name": "general", "type": TEXT_CHANNEL}]:
        if entry.get("name"):
            channel = _new_channel(guild_id, entry)
            STATE["channels"][channel["id"]] = channel
            STATE["messages"][channel["id"]] = []
    return _render_guild(guild)


@api.get("/guilds/{guild_id}")
async def get_guild(guild_id: str, request: Request):
    guild = STATE["guilds"].get(guild_id)
    if not guild:
        return _unknown_guild()
    with_counts = request.query_params.get("with_counts") in ("1", "true", "True")
    return _render_guild(guild, with_counts=with_counts)


@api.patch("/guilds/{guild_id}")
async def modify_guild(guild_id: str, request: Request):
    guild = STATE["guilds"].get(guild_id)
    if not guild:
        return _unknown_guild()
    body = await _json_body(request)
    for field in ("name", "description", "icon", "banner", "verification_level",
                  "explicit_content_filter", "default_message_notifications",
                  "afk_channel_id", "afk_timeout", "system_channel_id", "preferred_locale"):
        if field in body:
            guild[field] = body[field]
    return _render_guild(guild)


@api.delete("/guilds/{guild_id}", status_code=204)
def delete_guild(guild_id: str):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    for channel_id, channel in list(STATE["channels"].items()):
        if channel.get("guild_id") == guild_id:
            STATE["channels"].pop(channel_id, None)
            STATE["messages"].pop(channel_id, None)
    for webhook_id, hook in list(STATE["webhooks"].items()):
        if hook.get("guild_id") == guild_id:
            del STATE["webhooks"][webhook_id]
    for collection in ("roles", "members", "bans"):
        STATE[collection].pop(guild_id, None)
    del STATE["guilds"][guild_id]
    return Response(status_code=204)


@api.get("/guilds/{guild_id}/channels")
def list_guild_channels(guild_id: str):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    channels = [c for c in STATE["channels"].values()
                if c.get("guild_id") == guild_id and c.get("type") not in THREAD_TYPES]
    return [_render_channel(c) for c in sorted(channels, key=lambda c: c.get("position", 0))]


@api.post("/guilds/{guild_id}/channels", status_code=201)
async def create_channel(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    body = await _json_body(request)
    name = body.get("name")
    if not name:
        return discord_error(400, 50035, "Invalid Form Body")
    channel = _new_channel(guild_id, body)
    STATE["channels"][channel["id"]] = channel
    STATE["messages"][channel["id"]] = []
    return channel


def _new_channel(guild_id: str, body: dict) -> dict:
    channel_type = int(body.get("type") or TEXT_CHANNEL)
    channel: dict[str, Any] = {
        "id": _snowflake(),
        "type": channel_type,
        "guild_id": guild_id,
        "name": body["name"],
        "position": int(body.get("position") or len(
            [c for c in STATE["channels"].values() if c.get("guild_id") == guild_id])),
        "topic": body.get("topic"),
        "nsfw": bool(body.get("nsfw", False)),
        "parent_id": body.get("parent_id"),
        "permission_overwrites": body.get("permission_overwrites") or [],
        "rate_limit_per_user": int(body.get("rate_limit_per_user") or 0),
        "last_message_id": None,
        "flags": 0,
    }
    if channel_type == VOICE_CHANNEL:
        channel["bitrate"] = int(body.get("bitrate") or 64000)
        channel["user_limit"] = int(body.get("user_limit") or 0)
    if channel_type == FORUM_CHANNEL:
        channel["available_tags"] = body.get("available_tags") or []
        channel["default_reaction_emoji"] = body.get("default_reaction_emoji")
    return channel


@api.patch("/guilds/{guild_id}/channels", status_code=204)
async def modify_channel_positions(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    for entry in await _json_body(request) or []:
        channel = STATE["channels"].get(str(entry.get("id")))
        if channel and entry.get("position") is not None:
            channel["position"] = int(entry["position"])
    return Response(status_code=204)


@api.get("/guilds/{guild_id}/threads/active")
def list_active_threads(guild_id: str):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    threads = [_render_channel(c) for c in STATE["channels"].values()
               if c.get("guild_id") == guild_id and c.get("type") in THREAD_TYPES
               and not (c.get("thread_metadata") or {}).get("archived")]
    return {"threads": threads, "members": [], "has_more": False}


# --- guild members -----------------------------------------------------------

@api.get("/guilds/{guild_id}/members/search")
async def search_guild_members(guild_id: str, request: Request):
    """Registered before ``/members/{user_id}`` so "search" is not read as an id."""
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    query = (request.query_params.get("query") or "").lower()
    limit = _clamp(request.query_params.get("limit"), default=1, low=1, high=1000)
    matches = []
    for user_id, member in (STATE["members"].get(guild_id) or {}).items():
        rendered = _render_member(user_id, member)
        haystack = " ".join(str(v or "") for v in (
            rendered["user"].get("username"), rendered["user"].get("global_name"),
            rendered.get("nick"))).lower()
        if query in haystack:
            matches.append(rendered)
    return matches[:limit]


@api.get("/guilds/{guild_id}/members")
async def list_members(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    params = request.query_params
    limit = _clamp(params.get("limit"), default=1, low=1, high=1000)
    members = sorted((STATE["members"].get(guild_id) or {}).items(),
                     key=lambda item: _as_snowflake(item[0]))
    if params.get("after"):
        after = _as_snowflake(params["after"])
        members = [item for item in members if _as_snowflake(item[0]) > after]
    return [_render_member(uid, member) for uid, member in members[:limit]]


@api.get("/guilds/{guild_id}/members/{user_id}")
def get_member(guild_id: str, user_id: str):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    member = _member(guild_id, _resolve_user_id(user_id))
    if not member:
        return _unknown_member()
    return member


@api.patch("/guilds/{guild_id}/members/{user_id}")
async def modify_member(guild_id: str, user_id: str, request: Request):
    user_id = _resolve_user_id(user_id)
    members = STATE["members"].get(guild_id) or {}
    member = members.get(user_id)
    if not member:
        return _unknown_member()
    body = await _json_body(request)
    for field in ("nick", "mute", "deaf", "communication_disabled_until", "channel_id", "flags"):
        if field in body:
            member[field] = body[field]
    if "roles" in body:
        member["roles"] = [str(r) for r in body["roles"] or []]
    return _render_member(user_id, member)


@api.delete("/guilds/{guild_id}/members/{user_id}", status_code=204)
def kick_member(guild_id: str, user_id: str):
    members = STATE["members"].get(guild_id) or {}
    if _resolve_user_id(user_id) not in members:
        return _unknown_member()
    del members[_resolve_user_id(user_id)]
    return Response(status_code=204)


@api.put("/guilds/{guild_id}/members/{user_id}/roles/{role_id}", status_code=204)
def assign_role(guild_id: str, user_id: str, role_id: str):
    member = (STATE["members"].get(guild_id) or {}).get(_resolve_user_id(user_id))
    if not member:
        return _unknown_member()
    if role_id not in (STATE["roles"].get(guild_id) or {}):
        return discord_error(404, 10011, "Unknown Role")
    if role_id not in member.get("roles", []):
        member.setdefault("roles", []).append(role_id)
    return Response(status_code=204)


@api.delete("/guilds/{guild_id}/members/{user_id}/roles/{role_id}", status_code=204)
def remove_role_from_member(guild_id: str, user_id: str, role_id: str):
    member = (STATE["members"].get(guild_id) or {}).get(_resolve_user_id(user_id))
    if not member:
        return _unknown_member()
    member["roles"] = [r for r in member.get("roles", []) if str(r) != role_id]
    return Response(status_code=204)


# --- bans --------------------------------------------------------------------

@api.get("/guilds/{guild_id}/bans")
async def list_bans(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    bans = sorted((STATE["bans"].get(guild_id) or {}).items(),
                  key=lambda item: _as_snowflake(item[0]))
    params = request.query_params
    if params.get("after"):
        bans = [b for b in bans if _as_snowflake(b[0]) > _as_snowflake(params["after"])]
    if params.get("before"):
        bans = [b for b in bans if _as_snowflake(b[0]) < _as_snowflake(params["before"])]
    limit = _clamp(params.get("limit"), default=1000, low=1, high=1000)
    return [ban for _, ban in bans[:limit]]


@api.get("/guilds/{guild_id}/bans/{user_id}")
def get_ban(guild_id: str, user_id: str):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    ban = (STATE["bans"].get(guild_id) or {}).get(user_id)
    if not ban:
        return discord_error(404, 10026, "Unknown Ban")
    return ban


@api.put("/guilds/{guild_id}/bans/{user_id}", status_code=204)
async def create_ban(guild_id: str, user_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    body = await _json_body(request)
    seconds = int(body.get("delete_message_seconds")
                  or request.query_params.get("delete_message_seconds") or 0)
    STATE["bans"].setdefault(guild_id, {})[user_id] = {
        "user": _user(user_id),
        "reason": request.headers.get("x-audit-log-reason") or body.get("reason"),
    }
    STATE["members"].get(guild_id, {}).pop(user_id, None)
    if seconds:
        cutoff = (time.time() - seconds) * 1000
        for channel_id, msgs in STATE["messages"].items():
            if (_channel(channel_id) or {}).get("guild_id") != guild_id:
                continue
            STATE["messages"][channel_id] = [
                m for m in msgs
                if (m.get("author") or {}).get("id") != user_id
                or ((_as_snowflake(m["id"]) >> 22) + DISCORD_EPOCH_MS) < cutoff
            ]
    return Response(status_code=204)


@api.delete("/guilds/{guild_id}/bans/{user_id}", status_code=204)
def remove_ban(guild_id: str, user_id: str):
    bans = STATE["bans"].get(guild_id) or {}
    if user_id not in bans:
        return discord_error(404, 10026, "Unknown Ban")
    del bans[user_id]
    return Response(status_code=204)


@api.post("/guilds/{guild_id}/bulk-ban")
async def bulk_ban(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    body = await _json_body(request)
    banned, failed = [], []
    for user_id in [str(u) for u in body.get("user_ids") or []]:
        if user_id in (STATE["bans"].get(guild_id) or {}):
            failed.append(user_id)
            continue
        STATE["bans"].setdefault(guild_id, {})[user_id] = {
            "user": _user(user_id),
            "reason": request.headers.get("x-audit-log-reason"),
        }
        STATE["members"].get(guild_id, {}).pop(user_id, None)
        banned.append(user_id)
    return {"banned_users": banned, "failed_users": failed}


# --- roles -------------------------------------------------------------------

def _role_colors(body: dict) -> dict:
    """Roles carry a ``colors`` object now; SDKs send that and read it back."""
    colors = body.get("colors") or {}
    primary = colors.get("primary_color") if "primary_color" in colors else body.get("color")
    return {
        "primary_color": int(primary or 0),
        "secondary_color": colors.get("secondary_color"),
        "tertiary_color": colors.get("tertiary_color"),
    }


@api.get("/guilds/{guild_id}/roles")
def list_roles(guild_id: str):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    return list((STATE["roles"].get(guild_id) or {}).values())


@api.get("/guilds/{guild_id}/roles/{role_id}")
def get_role(guild_id: str, role_id: str):
    role = (STATE["roles"].get(guild_id) or {}).get(role_id)
    if not role:
        return discord_error(404, 10011, "Unknown Role")
    return role


@api.post("/guilds/{guild_id}/roles")
async def create_role(guild_id: str, request: Request):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    body = await _json_body(request)
    roles = STATE["roles"].setdefault(guild_id, {})
    colors = _role_colors(body)
    role: dict[str, Any] = {
        "id": _snowflake(),
        "name": body.get("name") or "new role",
        "description": body.get("description"),
        "color": colors["primary_color"],
        "colors": colors,
        "hoist": bool(body.get("hoist", False)),
        "icon": None,
        "unicode_emoji": body.get("unicode_emoji"),
        "permissions": str(body.get("permissions") or 0),
        "mentionable": bool(body.get("mentionable", False)),
        "position": int(body.get("position") or max(
            [r.get("position", 0) for r in roles.values()] or [0]) + 1),
        "managed": False,
        "flags": 0,
    }
    roles[role["id"]] = role
    return role


@api.patch("/guilds/{guild_id}/roles/{role_id}")
async def modify_role(guild_id: str, role_id: str, request: Request):
    role = (STATE["roles"].get(guild_id) or {}).get(role_id)
    if not role:
        return discord_error(404, 10011, "Unknown Role")
    body = await _json_body(request)
    for field in ("name", "hoist", "mentionable", "position", "unicode_emoji", "description"):
        if field in body:
            role[field] = body[field]
    if "permissions" in body:
        role["permissions"] = str(body["permissions"])
    if "color" in body or "colors" in body:
        role["colors"] = _role_colors(body)
        role["color"] = role["colors"]["primary_color"]
    return role


@api.delete("/guilds/{guild_id}/roles/{role_id}", status_code=204)
def delete_role(guild_id: str, role_id: str):
    roles = STATE["roles"].get(guild_id) or {}
    if role_id not in roles:
        return discord_error(404, 10011, "Unknown Role")
    del roles[role_id]
    for member in (STATE["members"].get(guild_id) or {}).values():
        member["roles"] = [r for r in member.get("roles", []) if str(r) != role_id]
    return Response(status_code=204)


# --- channels ----------------------------------------------------------------

@api.get("/channels/{channel_id}")
def get_channel(channel_id: str):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    return _render_channel(channel)


@api.patch("/channels/{channel_id}")
async def modify_channel(channel_id: str, request: Request):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    body = await _json_body(request)
    for field in ("name", "topic", "nsfw", "position", "parent_id", "rate_limit_per_user",
                  "bitrate", "user_limit", "permission_overwrites", "type", "flags"):
        if field in body:
            channel[field] = body[field]
    metadata = channel.get("thread_metadata")
    if metadata is not None:
        for field in ("archived", "locked", "invitable", "auto_archive_duration"):
            if field in body:
                metadata[field] = body[field]
        if "archived" in body:
            metadata["archive_timestamp"] = _now()
    return _render_channel(channel)


@api.delete("/channels/{channel_id}")
def delete_channel(channel_id: str):
    channel = STATE["channels"].pop(channel_id, None)
    if not channel:
        return _unknown_channel()
    STATE["messages"].pop(channel_id, None)
    for thread_id, thread in list(STATE["channels"].items()):
        if thread.get("parent_id") == channel_id and thread.get("type") in THREAD_TYPES:
            STATE["channels"].pop(thread_id, None)
            STATE["messages"].pop(thread_id, None)
    for webhook_id, hook in list(STATE["webhooks"].items()):
        if hook.get("channel_id") == channel_id:
            del STATE["webhooks"][webhook_id]
    return _render_channel(channel)


@api.post("/channels/{channel_id}/typing", status_code=204)
def trigger_typing(channel_id: str):
    if not _channel(channel_id):
        return _unknown_channel()
    return Response(status_code=204)


@api.put("/channels/{channel_id}/permissions/{overwrite_id}", status_code=204)
async def edit_channel_permissions(channel_id: str, overwrite_id: str, request: Request):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    body = await _json_body(request)
    overwrites = [o for o in channel.get("permission_overwrites", [])
                  if str(o.get("id")) != overwrite_id]
    overwrites.append({
        "id": overwrite_id,
        "type": int(body.get("type") or 0),
        "allow": str(body.get("allow") or 0),
        "deny": str(body.get("deny") or 0),
    })
    channel["permission_overwrites"] = overwrites
    return Response(status_code=204)


@api.delete("/channels/{channel_id}/permissions/{overwrite_id}", status_code=204)
def delete_channel_permission(channel_id: str, overwrite_id: str):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    channel["permission_overwrites"] = [o for o in channel.get("permission_overwrites", [])
                                        if str(o.get("id")) != overwrite_id]
    return Response(status_code=204)


# --- messages ----------------------------------------------------------------

def _clamp(raw: Any, *, default: int, low: int = 1, high: int = 100) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


async def _json_body(request: Request) -> Any:
    """The parsed JSON body ({} when there is none, so callers can just ``.get``)."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, (dict, list)) else {}


async def _message_body(request: Request) -> tuple[dict, list[tuple[str, bytes, str]]]:
    """A message body as JSON, or as multipart ``payload_json`` + ``files[n]``."""
    if "multipart/form-data" in (request.headers.get("content-type") or ""):
        form = await request.form()
        raw = form.get("payload_json")
        body = json.loads(raw) if isinstance(raw, str) and raw else {}
        uploads = []
        for key, value in form.multi_items():
            if key.startswith("files[") and hasattr(value, "read"):
                uploads.append((value.filename or key, await value.read(),
                                value.content_type or "application/octet-stream"))
            elif key != "payload_json" and isinstance(value, str):
                body.setdefault(key, value)
        return body, uploads
    return await _json_body(request), []


def _store_attachment(channel_id: str, filename: str, data: bytes, content_type: str,
                      description: str | None = None) -> dict:
    attachment_id = _snowflake()
    STATE.setdefault("_attachments", {})[attachment_id] = {
        "filename": filename,
        "content_type": content_type,
        "data": base64.b64encode(data).decode("ascii"),
    }
    url = f"https://cdn.discordapp.com/attachments/{channel_id}/{attachment_id}/{quote(filename)}"
    attachment = {
        "id": attachment_id,
        "filename": filename,
        "size": len(data),
        "url": url,
        "proxy_url": url,
        "content_type": content_type,
    }
    if description:
        attachment["description"] = description
    return attachment


_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")


def _new_message(channel: dict, body: dict, uploads: list[tuple[str, bytes, str]], *,
                 author: dict, webhook_id: str | None = None) -> dict:
    channel_id = channel["id"]
    content = body.get("content") or ""
    message_id = _snowflake()
    metadata = {str(a.get("id")): a for a in body.get("attachments") or []
                if isinstance(a, dict)}
    attachments = [
        _store_attachment(channel_id, name, data, content_type,
                          (metadata.get(str(index)) or {}).get("description"))
        for index, (name, data, content_type) in enumerate(uploads)
    ]
    message: dict[str, Any] = {
        "id": message_id,
        "type": 0,
        "channel_id": channel_id,
        "author": author,
        "content": content,
        "timestamp": _snowflake_time(message_id),
        "edited_timestamp": None,
        "tts": bool(body.get("tts", False)),
        "mention_everyone": "@everyone" in content or "@here" in content,
        "mentions": [_user(uid) for uid in _USER_MENTION.findall(content)],
        "mention_roles": _ROLE_MENTION.findall(content),
        "attachments": attachments,
        "embeds": body.get("embeds") or [],
        "components": body.get("components") or [],
        "reactions": [],
        "pinned": False,
        "flags": int(body.get("flags") or 0),
    }
    reference = body.get("message_reference")
    if isinstance(reference, dict) and reference.get("message_id"):
        # Omitted entirely when absent: SDKs treat a null reference as a parse error.
        message["message_reference"] = {
            "type": int(reference.get("type") or 0),
            "message_id": str(reference["message_id"]),
            "channel_id": str(reference.get("channel_id") or channel_id),
            **({"guild_id": channel["guild_id"]} if channel.get("guild_id") else {}),
        }
        message["type"] = 19  # REPLY
    if webhook_id:
        message["webhook_id"] = webhook_id
    _messages(channel_id).append(message)
    channel["last_message_id"] = message_id
    return message


@api.get("/channels/{channel_id}/messages")
async def list_messages(channel_id: str, request: Request):
    if not _channel(channel_id):
        return _unknown_channel()
    params = request.query_params
    limit = _clamp(params.get("limit"), default=50)
    msgs = sorted(_messages(channel_id), key=lambda m: _as_snowflake(m["id"]))
    around, before, after = params.get("around"), params.get("before"), params.get("after")
    if around:
        pivot = _as_snowflake(around)
        index = next((i for i, m in enumerate(msgs) if _as_snowflake(m["id"]) >= pivot), len(msgs))
        half = limit // 2
        window = msgs[max(0, index - half):index + half + 1]
    elif before:
        window = [m for m in msgs if _as_snowflake(m["id"]) < _as_snowflake(before)][-limit:]
    elif after:
        window = [m for m in msgs if _as_snowflake(m["id"]) > _as_snowflake(after)][:limit]
    else:
        window = msgs[-limit:]
    return [_render_message(m) for m in reversed(window)]  # newest first, as Discord does


@api.post("/channels/{channel_id}/messages")
async def create_message(channel_id: str, request: Request):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    body, uploads = await _message_body(request)
    if not (body.get("content") or body.get("embeds") or uploads or body.get("sticker_ids")
            or body.get("components") or body.get("poll")):
        return discord_error(400, 50006, "Cannot send an empty message")
    return _render_message(_new_message(channel, body, uploads, author=_build_bot_user()))


@api.get("/channels/{channel_id}/messages/pins")
async def list_message_pins(channel_id: str, request: Request):
    """The current pins endpoint: newest first, cursored by ``pinned_at``."""
    if not _channel(channel_id):
        return _unknown_channel()
    limit = _clamp(request.query_params.get("limit"), default=50)
    before = request.query_params.get("before")
    pinned = [m for m in _messages(channel_id) if m.get("pinned")]
    pinned.sort(key=lambda m: m.get("_pinned_at") or m["timestamp"], reverse=True)
    if before:
        pinned = [m for m in pinned if (m.get("_pinned_at") or m["timestamp"]) < before]
    window = pinned[:limit]
    return {
        "items": [{"pinned_at": m.get("_pinned_at") or m["timestamp"], "message": _render_message(m)}
                  for m in window],
        "has_more": len(pinned) > len(window),
    }


@api.put("/channels/{channel_id}/messages/pins/{message_id}", status_code=204)
def pin_message(channel_id: str, message_id: str):
    return _set_pinned(channel_id, message_id, True)


@api.delete("/channels/{channel_id}/messages/pins/{message_id}", status_code=204)
def unpin_message(channel_id: str, message_id: str):
    return _set_pinned(channel_id, message_id, False)


def _set_pinned(channel_id: str, message_id: str, pinned: bool) -> Response:
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    message["pinned"] = pinned
    message["_pinned_at"] = _now() if pinned else None
    return Response(status_code=204)


@api.post("/channels/{channel_id}/messages/bulk-delete", status_code=204)
async def bulk_delete_messages(channel_id: str, request: Request):
    if not _channel(channel_id):
        return _unknown_channel()
    body = await _json_body(request)
    ids = [str(m) for m in body.get("messages") or []]
    if len(set(ids)) < 2:
        return discord_error(
            400, 50016,
            "Provided too few messages to delete. Must provide at least 2 messages to delete.")
    if len(set(ids)) > 100:
        return discord_error(
            400, 50016,
            "Provided too many messages to delete. Must provide at most 100 messages to delete.")
    STATE["messages"][channel_id] = [m for m in _messages(channel_id) if m["id"] not in set(ids)]
    return Response(status_code=204)


@api.get("/channels/{channel_id}/messages/{message_id}")
def get_message(channel_id: str, message_id: str):
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    return _render_message(message)


@api.patch("/channels/{channel_id}/messages/{message_id}")
async def edit_message(channel_id: str, message_id: str, request: Request):
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    body, uploads = await _message_body(request)
    if "content" in body:
        message["content"] = body["content"] or ""
        message["mentions"] = [_user(uid) for uid in _USER_MENTION.findall(message["content"])]
        message["mention_roles"] = _ROLE_MENTION.findall(message["content"])
    if "embeds" in body:
        message["embeds"] = body["embeds"] or []
    if "components" in body:
        message["components"] = body["components"] or []
    if "flags" in body:
        message["flags"] = int(body["flags"] or 0)
    if "attachments" in body or uploads:
        kept = {str(a.get("id")) for a in body.get("attachments") or [] if isinstance(a, dict)}
        message["attachments"] = [a for a in message["attachments"] if a["id"] in kept]
        message["attachments"] += [_store_attachment(channel_id, name, data, content_type)
                                   for name, data, content_type in uploads]
    message["edited_timestamp"] = _now()
    return _render_message(message)


@api.delete("/channels/{channel_id}/messages/{message_id}", status_code=204)
def delete_message(channel_id: str, message_id: str):
    msgs = _messages(channel_id)
    index = next((i for i, m in enumerate(msgs) if m["id"] == message_id), None)
    if index is None:
        return _unknown_message()
    msgs.pop(index)
    return Response(status_code=204)


@api.post("/channels/{channel_id}/messages/{message_id}/crosspost")
def crosspost_message(channel_id: str, message_id: str):
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    message["flags"] = int(message.get("flags") or 0) | 2  # CROSSPOSTED
    return _render_message(message)


# --- reactions ---------------------------------------------------------------

def _emoji_payload(emoji: str) -> dict:
    """``name`` for unicode emoji, ``name:id`` for custom ones."""
    name, _, emoji_id = emoji.rpartition(":")
    if name and emoji_id.isdigit():
        return {"id": emoji_id, "name": name.removeprefix("a:")}
    return {"id": None, "name": emoji}


def _sync_reactions(message: dict) -> None:
    """Rebuild the public reaction array from the per-user bookkeeping."""
    bot_id = _bot_user_id()
    message["reactions"] = [
        {
            "emoji": _emoji_payload(emoji),
            "count": len(users),
            "count_details": {"burst": 0, "normal": len(users)},
            "me": bot_id in users,
            "me_burst": False,
            "burst_colors": [],
        }
        for emoji, users in (message.get("_reactions") or {}).items() if users
    ]


def _react(channel_id: str, message_id: str, emoji: str, user_id: str, add: bool) -> Response:
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    reactions = message.setdefault("_reactions", {})
    users = reactions.setdefault(emoji, [])
    if add and user_id not in users:
        users.append(user_id)  # PUT is idempotent on the real API
    if not add and user_id in users:
        users.remove(user_id)
    if not users:
        reactions.pop(emoji, None)
    _sync_reactions(message)
    return Response(status_code=204)


@api.put("/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", status_code=204)
def add_reaction(channel_id: str, message_id: str, emoji: str):
    return _react(channel_id, message_id, emoji, _bot_user_id(), add=True)


@api.delete("/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", status_code=204)
def remove_own_reaction(channel_id: str, message_id: str, emoji: str):
    return _react(channel_id, message_id, emoji, _bot_user_id(), add=False)


@api.delete("/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{user_id}",
            status_code=204)
def remove_user_reaction(channel_id: str, message_id: str, emoji: str, user_id: str):
    return _react(channel_id, message_id, emoji, _resolve_user_id(user_id), add=False)


@api.get("/channels/{channel_id}/messages/{message_id}/reactions/{emoji}")
async def get_reactions(channel_id: str, message_id: str, emoji: str, request: Request):
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    users = (message.get("_reactions") or {}).get(emoji, [])
    limit = _clamp(request.query_params.get("limit"), default=25)
    return [_user(uid) for uid in users[:limit]]


@api.delete("/channels/{channel_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
def clear_emoji_reactions(channel_id: str, message_id: str, emoji: str):
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    (message.get("_reactions") or {}).pop(emoji, None)
    _sync_reactions(message)
    return Response(status_code=204)


@api.delete("/channels/{channel_id}/messages/{message_id}/reactions", status_code=204)
def clear_reactions(channel_id: str, message_id: str):
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    message["_reactions"] = {}
    message["reactions"] = []
    return Response(status_code=204)


# --- pins (legacy routes, still served by the real API) ----------------------

@api.get("/channels/{channel_id}/pins")
def get_pins(channel_id: str):
    if not _channel(channel_id):
        return _unknown_channel()
    pinned = [m for m in _messages(channel_id) if m.get("pinned")]
    pinned.sort(key=lambda m: m.get("_pinned_at") or m["timestamp"], reverse=True)
    return [_render_message(m) for m in pinned]


@api.put("/channels/{channel_id}/pins/{message_id}", status_code=204)
def pin_message_legacy(channel_id: str, message_id: str):
    return _set_pinned(channel_id, message_id, True)


@api.delete("/channels/{channel_id}/pins/{message_id}", status_code=204)
def unpin_message_legacy(channel_id: str, message_id: str):
    return _set_pinned(channel_id, message_id, False)


# --- threads -----------------------------------------------------------------

def _new_thread(parent: dict, body: dict, *, thread_id: str | None = None,
                default_type: int = 11) -> dict:
    thread_id = thread_id or _snowflake()
    thread = {
        "id": thread_id,
        "type": int(body.get("type") or default_type),
        "guild_id": parent.get("guild_id"),
        "parent_id": parent["id"],
        "owner_id": _bot_user_id(),
        "name": body.get("name") or "thread",
        "last_message_id": None,
        "message_count": 0,
        "member_count": 1,
        "total_message_sent": 0,
        "rate_limit_per_user": int(body.get("rate_limit_per_user") or 0),
        "flags": 0,
        "applied_tags": [str(t) for t in body.get("applied_tags") or []],
        "thread_metadata": {
            "archived": False,
            "auto_archive_duration": int(body.get("auto_archive_duration") or 1440),
            "archive_timestamp": _snowflake_time(thread_id),
            "locked": False,
            "invitable": bool(body.get("invitable", True)),
            "create_timestamp": _snowflake_time(thread_id),
        },
    }
    STATE["channels"][thread_id] = thread
    STATE["messages"][thread_id] = []
    return thread


@api.post("/channels/{channel_id}/messages/{message_id}/threads", status_code=201)
async def start_thread_from_message(channel_id: str, message_id: str, request: Request):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    message = _find_message(channel_id, message_id)
    if not message:
        return _unknown_message()
    body = await _json_body(request)
    # A thread started from a message takes that message's id, as the real API does.
    return _new_thread(channel, body, thread_id=message_id,
                       default_type=10 if channel.get("type") == ANNOUNCEMENT_CHANNEL else 11)


@api.post("/channels/{channel_id}/threads", status_code=201)
async def start_thread(channel_id: str, request: Request):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    body, uploads = await _message_body(request)
    thread = _new_thread(channel, body)
    starter = body.get("message")
    if channel.get("type") == FORUM_CHANNEL or starter:
        message = _new_message(thread, starter or body, uploads, author=_build_bot_user())
        return {**thread, "message": _render_message(message)}
    return thread


@api.get("/channels/{channel_id}/threads/archived/public")
async def list_public_archived_threads(channel_id: str, request: Request):
    if not _channel(channel_id):
        return _unknown_channel()
    limit = _clamp(request.query_params.get("limit"), default=50)
    threads = [_render_channel(c) for c in STATE["channels"].values()
               if c.get("parent_id") == channel_id and c.get("type") in THREAD_TYPES
               and (c.get("thread_metadata") or {}).get("archived")]
    return {"threads": threads[:limit], "members": [], "has_more": len(threads) > limit}


@api.get("/channels/{channel_id}/threads/archived/private")
async def list_private_archived_threads(channel_id: str, request: Request):
    """The sandbox bot sees every thread, so both archives list the same ones."""
    return await list_public_archived_threads(channel_id, request)


@api.put("/channels/{channel_id}/thread-members/@me", status_code=204)
def join_thread(channel_id: str):
    return _thread_membership(channel_id, _bot_user_id(), join=True)


@api.delete("/channels/{channel_id}/thread-members/@me", status_code=204)
def leave_thread(channel_id: str):
    return _thread_membership(channel_id, _bot_user_id(), join=False)


@api.put("/channels/{channel_id}/thread-members/{user_id}", status_code=204)
def add_thread_member(channel_id: str, user_id: str):
    return _thread_membership(channel_id, _resolve_user_id(user_id), join=True)


@api.delete("/channels/{channel_id}/thread-members/{user_id}", status_code=204)
def remove_thread_member(channel_id: str, user_id: str):
    return _thread_membership(channel_id, _resolve_user_id(user_id), join=False)


def _thread_membership(channel_id: str, user_id: str, *, join: bool) -> Response:
    thread = _channel(channel_id)
    if not thread or thread.get("type") not in THREAD_TYPES:
        return _unknown_channel()
    members = thread.setdefault("_members", [])
    if join and user_id not in members:
        members.append(user_id)
    if not join and user_id in members:
        members.remove(user_id)
    thread["member_count"] = max(1, len(members))
    return Response(status_code=204)


@api.get("/channels/{channel_id}/thread-members")
def list_thread_members(channel_id: str):
    thread = _channel(channel_id)
    if not thread or thread.get("type") not in THREAD_TYPES:
        return _unknown_channel()
    return [{"id": channel_id, "user_id": uid, "join_timestamp": _now(), "flags": 0}
            for uid in thread.get("_members", [])]


# --- webhooks ----------------------------------------------------------------

def _webhook_payload(hook: dict) -> dict:
    return {k: v for k, v in hook.items() if not k.startswith("_")}


@api.post("/channels/{channel_id}/webhooks")
async def create_webhook(channel_id: str, request: Request):
    channel = _channel(channel_id)
    if not channel:
        return _unknown_channel()
    body = await _json_body(request)
    webhook_id = _snowflake()
    # discord.py's Webhook.from_url only accepts tokens of 60+ characters.
    token = uuid.uuid4().hex + uuid.uuid4().hex + uuid.uuid4().hex[:4]
    hook = {
        "id": webhook_id,
        "type": 1,
        "guild_id": channel.get("guild_id"),
        "channel_id": channel_id,
        "user": _build_bot_user(),
        "name": body.get("name") or "Webhook",
        "avatar": body.get("avatar"),
        "token": token,
        "application_id": None,
        "url": f"https://discord.com/api/webhooks/{webhook_id}/{token}",
    }
    STATE["webhooks"][webhook_id] = hook
    return hook


@api.get("/channels/{channel_id}/webhooks")
def list_channel_webhooks(channel_id: str):
    if not _channel(channel_id):
        return _unknown_channel()
    return [_webhook_payload(w) for w in STATE["webhooks"].values()
            if w.get("channel_id") == channel_id]


@api.get("/guilds/{guild_id}/webhooks")
def list_guild_webhooks(guild_id: str):
    if guild_id not in STATE["guilds"]:
        return _unknown_guild()
    return [_webhook_payload(w) for w in STATE["webhooks"].values()
            if w.get("guild_id") == guild_id]


def _webhook(webhook_id: str, token: str | None = None) -> dict | None:
    hook = STATE["webhooks"].get(webhook_id)
    if not hook or (token is not None and hook.get("token") != token):
        return None
    return hook


@api.get("/webhooks/{webhook_id}")
def get_webhook(webhook_id: str):
    hook = _webhook(webhook_id)
    if not hook:
        return discord_error(404, 10015, "Unknown Webhook")
    return _webhook_payload(hook)


@api.get("/webhooks/{webhook_id}/{webhook_token}")
def get_webhook_with_token(webhook_id: str, webhook_token: str):
    hook = _webhook(webhook_id, webhook_token)
    if not hook:
        return discord_error(404, 10015, "Unknown Webhook")
    # Fetching with a token omits the user, as the real API does.
    return {k: v for k, v in _webhook_payload(hook).items() if k != "user"}


@api.patch("/webhooks/{webhook_id}")
async def modify_webhook(webhook_id: str, request: Request):
    hook = _webhook(webhook_id)
    if not hook:
        return discord_error(404, 10015, "Unknown Webhook")
    body = await _json_body(request)
    for field in ("name", "avatar", "channel_id"):
        if field in body:
            hook[field] = body[field]
    return _webhook_payload(hook)


@api.patch("/webhooks/{webhook_id}/{webhook_token}")
async def modify_webhook_with_token(webhook_id: str, webhook_token: str, request: Request):
    if not _webhook(webhook_id, webhook_token):
        return discord_error(404, 10015, "Unknown Webhook")
    return await modify_webhook(webhook_id, request)


@api.delete("/webhooks/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: str):
    if webhook_id not in STATE["webhooks"]:
        return discord_error(404, 10015, "Unknown Webhook")
    del STATE["webhooks"][webhook_id]
    return Response(status_code=204)


@api.delete("/webhooks/{webhook_id}/{webhook_token}", status_code=204)
def delete_webhook_with_token(webhook_id: str, webhook_token: str):
    if not _webhook(webhook_id, webhook_token):
        return discord_error(404, 10015, "Unknown Webhook")
    del STATE["webhooks"][webhook_id]
    return Response(status_code=204)


@api.post("/webhooks/{webhook_id}/{webhook_token}")
async def execute_webhook(webhook_id: str, webhook_token: str, request: Request):
    hook = _webhook(webhook_id, webhook_token)
    if not hook:
        return discord_error(404, 10015, "Unknown Webhook")
    body, uploads = await _message_body(request)
    if not (body.get("content") or body.get("embeds") or uploads):
        return discord_error(400, 50006, "Cannot send an empty message")
    thread_id = request.query_params.get("thread_id") or body.get("thread_id")
    channel = _channel(str(thread_id)) if thread_id else _channel(hook["channel_id"])
    if not channel:
        return _unknown_channel()
    author = {
        "id": webhook_id,
        "username": body.get("username") or hook["name"],
        "discriminator": "0000",
        "global_name": None,
        "avatar": body.get("avatar_url") or hook.get("avatar"),
        "bot": True,
    }
    message = _new_message(channel, body, uploads, author=author, webhook_id=webhook_id)
    if request.query_params.get("wait") in ("1", "true", "True"):
        return _render_message(message)
    # Without ?wait=true the real API acknowledges and returns nothing.
    return Response(status_code=204)


@api.get("/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}")
def get_webhook_message(webhook_id: str, webhook_token: str, message_id: str):
    hook = _webhook(webhook_id, webhook_token)
    if not hook:
        return discord_error(404, 10015, "Unknown Webhook")
    message = _find_webhook_message(hook, message_id)
    if not message:
        return _unknown_message()
    return _render_message(message)


@api.patch("/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}")
async def edit_webhook_message(webhook_id: str, webhook_token: str, message_id: str,
                               request: Request):
    hook = _webhook(webhook_id, webhook_token)
    if not hook:
        return discord_error(404, 10015, "Unknown Webhook")
    message = _find_webhook_message(hook, message_id)
    if not message:
        return _unknown_message()
    return await edit_message(message["channel_id"], message_id, request)


@api.delete("/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}", status_code=204)
def delete_webhook_message(webhook_id: str, webhook_token: str, message_id: str):
    hook = _webhook(webhook_id, webhook_token)
    if not hook:
        return discord_error(404, 10015, "Unknown Webhook")
    message = _find_webhook_message(hook, message_id)
    if not message:
        return _unknown_message()
    return delete_message(message["channel_id"], message_id)


def _find_webhook_message(hook: dict, message_id: str) -> dict | None:
    """A webhook can reach its own messages in its channel and that channel's threads."""
    channels = [hook["channel_id"]] + [
        c["id"] for c in STATE["channels"].values() if c.get("parent_id") == hook["channel_id"]
    ]
    for channel_id in channels:
        message = _find_message(channel_id, message_id)
        if message:
            return message
    return None


# --- application (slash) commands -------------------------------------------

def _command_payload(body: dict, guild_id: str | None, command_id: str | None = None) -> dict:
    command_id = command_id or _snowflake()
    return {
        "id": command_id,
        "application_id": _bot_user_id(),
        "type": int(body.get("type") or 1),
        "name": body.get("name") or "command",
        "description": body.get("description") or "",
        "options": body.get("options") or [],
        "default_member_permissions": body.get("default_member_permissions"),
        "dm_permission": body.get("dm_permission", True),
        "nsfw": bool(body.get("nsfw", False)),
        "version": command_id,
        **({"guild_id": guild_id} if guild_id else {}),
    }


def _commands_for(guild_id: str | None) -> list[dict]:
    return [c for c in STATE["commands"].values() if c.get("guild_id") == guild_id]


def _upsert_command(body: dict, guild_id: str | None) -> dict:
    existing = next((c for c in _commands_for(guild_id) if c["name"] == body.get("name")), None)
    command = _command_payload(body, guild_id, existing["id"] if existing else None)
    STATE["commands"][command["id"]] = command
    return command


@api.get("/applications/{application_id}/commands")
def list_global_commands(application_id: str):
    return _commands_for(None)


@api.post("/applications/{application_id}/commands")
async def create_global_command(application_id: str, request: Request):
    return _upsert_command(await _json_body(request), None)


@api.put("/applications/{application_id}/commands")
async def bulk_overwrite_global_commands(application_id: str, request: Request):
    return _bulk_overwrite(await _json_body(request), None)


@api.delete("/applications/{application_id}/commands/{command_id}", status_code=204)
def delete_global_command(application_id: str, command_id: str):
    STATE["commands"].pop(command_id, None)
    return Response(status_code=204)


@api.get("/applications/{application_id}/guilds/{guild_id}/commands")
def list_guild_commands(application_id: str, guild_id: str):
    return _commands_for(guild_id)


@api.post("/applications/{application_id}/guilds/{guild_id}/commands")
async def create_guild_command(application_id: str, guild_id: str, request: Request):
    return _upsert_command(await _json_body(request), guild_id)


@api.put("/applications/{application_id}/guilds/{guild_id}/commands")
async def bulk_overwrite_guild_commands(application_id: str, guild_id: str, request: Request):
    return _bulk_overwrite(await _json_body(request), guild_id)


@api.delete("/applications/{application_id}/guilds/{guild_id}/commands/{command_id}",
            status_code=204)
def delete_guild_command(application_id: str, guild_id: str, command_id: str):
    STATE["commands"].pop(command_id, None)
    return Response(status_code=204)


def _bulk_overwrite(payload: Any, guild_id: str | None) -> list[dict]:
    """``tree.sync()`` replaces the whole command set in one PUT."""
    for command in _commands_for(guild_id):
        STATE["commands"].pop(command["id"], None)
    return [_upsert_command(entry, guild_id) for entry in payload or []]


# --- CDN ---------------------------------------------------------------------

@app.get("/attachments/{channel_id}/{attachment_id}/{filename}")
def download_attachment(channel_id: str, attachment_id: str, filename: str):
    """Uploads are served back from the CDN path their attachment objects point at."""
    blob = (STATE.get("_attachments") or {}).get(attachment_id)
    if not blob:
        return discord_error(404, 0, "404: Not Found")
    return Response(content=base64.b64decode(blob["data"]), media_type=blob["content_type"])


for _prefix in API_PREFIXES:
    app.include_router(api, prefix=_prefix)


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.discord_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
