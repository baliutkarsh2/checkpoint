"""The Linear twin's data model: state shape, records, mutations, views.

Both surfaces the twin serves — the real GraphQL API (:mod:`linear_graphql`)
and the REST-ish compatibility surface the MCP tools call (:mod:`linear`) —
go through these helpers, so an issue created over GraphQL is byte-identical
to one created over REST.

Records keep Linear's own field names and, like the API's own responses, carry
a resolved copy of their relations (``state``, ``team``, ``assignee``,
``labels``): the copies are references to the canonical records, so renaming a
team or a label is visible everywhere at once, and scenario checks that read
raw state see the same shapes the API returns.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from checkpoint.twins import kit

PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}

# Workflow states Linear creates for every new team, in board order.
DEFAULT_WORKFLOW_STATES = (
    ("Backlog", "backlog", "#bec2c8"),
    ("Todo", "unstarted", "#e2e2e2"),
    ("In Progress", "started", "#f2c94c"),
    ("Done", "completed", "#5e6ad2"),
    ("Canceled", "canceled", "#95a2b3"),
)

DEFAULT_ORG_ID = "org-checkpoint-test"
DEFAULT_TEAM_ID = "team-engineering"
DEFAULT_USER_ID = "user-default"

COLLECTIONS = ("teams", "workflow_states", "users", "labels", "projects", "cycles",
               "issues", "comments")


class LinearNotFound(Exception):
    """A referenced entity does not exist (Linear: ``Entity not found: <Type>``)."""

    def __init__(self, entity: str) -> None:
        super().__init__(f"Entity not found: {entity}")
        self.entity = entity

    @property
    def user_message(self) -> str:
        return f"Could not find referenced {self.entity}."


class LinearInvalidInput(Exception):
    """Input the API rejects before touching state."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


def priority_label(priority: Any) -> str:
    try:
        return PRIORITY_LABELS[int(priority)]
    except (TypeError, ValueError, KeyError):
        return PRIORITY_LABELS[0]


# --- state -------------------------------------------------------------------

def fresh_state() -> dict:
    """A workspace with one team, its workflow states, and the acting user."""
    created = now()
    states = {}
    for position, (name, type_, color) in enumerate(DEFAULT_WORKFLOW_STATES):
        sid = f"state-{name.lower().replace(' ', '-')}"
        states[sid] = {
            "id": sid, "name": name, "type": type_, "color": color,
            "position": float(position), "description": None,
            "teamId": DEFAULT_TEAM_ID, "createdAt": created, "updatedAt": created,
            "archivedAt": None,
        }
    return {
        "organization": {
            "id": DEFAULT_ORG_ID,
            "name": "Checkpoint Test Org",
            "urlKey": "checkpoint",
            "createdAt": created,
            "updatedAt": created,
            "userCount": 1,
            "createdIssueCount": 0,
        },
        "teams": {DEFAULT_TEAM_ID: _team_record(
            DEFAULT_TEAM_ID, "Engineering", "ENG", "Engineering team", created)},
        "workflow_states": states,
        "users": {DEFAULT_USER_ID: _user_record(
            DEFAULT_USER_ID, "Default User", "user@checkpoint.test", created, admin=True)},
        "labels": {},
        "projects": {},
        "cycles": {},
        "issues": {},
        "comments": {},
        "_counters": {
            "issue_seq": {},   # team key -> last issue number
            "sync": 0,         # bumped per mutation, returned as lastSyncId
        },
        # The user API keys in this workspace authenticate as; seeds may override.
        "_viewer": DEFAULT_USER_ID,
        "_config": {"rate_limit": None},
    }


def _team_record(team_id: str, name: str, key: str, description: str, created: str) -> dict:
    return {
        "id": team_id, "name": name, "key": key, "description": description,
        "color": "#5e6ad2", "icon": None, "private": False, "timezone": "UTC",
        "cyclesEnabled": False, "createdAt": created, "updatedAt": created,
        "archivedAt": None,
    }


def _user_record(user_id: str, name: str, email: str, created: str, *, admin: bool = False) -> dict:
    return {
        "id": user_id, "name": name, "displayName": name, "email": email,
        "active": True, "admin": admin, "guest": False, "avatarUrl": None,
        "createdAt": created, "updatedAt": created, "archivedAt": None,
        "url": f"https://linear.app/checkpoint/profiles/{user_id}",
    }


