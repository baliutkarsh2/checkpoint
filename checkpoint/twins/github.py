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
    # The mounted MCP transport also bypasses — MCP clients don't speak
    # the bootstrap-token contract; the MCP tool bodies stamp the token
    # back on when they shim into the REST surface.
    if path.startswith(INTROSPECTION_PREFIX) or path.startswith("/mcp"):
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


@app.post("/_seed-file")
async def load_seed_file(request: Request):
    """Apply an inline JSON seed payload (same shape as the named seed files
    under github_seeds/). Used by `seed-file:` in scenario config."""
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
    if "rate_limit" in cfg:
        STATE["_config"]["rate_limit"] = cfg["rate_limit"]
    if "permissions_denied" in cfg:
        STATE["_config"]["permissions_denied"] = bool(cfg["permissions_denied"])
    return {"ok": True, "config": STATE["_config"]}


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


@app.get("/search/repositories")
def search_repositories(q: str = "", per_page: int = 30, page: int = 1):
    matches = [
        r for r in STATE["repos"].values()
        if q.lower() in r["full_name"].lower() or q.lower() in r["name"].lower()
    ]
    start = (page - 1) * per_page
    items = matches[start:start + per_page]
    return {"total_count": len(matches), "incomplete_results": False, "items": items}


@app.post("/repos/{owner}/{name}/forks", status_code=202)
async def fork_repository(owner: str, name: str, request: Request):
    src_key = f"{owner}/{name}"
    if src_key not in STATE["repos"]:
        return gh_error(404, "Not Found")
    body = await request.json() if await request.body() else {}
    new_owner = body.get("organization") or "default-user"
    new_key = f"{new_owner}/{name}"
    if new_key in STATE["repos"]:
        return gh_error(422, "Repository already exists")
    # Shallow copy with a fresh ID.
    src = STATE["repos"][src_key]
    fork = _make_repo(new_owner, name)
    fork["fork"] = True
    fork["parent"] = {"full_name": src["full_name"], "id": src["id"]}
    STATE["repos"][new_key] = fork
    return fork


# --- files / contents ---------------------------------------------------

def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _b64decode(s: str) -> str:
    import base64
    try:
        return base64.b64decode(s).decode("utf-8")
    except Exception:
        return ""


@app.get("/repos/{owner}/{name}/contents/{path:path}")
def get_file_contents(owner: str, name: str, path: str, ref: str | None = None):
    repo_key = f"{owner}/{name}"
    if repo_key not in STATE["repos"]:
        return gh_error(404, "Not Found")
    repo = STATE["repos"][repo_key]
    if path not in repo["files"]:
        return gh_error(404, "Not Found")
    entry = repo["files"][path]
    return {
        "type": "file",
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": entry["sha"],
        "size": len(entry["content"]),
        "encoding": "base64",
        "content": _b64(entry["content"]),
    }


@app.put("/repos/{owner}/{name}/contents/{path:path}")
async def create_or_update_file(owner: str, name: str, path: str, request: Request):
    body = await request.json()
    message = body.get("message")
    if not message:
        return gh_error(422, "message is required")
    content_b64 = body.get("content")
    if content_b64 is None:
        return gh_error(422, "content is required")
    content = _b64decode(content_b64)
    repo = _ensure_repo(owner, name)
    repo_key = f"{owner}/{name}"
    branch = body.get("branch") or repo["default_branch"]
    sha = _synthetic_sha(repo_key)
    repo["files"][path] = {"content": content, "sha": sha}
    commit_sha = _synthetic_sha(repo_key)
    commit = {
        "sha": commit_sha,
        "commit": {
            "message": message,
            "author": {
                "name": (body.get("committer") or {}).get("name", "default-user"),
                "date": _now(),
            },
        },
        "files": [{"filename": path, "status": "modified", "sha": sha}],
    }
    repo["commits"].insert(0, commit)
    if branch in repo["branches"]:
        repo["branches"][branch]["sha"] = commit_sha
    status = 201 if not body.get("sha") else 200
    return JSONResponse(
        status_code=status,
        content={
            "content": {
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "sha": sha,
            },
            "commit": commit,
        },
    )


