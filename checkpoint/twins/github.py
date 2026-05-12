"""GitHub twin: stateful in-memory clone of a slice of the GitHub REST API.

Covers: repos, issues (CRUD + state changes), issue comments, labels.
Trace of every external request is exposed at GET /_trace.
Full state snapshot at GET /_state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response

app = FastAPI(title="checkpoint github twin")

STATE: dict = {
    "repos": {},     # "owner/name" -> repo dict
    "issues": {},    # "owner/name#number" -> issue dict
    "comments": {},  # comment_id (str) -> comment dict
    "labels": {},    # "owner/name/label_name" -> label dict
    "_counters": {"issue_number_per_repo": {}, "comment_id": 0, "repo_id": 0},
}

TRACE: list[dict] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/_"):
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


def _user(login: str) -> dict:
    return {"login": login, "id": 1, "type": "User"}


def _make_repo(owner: str, name: str) -> dict:
    STATE["_counters"]["repo_id"] += 1
    return {
        "id": STATE["_counters"]["repo_id"],
        "name": name,
        "full_name": f"{owner}/{name}",
        "owner": _user(owner),
        "default_branch": "main",
        "private": False,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _ensure_repo(owner: str, name: str) -> dict:
    key = f"{owner}/{name}"
    if key not in STATE["repos"]:
        STATE["repos"][key] = _make_repo(owner, name)
    return STATE["repos"][key]


# --- introspection endpoints (not traced) ---

@app.get("/_health")
def health():
    return {"ok": True}


@app.get("/_trace")
def get_trace():
    return TRACE


@app.get("/_state")
def get_state():
    return {
        "repos": STATE["repos"],
        "issues": STATE["issues"],
        "comments": STATE["comments"],
        "labels": STATE["labels"],
    }


@app.post("/_reset")
def reset():
    STATE["repos"].clear()
    STATE["issues"].clear()
    STATE["comments"].clear()
    STATE["labels"].clear()
    STATE["_counters"] = {"issue_number_per_repo": {}, "comment_id": 0, "repo_id": 0}
    TRACE.clear()
    return {"ok": True}


# --- repos ---

@app.post("/user/repos", status_code=201)
async def create_user_repo(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        raise HTTPException(422, {"message": "name is required"})
    owner = "default-user"
    key = f"{owner}/{name}"
    if key in STATE["repos"]:
        raise HTTPException(422, {"message": "Repository already exists"})
    repo = _make_repo(owner, name)
    STATE["repos"][key] = repo
    return repo


@app.get("/repos/{owner}/{name}")
def get_repo(owner: str, name: str):
    key = f"{owner}/{name}"
    if key not in STATE["repos"]:
        raise HTTPException(404, {"message": "Not Found"})
    return STATE["repos"][key]


# --- issues ---

@app.post("/repos/{owner}/{name}/issues", status_code=201)
async def create_issue(owner: str, name: str, request: Request):
    body = await request.json()
    title = body.get("title")
    if not title:
        raise HTTPException(422, {"message": "title is required"})
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
        raise HTTPException(404, {"message": "Not Found"})
    return STATE["issues"][key]


@app.patch("/repos/{owner}/{name}/issues/{number}")
async def update_issue(owner: str, name: str, number: int, request: Request):
    key = f"{owner}/{name}#{number}"
    if key not in STATE["issues"]:
        raise HTTPException(404, {"message": "Not Found"})
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


# --- comments ---

@app.post("/repos/{owner}/{name}/issues/{number}/comments", status_code=201)
async def add_comment(owner: str, name: str, number: int, request: Request):
    key = f"{owner}/{name}#{number}"
    if key not in STATE["issues"]:
        raise HTTPException(404, {"message": "Not Found"})
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


# --- labels ---

@app.get("/repos/{owner}/{name}/labels")
def list_labels(owner: str, name: str):
    repo_key = f"{owner}/{name}"
    return [v for k, v in STATE["labels"].items() if k.startswith(f"{repo_key}/")]


@app.post("/repos/{owner}/{name}/labels", status_code=201)
async def create_label(owner: str, name: str, request: Request):
    body = await request.json()
    label_name = body.get("name")
    if not label_name:
        raise HTTPException(422, {"message": "name required"})
    repo_key = f"{owner}/{name}"
    _ensure_repo(owner, name)
    key = f"{repo_key}/{label_name}"
    if key in STATE["labels"]:
        raise HTTPException(422, {"message": "already_exists"})
    label = {"name": label_name, "color": body.get("color", "ededed")}
    STATE["labels"][key] = label
    return label
