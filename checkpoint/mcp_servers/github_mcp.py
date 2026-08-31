"""GitHub MCP server — wraps `checkpoint.twins.github` REST surface.

Tool names match Archal's faithful list (SCOPE.md §3.2): 33 tools across
repositories, files, branches, issues, pull requests, commits, workflows,
and search. Each tool body is a thin REST shim — the FastAPI twin is
called in-process via `httpx.ASGITransport`, so REST and MCP share the
same `STATE` dict.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`, which
the twin module calls at import time.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN

from ._shim import make_shim

GITHUB_BOOTSTRAP_TOKEN = FAKE_GITHUB_TOKEN


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the given twin app."""
    import os

    token = os.environ.get("GITHUB_BOOTSTRAP_TOKEN", GITHUB_BOOTSTRAP_TOKEN)
    shim = make_shim(app, token, auth_scheme="token")

    mcp = FastMCP(
        name="checkpoint-github",
        instructions="Stateful synthetic GitHub. Tool names match Archal §3.2.",
        stateless_http=True,
        streamable_http_path="/",
    )

    # ----- Repositories -------------------------------------------------

    @mcp.tool()
    async def create_repository(
        name: str,
        owner: str | None = None,
        private: bool = False,
    ) -> Any:
        """Create a repository."""
        body: dict[str, Any] = {"name": name, "private": private}
        if owner is not None:
            body["owner"] = owner
        return await shim("POST", "/user/repos", json=body)

    @mcp.tool()
    async def get_repository(owner: str, repo: str) -> Any:
        """Get a single repository by owner/name."""
        return await shim("GET", f"/repos/{owner}/{repo}")

    @mcp.tool()
    async def search_repositories(
        q: str, per_page: int = 30, page: int = 1
    ) -> Any:
        """Substring search over repository full names."""
        return await shim(
            "GET", "/search/repositories",
            params={"q": q, "per_page": per_page, "page": page},
        )

    @mcp.tool()
    async def fork_repository(
        owner: str, repo: str, organization: str | None = None
    ) -> Any:
        """Fork a repository under a different owner."""
        body = {"organization": organization} if organization else {}
        return await shim("POST", f"/repos/{owner}/{repo}/forks", json=body)

    @mcp.tool()
    async def search_code(q: str, per_page: int = 30, page: int = 1) -> Any:
        """Substring search over file contents across all repos."""
        return await shim(
            "GET", "/search/code",
            params={"q": q, "per_page": per_page, "page": page},
        )

    @mcp.tool()
    async def search_users(q: str, per_page: int = 30, page: int = 1) -> Any:
        """Substring search over user logins."""
        return await shim(
            "GET", "/search/users",
            params={"q": q, "per_page": per_page, "page": page},
        )

    # ----- Files --------------------------------------------------------

    @mcp.tool()
    async def get_file_contents(
        owner: str, repo: str, path: str, ref: str | None = None
    ) -> Any:
        """Return base64-encoded file contents at a given path."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref} if ref else None,
        )

    @mcp.tool()
    async def create_or_update_file(
        owner: str,
        repo: str,
        path: str,
        message: str,
        content: str,
        branch: str | None = None,
        sha: str | None = None,
    ) -> Any:
        """Create or update a file. `content` must be base64-encoded."""
        body: dict[str, Any] = {"message": message, "content": content}
        if branch is not None:
            body["branch"] = branch
        if sha is not None:
            body["sha"] = sha
        return await shim(
            "PUT", f"/repos/{owner}/{repo}/contents/{path}", json=body,
        )

    @mcp.tool()
    async def push_files(
        owner: str,
        repo: str,
        files: list[dict[str, str]],
        branch: str | None = None,
        message: str | None = None,
    ) -> Any:
        """Batch-push files. `files` is a list of `{path, content}` dicts."""
        body: dict[str, Any] = {"files": files}
        if branch is not None:
            body["branch"] = branch
        if message is not None:
            body["message"] = message
        return await shim("POST", f"/repos/{owner}/{repo}/_push_files", json=body)

    # ----- Branches -----------------------------------------------------

    @mcp.tool()
    async def create_branch(
        owner: str, repo: str, branch: str, from_branch: str | None = None
    ) -> Any:
        """Create a new branch pointing at `from_branch`'s tip."""
        body: dict[str, Any] = {"ref": f"refs/heads/{branch}"}
        # Resolve from_branch sha via the twin if provided.
        if from_branch:
            src = await shim("GET", f"/repos/{owner}/{repo}/branches")
            if isinstance(src, list):
                for b in src:
                    if b.get("name") == from_branch:
                        body["sha"] = b["commit"]["sha"]
                        break
        return await shim("POST", f"/repos/{owner}/{repo}/git/refs", json=body)

    @mcp.tool()
    async def list_branches(owner: str, repo: str) -> Any:
        """List all branches in a repository."""
        return await shim("GET", f"/repos/{owner}/{repo}/branches")

    @mcp.tool()
    async def delete_branch(owner: str, repo: str, branch: str) -> Any:
        """Delete a branch (cannot delete default branch)."""
        return await shim(
            "DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
        )

    # ----- Issues -------------------------------------------------------

    @mcp.tool()
    async def create_issue(
        owner: str,
        repo: str,
        title: str,
        body: str | None = None,
        labels: list[str] | None = None,
    ) -> Any:
        """Create an issue."""
        payload: dict[str, Any] = {"title": title}
        if body is not None:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = labels
        return await shim("POST", f"/repos/{owner}/{repo}/issues", json=payload)

    @mcp.tool()
    async def get_issue(owner: str, repo: str, number: int) -> Any:
        """Get an issue by number."""
        return await shim("GET", f"/repos/{owner}/{repo}/issues/{number}")

    @mcp.tool()
    async def list_issues(
        owner: str, repo: str, state: str = "open", labels: str | None = None,
    ) -> Any:
        """List issues filtered by state and optional comma-separated labels."""
        params: dict[str, Any] = {"state": state}
        if labels is not None:
            params["labels"] = labels
        return await shim(
            "GET", f"/repos/{owner}/{repo}/issues", params=params,
        )

    @mcp.tool()
    async def update_issue(
        owner: str,
        repo: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> Any:
        """Patch an issue."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = labels
        return await shim(
            "PATCH", f"/repos/{owner}/{repo}/issues/{number}", json=payload,
        )

    @mcp.tool()
    async def search_issues(q: str, per_page: int = 30, page: int = 1) -> Any:
        """Substring search over issue title + body."""
        return await shim(
            "GET", "/search/issues",
            params={"q": q, "per_page": per_page, "page": page},
        )

    @mcp.tool()
    async def add_issue_comment(
        owner: str, repo: str, number: int, body: str
    ) -> Any:
        """Add a comment to an issue."""
        return await shim(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )

    # ----- Pull requests -----------------------------------------------

    @mcp.tool()
    async def create_pull_request(
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
        draft: bool = False,
    ) -> Any:
        """Open a pull request."""
        payload: dict[str, Any] = {
            "title": title, "head": head, "base": base, "draft": draft,
        }
        if body is not None:
            payload["body"] = body
        return await shim("POST", f"/repos/{owner}/{repo}/pulls", json=payload)

    @mcp.tool()
    async def get_pull_request(owner: str, repo: str, number: int) -> Any:
        """Get a pull request by number."""
        return await shim("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    @mcp.tool()
    async def list_pull_requests(
        owner: str,
        repo: str,
        state: str = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> Any:
        """List pull requests filtered by state/head/base."""
        params: dict[str, Any] = {"state": state}
        if head is not None:
            params["head"] = head
        if base is not None:
            params["base"] = base
        return await shim(
            "GET", f"/repos/{owner}/{repo}/pulls", params=params,
        )

    @mcp.tool()
    async def update_pull_request(
        owner: str,
        repo: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> Any:
        """Patch a pull request."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        return await shim(
            "PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json=payload,
        )

    @mcp.tool()
    async def merge_pull_request(
        owner: str,
        repo: str,
        number: int,
        commit_message: str | None = None,
    ) -> Any:
        """Merge a pull request."""
        body = {"commit_message": commit_message} if commit_message else {}
        return await shim(
            "PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge", json=body,
        )

    @mcp.tool()
    async def get_pull_request_diff(owner: str, repo: str, number: int) -> Any:
        """Get the unified diff for a pull request."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}.diff",
        )

    @mcp.tool()
    async def get_pull_request_commits(
        owner: str, repo: str, number: int
    ) -> Any:
        """List commits on a pull request."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/commits",
        )

    @mcp.tool()
    async def get_pull_request_reviews(
        owner: str, repo: str, number: int
    ) -> Any:
        """List reviews on a pull request."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/reviews",
        )

    @mcp.tool()
    async def create_pull_request_review(
        owner: str,
        repo: str,
        number: int,
        event: str = "COMMENT",
        body: str | None = None,
    ) -> Any:
        """Submit a review (APPROVE / REQUEST_CHANGES / COMMENT / PENDING)."""
        payload: dict[str, Any] = {"event": event}
        if body is not None:
            payload["body"] = body
        return await shim(
            "POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload,
        )

    @mcp.tool()
    async def get_pull_request_files(
        owner: str, repo: str, number: int
    ) -> Any:
        """List files changed by a pull request."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/files",
        )

    @mcp.tool()
    async def get_pull_request_status(
        owner: str, repo: str, number: int
    ) -> Any:
        """Combined commit status for a pull request's head."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/status",
        )

    @mcp.tool()
    async def update_pull_request_branch(
        owner: str, repo: str, number: int
    ) -> Any:
        """Update a pull request branch to track its base."""
        return await shim(
            "PUT", f"/repos/{owner}/{repo}/pulls/{number}/update-branch",
        )

    @mcp.tool()
    async def get_pull_request_comments(
        owner: str, repo: str, number: int
    ) -> Any:
        """List review comments on a pull request."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/comments",
        )

    # ----- Commits & workflows -----------------------------------------

    @mcp.tool()
    async def list_commits(
        owner: str,
        repo: str,
        sha: str | None = None,
        path: str | None = None,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List commits on a repository, optionally filtered by path or sha."""
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        if sha is not None:
            params["sha"] = sha
        if path is not None:
            params["path"] = path
        return await shim(
            "GET", f"/repos/{owner}/{repo}/commits", params=params,
        )

    @mcp.tool()
    async def list_workflow_runs(
        owner: str,
        repo: str,
        status: str | None = None,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List workflow runs for a repository."""
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        if status is not None:
            params["status"] = status
        return await shim(
            "GET", f"/repos/{owner}/{repo}/actions/runs", params=params,
        )

    @mcp.tool()
    async def get_workflow_run(owner: str, repo: str, run_id: int) -> Any:
        """Get a workflow run by id."""
        return await shim(
            "GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}",
        )

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the GitHub FastMCP server and mount it at `/mcp` on `app`."""
    from ._shim import mount_mcp_on_fastapi

    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