@app.post("/repos/{owner}/{name}/_push_files", status_code=201)
async def push_files(owner: str, name: str, request: Request):
    """MCP-shape batch push: body = {branch, message, files: [{path, content}, ...]}."""
    body = await request.json()
    repo = _ensure_repo(owner, name)
    repo_key = f"{owner}/{name}"
    branch = body.get("branch") or repo["default_branch"]
    message = body.get("message") or "Update files"
    files = body.get("files") or []
    if not files:
        return gh_error(422, "files list cannot be empty")
    file_entries = []
    for f in files:
        p = f.get("path")
        c = f.get("content", "")
        if not p:
            return gh_error(422, "each file requires a path")
        sha = _synthetic_sha(repo_key)
        repo["files"][p] = {"content": c, "sha": sha}
        file_entries.append({"filename": p, "status": "modified", "sha": sha})
    commit_sha = _synthetic_sha(repo_key)
    commit = {
        "sha": commit_sha,
        "commit": {"message": message, "author": {"name": "default-user", "date": _now()}},
        "files": file_entries,
    }
    repo["commits"].insert(0, commit)
    if branch in repo["branches"]:
        repo["branches"][branch]["sha"] = commit_sha
    return {"commit": commit, "branch": branch, "files_pushed": len(files)}


# --- branches -----------------------------------------------------------

@app.get("/repos/{owner}/{name}/branches")
def list_branches(owner: str, name: str):
    repo_key = f"{owner}/{name}"
    if repo_key not in STATE["repos"]:
        return gh_error(404, "Not Found")
    return [
        {"name": b["name"], "commit": {"sha": b["sha"]}, "protected": b.get("protected", False)}
        for b in STATE["repos"][repo_key]["branches"].values()
    ]


@app.post("/repos/{owner}/{name}/git/refs", status_code=201)
async def create_ref(owner: str, name: str, request: Request):
    body = await request.json()
    ref = body.get("ref", "")
    sha = body.get("sha")
    if not ref.startswith("refs/heads/"):
        return gh_error(422, "only refs/heads/<name> supported")
    branch_name = ref[len("refs/heads/"):]
    repo_key = f"{owner}/{name}"
    if repo_key not in STATE["repos"]:
        return gh_error(404, "Not Found")
    repo = STATE["repos"][repo_key]
    if branch_name in repo["branches"]:
        return gh_error(422, "Reference already exists")
    if not sha:
        sha = repo["branches"][repo["default_branch"]]["sha"]
    repo["branches"][branch_name] = {"name": branch_name, "sha": sha, "protected": False}
    return {"ref": ref, "object": {"sha": sha, "type": "commit"}}


@app.delete("/repos/{owner}/{name}/git/refs/heads/{branch}")
def delete_branch(owner: str, name: str, branch: str):
    repo_key = f"{owner}/{name}"
    if repo_key not in STATE["repos"]:
        return gh_error(404, "Not Found")
    repo = STATE["repos"][repo_key]
    if branch not in repo["branches"]:
        return gh_error(404, "Not Found")
    if branch == repo["default_branch"]:
        return gh_error(422, "Cannot delete default branch")
    del repo["branches"][branch]
    return Response(status_code=204)


# --- commits -------------------------------------------------------------

@app.get("/repos/{owner}/{name}/commits")
def list_commits(owner: str, name: str, sha: str | None = None,
                 path: str | None = None, per_page: int = 30, page: int = 1):
    repo_key = f"{owner}/{name}"
    if repo_key not in STATE["repos"]:
        return gh_error(404, "Not Found")
    commits = list(STATE["repos"][repo_key]["commits"])
    if path:
        commits = [c for c in commits if any(f["filename"] == path for f in c.get("files", []))]
    start = (page - 1) * per_page
    return commits[start:start + per_page]


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


# --- pull requests -------------------------------------------------------

def _pr_key(owner: str, name: str, number: int) -> str:
    return f"{owner}/{name}#{number}"


@app.post("/repos/{owner}/{name}/pulls", status_code=201)
async def create_pull_request(owner: str, name: str, request: Request):
    body = await request.json()
    title = body.get("title")
    head = body.get("head")
    base = body.get("base")
    if not title or not head or not base:
        return gh_error(422, "title, head, base are required")
    repo = _ensure_repo(owner, name)
    repo_key = f"{owner}/{name}"
    counters = STATE["_counters"]["pull_number_per_repo"]
    counters[repo_key] = counters.get(repo_key, 0) + 1
    number = counters[repo_key]
    head_sha = (
        repo["branches"].get(head, {}).get("sha")
        or _synthetic_sha(repo_key)
    )
    base_sha = repo["branches"].get(base, {}).get("sha", "")
    pr = {
        "number": number,
        "title": title,
        "body": body.get("body", "") or "",
        "state": "open",
        "merged": False,
        "draft": bool(body.get("draft")),
        "head": {"ref": head, "sha": head_sha},
        "base": {"ref": base, "sha": base_sha},
        "user": _user("default-user"),
        "created_at": _now(),
        "updated_at": _now(),
        "merged_at": None,
        "html_url": f"http://localhost/{repo_key}/pull/{number}",
        # inline children
        "_commits": [],
        "_reviews": [],
        "_files": [],
        "_comments": [],
        "_status": {"state": "pending", "statuses": []},
    }
    STATE["pulls"][_pr_key(owner, name, number)] = pr
    return _pr_view(pr)