def normalize(state: dict) -> None:
    """Make a freshly loaded seed self-consistent.

    Seeds are hand-written, so they may omit derived fields (``number``,
    ``archivedAt``), carry stale copies of relations, or leave the issue
    counter behind the identifiers they seed — which would hand the next
    created issue an identifier that is already taken.
    """
    for name in COLLECTIONS:
        state.setdefault(name, {})
    counters = state.setdefault("_counters", {})
    seq = counters.setdefault("issue_seq", {})
    counters.setdefault("sync", 0)
    state.setdefault("_viewer", next(iter(state["users"]), DEFAULT_USER_ID))

    for issue in state["issues"].values():
        team = state["teams"].get(issue.get("teamId")) or {}
        key = team.get("key") or str(issue.get("identifier", "ENG-0")).rsplit("-", 1)[0]
        number = issue.get("number")
        if number is None:
            tail = str(issue.get("identifier", "")).rsplit("-", 1)[-1]
            number = int(tail) if tail.isdigit() else 0
        issue["number"] = float(number)
        issue.setdefault("identifier", f"{key}-{int(number)}")
        issue.setdefault("labelIds", [])
        issue.setdefault("priorityLabel", priority_label(issue.get("priority", 0)))
        for field in ("archivedAt", "completedAt", "canceledAt", "startedAt", "assigneeId",
                      "projectId", "cycleId", "parentId", "estimate", "dueDate"):
            issue.setdefault(field, None)
        issue.setdefault("createdAt", now())
        issue.setdefault("updatedAt", issue["createdAt"])
        issue.setdefault("commentCount", 0)
        issue.setdefault("creatorId", state["_viewer"])
        issue.setdefault("branchName", _branch_name(issue))
        issue.setdefault("url", issue_url(state, issue))
        issue.setdefault("sortOrder", -float(number))
        sync_issue_relations(state, issue)
        seq[key] = max(seq.get(key, 0), int(number))

    for comment in state["comments"].values():
        comment.setdefault("createdAt", now())
        comment.setdefault("updatedAt", comment["createdAt"])
        comment.setdefault("archivedAt", None)
        comment.setdefault("userId", state["_viewer"])
        comment["user"] = state["users"].get(comment.get("userId"))

    for collection in ("teams", "users", "labels", "projects", "cycles", "workflow_states"):
        for record in state[collection].values():
            record.setdefault("createdAt", now())
            record.setdefault("updatedAt", record["createdAt"])
            record.setdefault("archivedAt", None)


def viewer(state: dict) -> dict | None:
    """The user the current credential authenticates as."""
    users = state.get("users") or {}
    return users.get(state.get("_viewer")) or next(iter(users.values()), None)


def bump_sync(state: dict) -> float:
    """Advance and return ``lastSyncId``, the counter Linear returns on writes."""
    counters = state.setdefault("_counters", {})
    counters["sync"] = counters.get("sync", 0) + 1
    return float(counters["sync"])


def org_key(state: dict) -> str:
    return (state.get("organization") or {}).get("urlKey") or "checkpoint"


def issue_url(state: dict, issue: dict) -> str:
    slug = _slug(issue.get("title") or "")
    tail = f"/{slug}" if slug else ""
    return f"https://linear.app/{org_key(state)}/issue/{issue['identifier']}{tail}"


def _branch_name(issue: dict) -> str:
    return f"{str(issue.get('identifier', '')).lower()}-{_slug(issue.get('title') or '')}".strip("-")


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40].strip("-")


# --- lookups -----------------------------------------------------------------

def find_issue(state: dict, ref: str) -> dict | None:
    """An issue by UUID or by human identifier (``ENG-12``)."""
    issue = state["issues"].get(ref)
    if issue is not None:
        return issue
    wanted = str(ref).strip().upper()
    return next((i for i in state["issues"].values()
                 if str(i.get("identifier", "")).upper() == wanted), None)


def require(state: dict, collection: str, entity: str, ref: str | None) -> dict:
    record = (state.get(collection) or {}).get(ref) if ref else None
    if record is None:
        raise LinearNotFound(entity)
    return record


def require_issue(state: dict, ref: str) -> dict:
    issue = find_issue(state, ref)
    if issue is None:
        raise LinearNotFound("Issue")
    return issue


