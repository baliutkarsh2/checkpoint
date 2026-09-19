"""Slack twin: a stateful, in-memory Slack Web API.

Slack is RPC over HTTP: every method lives at ``/api/<method>``, answers GET and
POST alike with arguments in the query string, a form body or a JSON body, and
reports application errors with HTTP 200 and ``{"ok": false, "error": "..."}``.
The twin mirrors all of that, because official SDKs (``slack_sdk``,
``@slack/web-api``) POST every method and branch on ``ok`` — a 404 or a 405 from
a web framework reaches an agent as an unrecoverable transport error.

The credential belongs to a bot user (``auth.test`` reports it) that behaves as
if it held ``chat:write.public``: it can read and post in any channel without
joining first, the way most installed apps are configured. Membership is still
tracked and reported (``is_member``, ``conversations.members``), it just is not
enforced, so an agent is never blocked on a detail a scenario's seed cannot
express.

The control plane and fault model come from :mod:`checkpoint.twins.kit`.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
import uuid
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from checkpoint.fake_credentials import FAKE_SLACK_TOKEN
from checkpoint.twins import kit

app = FastAPI(title="checkpoint slack twin")

DEFAULT_BOOTSTRAP_TOKEN = FAKE_SLACK_TOKEN

SEEDS_DIR = Path(__file__).parent / "slack_seeds"

# The workspace and the app the credential belongs to. Every write the agent
# makes is attributed to this bot user, so "did the agent post it?" is decidable
# from a message's ``user``/``bot_id`` alone.
TEAM_ID = "T0CHECKPOINT"
BOT_USER_ID = "U0CHECKPOINT"
BOT_ID = "B0CHECKPOINT"
APP_ID = "A0CHECKPOINT"

# Slack: lowercase letters, numbers, hyphens, underscores, periods; max 80 chars.
_CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_MAX_CHANNEL_NAME = 80
# Scheduling further out than this is rejected by the real API.
_MAX_SCHEDULE_SECONDS = 120 * 24 * 3600


def _bot_user() -> dict:
    return {
        "id": BOT_USER_ID,
        "team_id": TEAM_ID,
        "name": "checkpoint",
        "real_name": "Checkpoint",
        "deleted": False,
        "is_bot": True,
        "is_admin": False,
        "is_owner": False,
        "is_app_user": False,
        "profile": {
            "real_name": "Checkpoint",
            "display_name": "checkpoint",
            "title": "",
            "email": None,
            "bot_id": BOT_ID,
            "api_app_id": APP_ID,
        },
    }


def _fresh_state() -> dict:
    return {
        "team": {"id": TEAM_ID, "name": "Checkpoint", "domain": "checkpoint",
                 "email_domain": "", "url": "https://checkpoint.slack.com/"},
        "channels": {},             # channel_id -> channel (public, private, im or mpim)
        "users": {BOT_USER_ID: _bot_user()},
        "messages": {},             # channel_id -> [message], oldest first
        "ephemeral_messages": [],   # never in history, but a scenario may assert on them
        "files": {},                # file_id -> file
        "scheduled_messages": {},   # scheduled_message_id -> record
        "_counters": {
            "channel_id": 0,
            "im_id": 0,
            "user_id": 0,
            "file_id": 0,
            "scheduled_id": 0,
            "ts_seq": 0,
        },
        "_config": {
            "page_size": 100,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers -------------------------------------------------------------

def slack_error(error: str, status: int = 200, **extra: Any) -> JSONResponse:
    """A Slack application error: HTTP 200 with ``ok: false`` unless told otherwise."""
    body: dict[str, Any] = {"ok": False, "error": error}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def slack_ok(**fields: Any) -> dict:
    body: dict[str, Any] = {"ok": True}
    body.update(fields)
    return body


def _missing(field: str) -> JSONResponse:
    """How Slack reports an argument the caller left out."""
    return slack_error(
        "invalid_arguments",
        response_metadata={"messages": [f"[ERROR] missing required field: {field}"]},
    )


def _now() -> int:
    return int(time.time())


def _ts() -> str:
    """Slack-style ``seconds.microseconds`` message id, unique and increasing."""
    STATE["_counters"]["ts_seq"] += 1
    return f"{_now()}.{STATE['_counters']['ts_seq']:06d}"


def _ts_num(ts: Any) -> Decimal | None:
    """A timestamp as a number, or None if it is not one (``invalid_ts_*``)."""
    try:
        return Decimal(str(ts))
    except (InvalidOperation, ValueError):
        return None


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


def _new_id(counter: str, prefix: str) -> str:
    STATE["_counters"][counter] = STATE["_counters"].get(counter, 0) + 1
    return f"{prefix}{STATE['_counters'][counter]:08d}"


def _slack_headers() -> dict:
    return {
        "X-Slack-Req-Id": uuid.uuid4().hex[:16],
    }


def _team_url() -> str:
    team = STATE.get("team") or {}
    return str(team.get("url") or f"https://{team.get('domain', 'checkpoint')}.slack.com/")


def _bot() -> dict:
    return STATE["users"].get(BOT_USER_ID) or _bot_user()


# --- argument parsing ----------------------------------------------------

_TRUTHY = {"1", "true", "t", "yes", "y"}


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return "" if value is None else str(value).strip()


def _flag(args: dict, key: str, default: bool = False) -> bool:
    """SDKs send booleans as ``1``/``0``, ``true``/``false`` or real JSON booleans."""
    value = args.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _number(args: dict, key: str, default: int | None = None) -> int | None:
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _structured(args: dict, key: str) -> Any:
    """``blocks``/``attachments``/``files`` are JSON strings on form posts, lists on JSON posts."""
    value = args.get(key)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _ids(raw: str) -> list[str]:
    """A comma-separated id argument (``users``, ``channels``) as a list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


async def _args(request: Request) -> dict[str, Any] | Response:
    """The method's arguments, wherever the caller put them.

    ``slack_sdk`` sends a urlencoded body for most methods, a JSON body for
    ``chat.*``, and multipart for file arguments; ``@slack/web-api`` and curl
    recipes use the query string. All three are the same call to Slack.
    """
    args: dict[str, Any] = dict(request.query_params)
    body = await request.body()
    if not body:
        return args
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return slack_error("invalid_post_type")
        if not isinstance(payload, dict):
            return slack_error("invalid_post_type")
        args.update(payload)
        return args
    form = await request.form()
    args.update({key: value for key, value in form.items() if isinstance(value, str)})
    return args


# --- runtime: auth, faults, trace, control plane --------------------------

_MULTIPART_TOKEN = re.compile(rb'name="token"\r\n\r\n(?P<value>[^\r\n]*)')


def _request_token(request: Request) -> str | None:
    """Slack accepts the token as a Bearer header, a query param, or a form field."""
    token = _extract_token(request.headers.get("authorization")) or request.query_params.get("token")
    if token:
        return token
    body = getattr(request, "_body", b"") or b""
    if b"token=" in body:
        values = parse_qs(body.decode("utf-8", errors="replace")).get("token")
        return values[0] if values else None
    match = _MULTIPART_TOKEN.search(body)
    return match.group("value").decode("utf-8", errors="replace") if match else None


def _authenticate(request: Request) -> Response | None:
    # The upload URL carries its own credential in the path: the SDK POSTs the
    # file bytes there with no Authorization header at all.
    if request.url.path.startswith("/upload/"):
        return None
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


