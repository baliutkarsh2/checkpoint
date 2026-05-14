"""Supabase twin: stateful in-memory clone of the Supabase REST API.

Implements the primary Supabase surfaces:

  PostgREST   — table select/insert/update/delete/upsert via REST
  Auth        — user management (list, create, delete, update)
  Storage     — bucket + object management
  RPC         — stored function stubs
  Realtime    — presence state (simplified)

The PostgREST surface mirrors the real Supabase PostgREST interface:
  GET  /rest/v1/<table>?select=*&...  — query rows
  POST /rest/v1/<table>               — insert row(s)
  PATCH /rest/v1/<table>?<filter>     — update rows matching filter
  DELETE /rest/v1/<table>?<filter>    — delete rows matching filter

Authentication uses the Supabase anon key pattern.
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

app = FastAPI(title="checkpoint supabase twin")

# Supabase uses Bearer token auth with the anon/service_role key.
DEFAULT_BOOTSTRAP_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.checkpoint_anon_key_aabbccddeeff"
INTROSPECTION_PREFIX = "/_"

SEEDS_DIR = Path(__file__).parent / "supabase_seeds"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_unix() -> int:
    return int(time.time())


def _uid() -> str:
    return str(uuid.uuid4())


def _fresh_state() -> dict:
    return {
        "tables": {},       # table_name -> {columns: [...], rows: [...]}
        "auth_users": {},   # user_id -> auth user dict
        "storage": {
            "buckets": {},  # bucket_id -> bucket dict
            "objects": {},  # "bucket/path" -> object dict
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

def supabase_error(status: int, message: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"message": message, "code": str(status)}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _bootstrap_token() -> str:
    return os.environ.get("SUPABASE_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    for prefix in ("Bearer ", "bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return auth_header.strip()


def _ensure_table(name: str) -> dict:
    if name not in STATE["tables"]:
        STATE["tables"][name] = {"columns": [], "rows": []}
    return STATE["tables"][name]


def _apply_postgrest_filters(rows: list[dict], params: dict) -> list[dict]:
    """Apply PostgREST-style query filters from request query params.

    Supported operators (subset):
      col=eq.val     exact match
      col=neq.val    not equal
      col=lt.val     less than (numeric)
      col=lte.val    less than or equal
      col=gt.val     greater than
      col=gte.val    greater than or equal
      col=like.val   substring match (case-sensitive)
      col=ilike.val  substring match (case-insensitive)
      col=is.null    is null / is not null (is.not.null)
      col=in.(a,b)   in list
    """
    result = list(rows)
    skip = {"select", "order", "limit", "offset", "on_conflict", "columns", "count"}
    for key, val in params.items():
        if key in skip or not val:
            continue
        # Each param is col=op.value.
        if "." not in val:
            # Treat bare value as eq.
            op, raw = "eq", val
        else:
            op, _, raw = val.partition(".")
        op = op.lower()

        def _coerce(v: str, ref: Any) -> Any:
            if isinstance(ref, (int, float)):
                try:
                    return type(ref)(v)
                except Exception:
                    return v
            return v

        filtered = []
        for row in result:
            cell = row.get(key)
            if op == "eq":
                if str(cell) == raw:
                    filtered.append(row)
            elif op == "neq":
                if str(cell) != raw:
                    filtered.append(row)
            elif op == "lt":
                if cell is not None and cell < _coerce(raw, cell):
                    filtered.append(row)
            elif op == "lte":
                if cell is not None and cell <= _coerce(raw, cell):
                    filtered.append(row)
            elif op == "gt":
                if cell is not None and cell > _coerce(raw, cell):
                    filtered.append(row)
            elif op == "gte":
                if cell is not None and cell >= _coerce(raw, cell):
                    filtered.append(row)
            elif op == "like":
                if isinstance(cell, str) and raw.replace("%", "") in cell:
                    filtered.append(row)
            elif op == "ilike":
                if isinstance(cell, str) and raw.replace("%", "").lower() in cell.lower():
                    filtered.append(row)
            elif op == "is":
                if raw in ("null", "NULL"):
                    if cell is None:
                        filtered.append(row)
                elif raw in ("not.null", "NOT.NULL"):
                    if cell is not None:
                        filtered.append(row)
                else:
                    filtered.append(row)
            elif op == "in":
                choices = raw.strip("()").split(",")
                if str(cell) in choices:
                    filtered.append(row)
            else:
                filtered.append(row)
        result = filtered
    return result


def _apply_select(rows: list[dict], select: str | None) -> list[dict]:
    if not select or select.strip() == "*":
        return rows
    cols = [c.strip() for c in select.split(",") if c.strip()]
    return [{c: r.get(c) for c in cols} for r in rows]


def _apply_order(rows: list[dict], order: str | None) -> list[dict]:
    if not order:
        return rows
    parts = [p.strip() for p in order.split(",")]
    result = list(rows)
    for part in reversed(parts):
        col, *rest = part.split(".")
        desc = "desc" in rest
        try:
            result.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        except TypeError:
            pass
    return result


# --- middlewares -------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(INTROSPECTION_PREFIX) or path.startswith("/mcp"):
        return await call_next(request)
    # apikey header (Supabase pattern) or Authorization: Bearer.
    apikey = request.headers.get("apikey")
    token = _extract_token(request.headers.get("authorization"))
    valid = (apikey == _bootstrap_token()) or (token == _bootstrap_token())
    if not valid:
        return supabase_error(401, "Invalid API key")
    STATE["_counters"]["requests"] += 1
    rl = STATE["_config"].get("rate_limit")
    if rl is not None and STATE["_counters"]["requests"] > rl:
        return JSONResponse(status_code=429, content={"message": "Too Many Requests"})
    return await call_next(request)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(INTROSPECTION_PREFIX) or path.startswith("/mcp"):
        return await call_next(request)

    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else None
    except Exception:
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
        "ts": _now(),
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


# --- introspection -----------------------------------------------------------

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
    cfg = data.get("config") or {}
    for ck, cv in cfg.items():
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
    cfg = data.get("config") or {}
    for ck, cv in cfg.items():
        STATE["_config"][ck] = cv
    return {"ok": True}


# --- PostgREST REST surface --------------------------------------------------
# Supabase's PostgREST API: /rest/v1/<table>

@app.get("/rest/v1/")
def postgrest_list_tables():
    """Return available tables (OpenAPI-style definition list)."""
    return {
        "definitions": {
            name: {
                "properties": {col: {"type": "unknown"} for col in tbl.get("columns", [])},
                "row_count": len(tbl.get("rows", [])),
            }
            for name, tbl in STATE["tables"].items()
        }
    }


@app.get("/rest/v1/{table_name}")
async def postgrest_select(table_name: str, request: Request):
    """SELECT rows. Supports select=, order=, limit=, offset=, and col=op.val filters."""
    _ensure_table(table_name)
    params = dict(request.query_params)

    rows = list(STATE["tables"][table_name]["rows"])
    rows = _apply_postgrest_filters(rows, params)
    rows = _apply_order(rows, params.get("order"))

    try:
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 1000))
    except (TypeError, ValueError):
        offset, limit = 0, 1000

    rows = rows[offset:offset + limit]
    rows = _apply_select(rows, params.get("select"))

    # Supabase returns JSON array by default; application/json is set by FastAPI.
    prefer = request.headers.get("prefer", "")
    if "count=exact" in prefer:
        total = len(STATE["tables"][table_name]["rows"])
        return Response(
            content=json.dumps(rows),
            status_code=200,
            headers={"Content-Range": f"0-{len(rows) - 1}/{total}", "Content-Type": "application/json"},
        )
    return rows


@app.post("/rest/v1/{table_name}", status_code=201)
async def postgrest_insert(table_name: str, request: Request):
    """INSERT row(s). Body can be a single object or array.

    Supports Prefer: return=representation to get back the inserted rows.
    Supports on_conflict query param for upsert behaviour.
    """
    tbl = _ensure_table(table_name)
    body_bytes = await request.body()
    try:
        data = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        return supabase_error(400, "Request body must be valid JSON")

    rows_in = data if isinstance(data, list) else [data]
    inserted = []
    on_conflict = request.query_params.get("on_conflict")

    for row in rows_in:
        if not isinstance(row, dict):
            continue
        # Auto-generate id if not provided and not present.
        if "id" not in row:
            row = {"id": _uid(), **row}
        # Upsert: if on_conflict key exists and matches, replace.
        if on_conflict and on_conflict in row:
            conflict_val = row[on_conflict]
            existing_idx = next(
                (i for i, r in enumerate(tbl["rows"]) if r.get(on_conflict) == conflict_val),
                None,
            )
            if existing_idx is not None:
                tbl["rows"][existing_idx] = row
                inserted.append(row)
                continue
        tbl["rows"].append(row)
        inserted.append(row)

    prefer = request.headers.get("prefer", "")
    if "return=representation" in prefer:
        select = request.query_params.get("select")
        result = _apply_select(inserted, select)
        status = 200 if on_conflict else 201
        return JSONResponse(content=result, status_code=status)
    return Response(status_code=201)


@app.patch("/rest/v1/{table_name}")
async def postgrest_update(table_name: str, request: Request):
    """UPDATE rows matching query filters."""
    tbl = _ensure_table(table_name)
    params = dict(request.query_params)
    body_bytes = await request.body()
    try:
        patch = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        return supabase_error(400, "Request body must be valid JSON")
    if not isinstance(patch, dict):
        return supabase_error(400, "Body must be a JSON object")

    updated = []
    for i, row in enumerate(tbl["rows"]):
        matches = _apply_postgrest_filters([row], params)
        if matches:
            tbl["rows"][i] = {**row, **patch}
            updated.append(tbl["rows"][i])

    prefer = request.headers.get("prefer", "")
    if "return=representation" in prefer:
        return updated
    return Response(status_code=204)


@app.delete("/rest/v1/{table_name}")
async def postgrest_delete(table_name: str, request: Request):
    """DELETE rows matching query filters."""
    tbl = _ensure_table(table_name)
    params = dict(request.query_params)

    before = list(tbl["rows"])
    kept, deleted = [], []
    for row in before:
        if _apply_postgrest_filters([row], params):
            deleted.append(row)
        else:
            kept.append(row)
    tbl["rows"] = kept

    prefer = request.headers.get("prefer", "")
    if "return=representation" in prefer:
        return deleted
    return Response(status_code=204)


# --- RPC ---------------------------------------------------------------------

@app.post("/rest/v1/rpc/{fn_name}")
async def postgrest_rpc(fn_name: str, request: Request):
    """Stub for calling stored PostgreSQL functions via PostgREST."""
    try:
        params = await request.json()
    except Exception:
        params = {}
    stubs = STATE.get("rpc_stubs") or {}
    stub = stubs.get(fn_name)
    if stub and "returns" in stub:
        return stub["returns"]
    return {"_rpc": fn_name, "params": params, "_stub": True, "result": None}


# --- Auth API ----------------------------------------------------------------

@app.get("/auth/v1/admin/users")
async def auth_list_users(
    page: int = 1,
    per_page: int = 50,
):
    """List auth users."""
    users = list(STATE["auth_users"].values())
    start = (page - 1) * per_page
    page_users = users[start:start + per_page]
    return {
        "users": page_users,
        "aud": "authenticated",
        "total": len(users),
    }


@app.post("/auth/v1/admin/users", status_code=201)
async def auth_create_user(request: Request):
    """Create an auth user."""
    body = await request.json()
    email = body.get("email")
    if not email:
        return supabase_error(400, "email is required")
    uid = _uid()
    user: dict[str, Any] = {
        "id": uid,
        "aud": "authenticated",
        "role": "authenticated",
        "email": email,
        "phone": body.get("phone"),
        "created_at": _now(),
        "updated_at": _now(),
        "email_confirmed_at": _now() if body.get("email_confirm") else None,
        "confirmed_at": _now(),
        "user_metadata": body.get("user_metadata") or {},
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "identities": [],
        "last_sign_in_at": None,
    }
    STATE["auth_users"][uid] = user
    return user


@app.get("/auth/v1/admin/users/{user_id}")
def auth_get_user(user_id: str):
    u = STATE["auth_users"].get(user_id)
    if not u:
        return supabase_error(404, f"User {user_id!r} not found")
    return u


@app.put("/auth/v1/admin/users/{user_id}")
@app.patch("/auth/v1/admin/users/{user_id}")
async def auth_update_user(user_id: str, request: Request):
    u = STATE["auth_users"].get(user_id)
    if not u:
        return supabase_error(404, f"User {user_id!r} not found")
    body = await request.json()
    for field in ("email", "phone", "role", "user_metadata", "app_metadata"):
        if field in body:
            u[field] = body[field]
    if "ban_duration" in body:
        u["ban_duration"] = body["ban_duration"]
    u["updated_at"] = _now()
    return u


@app.delete("/auth/v1/admin/users/{user_id}")
def auth_delete_user(user_id: str):
    if user_id not in STATE["auth_users"]:
        return supabase_error(404, f"User {user_id!r} not found")
    del STATE["auth_users"][user_id]
    return Response(status_code=200, content=json.dumps({}))


# --- Storage API -------------------------------------------------------------

@app.get("/storage/v1/bucket")
def storage_list_buckets():
    buckets = list(STATE["storage"]["buckets"].values())
    return buckets


@app.post("/storage/v1/bucket", status_code=200)
async def storage_create_bucket(request: Request):
    body = await request.json()
    bucket_id = body.get("id") or body.get("name")
    if not bucket_id:
        return supabase_error(400, "id is required")
    if bucket_id in STATE["storage"]["buckets"]:
        return supabase_error(409, f"Bucket {bucket_id!r} already exists")
    bucket: dict[str, Any] = {
        "id": bucket_id,
        "name": body.get("name", bucket_id),
        "owner": "authenticated",
        "public": bool(body.get("public", False)),
        "created_at": _now(),
        "updated_at": _now(),
        "file_size_limit": body.get("fileSizeLimit"),
        "allowed_mime_types": body.get("allowedMimeTypes"),
        "object_count": 0,
    }
    STATE["storage"]["buckets"][bucket_id] = bucket
    return {"name": bucket_id}


@app.get("/storage/v1/bucket/{bucket_id}")
def storage_get_bucket(bucket_id: str):
    b = STATE["storage"]["buckets"].get(bucket_id)
    if not b:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    return b


@app.put("/storage/v1/bucket/{bucket_id}")
async def storage_update_bucket(bucket_id: str, request: Request):
    b = STATE["storage"]["buckets"].get(bucket_id)
    if not b:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    body = await request.json()
    if "public" in body:
        b["public"] = bool(body["public"])
    if "file_size_limit" in body or "fileSizeLimit" in body:
        b["file_size_limit"] = body.get("file_size_limit") or body.get("fileSizeLimit")
    if "allowed_mime_types" in body or "allowedMimeTypes" in body:
        b["allowed_mime_types"] = body.get("allowed_mime_types") or body.get("allowedMimeTypes")
    b["updated_at"] = _now()
    return {"message": f"Successfully updated {bucket_id}"}


@app.post("/storage/v1/bucket/{bucket_id}/empty")
def storage_empty_bucket(bucket_id: str):
    if bucket_id not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    to_remove = [k for k in STATE["storage"]["objects"] if k.startswith(f"{bucket_id}/")]
    for k in to_remove:
        del STATE["storage"]["objects"][k]
    STATE["storage"]["buckets"][bucket_id]["object_count"] = 0
    return {"message": f"Successfully emptied {bucket_id}"}


@app.delete("/storage/v1/bucket/{bucket_id}")
def storage_delete_bucket(bucket_id: str):
    if bucket_id not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    # Remove all objects in the bucket first.
    to_remove = [k for k in STATE["storage"]["objects"] if k.startswith(f"{bucket_id}/")]
    for k in to_remove:
        del STATE["storage"]["objects"][k]
    del STATE["storage"]["buckets"][bucket_id]
    return {"message": f"Successfully deleted {bucket_id}"}


def _list_objects(bucket_id: str, prefix: str) -> list:
    return [
        {
            "name": k[len(bucket_id) + 1:],
            **{fk: fv for fk, fv in v.items() if not fk.startswith("_")},
        }
        for k, v in STATE["storage"]["objects"].items()
        if k.startswith(f"{bucket_id}/{prefix}")
    ]


def _update_bucket_count(bucket_id: str) -> None:
    b = STATE["storage"]["buckets"].get(bucket_id)
    if b:
        b["object_count"] = sum(
            1 for k in STATE["storage"]["objects"] if k.startswith(f"{bucket_id}/")
        )


# --- Storage: Objects (specific paths before wildcards) ----------------------

# These routes MUST be registered before /object/{bucket_id}/{path:path} so
# Starlette doesn't swallow "list", "info", "sign", "move", "copy" as bucket ids.

@app.get("/storage/v1/object/list/{bucket_id}")
async def storage_list_objects_get(bucket_id: str, request: Request):
    if bucket_id not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    prefix = request.query_params.get("prefix", "")
    return _list_objects(bucket_id, prefix)


@app.post("/storage/v1/object/list/{bucket_id}")
async def storage_list_objects_post(bucket_id: str, request: Request):
    if bucket_id not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    prefix = body.get("prefix", "")
    return _list_objects(bucket_id, prefix)


@app.get("/storage/v1/object/info/{bucket_id}/{path:path}")
def storage_get_object_info(bucket_id: str, path: str):
    """Return object metadata without downloading its content."""
    key = f"{bucket_id}/{path}"
    obj = STATE["storage"]["objects"].get(key)
    if not obj:
        return supabase_error(404, "Object not found")
    return {k: v for k, v in obj.items() if not k.startswith("_")}


@app.post("/storage/v1/object/move")
async def storage_move_object(request: Request):
    body = await request.json()
    src_bucket = body.get("bucketId", "")
    src_key = f"{src_bucket}/{body.get('sourceKey', '')}"
    dst_bucket = body.get("destinationBucket") or src_bucket
    dst_key = f"{dst_bucket}/{body.get('destinationKey', '')}"
    if src_key not in STATE["storage"]["objects"]:
        return supabase_error(404, f"Source object {src_key!r} not found")
    if dst_bucket not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Destination bucket {dst_bucket!r} not found")
    obj = STATE["storage"]["objects"].pop(src_key)
    obj["name"] = body.get("destinationKey", obj["name"])
    obj["bucket_id"] = dst_bucket
    obj["updated_at"] = _now()
    STATE["storage"]["objects"][dst_key] = obj
    _update_bucket_count(src_bucket)
    if dst_bucket != src_bucket:
        _update_bucket_count(dst_bucket)
    return {"message": "Successfully moved"}


@app.post("/storage/v1/object/copy")
async def storage_copy_object(request: Request):
    body = await request.json()
    src_bucket = body.get("bucketId", "")
    src_key = f"{src_bucket}/{body.get('sourceKey', '')}"
    dst_bucket = body.get("destinationBucket") or src_bucket
    dst_key = f"{dst_bucket}/{body.get('destinationKey', '')}"
    if src_key not in STATE["storage"]["objects"]:
        return supabase_error(404, f"Source object {src_key!r} not found")
    if dst_bucket not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Destination bucket {dst_bucket!r} not found")
    import copy as _copy
    obj = _copy.deepcopy(STATE["storage"]["objects"][src_key])
    obj["id"] = _uid()
    obj["name"] = body.get("destinationKey", obj["name"])
    obj["bucket_id"] = dst_bucket
    obj["created_at"] = _now()
    obj["updated_at"] = _now()
    STATE["storage"]["objects"][dst_key] = obj
    _update_bucket_count(dst_bucket)
    return {"Id": obj["id"]}


@app.post("/storage/v1/object/sign/{bucket_id}/{path:path}")
async def storage_create_signed_url(bucket_id: str, path: str, request: Request):
    key = f"{bucket_id}/{path}"
    if key not in STATE["storage"]["objects"]:
        return supabase_error(404, "Object not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    expires_in = body.get("expiresIn", 3600)
    token = _uid().replace("-", "")
    signed_url = f"/storage/v1/object/authenticated/{bucket_id}/{path}?token={token}&expires_in={expires_in}"
    return {"signedURL": signed_url, "token": token, "path": path}


# --- Storage: Objects (wildcard routes — must come after specific routes) ----

@app.post("/storage/v1/object/{bucket_id}/{path:path}")
async def storage_upload_object(bucket_id: str, path: str, request: Request):
    if bucket_id not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    content_type_hdr = request.headers.get("content-type", "")
    if "application/json" in content_type_hdr:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if "_mcp_content" in payload:
            raw_content = payload["_mcp_content"]
            mime = payload.get("_mcp_content_type", "text/plain")
            body_bytes = raw_content.encode("utf-8") if isinstance(raw_content, str) else raw_content
        else:
            body_bytes = await request.body()
            mime = "application/json"
    else:
        body_bytes = await request.body()
        mime = content_type_hdr or "application/octet-stream"
    upsert = request.headers.get("x-upsert", "false").lower() == "true"
    key = f"{bucket_id}/{path}"
    if not upsert and key in STATE["storage"]["objects"]:
        return supabase_error(409, f"Object {key!r} already exists")
    obj: dict[str, Any] = {
        "id": _uid(),
        "bucket_id": bucket_id,
        "name": path,
        "owner": "authenticated",
        "created_at": _now(),
        "updated_at": _now(),
        "last_accessed_at": _now(),
        "metadata": {"size": len(body_bytes), "mimetype": mime},
        "_content": body_bytes.decode("utf-8", errors="replace") if isinstance(body_bytes, bytes) else body_bytes,
    }
    STATE["storage"]["objects"][key] = obj
    _update_bucket_count(bucket_id)
    return {"Key": key}


@app.get("/storage/v1/object/{bucket_id}/{path:path}")
def storage_download_object(bucket_id: str, path: str):
    key = f"{bucket_id}/{path}"
    obj = STATE["storage"]["objects"].get(key)
    if not obj:
        return supabase_error(404, "Object not found")
    content = obj.get("_content", "")
    mime = (obj.get("metadata") or {}).get("mimetype", "application/octet-stream")
    return Response(content=content, media_type=mime)


@app.delete("/storage/v1/object/{bucket_id}")
async def storage_bulk_delete_objects(bucket_id: str, request: Request):
    """Bulk-delete objects by path list (Supabase storage bulk delete API)."""
    if bucket_id not in STATE["storage"]["buckets"]:
        return supabase_error(404, f"Bucket {bucket_id!r} not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    prefixes: list[str] = body.get("prefixes", [])
    removed = []
    for rel_path in prefixes:
        key = f"{bucket_id}/{rel_path}"
        if key in STATE["storage"]["objects"]:
            del STATE["storage"]["objects"][key]
            removed.append({"name": rel_path})
    _update_bucket_count(bucket_id)
    return removed


@app.delete("/storage/v1/object/{bucket_id}/{path:path}")
def storage_delete_object(bucket_id: str, path: str):
    key = f"{bucket_id}/{path}"
    if key not in STATE["storage"]["objects"]:
        return supabase_error(404, "Object not found")
    del STATE["storage"]["objects"][key]
    _update_bucket_count(bucket_id)
    return [{"name": path}]


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.supabase_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