def team_states(state: dict, team_id: str) -> list[dict]:
    states = [s for s in state["workflow_states"].values() if s.get("teamId") == team_id]
    return sorted(states, key=lambda s: s.get("position", 0.0))


def default_state_id(state: dict, team_id: str) -> str:
    """The state a new issue lands in: the team's backlog, else its first state."""
    states = team_states(state, team_id)
    backlog = next((s for s in states if s.get("type") == "backlog"), None)
    chosen = backlog or (states[0] if states else None)
    if chosen is None:
        raise LinearNotFound("WorkflowState")
    return chosen["id"]


def sync_issue_relations(state: dict, issue: dict) -> None:
    """Point the issue's inlined relations at the canonical records."""
    workflow_state = state["workflow_states"].get(issue.get("stateId"))
    if workflow_state is not None:
        issue["state"] = workflow_state
    else:  # a seed may inline a state the workspace does not define
        issue.setdefault("state", {"id": issue.get("stateId"), "name": "Backlog",
                                   "type": "backlog", "color": "#bec2c8", "position": 0.0})
    issue["team"] = state["teams"].get(issue.get("teamId"))
    issue["assignee"] = state["users"].get(issue.get("assigneeId")) if issue.get("assigneeId") else None
    issue["labels"] = [state["labels"][lid] for lid in issue.get("labelIds") or []
                       if lid in state["labels"]]


# --- issues ------------------------------------------------------------------

def create_issue(state: dict, data: dict) -> dict:
    """Create an issue from Linear's ``IssueCreateInput`` fields."""
    team = require(state, "teams", "Team", data.get("teamId") or next(iter(state["teams"]), None))
    state_id = data.get("stateId") or default_state_id(state, team["id"])
    require(state, "workflow_states", "WorkflowState", state_id)
    assignee_id = data.get("assigneeId")
    if assignee_id:
        require(state, "users", "User", assignee_id)
    if data.get("projectId"):
        require(state, "projects", "Project", data["projectId"])
    if data.get("cycleId"):
        require(state, "cycles", "Cycle", data["cycleId"])
    for label_id in data.get("labelIds") or []:
        require(state, "labels", "IssueLabel", label_id)

    created = data.get("createdAt") or now()
    number = next_issue_number(state, team["key"])
    priority = int(data.get("priority") or 0)
    issue = {
        "id": data.get("id") or uid(),
        "identifier": f"{team['key']}-{number}",
        "number": float(number),
        "title": data.get("title") or "",
        "description": data.get("description") or "",
        "priority": priority,
        "priorityLabel": priority_label(priority),
        "estimate": data.get("estimate"),
        "dueDate": data.get("dueDate"),
        "stateId": state_id,
        "teamId": team["id"],
        "assigneeId": assignee_id,
        "creatorId": state.get("_viewer"),
        "labelIds": list(data.get("labelIds") or []),
        "projectId": data.get("projectId"),
        "cycleId": data.get("cycleId"),
        "parentId": data.get("parentId"),
        "subscriberIds": list(data.get("subscriberIds") or []),
        "sortOrder": float(data.get("sortOrder") or -number),
        "createdAt": created,
        "updatedAt": created,
        "startedAt": None,
        "completedAt": data.get("completedAt"),
        "canceledAt": None,
        "archivedAt": None,
        "trashed": None,
        "commentCount": 0,
    }
    issue["branchName"] = _branch_name(issue)
    issue["url"] = issue_url(state, issue)
    _stamp_state_timestamps(state, issue, state_id)
    sync_issue_relations(state, issue)
    state["issues"][issue["id"]] = issue
    _recount_project(state, issue.get("projectId"))
    bump_sync(state)
    return issue


def next_issue_number(state: dict, team_key: str) -> int:
    seq = state["_counters"]["issue_seq"]
    seq[team_key] = seq.get(team_key, 0) + 1
    return seq[team_key]


