"""Supabase twin: a stateful, in-memory Supabase project.

One project is really four services behind one origin, and each shapes its
errors differently — so the SDKs (postgrest, gotrue/supabase-auth, storage3)
parse them differently too:

  PostgREST   /rest/v1/<table>        rows, filters, embeds  {code, details, hint, message}
  Auth        /auth/v1/...            users and sessions     {code, error_code, msg}
  Storage     /storage/v1/...         buckets and objects    {statusCode, error, message}
  Functions   /functions/v1/<name>    edge function stubs

Tables come from seeds: the database has exactly the tables the scenario
declares, so a query against a table that does not exist fails the way it does
in production instead of quietly answering ``[]``. Query semantics live in
:mod:`checkpoint.twins.supabase_pgrst`. The control plane and fault model come
from :mod:`checkpoint.twins.kit`.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from checkpoint.fake_credentials import FAKE_SUPABASE_TOKEN
from checkpoint.twins import kit
from checkpoint.twins import supabase_pgrst as pgrst

app = FastAPI(title="checkpoint supabase twin")

# Supabase uses Bearer token auth with the anon/service_role key.
DEFAULT_BOOTSTRAP_TOKEN = FAKE_SUPABASE_TOKEN

# The secret a local Supabase stack signs its access tokens with. Nothing here
# is sensitive: it makes the twin's JWTs decode like real ones.
JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"
ACCESS_TOKEN_TTL = 3600

SEEDS_DIR = Path(__file__).parent / "supabase_seeds"

_JSON = "application/json; charset=utf-8"
_SINGULAR = "application/vnd.pgrst.object+json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _fresh_state() -> dict:
    return {
        # table -> {"columns": [...], "rows": [...], "primary_key": ..., "foreign_keys": {...}}
        "tables": {},
        "auth_users": {},   # user id -> auth user
        "storage": {
            "buckets": {},  # bucket id -> bucket
            "objects": {},  # "bucket/path" -> object (content in "_content_b64")
        },
        "rpc_stubs": {},        # function name -> {"returns": ...}
        "edge_functions": {},   # function name -> {"returns": ...}
        "_passwords": {},       # user id -> sha256 of the password, when one is set
        "_refresh_tokens": {},  # refresh token -> user id
        "_signed": {},          # storage signing token -> {"key", "expires_at", "upload"}
        "_counters": {},
        "_config": {
            "rate_limit": None,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- request context ---------------------------------------------------------

@dataclass(frozen=True)
class _Call:
    """What the uniform fault handler needs to know about the call in flight."""

    surface: str = "rest"
    new_api_version: bool = False


# The kit's error factory shapes faults without seeing the request, but which
# envelope an SDK can parse depends on the service the call was addressed to.
_CALL: ContextVar[_Call | None] = ContextVar("supabase_call", default=None)


def _call() -> _Call:
    return _CALL.get() or _Call()


def _surface(path: str) -> str:
    if path.startswith("/auth/v1"):
        return "auth"
    if path.startswith("/storage/v1"):
        return "storage"
    if path.startswith("/functions/v1"):
        return "functions"
    return "rest"


# --- error envelopes ---------------------------------------------------------

def postgrest_error(status: int, code: str, message: str, *, details: Any = None,
                    hint: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "code": code, "details": details, "hint": hint, "message": message,
    })


def auth_error(status: int, error_code: str, message: str) -> JSONResponse:
    """GoTrue's error body, in the shape the caller's API version asks for."""
    if _call().new_api_version:
        return JSONResponse(
            status_code=status, content={"code": error_code, "message": message},
            headers={"X-Supabase-Api-Version": "2024-01-01"},
        )
    return JSONResponse(status_code=status, content={
        "code": status, "error_code": error_code, "msg": message,
    })


def storage_error(status: int, code: str, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "statusCode": str(status), "code": code, "error": error, "message": message,
    })


def _pgrst_response(exc: pgrst.PgrstError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


def _error(kind: str, status: int, message: str) -> Response:
    """Shape an injected fault as the addressed service would report it."""
    surface = _call().surface
    if surface == "auth":
        codes = {"rate_limited": "over_request_rate_limit", "forbidden": "not_admin",
                 "read_only": "not_admin", "server_error": "unexpected_failure"}
        return auth_error(status, codes.get(kind, "unexpected_failure"), message)
    if surface == "storage":
        codes = {"rate_limited": ("SlowDown", "too_many_requests"),
                 "forbidden": ("AccessDenied", "Unauthorized"),
                 "read_only": ("AccessDenied", "Unauthorized"),
                 "server_error": ("InternalError", "Internal")}
        code, error = codes.get(kind, ("InternalError", "Internal"))
        return storage_error(status, code, error, message)
    if kind in ("forbidden", "read_only"):
        # PostgREST reports privilege errors with the Postgres code 42501.
        return postgrest_error(403, "42501", message)
    return postgrest_error(status, str(status), message)


# --- authentication ----------------------------------------------------------

def _bootstrap_token() -> str:
    return os.environ.get("SUPABASE_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    for prefix in ("Bearer ", "bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return auth_header.strip()


def _is_anonymous_route(request: Request) -> bool:
    """Object routes a browser reaches without any Supabase credential."""
    path = request.url.path
    if request.method not in ("GET", "HEAD", "PUT"):
        return False
    if "/storage/v1/object/public/" in path or "/storage/v1/object/info/public/" in path:
        return True
    return bool(request.query_params.get("token")) and (
        path.startswith("/storage/v1/object/sign/")
        or path.startswith("/storage/v1/object/upload/sign/")
    )


def _authenticate(request: Request) -> Response | None:
    surface = _surface(request.url.path)
    _CALL.set(_Call(
        surface=surface,
        new_api_version=request.headers.get("x-supabase-api-version") == "2024-01-01",
    ))
    if _is_anonymous_route(request):
        return None
    token = request.headers.get("apikey") or _extract_token(request.headers.get("authorization"))
    if not token:
        if surface == "storage":
            return storage_error(401, "InvalidJWT", "Invalid JWT", "no JWT provided")
        return JSONResponse(status_code=401, content={
            "message": "No API key found in request",
            "hint": "No `apikey` request header or url param was found.",
        })
    if TWIN.config.get("strict_auth") and token != _bootstrap_token() and not _decode_jwt(token):
        if surface == "storage":
            return storage_error(401, "InvalidJWT", "Invalid JWT", "invalid signature")
        return JSONResponse(status_code=401, content={
            "message": "Invalid API key",
            "hint": "Double check your Supabase `anon` or `service_role` API key.",
        })
    return None


# --- access tokens -----------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _encode_jwt(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256)
    return f"{header}.{body}.{_b64url(signature.digest())}"


def _decode_jwt(token: str) -> dict | None:
    """The payload of a token this twin signed, or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header, body, signature = parts
    expected = hmac.new(JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256)
    if not hmac.compare_digest(signature, _b64url(expected.digest())):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None


