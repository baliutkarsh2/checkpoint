"""Linear twin: stateful in-memory clone of the Linear REST/GraphQL API.

Implements the most-used Linear surfaces via a REST-ish API that mirrors
the shapes returned by the official Linear MCP server and GraphQL API:

  Issues       — create, list, get, update, close, add comments
  Teams        — list, get
  Projects     — list, get, create
  Users        — list, get
  Labels       — list, create
  WorkflowStates — list, get
  Cycles       — list, get

Introspection at /_health, /_trace, /_state, /_reset, /_seed/<name>,
/_seed-file, /_config.

Linear IDs use the standard UUID format. Bootstrap token mimics Linear
API key format: lin_api_<hex>.
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

app = FastAPI(title="checkpoint linear twin")

DEFAULT_BOOTSTRAP_TOKEN = "lin_api_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0011"
INTROSPECTION_PREFIX = "/_"

SEEDS_DIR = Path(__file__).parent / "linear_seeds"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _fresh_state() -> dict:
    org_id = "org-checkpoint-test"
    default_team_id = "team-engineering"
    return {
        "organization": {
            "id": org_id,
            "name": "Checkpoint Test Org",
            "urlKey": "checkpoint",
            "createdAt": _now(),
        },
        "teams": {
            default_team_id: {
                "id": default_team_id,
                "name": "Engineering",
                "key": "ENG",
                "description": "Engineering team",
                "createdAt": _now(),
            },
        },
        "workflow_states": {
            "state-backlog": {
                "id": "state-backlog",
                "teamId": default_team_id,
                "name": "Backlog",
                "type": "backlog",
                "color": "#95A5A6",
                "position": 0.0,
            },
            "state-todo": {
                "id": "state-todo",
                "teamId": default_team_id,
                "name": "Todo",
                "type": "unstarted",
                "color": "#E2E2E2",
                "position": 1.0,
            },
            "state-in-progress": {
                "id": "state-in-progress",
                "teamId": default_team_id,
                "name": "In Progress",
                "type": "started",
                "color": "#F2C94C",
                "position": 2.0,
            },
            "state-done": {
                "id": "state-done",
                "teamId": default_team_id,
                "name": "Done",
                "type": "completed",
                "color": "#5E6AD2",
                "position": 3.0,
            },
            "state-cancelled": {
                "id": "state-cancelled",
                "teamId": default_team_id,
                "name": "Cancelled",
                "type": "cancelled",
                "color": "#95A5A6",
                "position": 4.0,
            },
        },
        "projects": {},      # project_id -> project dict
        "cycles": {},        # cycle_id -> cycle dict
        "users": {
            "user-default": {
                "id": "user-default",
                "name": "Default User",
                "email": "user@checkpoint.test",
                "displayName": "Default User",
                "active": True,
                "admin": True,
                "createdAt": _now(),
            },
        },
        "labels": {},        # label_id -> label dict
        "issues": {},        # issue_id -> issue dict
        "comments": {},      # comment_id -> comment dict
        "_counters": {
            "issue_seq": {},    # team_key -> int (for identifier like ENG-1)
            "requests": 0,
        },
        "_config": {
            "rate_limit": None,
        },
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- helpers -----------------------------------------------------------------

def linear_error(status: int, message: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _bootstrap_token() -> str:
    return os.environ.get("LINEAR_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    for prefix in ("Bearer ", "bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return auth_header.strip()


def _next_issue_id(team_key: str) -> str:
    seq = STATE["_counters"]["issue_seq"]
    seq[team_key] = seq.get(team_key, 0) + 1
    return f"{team_key}-{seq[team_key]}"


def _default_state_id(team_id: str) -> str:
    for sid, s in STATE["workflow_states"].items():
        if s.get("teamId") == team_id and s.get("type") == "backlog":
            return sid
    for sid in STATE["workflow_states"]:
        return sid
    return "state-backlog"


def _team_key(team_id: str) -> str:
    t = STATE["teams"].get(team_id) or {}
    return t.get("key", "ENG")


# --- middlewares -------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(INTROSPECTION_PREFIX) or path.startswith("/mcp"):
        return await call_next(request)
    token = _extract_token(request.headers.get("authorization"))
    if token != _bootstrap_token():
        return linear_error(401, "Invalid API key")
    STATE["_counters"]["requests"] += 1
    rl = STATE["_config"].get("rate_limit")
    if rl is not None and STATE["_counters"]["requests"] > rl:
        return JSONResponse(
            status_code=429,
            content={"error": "Too Many Requests"},
            headers={"Retry-After": "60"},
        )
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


# --- organization ------------------------------------------------------------

@app.get("/v1/organization")
def get_organization():
    return STATE["organization"]


# --- teams -------------------------------------------------------------------

@app.get("/v1/teams")
def list_teams(includeArchived: bool = False):
    teams = list(STATE["teams"].values())
    return {"nodes": teams, "pageInfo": {"hasNextPage": False}}


@app.get("/v1/teams/{team_id}")
def get_team(team_id: str):
    if team_id not in STATE["teams"]:
        return linear_error(404, f"Team {team_id!r} not found")
    return STATE["teams"][team_id]


@app.post("/v1/teams", status_code=201)
async def create_team(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return linear_error(400, "name is required")
    key = (body.get("key") or name[:3].upper()).upper()
    tid = _uid()
    team = {
        "id": tid,
        "name": name,
        "key": key,
        "description": body.get("description", ""),
        "createdAt": _now(),
    }
    STATE["teams"][tid] = team
    # Add default workflow states for new team.
    for sname, stype, color, pos in [
        ("Backlog", "backlog", "#95A5A6", 0.0),
        ("Todo", "unstarted", "#E2E2E2", 1.0),
        ("In Progress", "started", "#F2C94C", 2.0),
        ("Done", "completed", "#5E6AD2", 3.0),
        ("Cancelled", "cancelled", "#95A5A6", 4.0),
    ]:
        sid = _uid()
        STATE["workflow_states"][sid] = {
            "id": sid, "teamId": tid, "name": sname, "type": stype,
            "color": color, "position": pos,
        }
    return team


# --- workflow states ---------------------------------------------------------

@app.get("/v1/teams/{team_id}/states")
def list_workflow_states(team_id: str):
    states = [s for s in STATE["workflow_states"].values() if s.get("teamId") == team_id]
    states.sort(key=lambda s: s.get("position", 0.0))
    return {"nodes": states, "pageInfo": {"hasNextPage": False}}


@app.get("/v1/workflow-states")
def list_all_workflow_states(teamId: str | None = None):
    states = list(STATE["workflow_states"].values())
    if teamId:
        states = [s for s in states if s.get("teamId") == teamId]
    states.sort(key=lambda s: s.get("position", 0.0))
    return {"nodes": states, "pageInfo": {"hasNextPage": False}}


# --- projects ----------------------------------------------------------------

@app.get("/v1/projects")
def list_projects(teamId: str | None = None):
    projects = list(STATE["projects"].values())
    if teamId:
        projects = [p for p in projects if teamId in (p.get("teamIds") or [])]
    return {"nodes": projects, "pageInfo": {"hasNextPage": False}}


@app.post("/v1/projects", status_code=201)
async def create_project(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return linear_error(400, "name is required")
    pid = _uid()
    project = {
        "id": pid,
        "name": name,
        "description": body.get("description", ""),
        "state": body.get("state", "planned"),
        "teamIds": body.get("teamIds") or [],
        "createdAt": _now(),
        "updatedAt": _now(),
        "startDate": body.get("startDate"),
        "targetDate": body.get("targetDate"),
        "progress": 0.0,
        "issueCount": 0,
    }
    STATE["projects"][pid] = project
    return project


@app.get("/v1/projects/{project_id}")
def get_project(project_id: str):
    if project_id not in STATE["projects"]:
        return linear_error(404, f"Project {project_id!r} not found")
    return STATE["projects"][project_id]


@app.patch("/v1/projects/{project_id}")
async def update_project(project_id: str, request: Request):
    if project_id not in STATE["projects"]:
        return linear_error(404, f"Project {project_id!r} not found")
    body = await request.json()
    proj = STATE["projects"][project_id]
    for field in ("name", "description", "state", "targetDate", "startDate"):
        if field in body:
            proj[field] = body[field]
    proj["updatedAt"] = _now()
    return proj


# --- cycles ------------------------------------------------------------------

@app.get("/v1/cycles")
def list_cycles(teamId: str | None = None):
    cycles = list(STATE["cycles"].values())
    if teamId:
        cycles = [c for c in cycles if c.get("teamId") == teamId]
    return {"nodes": cycles, "pageInfo": {"hasNextPage": False}}


@app.post("/v1/cycles", status_code=201)
async def create_cycle(request: Request):
    body = await request.json()
    team_id = body.get("teamId")
    if not team_id or team_id not in STATE["teams"]:
        return linear_error(400, "valid teamId is required")
    cid = _uid()
    cycle = {
        "id": cid,
        "teamId": team_id,
        "number": len([c for c in STATE["cycles"].values() if c.get("teamId") == team_id]) + 1,
        "name": body.get("name"),
        "startsAt": body.get("startsAt"),
        "endsAt": body.get("endsAt"),
        "description": body.get("description", ""),
        "createdAt": _now(),
        "issueCount": 0,
    }
    STATE["cycles"][cid] = cycle
    return cycle


# --- labels ------------------------------------------------------------------

@app.get("/v1/labels")
def list_labels(teamId: str | None = None):
    labels = list(STATE["labels"].values())
    if teamId:
        labels = [lab for lab in labels if lab.get("teamId") == teamId or not lab.get("teamId")]
    return {"nodes": labels, "pageInfo": {"hasNextPage": False}}


@app.post("/v1/labels", status_code=201)
async def create_label(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return linear_error(400, "name is required")
    lid = _uid()
    label = {
        "id": lid,
        "name": name,
        "color": body.get("color", "#B0B0B0"),
        "teamId": body.get("teamId"),
        "createdAt": _now(),
    }
    STATE["labels"][lid] = label
    return label


# --- users -------------------------------------------------------------------

@app.get("/v1/users")
def list_users():
    users = list(STATE["users"].values())
    return {"nodes": users, "pageInfo": {"hasNextPage": False}}


@app.get("/v1/users/me")
def get_me():
    user = next(iter(STATE["users"].values()), None)
    if not user:
        return linear_error(404, "No users in state")
    return user


@app.get("/v1/users/{user_id}")
def get_user(user_id: str):
    u = STATE["users"].get(user_id)
    if not u:
        # Try by email
        u = next((x for x in STATE["users"].values() if x.get("email") == user_id), None)
    if not u:
        return linear_error(404, f"User {user_id!r} not found")
    return u


# --- issues ------------------------------------------------------------------

@app.post("/v1/issues", status_code=201)
async def create_issue(request: Request):
    body = await request.json()
    title = body.get("title")
    if not title:
        return linear_error(400, "title is required")
    team_id = body.get("teamId") or next(iter(STATE["teams"]), "team-engineering")
    if team_id not in STATE["teams"]:
        return linear_error(400, f"Team {team_id!r} not found")

    state_id = body.get("stateId") or _default_state_id(team_id)
    assignee_id = body.get("assigneeId")
    iid = _uid()
    team_key = _team_key(team_id)
    identifier = _next_issue_id(team_key)

    label_ids = body.get("labelIds") or []
    priority = body.get("priority", 0)

    issue = {
        "id": iid,
        "identifier": identifier,
        "title": title,
        "description": body.get("description", ""),
        "priority": priority,
        "priorityLabel": _priority_label(priority),
        "state": STATE["workflow_states"].get(state_id, {"name": "Backlog", "type": "backlog"}),
        "stateId": state_id,
        "team": STATE["teams"].get(team_id, {}),
        "teamId": team_id,
        "assignee": STATE["users"].get(assignee_id) if assignee_id else None,
        "assigneeId": assignee_id,
        "labels": [STATE["labels"][lid] for lid in label_ids if lid in STATE["labels"]],
        "labelIds": label_ids,
        "projectId": body.get("projectId"),
        "cycleId": body.get("cycleId"),
        "estimate": body.get("estimate"),
        "dueDate": body.get("dueDate"),
        "createdAt": _now(),
        "updatedAt": _now(),
        "completedAt": None,
        "canceledAt": None,
        "url": f"https://linear.app/checkpoint/issue/{identifier}",
        "commentCount": 0,
    }
    STATE["issues"][iid] = issue

    # Bump project issue count.
    pid = body.get("projectId")
    if pid and pid in STATE["projects"]:
        STATE["projects"][pid]["issueCount"] += 1

    return issue


def _priority_label(p: int) -> str:
    return {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}.get(p, "No priority")


@app.get("/v1/issues")
def list_issues(
    teamId: str | None = None,
    stateId: str | None = None,
    assigneeId: str | None = None,
    projectId: str | None = None,
    labelId: str | None = None,
    priority: int | None = None,
    first: int = 50,
    after: str | None = None,
):
    issues = list(STATE["issues"].values())
    if teamId:
        issues = [i for i in issues if i.get("teamId") == teamId]
    if stateId:
        issues = [i for i in issues if i.get("stateId") == stateId]
    if assigneeId:
        issues = [i for i in issues if i.get("assigneeId") == assigneeId]
    if projectId:
        issues = [i for i in issues if i.get("projectId") == projectId]
    if labelId:
        issues = [i for i in issues if labelId in (i.get("labelIds") or [])]
    if priority is not None:
        issues = [i for i in issues if i.get("priority") == priority]
    # Cursor: simple offset by ID.
    if after:
        ids = [i["id"] for i in issues]
        try:
            idx = ids.index(after)
            issues = issues[idx + 1:]
        except ValueError:
            pass
    page = issues[:first]
    has_next = len(issues) > first
    return {
        "nodes": page,
        "pageInfo": {
            "hasNextPage": has_next,
            "endCursor": page[-1]["id"] if page else None,
        },
    }


@app.get("/v1/issues/{issue_id}")
def get_issue(issue_id: str):
    # Allow lookup by identifier (ENG-1) or UUID.
    issue = STATE["issues"].get(issue_id)
    if not issue:
        issue = next((i for i in STATE["issues"].values() if i.get("identifier") == issue_id), None)
    if not issue:
        return linear_error(404, f"Issue {issue_id!r} not found")
    return issue


@app.patch("/v1/issues/{issue_id}")
async def update_issue(issue_id: str, request: Request):
    issue = STATE["issues"].get(issue_id)
    if not issue:
        issue = next((i for i in STATE["issues"].values() if i.get("identifier") == issue_id), None)
    if not issue:
        return linear_error(404, f"Issue {issue_id!r} not found")
    body = await request.json()
    for field in ("title", "description", "priority", "estimate", "dueDate"):
        if field in body:
            issue[field] = body[field]
    if "priority" in body:
        issue["priorityLabel"] = _priority_label(body["priority"])
    if "stateId" in body:
        sid = body["stateId"]
        issue["stateId"] = sid
        issue["state"] = STATE["workflow_states"].get(sid, {"name": "Unknown"})
        s_type = issue["state"].get("type", "")
        if s_type == "completed":
            issue["completedAt"] = _now()
        elif s_type == "cancelled":
            issue["canceledAt"] = _now()
    if "assigneeId" in body:
        aid = body["assigneeId"]
        issue["assigneeId"] = aid
        issue["assignee"] = STATE["users"].get(aid) if aid else None
    if "labelIds" in body:
        lids = body["labelIds"] or []
        issue["labelIds"] = lids
        issue["labels"] = [STATE["labels"][lid] for lid in lids if lid in STATE["labels"]]
    if "projectId" in body:
        issue["projectId"] = body["projectId"]
    if "cycleId" in body:
        issue["cycleId"] = body["cycleId"]
    issue["updatedAt"] = _now()
    return issue


@app.delete("/v1/issues/{issue_id}")
def archive_issue(issue_id: str):
    issue = STATE["issues"].get(issue_id)
    if not issue:
        issue = next((i for i in STATE["issues"].values() if i.get("identifier") == issue_id), None)
    if not issue:
        return linear_error(404, f"Issue {issue_id!r} not found")
    issue["archivedAt"] = _now()
    return {"success": True}


# --- comments ----------------------------------------------------------------

@app.post("/v1/issues/{issue_id}/comments", status_code=201)
async def add_comment(issue_id: str, request: Request):
    issue = STATE["issues"].get(issue_id)
    if not issue:
        issue = next((i for i in STATE["issues"].values() if i.get("identifier") == issue_id), None)
    if not issue:
        return linear_error(404, f"Issue {issue_id!r} not found")
    body = await request.json()
    body_text = body.get("body")
    if not body_text:
        return linear_error(400, "body is required")
    cid = _uid()
    comment = {
        "id": cid,
        "body": body_text,
        "issueId": issue["id"],
        "userId": body.get("userId", "user-default"),
        "user": STATE["users"].get(body.get("userId", "user-default")),
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    STATE["comments"][cid] = comment
    issue["commentCount"] = issue.get("commentCount", 0) + 1
    return comment


@app.get("/v1/issues/{issue_id}/comments")
def list_comments(issue_id: str):
    issue = STATE["issues"].get(issue_id)
    if not issue:
        issue = next((i for i in STATE["issues"].values() if i.get("identifier") == issue_id), None)
    if not issue:
        return linear_error(404, f"Issue {issue_id!r} not found")
    iid = issue["id"]
    comments = [c for c in STATE["comments"].values() if c.get("issueId") == iid]
    comments.sort(key=lambda c: c["createdAt"])
    return {"nodes": comments, "pageInfo": {"hasNextPage": False}}


# --- search ------------------------------------------------------------------

@app.get("/v1/search/issues")
def search_issues(query: str = "", first: int = 50):
    q = query.lower()
    results = []
    for issue in STATE["issues"].values():
        text = f"{issue.get('title', '')} {issue.get('description', '')} {issue.get('identifier', '')}".lower()
        if not q or q in text:
            results.append(issue)
    return {"nodes": results[:first], "pageInfo": {"hasNextPage": False}}


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.linear_mcp import mount_on as _mount_mcp  # noqa: E402

_mount_mcp(app)
