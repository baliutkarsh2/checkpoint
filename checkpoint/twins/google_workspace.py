"""Google Workspace twin: stateful in-memory clone of Gmail + Drive APIs.

Implements the primary Google Workspace surfaces used by agents:

  Gmail   — threads, messages, labels, drafts, send, search, modify
  Drive   — files, folders, permissions, copy, search

Authentication mirrors Google OAuth 2.0 Bearer token format.
Introspection at /_health, /_trace, /_state, /_reset, /_seed/<name>, /_seed-file.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="checkpoint google-workspace twin")

DEFAULT_BOOTSTRAP_TOKEN = "ya29.checkpoint_google_workspace_token_aabbccddeeff"
INTROSPECTION_PREFIX = "/_"

SEEDS_DIR = Path(__file__).parent / "google_workspace_seeds"

# Built-in Gmail labels that always exist
_SYSTEM_LABELS = {
    "INBOX": {"id": "INBOX", "name": "INBOX", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "SENT": {"id": "SENT", "name": "SENT", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "DRAFT": {"id": "DRAFT", "name": "DRAFT", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "TRASH": {"id": "TRASH", "name": "TRASH", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "STARRED": {"id": "STARRED", "name": "STARRED", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "UNREAD": {"id": "UNREAD", "name": "UNREAD", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "IMPORTANT": {"id": "IMPORTANT", "name": "IMPORTANT", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "CATEGORY_PERSONAL": {"id": "CATEGORY_PERSONAL", "name": "CATEGORY_PERSONAL", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "CATEGORY_SOCIAL": {"id": "CATEGORY_SOCIAL", "name": "CATEGORY_SOCIAL", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "CATEGORY_PROMOTIONS": {"id": "CATEGORY_PROMOTIONS", "name": "CATEGORY_PROMOTIONS", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "CATEGORY_UPDATES": {"id": "CATEGORY_UPDATES", "name": "CATEGORY_UPDATES", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
    "CATEGORY_FORUMS": {"id": "CATEGORY_FORUMS", "name": "CATEGORY_FORUMS", "type": "system", "messagesTotal": 0, "messagesUnread": 0, "threadsTotal": 0, "threadsUnread": 0},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4()).replace("-", "")[:16]


def _fresh_state() -> dict:
    return {
        # Gmail
        "gmail_threads": {},   # thread_id -> thread dict
        "gmail_messages": {},  # message_id -> message dict
        "gmail_labels": dict(_SYSTEM_LABELS),  # label_id -> label dict
        "gmail_drafts": {},    # draft_id -> draft dict
        # Drive
        "drive_files": {},     # file_id -> file dict (includes folders)
        "drive_permissions": {},  # file_id -> {permission_id -> permission dict}
        # User profile
        "user_profile": {
            "emailAddress": "user@checkpoint.test",
            "messagesTotal": 0,
            "threadsTotal": 0,
            "historyId": "1",
        },
        "_counters": {
            "requests": 0,
        },
        "_config": {
            "rate_limit": None,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers -----------------------------------------------------------------

def gws_error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "status": "NOT_FOUND" if code == 404 else "INVALID_ARGUMENT"}
    })


def _bootstrap_token() -> str:
    return os.environ.get("GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _check_auth(request: Request) -> bool:
    if request.url.path.startswith(INTROSPECTION_PREFIX):
        return True
    expected = _bootstrap_token()
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return token == expected or f"Bearer {token}" == f"Bearer {expected}"


# --- middleware / tracing -----------------------------------------------------

@app.middleware("http")
async def _middleware(request: Request, call_next):
    path = request.url.path
    is_introspection = path.startswith(INTROSPECTION_PREFIX)
    is_mcp = path.startswith("/mcp")
    if not is_introspection and not is_mcp:
        STATE["_counters"]["requests"] += 1
        if not _check_auth(request):
            return JSONResponse(status_code=401, content={
                "error": {"code": 401, "message": "Request had invalid authentication credentials.", "status": "UNAUTHENTICATED"}
            })
    response = await call_next(request)
    if not is_introspection and not is_mcp:
        TRACE.append({"method": request.method, "path": path, "status": response.status_code, "ts": _now()})
    return response


# --- introspection -----------------------------------------------------------

@app.get("/_health")
def health():
    return {"ok": True, "twin": "google-workspace"}


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


# =============================================================================
# Gmail API
# =============================================================================

# --- User profile ------------------------------------------------------------

@app.get("/gmail/v1/users/me/profile")
def gmail_get_profile():
    p = dict(STATE["user_profile"])
    p["messagesTotal"] = len(STATE["gmail_messages"])
    p["threadsTotal"] = len(STATE["gmail_threads"])
    return p


# --- Labels ------------------------------------------------------------------

@app.get("/gmail/v1/users/me/labels")
def gmail_list_labels():
    return {"labels": list(STATE["gmail_labels"].values())}


@app.get("/gmail/v1/users/me/labels/{label_id}")
def gmail_get_label(label_id: str):
    lab = STATE["gmail_labels"].get(label_id)
    if not lab:
        return gws_error(404, 404, f"Label not found: {label_id}")
    return lab


@app.post("/gmail/v1/users/me/labels")
async def gmail_create_label(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return gws_error(400, 400, "name is required")
    lid = f"Label_{_uid()}"
    label = {
        "id": lid,
        "name": name,
        "type": "user",
        "messageListVisibility": body.get("messageListVisibility", "show"),
        "labelListVisibility": body.get("labelListVisibility", "labelShow"),
        "messagesTotal": 0,
        "messagesUnread": 0,
        "threadsTotal": 0,
        "threadsUnread": 0,
    }
    if "color" in body:
        label["color"] = body["color"]
    STATE["gmail_labels"][lid] = label
    return label


@app.patch("/gmail/v1/users/me/labels/{label_id}")
async def gmail_update_label(label_id: str, request: Request):
    lab = STATE["gmail_labels"].get(label_id)
    if not lab:
        return gws_error(404, 404, f"Label not found: {label_id}")
    if lab.get("type") == "system":
        return gws_error(400, 400, "Cannot update system label")
    body = await request.json()
    for field in ("name", "messageListVisibility", "labelListVisibility", "color"):
        if field in body:
            lab[field] = body[field]
    return lab


@app.delete("/gmail/v1/users/me/labels/{label_id}")
def gmail_delete_label(label_id: str):
    lab = STATE["gmail_labels"].get(label_id)
    if not lab:
        return gws_error(404, 404, f"Label not found: {label_id}")
    if lab.get("type") == "system":
        return gws_error(400, 400, "Cannot delete system label")
    del STATE["gmail_labels"][label_id]
    return Response(status_code=204)


# --- Threads -----------------------------------------------------------------

@app.get("/gmail/v1/users/me/threads")
async def gmail_list_threads(request: Request):
    params = dict(request.query_params)
    max_results = int(params.get("maxResults", 100))
    q = params.get("q", "").lower()
    label_ids = params.get("labelIds", "").split(",") if params.get("labelIds") else []

    threads = list(STATE["gmail_threads"].values())
    if q:
        threads = [t for t in threads if q in t.get("snippet", "").lower()]
    if label_ids:
        threads = [t for t in threads if any(lid in t.get("labelIds", []) for lid in label_ids)]

    threads = threads[:max_results]
    result: dict[str, Any] = {
        "threads": [{"id": t["id"], "snippet": t.get("snippet", ""), "historyId": t.get("historyId", "1")} for t in threads],
        "resultSizeEstimate": len(threads),
    }
    if len(threads) == max_results:
        result["nextPageToken"] = str(max_results)
    return result


@app.get("/gmail/v1/users/me/threads/{thread_id}")
def gmail_get_thread(thread_id: str):
    t = STATE["gmail_threads"].get(thread_id)
    if not t:
        return gws_error(404, 404, f"Thread not found: {thread_id}")
    # Hydrate messages
    result = dict(t)
    result["messages"] = [STATE["gmail_messages"][mid] for mid in t.get("messageIds", []) if mid in STATE["gmail_messages"]]
    return result


@app.delete("/gmail/v1/users/me/threads/{thread_id}")
def gmail_delete_thread(thread_id: str):
    t = STATE["gmail_threads"].pop(thread_id, None)
    if not t:
        return gws_error(404, 404, f"Thread not found: {thread_id}")
    for mid in t.get("messageIds", []):
        STATE["gmail_messages"].pop(mid, None)
    return Response(status_code=204)


@app.post("/gmail/v1/users/me/threads/{thread_id}/modify")
async def gmail_modify_thread(thread_id: str, request: Request):
    t = STATE["gmail_threads"].get(thread_id)
    if not t:
        return gws_error(404, 404, f"Thread not found: {thread_id}")
    body = await request.json()
    add_labels = body.get("addLabelIds", [])
    remove_labels = body.get("removeLabelIds", [])
    current = set(t.get("labelIds", []))
    current |= set(add_labels)
    current -= set(remove_labels)
    t["labelIds"] = list(current)
    # Propagate to messages
    for mid in t.get("messageIds", []):
        if mid in STATE["gmail_messages"]:
            msg_labels = set(STATE["gmail_messages"][mid].get("labelIds", []))
            msg_labels |= set(add_labels)
            msg_labels -= set(remove_labels)
            STATE["gmail_messages"][mid]["labelIds"] = list(msg_labels)
    return t


@app.post("/gmail/v1/users/me/threads/{thread_id}/trash")
def gmail_trash_thread(thread_id: str):
    t = STATE["gmail_threads"].get(thread_id)
    if not t:
        return gws_error(404, 404, f"Thread not found: {thread_id}")
    labels = set(t.get("labelIds", []))
    labels.add("TRASH")
    labels.discard("INBOX")
    t["labelIds"] = list(labels)
    return t


# --- Messages ----------------------------------------------------------------

@app.get("/gmail/v1/users/me/messages")
async def gmail_list_messages(request: Request):
    params = dict(request.query_params)
    max_results = int(params.get("maxResults", 100))
    q = params.get("q", "").lower()
    label_ids = params.get("labelIds", "").split(",") if params.get("labelIds") else []

    msgs = list(STATE["gmail_messages"].values())
    if q:
        payload_matches = lambda m: q in (m.get("snippet", "") + " ".join(
            h.get("value", "") for h in (m.get("payload", {}).get("headers") or [])
        )).lower()
        msgs = [m for m in msgs if payload_matches(m)]
    if label_ids:
        msgs = [m for m in msgs if any(lid in m.get("labelIds", []) for lid in label_ids)]

    msgs = msgs[:max_results]
    result: dict[str, Any] = {
        "messages": [{"id": m["id"], "threadId": m.get("threadId", m["id"])} for m in msgs],
        "resultSizeEstimate": len(msgs),
    }
    if len(msgs) == max_results:
        result["nextPageToken"] = str(max_results)
    return result


@app.get("/gmail/v1/users/me/messages/{message_id}")
def gmail_get_message(message_id: str):
    m = STATE["gmail_messages"].get(message_id)
    if not m:
        return gws_error(404, 404, f"Message not found: {message_id}")
    return m


@app.delete("/gmail/v1/users/me/messages/{message_id}")
def gmail_delete_message(message_id: str):
    m = STATE["gmail_messages"].pop(message_id, None)
    if not m:
        return gws_error(404, 404, f"Message not found: {message_id}")
    # Remove from thread
    tid = m.get("threadId")
    if tid and tid in STATE["gmail_threads"]:
        thread = STATE["gmail_threads"][tid]
        thread["messageIds"] = [x for x in thread.get("messageIds", []) if x != message_id]
        if not thread["messageIds"]:
            del STATE["gmail_threads"][tid]
    return Response(status_code=204)


@app.post("/gmail/v1/users/me/messages/{message_id}/modify")
async def gmail_modify_message(message_id: str, request: Request):
    m = STATE["gmail_messages"].get(message_id)
    if not m:
        return gws_error(404, 404, f"Message not found: {message_id}")
    body = await request.json()
    labels = set(m.get("labelIds", []))
    labels |= set(body.get("addLabelIds", []))
    labels -= set(body.get("removeLabelIds", []))
    m["labelIds"] = list(labels)
    return m


@app.post("/gmail/v1/users/me/messages/{message_id}/trash")
def gmail_trash_message(message_id: str):
    m = STATE["gmail_messages"].get(message_id)
    if not m:
        return gws_error(404, 404, f"Message not found: {message_id}")
    labels = set(m.get("labelIds", []))
    labels.add("TRASH")
    labels.discard("INBOX")
    m["labelIds"] = list(labels)
    return m


def _send_message_body(body: dict) -> dict:
    """Create a message + thread from a send/draft payload."""
    mid = _uid()
    headers = body.get("headers", [])
    # Support both raw headers list and structured fields
    to = next((h["value"] for h in headers if h.get("name") == "To"), body.get("to", ""))
    subject = next((h["value"] for h in headers if h.get("name") == "Subject"), body.get("subject", "(no subject)"))
    from_addr = body.get("from", STATE["user_profile"]["emailAddress"])
    text_body = body.get("body", body.get("text", ""))

    msg: dict[str, Any] = {
        "id": mid,
        "threadId": body.get("threadId", mid),
        "labelIds": body.get("labelIds", ["SENT"]),
        "snippet": text_body[:100],
        "historyId": "1",
        "internalDate": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": _now()},
            ] + [h for h in headers if h.get("name") not in ("From", "To", "Subject", "Date")],
            "mimeType": "text/plain",
            "body": {"size": len(text_body), "data": text_body},
        },
        "sizeEstimate": len(text_body),
    }
    STATE["gmail_messages"][mid] = msg
    tid = msg["threadId"]
    if tid not in STATE["gmail_threads"]:
        STATE["gmail_threads"][tid] = {
            "id": tid,
            "snippet": msg["snippet"],
            "historyId": "1",
            "labelIds": msg["labelIds"],
            "messageIds": [],
        }
    STATE["gmail_threads"][tid]["messageIds"].append(mid)
    return msg


@app.post("/gmail/v1/users/me/messages/send")
async def gmail_send_message(request: Request):
    body = await request.json()
    msg = _send_message_body({**body, "labelIds": ["SENT"]})
    return msg


# --- Drafts ------------------------------------------------------------------

@app.get("/gmail/v1/users/me/drafts")
async def gmail_list_drafts(request: Request):
    params = dict(request.query_params)
    max_results = int(params.get("maxResults", 100))
    drafts = list(STATE["gmail_drafts"].values())[:max_results]
    return {
        "drafts": [{"id": d["id"], "message": {"id": d["message"]["id"], "threadId": d["message"].get("threadId", d["message"]["id"])}} for d in drafts],
        "resultSizeEstimate": len(drafts),
    }


@app.get("/gmail/v1/users/me/drafts/{draft_id}")
def gmail_get_draft(draft_id: str):
    d = STATE["gmail_drafts"].get(draft_id)
    if not d:
        return gws_error(404, 404, f"Draft not found: {draft_id}")
    return d


@app.post("/gmail/v1/users/me/drafts")
async def gmail_create_draft(request: Request):
    body = await request.json()
    msg_body = body.get("message", body)
    did = _uid()
    mid = _uid()
    msg = {
        "id": mid,
        "threadId": mid,
        "labelIds": ["DRAFT"],
        "snippet": msg_body.get("body", msg_body.get("text", ""))[:100],
        "payload": {
            "headers": msg_body.get("headers", []),
            "mimeType": "text/plain",
            "body": {"size": 0, "data": msg_body.get("body", msg_body.get("text", ""))},
        },
    }
    STATE["gmail_messages"][mid] = msg
    draft = {"id": did, "message": msg}
    STATE["gmail_drafts"][did] = draft
    return draft


@app.patch("/gmail/v1/users/me/drafts/{draft_id}")
async def gmail_update_draft(draft_id: str, request: Request):
    d = STATE["gmail_drafts"].get(draft_id)
    if not d:
        return gws_error(404, 404, f"Draft not found: {draft_id}")
    body = await request.json()
    msg_body = body.get("message", body)
    if "headers" in msg_body:
        d["message"]["payload"]["headers"] = msg_body["headers"]
    text = msg_body.get("body", msg_body.get("text", ""))
    if text:
        d["message"]["snippet"] = text[:100]
        d["message"]["payload"]["body"]["data"] = text
    return d


@app.post("/gmail/v1/users/me/drafts/{draft_id}/send")
async def gmail_send_draft(draft_id: str, request: Request):
    d = STATE["gmail_drafts"].pop(draft_id, None)
    if not d:
        return gws_error(404, 404, f"Draft not found: {draft_id}")
    msg = d["message"]
    msg["labelIds"] = [l for l in msg.get("labelIds", []) if l != "DRAFT"] + ["SENT"]
    mid = msg["id"]
    STATE["gmail_messages"][mid] = msg
    return msg


@app.delete("/gmail/v1/users/me/drafts/{draft_id}")
def gmail_delete_draft(draft_id: str):
    d = STATE["gmail_drafts"].pop(draft_id, None)
    if not d:
        return gws_error(404, 404, f"Draft not found: {draft_id}")
    STATE["gmail_messages"].pop(d["message"]["id"], None)
    return Response(status_code=204)


# =============================================================================
# Drive API
# =============================================================================

_DRIVE_FIELDS = ("id", "name", "mimeType", "parents", "size", "createdTime", "modifiedTime", "webViewLink", "starred", "trashed", "shared", "ownedByMe", "owners", "description")


def _file_response(f: dict, fields: str = "*") -> dict:
    if fields == "*" or not fields:
        return f
    requested = set(fields.replace(" ", "").split(","))
    return {k: v for k, v in f.items() if k in requested}


@app.get("/drive/v3/files")
async def drive_list_files(request: Request):
    params = dict(request.query_params)
    page_size = int(params.get("pageSize", 100))
    q = params.get("q", "").lower()
    fields = params.get("fields", "*")

    files = [f for f in STATE["drive_files"].values() if not f.get("trashed")]
    if q:
        # Simple q parsing: name contains 'X', mimeType = 'Y', trashed = false/true
        def _matches(f: dict) -> bool:
            for clause in q.split(" and "):
                clause = clause.strip().strip("()")
                if "name contains" in clause:
                    needle = clause.split("name contains")[1].strip().strip("'\"")
                    if needle.lower() not in f.get("name", "").lower():
                        return False
                elif "mimetype =" in clause or "mimetype=" in clause:
                    mime = clause.split("=")[1].strip().strip("'\"")
                    if f.get("mimeType", "").lower() != mime.lower():
                        return False
                elif "trashed =" in clause or "trashed=" in clause:
                    val = "true" in clause
                    if f.get("trashed", False) != val:
                        return False
            return True
        files = [f for f in files if _matches(f)]

    files = files[:page_size]
    result: dict[str, Any] = {
        "kind": "drive#fileList",
        "files": [_file_response(f, fields) for f in files],
    }
    if len(files) == page_size:
        result["nextPageToken"] = str(page_size)
    return result


@app.post("/drive/v3/files")
async def drive_create_file(request: Request):
    params = dict(request.query_params)
    body = await request.json()
    fid = _uid()
    name = body.get("name", "Untitled")
    mime = body.get("mimeType", "application/octet-stream")
    is_folder = mime == "application/vnd.google-apps.folder"
    f: dict[str, Any] = {
        "id": fid,
        "name": name,
        "mimeType": mime,
        "parents": body.get("parents", ["root"]),
        "createdTime": _now(),
        "modifiedTime": _now(),
        "size": str(len(body.get("description", ""))),
        "webViewLink": f"https://drive.google.com/{'folders' if is_folder else 'file'}/d/{fid}/view",
        "starred": False,
        "trashed": False,
        "shared": False,
        "ownedByMe": True,
        "description": body.get("description", ""),
        "owners": [{"emailAddress": STATE["user_profile"]["emailAddress"], "displayName": "Me"}],
    }
    STATE["drive_files"][fid] = f
    STATE["drive_permissions"][fid] = {
        "perm-owner": {
            "id": "perm-owner",
            "type": "user",
            "role": "owner",
            "emailAddress": STATE["user_profile"]["emailAddress"],
            "displayName": "Me",
            "kind": "drive#permission",
        }
    }
    fields = params.get("fields", "*")
    return _file_response(f, fields)


@app.get("/drive/v3/files/{file_id}")
async def drive_get_file(file_id: str, request: Request):
    if file_id == "root":
        return {"id": "root", "name": "My Drive", "mimeType": "application/vnd.google-apps.folder"}
    f = STATE["drive_files"].get(file_id)
    if not f:
        return gws_error(404, 404, f"File not found: {file_id}")
    fields = request.query_params.get("fields", "*")
    return _file_response(f, fields)


@app.patch("/drive/v3/files/{file_id}")
async def drive_update_file(file_id: str, request: Request):
    f = STATE["drive_files"].get(file_id)
    if not f:
        return gws_error(404, 404, f"File not found: {file_id}")
    body = await request.json()
    for field in ("name", "description", "starred", "trashed", "mimeType", "parents"):
        if field in body:
            f[field] = body[field]
    f["modifiedTime"] = _now()
    fields = request.query_params.get("fields", "*")
    return _file_response(f, fields)


@app.delete("/drive/v3/files/{file_id}")
def drive_delete_file(file_id: str):
    if file_id not in STATE["drive_files"]:
        return gws_error(404, 404, f"File not found: {file_id}")
    del STATE["drive_files"][file_id]
    STATE["drive_permissions"].pop(file_id, None)
    return Response(status_code=204)


@app.post("/drive/v3/files/{file_id}/copy")
async def drive_copy_file(file_id: str, request: Request):
    f = STATE["drive_files"].get(file_id)
    if not f:
        return gws_error(404, 404, f"File not found: {file_id}")
    body = await request.json()
    new_id = _uid()
    copy = dict(f)
    copy["id"] = new_id
    copy["name"] = body.get("name", f"Copy of {f['name']}")
    copy["parents"] = body.get("parents", f.get("parents", ["root"]))
    copy["createdTime"] = _now()
    copy["modifiedTime"] = _now()
    copy["webViewLink"] = f["webViewLink"].replace(file_id, new_id)
    STATE["drive_files"][new_id] = copy
    STATE["drive_permissions"][new_id] = {"perm-owner": dict(STATE["drive_permissions"].get(file_id, {}).get("perm-owner", {}))}
    fields = request.query_params.get("fields", "*")
    return _file_response(copy, fields)


# --- Drive Permissions -------------------------------------------------------

@app.get("/drive/v3/files/{file_id}/permissions")
def drive_list_permissions(file_id: str):
    if file_id not in STATE["drive_files"]:
        return gws_error(404, 404, f"File not found: {file_id}")
    return {
        "kind": "drive#permissionList",
        "permissions": list(STATE["drive_permissions"].get(file_id, {}).values()),
    }


@app.post("/drive/v3/files/{file_id}/permissions")
async def drive_add_permission(file_id: str, request: Request):
    if file_id not in STATE["drive_files"]:
        return gws_error(404, 404, f"File not found: {file_id}")
    body = await request.json()
    pid = _uid()
    perm: dict[str, Any] = {
        "id": pid,
        "type": body.get("type", "user"),
        "role": body.get("role", "reader"),
        "emailAddress": body.get("emailAddress"),
        "domain": body.get("domain"),
        "allowFileDiscovery": body.get("allowFileDiscovery", False),
        "displayName": body.get("displayName", body.get("emailAddress", "")),
        "kind": "drive#permission",
    }
    STATE["drive_permissions"].setdefault(file_id, {})[pid] = perm
    STATE["drive_files"][file_id]["shared"] = True
    return perm


@app.get("/drive/v3/files/{file_id}/permissions/{permission_id}")
def drive_get_permission(file_id: str, permission_id: str):
    perm = (STATE["drive_permissions"].get(file_id) or {}).get(permission_id)
    if not perm:
        return gws_error(404, 404, f"Permission not found: {permission_id}")
    return perm


@app.patch("/drive/v3/files/{file_id}/permissions/{permission_id}")
async def drive_update_permission(file_id: str, permission_id: str, request: Request):
    perms = STATE["drive_permissions"].get(file_id) or {}
    perm = perms.get(permission_id)
    if not perm:
        return gws_error(404, 404, f"Permission not found: {permission_id}")
    body = await request.json()
    if "role" in body:
        perm["role"] = body["role"]
    return perm


@app.delete("/drive/v3/files/{file_id}/permissions/{permission_id}")
def drive_delete_permission(file_id: str, permission_id: str):
    perms = STATE["drive_permissions"].get(file_id) or {}
    if permission_id not in perms:
        return gws_error(404, 404, f"Permission not found: {permission_id}")
    del perms[permission_id]
    if not any(p.get("role") != "owner" for p in perms.values()):
        STATE["drive_files"][file_id]["shared"] = False
    return Response(status_code=204)


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.google_workspace_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
