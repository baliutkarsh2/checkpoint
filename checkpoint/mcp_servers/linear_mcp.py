"""Linear MCP server — wraps `checkpoint.twins.linear` REST surface.

Tool names mirror the official Linear MCP server's tool list: issues,
teams, projects, users, comments, labels, workflow states, cycles.
Each tool is a thin REST shim sharing STATE with the twin.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from ._shim import make_shim, mount_mcp_on_fastapi


LINEAR_BOOTSTRAP_TOKEN = "lin_api_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt0011"


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the Linear twin."""
    token = os.environ.get("LINEAR_BOOTSTRAP_TOKEN", LINEAR_BOOTSTRAP_TOKEN)
    shim = make_shim(app, token, auth_scheme="Bearer")

    mcp = FastMCP(
        name="checkpoint-linear",
        instructions="Stateful synthetic Linear. Tool names mirror the official Linear MCP server.",
        stateless_http=True,
        streamable_http_path="/",
    )

    # ----- Organization -------------------------------------------------

    @mcp.tool()
    async def linear_get_organization() -> Any:
        """Get the current organization info."""
        return await shim("GET", "/v1/organization")

    # ----- Teams --------------------------------------------------------

    @mcp.tool()
    async def linear_list_teams() -> Any:
        """List all teams in the organization."""
        return await shim("GET", "/v1/teams")

    @mcp.tool()
    async def linear_get_team(team_id: str) -> Any:
        """Get a single team by id."""
        return await shim("GET", f"/v1/teams/{team_id}")

    @mcp.tool()
    async def linear_create_team(
        name: str,
        key: str | None = None,
        description: str | None = None,
    ) -> Any:
        """Create a new team."""
        body: dict[str, Any] = {"name": name}
        if key is not None:
            body["key"] = key
        if description is not None:
            body["description"] = description
        return await shim("POST", "/v1/teams", json=body)

    # ----- Workflow States ----------------------------------------------

    @mcp.tool()
    async def linear_list_workflow_states(team_id: str | None = None) -> Any:
        """List workflow states, optionally filtered by team."""
        params: dict[str, Any] = {}
        if team_id is not None:
            params["teamId"] = team_id
        return await shim("GET", "/v1/workflow-states", params=params)

    # ----- Projects -----------------------------------------------------

    @mcp.tool()
    async def linear_list_projects(team_id: str | None = None) -> Any:
        """List projects, optionally filtered by team."""
        params: dict[str, Any] = {}
        if team_id is not None:
            params["teamId"] = team_id
        return await shim("GET", "/v1/projects", params=params)

    @mcp.tool()
    async def linear_get_project(project_id: str) -> Any:
        """Get a project by id."""
        return await shim("GET", f"/v1/projects/{project_id}")

    @mcp.tool()
    async def linear_create_project(
        name: str,
        team_ids: list[str] | None = None,
        description: str | None = None,
        state: str | None = None,
        target_date: str | None = None,
        start_date: str | None = None,
    ) -> Any:
        """Create a project."""
        body: dict[str, Any] = {"name": name}
        if team_ids is not None:
            body["teamIds"] = team_ids
        if description is not None:
            body["description"] = description
        if state is not None:
            body["state"] = state
        if target_date is not None:
            body["targetDate"] = target_date
        if start_date is not None:
            body["startDate"] = start_date
        return await shim("POST", "/v1/projects", json=body)

    @mcp.tool()
    async def linear_update_project(
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        state: str | None = None,
        target_date: str | None = None,
    ) -> Any:
        """Update a project."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if state is not None:
            body["state"] = state
        if target_date is not None:
            body["targetDate"] = target_date
        return await shim("PATCH", f"/v1/projects/{project_id}", json=body)

    # ----- Cycles -------------------------------------------------------

    @mcp.tool()
    async def linear_list_cycles(team_id: str | None = None) -> Any:
        """List cycles (sprints), optionally filtered by team."""
        params: dict[str, Any] = {}
        if team_id is not None:
            params["teamId"] = team_id
        return await shim("GET", "/v1/cycles", params=params)

    @mcp.tool()
    async def linear_create_cycle(
        team_id: str,
        starts_at: str,
        ends_at: str,
        name: str | None = None,
        description: str | None = None,
    ) -> Any:
        """Create a cycle (sprint) for a team."""
        body: dict[str, Any] = {
            "teamId": team_id,
            "startsAt": starts_at,
            "endsAt": ends_at,
        }
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        return await shim("POST", "/v1/cycles", json=body)

    # ----- Labels -------------------------------------------------------

    @mcp.tool()
    async def linear_list_labels(team_id: str | None = None) -> Any:
        """List labels, optionally filtered by team."""
        params: dict[str, Any] = {}
        if team_id is not None:
            params["teamId"] = team_id
        return await shim("GET", "/v1/labels", params=params)

    @mcp.tool()
    async def linear_create_label(
        name: str, color: str | None = None, team_id: str | None = None
    ) -> Any:
        """Create a label."""
        body: dict[str, Any] = {"name": name}
        if color is not None:
            body["color"] = color
        if team_id is not None:
            body["teamId"] = team_id
        return await shim("POST", "/v1/labels", json=body)

    # ----- Users --------------------------------------------------------

    @mcp.tool()
    async def linear_list_users() -> Any:
        """List all workspace members."""
        return await shim("GET", "/v1/users")

    @mcp.tool()
    async def linear_get_user(user_id: str) -> Any:
        """Get a user by id or email."""
        return await shim("GET", f"/v1/users/{user_id}")

    @mcp.tool()
    async def linear_get_viewer() -> Any:
        """Get the currently authenticated user."""
        return await shim("GET", "/v1/users/me")

    # ----- Issues -------------------------------------------------------

    @mcp.tool()
    async def linear_create_issue(
        title: str,
        team_id: str | None = None,
        description: str | None = None,
        state_id: str | None = None,
        assignee_id: str | None = None,
        priority: int | None = None,
        label_ids: list[str] | None = None,
        project_id: str | None = None,
        cycle_id: str | None = None,
        estimate: int | None = None,
        due_date: str | None = None,
    ) -> Any:
        """Create an issue.

        priority: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low
        """
        body: dict[str, Any] = {"title": title}
        if team_id is not None:
            body["teamId"] = team_id
        if description is not None:
            body["description"] = description
        if state_id is not None:
            body["stateId"] = state_id
        if assignee_id is not None:
            body["assigneeId"] = assignee_id
        if priority is not None:
            body["priority"] = priority
        if label_ids is not None:
            body["labelIds"] = label_ids
        if project_id is not None:
            body["projectId"] = project_id
        if cycle_id is not None:
            body["cycleId"] = cycle_id
        if estimate is not None:
            body["estimate"] = estimate
        if due_date is not None:
            body["dueDate"] = due_date
        return await shim("POST", "/v1/issues", json=body)

    @mcp.tool()
    async def linear_get_issue(issue_id: str) -> Any:
        """Get an issue by UUID or identifier (e.g. ENG-42)."""
        return await shim("GET", f"/v1/issues/{issue_id}")

    @mcp.tool()
    async def linear_list_issues(
        team_id: str | None = None,
        state_id: str | None = None,
        assignee_id: str | None = None,
        project_id: str | None = None,
        label_id: str | None = None,
        priority: int | None = None,
        first: int = 50,
    ) -> Any:
        """List issues with optional filters."""
        params: dict[str, Any] = {"first": first}
        if team_id is not None:
            params["teamId"] = team_id
        if state_id is not None:
            params["stateId"] = state_id
        if assignee_id is not None:
            params["assigneeId"] = assignee_id
        if project_id is not None:
            params["projectId"] = project_id
        if label_id is not None:
            params["labelId"] = label_id
        if priority is not None:
            params["priority"] = priority
        return await shim("GET", "/v1/issues", params=params)

    @mcp.tool()
    async def linear_update_issue(
        issue_id: str,
        title: str | None = None,
        description: str | None = None,
        state_id: str | None = None,
        assignee_id: str | None = None,
        priority: int | None = None,
        label_ids: list[str] | None = None,
        project_id: str | None = None,
        cycle_id: str | None = None,
        estimate: int | None = None,
        due_date: str | None = None,
    ) -> Any:
        """Update an issue."""
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if state_id is not None:
            body["stateId"] = state_id
        if assignee_id is not None:
            body["assigneeId"] = assignee_id
        if priority is not None:
            body["priority"] = priority
        if label_ids is not None:
            body["labelIds"] = label_ids
        if project_id is not None:
            body["projectId"] = project_id
        if cycle_id is not None:
            body["cycleId"] = cycle_id
        if estimate is not None:
            body["estimate"] = estimate
        if due_date is not None:
            body["dueDate"] = due_date
        return await shim("PATCH", f"/v1/issues/{issue_id}", json=body)

    @mcp.tool()
    async def linear_archive_issue(issue_id: str) -> Any:
        """Archive (soft-delete) an issue."""
        return await shim("DELETE", f"/v1/issues/{issue_id}")

    @mcp.tool()
    async def linear_search_issues(query: str, first: int = 50) -> Any:
        """Full-text search across issue titles and descriptions."""
        return await shim(
            "GET", "/v1/search/issues",
            params={"query": query, "first": first},
        )

    # ----- Comments -----------------------------------------------------

    @mcp.tool()
    async def linear_create_comment(issue_id: str, body: str) -> Any:
        """Add a comment to an issue."""
        return await shim(
            "POST", f"/v1/issues/{issue_id}/comments",
            json={"body": body},
        )

    @mcp.tool()
    async def linear_list_comments(issue_id: str) -> Any:
        """List comments on an issue."""
        return await shim("GET", f"/v1/issues/{issue_id}/comments")

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the Linear FastMCP server and mount it at `/mcp` on `app`."""
    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