def _pr_view(pr: dict) -> dict:
    """Strip private inline children before serializing."""
    return {k: v for k, v in pr.items() if not k.startswith("_")}


# Registered BEFORE `/pulls/{number}` so FastAPI's int validator doesn't 422
# on `1.diff`.
@app.get("/repos/{owner}/{name}/pulls/{number_diff}.diff")
def get_pull_request_diff(owner: str, name: str, number_diff: str):
    try:
        number = int(number_diff)
    except ValueError:
        return gh_error(404, "Not Found")
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    pr = STATE["pulls"][key]
    diff_lines = [f"diff --git a/{f.get('filename')} b/{f.get('filename')}" for f in pr["_files"]]
    return PlainTextResponse("\n".join(diff_lines) or "diff (empty)")


@app.get("/repos/{owner}/{name}/pulls")
def list_pull_requests(owner: str, name: str, state: str = "open",
                       head: str | None = None, base: str | None = None):
    repo_key = f"{owner}/{name}"
    out = []
    for key, pr in STATE["pulls"].items():
        if not key.startswith(f"{repo_key}#"):
            continue
        if state != "all" and pr["state"] != state:
            continue
        if head and pr["head"]["ref"] != head:
            continue
        if base and pr["base"]["ref"] != base:
            continue
        out.append(_pr_view(pr))
    return out


@app.get("/repos/{owner}/{name}/pulls/{number}")
def get_pull_request(owner: str, name: str, number: int):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    return _pr_view(STATE["pulls"][key])


@app.patch("/repos/{owner}/{name}/pulls/{number}")
async def update_pull_request(owner: str, name: str, number: int, request: Request):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    body = await request.json()
    pr = STATE["pulls"][key]
    for field in ("title", "body"):
        if body.get(field) is not None:
            pr[field] = body[field]
    if body.get("state") in ("open", "closed"):
        pr["state"] = body["state"]
    pr["updated_at"] = _now()
    return _pr_view(pr)


@app.put("/repos/{owner}/{name}/pulls/{number}/merge")
async def merge_pull_request(owner: str, name: str, number: int, request: Request):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    pr = STATE["pulls"][key]
    if pr["state"] != "open":
        return gh_error(409, "Pull Request is not mergeable")
    body = await request.json() if await request.body() else {}
    message = body.get("commit_message") or f"Merge pull request #{number}"
    repo_key = f"{owner}/{number}"  # not used for sha generation
    merge_sha = _synthetic_sha(f"{owner}/{name}")
    pr["state"] = "closed"
    pr["merged"] = True
    pr["merged_at"] = _now()
    pr["merge_commit_sha"] = merge_sha
    # Record a synthetic commit on the base branch.
    repo = STATE["repos"].get(f"{owner}/{name}")
    if repo:
        repo["commits"].insert(0, {
            "sha": merge_sha,
            "commit": {"message": message, "author": {"name": "default-user", "date": _now()}},
            "files": [],
        })
        if pr["base"]["ref"] in repo["branches"]:
            repo["branches"][pr["base"]["ref"]]["sha"] = merge_sha
    return {"sha": merge_sha, "merged": True, "message": "Pull Request successfully merged"}


@app.get("/repos/{owner}/{name}/pulls/{number}/commits")
def get_pull_request_commits(owner: str, name: str, number: int):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    return STATE["pulls"][key]["_commits"]


@app.get("/repos/{owner}/{name}/pulls/{number}/files")
def get_pull_request_files(owner: str, name: str, number: int):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    return STATE["pulls"][key]["_files"]


@app.get("/repos/{owner}/{name}/pulls/{number}/reviews")
def get_pull_request_reviews(owner: str, name: str, number: int):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    return STATE["pulls"][key]["_reviews"]


@app.post("/repos/{owner}/{name}/pulls/{number}/reviews", status_code=200)
async def create_pull_request_review(owner: str, name: str, number: int, request: Request):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    body = await request.json()
    event = body.get("event") or "COMMENT"
    if event not in ("APPROVE", "REQUEST_CHANGES", "COMMENT", "PENDING"):
        return gh_error(422, "invalid review event")
    STATE["_counters"]["review_id"] += 1
    rid = STATE["_counters"]["review_id"]
    state_map = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED",
                 "COMMENT": "COMMENTED", "PENDING": "PENDING"}
    review = {
        "id": rid,
        "user": _user("default-user"),
        "body": body.get("body", "") or "",
        "state": state_map[event],
        "submitted_at": _now(),
    }
    STATE["pulls"][key]["_reviews"].append(review)
    return review