def _issue_session(user: dict) -> dict:
    now = int(time.time())
    payload = {
        "aud": "authenticated",
        "exp": now + ACCESS_TOKEN_TTL,
        "iat": now,
        "iss": "supabase",
        "sub": user["id"],
        "email": user.get("email") or "",
        "phone": user.get("phone") or "",
        "role": user.get("role") or "authenticated",
        "session_id": _uid(),
        "user_metadata": user.get("user_metadata") or {},
        "app_metadata": user.get("app_metadata") or {},
    }
    refresh_token = secrets.token_urlsafe(16)
    STATE["_refresh_tokens"][refresh_token] = user["id"]
    user["last_sign_in_at"] = _now()
    return {
        "access_token": _encode_jwt(payload),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "expires_at": now + ACCESS_TOKEN_TTL,
        "refresh_token": refresh_token,
        "user": _auth_user_json(user),
    }


def _bearer_user(request: Request) -> dict | None:
    payload = _decode_jwt(_extract_token(request.headers.get("authorization")) or "")
    if not payload:
        return None
    return STATE["auth_users"].get(payload.get("sub"))


# --- PostgREST ---------------------------------------------------------------

def _schema() -> pgrst.Schema:
    return pgrst.Schema(STATE["tables"])


def _prefer(request: Request) -> dict[str, str]:
    """``Prefer: return=representation,count=exact`` -> {"return": ..., "count": ...}."""
    out: dict[str, str] = {}
    for part in request.headers.get("prefer", "").split(","):
        key, _, value = part.strip().partition("=")
        if key:
            out[key.strip()] = value.strip()
    return out


def _rows_response(request: Request, rows: list[dict], *, total: int, offset: int = 0,
                   status: int = 200, head: bool = False) -> Response:
    """Render rows the way PostgREST does, honouring Accept and Prefer: count."""
    prefer = _prefer(request)
    counted = total if prefer.get("count") in ("exact", "planned", "estimated") else None
    span = f"{offset}-{offset + len(rows) - 1}" if rows else "*"
    headers = {"Content-Range": f"{span}/{counted if counted is not None else '*'}"}
    if counted is not None and (offset > 0 or len(rows) < counted):
        status = 206
    accept = request.headers.get("accept", "")
    media_type = _JSON
    if "text/csv" in accept:
        return Response(content=b"" if head else _csv(rows).encode(), status_code=status,
                        headers=headers, media_type="text/csv; charset=utf-8")
    if _SINGULAR in accept:
        if len(rows) != 1:
            return _pgrst_response(pgrst.PgrstError(
                406, "PGRST116", "Cannot coerce the result to a single JSON object",
                details=f"The result contains {len(rows)} rows",
                hint="Add a filter that matches exactly one row, or use maybe_single().",
            ))
        body, media_type = json.dumps(rows[0]), f"{_SINGULAR}; charset=utf-8"
    else:
        body = json.dumps(rows)
    return Response(content=b"" if head else body.encode(), status_code=status,
                    headers=headers, media_type=media_type)


def _csv(rows: list[dict]) -> str:
    """``Accept: text/csv`` (postgrest-py's ``.csv()``) asks for a CSV document."""
    columns: list[str] = []
    for row in rows:
        columns.extend(column for column in row if column not in columns)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _cell_text(row.get(c)) for c in columns})
    return buffer.getvalue()


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else json.dumps(value)


@app.get("/rest/v1/")
def postgrest_root() -> dict:
    """The OpenAPI-ish schema listing PostgREST serves at the API root."""
    schema = _schema()
    return {
        "swagger": "2.0",
        "info": {"title": "standard public schema", "version": "12.2.0"},
        "definitions": {
            name: {
                "type": "object",
                "properties": {column: {"format": "text"}
                               for column in schema.get(name).columns},
            }
            for name in schema.names()
        },
    }


@app.api_route("/rest/v1/{table_name}", methods=["GET", "HEAD"])
async def postgrest_select(table_name: str, request: Request) -> Response:
    """SELECT rows: filters, ``select`` with embeds, ``order``, ``limit``/``offset``."""
    try:
        schema = _schema()
        table = schema.get(table_name)
        nodes = pgrst.parse_select(request.query_params.get("select"))
        pgrst.check_select(table, nodes)
        scope = pgrst.parse_scope(request.query_params.multi_items(), table=table,
                                  embeds=pgrst.embed_names(nodes))
        matched = pgrst.filter_rows(table.rows, scope)
        page = pgrst.slice_rows(pgrst.order_rows(matched, scope.order), scope)
        shaped = pgrst.shape_rows(page, nodes, table=table, schema=schema, scope=scope)
    except pgrst.PgrstError as exc:
        return _pgrst_response(exc)
    return _rows_response(request, shaped, total=len(matched), offset=scope.offset,
                          head=request.method == "HEAD")


@app.post("/rest/v1/{table_name}")
async def postgrest_insert(table_name: str, request: Request) -> Response:
    """INSERT or upsert rows (``Prefer: resolution=merge-duplicates``)."""
    try:
        schema = _schema()
        table = schema.get(table_name)
        payload = await _json_body(request)
        incoming = payload if isinstance(payload, list) else [payload]
        if not all(isinstance(row, dict) for row in incoming):
            raise pgrst.PgrstError(400, "PGRST102", "Invalid body",
                                   details="The body must be an object or an array of objects")
        prefer = _prefer(request)
        resolution = prefer.get("resolution")
        conflict = _conflict_columns(table, request.query_params.get("on_conflict"))
        written: list[dict] = []
        for row in incoming:
            _check_columns(table, row)
            candidate = _with_defaults(table, row)
            existing, columns = _conflicting_row(table, candidate, conflict)
            if existing is not None:
                # Only a collision on the conflict target can be resolved; any other
                # unique constraint is still a duplicate key error.
                if columns == conflict and resolution == "ignore-duplicates":
                    continue
                if columns != conflict or resolution != "merge-duplicates":
                    raise _duplicate_key(table, existing, columns)
                existing.update(row)
                written.append(existing)
                continue
            table.rows.append(candidate)
            written.append(candidate)
        shaped = _represent(request, table, schema, written)
    except pgrst.PgrstError as exc:
        return _pgrst_response(exc)
    if shaped is None:
        return Response(status_code=201, headers=_count_header(request, len(written)))
    return _rows_response(request, shaped, total=len(written), status=201)


@app.patch("/rest/v1/{table_name}")
async def postgrest_update(table_name: str, request: Request) -> Response:
    """UPDATE the rows matching the filters."""
    try:
        schema = _schema()
        table = schema.get(table_name)
        patch = await _json_body(request)
        if not isinstance(patch, dict):
            raise pgrst.PgrstError(400, "PGRST102", "Invalid body",
                                   details="The body must be a JSON object")
        _check_columns(table, patch)
        scope = pgrst.parse_scope(request.query_params.multi_items(), table=table)
        updated = pgrst.filter_rows(table.rows, scope)
        _check_max_affected(request, len(updated))
        for row in updated:
            row.update(patch)
        shaped = _represent(request, table, schema, updated)
    except pgrst.PgrstError as exc:
        return _pgrst_response(exc)
    if shaped is None:
        return Response(status_code=204, headers=_count_header(request, len(updated)))
    return _rows_response(request, shaped, total=len(updated))


