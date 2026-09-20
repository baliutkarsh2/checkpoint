"""Google Workspace twin: a stateful, in-memory Gmail, Drive and Calendar API.

Serves the surfaces agents reach for through the official clients, on the paths
the real services use, so ``google-api-python-client`` (and the Node/Go clients)
work unmodified:

    Gmail     /gmail/v1/users/{userId}/...    messages, threads, labels, drafts, history
    Drive     /drive/v3/...                   files, uploads, downloads, permissions
    Calendar  /calendar/v3/...                calendars, events, free/busy
    uploads   /upload/{api}/...               media, multipart and resumable uploads
    batch     /batch, /batch/{api}/{version}  multipart batch fan-out
    OAuth     /token                          token exchange (oauth2.googleapis.com)

A message is stored once, in the shape the API returns it (a MIME payload tree
with base64url bodies), so ``format=full|metadata|minimal|raw`` are renderings of
that one copy rather than separate state. Authentication accepts an OAuth 2.0
Bearer token (or an ``access_token`` query param). The control plane and fault
model come from :mod:`checkpoint.twins.kit`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from checkpoint.fake_credentials import FAKE_GOOGLE_WORKSPACE_TOKEN
from checkpoint.twins import kit

app = FastAPI(title="checkpoint google-workspace twin")

DEFAULT_BOOTSTRAP_TOKEN = FAKE_GOOGLE_WORKSPACE_TOKEN
DEFAULT_EMAIL = "user@checkpoint.test"

SEEDS_DIR = Path(__file__).parent / "google_workspace_seeds"

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"
ROOT_ID = "root"

# Gmail's built-in labels. They exist in every mailbox and cannot be renamed or
# deleted; user labels get a ``Label_<n>`` id.
_SYSTEM_LABELS = (
    "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED", "UNREAD", "IMPORTANT", "CHAT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
)

# Paths served without a credential: the OAuth token endpoint mints them.
_PUBLIC_PATHS = frozenset({"/token", "/oauth2/v4/token", "/oauth2/v3/token", "/o/oauth2/token"})


def _fresh_state() -> dict:
    return {
        # Gmail
        "gmail_messages": {},   # message id -> message (id, threadId, labelIds, payload, ...)
        "gmail_threads": {},    # thread id -> thread (id, messageIds, snippet, labelIds)
        "gmail_labels": {lid: {"id": lid, "name": lid, "type": "system"} for lid in _SYSTEM_LABELS},
        "gmail_drafts": {},     # draft id -> {"id", "messageId"}
        # Drive
        "drive_files": {},      # file id -> file metadata (folders included)
        "drive_permissions": {},  # file id -> {permission id -> permission}
        "drive_content": {},    # file id -> str (text) or {"base64": "..."} (binary)
        # Calendar
        "calendars": {
            DEFAULT_EMAIL: {"id": DEFAULT_EMAIL, "summary": DEFAULT_EMAIL, "timeZone": "UTC",
                            "primary": True, "accessRole": "owner"},
        },
        "calendar_events": {},  # event id -> event (with the owning calendar in _calendarId)
        # Account
        "user_profile": {
            "emailAddress": DEFAULT_EMAIL,
            "displayName": "Checkpoint User",
            "messagesTotal": 0,
            "threadsTotal": 0,
            "historyId": "1",
        },
        "_history": [],         # Gmail history records, oldest first
        "_uploads": {},         # resumable upload session id -> pending upload
        "_counters": {},
        "_config": {"rate_limit": None},
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- errors ------------------------------------------------------------------

# Google's JSON error envelope carries both a legacy ``errors[]`` array (which
# SDKs read for the machine-readable reason) and a canonical ``status``.
_REASONS = {
    400: "badRequest", 401: "authError", 403: "forbidden", 404: "notFound",
    405: "httpMethodNotAllowed", 409: "duplicate", 410: "deleted", 412: "conditionNotMet",
    429: "rateLimitExceeded", 500: "backendError", 503: "backendError",
}
_STATUSES = {
    400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
    404: "NOT_FOUND", 405: "FAILED_PRECONDITION", 409: "ALREADY_EXISTS", 410: "NOT_FOUND",
    412: "FAILED_PRECONDITION", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 503: "UNAVAILABLE",
}


def google_error(status: int, message: str, *, reason: str | None = None,
                 location: str | None = None) -> JSONResponse:
    """Return a Google-shaped error response (SDKs parse ``error.message``)."""
    detail: dict[str, Any] = {
        "message": message, "domain": "global", "reason": reason or _REASONS.get(status, "failed"),
    }
    if location:
        detail["location"] = location
        detail["locationType"] = "parameter"
    return JSONResponse(status_code=status, content={"error": {
        "code": status, "message": message, "errors": [detail],
        "status": _STATUSES.get(status, "UNKNOWN"),
    }})


class ApiError(Exception):
    """Raised anywhere in a handler; rendered as Google's error envelope."""

    def __init__(self, status: int, message: str, *, reason: str | None = None,
                 location: str | None = None) -> None:
        super().__init__(message)
        self.response = google_error(status, message, reason=reason, location=location)


@app.exception_handler(ApiError)
def _handle_api_error(_request: Request, exc: ApiError) -> Response:
    return exc.response


@app.exception_handler(StarletteHTTPException)
def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> Response:
    # Unknown routes must look like Google, not like FastAPI's {"detail": ...}.
    message = "Not Found" if exc.status_code == 404 else str(exc.detail)
    return google_error(exc.status_code, message)


@app.exception_handler(RequestValidationError)
def _handle_validation_error(_request: Request, exc: RequestValidationError) -> Response:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", ())[1:]) or "request"
    return google_error(400, f"Invalid value for {field}: {first.get('msg', 'invalid')}",
                        reason="invalidParameter", location=field)


def _not_found(kind: str, ident: str) -> ApiError:
    return ApiError(404, f"{kind} not found: {ident}")


# --- auth --------------------------------------------------------------------

def _bootstrap_token() -> str:
    return os.environ.get("GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _request_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").removeprefix("bearer ").strip()
    return token or request.query_params.get("access_token") or request.query_params.get("key")


def _authenticate(request: Request) -> Response | None:
    if request.url.path in _PUBLIC_PATHS:
        return None
    token = _request_token(request)
    if not token:
        return google_error(401, "Request is missing required authentication credential. "
                                 "Expected OAuth 2 access token, login cookie or other valid "
                                 "authentication credential.", reason="required")
    if TWIN.config.get("strict_auth") and token != _bootstrap_token():
        return google_error(401, "Request had invalid authentication credentials. Expected "
                                 "OAuth 2 access token, login cookie or other valid "
                                 "authentication credential.", reason="authError")
    return None


def _error(kind: str, status: int, message: str) -> Response:
    """Uniform faults, shaped like the service reports them."""
    if kind == "rate_limited":
        return google_error(429, "User-rate limit exceeded.  Retry after "
                                 f"{_rfc3339(_now() + timedelta(seconds=30))}",
                            reason="rateLimitExceeded")
    if kind in ("forbidden", "read_only"):
        return google_error(403, message, reason="forbidden")
    return google_error(status, message)


def _me() -> str:
    return STATE["user_profile"]["emailAddress"]


def _check_user(user_id: str) -> None:
    """Gmail accepts ``me`` or the authenticated user's own address, nothing else."""
    if user_id != "me" and user_id.lower() != _me().lower():
        raise ApiError(403, f"Delegation denied for {user_id}", reason="forbidden")


# --- small helpers -----------------------------------------------------------

def _now() -> datetime:
    return datetime.now(UTC)


def _rfc3339(when: datetime | None = None, *, millis: bool = True) -> str:
    when = (when or _now()).astimezone(UTC)
    text = when.isoformat(timespec="milliseconds" if millis else "seconds")
    return text.replace("+00:00", "Z")


def _rfc2822(when: datetime | None = None) -> str:
    return format_datetime(when or _now())


def _counter(name: str) -> int:
    counters = STATE.setdefault("_counters", {})
    counters[name] = int(counters.get(name, 0)) + 1
    return counters[name]


def _unique(name: str, make: Callable[[int], str], taken: Iterable[str]) -> str:
    """A generated id that no seeded record already uses."""
    existing = set(taken)
    while True:
        candidate = make(_counter(name))
        if candidate not in existing:
            return candidate


def _gmail_id() -> str:
    # Gmail ids are 16 hex digits that grow over time; keep ours ordered too.
    return _unique("gmail_id", lambda n: f"{0x18f0a00000000 + n * 0x10001:x}",
                   {*STATE["gmail_messages"], *STATE["gmail_threads"]})


def _draft_id() -> str:
    return _unique("draft", lambda n: f"r-{7000000000000000000 + n}", STATE["gmail_drafts"])


def _label_id() -> str:
    return _unique("label", lambda n: f"Label_{n}", STATE["gmail_labels"])


def _file_id() -> str:
    def make(n: int) -> str:
        digest = hashlib.sha256(f"checkpoint-drive-{n}".encode()).digest()
        return "1" + base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]

    return _unique("drive_file", make, STATE["drive_files"])


def _permission_id() -> str:
    taken = {pid for perms in STATE["drive_permissions"].values() for pid in perms}
    return _unique("permission", lambda n: str(10000000000000000000 + n), taken)


def _event_id() -> str:
    # Calendar event ids are base32hex ([a-v0-9]); hex digits are a valid subset.
    return _unique("event", lambda n: hashlib.sha1(f"cp-event-{n}".encode()).hexdigest()[:26],  # noqa: S324 - an identifier the vendor shapes, not a secret
                   STATE["calendar_events"])


def _next_history_id() -> str:
    profile = STATE["user_profile"]
    profile["historyId"] = str(int(profile.get("historyId", "1")) + 1)
    return profile["historyId"]


async def _body(request: Request) -> dict:
    """The request's JSON body ({} when empty — PATCH with no body is legal)."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "Invalid JSON payload received.", reason="parseError") from None
    if not isinstance(data, dict):
        raise ApiError(400, "Invalid JSON payload received.", reason="parseError")
    return data


def _int_param(request: Request, name: str, default: int, *, maximum: int | None = None) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, f"Invalid value for {name}: {raw}", reason="invalidParameter",
                       location=name) from None
    if value < 0:
        raise ApiError(400, f"Invalid value for {name}: {raw}", reason="invalidParameter",
                       location=name)
    return min(value, maximum) if maximum is not None else value


def _bool_param(request: Request, name: str, default: bool = False) -> bool:
    raw = request.query_params.get(name)
    return default if raw is None else raw.lower() in ("1", "true", "yes")


def _repeated(request: Request, name: str) -> list[str]:
    """A repeated query parameter, also accepting one comma-separated value."""
    values: list[str] = []
    for raw in request.query_params.getlist(name):
        values.extend(part for part in raw.split(",") if part)
    return values


# --- partial responses (?fields=) --------------------------------------------

def _parse_fields(fields: str) -> dict | None:
    """Parse a field mask (``nextPageToken,files(id,name)``) into a nested mask.

    ``None`` (and an empty sub-mask) means "everything below here".
    """
    fields = fields.strip()
    if not fields or fields == "*":
        return None
    mask: dict[str, Any] = {}
    for item in _split_fields(fields):
        path, _, sub = item.partition("(")
        node = mask
        segments = [s for s in path.strip().split("/") if s]
        if not segments:
            continue
        for segment in segments[:-1]:
            node = node.setdefault(segment.strip(), {})
        leaf = segments[-1].strip()
        if sub:
            child = _parse_fields(sub.rstrip(")"))
            existing = node.get(leaf)
            node[leaf] = {} if child is None else {**(existing or {}), **child}
        else:
            node.setdefault(leaf, {})
    return mask


def _split_fields(fields: str) -> list[str]:
    """Split on commas that are not inside parentheses."""
    items, depth, current = [], 0, ""
    for char in fields:
        if char == "," and depth == 0:
            items.append(current)
            current = ""
            continue
        depth += char == "("
        depth -= char == ")"
        current += char
    if current.strip():
        items.append(current)
    return [item for item in items if item.strip()]


def _apply_fields(data: Any, mask: dict | None) -> Any:
    if mask is None or not mask:
        return data
    if isinstance(data, list):
        return [_apply_fields(item, mask) for item in data]
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, sub in mask.items():
        if key == "*":
            return data
        if key in data:
            out[key] = _apply_fields(data[key], sub)
    return out


def _reply(request: Request, data: Any, *, default: str | None = None, status: int = 200,
           fields: str | None = None) -> Response:
    """JSON response honouring ``?fields=``; ``default`` is the mask Google applies
    when the caller asks for none (Drive returns four file fields, not the lot).

    ``fields`` overrides the request's, for a resumable upload that finishes on a
    later request than the one which carried the mask.
    """
    fields = fields or request.query_params.get("fields")
    mask = _parse_fields(fields) if fields else (_parse_fields(default) if default else None)
    return JSONResponse(_apply_fields(data, mask), status_code=status)


# --- pagination --------------------------------------------------------------

def _page_token(offset: int) -> str:
    return base64.urlsafe_b64encode(f"cp{offset}".encode()).decode().rstrip("=")


def _page_offset(token: str | None) -> int:
    if not token:
        return 0
    padded = token + "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode()
        if not decoded.startswith("cp"):
            raise ValueError(decoded)
        return int(decoded[2:])
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ApiError(400, f"Invalid pageToken value: {token}", reason="invalidParameter",
                       location="pageToken") from None


def _paginate(items: list, request: Request, *, size_param: str, default_size: int,
              max_size: int) -> tuple[list, str | None]:
    """One page of ``items`` plus the token for the next — or None when done.

    Emitting a token only while records remain is what stops an agent's
    ``while page_token:`` loop; echoing one forever never terminates.
    """
    size = max(1, _int_param(request, size_param, default_size, maximum=max_size))
    start = _page_offset(request.query_params.get("pageToken"))
    window = items[start:start + size]
    more = start + size < len(items)
    return window, _page_token(start + size) if more else None


# --- MIME --------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def _unb64(data: str, *, field: str = "raw") -> bytes:
    """Decode base64url (or plain base64), padded or not, like Google's parsers."""
    text = re.sub(r"\s+", "", data or "").replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text)
    except (binascii.Error, ValueError):
        raise ApiError(400, f"Invalid {field} content: not base64url encoded",
                       reason="invalidArgument") from None


_BASE64ISH = re.compile(r"^[A-Za-z0-9+/\-_]+={0,2}$")


def _looks_base64(text: str) -> bool:
    return bool(text) and len(text) % 4 in (0, 2, 3) and bool(_BASE64ISH.fullmatch(text))


def _parse_message(raw: bytes) -> EmailMessage:
    parsed = message_from_bytes(raw, policy=policy.default)
    if not isinstance(parsed, EmailMessage):  # pragma: no cover - policy.default returns one
        raise ApiError(400, "Invalid raw message", reason="invalidArgument")
    return parsed