def update_issue(state: dict, issue: dict, data: dict) -> dict:
    """Apply Linear's ``IssueUpdateInput`` fields to an issue."""
    previous_project = issue.get("projectId")
    for field in ("title", "description", "estimate", "dueDate", "sortOrder", "parentId",
                  "subscriberIds", "trashed"):
        if field in data:
            issue[field] = data[field]
    if "priority" in data:
        issue["priority"] = int(data["priority"] or 0)
        issue["priorityLabel"] = priority_label(issue["priority"])
    if "teamId" in data and data["teamId"]:
        issue["teamId"] = require(state, "teams", "Team", data["teamId"])["id"]
    if "assigneeId" in data:
        assignee_id = data["assigneeId"]
        if assignee_id:
            require(state, "users", "User", assignee_id)
        issue["assigneeId"] = assignee_id
    if "projectId" in data:
        if data["projectId"]:
            require(state, "projects", "Project", data["projectId"])
        issue["projectId"] = data["projectId"]
    if "cycleId" in data:
        if data["cycleId"]:
            require(state, "cycles", "Cycle", data["cycleId"])
        issue["cycleId"] = data["cycleId"]
    if "labelIds" in data:
        label_ids = list(data["labelIds"] or [])
        for label_id in label_ids:
            require(state, "labels", "IssueLabel", label_id)
        issue["labelIds"] = label_ids
    for field, sign in (("addedLabelIds", 1), ("removedLabelIds", -1)):
        for label_id in data.get(field) or []:
            require(state, "labels", "IssueLabel", label_id)
            labels = issue.setdefault("labelIds", [])
            if sign > 0 and label_id not in labels:
                labels.append(label_id)
            elif sign < 0 and label_id in labels:
                labels.remove(label_id)
    if data.get("stateId"):
        require(state, "workflow_states", "WorkflowState", data["stateId"])
        issue["stateId"] = data["stateId"]
        _stamp_state_timestamps(state, issue, data["stateId"])
    if "title" in data:
        issue["branchName"] = _branch_name(issue)
        issue["url"] = issue_url(state, issue)
    issue["updatedAt"] = now()
    sync_issue_relations(state, issue)
    if issue.get("projectId") != previous_project:
        _recount_project(state, previous_project)
        _recount_project(state, issue.get("projectId"))
    bump_sync(state)
    return issue


def _stamp_state_timestamps(state: dict, issue: dict, state_id: str) -> None:
    """Linear stamps startedAt/completedAt/canceledAt from the state's type."""
    workflow_state = state["workflow_states"].get(state_id) or {}
    type_ = workflow_state.get("type")
    stamp = now()
    if type_ == "started" and not issue.get("startedAt"):
        issue["startedAt"] = stamp
    if type_ == "completed":
        issue["completedAt"] = issue.get("completedAt") or stamp
        issue["canceledAt"] = None
    elif type_ in ("canceled", "cancelled"):
        issue["canceledAt"] = issue.get("canceledAt") or stamp
        issue["completedAt"] = None
    else:
        issue["completedAt"] = None
        issue["canceledAt"] = None


def archive_issue(state: dict, issue: dict, *, trash: bool = False) -> dict:
    """Linear archives instead of deleting; ``trash`` moves it to the trash."""
    issue["archivedAt"] = now()
    issue["updatedAt"] = issue["archivedAt"]
    if trash:
        issue["trashed"] = True
    _recount_project(state, issue.get("projectId"))
    bump_sync(state)
    return issue


def unarchive_issue(state: dict, issue: dict) -> dict:
    issue["archivedAt"] = None
    issue["trashed"] = None
    issue["updatedAt"] = now()
    _recount_project(state, issue.get("projectId"))
    bump_sync(state)
    return issue


def _recount_project(state: dict, project_id: str | None) -> None:
    project = state["projects"].get(project_id) if project_id else None
    if project is None:
        return
    issues = [i for i in state["issues"].values()
              if i.get("projectId") == project_id and not i.get("archivedAt")]
    done = [i for i in issues
            if (state["workflow_states"].get(i.get("stateId")) or {}).get("type") == "completed"]
    project["issueCount"] = len(issues)
    project["progress"] = round(len(done) / len(issues), 4) if issues else 0.0


# --- comments ----------------------------------------------------------------

def create_comment(state: dict, data: dict) -> dict:
    issue = require_issue(state, data["issueId"]) if data.get("issueId") else None
    if issue is None:
        raise LinearInvalidInput(
            "Argument Validation Error",
            "A comment must belong to an issue, project update or document.")
    body = data.get("body")
    if not body:
        raise LinearInvalidInput("Argument Validation Error", "Comment body cannot be empty.")
    created = data.get("createdAt") or now()
    comment = {
        "id": data.get("id") or uid(),
        "body": body,
        "issueId": issue["id"],
        "userId": data.get("userId") or state.get("_viewer"),
        "parentId": data.get("parentId"),
        "createdAt": created,
        "updatedAt": created,
        "editedAt": None,
        "archivedAt": None,
    }
    comment["url"] = f"{issue['url']}#comment-{comment['id'][:8]}"
    comment["user"] = state["users"].get(comment["userId"])
    state["comments"][comment["id"]] = comment
    issue["commentCount"] = issue.get("commentCount", 0) + 1
    bump_sync(state)
    return comment