@app.delete("/rest/v1/{table_name}")
async def postgrest_delete(table_name: str, request: Request) -> Response:
    """DELETE the rows matching the filters."""
    try:
        schema = _schema()
        table = schema.get(table_name)
        scope = pgrst.parse_scope(request.query_params.multi_items(), table=table)
        deleted = pgrst.filter_rows(table.rows, scope)
        _check_max_affected(request, len(deleted))
        doomed = {id(row) for row in deleted}
        table.spec["rows"] = [row for row in table.rows if id(row) not in doomed]
        shaped = _represent(request, table, schema, deleted)
    except pgrst.PgrstError as exc:
        return _pgrst_response(exc)
    if shaped is None:
        return Response(status_code=204, headers=_count_header(request, len(deleted)))
    return _rows_response(request, shaped, total=len(deleted))


def _count_header(request: Request, affected: int) -> dict[str, str]:
    """``Prefer: count=exact`` reports affected rows even when there is no body to return."""
    if _prefer(request).get("count") in ("exact", "planned", "estimated"):
        return {"Content-Range": f"*/{affected}"}
    return {}


async def _json_body(request: Request) -> Any:
    """The request's JSON body; a malformed one is a PostgREST 400."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise pgrst.PgrstError(400, "PGRST102", "Invalid body",
                               details="The body is not valid JSON") from None


async def _object_body(request: Request) -> dict:
    """The request's JSON body as an object, or ``{}`` when it is neither."""
    try:
        body = await _json_body(request)
    except pgrst.PgrstError:
        return {}
    return body if isinstance(body, dict) else {}


def _represent(request: Request, table: pgrst.Table, schema: pgrst.Schema,
               rows: list[dict]) -> list[dict] | None:
    """Rows to echo back, or None when the caller asked for ``return=minimal``."""
    if _prefer(request).get("return", "minimal") != "representation":
        return None
    nodes = pgrst.parse_select(request.query_params.get("select"))
    return pgrst.shape_rows(rows, nodes, table=table, schema=schema, scope=pgrst.Scope())


def _check_columns(table: pgrst.Table, row: dict) -> None:
    if not table.knows_columns():
        return
    for column in row:
        if column not in table.columns:
            raise pgrst.PgrstError(
                400, "PGRST204",
                f"Could not find the '{column}' column of '{table.name}' in the schema cache")


def _check_max_affected(request: Request, affected: int) -> None:
    prefer = _prefer(request)
    limit = prefer.get("max-affected")
    if limit is None or prefer.get("handling") != "strict":
        return
    if affected > int(limit):
        raise pgrst.PgrstError(
            400, "PGRST124", "Query result exceeds max-affected preference constraint",
            details=f"The query affects {affected} rows")


def _conflict_columns(table: pgrst.Table, on_conflict: str | None) -> tuple[str, ...]:
    if on_conflict:
        return tuple(c.strip() for c in on_conflict.split(",") if c.strip())
    return table.primary_key


def _conflicting_row(table: pgrst.Table, candidate: dict,
                     conflict: tuple[str, ...]) -> tuple[dict | None, tuple[str, ...]]:
    """The stored row ``candidate`` collides with, and the unique key it collides on."""
    for columns in (conflict, *table.unique_keys):
        if not columns or any(candidate.get(c) is None for c in columns):
            continue
        for row in table.rows:
            if all(row.get(c) == candidate.get(c) for c in columns):
                return row, columns
    return None, ()


def _duplicate_key(table: pgrst.Table, existing: dict,
                   columns: tuple[str, ...]) -> pgrst.PgrstError:
    columns = columns or table.primary_key or ("id",)
    constraint = f"{table.name}_pkey" if columns == table.primary_key else \
        f"{table.name}_{'_'.join(columns)}_key"
    values = ", ".join(str(existing.get(c)) for c in columns)
    return pgrst.PgrstError(
        409, "23505",
        f'duplicate key value violates unique constraint "{constraint}"',
        details=f"Key ({', '.join(columns)})=({values}) already exists.",
    )


def _with_defaults(table: pgrst.Table, row: dict) -> dict:
    """Fill the columns the caller left out, the way column defaults would.

    Built in declared column order so a returned row looks like a table row.
    """
    out: dict[str, Any] = {}
    for column in table.declared_columns:
        if row.get(column) is not None:
            out[column] = row[column]
        elif column in table.primary_key:
            out[column] = _next_key(table, column)
        elif column in ("created_at", "updated_at", "inserted_at"):
            out[column] = _now()
        else:
            out[column] = row.get(column)
    for column, value in row.items():
        if column not in out:
            out[column] = value
    for column in table.primary_key:
        if out.get(column) is None:
            out[column] = _next_key(table, column)
    return out


_INTEGER_TYPES = frozenset({"int", "int2", "int4", "int8", "integer", "bigint", "smallint",
                            "serial", "bigserial", "identity"})


def _next_key(table: pgrst.Table, column: str) -> Any:
    """A generated key: integer columns keep counting past the seeded rows."""
    numbers = [row[column] for row in table.rows
               if isinstance(row.get(column), int) and not isinstance(row.get(column), bool)]
    if numbers or _column_type(table, column) in _INTEGER_TYPES:
        return max(numbers, default=0) + 1
    return _uid()


def _column_type(table: pgrst.Table, column: str) -> str:
    for raw in table.spec.get("columns") or []:
        if isinstance(raw, dict) and raw.get("name") == column:
            return str(raw.get("type", "")).lower()
    return ""


# --- RPC and edge functions --------------------------------------------------

@app.api_route("/rest/v1/rpc/{fn_name}", methods=["GET", "POST"])
async def postgrest_rpc(fn_name: str, request: Request) -> Response:
    """Call a stored function. Seeds define results in ``rpc_stubs``."""
    args = await _object_body(request) if request.method == "POST" else dict(request.query_params)
    stub = (STATE.get("rpc_stubs") or {}).get(fn_name)
    if stub is not None and "returns" in stub:
        return JSONResponse(content=stub["returns"])
    # An unseeded function still answers, so a scenario that never declared one
    # does not fail the agent for calling it.
    return JSONResponse(content={"_rpc": fn_name, "params": args, "_stub": True, "result": None})


@app.api_route("/functions/v1/{fn_name:path}", methods=["GET", "POST"])
async def invoke_edge_function(fn_name: str, request: Request) -> Response:
    """Invoke an edge function. Seeds define results in ``edge_functions``."""
    body = await _object_body(request) if request.method == "POST" else dict(request.query_params)
    stub = (STATE.get("edge_functions") or {}).get(fn_name)
    if stub is not None and "returns" in stub:
        return JSONResponse(content=stub["returns"])
    return JSONResponse(content={"_function": fn_name, "body": body, "_stub": True})


# --- Auth --------------------------------------------------------------------