# The two method families whose name differs from the resource they touch; for
# every other family (users, files, pins, ...) the family name is the resource.
_SLACK_FAMILIES = {"chat": "messages", "conversations": "channels"}
_READ_VERBS = ("list", "info", "history", "replies", "get", "lookup", "test", "members", "search")
_DELETE_VERBS = ("delete", "remove", "kick", "leave", "archive")
_UPDATE_VERBS = ("update", "set", "rename", "unarchive", "mark")

# Methods the verb heuristic below would misread: membership changes edit a
# channel rather than create or delete one, the upload dance creates a file
# through a "get" method, and a few methods name a resource the family does not.
_CLASSIFY_OVERRIDES: dict[str, tuple[kit.Op, str]] = {
    "conversations.join": ("update", "channels"),
    "conversations.invite": ("update", "channels"),
    "conversations.kick": ("update", "channels"),
    "conversations.leave": ("update", "channels"),
    "chat.scheduleMessage": ("create", "scheduled_messages"),
    "chat.scheduledMessages.list": ("read", "scheduled_messages"),
    "chat.deleteScheduledMessage": ("delete", "scheduled_messages"),
    "chat.postEphemeral": ("create", "ephemeral_messages"),
    "files.getUploadURLExternal": ("create", "files"),
    "files.completeUploadExternal": ("update", "files"),
    "users.conversations": ("read", "channels"),
    "search.messages": ("read", "messages"),
    "auth.test": ("read", "auth"),
    "team.info": ("read", "team"),
    "api.test": ("read", "api"),
}


def _classify(method: str, path: str, body: object) -> tuple[kit.Op, str] | None:
    """Slack is RPC over HTTP: the method name, not the HTTP verb, says what happened."""
    if path.startswith("/upload/"):
        return "update", "files"
    name = path.rsplit("/", 1)[-1]
    override = _CLASSIFY_OVERRIDES.get(name)
    if override is not None:
        return override
    parts = name.split(".")
    if len(parts) < 2:
        return None
    resource = _SLACK_FAMILIES.get(parts[0], parts[0])
    verb = parts[-1].lower()
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


# --- seeds ---------------------------------------------------------------

def _id_number(ident: str) -> int:
    match = re.search(r"(\d+)$", ident)
    return int(match.group(1)) if match else 0


def _normalize_channel(channel_id: str, channel: dict) -> None:
    """Fill a hand-written seed channel out into the object the API returns."""
    channel.setdefault("id", channel_id)
    channel.setdefault("created", _now())
    channel.setdefault("creator", "")
    channel.setdefault("is_archived", False)
    channel.setdefault("is_org_shared", False)
    channel.setdefault("members", [])
    if channel.get("is_im"):
        channel.setdefault("is_channel", False)
        channel.setdefault("is_group", False)
        channel.setdefault("is_mpim", False)
        channel.setdefault("is_private", True)
        channel.setdefault("user", "")
        channel.setdefault("is_user_deleted", False)
        channel.setdefault("priority", 0)
        return
    name = str(channel.get("name") or channel_id.lower())
    channel["name"] = name
    is_mpim = bool(channel.get("is_mpim"))
    channel.setdefault("name_normalized", name)
    channel.setdefault("is_channel", not is_mpim)
    channel.setdefault("is_group", is_mpim)
    channel.setdefault("is_im", False)
    channel.setdefault("is_mpim", is_mpim)
    channel.setdefault("is_private", is_mpim)
    channel.setdefault("is_general", name == "general")
    channel.setdefault("unlinked", 0)
    channel.setdefault("is_shared", False)
    channel.setdefault("is_ext_shared", False)
    channel.setdefault("is_pending_ext_shared", False)
    channel.setdefault("pending_shared", [])
    channel.setdefault("parent_conversation", None)
    channel.setdefault("shared_team_ids", [TEAM_ID])
    channel.setdefault("pending_connected_team_ids", [])
    channel.setdefault("previous_names", [])
    channel.setdefault("num_members", len(channel.get("members") or []))
    for field in ("topic", "purpose"):
        value = channel.get(field)
        if not isinstance(value, dict):
            value = {"value": str(value or "")}
            channel[field] = value
        value.setdefault("creator", "")
        value.setdefault("last_set", 0)


def _after_seed(state: dict) -> None:
    """Turn a compact seed into full API records and keep id counters ahead of it.

    Seeds stay readable (a channel is a name, a topic and a member list); the
    twin fills in the dozens of fields SDKs expect. Counters are advanced past
    every id the seed already uses so generated ids never collide with it.
    """
    users = state.setdefault("users", {})
    # The app's own user belongs after the workspace's people: a seed's first
    # user should be a person, the way the seed file reads.
    users[BOT_USER_ID] = users.pop(BOT_USER_ID, None) or _bot_user()
    for user_id, user in users.items():
        user.setdefault("id", user_id)
        user.setdefault("team_id", TEAM_ID)
        user.setdefault("deleted", False)
        user.setdefault("is_bot", user_id == BOT_USER_ID)
        profile = user.setdefault("profile", {})
        profile.setdefault("real_name", user.get("real_name") or user.get("name", ""))
        profile.setdefault("display_name", user.get("name", ""))

    channels = state.setdefault("channels", {})
    messages = state.setdefault("messages", {})
    for channel_id, channel in channels.items():
        _normalize_channel(channel_id, channel)
        messages.setdefault(channel_id, [])
    for channel_id, channel_messages in messages.items():
        for message in channel_messages:
            message.setdefault("type", "message")
            message.setdefault("channel", channel_id)

    counters = state.setdefault("_counters", {})
    for counter, prefix, ids in (
        ("channel_id", "C", channels),
        ("im_id", "D", channels),
        ("user_id", "U", users),
        ("file_id", "F", state.get("files") or {}),
        ("scheduled_id", "Q", state.get("scheduled_messages") or {}),
    ):
        highest = max((_id_number(i) for i in ids if str(i).startswith(prefix)), default=0)
        counters[counter] = max(int(counters.get(counter) or 0), highest)


# --- views ---------------------------------------------------------------

