"""Linear twin: a stateful, in-memory Linear API.

Linear is GraphQL-only, so the twin's real surface is ``POST /graphql``
(:mod:`checkpoint.twins.linear_graphql`), served from Linear's own published
schema: @linear/sdk, `gql`, and hand-rolled clients all work against it
unchanged.

A REST-ish ``/v1/*`` surface sits beside it for the twin's MCP server, whose
tools predate the GraphQL surface and call it in-process. Both write through
:mod:`checkpoint.twins.linear_store`, so state is identical whichever one an
agent uses. The control plane and fault model come from
:mod:`checkpoint.twins.kit`.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from checkpoint.fake_credentials import FAKE_LINEAR_TOKEN
from checkpoint.twins import kit, linear_graphql
from checkpoint.twins import linear_store as store

app = FastAPI(title="checkpoint linear twin")

DEFAULT_BOOTSTRAP_TOKEN = FAKE_LINEAR_TOKEN

SEEDS_DIR = Path(__file__).parent / "linear_seeds"
GRAPHQL_PATH = "/graphql"

STATE: dict = store.fresh_state()
TRACE: list[dict] = []

# The kit hands the error factory no request, but faults must be shaped for the
# surface that is being called — a GraphQL error envelope on /graphql, a REST
# error body on /v1/*. The authenticator runs first for every request, so it
# records the surface here for the factory that follows it.
_on_graphql: ContextVar[bool] = ContextVar("linear_on_graphql", default=False)


# --- helpers -----------------------------------------------------------------

def linear_error(status: int, message: str, **extra: Any) -> JSONResponse:
    """The REST surface's error body."""
    body: dict[str, Any] = {"error": message}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def graphql_error(status: int, shape: tuple[str, str, int], message: str,
                  user_message: str | None = None, **kwargs: Any) -> JSONResponse:
    type_, code, _status = shape
    return JSONResponse(status_code=status, content=linear_graphql.error_body(
        message, type_=type_, code=code, status=status, user_message=user_message), **kwargs)