def _auth_user_json(user: dict) -> dict:
    """A GoTrue user record, with the fields the SDK models require always present."""
    created = user.get("created_at") or _now()
    confirmed = user.get("email_confirmed_at") or user.get("phone_confirmed_at")
    out: dict[str, Any] = {
        "id": user.get("id", ""),
        "aud": user.get("aud") or "authenticated",
        "role": user.get("role") or "authenticated",
        "email": user.get("email"),
        "phone": user.get("phone") or "",
        "created_at": created,
        "updated_at": user.get("updated_at") or created,
        "email_confirmed_at": user.get("email_confirmed_at"),
        "phone_confirmed_at": user.get("phone_confirmed_at"),
        "confirmed_at": user.get("confirmed_at") or confirmed,
        "last_sign_in_at": user.get("last_sign_in_at"),
        "app_metadata": user.get("app_metadata") or {"provider": "email", "providers": ["email"]},
        "user_metadata": user.get("user_metadata") or {},
        "identities": user.get("identities") or [],
        "is_anonymous": bool(user.get("is_anonymous", False)),
        "is_sso_user": bool(user.get("is_sso_user", False)),
    }
    for extra in ("invited_at", "banned_until", "deleted_at", "new_email", "action_link"):
        if user.get(extra) is not None:
            out[extra] = user[extra]
    return out


def _password_hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _find_user(*, email: str | None = None, phone: str | None = None) -> dict | None:
    for user in STATE["auth_users"].values():
        if email and (user.get("email") or "").lower() == email.lower():
            return user
        if phone and user.get("phone") == phone:
            return user
    return None


def _new_user(body: dict, *, confirmed: bool) -> dict:
    user_id = body.get("id") or _uid()
    now = _now()
    user: dict[str, Any] = {
        "id": user_id,
        "aud": "authenticated",
        "role": body.get("role") or "authenticated",
        "email": body.get("email"),
        "phone": body.get("phone") or "",
        "created_at": now,
        "updated_at": now,
        "email_confirmed_at": now if confirmed and body.get("email") else None,
        "phone_confirmed_at": now if confirmed and body.get("phone") else None,
        "last_sign_in_at": None,
        "app_metadata": body.get("app_metadata") or {"provider": "email", "providers": ["email"]},
        "user_metadata": body.get("user_metadata") or body.get("data") or {},
        "identities": [{
            "identity_id": _uid(),
            "id": user_id,
            "user_id": user_id,
            "identity_data": {"email": body.get("email"), "sub": user_id},
            "provider": "email",
            "created_at": now,
            "last_sign_in_at": now,
            "updated_at": now,
        }] if body.get("email") else [],
    }
    STATE["auth_users"][user_id] = user
    if body.get("password"):
        STATE["_passwords"][user_id] = _password_hash(body["password"])
    return user


def _password_ok(user: dict, password: str) -> bool:
    """Seeded users usually have no password; those accept any (there is nothing to get right)."""
    stored = STATE["_passwords"].get(user["id"])
    return stored is None or stored == _password_hash(password)


def _weak_password(password: str | None) -> JSONResponse | None:
    """GoTrue's weak-password error, which the SDK turns into AuthWeakPasswordError."""
    if password is None or len(password) >= 6:
        return None
    message = "Password should be at least 6 characters."
    reasons = {"weak_password": {"reasons": ["length"]}}
    if _call().new_api_version:
        return JSONResponse(status_code=422, headers={"X-Supabase-Api-Version": "2024-01-01"},
                            content={"code": "weak_password", "message": message, **reasons})
    return JSONResponse(status_code=422, content={
        "code": 422, "error_code": "weak_password", "msg": message, **reasons})


@app.get("/auth/v1/admin/users")
async def auth_list_users(request: Request) -> Response:
    """List users. GoTrue pages with ``page``/``per_page`` and reports the total."""
    users = [u for u in STATE["auth_users"].values() if not u.get("deleted_at")]
    page = _int_param(request, "page", 1) or 1
    per_page = _int_param(request, "per_page", 50) or 50
    start = max(page - 1, 0) * per_page
    window = users[start:start + per_page]
    last_page = max((len(users) + per_page - 1) // per_page, 1)
    return JSONResponse(
        content={"users": [_auth_user_json(u) for u in window], "aud": "authenticated"},
        headers={"x-total-count": str(len(users)), "x-total-pages": str(last_page)},
    )


def _int_param(request: Request, name: str, default: int) -> int | None:
    """GoTrue ignores blank paging parameters; the SDK sends them blank when unset."""
    raw = request.query_params.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@app.post("/auth/v1/admin/users", status_code=200)
async def auth_create_user(request: Request) -> Response:
    body = await _object_body(request)
    if not (body.get("email") or body.get("phone")):
        return auth_error(400, "validation_failed",
                          "Unable to validate email address or phone number")
    if body.get("email") and _find_user(email=body["email"]):
        return auth_error(422, "email_exists",
                          "A user with this email address has already been registered")
    weak = _weak_password(body.get("password"))
    if weak is not None:
        return weak
    user = _new_user(body, confirmed=bool(body.get("email_confirm", True)))
    if body.get("ban_duration") and body["ban_duration"] != "none":
        user["banned_until"] = _now()
    return JSONResponse(content=_auth_user_json(user))


@app.get("/auth/v1/admin/users/{user_id}")
def auth_get_user(user_id: str) -> Response:
    user = STATE["auth_users"].get(user_id)
    if not user:
        return auth_error(404, "user_not_found", "User not found")
    return JSONResponse(content=_auth_user_json(user))


@app.put("/auth/v1/admin/users/{user_id}")
@app.patch("/auth/v1/admin/users/{user_id}")
async def auth_update_user(user_id: str, request: Request) -> Response:
    user = STATE["auth_users"].get(user_id)
    if not user:
        return auth_error(404, "user_not_found", "User not found")
    body = await _object_body(request)
    weak = _weak_password(body.get("password"))
    if weak is not None:
        return weak
    for field in ("email", "phone", "role", "user_metadata", "app_metadata"):
        if field in body:
            user[field] = body[field]
    if "data" in body:
        user["user_metadata"] = {**(user.get("user_metadata") or {}), **(body["data"] or {})}
    if body.get("password"):
        STATE["_passwords"][user_id] = _password_hash(body["password"])
    if body.get("email_confirm"):
        user["email_confirmed_at"] = user.get("email_confirmed_at") or _now()
    if "ban_duration" in body:
        user["banned_until"] = None if body["ban_duration"] in ("none", None) else _now()
    user["updated_at"] = _now()
    return JSONResponse(content=_auth_user_json(user))


@app.delete("/auth/v1/admin/users/{user_id}")
async def auth_delete_user(user_id: str, request: Request) -> Response:
    user = STATE["auth_users"].get(user_id)
    if not user:
        return auth_error(404, "user_not_found", "User not found")
    body = await _object_body(request)
    if body.get("should_soft_delete"):
        user["deleted_at"] = _now()
    else:
        del STATE["auth_users"][user_id]
        STATE["_passwords"].pop(user_id, None)
    return JSONResponse(content={})


@app.post("/auth/v1/invite")
async def auth_invite_user(request: Request) -> Response:
    body = await _object_body(request)
    email = body.get("email")
    if not email:
        return auth_error(400, "validation_failed", "Unable to validate email address")
    if _find_user(email=email):
        return auth_error(422, "email_exists",
                          "A user with this email address has already been registered")
    user = _new_user({"email": email, "user_metadata": body.get("data") or {}}, confirmed=False)
    user["invited_at"] = _now()
    return JSONResponse(content=_auth_user_json(user))


@app.post("/auth/v1/signup")
async def auth_signup(request: Request) -> Response:
    """Sign up with email and password; the twin confirms the address immediately."""
    body = await _object_body(request)
    if not (body.get("email") or body.get("phone")):
        return auth_error(400, "validation_failed",
                          "Unable to validate email address or phone number")
    weak = _weak_password(body.get("password"))
    if weak is not None:
        return weak
    if _find_user(email=body.get("email"), phone=body.get("phone")):
        return auth_error(422, "user_already_exists", "User already registered")
    user = _new_user(body, confirmed=True)
    return JSONResponse(content=_issue_session(user))


@app.post("/auth/v1/token")
async def auth_token(request: Request) -> Response:
    """Password and refresh-token grants: ``/auth/v1/token?grant_type=password``."""
    grant = request.query_params.get("grant_type", "password")
    body = await _object_body(request)
    if grant == "refresh_token":
        user_id = STATE["_refresh_tokens"].pop(body.get("refresh_token") or "", None)
        user = STATE["auth_users"].get(user_id or "")
        if not user:
            return auth_error(400, "refresh_token_not_found", "Invalid Refresh Token: Not Found")
        return JSONResponse(content=_issue_session(user))
    if grant != "password":
        return auth_error(400, "validation_failed", f"unsupported_grant_type: {grant}")
    user = _find_user(email=body.get("email"), phone=body.get("phone"))
    if not user or not _password_ok(user, body.get("password") or ""):
        return auth_error(400, "invalid_credentials", "Invalid login credentials")
    if user.get("banned_until"):
        return auth_error(403, "user_banned", "User is banned")
    return JSONResponse(content=_issue_session(user))


@app.get("/auth/v1/user")
def auth_get_current_user(request: Request) -> Response:
    user = _bearer_user(request)
    if not user:
        return auth_error(401, "bad_jwt", "invalid claim: missing sub claim")
    return JSONResponse(content=_auth_user_json(user))


@app.put("/auth/v1/user")
async def auth_update_current_user(request: Request) -> Response:
    user = _bearer_user(request)
    if not user:
        return auth_error(401, "bad_jwt", "invalid claim: missing sub claim")
    return await auth_update_user(user["id"], request)


@app.post("/auth/v1/logout")
async def auth_logout(request: Request) -> Response:
    user = _bearer_user(request)
    if user:
        for token, owner in list(STATE["_refresh_tokens"].items()):
            if owner == user["id"]:
                del STATE["_refresh_tokens"][token]
    return Response(status_code=204)


@app.post("/auth/v1/recover")
async def auth_recover(request: Request) -> Response:
    await _object_body(request)
    return JSONResponse(content={})


# --- Storage: buckets --------------------------------------------------------

_SIZE_UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4}