def _views(state: dict) -> dict[str, kit.View]:
    """Slack state as flat collections, with the channel names assertions use.

    Messages live in state as ``channel_id -> [message]``; nothing can be
    asserted about them without knowing which channel they are in and who
    posted them, so every record carries that denormalized.
    """
    channels = state.get("channels") or {}
    users = state.get("users") or {}
    labels = {cid: _channel_label(ch) for cid, ch in channels.items()}
    handles = {uid: str(u.get("name") or "") for uid, u in users.items()}

    channel_items = [{
        "id": cid,
        "name": labels.get(cid, ""),
        "type": _channel_type(ch),
        "is_private": bool(ch.get("is_private")),
        "is_archived": bool(ch.get("is_archived")),
        "topic": (ch.get("topic") or {}).get("value", ""),
        "purpose": (ch.get("purpose") or {}).get("value", ""),
        "creator": ch.get("creator", ""),
        "members": list(ch.get("members") or []),
        "num_members": int(ch.get("num_members") or len(ch.get("members") or [])),
    } for cid, ch in channels.items()]

    message_items = []
    reaction_items = []
    for cid, messages in (state.get("messages") or {}).items():
        for message in messages:
            ts = message.get("ts", "")
            message_items.append({
                "id": f"{cid}:{ts}",
                "ts": ts,
                "channel": cid,
                "channel_name": labels.get(cid, ""),
                "channel_type": _channel_type(channels.get(cid) or {}),
                "user": message.get("user", ""),
                "user_name": handles.get(message.get("user", ""), ""),
                "text": message.get("text", ""),
                "subtype": message.get("subtype"),
                "thread_ts": message.get("thread_ts"),
                "is_reply": bool(message.get("thread_ts")
                                 and message.get("thread_ts") != ts),
                "reply_count": message.get("reply_count", 0),
                "reactions": [r.get("name") for r in message.get("reactions") or []],
                "files": [f.get("name") for f in message.get("files") or []],
                "pinned": bool(message.get("pinned_to")),
                "edited": bool(message.get("edited")),
                "deleted": message.get("subtype") == "tombstone",
            })
            for reaction in message.get("reactions") or []:
                reaction_items.append({
                    "id": f"{cid}:{ts}:{reaction.get('name')}",
                    "name": reaction.get("name", ""),
                    "channel": cid,
                    "channel_name": labels.get(cid, ""),
                    "message_ts": ts,
                    "message_text": message.get("text", ""),
                    "count": reaction.get("count", len(reaction.get("users") or [])),
                    "users": list(reaction.get("users") or []),
                })

    user_items = [{
        "id": uid,
        "name": user.get("name", ""),
        "real_name": user.get("real_name", ""),
        "email": (user.get("profile") or {}).get("email") or user.get("email", ""),
        "title": (user.get("profile") or {}).get("title", ""),
        "is_bot": bool(user.get("is_bot")),
        "deleted": bool(user.get("deleted")),
    } for uid, user in users.items()]

    file_items = [{
        "id": fid,
        "name": file.get("name", ""),
        "title": file.get("title", ""),
        "size": file.get("size", 0),
        "user": file.get("user", ""),
        "channels": list(file.get("channels") or []),
        "channel_names": [labels.get(cid, "") for cid in file.get("channels") or []],
        "preview": file.get("preview", ""),
    } for fid, file in (state.get("files") or {}).items()]

    scheduled_items = [{
        "id": record.get("id", sid),
        "channel": record.get("channel_id", ""),
        "channel_name": labels.get(record.get("channel_id", ""), ""),
        "post_at": record.get("post_at"),
        "text": record.get("text", ""),
    } for sid, record in (state.get("scheduled_messages") or {}).items()]

    ephemeral_items = [{
        "id": record.get("ts", ""),
        "channel": record.get("channel", ""),
        "channel_name": labels.get(record.get("channel", ""), ""),
        "user": record.get("user", ""),
        "user_name": handles.get(record.get("user", ""), ""),
        "text": record.get("text", ""),
    } for record in (state.get("ephemeral_messages") or [])]

    return {
        # Archiving is the only way to retire a Slack channel, so it is the
        # channel tombstone.
        "channels": kit.View(channel_items, tombstone="is_archived",
                             nouns=("channel", "channels", "conversation", "conversations")),
        "messages": kit.View(message_items, tombstone="deleted",
                             nouns=("message", "messages", "post", "posts")),
        "users": kit.View(user_items, tombstone="deleted",
                          nouns=("user", "users", "member", "members")),
        "reactions": kit.View(reaction_items,
                              nouns=("reaction", "reactions", "emoji reaction")),
        "files": kit.View(file_items, nouns=("file", "files", "upload", "uploads")),
        "scheduled_messages": kit.View(
            scheduled_items, nouns=("scheduled message", "scheduled messages")),
        "ephemeral_messages": kit.View(
            ephemeral_items, nouns=("ephemeral message", "ephemeral messages")),
    }


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
    after_seed=_after_seed,
    views=_views,
))


# --- lookup and serialization --------------------------------------------

_CHANNEL_MENTION = re.compile(r"^<#(?P<id>[^|>]+)(\|[^>]*)?>$")
_USER_MENTION = re.compile(r"^<@(?P<id>[^|>]+)(\|[^>]*)?>$")


def _find_channel(ident: str) -> dict | None:
    """A conversation by id, by name, by ``#name`` or by a ``<#C123>`` mention."""
    if not ident:
        return None
    ident = str(ident).strip()
    mention = _CHANNEL_MENTION.match(ident)
    if mention:
        ident = mention.group("id")
    if ident in STATE["channels"]:
        return STATE["channels"][ident]
    name = ident.lstrip("#").lower()
    return next((ch for ch in STATE["channels"].values() if ch.get("name") == name), None)


def _find_user(ident: str) -> dict | None:
    """A user by id, by handle, or by a ``<@U123>`` mention."""
    if not ident:
        return None
    ident = str(ident).strip()
    mention = _USER_MENTION.match(ident)
    if mention:
        ident = mention.group("id")
    if ident in STATE["users"]:
        return STATE["users"][ident]
    handle = ident.lstrip("@").lower()
    return next((u for u in STATE["users"].values() if str(u.get("name", "")).lower() == handle), None)


def _find_message(channel_id: str, ts: str) -> dict | None:
    for m in STATE["messages"].get(channel_id, []):
        if m["ts"] == ts:
            return m
    return None


def _resolve_target(ident: str) -> dict | None:
    """Where a message goes: a conversation, or the bot's DM with a user id."""
    channel = _find_channel(ident)
    if channel is not None:
        return channel
    text = str(ident).strip()
    if not (text.startswith(("U", "W")) or _USER_MENTION.match(text)):
        return None
    user = _find_user(text)
    return _open_im(user["id"]) if user else None


def _serialize_channel(channel: dict) -> dict:
    """The channel as the API returns it: the member list itself is not on the wire."""
    members = channel.get("members") or []
    out = {k: v for k, v in channel.items() if k != "members"}
    if not channel.get("is_im"):
        out["is_member"] = BOT_USER_ID in members
        out["num_members"] = int(channel.get("num_members") or len(members))
    return out


def _adjust_members(channel: dict, delta: int) -> None:
    """Move the visible member count with a membership change.

    A seed may model a 200-person channel with a handful of named users, so the
    count it declares is the truth for the wire; joins and kicks move it instead
    of replacing it with the length of the modelled list.
    """
    channel["num_members"] = max(0, int(channel.get("num_members") or 0) + delta)


def _serialize_file(file: dict) -> dict:
    return {k: v for k, v in file.items() if not k.startswith("_")}


def _channel_type(channel: dict) -> str:
    if channel.get("is_im"):
        return "im"
    if channel.get("is_mpim"):
        return "mpim"
    return "private_channel" if channel.get("is_private") else "public_channel"


def _channel_label(channel: dict) -> str:
    """How a channel is named in a view: its name, or the DM partner for an IM."""
    if channel.get("name"):
        return str(channel["name"])
    if channel.get("is_im"):
        partner = STATE["users"].get(channel.get("user", ""))
        return f"dm:{partner['name']}" if partner else f"dm:{channel.get('user', '')}"
    return ""


def _make_channel(name: str, *, is_private: bool, creator: str = BOT_USER_ID) -> dict:
    channel_id = _new_id("channel_id", "C")
    channel = {
        "id": channel_id,
        "name": name,
        "name_normalized": name,
        "is_channel": True,
        "is_group": False,
        "is_im": False,
        "is_mpim": False,
        "is_private": is_private,
        "is_archived": False,
        "is_general": False,
        "created": _now(),
        "creator": creator,
        "unlinked": 0,
        "is_shared": False,
        "is_ext_shared": False,
        "is_org_shared": False,
        "is_pending_ext_shared": False,
        "pending_shared": [],
        "parent_conversation": None,
        "shared_team_ids": [TEAM_ID],
        "pending_connected_team_ids": [],
        "previous_names": [],
        "topic": {"value": "", "creator": "", "last_set": 0},
        "purpose": {"value": "", "creator": "", "last_set": 0},
        "num_members": 1,
        "members": [creator],
    }
    STATE["channels"][channel_id] = channel
    STATE["messages"].setdefault(channel_id, [])
    return channel