def issue_comments(state: dict, issue_id: str) -> list[dict]:
    comments = [c for c in state["comments"].values() if c.get("issueId") == issue_id]
    return sorted(comments, key=lambda c: c.get("createdAt") or "")


# --- labels, projects, cycles, teams -----------------------------------------

def create_label(state: dict, data: dict) -> dict:
    name = data.get("name")
    if not name:
        raise LinearInvalidInput("Argument Validation Error", "A label needs a name.")
    if data.get("teamId"):
        require(state, "teams", "Team", data["teamId"])
    created = now()
    label = {
        "id": data.get("id") or uid(),
        "name": name,
        "description": data.get("description"),
        "color": data.get("color") or "#bec2c8",
        "teamId": data.get("teamId"),
        "creatorId": state.get("_viewer"),
        "parentId": data.get("parentId"),
        "isGroup": bool(data.get("isGroup")),
        "createdAt": created,
        "updatedAt": created,
        "archivedAt": None,
    }
    state["labels"][label["id"]] = label
    bump_sync(state)
    return label


def create_project(state: dict, data: dict) -> dict:
    name = data.get("name")
    if not name:
        raise LinearInvalidInput("Argument Validation Error", "A project needs a name.")
    team_ids = list(data.get("teamIds") or [])
    for team_id in team_ids:
        require(state, "teams", "Team", team_id)
    if data.get("leadId"):
        require(state, "users", "User", data["leadId"])
    created = now()
    project = {
        "id": data.get("id") or uid(),
        "name": name,
        "description": data.get("description") or "",
        "content": data.get("content"),
        "state": data.get("state") or "planned",
        "teamIds": team_ids,
        "leadId": data.get("leadId"),
        "memberIds": list(data.get("memberIds") or []),
        "color": data.get("color") or "#5e6ad2",
        "icon": data.get("icon"),
        "priority": int(data.get("priority") or 0),
        "startDate": data.get("startDate"),
        "targetDate": data.get("targetDate"),
        "createdAt": created,
        "updatedAt": created,
        "startedAt": None,
        "completedAt": None,
        "canceledAt": None,
        "archivedAt": None,
        "progress": 0.0,
        "issueCount": 0,
        "slugId": uid()[:8],
    }
    project["url"] = f"https://linear.app/{org_key(state)}/project/{_slug(name)}-{project['slugId']}"
    state["projects"][project["id"]] = project
    bump_sync(state)
    return project


def update_project(state: dict, project: dict, data: dict) -> dict:
    for field in ("name", "description", "content", "state", "startDate", "targetDate",
                  "color", "icon", "priority"):
        if field in data:
            project[field] = data[field]
    if "teamIds" in data:
        for team_id in data["teamIds"] or []:
            require(state, "teams", "Team", team_id)
        project["teamIds"] = list(data["teamIds"] or [])
    if "leadId" in data:
        if data["leadId"]:
            require(state, "users", "User", data["leadId"])
        project["leadId"] = data["leadId"]
    project["updatedAt"] = now()
    bump_sync(state)
    return project


def create_team(state: dict, data: dict) -> dict:
    name = data.get("name")
    if not name:
        raise LinearInvalidInput("Argument Validation Error", "A team needs a name.")
    key = (data.get("key") or name[:3]).upper()
    created = now()
    team = _team_record(data.get("id") or uid(), name, key, data.get("description") or "", created)
    team["cyclesEnabled"] = bool(data.get("cyclesEnabled"))
    state["teams"][team["id"]] = team
    for position, (state_name, type_, color) in enumerate(DEFAULT_WORKFLOW_STATES):
        sid = uid()
        state["workflow_states"][sid] = {
            "id": sid, "name": state_name, "type": type_, "color": color,
            "position": float(position), "description": None, "teamId": team["id"],
            "createdAt": created, "updatedAt": created, "archivedAt": None,
        }
    bump_sync(state)
    return team