def _file_size_limit(value: Any) -> int | None:
    """Storage accepts ``5242880`` or ``"5MB"``; the API reports bytes."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?b)?\s*", str(value), re.I)
    if not match:
        return None
    return int(float(match[1]) * _SIZE_UNITS.get((match[2] or "b").lower(), 1))


def _bucket_json(bucket: dict) -> dict:
    created = bucket.get("created_at") or _now()
    return {
        "id": bucket.get("id", ""),
        "name": bucket.get("name") or bucket.get("id", ""),
        "owner": bucket.get("owner") or "",
        "public": bool(bucket.get("public", False)),
        "file_size_limit": _file_size_limit(bucket.get("file_size_limit")),
        "allowed_mime_types": bucket.get("allowed_mime_types"),
        "created_at": created,
        "updated_at": bucket.get("updated_at") or created,
        "type": bucket.get("type") or "STANDARD",
    }


def _no_such_bucket() -> JSONResponse:
    return storage_error(404, "NoSuchBucket", "Bucket not found", "Bucket not found")


def _no_such_key() -> JSONResponse:
    return storage_error(404, "NoSuchKey", "not_found", "Object not found")


@app.get("/storage/v1/bucket")
def storage_list_buckets() -> Response:
    return JSONResponse(content=[_bucket_json(b) for b in STATE["storage"]["buckets"].values()])


@app.post("/storage/v1/bucket")
async def storage_create_bucket(request: Request) -> Response:
    body = await _object_body(request)
    bucket_id = body.get("id") or body.get("name") or ""
    if not bucket_id:
        return storage_error(400, "InvalidRequest", "Invalid Request", "id is required")
    if bucket_id in STATE["storage"]["buckets"]:
        return storage_error(409, "ResourceAlreadyExists", "Duplicate",
                             "The resource already exists")
    now = _now()
    STATE["storage"]["buckets"][bucket_id] = {
        "id": bucket_id,
        "name": body.get("name") or bucket_id,
        "owner": "",
        "public": bool(body.get("public", False)),
        "file_size_limit": _file_size_limit(body.get("file_size_limit")),
        "allowed_mime_types": body.get("allowed_mime_types"),
        "created_at": now,
        "updated_at": now,
        "type": body.get("type") or "STANDARD",
    }
    return JSONResponse(content={"name": bucket_id})


@app.get("/storage/v1/bucket/{bucket_id}")
def storage_get_bucket(bucket_id: str) -> Response:
    bucket = STATE["storage"]["buckets"].get(bucket_id)
    if bucket is None:
        return _no_such_bucket()
    return JSONResponse(content=_bucket_json(bucket))


@app.put("/storage/v1/bucket/{bucket_id}")
async def storage_update_bucket(bucket_id: str, request: Request) -> Response:
    bucket = STATE["storage"]["buckets"].get(bucket_id)
    if bucket is None:
        return _no_such_bucket()
    body = await _object_body(request)
    if "public" in body:
        bucket["public"] = bool(body["public"])
    if "file_size_limit" in body:
        bucket["file_size_limit"] = _file_size_limit(body["file_size_limit"])
    if "allowed_mime_types" in body:
        bucket["allowed_mime_types"] = body["allowed_mime_types"]
    bucket["updated_at"] = _now()
    return JSONResponse(content={"message": "Successfully updated"})


@app.post("/storage/v1/bucket/{bucket_id}/empty")
def storage_empty_bucket(bucket_id: str) -> Response:
    if bucket_id not in STATE["storage"]["buckets"]:
        return _no_such_bucket()
    for key in [k for k in STATE["storage"]["objects"] if k.startswith(f"{bucket_id}/")]:
        del STATE["storage"]["objects"][key]
    return JSONResponse(content={"message": "Successfully emptied"})


@app.delete("/storage/v1/bucket/{bucket_id}")
def storage_delete_bucket(bucket_id: str) -> Response:
    if bucket_id not in STATE["storage"]["buckets"]:
        return _no_such_bucket()
    if any(k.startswith(f"{bucket_id}/") for k in STATE["storage"]["objects"]):
        return storage_error(409, "InvalidRequest", "Invalid Request",
                             "The bucket you tried to delete is not empty")
    del STATE["storage"]["buckets"][bucket_id]
    return JSONResponse(content={"message": "Successfully deleted"})


# --- Storage: objects --------------------------------------------------------
# Specific routes come first: Starlette matches in registration order, so
# "list", "info", "sign", "move" and "copy" must not be read as bucket ids.

def _object_json(obj: dict, *, name: str | None = None) -> dict:
    created = obj.get("created_at") or _now()
    return {
        "name": name if name is not None else obj.get("name", ""),
        "id": obj.get("id") or _uid(),
        "updated_at": obj.get("updated_at") or created,
        "created_at": created,
        "last_accessed_at": obj.get("last_accessed_at") or created,
        "metadata": obj.get("metadata") or {},
    }


def _object_bytes(obj: dict) -> bytes:
    if obj.get("_content_b64"):
        return base64.b64decode(obj["_content_b64"])
    content = obj.get("content")
    if isinstance(content, str):
        return content.encode()
    return b""


def _store_object(bucket_id: str, path: str, content: bytes, mime: str,
                  metadata: dict | None = None) -> dict:
    key = f"{bucket_id}/{path}"
    now = _now()
    existing = STATE["storage"]["objects"].get(key) or {}
    obj = {
        "id": existing.get("id") or _uid(),
        "bucket_id": bucket_id,
        "name": path,
        "owner": existing.get("owner") or "",
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "last_accessed_at": now,
        "metadata": {
            # An ETag the vendor's own clients compare; not a secret.
            "eTag": f'"{hashlib.md5(content).hexdigest()}"',  # noqa: S324 - an identifier the vendor shapes, not a secret
            "size": len(content),
            "mimetype": mime,
            "cacheControl": "max-age=3600",
            "lastModified": now,
            "contentLength": len(content),
            "httpStatusCode": 200,
            **(metadata or {}),
        },
        "_content_b64": base64.b64encode(content).decode(),
    }
    STATE["storage"]["objects"][key] = obj
    return obj


def _parse_upload(body: bytes, content_type: str) -> tuple[bytes, str, dict[str, str]]:
    """Read an upload body: multipart (both SDKs) or a raw body with a content type."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if "multipart/form-data" not in (content_type or "") or boundary is None:
        return body, (content_type or "application/octet-stream").split(";")[0].strip(), {}
    fields: dict[str, str] = {}
    file_content, file_type = b"", "application/octet-stream"
    separator = b"--" + boundary[1].encode()
    for chunk in body.split(separator):
        part = chunk.strip(b"\r\n")
        if not part or part == b"--":
            continue
        raw_headers, _, payload = part.partition(b"\r\n\r\n")
        headers = {}
        for line in raw_headers.decode("utf-8", "replace").splitlines():
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        if "filename=" in disposition or (name_match and name_match[1] in ("file", "")):
            file_content = payload
            file_type = headers.get("content-type", file_type).split(";")[0].strip()
        elif name_match:
            fields[name_match[1]] = payload.decode("utf-8", "replace")
    return file_content, file_type, fields