def _open_im(user_id: str) -> dict:
    """The bot's DM with ``user_id``, opened on first use the way Slack does."""
    existing = next((ch for ch in STATE["channels"].values()
                     if ch.get("is_im") and ch.get("user") == user_id), None)
    if existing is not None:
        return existing
    channel_id = _new_id("im_id", "D")
    channel = {
        "id": channel_id,
        "created": _now(),
        "is_im": True,
        "is_channel": False,
        "is_group": False,
        "is_mpim": False,
        "is_private": True,
        "is_archived": False,
        "is_org_shared": False,
        "is_user_deleted": False,
        "user": user_id,
        "priority": 0,
        "members": [BOT_USER_ID, user_id],
    }
    STATE["channels"][channel_id] = channel
    STATE["messages"].setdefault(channel_id, [])
    return channel


def _open_mpim(user_ids: list[str]) -> dict:
    """The group DM with ``user_ids``, matched on its member set like Slack's."""
    members = sorted({BOT_USER_ID, *user_ids})
    existing = next((ch for ch in STATE["channels"].values()
                     if ch.get("is_mpim") and sorted(ch.get("members") or []) == members), None)
    if existing is not None:
        return existing
    handles = [str((STATE["users"].get(uid) or {}).get("name") or uid) for uid in members]
    channel = _make_channel(f"mpdm-{'--'.join(handles)}-1", is_private=True)
    channel.update({"is_channel": False, "is_group": True, "is_mpim": True,
                    "members": members, "num_members": len(members)})
    return channel


# --- pagination ----------------------------------------------------------

def _encode_cursor(kind: str, offset: int) -> str:
    return base64.urlsafe_b64encode(f"{kind}:{offset}".encode()).decode()


def _decode_cursor(cursor: str, kind: str) -> int | None:
    """The offset inside one of our opaque cursors, or None if it is not one."""
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    prefix, _, offset = decoded.partition(":")
    return int(offset) if prefix == kind and offset.isdigit() else None


def _page(items: list, args: dict, kind: str, *, maximum: int = 1000) -> tuple[list, dict] | JSONResponse:
    """One page of ``items`` plus the ``response_metadata`` Slack returns beside it.

    Cursors are opaque (base64, like Slack's) so an agent that tries to compute
    the next one instead of echoing ``next_cursor`` fails here as it would in
    production.
    """
    limit = _number(args, "limit", int(TWIN.config.get("page_size") or 100))
    if limit is None or limit <= 0:
        return slack_error("invalid_limit")
    limit = min(limit, maximum)
    start = 0
    cursor = _text(args, "cursor")
    if cursor:
        decoded = _decode_cursor(cursor, kind)
        if decoded is None:
            return slack_error("invalid_cursor")
        start = decoded
    window = items[start:start + limit]
    has_more = start + limit < len(items)
    return window, {"next_cursor": _encode_cursor(kind, start + limit) if has_more else ""}


# --- messages ------------------------------------------------------------

def _message_key(message: dict) -> Decimal:
    return _ts_num(message.get("ts")) or Decimal(0)


def _thread_root(message: dict) -> str:
    return str(message.get("thread_ts") or message.get("ts"))


def _visible_history(channel_id: str) -> list[dict]:
    """Top-level messages, newest first — replies belong to conversations.replies."""
    messages = [m for m in STATE["messages"].get(channel_id, [])
                if _thread_root(m) == m.get("ts") or m.get("subtype") == "thread_broadcast"]
    return sorted(messages, key=_message_key, reverse=True)


def _in_range(messages: list[dict], args: dict) -> list[dict] | JSONResponse:
    """Apply ``oldest``/``latest``/``inclusive`` the way the real API does."""
    oldest = _ts_num(_text(args, "oldest") or "0")
    if oldest is None:
        return slack_error("invalid_ts_oldest")
    latest_arg = _text(args, "latest")
    latest = _ts_num(latest_arg) if latest_arg else None
    if latest_arg and latest is None:
        return slack_error("invalid_ts_latest")
    inclusive = _flag(args, "inclusive")

    def keep(message: dict) -> bool:
        ts = _message_key(message)
        if ts < oldest or (ts == oldest and not inclusive):
            return False
        if latest is not None and (ts > latest or (ts == latest and not inclusive)):
            return False
        return True

    return [m for m in messages if keep(m)]


def _message_body(args: dict) -> tuple[dict, JSONResponse | None]:
    """The text/blocks/attachments trio, or the error Slack answers with."""
    fields: dict[str, Any] = {}
    text = args.get("text")
    if text is not None:
        fields["text"] = str(text)
    for key in ("blocks", "attachments"):
        if args.get(key) in (None, ""):
            continue
        parsed = _structured(args, key)
        if not isinstance(parsed, list):
            return {}, slack_error("invalid_blocks" if key == "blocks" else "invalid_attachments")
        fields[key] = parsed
    if not fields.get("text") and not fields.get("blocks") and not fields.get("attachments"):
        return {}, slack_error("no_text")
    # Slack always echoes a text field, even when the message is blocks-only.
    fields.setdefault("text", "")
    return fields, None


def _new_message(channel: dict, fields: dict) -> dict:
    """A message posted by the app: the bot user's, with the bot's identity on it."""
    message = {
        "type": "message",
        "user": BOT_USER_ID,
        "bot_id": BOT_ID,
        "app_id": APP_ID,
        "team": STATE["team"]["id"],
        "ts": _ts(),
        # Messages are stored per channel; carrying the id keeps a message
        # self-describing wherever state is read (seeds do the same).
        "channel": channel["id"],
    }
    message.update(fields)
    return message


def _record_reply(parent: dict, reply: dict) -> None:
    """Keep the thread parent's reply bookkeeping in step, as Slack does."""
    parent["thread_ts"] = parent["ts"]
    parent["reply_count"] = parent.get("reply_count", 0) + 1
    users = parent.setdefault("reply_users", [])
    if reply["user"] not in users:
        users.append(reply["user"])
    parent["reply_users_count"] = len(users)
    parent["latest_reply"] = reply["ts"]


def _permalink(channel_id: str, message: dict) -> str:
    ts = str(message["ts"]).replace(".", "")
    link = f"{_team_url()}archives/{channel_id}/p{ts}"
    root = _thread_root(message)
    if root != message["ts"]:
        link += f"?thread_ts={root}&cid={channel_id}"
    return link


# --- auth, workspace -----------------------------------------------------

def _auth_test(args: dict, request: Request) -> dict:
    team = STATE["team"]
    return slack_ok(url=_team_url(), team=team.get("name"), user=_bot().get("name"),
                    team_id=team.get("id"), user_id=BOT_USER_ID, bot_id=BOT_ID,
                    is_enterprise_install=False)


def _api_test(args: dict, request: Request) -> dict | JSONResponse:
    """Echoes its arguments; ``error`` makes it fail, which is what it is for."""
    error = _text(args, "error")
    echoed = {k: v for k, v in args.items() if k != "token"}
    return slack_error(error, args=echoed) if error else slack_ok(args=echoed)


def _team_info(args: dict, request: Request) -> dict:
    team = STATE["team"]
    return slack_ok(team={"id": team.get("id"), "name": team.get("name"),
                          "domain": team.get("domain"),
                          "email_domain": team.get("email_domain", ""), "icon": {}})


# --- chat ----------------------------------------------------------------