def _bootstrap_token() -> str:
    return os.environ.get("LINEAR_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _extract_token(auth_header: str | None) -> str | None:
    """Linear takes API keys bare and OAuth tokens with a ``Bearer`` prefix."""
    if not auth_header:
        return None
    prefix, _, rest = auth_header.partition(" ")
    if prefix.lower() == "bearer":
        return rest.strip()
    return auth_header.strip()


# Both carry a user_message; the REST surface reports them the same way.
STORE_ERRORS = (store.LinearInvalidInput, store.LinearNotFound)


def _store_error(exc: store.LinearInvalidInput | store.LinearNotFound) -> JSONResponse:
    """A rejection from the store, in the REST surface's error shape."""
    return linear_error(400, str(exc), message=exc.user_message)


def _issue_or_404(issue_id: str) -> tuple[dict | None, JSONResponse | None]:
    issue = store.find_issue(STATE, issue_id)
    if issue is None:
        return None, linear_error(404, f"Issue {issue_id!r} not found")
    return issue, None


def _nodes(records: list[dict], first: int | None = None) -> dict:
    """The connection-ish envelope the MCP tools read."""
    page = records if first is None else records[:first]
    return {"nodes": page, "pageInfo": {
        "hasNextPage": first is not None and len(records) > first,
        "endCursor": page[-1]["id"] if page else None}}


# --- runtime: auth, faults, trace, control plane ----------------------------

def _authenticate(request: Request) -> Response | None:
    on_graphql = request.url.path.rstrip("/") == GRAPHQL_PATH
    _on_graphql.set(on_graphql)
    token = _extract_token(request.headers.get("authorization"))
    if token and not (TWIN.config.get("strict_auth") and token != _bootstrap_token()):
        return None
    message = "Authentication required, not authenticated" if not token else "Invalid API key"
    if on_graphql:
        return graphql_error(401, linear_graphql.AUTH, message,
                             "You need to authenticate to access this operation.")
    return linear_error(401, message)


_FAULT_SHAPES = {
    "forbidden": (linear_graphql.FORBIDDEN, "You do not have access to this resource."),
    "read_only": (linear_graphql.FORBIDDEN, "This workspace is read-only."),
    "rate_limited": (linear_graphql.RATELIMITED, "You have exceeded the rate limit."),
}

# What Linear returns alongside a rate-limited response (API-key defaults).
_RATE_LIMIT_HEADERS = {
    "Retry-After": "60",
    "X-RateLimit-Requests-Limit": "2500",
    "X-RateLimit-Requests-Remaining": "0",
}


def _error(kind: str, status: int, message: str) -> Response:
    if not _on_graphql.get():
        if kind == "rate_limited":
            return linear_error(429, "Rate limit exceeded", headers=_RATE_LIMIT_HEADERS)
        return linear_error(status, message)
    shape, user_message = _FAULT_SHAPES.get(kind, (linear_graphql.INTERNAL, message))
    if kind == "rate_limited":
        # Linear reports rate limiting as a GraphQL error on HTTP 400, not 429.
        return graphql_error(400, shape, "Rate limit exceeded", user_message,
                             headers=_RATE_LIMIT_HEADERS)
    return graphql_error(shape[2] if kind in _FAULT_SHAPES else status, shape,
                         message, user_message)


def _classify(method: str, path: str, body: Any) -> tuple[kit.Op, str] | None:
    """Name the resource every call touched, on either surface."""
    if path.rstrip("/") == GRAPHQL_PATH:
        query = body.get("query") if isinstance(body, dict) else None
        if not query:
            return "other", "graphql"
        # An unparseable document names no resource, but it is still a call.
        return linear_graphql.classify(query, body.get("operationName")) or ("other", "graphql")
    segments = [s for s in path.split("/") if s and s != "v1"]
    if not segments:
        return None
    resource = _REST_RESOURCES.get(segments[-1]) or _REST_RESOURCES.get(segments[0])
    if resource is None:
        return None
    op = {"GET": "read", "POST": "create", "PATCH": "update",
          "PUT": "update", "DELETE": "delete"}.get(method.upper(), "other")
    return op, resource


_REST_RESOURCES = {
    "issues": "issues", "comments": "comments", "teams": "teams", "users": "users",
    "labels": "labels", "projects": "projects", "cycles": "cycles",
    "workflow-states": "workflow_states", "states": "workflow_states",
    "organization": "organization", "search": "issues",
}


def _failed(status: int, body: Any) -> bool:
    # GraphQL reports application errors on HTTP 200 with an `errors` array.
    return status >= 400 or bool(isinstance(body, dict) and body.get("errors"))


TWIN = kit.install(app, kit.Twin(
    name="linear",
    state=STATE,
    trace=TRACE,
    fresh_state=store.fresh_state,
    seeds_dir=SEEDS_DIR,
    error=_error,
    authenticate=_authenticate,
    after_seed=store.normalize,
    views=store.views,
    classify=_classify,
    failed=_failed,
))


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """Unknown routes answer in Linear's shape, not FastAPI's ``{"detail": ...}``."""
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    if request.url.path.rstrip("/") == GRAPHQL_PATH:
        return graphql_error(exc.status_code, linear_graphql.INVALID_INPUT, message)
    return linear_error(exc.status_code, message)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    return linear_error(400, "Argument Validation Error", details=exc.errors())


# --- GraphQL -----------------------------------------------------------------

@app.post(GRAPHQL_PATH)
async def graphql_endpoint(request: Request) -> Response:
    """Linear's only public endpoint."""
    try:
        payload = await request.json()
    except ValueError:
        return graphql_error(400, linear_graphql.INVALID_INPUT, "Request body is not valid JSON.")
    status, body = linear_graphql.execute(STATE, payload)
    return JSONResponse(status_code=status, content=body)


# --- organization ------------------------------------------------------------

@app.get("/v1/organization")
def get_organization():
    return STATE["organization"]


# --- teams -------------------------------------------------------------------

@app.get("/v1/teams")
def list_teams(includeArchived: bool = False):
    teams = [t for t in STATE["teams"].values() if includeArchived or not t.get("archivedAt")]
    return _nodes(teams)


@app.get("/v1/teams/{team_id}")
def get_team(team_id: str):
    team = STATE["teams"].get(team_id)
    if team is None:
        return linear_error(404, f"Team {team_id!r} not found")
    return team


@app.post("/v1/teams", status_code=201)
async def create_team(request: Request):
    body = await request.json()
    try:
        return store.create_team(STATE, body)
    except STORE_ERRORS as exc:
        return _store_error(exc)


# --- workflow states ---------------------------------------------------------

@app.get("/v1/teams/{team_id}/states")
def list_workflow_states(team_id: str):
    return _nodes(store.team_states(STATE, team_id))


@app.get("/v1/workflow-states")
def list_all_workflow_states(teamId: str | None = None):
    states = list(STATE["workflow_states"].values())
    if teamId:
        states = [s for s in states if s.get("teamId") == teamId]
    states.sort(key=lambda s: s.get("position", 0.0))
    return _nodes(states)


# --- projects ----------------------------------------------------------------

@app.get("/v1/projects")
def list_projects(teamId: str | None = None, includeArchived: bool = False):
    projects = [p for p in STATE["projects"].values()
                if includeArchived or not p.get("archivedAt")]
    if teamId:
        projects = [p for p in projects if teamId in (p.get("teamIds") or [])]
    return _nodes(projects)


@app.post("/v1/projects", status_code=201)
async def create_project(request: Request):
    body = await request.json()
    try:
        return store.create_project(STATE, body)
    except STORE_ERRORS as exc:
        return _store_error(exc)


@app.get("/v1/projects/{project_id}")
def get_project(project_id: str):
    project = STATE["projects"].get(project_id)
    if project is None:
        return linear_error(404, f"Project {project_id!r} not found")
    return project


@app.patch("/v1/projects/{project_id}")
async def update_project(project_id: str, request: Request):
    project = STATE["projects"].get(project_id)
    if project is None:
        return linear_error(404, f"Project {project_id!r} not found")
    try:
        return store.update_project(STATE, project, await request.json())
    except STORE_ERRORS as exc:
        return _store_error(exc)


# --- cycles ------------------------------------------------------------------

@app.get("/v1/cycles")
def list_cycles(teamId: str | None = None):
    cycles = list(STATE["cycles"].values())
    if teamId:
        cycles = [c for c in cycles if c.get("teamId") == teamId]
    return _nodes(cycles)


@app.post("/v1/cycles", status_code=201)
async def create_cycle(request: Request):
    body = await request.json()
    try:
        return store.create_cycle(STATE, body)
    except STORE_ERRORS as exc:
        return _store_error(exc)


# --- labels ------------------------------------------------------------------

@app.get("/v1/labels")
def list_labels(teamId: str | None = None):
    labels = list(STATE["labels"].values())
    if teamId:
        labels = [lab for lab in labels if lab.get("teamId") in (teamId, None)]
    return _nodes(labels)


@app.post("/v1/labels", status_code=201)
async def create_label(request: Request):
    body = await request.json()
    try:
        return store.create_label(STATE, body)
    except STORE_ERRORS as exc:
        return _store_error(exc)


# --- users -------------------------------------------------------------------

@app.get("/v1/users")
def list_users():
    return _nodes(list(STATE["users"].values()))


@app.get("/v1/users/me")
def get_me():
    user = store.viewer(STATE)
    if user is None:
        return linear_error(404, "No users in state")
    return user


@app.get("/v1/users/{user_id}")
def get_user(user_id: str):
    user = STATE["users"].get(user_id) or next(
        (u for u in STATE["users"].values() if u.get("email") == user_id), None)
    if user is None:
        return linear_error(404, f"User {user_id!r} not found")
    return user


# --- issues ------------------------------------------------------------------

@app.post("/v1/issues", status_code=201)
async def create_issue(request: Request):
    body = await request.json()
    if not body.get("title"):
        return linear_error(400, "title is required")
    try:
        return store.create_issue(STATE, body)
    except STORE_ERRORS as exc:
        return _store_error(exc)


@app.get("/v1/issues")
def list_issues(
    teamId: str | None = None,
    stateId: str | None = None,
    assigneeId: str | None = None,
    projectId: str | None = None,
    cycleId: str | None = None,
    labelId: str | None = None,
    priority: int | None = None,
    includeArchived: bool = False,
    first: int = 50,
    after: str | None = None,
):
    issues = [i for i in STATE["issues"].values()
              if includeArchived or not i.get("archivedAt")]
    for field, value in (("teamId", teamId), ("stateId", stateId), ("assigneeId", assigneeId),
                         ("projectId", projectId), ("cycleId", cycleId)):
        if value:
            issues = [i for i in issues if i.get(field) == value]
    if labelId:
        issues = [i for i in issues if labelId in (i.get("labelIds") or [])]
    if priority is not None:
        issues = [i for i in issues if i.get("priority") == priority]
    if after:
        ids = [i["id"] for i in issues]
        if after in ids:
            issues = issues[ids.index(after) + 1:]
    return _nodes(issues, first)


@app.get("/v1/issues/{issue_id}")
def get_issue(issue_id: str):
    issue, error = _issue_or_404(issue_id)
    return error or issue


@app.patch("/v1/issues/{issue_id}")
async def update_issue(issue_id: str, request: Request):
    issue, error = _issue_or_404(issue_id)
    if error is not None:
        return error
    try:
        return store.update_issue(STATE, issue, await request.json())
    except STORE_ERRORS as exc:
        return _store_error(exc)


@app.delete("/v1/issues/{issue_id}")
def archive_issue(issue_id: str):
    issue, error = _issue_or_404(issue_id)
    if error is not None:
        return error
    store.archive_issue(STATE, issue)
    return {"success": True}


# --- comments ----------------------------------------------------------------

@app.post("/v1/issues/{issue_id}/comments", status_code=201)
async def add_comment(issue_id: str, request: Request):
    issue, error = _issue_or_404(issue_id)
    if error is not None:
        return error
    body = await request.json()
    try:
        return store.create_comment(STATE, {**body, "issueId": issue["id"]})
    except store.LinearInvalidInput as exc:
        return linear_error(400, "body is required", message=exc.user_message)


@app.get("/v1/issues/{issue_id}/comments")
def list_comments(issue_id: str):
    issue, error = _issue_or_404(issue_id)
    if error is not None:
        return error
    return _nodes(store.issue_comments(STATE, issue["id"]))


# --- search ------------------------------------------------------------------

@app.get("/v1/search/issues")
def search_issues(query: str = "", first: int = 50):
    needle = query.lower()
    results = [i for i in STATE["issues"].values()
               if not i.get("archivedAt") and needle in
               f"{i.get('title', '')} {i.get('description', '')} {i.get('identifier', '')}".lower()]
    return _nodes(results, first)


# --- MCP transport -----------------------------------------------------------

from checkpoint.mcp_servers.linear_mcp import mount_on as _mount_mcp

_mount_mcp(app)