def _list_objects(bucket_id: str, body: dict) -> list[dict]:
    """One folder level, the way storage's ``search`` does: files, then sub-folders."""
    prefix = (body.get("prefix") or "").strip()
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    search = (body.get("search") or "").lower()
    files: list[dict] = []
    folders: dict[str, str] = {}
    for key, obj in STATE["storage"]["objects"].items():
        if not key.startswith(f"{bucket_id}/"):
            continue
        name = key[len(bucket_id) + 1:]
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix):]
        if search and not relative.lower().startswith(search):
            continue
        head, slash, _ = relative.partition("/")
        if slash:
            folders.setdefault(head, obj.get("created_at") or _now())
        else:
            files.append(_object_json(obj, name=relative))
    listing = files + [{"name": name, "id": None, "updated_at": None, "created_at": None,
                        "last_accessed_at": None, "metadata": None}
                       for name in sorted(folders)]
    column = (body.get("sortBy") or {}).get("column", "name")
    descending = (body.get("sortBy") or {}).get("order", "asc") == "desc"
    listing.sort(key=lambda item: str(item.get(column) or ""), reverse=descending)
    offset = int(body.get("offset") or 0)
    limit = int(body.get("limit") or 100)
    return listing[offset:offset + limit]


@app.post("/storage/v1/object/list/{bucket_id}")
async def storage_list_objects(bucket_id: str, request: Request) -> Response:
    if bucket_id not in STATE["storage"]["buckets"]:
        return _no_such_bucket()
    body = await _object_body(request)
    return JSONResponse(content=_list_objects(bucket_id, body))


@app.get("/storage/v1/object/info/public/{bucket_id}/{path:path}")
def storage_object_info_public(bucket_id: str, path: str) -> Response:
    return storage_object_info(bucket_id, path)


@app.get("/storage/v1/object/info/{bucket_id}/{path:path}")
def storage_object_info(bucket_id: str, path: str) -> Response:
    obj = STATE["storage"]["objects"].get(f"{bucket_id}/{path}")
    if obj is None:
        return _no_such_key()
    metadata = obj.get("metadata") or {}
    return JSONResponse(content={
        **_object_json(obj),
        "bucket_id": bucket_id,
        "version": obj.get("version") or "1",
        "size": metadata.get("size", 0),
        "content_type": metadata.get("mimetype", "application/octet-stream"),
        "cache_control": metadata.get("cacheControl", "max-age=3600"),
        "etag": metadata.get("eTag", ""),
    })


@app.post("/storage/v1/object/move")
async def storage_move_object(request: Request) -> Response:
    body = await _object_body(request)
    source_bucket = body.get("bucketId", "")
    target_bucket = body.get("destinationBucket") or source_bucket
    source = f"{source_bucket}/{body.get('sourceKey', '')}"
    obj = STATE["storage"]["objects"].get(source)
    if obj is None:
        return _no_such_key()
    if target_bucket not in STATE["storage"]["buckets"]:
        return _no_such_bucket()
    del STATE["storage"]["objects"][source]
    obj["name"] = body.get("destinationKey", obj["name"])
    obj["bucket_id"] = target_bucket
    obj["updated_at"] = _now()
    STATE["storage"]["objects"][f"{target_bucket}/{obj['name']}"] = obj
    return JSONResponse(content={"message": "Successfully moved"})