def _chat_post_message(args: dict, request: Request) -> dict | JSONResponse:
    target = _text(args, "channel")
    if not target:
        return slack_error("channel_not_found")
    channel = _resolve_target(target)
    if channel is None:
        return slack_error("channel_not_found")
    if channel.get("is_archived"):
        return slack_error("is_archived")
    fields, problem = _message_body(args)
    if problem is not None:
        return problem

    thread_ts = _text(args, "thread_ts")
    parent = None
    if thread_ts:
        parent = _find_message(channel["id"], thread_ts)
        if parent is None:
            return slack_error("thread_not_found")
        fields["thread_ts"] = _thread_root(parent)
        fields["parent_user_id"] = parent.get("user")
        if _flag(args, "reply_broadcast"):
            fields["subtype"] = "thread_broadcast"

    message = _new_message(channel, fields)
    if parent is not None:
        _record_reply(parent, message)
    STATE["messages"].setdefault(channel["id"], []).append(message)
    return slack_ok(channel=channel["id"], ts=message["ts"], message=message)


def _chat_post_ephemeral(args: dict, request: Request) -> dict | JSONResponse:
    channel = _resolve_target(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    user = _find_user(_text(args, "user"))
    if user is None:
        return slack_error("user_not_found")
    fields, problem = _message_body(args)
    if problem is not None:
        return problem
    ephemeral = {"channel": channel["id"], "user": user["id"], "ts": _ts(),
                 "sent_by": BOT_USER_ID, **fields}
    if _text(args, "thread_ts"):
        ephemeral["thread_ts"] = _text(args, "thread_ts")
    STATE["ephemeral_messages"].append(ephemeral)
    return slack_ok(message_ts=ephemeral["ts"])


def _chat_update(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    ts = _text(args, "ts")
    if not ts:
        return _missing("ts")
    message = _find_message(channel["id"], ts)
    if message is None or message.get("subtype") == "tombstone":
        return slack_error("message_not_found")
    if message.get("user") != BOT_USER_ID:
        # A bot token may only edit what that bot posted.
        return slack_error("cant_update_message")
    fields, problem = _message_body(args)
    if problem is not None:
        return problem
    message.update(fields)
    message["edited"] = {"user": BOT_USER_ID, "ts": _ts()}
    return slack_ok(channel=channel["id"], ts=ts, text=message.get("text", ""), message=message)


def _chat_delete(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    ts = _text(args, "ts")
    if not ts:
        return _missing("ts")
    message = _find_message(channel["id"], ts)
    if message is None or message.get("subtype") == "tombstone":
        return slack_error("message_not_found")
    if message.get("user") != BOT_USER_ID:
        return slack_error("cant_delete_message")
    if message.get("reply_count"):
        # Slack keeps a thread parent as a tombstone so its replies stay reachable.
        for key in ("text", "blocks", "attachments", "files"):
            message.pop(key, None)
        message.update({"subtype": "tombstone", "text": "This message was deleted.",
                        "hidden": True})
    else:
        STATE["messages"][channel["id"]].remove(message)
    return slack_ok(channel=channel["id"], ts=ts)


def _chat_get_permalink(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    ts = _text(args, "message_ts")
    message = _find_message(channel["id"], ts) if ts else None
    if message is None:
        return slack_error("message_not_found")
    return slack_ok(channel=channel["id"], permalink=_permalink(channel["id"], message))


def _chat_schedule_message(args: dict, request: Request) -> dict | JSONResponse:
    channel = _resolve_target(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    post_at = _number(args, "post_at")
    if post_at is None:
        return _missing("post_at")
    if post_at <= _now():
        return slack_error("time_in_past")
    if post_at > _now() + _MAX_SCHEDULE_SECONDS:
        return slack_error("time_too_far")
    fields, problem = _message_body(args)
    if problem is not None:
        return problem
    scheduled_id = _new_id("scheduled_id", "Q")
    record = {"id": scheduled_id, "channel_id": channel["id"], "post_at": post_at,
              "date_created": _now(), "text": fields.get("text", ""),
              "thread_ts": _text(args, "thread_ts") or None}
    STATE["scheduled_messages"][scheduled_id] = record
    message = {"bot_id": BOT_ID, "type": "delayed_message", "subtype": "bot_message", **fields}
    return slack_ok(channel=channel["id"], scheduled_message_id=scheduled_id,
                    post_at=str(post_at), message=message)


def _chat_scheduled_messages_list(args: dict, request: Request) -> dict | JSONResponse:
    records = sorted(STATE["scheduled_messages"].values(), key=lambda r: (r["post_at"], r["id"]))
    channel = _text(args, "channel")
    if channel:
        found = _find_channel(channel)
        if found is None:
            return slack_error("channel_not_found")
        records = [r for r in records if r["channel_id"] == found["id"]]
    page = _page(records, args, "scheduled")
    if isinstance(page, JSONResponse):
        return page
    window, metadata = page
    return slack_ok(scheduled_messages=window, response_metadata=metadata)


def _chat_delete_scheduled_message(args: dict, request: Request) -> dict | JSONResponse:
    scheduled_id = _text(args, "scheduled_message_id")
    if scheduled_id not in STATE["scheduled_messages"]:
        return slack_error("invalid_scheduled_message_id")
    del STATE["scheduled_messages"][scheduled_id]
    return slack_ok()


# --- conversations -------------------------------------------------------

_DEFAULT_TYPES = "public_channel"


def _conversations_list(args: dict, request: Request) -> dict | JSONResponse:
    wanted = set(_ids(_text(args, "types") or _DEFAULT_TYPES))
    exclude_archived = _flag(args, "exclude_archived")
    channels = [ch for ch in STATE["channels"].values() if _channel_type(ch) in wanted]
    if exclude_archived:
        channels = [ch for ch in channels if not ch.get("is_archived")]
    channels.sort(key=lambda ch: ch["id"])
    page = _page(channels, args, "channels", maximum=1000)
    if isinstance(page, JSONResponse):
        return page
    window, metadata = page
    return slack_ok(channels=[_serialize_channel(ch) for ch in window],
                    response_metadata=metadata)


def _conversations_info(args: dict, request: Request) -> dict | JSONResponse:
    channel = _text(args, "channel")
    if not channel:
        return _missing("channel")
    found = _find_channel(channel)
    if found is None:
        return slack_error("channel_not_found")
    return slack_ok(channel=_serialize_channel(found))


def _conversations_create(args: dict, request: Request) -> dict | JSONResponse:
    raw = _text(args, "name")
    if not raw:
        return slack_error("invalid_name_required")
    name = raw.lstrip("#").strip().lower()
    if len(name) > _MAX_CHANNEL_NAME:
        return slack_error("invalid_name_maxlength")
    if not _CHANNEL_NAME_RE.match(name):
        return slack_error("invalid_name")
    if any(ch.get("name") == name for ch in STATE["channels"].values()):
        return slack_error("name_taken")
    channel = _make_channel(name, is_private=_flag(args, "is_private"))
    return slack_ok(channel=_serialize_channel(channel))


def _conversations_join(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    if channel.get("is_im") or channel.get("is_mpim"):
        return slack_error("method_not_supported_for_channel_type")
    if channel.get("is_archived"):
        return slack_error("is_archived")
    members = channel.setdefault("members", [])
    if BOT_USER_ID in members:
        return slack_ok(channel=_serialize_channel(channel), warning="already_in_channel",
                        response_metadata={"warnings": ["already_in_channel"]})
    members.append(BOT_USER_ID)
    _adjust_members(channel, 1)
    return slack_ok(channel=_serialize_channel(channel))


def _conversations_invite(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    if channel.get("is_archived"):
        return slack_error("is_archived")
    raw = _ids(_text(args, "users"))
    if not raw:
        return _missing("users")
    invited = []
    for ident in raw:
        user = _find_user(ident)
        if user is None:
            return slack_error("user_not_found")
        if user["id"] == BOT_USER_ID:
            return slack_error("cant_invite_self")
        invited.append(user["id"])
    members = channel.setdefault("members", [])
    already = [uid for uid in invited if uid in members]
    if already:
        return slack_error("already_in_channel",
                           errors=[{"user": uid, "ok": False, "error": "already_in_channel"}
                                   for uid in already])
    members.extend(invited)
    _adjust_members(channel, len(invited))
    return slack_ok(channel=_serialize_channel(channel))


def _conversations_kick(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    user = _find_user(_text(args, "user"))
    if user is None:
        return slack_error("user_not_found")
    if user["id"] == BOT_USER_ID:
        return slack_error("cant_kick_self")
    if channel.get("is_general"):
        return slack_error("cant_kick_from_general")
    members = channel.setdefault("members", [])
    if user["id"] not in members:
        return slack_error("not_in_channel")
    members.remove(user["id"])
    _adjust_members(channel, -1)
    return slack_ok()


def _conversations_leave(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    if channel.get("is_general"):
        return slack_error("cant_leave_general")
    members = channel.setdefault("members", [])
    if BOT_USER_ID not in members:
        return slack_ok(not_in_channel=True)
    members.remove(BOT_USER_ID)
    _adjust_members(channel, -1)
    return slack_ok()


def _conversations_members(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    page = _page(list(channel.get("members") or []), args, "members")
    if isinstance(page, JSONResponse):
        return page
    window, metadata = page
    return slack_ok(members=window, response_metadata=metadata)


def _set_channel_text(args: dict, channel: dict, field: str) -> dict | JSONResponse:
    value = args.get(field)
    if value is None:
        return _missing(field)
    value = str(value)
    if len(value) > 250:
        return slack_error("too_long")
    channel[field] = {"value": value, "creator": BOT_USER_ID, "last_set": _now()}
    return slack_ok(channel=_serialize_channel(channel))


def _conversations_set_topic(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    if channel.get("is_archived"):
        return slack_error("is_archived")
    return _set_channel_text(args, channel, "topic")


def _conversations_set_purpose(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    if channel.get("is_archived"):
        return slack_error("is_archived")
    return _set_channel_text(args, channel, "purpose")


def _conversations_rename(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    name = _text(args, "name").lstrip("#").lower()
    if not name or not _CHANNEL_NAME_RE.match(name):
        return slack_error("invalid_name")
    if any(ch.get("name") == name and ch["id"] != channel["id"]
           for ch in STATE["channels"].values()):
        return slack_error("name_taken")
    channel.setdefault("previous_names", []).append(channel.get("name", ""))
    channel["name"] = name
    channel["name_normalized"] = name
    return slack_ok(channel=_serialize_channel(channel))


def _conversations_archive(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    if channel.get("is_im"):
        return slack_error("method_not_supported_for_channel_type")
    if channel.get("is_general"):
        return slack_error("cant_archive_general")
    if channel.get("is_archived"):
        return slack_error("already_archived")
    channel["is_archived"] = True
    return slack_ok()


def _conversations_unarchive(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    if not channel.get("is_archived"):
        return slack_error("not_archived")
    channel["is_archived"] = False
    return slack_ok()


def _conversations_open(args: dict, request: Request) -> dict | JSONResponse:
    existing = _text(args, "channel")
    if existing:
        channel = _find_channel(existing)
        if channel is None:
            return slack_error("channel_not_found")
        return _opened(channel, args, already_open=True)
    users = _ids(_text(args, "users"))
    if not users:
        return _missing("users")
    if len(users) > 8:
        return slack_error("too_many_users")
    resolved = []
    for ident in users:
        user = _find_user(ident)
        if user is None:
            return slack_error("user_not_found")
        resolved.append(user["id"])
    if _flag(args, "prevent_creation"):
        found = next((ch for ch in STATE["channels"].values()
                      if ch.get("is_im") and ch.get("user") in resolved), None)
        if found is None:
            return slack_error("channel_not_found")
        return _opened(found, args, already_open=True)
    known = len(STATE["channels"])
    channel = _open_im(resolved[0]) if len(resolved) == 1 else _open_mpim(resolved)
    return _opened(channel, args, already_open=len(STATE["channels"]) == known)


def _opened(channel: dict, args: dict, *, already_open: bool) -> dict:
    """conversations.open returns just the id unless the caller asks for more."""
    if not _flag(args, "return_im"):
        return slack_ok(channel={"id": channel["id"]})
    body = slack_ok(channel=_serialize_channel(channel))
    if already_open:
        body.update({"no_op": True, "already_open": True})
    return body


def _conversations_history(args: dict, request: Request) -> dict | JSONResponse:
    channel = _text(args, "channel")
    if not channel:
        return _missing("channel")
    found = _find_channel(channel)
    if found is None:
        return slack_error("channel_not_found")
    messages = _in_range(_visible_history(found["id"]), args)
    if isinstance(messages, JSONResponse):
        return messages
    page = _page(messages, args, "history", maximum=999)
    if isinstance(page, JSONResponse):
        return page
    window, metadata = page
    pinned = sum(1 for m in STATE["messages"].get(found["id"], []) if m.get("pinned_to"))
    return slack_ok(messages=window, has_more=bool(metadata["next_cursor"]),
                    pin_count=pinned, response_metadata=metadata)


def _conversations_replies(args: dict, request: Request) -> dict | JSONResponse:
    channel = _text(args, "channel")
    if not channel:
        return _missing("channel")
    found = _find_channel(channel)
    if found is None:
        return slack_error("channel_not_found")
    ts = _text(args, "ts")
    if not ts:
        return _missing("ts")
    anchor = _find_message(found["id"], ts)
    if anchor is None:
        return slack_error("thread_not_found")
    root = _thread_root(anchor)
    thread = [m for m in STATE["messages"].get(found["id"], [])
              if m.get("ts") == root or m.get("thread_ts") == root]
    thread.sort(key=_message_key)
    messages = _in_range(thread, args)
    if isinstance(messages, JSONResponse):
        return messages
    page = _page(messages, args, "replies", maximum=1000)
    if isinstance(page, JSONResponse):
        return page
    window, metadata = page
    return slack_ok(messages=window, has_more=bool(metadata["next_cursor"]),
                    response_metadata=metadata)


# --- reactions -----------------------------------------------------------

def _reaction_target(args: dict) -> tuple[dict, dict] | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    timestamp = _text(args, "timestamp")
    if not channel:
        return slack_error("channel_not_found")
    if not timestamp:
        return slack_error("no_item_specified")
    message = _find_message(channel["id"], timestamp)
    if message is None:
        return slack_error("message_not_found")
    return channel, message


def _reactions_add(args: dict, request: Request) -> dict | JSONResponse:
    name = _text(args, "name")
    if not name:
        return slack_error("invalid_name")
    target = _reaction_target(args)
    if isinstance(target, JSONResponse):
        return target
    _, message = target
    reactions = message.setdefault("reactions", [])
    existing = next((r for r in reactions if r["name"] == name), None)
    if existing is None:
        reactions.append({"name": name, "users": [BOT_USER_ID], "count": 1})
        return slack_ok()
    if BOT_USER_ID in existing["users"]:
        return slack_error("already_reacted")
    existing["users"].append(BOT_USER_ID)
    existing["count"] = len(existing["users"])
    return slack_ok()


def _reactions_remove(args: dict, request: Request) -> dict | JSONResponse:
    name = _text(args, "name")
    if not name:
        return slack_error("invalid_name")
    target = _reaction_target(args)
    if isinstance(target, JSONResponse):
        return target
    _, message = target
    reactions = message.get("reactions") or []
    existing = next((r for r in reactions if r["name"] == name), None)
    if existing is None or BOT_USER_ID not in existing["users"]:
        return slack_error("no_reaction")
    existing["users"].remove(BOT_USER_ID)
    existing["count"] = len(existing["users"])
    if not existing["users"]:
        reactions.remove(existing)
    if not reactions:
        message.pop("reactions", None)
    return slack_ok()


def _reactions_get(args: dict, request: Request) -> dict | JSONResponse:
    target = _reaction_target(args)
    if isinstance(target, JSONResponse):
        return target
    channel, message = target
    return slack_ok(type="message", channel=channel["id"], message=message)


# --- users ---------------------------------------------------------------

def _users_list(args: dict, request: Request) -> dict | JSONResponse:
    members = sorted(STATE["users"].values(), key=lambda u: u["id"])
    page = _page(members, args, "users")
    if isinstance(page, JSONResponse):
        return page
    window, metadata = page
    return slack_ok(members=window, cache_ts=_now(), response_metadata=metadata)


def _users_info(args: dict, request: Request) -> dict | JSONResponse:
    user = _text(args, "user")
    if not user:
        return _missing("user")
    found = _find_user(user)
    if found is None:
        return slack_error("user_not_found")
    return slack_ok(user=found)


def _users_lookup_by_email(args: dict, request: Request) -> dict | JSONResponse:
    email = _text(args, "email").lower()
    if not email:
        return _missing("email")
    for user in STATE["users"].values():
        candidate = (user.get("profile") or {}).get("email") or user.get("email") or ""
        if str(candidate).lower() == email:
            return slack_ok(user=user)
    return slack_error("users_not_found")


def _users_profile_get(args: dict, request: Request) -> dict | JSONResponse:
    ident = _text(args, "user") or BOT_USER_ID
    user = _find_user(ident)
    if user is None:
        return slack_error("user_not_found")
    profile = user.get("profile") or {
        "real_name": user.get("real_name", user.get("name", "")),
        "display_name": user.get("name", ""),
        "email": user.get("email", ""),
    }
    return slack_ok(profile=profile)


def _users_conversations(args: dict, request: Request) -> dict | JSONResponse:
    ident = _text(args, "user") or BOT_USER_ID
    user = _find_user(ident)
    if user is None:
        return slack_error("user_not_found")
    wanted = set(_ids(_text(args, "types") or _DEFAULT_TYPES))
    channels = [ch for ch in STATE["channels"].values()
                if _channel_type(ch) in wanted and user["id"] in (ch.get("members") or [])]
    if _flag(args, "exclude_archived"):
        channels = [ch for ch in channels if not ch.get("is_archived")]
    channels.sort(key=lambda ch: ch["id"])
    page = _page(channels, args, "channels")
    if isinstance(page, JSONResponse):
        return page
    window, metadata = page
    return slack_ok(channels=[_serialize_channel(ch) for ch in window],
                    response_metadata=metadata)


# --- files ---------------------------------------------------------------

def _files_get_upload_url_external(args: dict, request: Request) -> dict | JSONResponse:
    filename = _text(args, "filename")
    if not filename:
        return _missing("filename")
    file_id = _new_id("file_id", "F")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file = {
        "id": file_id,
        "created": _now(),
        "timestamp": _now(),
        "name": filename,
        "title": filename,
        "mimetype": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "filetype": extension,
        "pretty_type": extension.upper() or "Binary",
        "user": BOT_USER_ID,
        "user_team": TEAM_ID,
        "size": _number(args, "length", 0) or 0,
        "mode": "hosted",
        "is_external": False,
        "external_type": "",
        "is_public": False,
        "public_url_shared": False,
        "display_as_bot": False,
        "username": "",
        "channels": [],
        "groups": [],
        "ims": [],
        "comments_count": 0,
        "url_private": f"{_team_url()}files-pri/{TEAM_ID}-{file_id}/{filename}",
        "permalink": f"{_team_url()}files/{BOT_USER_ID}/{file_id}/{filename}",
        "alt_txt": _text(args, "alt_txt"),
    }
    STATE["files"][file_id] = file
    base = str(request.base_url).rstrip("/")
    return slack_ok(upload_url=f"{base}/upload/v1/{file_id}", file_id=file_id)


def _files_complete_upload_external(args: dict, request: Request) -> dict | JSONResponse:
    requested = _structured(args, "files")
    if not isinstance(requested, list) or not requested:
        return _missing("files")
    files = []
    for item in requested:
        if not isinstance(item, dict):
            return slack_error("invalid_array_arg")
        file = STATE["files"].get(str(item.get("id")))
        if file is None:
            return slack_error("file_not_found")
        if item.get("title"):
            file["title"] = str(item["title"])
        files.append(file)

    targets = []
    for ident in [_text(args, "channel_id"), *_ids(_text(args, "channels"))]:
        if not ident:
            continue
        channel = _resolve_target(ident)
        if channel is None:
            return slack_error("channel_not_found")
        targets.append(channel)

    for channel in targets:
        for file in files:
            if channel["id"] not in file["channels"]:
                file["channels"].append(channel["id"])
        fields: dict[str, Any] = {"text": _text(args, "initial_comment"),
                                  "subtype": "file_share", "upload": True,
                                  "files": [_serialize_file(f) for f in files]}
        thread_ts = _text(args, "thread_ts")
        parent = _find_message(channel["id"], thread_ts) if thread_ts else None
        if parent is not None:
            fields["thread_ts"] = _thread_root(parent)
        message = _new_message(channel, fields)
        if parent is not None:
            _record_reply(parent, message)
        STATE["messages"].setdefault(channel["id"], []).append(message)
    return slack_ok(files=[_serialize_file(f) for f in files])


def _files_info(args: dict, request: Request) -> dict | JSONResponse:
    file = STATE["files"].get(_text(args, "file"))
    if file is None:
        return slack_error("file_not_found")
    return slack_ok(file=_serialize_file(file), comments=[])


def _files_list(args: dict, request: Request) -> dict | JSONResponse:
    files = sorted(STATE["files"].values(), key=lambda f: f["id"], reverse=True)
    channel = _text(args, "channel")
    if channel:
        found = _find_channel(channel)
        if found is None:
            return slack_error("channel_not_found")
        files = [f for f in files if found["id"] in f.get("channels", [])]
    count = min(max(_number(args, "count", 100) or 100, 1), 1000)
    page_number = max(1, _number(args, "page", 1) or 1)
    window = files[(page_number - 1) * count: page_number * count]
    pages = max(1, -(-len(files) // count))
    return slack_ok(files=[_serialize_file(f) for f in window],
                    paging={"count": count, "total": len(files),
                            "page": page_number, "pages": pages})


def _files_delete(args: dict, request: Request) -> dict | JSONResponse:
    file_id = _text(args, "file")
    if file_id not in STATE["files"]:
        return slack_error("file_not_found")
    del STATE["files"][file_id]
    for messages in STATE["messages"].values():
        for message in messages:
            attached = message.get("files")
            if attached:
                message["files"] = [f for f in attached if f.get("id") != file_id]
    return slack_ok()


@app.post("/upload/v1/{file_id}", include_in_schema=False)
async def upload_file_content(file_id: str, request: Request) -> Response:
    """The edge-upload endpoint ``files.getUploadURLExternal`` hands out.

    ``files_upload_v2`` POSTs the raw bytes here with no credential and only
    checks for HTTP 200, so the reply is the plain-text one Slack sends.
    """
    file = STATE["files"].get(file_id)
    if file is None:
        return PlainTextResponse("no such upload", status_code=404)
    data = await request.body()
    file["size"] = len(data)
    file["preview"] = data.decode("utf-8", errors="replace")[:500]
    return PlainTextResponse(f"OK - {len(data)}")


# --- search --------------------------------------------------------------

_SEARCH_TOKEN = re.compile(r'"[^"]*"|\S+')


def _search_plan(query: str) -> tuple[list[str], str | None, str | None]:
    """Split a query into free-text terms plus the ``in:``/``from:`` filters."""
    terms: list[str] = []
    channel_id: str | None = None
    user_id: str | None = None
    for raw in _SEARCH_TOKEN.findall(query):
        token = raw.strip('"')
        if token.startswith("in:"):
            found = _find_channel(token[3:])
            channel_id = found["id"] if found else "<none>"
        elif token.startswith("from:"):
            found = _find_user(token[5:])
            user_id = found["id"] if found else "<none>"
        elif token:
            terms.append(token.lower())
    return terms, channel_id, user_id


def _search_messages(args: dict, request: Request) -> dict | JSONResponse:
    query = _text(args, "query")
    if not query:
        return slack_error("no_query")
    terms, channel_id, user_id = _search_plan(query)
    matches = []
    for cid, messages in STATE["messages"].items():
        channel = STATE["channels"].get(cid)
        if channel is None or (channel_id and cid != channel_id):
            continue
        for message in messages:
            if user_id and message.get("user") != user_id:
                continue
            text = str(message.get("text") or "")
            if not all(term in text.lower() for term in terms):
                continue
            matches.append({
                "type": "message",
                "iid": uuid.uuid4().hex,
                "team": STATE["team"]["id"],
                "channel": {"id": cid, "name": channel.get("name", ""),
                            "is_private": bool(channel.get("is_private")),
                            "is_mpim": bool(channel.get("is_mpim"))},
                "user": message.get("user", ""),
                "username": str((STATE["users"].get(message.get("user", "")) or {}).get("name", "")),
                "ts": message["ts"],
                "text": text,
                "permalink": _permalink(cid, message),
            })
    reverse = _text(args, "sort_dir") != "asc"
    matches.sort(key=lambda m: _ts_num(m["ts"]) or Decimal(0), reverse=reverse)

    count = min(max(_number(args, "count", 20) or 20, 1), 100)
    page_number = max(1, _number(args, "page", 1) or 1)
    window = matches[(page_number - 1) * count: page_number * count]
    pages = max(1, -(-len(matches) // count))
    first = (page_number - 1) * count + 1
    return slack_ok(query=query, messages={
        "total": len(matches),
        "matches": window,
        "pagination": {"total_count": len(matches), "page": page_number, "per_page": count,
                       "page_count": pages, "first": first, "last": first + len(window) - 1},
        "paging": {"count": count, "total": len(matches), "page": page_number, "pages": pages},
    })


# --- pins ----------------------------------------------------------------

def _pins_add(args: dict, request: Request) -> dict | JSONResponse:
    target = _reaction_target(args)
    if isinstance(target, JSONResponse):
        return target
    channel, message = target
    pinned = message.setdefault("pinned_to", [])
    if channel["id"] in pinned:
        return slack_error("already_pinned")
    pinned.append(channel["id"])
    message["pinned_info"] = {"channel": channel["id"], "pinned_by": BOT_USER_ID,
                              "pinned_ts": _now()}
    return slack_ok()


def _pins_remove(args: dict, request: Request) -> dict | JSONResponse:
    target = _reaction_target(args)
    if isinstance(target, JSONResponse):
        return target
    channel, message = target
    pinned = message.get("pinned_to") or []
    if channel["id"] not in pinned:
        return slack_error("no_pin")
    pinned.remove(channel["id"])
    if not pinned:
        message.pop("pinned_to", None)
        message.pop("pinned_info", None)
    return slack_ok()


def _pins_list(args: dict, request: Request) -> dict | JSONResponse:
    channel = _find_channel(_text(args, "channel"))
    if channel is None:
        return slack_error("channel_not_found")
    items = [{"type": "message", "channel": channel["id"], "message": m,
              "created": (m.get("pinned_info") or {}).get("pinned_ts", _now()),
              "created_by": (m.get("pinned_info") or {}).get("pinned_by", BOT_USER_ID)}
             for m in STATE["messages"].get(channel["id"], [])
             if channel["id"] in (m.get("pinned_to") or [])]
    return slack_ok(items=items)


# --- method table and dispatch -------------------------------------------

Handler = Callable[[dict, Request], "dict | Response"]

_METHODS: dict[str, Handler] = {
    "api.test": _api_test,
    "auth.test": _auth_test,
    "team.info": _team_info,
    "chat.postMessage": _chat_post_message,
    "chat.postEphemeral": _chat_post_ephemeral,
    "chat.update": _chat_update,
    "chat.delete": _chat_delete,
    "chat.getPermalink": _chat_get_permalink,
    "chat.scheduleMessage": _chat_schedule_message,
    "chat.scheduledMessages.list": _chat_scheduled_messages_list,
    "chat.deleteScheduledMessage": _chat_delete_scheduled_message,
    "conversations.list": _conversations_list,
    "conversations.info": _conversations_info,
    "conversations.create": _conversations_create,
    "conversations.join": _conversations_join,
    "conversations.invite": _conversations_invite,
    "conversations.kick": _conversations_kick,
    "conversations.leave": _conversations_leave,
    "conversations.members": _conversations_members,
    "conversations.setTopic": _conversations_set_topic,
    "conversations.setPurpose": _conversations_set_purpose,
    "conversations.rename": _conversations_rename,
    "conversations.archive": _conversations_archive,
    "conversations.unarchive": _conversations_unarchive,
    "conversations.open": _conversations_open,
    "conversations.history": _conversations_history,
    "conversations.replies": _conversations_replies,
    "reactions.add": _reactions_add,
    "reactions.remove": _reactions_remove,
    "reactions.get": _reactions_get,
    "users.list": _users_list,
    "users.info": _users_info,
    "users.lookupByEmail": _users_lookup_by_email,
    "users.profile.get": _users_profile_get,
    "users.conversations": _users_conversations,
    "files.getUploadURLExternal": _files_get_upload_url_external,
    "files.completeUploadExternal": _files_complete_upload_external,
    "files.info": _files_info,
    "files.list": _files_list,
    "files.delete": _files_delete,
    "search.messages": _search_messages,
    "pins.add": _pins_add,
    "pins.remove": _pins_remove,
    "pins.list": _pins_list,
}


@app.api_route("/api/{method}", methods=["GET", "POST"], include_in_schema=False)
async def api_method(method: str, request: Request) -> Response:
    """Every Slack method: one path, both verbs, arguments from anywhere."""
    handler = _METHODS.get(method)
    if handler is None:
        # Slack answers an unimplemented method with ok:false, never a 404 page.
        return slack_error("unknown_method")
    args = await _args(request)
    if isinstance(args, Response):
        return args
    result = handler(args, request)
    return JSONResponse(result) if isinstance(result, dict) else result


@app.exception_handler(StarletteHTTPException)
async def _slack_shaped_http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """Keep FastAPI's ``{"detail": ...}`` off the wire — SDKs parse Slack's shape."""
    if exc.status_code == 404:
        return slack_error("unknown_method")
    return slack_error("fatal_error", status=exc.status_code)


# --- MCP transport -------------------------------------------------------
# Mount the Slack MCP server at /mcp on this same FastAPI app so REST and
# MCP share the same STATE dict (Phase 6, MCP-01/MCP-02).

from checkpoint.mcp_servers.slack_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