@app.get("/repos/{owner}/{name}/pulls/{number}/comments")
def get_pull_request_comments(owner: str, name: str, number: int):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    return STATE["pulls"][key]["_comments"]


@app.post("/repos/{owner}/{name}/pulls/{number}/comments", status_code=201)
async def create_pull_request_comment(owner: str, name: str, number: int, request: Request):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    body = await request.json()
    STATE["_counters"]["comment_id"] += 1
    cid = STATE["_counters"]["comment_id"]
    comment = {
        "id": cid,
        "body": body.get("body", "") or "",
        "user": _user("default-user"),
        "created_at": _now(),
        "path": body.get("path"),
    }
    STATE["pulls"][key]["_comments"].append(comment)
    return comment


@app.put("/repos/{owner}/{name}/pulls/{number}/update-branch")
async def update_pull_request_branch(owner: str, name: str, number: int):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    pr = STATE["pulls"][key]
    pr["head"]["sha"] = _synthetic_sha(f"{owner}/{name}")
    pr["updated_at"] = _now()
    return {"message": "Updating pull request branch.", "url": pr["html_url"]}


@app.get("/repos/{owner}/{name}/pulls/{number}/status")
def get_pull_request_status(owner: str, name: str, number: int):
    key = _pr_key(owner, name, number)
    if key not in STATE["pulls"]:
        return gh_error(404, "Not Found")
    return STATE["pulls"][key]["_status"]


# `get_pull_request_diff` is registered earlier (before `/pulls/{number}` int
# route) so FastAPI's int-coercion doesn't 422 on `1.diff`.


# --- combined status -----------------------------------------------------

@app.get("/repos/{owner}/{name}/commits/{ref}/check-runs")
def get_check_runs(owner: str, name: str, ref: str):
    return {"total_count": 0, "check_runs": []}


# --- workflows -----------------------------------------------------------

@app.get("/repos/{owner}/{name}/actions/runs")
def list_workflow_runs(owner: str, name: str, status: str | None = None,
                       per_page: int = 30, page: int = 1):
    repo_key = f"{owner}/{name}"
    runs = [r for k, r in STATE["workflow_runs"].items() if k.startswith(f"{repo_key}#")]
    if status:
        runs = [r for r in runs if r.get("status") == status or r.get("conclusion") == status]
    start = (page - 1) * per_page
    items = runs[start:start + per_page]
    return {"total_count": len(runs), "workflow_runs": items}


@app.get("/repos/{owner}/{name}/actions/runs/{run_id}")
def get_workflow_run(owner: str, name: str, run_id: int):
    key = f"{owner}/{name}#{run_id}"
    if key not in STATE["workflow_runs"]:
        return gh_error(404, "Not Found")
    return STATE["workflow_runs"][key]


# --- search --------------------------------------------------------------

@app.get("/search/code")
def search_code(q: str = "", per_page: int = 30, page: int = 1):
    """Substring search over repo file contents."""
    needle = q.lower()
    items: list[dict] = []
    for repo_key, repo in STATE["repos"].items():
        for path, entry in repo["files"].items():
            if needle and needle not in entry["content"].lower() and needle not in path.lower():
                continue
            items.append({
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "sha": entry["sha"],
                "repository": {"full_name": repo_key, "id": repo["id"]},
                "html_url": f"http://localhost/{repo_key}/blob/main/{path}",
            })
    start = (page - 1) * per_page
    return {
        "total_count": len(items),
        "incomplete_results": False,
        "items": items[start:start + per_page],
    }


@app.get("/search/users")
def search_users(q: str = "", per_page: int = 30, page: int = 1):
    needle = q.lower()
    items = [u for u in STATE["users"].values() if needle in u["login"].lower()]
    start = (page - 1) * per_page
    return {
        "total_count": len(items),
        "incomplete_results": False,
        "items": items[start:start + per_page],
    }


@app.get("/search/issues")
def search_issues(q: str = "", per_page: int = 30, page: int = 1):
    """Substring search over issue title + body."""
    needle = q.lower()
    items: list[dict] = []
    for key, issue in STATE["issues"].items():
        repo_key = key.split("#")[0]
        text = f"{issue['title']} {issue.get('body', '')}".lower()
        if needle and needle not in text:
            continue
        item = dict(issue)
        item["repository_url"] = f"http://localhost/repos/{repo_key}"
        items.append(item)
    start = (page - 1) * per_page
    return {
        "total_count": len(items),
        "incomplete_results": False,
        "items": items[start:start + per_page],
    }


# --- MCP transport -------------------------------------------------------
# Mount the GitHub MCP server at /mcp on this same FastAPI app so REST and
# MCP share the same STATE dict (Phase 6, MCP-01/MCP-02).

from checkpoint.mcp_servers.github_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