@app.post("/storage/v1/object/copy")
async def storage_copy_object(request: Request) -> Response:
    body = await _object_body(request)
    source_bucket = body.get("bucketId", "")
    target_bucket = body.get("destinationBucket") or source_bucket
    obj = STATE["storage"]["objects"].get(f"{source_bucket}/{body.get('sourceKey', '')}")
    if obj is None:
        return _no_such_key()
    if target_bucket not in STATE["storage"]["buckets"]:
        return _no_such_bucket()
    target_path = body.get("destinationKey", obj["name"])
    copy = _store_object(target_bucket, target_path, _object_bytes(obj),
                         (obj.get("metadata") or {}).get("mimetype", "application/octet-stream"))
    return JSONResponse(content={"Id": copy["id"], "Key": f"{target_bucket}/{target_path}"})


@app.post("/storage/v1/object/sign/{bucket_id}/{path:path}")
async def storage_sign_object(bucket_id: str, path: str, request: Request) -> Response:
    """Mint a signed URL: ``/object/sign/<bucket>/<path>?token=...``, valid without a key."""
    if f"{bucket_id}/{path}" not in STATE["storage"]["objects"]:
        return _no_such_key()
    body = await _object_body(request)
    token = _sign(f"{bucket_id}/{path}", int(body.get("expiresIn") or 3600))
    return JSONResponse(content={"signedURL": f"/object/sign/{bucket_id}/{path}?token={token}"})


@app.post("/storage/v1/object/sign/{bucket_id}")
async def storage_sign_objects(bucket_id: str, request: Request) -> Response:
    body = await _object_body(request)
    expires_in = int(body.get("expiresIn") or 3600)
    signed = []
    for path in body.get("paths") or []:
        if f"{bucket_id}/{path}" in STATE["storage"]["objects"]:
            token = _sign(f"{bucket_id}/{path}", expires_in)
            signed.append({"error": None, "path": path,
                           "signedURL": f"/object/sign/{bucket_id}/{path}?token={token}"})
        else:
            signed.append({"error": "Either the object does not exist or you do not have access",
                           "path": path, "signedURL": None})
    return JSONResponse(content=signed)


@app.post("/storage/v1/object/upload/sign/{bucket_id}/{path:path}")
def storage_sign_upload(bucket_id: str, path: str) -> Response:
    if bucket_id not in STATE["storage"]["buckets"]:
        return _no_such_bucket()
    token = _sign(f"{bucket_id}/{path}", 7200, upload=True)
    return JSONResponse(content={"url": f"/object/upload/sign/{bucket_id}/{path}?token={token}"})


@app.put("/storage/v1/object/upload/sign/{bucket_id}/{path:path}")
async def storage_upload_signed(bucket_id: str, path: str, request: Request) -> Response:
    grant = _verify_signature(request.query_params.get("token"), f"{bucket_id}/{path}",
                              upload=True)
    if grant is None:
        return storage_error(400, "InvalidSignature", "InvalidSignature",
                             "The signature is invalid or expired")
    content, mime, _ = _parse_upload(await request.body(), request.headers.get("content-type", ""))
    obj = _store_object(bucket_id, path, content, mime)
    return JSONResponse(content={"Id": obj["id"], "Key": f"{bucket_id}/{path}"})


@app.get("/storage/v1/object/sign/{bucket_id}/{path:path}")
def storage_download_signed(bucket_id: str, path: str, request: Request) -> Response:
    key = f"{bucket_id}/{path}"
    if _verify_signature(request.query_params.get("token"), key) is None:
        return storage_error(400, "InvalidSignature", "InvalidSignature",
                             "The signature is invalid or expired")
    return _download(key)


@app.api_route("/storage/v1/object/public/{bucket_id}/{path:path}", methods=["GET", "HEAD"])
def storage_download_public(bucket_id: str, path: str) -> Response:
    bucket = STATE["storage"]["buckets"].get(bucket_id)
    if bucket is None or not bucket.get("public"):
        return storage_error(400, "NoSuchKey", "not_found", "Object not found")
    return _download(f"{bucket_id}/{path}")


@app.api_route("/storage/v1/object/authenticated/{bucket_id}/{path:path}",
               methods=["GET", "HEAD"])
def storage_download_authenticated(bucket_id: str, path: str) -> Response:
    return _download(f"{bucket_id}/{path}")


@app.api_route("/storage/v1/object/{bucket_id}/{path:path}", methods=["GET", "HEAD"])
def storage_download_object(bucket_id: str, path: str) -> Response:
    return _download(f"{bucket_id}/{path}")


def _download(key: str) -> Response:
    obj = STATE["storage"]["objects"].get(key)
    if obj is None:
        return _no_such_key()
    metadata = obj.get("metadata") or {}
    return Response(content=_object_bytes(obj),
                    media_type=metadata.get("mimetype", "application/octet-stream"),
                    headers={"cache-control": metadata.get("cacheControl", "max-age=3600"),
                             "etag": metadata.get("eTag", "")})


@app.post("/storage/v1/object/{bucket_id}/{path:path}")
@app.put("/storage/v1/object/{bucket_id}/{path:path}")
async def storage_upload_object(bucket_id: str, path: str, request: Request) -> Response:
    """Upload (POST) or replace (PUT) an object; both SDKs send multipart bodies."""
    bucket = STATE["storage"]["buckets"].get(bucket_id)
    if bucket is None:
        return _no_such_bucket()
    key = f"{bucket_id}/{path}"
    upsert = request.headers.get("x-upsert", "").lower() == "true" or request.method == "PUT"
    if not upsert and key in STATE["storage"]["objects"]:
        return storage_error(409, "KeyAlreadyExists", "Duplicate", "The resource already exists")
    if request.method == "PUT" and key not in STATE["storage"]["objects"]:
        return _no_such_key()
    content, mime, _ = _parse_upload(await request.body(), request.headers.get("content-type", ""))
    limit = _file_size_limit(bucket.get("file_size_limit"))
    if limit is not None and len(content) > limit:
        return storage_error(413, "EntityTooLarge", "Payload too large",
                             "The object exceeded the maximum allowed size")
    allowed = bucket.get("allowed_mime_types")
    if allowed and mime not in allowed:
        return storage_error(415, "InvalidMimeType", "invalid_mime_type",
                             f"mime type {mime} is not supported")
    obj = _store_object(bucket_id, path, content, mime)
    return JSONResponse(content={"Id": obj["id"], "Key": key})


@app.delete("/storage/v1/object/{bucket_id}")
async def storage_delete_objects(bucket_id: str, request: Request) -> Response:
    """Bulk delete: ``{"prefixes": ["a.txt", "b/c.txt"]}``."""
    if bucket_id not in STATE["storage"]["buckets"]:
        return _no_such_bucket()
    body = await _object_body(request)
    removed = []
    for path in body.get("prefixes") or []:
        obj = STATE["storage"]["objects"].pop(f"{bucket_id}/{path}", None)
        if obj is not None:
            removed.append({**_object_json(obj, name=path), "bucket_id": bucket_id})
    return JSONResponse(content=removed)


@app.delete("/storage/v1/object/{bucket_id}/{path:path}")
def storage_delete_object(bucket_id: str, path: str) -> Response:
    if STATE["storage"]["objects"].pop(f"{bucket_id}/{path}", None) is None:
        return _no_such_key()
    return JSONResponse(content={"message": "Successfully deleted"})