_CANONICAL_HEADERS = {
    "message-id": "Message-ID", "mime-version": "MIME-Version", "in-reply-to": "In-Reply-To",
    "dkim-signature": "DKIM-Signature", "content-id": "Content-ID",
}


def _canonical_header(name: str) -> str:
    """Gmail normalizes header names, so ``m["to"] = ...`` comes back as ``To``."""
    return _CANONICAL_HEADERS.get(name.lower(), "-".join(
        word[:1].upper() + word[1:] for word in name.split("-")))


def _payload_of(part: EmailMessage, part_id: str = "") -> dict:
    """Gmail's MessagePart tree for one MIME part (bodies base64url encoded)."""
    node: dict[str, Any] = {
        "partId": part_id,
        "mimeType": part.get_content_type(),
        "filename": part.get_filename() or "",
        "headers": [{"name": _canonical_header(name), "value": str(value)}
                    for name, value in part.items()],
    }
    if part.is_multipart():
        node["body"] = {"size": 0}
        node["parts"] = [
            _payload_of(sub, f"{part_id}.{index}" if part_id else str(index))
            for index, sub in enumerate(part.iter_parts())
        ]
        return node
    content = part.get_payload(decode=True) or b""
    node["body"] = {"size": len(content), "data": _b64(content)}
    if node["filename"]:
        # Attachment bytes are fetched separately, by attachmentId.
        node["body"]["attachmentId"] = _attachment_id(content)
    return node


def _attachment_id(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    return "ANGjdJ_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")[:40]


def _render_payload(node: dict) -> dict:
    """The payload as the API returns it: attachment bytes live behind their id."""
    out: dict[str, Any] = {
        "partId": node.get("partId", ""),
        "mimeType": node.get("mimeType", "text/plain"),
        "filename": node.get("filename", ""),
        "headers": [dict(h) for h in node.get("headers", [])],
    }
    body = dict(node.get("body") or {})
    if body.get("attachmentId"):
        body.pop("data", None)
    out["body"] = body
    if node.get("parts"):
        out["parts"] = [_render_payload(part) for part in node["parts"]]
    return out


def _walk_parts(node: dict) -> Iterator[dict]:
    yield node
    for part in node.get("parts") or []:
        yield from _walk_parts(part)


def _header(payload: dict, name: str) -> str:
    for entry in payload.get("headers") or []:
        if entry.get("name", "").lower() == name.lower():
            return str(entry.get("value", ""))
    return ""


def _set_header(payload: dict, name: str, value: str) -> None:
    for entry in payload.setdefault("headers", []):
        if entry.get("name", "").lower() == name.lower():
            entry["value"] = value
            return
    payload["headers"].append({"name": name, "value": value})


_TAGS = re.compile(r"<[^>]+>")


def _body_text(payload: dict) -> str:
    """The message's plain-text body (HTML stripped when that is all there is)."""
    for mime in ("text/plain", "text/html"):
        for part in _walk_parts(payload):
            data = (part.get("body") or {}).get("data")
            if part.get("mimeType") != mime or not data:
                continue
            try:
                text = _unb64(data).decode("utf-8", "replace")
            except ApiError:  # a hand-written seed the normalizer never saw
                continue
            return _TAGS.sub(" ", text) if mime == "text/html" else text
    return ""


_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def _snippet(text: str) -> str:
    """Gmail's snippet: the first ~200 characters, collapsed and HTML-escaped."""
    collapsed = " ".join(text.split())[:200]
    return "".join(_ESCAPES.get(char, char) for char in collapsed)


def _raw_of(message: dict) -> str:
    """The message as RFC 822 bytes, base64url encoded (``format=raw``)."""
    if message.get("_raw"):
        return message["_raw"]
    return _b64(_rebuild_mime(message["payload"]))


def _rebuild_mime(node: dict) -> bytes:
    """Serialise a stored payload tree back to RFC 822 (for seeded messages)."""
    part = EmailMessage()
    for entry in node.get("headers") or []:
        # The content headers are regenerated below, boundaries and all.
        if entry.get("name", "").lower().startswith(("content-", "mime-version")):
            continue
        part[entry.get("name", "")] = entry.get("value", "")
    if node.get("parts"):
        part["MIME-Version"] = "1.0"
        if node.get("mimeType") == "multipart/alternative":
            part.make_alternative()
        else:
            part.make_mixed()
        for child in node["parts"]:
            part.attach(message_from_bytes(_rebuild_mime(child), policy=policy.default))
        return part.as_bytes()
    content = _unb64((node.get("body") or {}).get("data") or "") or b""
    maintype, _, subtype = (node.get("mimeType") or "text/plain").partition("/")
    filename = node.get("filename") or None
    if maintype == "text":
        part.set_content(content.decode("utf-8", "replace"), subtype=subtype or "plain",
                         **({"filename": filename} if filename else {}))
    else:
        part.set_content(content, maintype=maintype, subtype=subtype or "octet-stream",
                         filename=filename)
    return part.as_bytes()


def _addresses(*values: str) -> list[str]:
    """Every address in a set of To/Cc/Bcc header values."""
    return [addr for _name, addr in getaddresses([v for v in values if v]) if addr]


def _normalize_date(value: str) -> str:
    """Seeds may carry an ISO date; the wire format is RFC 2822."""
    if not value:
        return _rfc2822()
    try:
        parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            return _rfc2822(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return _rfc2822()
    return value


# =============================================================================
# Gmail
# =============================================================================

def _label(label_id: str) -> dict:
    label = STATE["gmail_labels"].get(label_id)
    if not label:
        raise _not_found("Label", label_id)
    return label


def _known_label(label_id: str) -> str:
    """Resolve a label id, tolerating the wrong case on a built-in label."""
    if label_id in STATE["gmail_labels"]:
        return label_id
    if label_id.upper() in _SYSTEM_LABELS:
        return label_id.upper()
    raise ApiError(400, f"Invalid label: {label_id}", reason="invalidArgument")


def _apply_labels(record: dict, add: list[str], remove: list[str]) -> None:
    """Add/remove label ids, keeping insertion order stable across calls."""
    labels = [lid for lid in record.get("labelIds", []) if lid not in remove]
    for lid in add:
        if lid not in labels:
            labels.append(lid)
    record["labelIds"] = labels


def _message(message_id: str) -> dict:
    message = STATE["gmail_messages"].get(message_id)
    if not message:
        raise _not_found("Message", message_id)
    return message


def _message_id_header() -> str:
    return f"<{uuid.uuid4().hex}@mail.gmail.com>"


def _thread_for(payload: dict) -> str | None:
    """The thread a reply belongs to, from In-Reply-To / References."""
    refs = f"{_header(payload, 'In-Reply-To')} {_header(payload, 'References')}"
    ids = re.findall(r"<[^>]+>", refs)
    for message in STATE["gmail_messages"].values():
        if ids and _header(message["payload"], "Message-ID") in ids:
            return message["threadId"]
    return None


def _store_message(raw: bytes, *, labels: Iterable[str], thread_id: str | None = None,
                   envelope: bool = True) -> dict:
    """Parse RFC 822 bytes into a stored message (and into its thread)."""
    parsed = _parse_message(raw)
    if envelope:
        # Gmail stamps the envelope headers a client left out.
        for name, value in (("From", _me()), ("Date", _rfc2822()),
                            ("Message-ID", _message_id_header()), ("MIME-Version", "1.0")):
            if not parsed.get(name):
                parsed[name] = value
    raw = parsed.as_bytes()
    payload = _payload_of(parsed)
    when = _now()
    try:
        when = parsedate_to_datetime(_header(payload, "Date")) or when
    except (TypeError, ValueError):
        pass
    message_id = _gmail_id()
    message = {
        "id": message_id,
        "threadId": thread_id or _thread_for(payload) or message_id,
        "labelIds": list(labels),
        "snippet": _snippet(_body_text(payload)),
        "historyId": STATE["user_profile"]["historyId"],
        "internalDate": str(int(when.timestamp() * 1000)),
        "sizeEstimate": len(raw),
        "payload": payload,
        "_raw": _b64(raw),
    }
    STATE["gmail_messages"][message_id] = message
    _history_event("messagesAdded", message)
    _refresh_thread(message["threadId"])
    return message


def _refresh_thread(thread_id: str) -> dict | None:
    """Keep the thread index in step with its messages (or drop it when empty)."""
    messages = _thread_messages(thread_id)
    if not messages:
        STATE["gmail_threads"].pop(thread_id, None)
        return None
    labels: list[str] = []
    for message in messages:
        for lid in message["labelIds"]:
            if lid not in labels:
                labels.append(lid)
    thread = STATE["gmail_threads"].setdefault(thread_id, {"id": thread_id})
    thread.update({
        "snippet": messages[-1]["snippet"],
        "historyId": max(m["historyId"] for m in messages),
        "messageIds": [m["id"] for m in messages],
        "labelIds": labels,
    })
    return thread


def _thread_messages(thread_id: str) -> list[dict]:
    messages = [m for m in STATE["gmail_messages"].values() if m["threadId"] == thread_id]
    return sorted(messages, key=lambda m: (int(m["internalDate"]), m["id"]))


def _thread(thread_id: str) -> dict:
    thread = STATE["gmail_threads"].get(thread_id)
    if not thread:
        raise _not_found("Thread", thread_id)
    return thread


def _history_event(kind: str, message: dict, label_ids: list[str] | None = None) -> None:
    """Append a users.history record so history.list can replay the change."""
    history_id = _next_history_id()
    message["historyId"] = history_id
    minimal = {"id": message["id"], "threadId": message["threadId"],
               "labelIds": list(message["labelIds"])}
    record: dict[str, Any] = {
        "id": history_id,
        "messages": [{"id": message["id"], "threadId": message["threadId"]}],
        kind: [{"message": minimal, "labelIds": list(label_ids)} if label_ids is not None
               else {"message": minimal}],
    }
    STATE["_history"].append(record)


def _forget_message(message: dict) -> None:
    """Permanently delete a message (and the draft that pointed at it)."""
    STATE["gmail_messages"].pop(message["id"], None)
    for draft_id, draft in list(STATE["gmail_drafts"].items()):
        if draft.get("messageId") == message["id"]:
            del STATE["gmail_drafts"][draft_id]
    _history_event("messagesDeleted", message)
    _refresh_thread(message["threadId"])


def _sorted_messages() -> list[dict]:
    """Newest first — the order Gmail lists a mailbox in."""
    return sorted(STATE["gmail_messages"].values(),
                  key=lambda m: (int(m["internalDate"]), m["id"]), reverse=True)


def _visible(message: dict, include_spam_trash: bool) -> bool:
    return include_spam_trash or not {"TRASH", "SPAM"} & set(message["labelIds"])


def _render_message(message: dict, fmt: str = "full",
                    metadata_headers: Iterable[str] = ()) -> dict:
    out = {key: message[key] for key in
           ("id", "threadId", "labelIds", "snippet", "historyId", "internalDate", "sizeEstimate")
           if key in message}
    if fmt == "minimal":
        return out
    if fmt == "raw":
        out["raw"] = _raw_of(message)
        return out
    payload = message["payload"]
    if fmt == "metadata":
        wanted = {name.lower() for name in metadata_headers}
        headers = [dict(h) for h in payload.get("headers", [])
                   if not wanted or h.get("name", "").lower() in wanted]
        out["payload"] = {"partId": "", "mimeType": payload.get("mimeType", "text/plain"),
                          "filename": "", "headers": headers,
                          "body": {"size": (payload.get("body") or {}).get("size", 0)}}
        return out
    out["payload"] = _render_payload(payload)
    return out


_FORMATS = ("minimal", "full", "raw", "metadata")


def _format(request: Request, default: str = "full") -> str:
    fmt = request.query_params.get("format", default).lower()
    if fmt not in _FORMATS:
        raise ApiError(400, f"Invalid format: {fmt}", reason="invalidArgument", location="format")
    return fmt


# --- Gmail search (the ``q`` parameter) --------------------------------------

_Q_TOKEN = re.compile(r"""\s*(?:
      (?P<lparen>\() | (?P<rparen>\)) | (?P<lbrace>\{) | (?P<rbrace>\})
    | (?P<or>\bOR\b|\|)
    | (?P<neg>-(?=\S))
    | (?P<key>[A-Za-z_]+):(?P<value>\([^)]*\)|"[^"]*"|[^\s()}{]*)
    | "(?P<phrase>[^"]*)"
    | (?P<word>[^\s()}{|]+)
)""", re.VERBOSE)

_AGE_UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}
_SIZE_UNITS = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}


def _norm_label(name: str) -> str:
    return name.strip().strip("'\"").lower().replace(" ", "-")


def _search_fields(message: dict) -> dict:
    """The haystack one message offers a query — headers, body, labels, dates."""
    payload = message["payload"]
    labels = {_norm_label(lid) for lid in message["labelIds"]}
    for lid in message["labelIds"]:
        label = STATE["gmail_labels"].get(lid)
        if label:
            labels.add(_norm_label(label.get("name", lid)))
    text = _body_text(payload)
    recipients = " ".join(_header(payload, h) for h in ("To", "Cc", "Bcc")).lower()
    return {
        "from": _header(payload, "From").lower(),
        "to": recipients,
        "cc": _header(payload, "Cc").lower(),
        "bcc": _header(payload, "Bcc").lower(),
        "subject": _header(payload, "Subject").lower(),
        "labels": labels,
        "filenames": " ".join(p.get("filename", "") for p in _walk_parts(payload)).lower(),
        "msgid": _header(payload, "Message-ID").lower(),
        "date": int(message["internalDate"]),
        "size": message.get("sizeEstimate", 0),
        "everything": " ".join([
            _header(payload, "From"), recipients, _header(payload, "Subject"), text,
        ]).lower(),
    }


def _parse_query_date(value: str) -> datetime:
    text = value.strip().strip("'\"")
    if text.isdigit() and len(text) >= 9:
        return datetime.fromtimestamp(int(text), UTC)
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ApiError(400, f"Invalid date in query: {value}", reason="invalidArgument", location="q")


def _parse_query_size(value: str) -> int:
    text = value.strip().lower()
    if text and text[-1] in _SIZE_UNITS:
        return int(float(text[:-1] or 0) * _SIZE_UNITS[text[-1]])
    return int(float(text or 0))


def _relative_cutoff(value: str) -> int:
    match = re.fullmatch(r"(\d+)([dwmy])", value.strip().lower())
    if not match:
        raise ApiError(400, f"Invalid age in query: {value}", reason="invalidArgument",
                       location="q")
    days = int(match.group(1)) * _AGE_UNITS[match.group(2)]
    return int((_now() - timedelta(days=days)).timestamp() * 1000)