def create_cycle(state: dict, data: dict) -> dict:
    team = require(state, "teams", "Team", data.get("teamId"))
    if not data.get("startsAt") or not data.get("endsAt"):
        raise LinearInvalidInput("Argument Validation Error",
                                 "A cycle needs a start and an end date.")
    created = now()
    number = len([c for c in state["cycles"].values() if c.get("teamId") == team["id"]]) + 1
    cycle = {
        "id": data.get("id") or uid(),
        "teamId": team["id"],
        "number": float(number),
        "name": data.get("name"),
        "description": data.get("description"),
        "startsAt": data["startsAt"],
        "endsAt": data["endsAt"],
        "completedAt": data.get("completedAt"),
        "createdAt": created,
        "updatedAt": created,
        "archivedAt": None,
        "progress": 0.0,
        "issueCount": 0,
    }
    state["cycles"][cycle["id"]] = cycle
    bump_sync(state)
    return cycle


# --- views -------------------------------------------------------------------

def views(state: dict) -> dict[str, kit.View]:
    """Normalized collections for assertions.

    Every record keeps its API field names and gains the denormalized fields a
    criterion reaches for ("is any issue still assigned to Bob?") without
    having to join by id.
    """
    teams = state.get("teams") or {}
    users = state.get("users") or {}
    labels = state.get("labels") or {}
    projects = state.get("projects") or {}
    cycles = state.get("cycles") or {}
    workflow_states = state.get("workflow_states") or {}

    def _name(collection: dict, ref: Any, field: str = "name") -> Any:
        record = collection.get(ref) if ref else None
        return record.get(field) if record else None

    issues = []
    for issue in (state.get("issues") or {}).values():
        workflow_state = workflow_states.get(issue.get("stateId")) or issue.get("state") or {}
        assignee = users.get(issue.get("assigneeId")) or {}
        issues.append({
            **{k: v for k, v in issue.items() if k not in ("state", "team", "assignee", "labels")},
            "stateName": workflow_state.get("name"),
            "stateType": workflow_state.get("type"),
            "teamKey": _name(teams, issue.get("teamId"), "key"),
            "teamName": _name(teams, issue.get("teamId")),
            "assigneeName": assignee.get("name"),
            "assigneeEmail": assignee.get("email"),
            "creatorName": _name(users, issue.get("creatorId")),
            "labelNames": [labels[lid]["name"] for lid in issue.get("labelIds") or []
                           if lid in labels],
            "projectName": _name(projects, issue.get("projectId")),
            "cycleName": _name(cycles, issue.get("cycleId")),
        })

    comments = [{
        **{k: v for k, v in comment.items() if k != "user"},
        "issueIdentifier": _name(state.get("issues") or {}, comment.get("issueId"), "identifier"),
        "userName": _name(users, comment.get("userId")),
    } for comment in (state.get("comments") or {}).values()]

    return {
        "issues": kit.View(issues, tombstone="archivedAt", nouns=("issue", "issues")),
        "comments": kit.View(comments, tombstone="archivedAt", nouns=("comment", "comments")),
        "teams": kit.View(list(teams.values()), tombstone="archivedAt", nouns=("team", "teams")),
        "users": kit.View(list(users.values()), tombstone="archivedAt",
                          nouns=("user", "users", "member", "members")),
        "labels": kit.View([{**label, "teamKey": _name(teams, label.get("teamId"), "key")}
                            for label in labels.values()],
                           tombstone="archivedAt", nouns=("label", "labels")),
        "projects": kit.View([{**project,
                               "teamNames": [_name(teams, t) for t in project.get("teamIds") or []],
                               "leadName": _name(users, project.get("leadId"))}
                              for project in projects.values()],
                             tombstone="archivedAt", nouns=("project", "projects")),
        "cycles": kit.View([{**cycle, "teamKey": _name(teams, cycle.get("teamId"), "key")}
                            for cycle in cycles.values()],
                           tombstone="archivedAt",
                           nouns=("cycle", "cycles", "sprint", "sprints")),
        "workflow_states": kit.View([{**s, "teamKey": _name(teams, s.get("teamId"), "key")}
                                     for s in workflow_states.values()],
                                    nouns=("workflow state", "workflow states",
                                           "status", "statuses")),
    }