def _sign(key: str, expires_in: int, *, upload: bool = False) -> str:
    token = secrets.token_hex(16)
    STATE["_signed"][token] = {"key": key, "expires_at": time.time() + expires_in,
                               "upload": upload}
    return token


def _verify_signature(token: str | None, key: str, *, upload: bool = False) -> dict | None:
    grant = STATE["_signed"].get(token or "")
    if grant is None or grant["key"] != key or grant.get("upload", False) != upload:
        return None
    return None if grant["expires_at"] < time.time() else grant


# --- runtime: faults, trace, views, control plane ----------------------------

def _classify(method: str, path: str, body: object) -> tuple[kit.Op, str] | None:
    """Map a call to (op, resource); several Supabase writes hide behind POST."""
    verb: kit.Op = {"GET": "read", "HEAD": "read", "POST": "create", "PUT": "update",
                    "PATCH": "update", "DELETE": "delete"}.get(method.upper(), "other")
    segments = [s for s in path.split("/") if s]
    if path.startswith("/rest/v1/rpc/"):
        return "other", f"rpc:{segments[-1]}"
    if path.startswith("/rest/v1"):
        return (verb, segments[2]) if len(segments) >= 3 else ("read", "tables")
    if path.startswith("/functions/v1/"):
        return "other", f"function:{'/'.join(segments[2:])}"
    if path.startswith("/auth/v1"):
        if path.endswith(("/token", "/logout")):
            return ("delete" if path.endswith("/logout") else "create"), "auth.sessions"
        return verb, "auth.users"
    if path.startswith("/storage/v1/bucket"):
        if path.endswith("/empty"):
            return "delete", "storage.objects"
        return verb, "storage.buckets"
    if path.startswith("/storage/v1/object"):
        if path.startswith("/storage/v1/object/upload/sign/"):
            # POST mints the upload URL; the PUT that follows stores the object.
            return ("create", "storage.objects" if method == "PUT" else "storage.signed_urls")
        if "/object/sign" in path:
            # Signing creates a grant; fetching through it downloads the object.
            return ("create", "storage.signed_urls") if method == "POST" \
                else ("read", "storage.objects")
        if path.startswith("/storage/v1/object/move"):
            return "update", "storage.objects"
        if path.startswith("/storage/v1/object/copy"):
            return "create", "storage.objects"
        if path.startswith("/storage/v1/object/list/"):
            return "read", "storage.objects"
        return verb, "storage.objects"
    return None


def _views(state: dict) -> dict[str, kit.View]:
    """Every table as its own collection, plus auth users, buckets and objects.

    The names carry no dots. A collection name is half of an assertion's path —
    ``supabase.auth_users`` — so a dot inside it would read as another level and
    make the collection unaddressable: criteria about auth users or buckets
    could not be written at all, and the ones that tried failed as schema
    errors. The service's own dotted names survive in the request trace, where
    they are strings rather than paths.
    """
    views: dict[str, kit.View] = {}
    schema = pgrst.Schema(state.get("tables") or {})
    for name in schema.names():
        table = schema.get(name)
        key_columns = table.primary_key
        items = []
        for index, row in enumerate(table.rows):
            key = ("|".join(str(row.get(c)) for c in key_columns) if key_columns
                   else f"{name}#{index}")
            items.append({"_key": key, **row})
        views[name] = kit.View(items, key="_key", nouns=(pgrst.singular(name), name))
    views["auth_users"] = kit.View(
        [{**_auth_user_json(user),
          "provider": (user.get("app_metadata") or {}).get("provider", "email"),
          "confirmed": bool(user.get("email_confirmed_at") or user.get("phone_confirmed_at")),
          "banned": bool(user.get("banned_until"))}
         for user in (state.get("auth_users") or {}).values()],
        key="id", tombstone="deleted_at", nouns=("auth user", "auth users"))
    storage = state.get("storage") or {}
    views["storage_buckets"] = kit.View(
        [{**_bucket_json(bucket),
          "object_count": sum(1 for k in (storage.get("objects") or {})
                              if k.startswith(f"{bucket.get('id', '')}/"))}
         for bucket in (storage.get("buckets") or {}).values()],
        key="id", nouns=("storage bucket", "storage buckets"))
    views["storage_objects"] = kit.View(
        [{"key": key,
          "bucket": obj.get("bucket_id") or key.split("/", 1)[0],
          "name": obj.get("name") or key.split("/", 1)[-1],
          "size": (obj.get("metadata") or {}).get("size", 0),
          "mimetype": (obj.get("metadata") or {}).get("mimetype", ""),
          "created_at": obj.get("created_at"),
          "updated_at": obj.get("updated_at")}
         for key, obj in (storage.get("objects") or {}).items()],
        key="key", nouns=("file", "files"))
    return views


def _after_seed(state: dict) -> None:
    """Normalise a seed: passwords move out of the user records, objects get metadata."""
    state.setdefault("_passwords", {})
    state.setdefault("_refresh_tokens", {})
    state.setdefault("_signed", {})
    for user_id, user in (state.get("auth_users") or {}).items():
        user.setdefault("id", user_id)
        password = user.pop("password", None)
        if password:
            state["_passwords"][user_id] = _password_hash(password)
    objects = (state.get("storage") or {}).get("objects") or {}
    for key, obj in objects.items():
        obj.setdefault("bucket_id", key.split("/", 1)[0])
        obj.setdefault("name", key.split("/", 1)[-1])
        content = obj.pop("content", "")
        obj.setdefault("_content_b64", base64.b64encode(str(content).encode()).decode())
        size = obj.get("size", len(str(content)))
        obj.setdefault("metadata", {"size": size, "mimetype": obj.get("content_type")
                                    or "application/octet-stream"})


TWIN = kit.install(app, kit.Twin(
    name="supabase",
    state=STATE,
    trace=TRACE,
    fresh_state=_fresh_state,
    seeds_dir=SEEDS_DIR,
    error=_error,
    authenticate=_authenticate,
    after_seed=_after_seed,
    views=_views,
    classify=_classify,
))


@app.exception_handler(StarletteHTTPException)
async def _service_shaped_errors(request: Request, exc: StarletteHTTPException) -> Response:
    """Unknown routes must answer like Supabase, not like FastAPI's ``{"detail": ...}``."""
    detail = exc.detail if isinstance(exc.detail, str) else "Not Found"
    if request.url.path.startswith(kit.CONTROL_PREFIX):
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})
    surface = _surface(request.url.path)
    if surface == "auth":
        return auth_error(exc.status_code, "not_found" if exc.status_code == 404
                          else "validation_failed", detail)
    if surface == "storage":
        return storage_error(exc.status_code, "InvalidRequest", "Invalid Request", detail)
    return postgrest_error(exc.status_code, str(exc.status_code), detail)


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.supabase_mcp import mount_on as _mount_mcp

_mount_mcp(app)