def _term_predicate(key: str, value: str, scope: dict) -> Callable[[dict], bool]:
    """One ``key:value`` operator of a Gmail query."""
    key = key.lower()
    raw = value.strip()
    if raw.startswith("(") and raw.endswith(")"):  # subject:(q1 planning) is an OR group
        options = [part for part in re.split(r"\s+(?:OR\s+)?", raw[1:-1].strip()) if part]
        parts = [_term_predicate(key, option, scope) for option in options]
        return lambda message: any(part(message) for part in parts)
    needle = raw.strip("'\"").lower()

    if key in ("from", "to", "cc", "bcc", "subject", "deliveredto", "filename", "rfc822msgid"):
        field = {"deliveredto": "to", "rfc822msgid": "msgid", "filename": "filenames"}.get(key, key)
        return lambda message: needle in _search_fields(message)[field]
    if key in ("label", "l"):
        wanted = _norm_label(needle)
        return lambda message: wanted in _search_fields(message)["labels"]
    if key == "category":
        return lambda message: f"category_{needle}" in _search_fields(message)["labels"]
    if key == "in":
        target = {"drafts": "draft", "anywhere": "anywhere"}.get(needle, needle)
        if target in ("trash", "spam", "anywhere"):
            scope["include_spam_trash"] = True
        if target == "anywhere":
            return lambda message: True
        return lambda message: target in _search_fields(message)["labels"]
    if key == "is":
        if needle == "read":
            return lambda message: "unread" not in _search_fields(message)["labels"]
        if needle in ("trash", "spam"):
            scope["include_spam_trash"] = True
        if needle not in ("unread", "starred", "important", "sent", "draft", "chat",
                          "trash", "spam"):
            return lambda message: True  # is:snoozed / is:muted: nothing to match on
        return lambda message: needle in _search_fields(message)["labels"]
    if key == "has":
        if needle == "attachment":
            return lambda message: bool(_search_fields(message)["filenames"].strip())
        if needle == "userlabels":
            return lambda message: any(
                (STATE["gmail_labels"].get(lid) or {}).get("type") == "user"
                for lid in message["labelIds"])
        return lambda message: True
    if key in ("after", "newer", "before", "older"):
        cutoff = int(_parse_query_date(needle).timestamp() * 1000)
        if key in ("after", "newer"):
            return lambda message: _search_fields(message)["date"] >= cutoff
        return lambda message: _search_fields(message)["date"] < cutoff
    if key in ("newer_than", "older_than"):
        cutoff = _relative_cutoff(needle)
        if key == "newer_than":
            return lambda message: _search_fields(message)["date"] >= cutoff
        return lambda message: _search_fields(message)["date"] < cutoff
    if key in ("larger", "size", "smaller"):
        size = _parse_query_size(needle)
        if key == "smaller":
            return lambda message: _search_fields(message)["size"] < size
        return lambda message: _search_fields(message)["size"] >= size
    # An unknown operator is searched as plain text, the way Gmail treats it.
    text = f"{key}:{needle}"
    return lambda message: text in _search_fields(message)["everything"]


def _word_predicate(word: str) -> Callable[[dict], bool]:
    needle = word.strip().lower()
    return lambda message: needle in _search_fields(message)["everything"]


def _gmail_query(query: str) -> tuple[Callable[[dict], bool], bool]:
    """Compile a Gmail ``q`` string into a predicate over stored messages.

    Also reports whether the query reaches into Trash/Spam, which every other
    search hides.
    """
    if not (query or "").strip():
        return (lambda message: True), False
    tokens = [m for m in _Q_TOKEN.finditer(query) if m.group().strip()]
    scope = {"include_spam_trash": False}
    position = 0

    def parse_or() -> Callable[[dict], bool]:
        nonlocal position
        parts = [parse_and()]
        while position < len(tokens) and tokens[position].lastgroup == "or":
            position += 1
            parts.append(parse_and())
        return lambda message: any(part(message) for part in parts)

    def parse_and() -> Callable[[dict], bool]:
        nonlocal position
        parts = []
        while position < len(tokens):
            if tokens[position].lastgroup in ("or", "rparen", "rbrace"):
                break
            parts.append(parse_unary())
        if not parts:
            return lambda message: True
        return lambda message: all(part(message) for part in parts)

    def parse_unary() -> Callable[[dict], bool]:
        nonlocal position
        token = tokens[position]
        kind = token.lastgroup
        if kind == "neg":
            position += 1
            inner = parse_unary()
            return lambda message: not inner(message)
        if kind == "lparen":
            position += 1
            inner = parse_or()
            if position < len(tokens) and tokens[position].lastgroup == "rparen":
                position += 1
            return inner
        if kind == "lbrace":  # {a b} is Gmail's OR group
            position += 1
            parts = []
            while position < len(tokens) and tokens[position].lastgroup != "rbrace":
                parts.append(parse_unary())
            if position < len(tokens):
                position += 1
            return lambda message: any(part(message) for part in parts)
        position += 1
        if token.group("key") is not None:
            return _term_predicate(token.group("key"), token.group("value") or "", scope)
        if token.group("phrase") is not None:
            return _word_predicate(token.group("phrase"))
        return _word_predicate(token.group().strip())

    predicate = parse_or()
    return predicate, bool(scope["include_spam_trash"])


# --- Gmail: profile and labels -----------------------------------------------

