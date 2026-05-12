"""GitHub twin: stateful in-memory clone of GitHub REST API.

Phase 1 covered: repos, issues, comments, labels.
Phase 2 adds: bootstrap-token auth, GitHub-shape error envelopes,
`X-GitHub-*` response headers, full repos/branches/files/commits/PRs/
workflows/search surface, named seeds, rate-limit + permissions-denied.

Introspection at /_health, /_trace, /_state, /_reset, /_seed/<name>, /_config.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

app = FastAPI(title="checkpoint github twin")

# Per SCOPE §3.2 / REQUIREMENTS.md GH-02.
DEFAULT_BOOTSTRAP_TOKEN = "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt"
DOC_URL = "https://docs.github.com/rest"
RATE_DOC_URL = (
    "https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting"
)
INTROSPECTION_PREFIX = "/_"

SEEDS_DIR = Path(__file__).parent / "github_seeds"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_state() -> dict:
    return {
        "repos": {},
        "issues": {},
        "comments": {},
        "labels": {},
        "pulls": {},          # "owner/name#number" -> pull dict (with reviews/comments/files/commits inline)
        "workflow_runs": {},  # "owner/name#run_id" -> run dict
        "users": {            # login -> user dict (search_users target)
            "default-user": {"login": "default-user", "id": 1, "type": "User"},
        },
        "_counters": {
            "issue_number_per_repo": {},
            "pull_number_per_repo": {},
            "comment_id": 0,
            "review_id": 0,
            "repo_id": 0,
            "run_id": 0,
            "sha_seq_per_repo": {},
            "requests": 0,
        },
        "_config": {
            "rate_limit": None,         # None = unlimited
            "permissions_denied": False,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers -------------------------------------------------------------

def gh_error(status: int, message: str, *, documentation_url: str = DOC_URL,
             **extra: Any) -> JSONResponse:
    """Return a GitHub-shaped error JSONResponse."""
    body: dict[str, Any] = {"message": message, "documentation_url": documentation_url}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _bootstrap_token() -> str:
    return os.environ.get("GITHUB_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    auth_header = auth_header.strip()
    for prefix in ("token ", "Bearer ", "bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return None


def _user(login: str) -> dict:
    if login not in STATE["users"]:
        STATE["users"][login] = {
            "login": login, "id": len(STATE["users"]) + 1, "type": "User",
        }
    return STATE["users"][login]


def _synthetic_sha(repo_key: str) -> str:
    seq_map = STATE["_counters"]["sha_seq_per_repo"]
    seq_map[repo_key] = seq_map.get(repo_key, 0) + 1
    return hashlib.sha256(
        f"{repo_key}#{seq_map[repo_key]}".encode()
    ).hexdigest()[:40]


def _make_repo(owner: str, name: str) -> dict:
    STATE["_counters"]["repo_id"] += 1
    repo_key = f"{owner}/{name}"
    initial_sha = _synthetic_sha(repo_key)
    repo = {
        "id": STATE["_counters"]["repo_id"],
        "name": name,
        "full_name": repo_key,
        "owner": _user(owner),
        "default_branch": "main",
        "private": False,
        "created_at": _now(),
        "updated_at": _now(),
        "html_url": f"http://localhost/{repo_key}",
        "branches": {
            "main": {"name": "main", "sha": initial_sha, "protected": False},
        },
        "commits": [
            {
                "sha": initial_sha,
                "commit": {
                    "message": "Initial commit",
                    "author": {"name": owner, "date": _now()},
                },
                "files": [],
            }
        ],
        "files": {},  # path -> {content (str), sha, branch_shas: {branch: sha}}
    }
    return repo


def _ensure_repo(owner: str, name: str) -> dict:
    key = f"{owner}/{name}"
    if key not in STATE["repos"]:
        STATE["repos"][key] = _make_repo(owner, name)
    return STATE["repos"][key]


def _gh_headers(extra: dict | None = None) -> dict:
    rate_limit = STATE["_config"].get("rate_limit")
    used = STATE["_counters"]["requests"]
    if rate_limit is None:
        limit = 5000
        remaining = 5000
    else:
        limit = rate_limit
        remaining = max(0, rate_limit - used)
    headers = {
        "X-GitHub-Media-Type": "github.v3; format=json",
        "X-GitHub-Request-Id": uuid.uuid4().hex[:16].upper(),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": "0",
    }
    if extra:
        headers.update(extra)
    return headers


# --- middlewares ---------------------------------------------------------

@app.middleware("http")
async def auth_and_limits_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method

    # Introspection bypasses all auth/limits/headers logic.
    if path.startswith(INTROSPECTION_PREFIX):
        return await call_next(request)

    # 1. Auth gate
    token = _extract_token(request.headers.get("authorization"))
    if token != _bootstrap_token():
        return gh_error(
            401,
            "Bad credentials",
            documentation_url="https://docs.github.com/rest",
        )

    # 2. Permissions-denied gate (writes only)
    if STATE["_config"].get("permissions_denied") and method in (
        "POST", "PATCH", "PUT", "DELETE",
    ):
        return gh_error(
            403,
            "Resource not accessible by integration",
            documentation_url="https://docs.github.com/rest",
        )

    # 3. Rate-limit gate (count BEFORE running handler so /repos GET counts)
    STATE["_counters"]["requests"] += 1
    rl = STATE["_config"].get("rate_limit")
    if rl is not None and STATE["_counters"]["requests"] > rl:
        # Build a deterministic message in real-GitHub shape.
        return JSONResponse(
            status_code=429,
            content={
                "message": "API rate limit exceeded for 127.0.0.1.",
                "documentation_url": RATE_DOC_URL,
            },
            headers=_gh_headers({"Retry-After": "60"}),
        )

    response = await call_next(request)
    # Stamp headers (don't override Content-Length).
    for k, v in _gh_headers().items():
        response.headers[k] = v
    return response


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(INTROSPECTION_PREFIX):
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


# --- introspection endpoints (not traced, not authed) -------------------

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
    if "rate_limit" in body:
        cfg["rate_limit"] = body["rate_limit"]
    if "permissions_denied" in body:
        cfg["permissions_denied"] = bool(body["permissions_denied"])
    return {"ok": True, "config": cfg}


@app.post("/_seed/{name}")
def load_seed(name: str):
    path = SEEDS_DIR / f"{name}.json"
    if not path.exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": f"seed {name!r} not found"})
    data = json.loads(path.read_text())
    # Reset first.
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()
    # Deep-merge seed state.
    for k, v in (data.get("state") or {}).items():
        if isinstance(v, dict) and isinstance(STATE.get(k), dict):
            STATE[k].update(v)
        else:
            STATE[k] = v
    # Apply config.
    cfg = data.get("config") or {}
    if "rate_limit" in cfg:
        STATE["_config"]["rate_limit"] = cfg["rate_limit"]
    if "permissions_denied" in cfg:
        STATE["_config"]["permissions_denied"] = bool(cfg["permissions_denied"])
    return {"ok": True, "seed": name, "config": STATE["_config"]}


# --- repos ---------------------------------------------------------------

@app.post("/user/repos", status_code=201)
async def create_user_repo(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return gh_error(422, "name is required")
    owner = body.get("owner") or "default-user"
    key = f"{owner}/{name}"
    if key in STATE["repos"]:
        return gh_error(422, "Repository already exists")
    repo = _make_repo(owner, name)
    if "private" in body:
        repo["private"] = bool(body["private"])
    STATE["repos"][key] = repo
    return repo


@app.get("/repos/{owner}/{name}")
def get_repo(owner: str, name: str):
    key = f"{owner}/{name}"
    if key not in STATE["repos"]:
        return gh_error(404, "Not Found")
    return STATE["repos"][key]


# --- issues --------------------------------------------------------------

@app.post("/repos/{owner}/{name}/issues", status_code=201)
async def create_issue(owner: str, name: str, request: Request):
    body = await request.json()
    title = body.get("title")
    if not title:
        return gh_error(422, "title is required")
    _ensure_repo(owner, name)
    repo_key = f"{owner}/{name}"
    counters = STATE["_counters"]["issue_number_per_repo"]
    counters[repo_key] = counters.get(repo_key, 0) + 1
    number = counters[repo_key]
    labels = [{"name": n, "color": "ededed"} for n in (body.get("labels") or [])]
    issue = {
        "number": number,
        "title": title,
        "body": body.get("body", "") or "",
        "state": "open",
        "labels": labels,
        "comments": 0,
        "user": _user("default-user"),
        "created_at": _now(),
        "updated_at": _now(),
        "closed_at": None,
        "html_url": f"http://localhost/{repo_key}/issues/{number}",
    }
    STATE["issues"][f"{repo_key}#{number}"] = issue
    return issue


@app.get("/repos/{owner}/{name}/issues")
def list_issues(owner: str, name: str, state: str = "open", labels: str | None = None):
    repo_key = f"{owner}/{name}"
    out = []
    for key, issue in STATE["issues"].items():
        if not key.startswith(f"{repo_key}#"):
            continue
        if state != "all" and issue["state"] != state:
            continue
        if labels:
            wanted = set(labels.split(","))
            have = {lab["name"] for lab in issue["labels"]}
            if not wanted.issubset(have):
                continue
        out.append(issue)
    return out


@app.get("/repos/{owner}/{name}/issues/{number}")
def get_issue(owner: str, name: str, number: int):
    key = f"{owner}/{name}#{number}"
    if key not in STATE["issues"]:
        return gh_error(404, "Not Found")
    return STATE["issues"][key]


@app.patch("/repos/{owner}/{name}/issues/{number}")
async def update_issue(owner: str, name: str, number: int, request: Request):
    key = f"{owner}/{name}#{number}"
    if key not in STATE["issues"]:
        return gh_error(404, "Not Found")
    body = await request.json()
    issue = STATE["issues"][key]
    if body.get("title") is not None:
        issue["title"] = body["title"]
    if body.get("body") is not None:
        issue["body"] = body["body"]
    if body.get("state") in ("open", "closed"):
        issue["state"] = body["state"]
        issue["closed_at"] = _now() if body["state"] == "closed" else None
    if isinstance(body.get("labels"), list):
        issue["labels"] = [{"name": n, "color": "ededed"} for n in body["labels"]]
    issue["updated_at"] = _now()
    return issue


# --- comments ------------------------------------------------------------

@app.post("/repos/{owner}/{name}/issues/{number}/comments", status_code=201)
async def add_comment(owner: str, name: str, number: int, request: Request):
    key = f"{owner}/{name}#{number}"
    if key not in STATE["issues"]:
        return gh_error(404, "Not Found")
    body = await request.json()
    STATE["_counters"]["comment_id"] += 1
    cid = STATE["_counters"]["comment_id"]
    comment = {
        "id": cid,
        "body": body.get("body", "") or "",
        "user": _user("default-user"),
        "created_at": _now(),
        "updated_at": _now(),
        "_issue": key,
    }
    STATE["comments"][str(cid)] = comment
    STATE["issues"][key]["comments"] += 1
    return comment


@app.get("/repos/{owner}/{name}/issues/{number}/comments")
def list_comments(owner: str, name: str, number: int):
    key = f"{owner}/{name}#{number}"
    return [c for c in STATE["comments"].values() if c["_issue"] == key]


# --- labels --------------------------------------------------------------

@app.get("/repos/{owner}/{name}/labels")
def list_labels(owner: str, name: str):
    repo_key = f"{owner}/{name}"
    return [v for k, v in STATE["labels"].items() if k.startswith(f"{repo_key}/")]


@app.post("/repos/{owner}/{name}/labels", status_code=201)
async def create_label(owner: str, name: str, request: Request):
    body = await request.json()
    label_name = body.get("name")
    if not label_name:
        return gh_error(422, "name required")
    repo_key = f"{owner}/{name}"
    _ensure_repo(owner, name)
    key = f"{repo_key}/{label_name}"
    if key in STATE["labels"]:
        return gh_error(422, "already_exists")
    label = {"name": label_name, "color": body.get("color", "ededed")}
    STATE["labels"][key] = label
    return label