@app.get("/gmail/v1/users/{user_id}/profile")
def gmail_get_profile(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    profile = STATE["user_profile"]
    return _reply(request, {
        "emailAddress": profile["emailAddress"],
        "messagesTotal": len(STATE["gmail_messages"]),
        "threadsTotal": len(STATE["gmail_threads"]),
        "historyId": profile.get("historyId", "1"),
    })


def _label_counts(label_id: str) -> dict:
    messages = [m for m in STATE["gmail_messages"].values() if label_id in m["labelIds"]]
    unread = [m for m in messages if "UNREAD" in m["labelIds"]]
    return {
        "messagesTotal": len(messages),
        "messagesUnread": len(unread),
        "threadsTotal": len({m["threadId"] for m in messages}),
        "threadsUnread": len({m["threadId"] for m in unread}),
    }


def _render_label(label: dict, *, counts: bool = False) -> dict:
    out = {"id": label["id"], "name": label.get("name", label["id"]),
           "type": label.get("type", "user")}
    for key in ("messageListVisibility", "labelListVisibility", "color"):
        if label.get(key) is not None:
            out[key] = label[key]
    if counts:
        out.update(_label_counts(label["id"]))
    return out


@app.get("/gmail/v1/users/{user_id}/labels")
def gmail_list_labels(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    return _reply(request, {"labels": [_render_label(lab)
                                       for lab in STATE["gmail_labels"].values()]})


@app.get("/gmail/v1/users/{user_id}/labels/{label_id}")
def gmail_get_label(user_id: str, label_id: str, request: Request) -> Response:
    _check_user(user_id)
    return _reply(request, _render_label(_label(label_id), counts=True))


@app.post("/gmail/v1/users/{user_id}/labels")
async def gmail_create_label(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Invalid label name: ", reason="invalidArgument")
    if name.upper() in _SYSTEM_LABELS:
        raise ApiError(400, f"Invalid label name: {name}", reason="invalidArgument")
    if any(lab.get("name", "").lower() == name.lower() for lab in STATE["gmail_labels"].values()):
        raise ApiError(409, "Label name exists or conflicts", reason="duplicate")
    label = {
        "id": _label_id(),
        "name": name,
        "type": "user",
        "messageListVisibility": body.get("messageListVisibility", "show"),
        "labelListVisibility": body.get("labelListVisibility", "labelShow"),
    }
    if "color" in body:
        label["color"] = body["color"]
    STATE["gmail_labels"][label["id"]] = label
    return _reply(request, _render_label(label))


async def _update_label(label_id: str, request: Request, *, replace: bool) -> Response:
    label = _label(label_id)
    if label.get("type") == "system":
        raise ApiError(400, f"Invalid label modification: {label_id}", reason="invalidArgument")
    body = await _body(request)
    if replace and not str(body.get("name") or "").strip():
        raise ApiError(400, "Invalid label name: ", reason="invalidArgument")
    for field in ("name", "messageListVisibility", "labelListVisibility", "color"):
        if field in body:
            label[field] = body[field]
    return _reply(request, _render_label(label, counts=True))


@app.put("/gmail/v1/users/{user_id}/labels/{label_id}")
async def gmail_update_label(user_id: str, label_id: str, request: Request) -> Response:
    _check_user(user_id)
    return await _update_label(label_id, request, replace=True)


@app.patch("/gmail/v1/users/{user_id}/labels/{label_id}")
async def gmail_patch_label(user_id: str, label_id: str, request: Request) -> Response:
    _check_user(user_id)
    return await _update_label(label_id, request, replace=False)


@app.delete("/gmail/v1/users/{user_id}/labels/{label_id}")
def gmail_delete_label(user_id: str, label_id: str) -> Response:
    _check_user(user_id)
    label = _label(label_id)
    if label.get("type") == "system":
        raise ApiError(400, f"Invalid label modification: {label_id}", reason="invalidArgument")
    del STATE["gmail_labels"][label_id]
    # Deleting a label strips it from every message that carried it.
    for message in STATE["gmail_messages"].values():
        if label_id in message["labelIds"]:
            _apply_labels(message, [], [label_id])
    for thread_id in list(STATE["gmail_threads"]):
        _refresh_thread(thread_id)
    return Response(status_code=204)


# --- Gmail: messages ---------------------------------------------------------

def _filtered_messages(request: Request) -> list[dict]:
    predicate, query_wants_trash = _gmail_query(request.query_params.get("q", ""))
    label_ids = [_known_label(lid) for lid in _repeated(request, "labelIds")]
    include = (_bool_param(request, "includeSpamTrash") or query_wants_trash
               or bool({"TRASH", "SPAM"} & set(label_ids)))
    return [m for m in _sorted_messages()
            if _visible(m, include)
            and all(lid in m["labelIds"] for lid in label_ids)  # repeated labelIds are ANDed
            and predicate(m)]


@app.get("/gmail/v1/users/{user_id}/messages")
def gmail_list_messages(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    messages = _filtered_messages(request)
    window, next_token = _paginate(messages, request, size_param="maxResults",
                                   default_size=100, max_size=500)
    body: dict[str, Any] = {}
    if window:  # Gmail omits the array entirely when nothing matched
        body["messages"] = [{"id": m["id"], "threadId": m["threadId"]} for m in window]
    if next_token:
        body["nextPageToken"] = next_token
    body["resultSizeEstimate"] = len(messages)
    return _reply(request, body)


@app.post("/gmail/v1/users/{user_id}/messages/send")
async def gmail_send_message(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    body = await _body(request)
    raw = body.get("raw")
    if not raw:
        raise ApiError(400, "'raw' RFC822 payload message string or uploading message via "
                            "/upload/* URL required", reason="invalidArgument")
    message = _send(_unb64(raw), thread_id=body.get("threadId"))
    return _reply(request, _render_message(message, "minimal"))


def _send(raw: bytes, *, thread_id: str | None = None) -> dict:
    """Deliver an RFC 822 message: SENT (plus INBOX when it is addressed to us)."""
    if thread_id and thread_id not in STATE["gmail_threads"]:
        raise ApiError(400, f"Invalid threadId value: {thread_id}", reason="invalidArgument")
    parsed = _parse_message(raw)
    recipients = _addresses(*(str(parsed.get(h, "")) for h in ("To", "Cc", "Bcc")))
    if not recipients:
        raise ApiError(400, "Recipient address required", reason="invalidArgument")
    labels = ["SENT"]
    if any(address.lower() == _me().lower() for address in recipients):
        labels += ["INBOX", "UNREAD"]
    return _store_message(raw, labels=labels, thread_id=thread_id)


@app.post("/gmail/v1/users/{user_id}/messages")
async def gmail_insert_message(user_id: str, request: Request) -> Response:
    """messages.insert — add a message to the mailbox without sending it."""
    _check_user(user_id)
    body = await _body(request)
    if not body.get("raw"):
        raise ApiError(400, "'raw' RFC822 payload message string required",
                       reason="invalidArgument")
    # Without labelIds the message lands like delivered mail, which is what a
    # caller seeding a mailbox means.
    labels = [_known_label(lid) for lid in body.get("labelIds") or ["INBOX", "UNREAD"]]
    message = _store_message(_unb64(body["raw"]), labels=labels, thread_id=body.get("threadId"))
    return _reply(request, _render_message(message, "minimal"))


@app.post("/gmail/v1/users/{user_id}/messages/import")
async def gmail_import_message(user_id: str, request: Request) -> Response:
    return await gmail_insert_message(user_id, request)


@app.post("/gmail/v1/users/{user_id}/messages/batchModify")
async def gmail_batch_modify(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    body = await _body(request)
    add = [_known_label(lid) for lid in body.get("addLabelIds") or []]
    remove = [_known_label(lid) for lid in body.get("removeLabelIds") or []]
    for message_id in body.get("ids") or []:
        message = STATE["gmail_messages"].get(message_id)
        if message is None:
            raise _not_found("Message", message_id)
        _modify_message(message, add, remove)
    return Response(status_code=204)


@app.post("/gmail/v1/users/{user_id}/messages/batchDelete")
async def gmail_batch_delete(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    body = await _body(request)
    for message_id in body.get("ids") or []:
        message = STATE["gmail_messages"].get(message_id)
        if message is None:
            raise _not_found("Message", message_id)
        _forget_message(message)
    return Response(status_code=204)


@app.get("/gmail/v1/users/{user_id}/messages/{message_id}")
def gmail_get_message(user_id: str, message_id: str, request: Request) -> Response:
    _check_user(user_id)
    message = _message(message_id)
    rendered = _render_message(message, _format(request), _repeated(request, "metadataHeaders"))
    return _reply(request, rendered)


@app.get("/gmail/v1/users/{user_id}/messages/{message_id}/attachments/{attachment_id}")
def gmail_get_attachment(user_id: str, message_id: str, attachment_id: str,
                         request: Request) -> Response:
    _check_user(user_id)
    message = _message(message_id)
    for part in _walk_parts(message["payload"]):
        body = part.get("body") or {}
        if body.get("attachmentId") == attachment_id:
            return _reply(request, {"size": body.get("size", 0), "data": body.get("data", "")})
    raise _not_found("Attachment", attachment_id)


def _modify_message(message: dict, add: list[str], remove: list[str]) -> dict:
    _apply_labels(message, add, remove)
    if add:
        _history_event("labelsAdded", message, add)
    if remove:
        _history_event("labelsRemoved", message, remove)
    _refresh_thread(message["threadId"])
    return message


@app.post("/gmail/v1/users/{user_id}/messages/{message_id}/modify")
async def gmail_modify_message(user_id: str, message_id: str, request: Request) -> Response:
    _check_user(user_id)
    message = _message(message_id)
    body = await _body(request)
    add = [_known_label(lid) for lid in body.get("addLabelIds") or []]
    remove = [_known_label(lid) for lid in body.get("removeLabelIds") or []]
    return _reply(request, _render_message(_modify_message(message, add, remove), "minimal"))


def _trash(message: dict) -> dict:
    """Trashing hides a message from the inbox; untrash puts it back."""
    if "INBOX" in message["labelIds"]:
        message["_inboxBeforeTrash"] = True
    return _modify_message(message, ["TRASH"], ["INBOX"])


def _untrash(message: dict) -> dict:
    restore = ["INBOX"] if message.pop("_inboxBeforeTrash", False) else []
    return _modify_message(message, restore, ["TRASH"])


@app.post("/gmail/v1/users/{user_id}/messages/{message_id}/trash")
def gmail_trash_message(user_id: str, message_id: str, request: Request) -> Response:
    _check_user(user_id)
    return _reply(request, _render_message(_trash(_message(message_id)), "minimal"))


@app.post("/gmail/v1/users/{user_id}/messages/{message_id}/untrash")
def gmail_untrash_message(user_id: str, message_id: str, request: Request) -> Response:
    _check_user(user_id)
    return _reply(request, _render_message(_untrash(_message(message_id)), "minimal"))


@app.delete("/gmail/v1/users/{user_id}/messages/{message_id}")
def gmail_delete_message(user_id: str, message_id: str) -> Response:
    _check_user(user_id)
    _forget_message(_message(message_id))
    return Response(status_code=204)


# --- Gmail: threads ----------------------------------------------------------

def _thread_matches(thread: dict, keep: set[str]) -> bool:
    return bool(set(thread.get("messageIds", [])) & keep)


@app.get("/gmail/v1/users/{user_id}/threads")
def gmail_list_threads(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    keep = {m["id"] for m in _filtered_messages(request)}
    threads = [t for t in _sorted_threads() if _thread_matches(t, keep)]
    window, next_token = _paginate(threads, request, size_param="maxResults",
                                   default_size=100, max_size=500)
    body: dict[str, Any] = {}
    if window:
        body["threads"] = [{"id": t["id"], "snippet": t.get("snippet", ""),
                            "historyId": t.get("historyId", "1")} for t in window]
    if next_token:
        body["nextPageToken"] = next_token
    body["resultSizeEstimate"] = len(threads)
    return _reply(request, body)


def _sorted_threads() -> list[dict]:
    def newest(thread: dict) -> tuple[int, str]:
        messages = _thread_messages(thread["id"])
        return (int(messages[-1]["internalDate"]) if messages else 0, thread["id"])

    return sorted(STATE["gmail_threads"].values(), key=newest, reverse=True)


def _render_thread(thread: dict, fmt: str = "full",
                   metadata_headers: Iterable[str] = ()) -> dict:
    return {
        "id": thread["id"],
        "historyId": thread.get("historyId", "1"),
        "messages": [_render_message(m, fmt, metadata_headers)
                     for m in _thread_messages(thread["id"])],
    }


@app.get("/gmail/v1/users/{user_id}/threads/{thread_id}")
def gmail_get_thread(user_id: str, thread_id: str, request: Request) -> Response:
    _check_user(user_id)
    thread = _thread(thread_id)
    return _reply(request, _render_thread(thread, _format(request),
                                          _repeated(request, "metadataHeaders")))


@app.post("/gmail/v1/users/{user_id}/threads/{thread_id}/modify")
async def gmail_modify_thread(user_id: str, thread_id: str, request: Request) -> Response:
    _check_user(user_id)
    thread = _thread(thread_id)
    body = await _body(request)
    add = [_known_label(lid) for lid in body.get("addLabelIds") or []]
    remove = [_known_label(lid) for lid in body.get("removeLabelIds") or []]
    for message in _thread_messages(thread_id):
        _modify_message(message, add, remove)
    return _reply(request, _render_thread(_refresh_thread(thread_id) or thread, "minimal"))


@app.post("/gmail/v1/users/{user_id}/threads/{thread_id}/trash")
def gmail_trash_thread(user_id: str, thread_id: str, request: Request) -> Response:
    _check_user(user_id)
    thread = _thread(thread_id)
    for message in _thread_messages(thread_id):
        _trash(message)
    return _reply(request, _render_thread(_refresh_thread(thread_id) or thread, "minimal"))


@app.post("/gmail/v1/users/{user_id}/threads/{thread_id}/untrash")
def gmail_untrash_thread(user_id: str, thread_id: str, request: Request) -> Response:
    _check_user(user_id)
    thread = _thread(thread_id)
    for message in _thread_messages(thread_id):
        _untrash(message)
    return _reply(request, _render_thread(_refresh_thread(thread_id) or thread, "minimal"))


@app.delete("/gmail/v1/users/{user_id}/threads/{thread_id}")
def gmail_delete_thread(user_id: str, thread_id: str) -> Response:
    _check_user(user_id)
    _thread(thread_id)
    for message in _thread_messages(thread_id):
        _forget_message(message)
    STATE["gmail_threads"].pop(thread_id, None)
    return Response(status_code=204)


# --- Gmail: drafts -----------------------------------------------------------

def _draft(draft_id: str) -> dict:
    draft = STATE["gmail_drafts"].get(draft_id)
    if not draft:
        raise _not_found("Draft", draft_id)
    return draft


def _draft_message(draft: dict) -> dict:
    return _message(draft["messageId"])


def _render_draft(draft: dict, fmt: str = "minimal",
                  metadata_headers: Iterable[str] = ()) -> dict:
    return {"id": draft["id"],
            "message": _render_message(_draft_message(draft), fmt, metadata_headers)}


def _draft_body(body: dict) -> tuple[bytes, str | None]:
    """The RFC 822 bytes and threadId of a Draft resource in a request body."""
    message = body.get("message") or {}
    raw = message.get("raw")
    return (_unb64(raw) if raw else b""), message.get("threadId")


@app.get("/gmail/v1/users/{user_id}/drafts")
def gmail_list_drafts(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    predicate, _ = _gmail_query(request.query_params.get("q", ""))
    drafts = [d for d in _sorted_drafts() if predicate(_draft_message(d))]
    window, next_token = _paginate(drafts, request, size_param="maxResults",
                                   default_size=100, max_size=500)
    body: dict[str, Any] = {}
    if window:
        body["drafts"] = [_render_draft(d) for d in window]
    if next_token:
        body["nextPageToken"] = next_token
    body["resultSizeEstimate"] = len(drafts)
    return _reply(request, body)


def _sorted_drafts() -> list[dict]:
    def newest(draft: dict) -> tuple[int, str]:
        message = STATE["gmail_messages"].get(draft["messageId"]) or {}
        return (int(message.get("internalDate", 0)), draft["id"])

    return sorted(STATE["gmail_drafts"].values(), key=newest, reverse=True)


@app.post("/gmail/v1/users/{user_id}/drafts")
async def gmail_create_draft(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    raw, thread_id = _draft_body(await _body(request))
    draft = _store_draft(raw, thread_id=thread_id)
    return _reply(request, _render_draft(draft))


def _store_draft(raw: bytes, *, thread_id: str | None = None) -> dict:
    message = _store_message(raw, labels=["DRAFT"], thread_id=thread_id)
    draft = _link_draft({"id": _draft_id()}, message)
    STATE["gmail_drafts"][draft["id"]] = draft
    return draft


def _link_draft(draft: dict, message: dict) -> dict:
    """Point a draft at its message.

    ``message`` is the very object stored in ``gmail_messages``, so a draft in
    ``/_state`` reads like the API's Draft resource without the two copies ever
    drifting apart.
    """
    draft.update({"messageId": message["id"], "message": message})
    return draft


@app.put("/gmail/v1/users/{user_id}/drafts/{draft_id}")
async def gmail_update_draft(user_id: str, draft_id: str, request: Request) -> Response:
    _check_user(user_id)
    draft = _draft(draft_id)
    raw, thread_id = _draft_body(await _body(request))
    old = _draft_message(draft)
    # Gmail replaces the draft's message wholesale, so it gets a new message id.
    STATE["gmail_messages"].pop(old["id"], None)
    _refresh_thread(old["threadId"])
    message = _store_message(raw, labels=["DRAFT"], thread_id=thread_id)
    _link_draft(draft, message)
    return _reply(request, _render_draft(draft))


@app.post("/gmail/v1/users/{user_id}/drafts/send")
async def gmail_send_draft(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    body = await _body(request)
    draft_id = body.get("id")
    if not draft_id:
        raise ApiError(400, "'id' is required to send a draft", reason="invalidArgument")
    draft = _draft(draft_id)
    message = _draft_message(draft)
    raw, thread_id = _draft_body(body)
    if not raw:  # no replacement content: send what the draft holds
        raw = _unb64(_raw_of(message))
        # A draft written as a reply keeps the thread it was drafted into.
        if len(_thread_messages(message["threadId"])) > 1:
            thread_id = message["threadId"]
    del STATE["gmail_drafts"][draft_id]
    STATE["gmail_messages"].pop(message["id"], None)
    _refresh_thread(message["threadId"])
    sent = _send(raw, thread_id=thread_id)
    return _reply(request, _render_message(sent, "minimal"))


@app.get("/gmail/v1/users/{user_id}/drafts/{draft_id}")
def gmail_get_draft(user_id: str, draft_id: str, request: Request) -> Response:
    _check_user(user_id)
    draft = _draft(draft_id)
    return _reply(request, _render_draft(draft, _format(request),
                                         _repeated(request, "metadataHeaders")))


@app.delete("/gmail/v1/users/{user_id}/drafts/{draft_id}")
def gmail_delete_draft(user_id: str, draft_id: str) -> Response:
    _check_user(user_id)
    draft = _draft(draft_id)
    del STATE["gmail_drafts"][draft_id]
    message = STATE["gmail_messages"].get(draft["messageId"])
    if message:
        _forget_message(message)
    return Response(status_code=204)


# --- Gmail: history ----------------------------------------------------------

_HISTORY_TYPES = {"messageAdded": "messagesAdded", "messageDeleted": "messagesDeleted",
                  "labelAdded": "labelsAdded", "labelRemoved": "labelsRemoved"}


@app.get("/gmail/v1/users/{user_id}/history")
def gmail_list_history(user_id: str, request: Request) -> Response:
    """Changes since ``startHistoryId`` — how agents poll a mailbox."""
    _check_user(user_id)
    start = request.query_params.get("startHistoryId")
    if not start or not start.isdigit():
        raise ApiError(400, "Invalid startHistoryId", reason="invalidArgument",
                       location="startHistoryId")
    wanted = {_HISTORY_TYPES[t] for t in _repeated(request, "historyTypes")
              if t in _HISTORY_TYPES}
    label_id = request.query_params.get("labelId")
    records = []
    for record in STATE["_history"]:
        if int(record["id"]) <= int(start):
            continue
        kinds = {k for k in _HISTORY_TYPES.values() if k in record}
        if wanted and not kinds & wanted:
            continue
        if label_id and not any(
            label_id in item.get("message", {}).get("labelIds", []) or
            label_id in item.get("labelIds", [])
            for kind in kinds for item in record[kind]
        ):
            continue
        records.append(record)
    window, next_token = _paginate(records, request, size_param="maxResults",
                                   default_size=100, max_size=500)
    body: dict[str, Any] = {}
    if window:
        body["history"] = window
    if next_token:
        body["nextPageToken"] = next_token
    body["historyId"] = STATE["user_profile"].get("historyId", "1")
    return _reply(request, body)


# =============================================================================
# Drive
# =============================================================================

# Drive returns four fields per file unless the caller asks for more, and SDK
# users who never pass ``fields`` see exactly this much.
_FILE_FIELDS = "kind,id,name,mimeType"
_FILE_LIST_FIELDS = f"kind,incompleteSearch,nextPageToken,files({_FILE_FIELDS})"
_PERMISSION_FIELDS = "kind,id,type,role"
_PERMISSION_LIST_FIELDS = ("kind,nextPageToken,permissions(kind,id,type,emailAddress,domain,"
                           "role,displayName,allowFileDiscovery,deleted)")

_NATIVE_PREFIX = "application/vnd.google-apps."
_EXPORTS = {
    DOC_MIME: ("text/plain", "text/html", "text/markdown", "application/pdf", "application/rtf",
               "application/vnd.oasis.opendocument.text", "application/epub+zip",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    SHEET_MIME: ("text/csv", "text/tab-separated-values", "application/pdf", "application/zip",
                 "application/x-vnd.oasis.opendocument.spreadsheet",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    SLIDES_MIME: ("text/plain", "application/pdf",
                  "application/vnd.oasis.opendocument.presentation",
                  "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}


def _file(file_id: str) -> dict:
    record = STATE["drive_files"].get(file_id)
    if not record:
        raise ApiError(404, f"File not found: {file_id}.", reason="notFound", location="fileId")
    return record


def _is_native(file: dict) -> bool:
    return str(file.get("mimeType", "")).startswith(_NATIVE_PREFIX)


def _content_bytes(file_id: str) -> bytes:
    value = STATE["drive_content"].get(file_id)
    if value is None:
        return b""
    if isinstance(value, dict):
        return _unb64(value.get("base64", ""), field="content")
    return str(value).encode()


def _set_content(file_id: str, data: bytes) -> None:
    """Text content stays readable in ``/_state``; anything else is base64."""
    try:
        STATE["drive_content"][file_id] = data.decode()
    except UnicodeDecodeError:
        STATE["drive_content"][file_id] = {"base64": _b64(data)}


def _drive_user() -> dict:
    profile = STATE["user_profile"]
    return {"kind": "drive#user", "displayName": profile.get("displayName") or profile["emailAddress"],
            "emailAddress": profile["emailAddress"], "me": True,
            "permissionId": _owner_permission_id()}


def _owner_permission_id() -> str:
    return str(int(hashlib.sha1(_me().encode()).hexdigest()[:12], 16))  # noqa: S324 - an identifier the vendor shapes, not a secret


def _web_view_link(file: dict) -> str:
    kinds = {DOC_MIME: "document", SHEET_MIME: "spreadsheets", SLIDES_MIME: "presentation"}
    if file.get("mimeType") == FOLDER_MIME:
        return f"https://drive.google.com/drive/folders/{file['id']}"
    if file.get("mimeType") in kinds:
        return f"https://docs.google.com/{kinds[file['mimeType']]}/d/{file['id']}/edit"
    return f"https://drive.google.com/file/d/{file['id']}/view?usp=drivesdk"


def _file_resource(file: dict) -> dict:
    """The full File resource; ``?fields=`` (or the default mask) trims it."""
    file_id = file["id"]
    permissions = STATE["drive_permissions"].get(file_id) or {}
    content = _content_bytes(file_id)
    modified = file.get("modifiedTime") or _rfc3339()
    out: dict[str, Any] = {
        "kind": "drive#file",
        "id": file_id,
        "name": file.get("name", "Untitled"),
        "mimeType": file.get("mimeType", "application/octet-stream"),
        "description": file.get("description", ""),
        "starred": bool(file.get("starred")),
        "trashed": bool(file.get("trashed")),
        "explicitlyTrashed": bool(file.get("trashed")),
        "parents": list(file.get("parents") or []),
        "spaces": ["drive"],
        "version": str(file.get("version", 1)),
        "webViewLink": file.get("webViewLink") or _web_view_link(file),
        "iconLink": f"https://drive-thirdparty.googleusercontent.com/16/type/{file.get('mimeType')}",
        "hasThumbnail": False,
        "viewedByMe": True,
        "createdTime": file.get("createdTime") or modified,
        "modifiedTime": modified,
        "modifiedByMeTime": modified,
        "modifiedByMe": True,
        "owners": [_drive_user()],
        "lastModifyingUser": _drive_user(),
        "shared": any(p.get("role") != "owner" for p in permissions.values()),
        "ownedByMe": True,
        "viewersCanCopyContent": True,
        "copyRequiresWriterPermission": False,
        "writersCanShare": True,
        "permissions": [_render_permission(p) for p in permissions.values()],
        "permissionIds": list(permissions),
        "quotaBytesUsed": "0" if _is_native(file) else str(len(content)),
        "isAppAuthorized": False,
        "capabilities": {"canEdit": True, "canComment": True, "canShare": True, "canCopy": True,
                         "canDelete": True, "canRename": True, "canTrash": True,
                         "canDownload": not _is_native(file),
                         "canAddChildren": file.get("mimeType") == FOLDER_MIME},
    }
    for key in ("properties", "appProperties", "folderColorRgb"):
        if file.get(key) is not None:
            out[key] = file[key]
    if _is_native(file):
        out["exportLinks"] = {mime: f"https://docs.google.com/feeds/download/export?id={file_id}"
                              f"&exportFormat={mime}" for mime in _EXPORTS.get(file["mimeType"], ())}
    else:
        extension = file.get("name", "").rpartition(".")[2] if "." in file.get("name", "") else ""
        out.update({
            "size": str(len(content)),
            "md5Checksum": hashlib.md5(content, usedforsecurity=False).hexdigest(),
            "fileExtension": extension,
            "fullFileExtension": extension,
            "originalFilename": file.get("name", ""),
            "webContentLink": f"https://drive.google.com/uc?id={file_id}&export=download",
        })
    return out


def _check_parents(parents: Iterable[str]) -> None:
    for parent in parents:
        if parent == ROOT_ID:
            continue
        folder = STATE["drive_files"].get(parent)
        if folder is None:
            raise ApiError(404, f"File not found: {parent}.", reason="notFound",
                           location="parents")
        if folder.get("mimeType") != FOLDER_MIME:
            raise ApiError(400, f"The specified parent is not a folder: {parent}",
                           reason="invalidArgument", location="parents")


def _create_file(meta: dict, *, content: bytes | None = None,
                 content_type: str | None = None) -> dict:
    parents = list(meta.get("parents") or [ROOT_ID])
    _check_parents(parents)
    mime = meta.get("mimeType") or content_type or "application/octet-stream"
    now = _rfc3339()
    file_id = meta.get("id") or _file_id()
    file = {
        "id": file_id,
        "name": meta.get("name") or "Untitled",
        "mimeType": mime,
        "parents": parents,
        "description": meta.get("description", ""),
        "starred": bool(meta.get("starred")),
        "trashed": bool(meta.get("trashed")),
        "createdTime": meta.get("createdTime") or now,
        "modifiedTime": meta.get("modifiedTime") or now,
        "version": 1,
    }
    for key in ("properties", "appProperties", "folderColorRgb"):
        if key in meta:
            file[key] = meta[key]
    STATE["drive_files"][file_id] = file
    STATE["drive_permissions"][file_id] = {_owner_permission_id(): {
        "id": _owner_permission_id(), "type": "user", "role": "owner",
        "emailAddress": _me(), "displayName": STATE["user_profile"].get("displayName", ""),
        "kind": "drive#permission",
    }}
    if content is not None:
        _set_content(file_id, content)
    return file


def _touch(file: dict) -> dict:
    file["modifiedTime"] = _rfc3339()
    file["version"] = int(file.get("version", 1)) + 1
    return file


def _descendants(file_id: str) -> list[dict]:
    children = [f for f in STATE["drive_files"].values() if file_id in (f.get("parents") or [])]
    return [child for f in children for child in [f, *_descendants(f["id"])]]


def _remove_file(file: dict) -> None:
    """Permanently delete a file, and everything under it when it is a folder."""
    for record in [*_descendants(file["id"]), file]:
        STATE["drive_files"].pop(record["id"], None)
        STATE["drive_permissions"].pop(record["id"], None)
        STATE["drive_content"].pop(record["id"], None)


# --- Drive search (the ``q`` parameter) --------------------------------------

_DRIVE_TOKEN = re.compile(r"""\s*(?:
      (?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    | (?P<op><=|>=|!=|=|<|>)
    | (?P<lparen>\() | (?P<rparen>\)) | (?P<lbrace>\{) | (?P<rbrace>\})
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<comma>,)
    | (?P<junk>\S)
)""", re.VERBOSE)

_DRIVE_STRINGS = ("name", "mimeType", "fullText", "description", "originalFilename")
_DRIVE_BOOLS = ("trashed", "starred", "sharedWithMe", "hidden", "viewedByMe", "writersCanShare")
_DRIVE_TIMES = ("modifiedTime", "createdTime", "viewedByMeTime", "sharedWithMeTime",
                "modifiedByMeTime")
_DRIVE_COLLECTIONS = ("parents", "owners", "writers", "readers")


def _invalid_query(detail: str) -> ApiError:
    return ApiError(400, f"Invalid Value: {detail}", reason="invalid", location="q")


def _file_text(file: dict) -> str:
    """Everything ``fullText contains`` searches: name, description and content."""
    return " ".join([file.get("name", ""), file.get("description", ""),
                     _content_bytes(file["id"]).decode("utf-8", "ignore")])


def _prefix_match(haystack: str, needle: str) -> bool:
    """Drive's ``contains`` matches the start of a word, not any substring."""
    hay, want = haystack.lower(), needle.lower()
    if not want:
        return True
    return any(hay.startswith(want, index) for index in range(len(hay))
               if index == 0 or not hay[index - 1].isalnum())


def _parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _invalid_query(f"not a date: {value}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    return left >= right


def _drive_clause(field: str, op: str, value: Any) -> Callable[[dict], bool]:
    if field in _DRIVE_TIMES:
        moment = _parse_time(str(value))
        return lambda f: _compare(_parse_time(f.get(field) or "1970-01-01T00:00:00Z"), op, moment)
    if field in _DRIVE_BOOLS:
        wanted = value if isinstance(value, bool) else str(value).lower() == "true"
        return lambda f: _compare(bool(f.get(field, False)), op, wanted)
    if field == "fullText":
        needle = str(value)
        if op != "contains":
            raise _invalid_query("fullText supports only 'contains'")
        return lambda f: needle.lower() in _file_text(f).lower()
    if field in _DRIVE_STRINGS:
        needle = str(value)
        if op == "contains":
            if field == "name":
                return lambda f: _prefix_match(str(f.get(field, "")), needle)
            return lambda f: needle.lower() in str(f.get(field, "")).lower()
        return lambda f: _compare(str(f.get(field, "")), op, needle)
    raise _invalid_query(f"unsupported field {field!r}")


def _membership_clause(value: str, field: str) -> Callable[[dict], bool]:
    if field == "parents":
        return lambda f: value in (f.get("parents") or [])
    if field in ("owners", "writers", "readers"):
        def match(f: dict) -> bool:
            if field == "owners":
                return value.lower() == _me().lower()
            roles = {"writers": ("writer", "owner", "organizer", "fileOrganizer"),
                     "readers": ("reader", "commenter", "writer", "owner")}[field]
            return any(p.get("emailAddress", "").lower() == value.lower()
                       and p.get("role") in roles
                       for p in (STATE["drive_permissions"].get(f["id"]) or {}).values())

        return match
    raise _invalid_query(f"unsupported collection {field!r}")


def _drive_query(query: str) -> Callable[[dict], bool]:
    """Compile a Drive ``q`` expression into a predicate over file records."""
    if not (query or "").strip():
        return lambda f: True
    tokens = [m for m in _DRIVE_TOKEN.finditer(query) if m.group().strip()]
    position = 0

    def peek() -> str | None:
        return tokens[position].lastgroup if position < len(tokens) else None

    def text(index: int) -> str:
        token = tokens[index]
        raw = token.group()
        if token.lastgroup == "string":
            return re.sub(r"\\(.)", r"\1", raw.strip()[1:-1])
        return raw.strip()

    def take() -> tuple[str, str]:
        nonlocal position
        if position >= len(tokens):
            raise _invalid_query("unexpected end of query")
        kind, value = tokens[position].lastgroup or "", text(position)
        position += 1
        return kind, value

    def parse_or() -> Callable[[dict], bool]:
        nonlocal position
        parts = [parse_and()]
        while peek() == "word" and text(position).lower() == "or":
            position += 1
            parts.append(parse_and())
        return lambda f: any(part(f) for part in parts)

    def parse_and() -> Callable[[dict], bool]:
        nonlocal position
        parts = [parse_unary()]
        while peek() == "word" and text(position).lower() == "and":
            position += 1
            parts.append(parse_unary())
        return lambda f: all(part(f) for part in parts)

    def parse_unary() -> Callable[[dict], bool]:
        nonlocal position
        if peek() == "word" and text(position).lower() == "not":
            position += 1
            inner = parse_unary()
            return lambda f: not inner(f)
        if peek() == "lparen":
            position += 1
            inner = parse_or()
            if peek() != "rparen":
                raise _invalid_query("unbalanced parentheses")
            position += 1
            return inner
        return parse_clause()

    def parse_clause() -> Callable[[dict], bool]:
        nonlocal position
        kind, value = take()
        if kind == "string":  # "'<folder id>' in parents"
            _, keyword = take()
            if keyword.lower() != "in":
                raise _invalid_query("expected 'in'")
            _, collection = take()
            return _membership_clause(value, collection)
        if kind != "word":
            raise _invalid_query(f"unexpected {value!r}")
        field = value
        if field in _DRIVE_COLLECTIONS:
            raise _invalid_query(f"{field} is only usable as \"'id' in {field}\"")
        if peek() is None or peek() in ("rparen",) or (
                peek() == "word" and text(position).lower() in ("and", "or")):
            return _drive_clause(field, "=", True)  # a bare boolean, e.g. "starred"
        kind, operator = take()
        if kind == "word" and operator.lower() == "contains":
            _, needle = take()
            return _drive_clause(field, "contains", needle)
        if kind == "word" and operator.lower() == "has":
            return _properties_clause(field)
        if kind != "op":
            raise _invalid_query(f"unexpected {operator!r}")
        _, literal = take()
        return _drive_clause(field, operator, literal)

    def _properties_clause(field: str) -> Callable[[dict], bool]:
        """properties has { key='k' and value='v' }"""
        nonlocal position
        if peek() != "lbrace":
            raise _invalid_query("expected '{'")
        position += 1
        wanted: dict[str, str] = {}
        current = None
        while peek() is not None and peek() != "rbrace":
            kind, value = take()
            if kind == "word" and value.lower() in ("key", "value"):
                current = value.lower()
                if peek() == "op":
                    take()
                _, literal = take()
                wanted[current] = literal
        if peek() == "rbrace":
            position += 1
        return lambda f: any(
            (not wanted.get("key") or key == wanted["key"])
            and (not wanted.get("value") or str(val) == wanted["value"])
            for key, val in (f.get(field) or {}).items())

    predicate = parse_or()
    if position != len(tokens):
        raise _invalid_query(f"unexpected {text(position)!r}")
    return predicate


_ORDER_KEYS = {
    "name": lambda f: str(f.get("name", "")).lower(),
    "name_natural": lambda f: str(f.get("name", "")).lower(),
    "createdTime": lambda f: str(f.get("createdTime", "")),
    "modifiedTime": lambda f: str(f.get("modifiedTime", "")),
    "modifiedByMeTime": lambda f: str(f.get("modifiedTime", "")),
    "viewedByMeTime": lambda f: str(f.get("modifiedTime", "")),
    "recency": lambda f: str(f.get("modifiedTime", "")),
    "starred": lambda f: bool(f.get("starred")),
    "folder": lambda f: f.get("mimeType") != FOLDER_MIME,
    "quotaBytesUsed": lambda f: len(_content_bytes(f["id"])),
}


def _order_files(files: list[dict], order_by: str) -> list[dict]:
    ordered = sorted(files, key=lambda f: (str(f.get("modifiedTime", "")), f["id"]), reverse=True)
    for clause in reversed([c.strip() for c in order_by.split(",") if c.strip()]):
        key, _, direction = clause.partition(" ")
        if key not in _ORDER_KEYS:
            raise ApiError(400, f"Invalid Value: {key}", reason="invalid", location="orderBy")
        ordered = sorted(ordered, key=_ORDER_KEYS[key], reverse=direction.strip() == "desc")
    return ordered


# --- Drive: files ------------------------------------------------------------

@app.get("/drive/v3/about")
def drive_about(request: Request) -> Response:
    if not request.query_params.get("fields"):
        raise ApiError(400, "The 'fields' parameter is required for this method.",
                       reason="required", location="fields")
    used = sum(len(_content_bytes(fid)) for fid in STATE["drive_files"])
    return _reply(request, {
        "kind": "drive#about",
        "user": _drive_user(),
        "storageQuota": {"limit": str(15 * 1024 ** 3), "usage": str(used),
                         "usageInDrive": str(used), "usageInDriveTrash": "0"},
        "maxImportSizes": {}, "maxUploadSize": str(5 * 1024 ** 4),
        "appInstalled": False, "folderColorPalette": [],
    })


@app.get("/drive/v3/drives")
def drive_list_drives(request: Request) -> Response:
    # No shared drives in the twin, but the call must not 404 for agents that probe.
    return _reply(request, {"kind": "drive#driveList", "drives": []})


@app.get("/drive/v3/files/generateIds")
def drive_generate_ids(request: Request) -> Response:
    count = _int_param(request, "count", 10, maximum=1000)
    return _reply(request, {"kind": "drive#generatedIds", "space": "drive",
                            "ids": [_file_id() for _ in range(count)]})


@app.get("/drive/v3/files")
def drive_list_files(request: Request) -> Response:
    predicate = _drive_query(request.query_params.get("q", ""))
    files = _order_files([f for f in STATE["drive_files"].values() if predicate(f)],
                         request.query_params.get("orderBy", ""))
    window, next_token = _paginate(files, request, size_param="pageSize",
                                   default_size=100, max_size=1000)
    body: dict[str, Any] = {"kind": "drive#fileList", "incompleteSearch": False,
                            "files": [_file_resource(f) for f in window]}
    if next_token:
        body["nextPageToken"] = next_token
    return _reply(request, body, default=_FILE_LIST_FIELDS)


@app.post("/drive/v3/files")
async def drive_create_file(request: Request) -> Response:
    file = _create_file(await _body(request))
    return _reply(request, _file_resource(file), default=_FILE_FIELDS)


@app.delete("/drive/v3/files/trash")
def drive_empty_trash() -> Response:
    for file in [f for f in STATE["drive_files"].values() if f.get("trashed")]:
        _remove_file(file)
    return Response(status_code=204)


@app.get("/drive/v3/files/{file_id}")
def drive_get_file(file_id: str, request: Request) -> Response:
    if file_id == ROOT_ID:
        return _reply(request, {"kind": "drive#file", "id": ROOT_ID, "name": "My Drive",
                                "mimeType": FOLDER_MIME}, default=_FILE_FIELDS)
    file = _file(file_id)
    if request.query_params.get("alt") == "media":
        if _is_native(file):
            raise ApiError(403, "Only files with binary content can be downloaded. Use Export "
                                "with Docs Editors files.", reason="fileNotDownloadable")
        return Response(content=_content_bytes(file_id),
                        media_type=file.get("mimeType") or "application/octet-stream")
    return _reply(request, _file_resource(file), default=_FILE_FIELDS)


@app.get("/drive/v3/files/{file_id}/export")
def drive_export_file(file_id: str, request: Request) -> Response:
    file = _file(file_id)
    mime = request.query_params.get("mimeType", "")
    if not _is_native(file):
        raise ApiError(403, "Export only supports Docs Editors files.",
                       reason="fileNotExportable")
    if mime not in _EXPORTS.get(file["mimeType"], ()):
        raise ApiError(400, f"Export format {mime!r} is not supported for this document.",
                       reason="badRequest", location="mimeType")
    text = _content_bytes(file_id).decode("utf-8", "replace")
    if mime == "text/html":
        text = f"<html><body><p>{text}</p></body></html>"
    # Binary export targets (PDF, Office) carry the document's text: the twin
    # keeps content as text, and agents assert on what they exported.
    return Response(content=text.encode(), media_type=mime)


@app.patch("/drive/v3/files/{file_id}")
async def drive_update_file(file_id: str, request: Request) -> Response:
    file = _file(file_id)
    body = await _body(request)
    if "parents" in body:
        raise ApiError(403, "The parents field is not directly writable in update requests. "
                            "Use the addParents and removeParents parameters instead.",
                       reason="fieldNotWritable")
    for field in ("name", "description", "starred", "trashed", "mimeType", "properties",
                  "appProperties", "folderColorRgb", "modifiedTime"):
        if field in body:
            file[field] = body[field]
    add = _repeated(request, "addParents")
    remove = set(_repeated(request, "removeParents"))
    if add or remove:
        _check_parents(add)
        parents = [p for p in (file.get("parents") or []) if p not in remove]
        file["parents"] = parents + [p for p in add if p not in parents]
    _touch(file)
    return _reply(request, _file_resource(file), default=_FILE_FIELDS)


@app.delete("/drive/v3/files/{file_id}")
def drive_delete_file(file_id: str) -> Response:
    _remove_file(_file(file_id))
    return Response(status_code=204)


@app.post("/drive/v3/files/{file_id}/copy")
async def drive_copy_file(file_id: str, request: Request) -> Response:
    source = _file(file_id)
    if source.get("mimeType") == FOLDER_MIME:
        raise ApiError(403, "Cannot copy a folder.", reason="cannotCopyFile")
    body = await _body(request)
    copy = _create_file({
        "name": body.get("name") or f"Copy of {source.get('name', 'Untitled')}",
        "mimeType": body.get("mimeType") or source.get("mimeType"),
        "parents": body.get("parents") or source.get("parents") or [ROOT_ID],
        "description": body.get("description", source.get("description", "")),
        "starred": body.get("starred", False),
    }, content=_content_bytes(file_id))
    return _reply(request, _file_resource(copy), default=_FILE_FIELDS)


# --- Drive: uploads ----------------------------------------------------------

async def _upload_parts(request: Request) -> tuple[dict, bytes, str | None]:
    """(metadata, content, content type) of a ``media`` or ``multipart`` upload."""
    raw = await request.body()
    content_type = request.headers.get("content-type", "")
    if request.query_params.get("uploadType") == "media" or not content_type.startswith(
            "multipart/"):
        return {}, raw, content_type.partition(";")[0] or None
    envelope = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
    parsed = message_from_bytes(envelope, policy=policy.default)
    metadata: dict = {}
    content, part_type = b"", None
    for part in parsed.iter_parts():
        payload = part.get_payload(decode=True) or b""
        if part.get_content_type() == "application/json" and not metadata:
            try:
                metadata = json.loads(payload or b"{}")
            except json.JSONDecodeError:
                raise ApiError(400, "Invalid JSON payload received.",
                               reason="parseError") from None
        else:
            content, part_type = payload, part.get_content_type()
    return metadata, content, part_type


def _external_base(request: Request) -> str:
    """The origin the caller used, so a resumable session URL points back at it."""
    host = request.headers.get("host") or request.url.netloc
    scheme = "https" if host.endswith("googleapis.com") else request.url.scheme
    return f"{scheme}://{host}"


async def _start_resumable(request: Request, kind: str, target: str | None = None) -> Response:
    metadata = await _body(request)
    upload_id = uuid.uuid4().hex
    STATE["_uploads"][upload_id] = {
        "kind": kind, "target": target, "metadata": metadata, "data": "",
        "contentType": request.headers.get("x-upload-content-type"),
        # The chunk PUTs go to the session URL, so the session remembers the
        # field mask the caller asked for when it started the upload.
        "fields": request.query_params.get("fields"),
    }
    location = (f"{_external_base(request)}{request.url.path}"
                f"?uploadType=resumable&upload_id={upload_id}")
    return Response(status_code=200, headers={"Location": location,
                                              "X-GUploader-UploadID": upload_id})


async def _resume_upload(request: Request) -> Response:
    """A chunk PUT against a resumable session: 308 while incomplete, then the resource."""
    upload_id = request.query_params.get("upload_id", "")
    session = STATE["_uploads"].get(upload_id)
    if session is None:
        raise ApiError(404, f"Upload session not found: {upload_id}", reason="notFound")
    chunk = await request.body()
    buffer = _unb64(session["data"], field="content") if session["data"] else b""
    total: int | None = None
    match = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", request.headers.get("content-range", ""))
    if match:
        start, size = int(match.group(1)), match.group(3)
        buffer = buffer[:start] + chunk
        total = None if size == "*" else int(size)
    else:
        buffer += chunk
        total = len(buffer)
    session["data"] = _b64(buffer)
    if total is None or len(buffer) < total:
        return Response(status_code=308, headers={"Range": f"bytes=0-{max(len(buffer) - 1, 0)}",
                                                  "X-GUploader-UploadID": upload_id})
    del STATE["_uploads"][upload_id]
    return _finish_upload(request, session, buffer)


def _finish_upload(request: Request, session: dict, content: bytes) -> Response:
    metadata, kind = session.get("metadata") or {}, session["kind"]
    content_type, fields = session.get("contentType"), session.get("fields")
    if kind == "drive.create":
        file = _create_file(metadata, content=content, content_type=content_type)
        return _reply(request, _file_resource(file), default=_FILE_FIELDS, fields=fields)
    if kind == "drive.update":
        file = _file(str(session.get("target")))
        for field in ("name", "description", "mimeType"):
            if field in metadata:
                file[field] = metadata[field]
        _set_content(file["id"], content)
        _touch(file)
        return _reply(request, _file_resource(file), default=_FILE_FIELDS, fields=fields)
    raw = content or _unb64(metadata.get("raw", ""))
    if kind == "gmail.send":
        message = _send(raw, thread_id=metadata.get("threadId"))
    else:
        message = _store_message(raw, labels=metadata.get("labelIds") or ["INBOX", "UNREAD"],
                                 thread_id=metadata.get("threadId"))
    return _reply(request, _render_message(message, "minimal"), fields=fields)


@app.post("/upload/drive/v3/files")
async def drive_upload_create(request: Request) -> Response:
    if request.query_params.get("uploadType") == "resumable":
        return await _start_resumable(request, "drive.create")
    metadata, content, content_type = await _upload_parts(request)
    file = _create_file(metadata, content=content, content_type=content_type)
    return _reply(request, _file_resource(file), default=_FILE_FIELDS)


@app.put("/upload/drive/v3/files")
async def drive_upload_resume(request: Request) -> Response:
    return await _resume_upload(request)


@app.patch("/upload/drive/v3/files/{file_id}")
async def drive_upload_update(file_id: str, request: Request) -> Response:
    file = _file(file_id)
    if request.query_params.get("uploadType") == "resumable":
        return await _start_resumable(request, "drive.update", target=file_id)
    metadata, content, content_type = await _upload_parts(request)
    for field in ("name", "description", "mimeType"):
        if field in metadata:
            file[field] = metadata[field]
    if content_type and not metadata.get("mimeType") and not _is_native(file):
        file["mimeType"] = content_type
    _set_content(file_id, content)
    _touch(file)
    return _reply(request, _file_resource(file), default=_FILE_FIELDS)


@app.put("/upload/drive/v3/files/{file_id}")
async def drive_upload_update_resume(file_id: str, request: Request) -> Response:
    return await _resume_upload(request)


@app.post("/upload/gmail/v1/users/{user_id}/messages/send")
async def gmail_upload_send(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    if request.query_params.get("uploadType") == "resumable":
        return await _start_resumable(request, "gmail.send")
    metadata, content, _ = await _upload_parts(request)
    raw = content or _unb64(metadata.get("raw", ""))
    return _reply(request, _render_message(_send(raw, thread_id=metadata.get("threadId")),
                                           "minimal"))


@app.put("/upload/gmail/v1/users/{user_id}/messages/send")
async def gmail_upload_send_resume(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    return await _resume_upload(request)


@app.post("/upload/gmail/v1/users/{user_id}/messages")
async def gmail_upload_insert(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    if request.query_params.get("uploadType") == "resumable":
        return await _start_resumable(request, "gmail.insert")
    metadata, content, _ = await _upload_parts(request)
    raw = content or _unb64(metadata.get("raw", ""))
    message = _store_message(raw, labels=metadata.get("labelIds") or ["INBOX", "UNREAD"],
                             thread_id=metadata.get("threadId"))
    return _reply(request, _render_message(message, "minimal"))


@app.put("/upload/gmail/v1/users/{user_id}/messages")
async def gmail_upload_insert_resume(user_id: str, request: Request) -> Response:
    _check_user(user_id)
    return await _resume_upload(request)


# --- Drive: permissions ------------------------------------------------------

def _render_permission(permission: dict) -> dict:
    out = {"kind": "drive#permission", "id": permission["id"],
           "type": permission.get("type", "user"), "role": permission.get("role", "reader")}
    for key in ("emailAddress", "domain", "displayName", "allowFileDiscovery", "expirationTime",
                "deleted", "pendingOwner", "photoLink"):
        if permission.get(key) is not None:
            out[key] = permission[key]
    return out


_ROLES = ("owner", "organizer", "fileOrganizer", "writer", "commenter", "reader")
_PERMISSION_TYPES = ("user", "group", "domain", "anyone")


@app.get("/drive/v3/files/{file_id}/permissions")
def drive_list_permissions(file_id: str, request: Request) -> Response:
    _file(file_id)
    permissions = list((STATE["drive_permissions"].get(file_id) or {}).values())
    window, next_token = _paginate(permissions, request, size_param="pageSize",
                                   default_size=100, max_size=100)
    body: dict[str, Any] = {"kind": "drive#permissionList",
                            "permissions": [_render_permission(p) for p in window]}
    if next_token:
        body["nextPageToken"] = next_token
    return _reply(request, body, default=_PERMISSION_LIST_FIELDS)


@app.post("/drive/v3/files/{file_id}/permissions")
async def drive_create_permission(file_id: str, request: Request) -> Response:
    _file(file_id)
    body = await _body(request)
    kind, role = body.get("type"), body.get("role")
    if kind not in _PERMISSION_TYPES:
        raise ApiError(400, "The permission type field is required.", reason="required",
                       location="type")
    if role not in _ROLES:
        raise ApiError(400, "The permission role field is required.", reason="required",
                       location="role")
    if kind in ("user", "group") and not body.get("emailAddress"):
        raise ApiError(400, "The email address field is required for user and group permissions.",
                       reason="required", location="emailAddress")
    if kind == "domain" and not body.get("domain"):
        raise ApiError(400, "The domain field is required for domain permissions.",
                       reason="required", location="domain")
    if role == "owner" and not _bool_param(request, "transferOwnership"):
        raise ApiError(403, "Ownership transfer requires the transferOwnership parameter.",
                       reason="insufficientFilePermissions")
    permissions = STATE["drive_permissions"].setdefault(file_id, {})
    email = body.get("emailAddress")
    existing = next((p for p in permissions.values()
                     if p.get("type") == kind and p.get("emailAddress") == email
                     and p.get("domain") == body.get("domain")), None)
    if existing is not None:  # re-sharing with the same grantee updates the role
        existing["role"] = role
        return _reply(request, _render_permission(existing), default=_PERMISSION_FIELDS)
    permission = {"id": "anyoneWithLink" if kind == "anyone" else _permission_id(),
                  "type": kind, "role": role, "kind": "drive#permission"}
    for key in ("emailAddress", "domain", "displayName", "allowFileDiscovery", "expirationTime"):
        if body.get(key) is not None:
            permission[key] = body[key]
    permission.setdefault("displayName", email or body.get("domain", ""))
    permissions[permission["id"]] = permission
    return _reply(request, _render_permission(permission), default=_PERMISSION_FIELDS)


def _permission(file_id: str, permission_id: str) -> dict:
    _file(file_id)
    permission = (STATE["drive_permissions"].get(file_id) or {}).get(permission_id)
    if not permission:
        raise ApiError(404, f"Permission not found: {permission_id}.", reason="notFound",
                       location="permissionId")
    return permission


@app.get("/drive/v3/files/{file_id}/permissions/{permission_id}")
def drive_get_permission(file_id: str, permission_id: str, request: Request) -> Response:
    return _reply(request, _render_permission(_permission(file_id, permission_id)),
                  default=_PERMISSION_FIELDS)


@app.patch("/drive/v3/files/{file_id}/permissions/{permission_id}")
async def drive_update_permission(file_id: str, permission_id: str,
                                  request: Request) -> Response:
    permission = _permission(file_id, permission_id)
    body = await _body(request)
    if body.get("role") and body["role"] not in _ROLES:
        raise ApiError(400, f"Invalid value for role: {body['role']}", reason="invalid",
                       location="role")
    for field in ("role", "expirationTime"):
        if field in body:
            permission[field] = body[field]
    return _reply(request, _render_permission(permission), default=_PERMISSION_FIELDS)


@app.delete("/drive/v3/files/{file_id}/permissions/{permission_id}")
def drive_delete_permission(file_id: str, permission_id: str) -> Response:
    _permission(file_id, permission_id)
    del STATE["drive_permissions"][file_id][permission_id]
    return Response(status_code=204)


# =============================================================================
# Calendar
# =============================================================================

def _calendar(calendar_id: str) -> dict:
    if calendar_id in ("primary", _me()):
        primary = next((c for c in STATE["calendars"].values() if c.get("primary")), None)
        if primary is not None:
            return primary
    calendar = STATE["calendars"].get(calendar_id)
    if not calendar:
        raise ApiError(404, "Not Found", reason="notFound")
    return calendar


def _event(calendar_id: str, event_id: str) -> dict:
    event = STATE["calendar_events"].get(event_id)
    if not event or event.get("_calendarId") != calendar_id:
        raise ApiError(404, "Not Found", reason="notFound")
    return event


def _event_time(slot: Any) -> datetime | None:
    if not isinstance(slot, dict):
        return None
    if slot.get("dateTime"):
        return _parse_time(str(slot["dateTime"]))
    if slot.get("date"):
        try:
            return datetime.combine(date.fromisoformat(str(slot["date"])),
                                    datetime.min.time(), UTC)
        except ValueError:
            raise ApiError(400, f"Invalid date: {slot['date']}", reason="invalid") from None
    return None


def _event_resource(event: dict) -> dict:
    """The Event resource, in the field order the API returns it."""
    stored = {key: value for key, value in event.items() if not key.startswith("_")}
    calendar_id = event.get("_calendarId", "")
    digest = hashlib.sha1(  # noqa: S324 - an identifier the vendor shapes, not a secret
        json.dumps(stored, sort_keys=True, default=str).encode()).hexdigest()
    out: dict[str, Any] = {
        "kind": "calendar#event",
        "etag": f'"{digest[:16]}"',
        "id": event["id"],
        "status": stored.get("status", "confirmed"),
        "htmlLink": f"https://www.google.com/calendar/event?eid={event['id']}",
        "created": stored.get("created", ""),
        "updated": stored.get("updated", ""),
        "creator": {"email": _me(), "self": True},
        "organizer": {"email": calendar_id, "displayName": calendar_id, "self": True},
        "iCalUID": f"{event['id']}@google.com",
        "sequence": 0,
        "reminders": {"useDefault": True},
        "eventType": "default",
    }
    out.update(stored)
    return out


def _event_body(body: dict, calendar_id: str, *, existing: dict | None = None) -> dict:
    start, end = body.get("start", (existing or {}).get("start")), body.get("end", (existing or {}).get("end"))
    starts_at, ends_at = _event_time(start), _event_time(end)
    if starts_at is None:
        raise ApiError(400, "Missing start time.", reason="required", location="start")
    if ends_at is None:
        raise ApiError(400, "Missing end time.", reason="required", location="end")
    if ends_at < starts_at:
        raise ApiError(400, "The specified time range is empty.", reason="timeRangeEmpty")
    now = _rfc3339()
    event = dict(existing or {})
    event.update({key: value for key, value in body.items()
                  if key not in ("id", "kind", "etag", "created", "updated", "iCalUID")})
    event["start"], event["end"] = start, end
    event.setdefault("id", body.get("id") or _event_id())
    event.setdefault("created", now)
    event.setdefault("status", "confirmed")
    event["updated"] = now
    event["_calendarId"] = calendar_id
    event["attendees"] = [
        {**attendee, "responseStatus": attendee.get("responseStatus", "needsAction")}
        for attendee in body.get("attendees", event.get("attendees") or [])
        if isinstance(attendee, dict)
    ] or event.get("attendees") or []
    if not event["attendees"]:
        event.pop("attendees")
    return event


@app.get("/calendar/v3/users/me/calendarList")
def calendar_list_calendars(request: Request) -> Response:
    items = [{**calendar, "kind": "calendar#calendarListEntry",
              "accessRole": calendar.get("accessRole", "owner"),
              "selected": True, "defaultReminders": []}
             for calendar in STATE["calendars"].values()]
    return _reply(request, {"kind": "calendar#calendarList", "items": items,
                            "nextSyncToken": _page_token(len(items))})


@app.get("/calendar/v3/users/me/calendarList/{calendar_id}")
def calendar_get_calendar_list_entry(calendar_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    return _reply(request, {**calendar, "kind": "calendar#calendarListEntry",
                            "accessRole": calendar.get("accessRole", "owner"),
                            "selected": True, "defaultReminders": []})


@app.get("/calendar/v3/calendars/{calendar_id}")
def calendar_get_calendar(calendar_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    return _reply(request, {"kind": "calendar#calendar", "id": calendar["id"],
                            "summary": calendar.get("summary", calendar["id"]),
                            "timeZone": calendar.get("timeZone", "UTC"),
                            "description": calendar.get("description", "")})


@app.post("/calendar/v3/calendars")
async def calendar_create_calendar(request: Request) -> Response:
    body = await _body(request)
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise ApiError(400, "Missing summary.", reason="required", location="summary")
    calendar = {"id": f"{uuid.uuid4().hex}@group.calendar.google.com", "summary": summary,
                "timeZone": body.get("timeZone", "UTC"), "primary": False,
                "accessRole": "owner", "description": body.get("description", "")}
    STATE["calendars"][calendar["id"]] = calendar
    return _reply(request, {**calendar, "kind": "calendar#calendar"})


@app.delete("/calendar/v3/calendars/{calendar_id}")
def calendar_delete_calendar(calendar_id: str) -> Response:
    calendar = _calendar(calendar_id)
    if calendar.get("primary"):
        raise ApiError(403, "Cannot delete the primary calendar.", reason="forbidden")
    del STATE["calendars"][calendar["id"]]
    for event_id, event in list(STATE["calendar_events"].items()):
        if event.get("_calendarId") == calendar["id"]:
            del STATE["calendar_events"][event_id]
    return Response(status_code=204)


@app.get("/calendar/v3/calendars/{calendar_id}/events")
def calendar_list_events(calendar_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    params = request.query_params
    order_by = params.get("orderBy", "")
    if order_by == "startTime" and not _bool_param(request, "singleEvents"):
        raise ApiError(400, "The orderBy startTime is only available when querying single "
                            "events.", reason="invalidParameter", location="orderBy")
    time_min = _parse_time(params["timeMin"]) if params.get("timeMin") else None
    time_max = _parse_time(params["timeMax"]) if params.get("timeMax") else None
    needle = (params.get("q") or "").lower()
    events = []
    for event in STATE["calendar_events"].values():
        if event.get("_calendarId") != calendar["id"]:
            continue
        if event.get("status") == "cancelled" and not _bool_param(request, "showDeleted"):
            continue
        starts_at, ends_at = _event_time(event.get("start")), _event_time(event.get("end"))
        # timeMin bounds the event's end, timeMax its start — Google's semantics.
        if time_min and ends_at and ends_at <= time_min:
            continue
        if time_max and starts_at and starts_at >= time_max:
            continue
        if params.get("iCalUID") and f"{event['id']}@google.com" != params["iCalUID"]:
            continue
        if needle and needle not in json.dumps(event, default=str).lower():
            continue
        events.append(event)
    key = (lambda e: str(e.get("updated", ""))) if order_by == "updated" else (
        lambda e: (_event_time(e.get("start")) or datetime.min.replace(tzinfo=UTC), e["id"]))
    events.sort(key=key)
    window, next_token = _paginate(events, request, size_param="maxResults",
                                   default_size=250, max_size=2500)
    body: dict[str, Any] = {
        "kind": "calendar#events",
        "summary": calendar.get("summary", calendar["id"]),
        "updated": _rfc3339(),
        "timeZone": calendar.get("timeZone", "UTC"),
        "accessRole": calendar.get("accessRole", "owner"),
        "defaultReminders": [],
        "items": [_event_resource(event) for event in window],
    }
    if next_token:
        body["nextPageToken"] = next_token
    else:
        body["nextSyncToken"] = _page_token(len(events))
    return _reply(request, body)


@app.post("/calendar/v3/calendars/{calendar_id}/events")
async def calendar_insert_event(calendar_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    event = _event_body(await _body(request), calendar["id"])
    STATE["calendar_events"][event["id"]] = event
    return _reply(request, _event_resource(event))


@app.post("/calendar/v3/calendars/{calendar_id}/events/quickAdd")
def calendar_quick_add_event(calendar_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    text = request.query_params.get("text", "").strip()
    if not text:
        raise ApiError(400, "Missing text.", reason="required", location="text")
    start = _now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    event = _event_body({"summary": text,
                         "start": {"dateTime": _rfc3339(start, millis=False)},
                         "end": {"dateTime": _rfc3339(start + timedelta(hours=1), millis=False)}},
                        calendar["id"])
    STATE["calendar_events"][event["id"]] = event
    return _reply(request, _event_resource(event))


@app.get("/calendar/v3/calendars/{calendar_id}/events/{event_id}")
def calendar_get_event(calendar_id: str, event_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    return _reply(request, _event_resource(_event(calendar["id"], event_id)))


@app.patch("/calendar/v3/calendars/{calendar_id}/events/{event_id}")
async def calendar_patch_event(calendar_id: str, event_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    existing = _event(calendar["id"], event_id)
    updated = _event_body(await _body(request), calendar["id"], existing=existing)
    STATE["calendar_events"][event_id] = updated
    return _reply(request, _event_resource(updated))


@app.put("/calendar/v3/calendars/{calendar_id}/events/{event_id}")
async def calendar_update_event(calendar_id: str, event_id: str, request: Request) -> Response:
    calendar = _calendar(calendar_id)
    existing = _event(calendar["id"], event_id)
    body = await _body(request)
    replacement = _event_body(body, calendar["id"],
                              existing={"id": event_id, "created": existing.get("created")})
    STATE["calendar_events"][event_id] = replacement
    return _reply(request, _event_resource(replacement))


@app.delete("/calendar/v3/calendars/{calendar_id}/events/{event_id}")
def calendar_delete_event(calendar_id: str, event_id: str) -> Response:
    calendar = _calendar(calendar_id)
    event = _event(calendar["id"], event_id)
    if event.get("status") == "cancelled":
        raise ApiError(410, "Resource has been deleted", reason="deleted")
    # Calendar keeps a cancelled tombstone rather than dropping the row.
    event["status"] = "cancelled"
    event["updated"] = _rfc3339()
    return Response(status_code=204)


@app.post("/calendar/v3/freeBusy")
async def calendar_free_busy(request: Request) -> Response:
    body = await _body(request)
    time_min = _parse_time(body["timeMin"]) if body.get("timeMin") else _now()
    time_max = _parse_time(body["timeMax"]) if body.get("timeMax") else _now() + timedelta(days=7)
    calendars: dict[str, Any] = {}
    for item in body.get("items") or [{"id": "primary"}]:
        requested = str(item.get("id", "primary"))
        try:
            calendar = _calendar(requested)
        except ApiError:
            calendars[requested] = {"errors": [{"domain": "global", "reason": "notFound"}],
                                    "busy": []}
            continue
        busy = []
        for event in STATE["calendar_events"].values():
            if event.get("_calendarId") != calendar["id"] or event.get("status") == "cancelled":
                continue
            starts_at, ends_at = _event_time(event.get("start")), _event_time(event.get("end"))
            if not starts_at or not ends_at or ends_at <= time_min or starts_at >= time_max:
                continue
            if str(event.get("transparency")) == "transparent":
                continue
            busy.append({"start": _rfc3339(starts_at, millis=False),
                         "end": _rfc3339(ends_at, millis=False)})
        calendars[requested] = {"busy": sorted(busy, key=lambda slot: slot["start"])}
    return _reply(request, {"kind": "calendar#freeBusy",
                            "timeMin": _rfc3339(time_min, millis=False),
                            "timeMax": _rfc3339(time_max, millis=False),
                            "calendars": calendars})


# =============================================================================
# OAuth 2 token endpoint and batch fan-out
# =============================================================================

_GRANTS = ("refresh_token", "authorization_code", "client_credentials",
           "urn:ietf:params:oauth:grant-type:jwt-bearer")


@app.post("/token")
@app.post("/oauth2/v4/token")
@app.post("/oauth2/v3/token")
@app.post("/o/oauth2/token")
async def oauth_token(request: Request) -> Response:
    """Mint an access token, so refresh and service-account flows stay in the sandbox."""
    form: dict[str, Any] = {key: value for key, value in (await request.form()).items()}
    if not form:
        form = await _body(request)
    grant = str(form.get("grant_type") or "")
    if grant not in _GRANTS:
        # OAuth errors have their own shape; google-auth reads error_description.
        return JSONResponse(status_code=400, content={
            "error": "unsupported_grant_type",
            "error_description": f"Invalid grant_type: {grant or 'missing'}",
        })
    token: dict[str, Any] = {"access_token": _bootstrap_token(), "expires_in": 3599,
                             "token_type": "Bearer",
                             "scope": form.get("scope", "https://www.googleapis.com/auth/drive "
                                                        "https://mail.google.com/")}
    if grant == "authorization_code":
        token["refresh_token"] = "1//checkpoint-fake-refresh-token"
    return JSONResponse(token)


def _parse_sub_request(text: str) -> tuple[str, str, dict[str, str], bytes]:
    head, _, body = text.replace("\r\n", "\n").partition("\n\n")
    lines = [line for line in head.split("\n") if line.strip()]
    method, _, rest = (lines[0] if lines else "GET / HTTP/1.1").partition(" ")
    url = rest.rsplit(" HTTP/", 1)[0].strip() or "/"
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return method.strip().upper(), url, headers, body.encode()


_REASON_PHRASES = {200: "OK", 201: "Created", 204: "No Content", 308: "Resume Incomplete",
                   400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                   409: "Conflict", 429: "Too Many Requests", 500: "Internal Server Error"}


@app.post("/batch")
@app.post("/batch/{api}/{version}")
async def batch_request(request: Request) -> Response:
    """Run a multipart batch: each part is dispatched as its own traced call."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/"):
        raise ApiError(400, "Batch requests must be sent as multipart/mixed.",
                       reason="badRequest")
    raw = await request.body()
    envelope = message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw,
                                  policy=policy.default)
    boundary = f"batch_{uuid.uuid4().hex}"
    chunks: list[str] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://twin") as client:
        for part in envelope.iter_parts():
            payload = part.get_payload(decode=True)
            text = payload.decode("utf-8", "replace") if payload else str(part.get_payload())
            method, url, headers, body = _parse_sub_request(text)
            headers.setdefault("authorization", request.headers.get("authorization", ""))
            headers.pop("host", None)
            headers.pop("content-length", None)
            response = await client.request(method, url, headers=headers,
                                            content=body or None)
            content_id = (part.get("Content-ID") or "<0>").strip()
            reply_id = f"<response-{content_id[1:]}" if content_id.startswith("<") else content_id
            phrase = _REASON_PHRASES.get(response.status_code, "OK")
            chunks.append(
                f"--{boundary}\r\n"
                "Content-Type: application/http\r\n"
                "Content-Transfer-Encoding: binary\r\n"
                f"Content-ID: {reply_id}\r\n"
                "\r\n"
                f"HTTP/1.1 {response.status_code} {phrase}\r\n"
                f"Content-Type: {response.headers.get('content-type', 'application/json')}\r\n"
                f"Content-Length: {len(response.content)}\r\n"
                "\r\n"
                f"{response.text}\r\n"
            )
    chunks.append(f"--{boundary}--")
    return Response(content="".join(chunks),
                    media_type=f'multipart/mixed; boundary="{boundary}"')


# =============================================================================
# Seeds, views and call classification
# =============================================================================

def _normalize_message(message: dict) -> None:
    """Bring a seeded message up to what the API would have stored."""
    payload = message.setdefault("payload", {})
    payload.setdefault("partId", "")
    payload.setdefault("mimeType", "text/plain")
    payload.setdefault("filename", "")
    payload.setdefault("headers", [])
    for part in _walk_parts(payload):
        body = part.setdefault("body", {})
        data = body.get("data")
        if data and not _looks_base64(str(data)):
            body["data"] = _b64(str(data).encode())  # seeds may write the body as plain text
        if body.get("data"):
            body["size"] = len(_unb64(body["data"], field="body.data"))
            if part.get("filename"):
                body.setdefault("attachmentId", _attachment_id(_unb64(body["data"])))
    _set_header(payload, "Date", _normalize_date(_header(payload, "Date")))
    if not _header(payload, "Message-ID"):
        _set_header(payload, "Message-ID", _message_id_header())
    message.setdefault("labelIds", [])
    message["threadId"] = message.get("threadId") or message["id"]
    if not message.get("snippet"):
        message["snippet"] = _snippet(_body_text(payload))
    if not message.get("internalDate"):
        when = parsedate_to_datetime(_header(payload, "Date"))
        message["internalDate"] = str(int(when.timestamp() * 1000))
    if not message.get("_raw"):
        message["_raw"] = _b64(_rebuild_mime(payload))
    message.setdefault("sizeEstimate", len(_unb64(message["_raw"], field="raw")))
    message.setdefault("historyId", STATE["user_profile"].get("historyId", "1"))


def _after_seed(state: dict) -> None:
    """Normalize a hand-written seed into the shapes the API serves.

    ``state`` is the twin's live state, so the module helpers see the same dict.
    """
    profile = state["user_profile"]
    primary = next((c for c in state["calendars"].values() if c.get("primary")), None)
    if primary is not None and primary["id"] != profile["emailAddress"]:
        # The primary calendar is the account's own address.
        state["calendars"].pop(primary["id"], None)
        primary.update({"id": profile["emailAddress"], "summary": profile["emailAddress"]})
        state["calendars"][primary["id"]] = primary

    for draft_id, draft in list(state["gmail_drafts"].items()):
        message = draft.get("message")
        if isinstance(message, dict):  # seeds may inline the draft's message
            message.setdefault("id", _gmail_id())
            message.setdefault("labelIds", ["DRAFT"])
            state["gmail_messages"].setdefault(message["id"], message)
        elif draft.get("messageId"):
            message = state["gmail_messages"].get(draft["messageId"])
        if isinstance(message, dict):
            state["gmail_drafts"][draft_id] = _link_draft(
                {"id": draft_id}, state["gmail_messages"][message["id"]])

    for message_id, message in list(state["gmail_messages"].items()):
        message.setdefault("id", message_id)
        _normalize_message(message)

    state["gmail_threads"] = {tid: thread for tid, thread in state["gmail_threads"].items()
                              if any(m["threadId"] == tid
                                     for m in state["gmail_messages"].values())}
    for message in state["gmail_messages"].values():
        _refresh_thread(message["threadId"])

    now = _rfc3339()
    for file_id, file in state["drive_files"].items():
        file.setdefault("id", file_id)
        file.setdefault("name", "Untitled")
        file.setdefault("mimeType", "application/octet-stream")
        file.setdefault("parents", [ROOT_ID])
        file.setdefault("createdTime", now)
        file.setdefault("modifiedTime", file["createdTime"])
        file.setdefault("trashed", False)
        file.setdefault("version", 1)
        state["drive_permissions"].setdefault(file_id, {})

    for event_id, event in state["calendar_events"].items():
        event.setdefault("id", event_id)
        event.setdefault("status", "confirmed")
        event.setdefault("created", now)
        event.setdefault("updated", now)
        event.setdefault("_calendarId", profile["emailAddress"])


def _views(state: dict) -> dict[str, kit.View]:
    """Every collection as flat records, with the fields assertions ask about."""
    labels = state["gmail_labels"]

    def label_names(ids: Iterable[str]) -> list[str]:
        return [labels.get(lid, {}).get("name", lid) for lid in ids]

    messages = []
    for message in state["gmail_messages"].values():
        payload = message.get("payload") or {}
        label_ids = list(message.get("labelIds") or [])
        messages.append({
            "id": message.get("id"),
            "thread_id": message.get("threadId"),
            "from": _header(payload, "From"),
            "to": _header(payload, "To"),
            "cc": _header(payload, "Cc"),
            "bcc": _header(payload, "Bcc"),
            "subject": _header(payload, "Subject"),
            "date": _header(payload, "Date"),
            "snippet": message.get("snippet", ""),
            "body": _body_text(payload),
            "labelIds": label_ids,
            "labels": label_names(label_ids),
            "attachments": [p.get("filename") for p in _walk_parts(payload) if p.get("filename")],
            "unread": "UNREAD" in label_ids,
            "starred": "STARRED" in label_ids,
            "sent": "SENT" in label_ids,
            "draft": "DRAFT" in label_ids,
            "trashed": "TRASH" in label_ids,
        })

    threads = []
    for thread in state["gmail_threads"].values():
        thread_messages = [m for m in state["gmail_messages"].values()
                           if m.get("threadId") == thread["id"]]
        first = thread_messages[0] if thread_messages else {}
        threads.append({
            "id": thread["id"],
            "subject": _header(first.get("payload") or {}, "Subject"),
            "snippet": thread.get("snippet", ""),
            "message_count": len(thread_messages),
            "participants": sorted({_header(m.get("payload") or {}, "From")
                                    for m in thread_messages if m.get("payload")}),
            "labelIds": list(thread.get("labelIds") or []),
            "trashed": "TRASH" in (thread.get("labelIds") or []),
        })

    drafts = []
    for draft in state["gmail_drafts"].values():
        message = state["gmail_messages"].get(draft.get("messageId")) or {}
        payload = message.get("payload") or {}
        drafts.append({
            "id": draft["id"],
            "message_id": draft.get("messageId"),
            "thread_id": message.get("threadId"),
            "to": _header(payload, "To"),
            "cc": _header(payload, "Cc"),
            "subject": _header(payload, "Subject"),
            "body": _body_text(payload),
        })

    files = []
    for file in state["drive_files"].values():
        permissions = (state["drive_permissions"].get(file["id"]) or {}).values()
        parents = file.get("parents") or []
        parent = state["drive_files"].get(parents[0]) if parents else None
        files.append({
            "id": file["id"],
            "name": file.get("name", ""),
            "mimeType": file.get("mimeType", ""),
            "is_folder": file.get("mimeType") == FOLDER_MIME,
            "folder": (parent or {}).get("name", "My Drive" if parents == [ROOT_ID] else ""),
            "parents": list(parents),
            "owner": _me(),
            "shared_with": sorted({p.get("emailAddress") or p.get("domain", "")
                                   for p in permissions if p.get("role") != "owner"}),
            "starred": bool(file.get("starred")),
            "trashed": bool(file.get("trashed")),
            "content": _content_bytes(file["id"]).decode("utf-8", "replace"),
            "modifiedTime": file.get("modifiedTime", ""),
        })

    permissions = [
        {"id": permission["id"], "file_id": file_id,
         "file_name": (state["drive_files"].get(file_id) or {}).get("name", ""),
         "type": permission.get("type", "user"), "role": permission.get("role", "reader"),
         "email": permission.get("emailAddress", ""), "domain": permission.get("domain", "")}
        for file_id, perms in state["drive_permissions"].items()
        for permission in perms.values()
    ]

    events = []
    for event in state["calendar_events"].values():
        start, end = event.get("start") or {}, event.get("end") or {}
        events.append({
            "id": event["id"],
            "calendar_id": event.get("_calendarId", ""),
            "summary": event.get("summary", ""),
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "all_day": bool(start.get("date")),
            "attendees": [a.get("email", "") for a in event.get("attendees") or []],
            "status": event.get("status", "confirmed"),
            "cancelled": event.get("status") == "cancelled",
        })

    return {
        "gmail_messages": kit.View(messages, tombstone="trashed",
                                   nouns=("email", "emails", "gmail message", "gmail messages")),
        "gmail_threads": kit.View(threads, tombstone="trashed",
                                  nouns=("thread", "threads", "email thread", "email threads")),
        "gmail_labels": kit.View([{**_render_label(lab), **_label_counts(lab["id"])}
                                  for lab in labels.values()],
                                 nouns=("gmail label", "gmail labels")),
        "gmail_drafts": kit.View(drafts, nouns=("draft", "drafts", "email draft", "email drafts")),
        "drive_files": kit.View(files, tombstone="trashed",
                                nouns=("drive file", "drive files", "document", "documents",
                                       "folder", "folders")),
        "drive_permissions": kit.View(permissions,
                                      nouns=("share", "shares", "permission", "permissions")),
        "calendar_events": kit.View(events, tombstone="cancelled",
                                    nouns=("event", "events", "calendar event",
                                           "calendar events", "meeting", "meetings")),
        "calendars": kit.View(list(state["calendars"].values()),
                              nouns=("calendar", "calendars")),
    }


_GMAIL_MESSAGE_OPS: dict[str, tuple[kit.Op, str]] = {
    "send": ("create", "gmail_messages"),
    "import": ("create", "gmail_messages"),
    "modify": ("update", "gmail_messages"),
    "untrash": ("update", "gmail_messages"),
    "batchModify": ("update", "gmail_messages"),
    "trash": ("delete", "gmail_messages"),
    "batchDelete": ("delete", "gmail_messages"),
}
_GMAIL_COLLECTIONS = {"messages": "gmail_messages", "threads": "gmail_threads",
                      "labels": "gmail_labels", "drafts": "gmail_drafts",
                      "history": "gmail_history", "profile": "user_profile",
                      "settings": "gmail_settings"}
_METHOD_OPS: dict[str, kit.Op] = {"GET": "read", "POST": "create", "PUT": "update",
                                  "PATCH": "update", "DELETE": "delete"}


def _classify(method: str, path: str, body: Any) -> tuple[kit.Op, str] | None:
    """Map an endpoint to the (op, resource) a trajectory check reads."""
    method = method.upper()
    path = path.removeprefix("/upload")
    default: kit.Op = _METHOD_OPS.get(method, "other")
    if path.startswith("/batch"):
        return "other", "batch"
    if path in _PUBLIC_PATHS:
        return "other", "oauth_token"
    if path.startswith("/gmail/v1/users/"):
        parts = path.split("/")[5:]  # drop "", gmail, v1, users, <userId>
        if not parts:
            return "read", "user_profile"
        resource = _GMAIL_COLLECTIONS.get(parts[0], parts[0])
        last = parts[-1]
        if resource == "gmail_messages":
            if "attachments" in parts:
                return "read", "gmail_attachments"
            if last in _GMAIL_MESSAGE_OPS:
                return _GMAIL_MESSAGE_OPS[last]
        if resource == "gmail_threads" and last in _GMAIL_MESSAGE_OPS:
            op, _ = _GMAIL_MESSAGE_OPS[last]
            return op, "gmail_threads"
        if resource == "gmail_drafts" and last == "send":
            return "create", "gmail_messages"  # sending a draft is what produced the email
        return default, resource
    if path.startswith("/drive/v3/"):
        parts = path.split("/")[3:]
        if parts[:1] == ["about"]:
            return "read", "drive_about"
        if parts[:1] == ["drives"]:
            return "read", "drives"
        if "permissions" in parts:
            return default, "drive_permissions"
        if parts[-1] in ("copy", "generateIds"):
            return ("create" if parts[-1] == "copy" else "read"), "drive_files"
        if parts[-1] == "export":
            return "read", "drive_files"
        if method == "PATCH" and isinstance(body, dict) and body.get("trashed") is True:
            return "delete", "drive_files"  # trashing is how Drive deletes
        return default, "drive_files"
    if path.startswith("/calendar/v3/"):
        parts = path.split("/")[3:]
        if parts[:1] == ["freeBusy"]:
            return "read", "calendar_freebusy"
        if "calendarList" in parts or parts[:1] == ["calendars"] and "events" not in parts:
            return default, "calendars"
        if parts[-1] == "quickAdd":
            return "create", "calendar_events"
        return default, "calendar_events"
    return None


def _failed(status: int, body: Any) -> bool:
    """A batch response is 200 even when the calls inside it failed."""
    if status >= 400:
        return True
    return isinstance(body, str) and body.lstrip().startswith("--") and '"error"' in body


TWIN = kit.install(app, kit.Twin(
    name="google-workspace",
    state=STATE,
    trace=TRACE,
    fresh_state=_fresh_state,
    seeds_dir=SEEDS_DIR,
    error=_error,
    authenticate=_authenticate,
    after_seed=_after_seed,
    views=_views,
    classify=_classify,
    failed=_failed,
))


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.google_workspace_mcp import (
    mount_on as _mount_mcp,
)

_mount_mcp(app)
