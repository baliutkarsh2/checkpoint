"""GitHub twin: a stateful, in-memory GitHub REST API.

Covers repos, git refs, file contents, commits, issues, comments, labels,
assignees, pull requests (reviews, files, merges), releases, workflows and
search, with GitHub-shaped errors, ``X-GitHub-*``/rate-limit headers and
``Link`` pagination. The control plane and fault model come from
:mod:`checkpoint.twins.kit`.

Three things here exist because the official SDKs depend on them:

* **Hypermedia URLs.** Clients build their next request from the URLs in a
  payload (PyGithub fetches ``issue.url`` to complete an object), so every
  record carries absolute ``url``/``html_url``/``comments_url`` links built
  from the host the request arrived on.
* **One number space.** Issues and pull requests share a repository's numbers
  and the ``/issues/{n}`` endpoints answer for both, exactly as GitHub does —
  commenting on a PR, labelling it and assigning it all go through them.
* **Real trees.** Files live in per-commit trees over a blob store, so contents
  are branch-aware, a stale ``sha`` conflicts, and a pull request's files and
  mergeability are derived from its head and base rather than stored.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import math
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN
from checkpoint.twins import kit, registry

app = FastAPI(title="checkpoint github twin")

DEFAULT_BOOTSTRAP_TOKEN = FAKE_GITHUB_TOKEN
DOC_URL = "https://docs.github.com/rest"
RATE_DOC_URL = (
    "https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting"
)
# Web (not API) host: html_url, diff_url and friends point at it the way they do
# in production, so an agent quoting a link in a comment quotes a plausible one.
HTML_BASE = "https://github.com"
# Hostnames the sandbox's TLS proxy forwards here with the production Host
# header intact; a request that arrives under one of them gets https:// URLs.
PRODUCTION_HOSTS = frozenset(registry.get("github").domains)
# Seconds an exhausted rate-limit budget stays exhausted. Real GitHub hands out
# a fresh allowance at X-RateLimit-Reset; an agent that backs off correctly has
# to be able to make progress, so the window has to be short enough to wait out.
RATE_LIMIT_RESET_S = 2
UNLIMITED = 5000

SEEDS_DIR = Path(__file__).parent / "github_seeds"


def _now() -> str:
    """Timestamp in GitHub's wire format (SDKs parse it as an ISO 8601 date)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh_state() -> dict:
    return {
        "repos": {},          # "owner/name" -> repo (branches, tags, commits, blobs, files)
        "issues": {},         # "owner/name#number" -> issue
        "pulls": {},          # "owner/name#number" -> pull request
        "comments": {},       # str(id) -> issue or review comment
        "labels": {},         # "owner/name/label" -> label
        "releases": {},       # "owner/name#tag" -> release
        "workflow_runs": {},  # "owner/name#run_id" -> run
        "users": {            # login -> user
            "default-user": {"login": "default-user", "id": 1, "type": "User",
                             "name": "Default User"},
        },
        "_counters": {
            "repo_id": 0,
            "issue_id": 0,
            "comment_id": 0,
            "review_id": 0,
            "label_id": 0,
            "release_id": 0,
            "run_id": 0,
            "user_id": 1,
            "sha_seq_per_repo": {},
        },
        # When the exhausted rate-limit budget refills (epoch seconds).
        "_rate": {"reset_at": None},
    }


STATE: dict = _fresh_state()
TRACE: list[dict] = []


# --- errors --------------------------------------------------------------

class _ApiError(Exception):
    """A failure answered with GitHub's own error envelope."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def gh_error(status: int, message: str, *, documentation_url: str = DOC_URL,
             **extra: Any) -> JSONResponse:
    """Return a GitHub-shaped error JSONResponse."""
    body: dict[str, Any] = {"message": message, "documentation_url": documentation_url}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _not_found() -> _ApiError:
    return _ApiError(404, "Not Found")


def _invalid(resource: str, field: str, code: str = "missing_field",
             **extra: Any) -> _ApiError:
    """The 422 envelope GitHub returns for a rejected payload."""
    return _ApiError(422, "Validation Failed",
                     errors=[{"resource": resource, "field": field, "code": code, **extra}])


async def _body(request: Request) -> Any:
    """The request's JSON body, or a GitHub-shaped 400 if it is not JSON."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise _ApiError(400, "Problems parsing JSON") from None


# --- counters, users -----------------------------------------------------

def _next_id(counter: str) -> int:
    counters = STATE["_counters"]
    counters[counter] = int(counters.get(counter, 0)) + 1
    return counters[counter]


def _user(login: str) -> dict:
    """The user record for ``login``, created on first mention."""
    users = STATE["users"]
    if login not in users:
        users[login] = {"login": login, "id": _next_id("user_id"), "type": "User"}
    return users[login]


def _actor() -> dict:
    """Who the twin attributes writes to: the token's owner."""
    return _user("default-user")


# --- git object model ----------------------------------------------------
#
# A repo keeps a blob store (content by git blob sha) and one tree per commit
# ({path: blob sha}). A branch is a name pointing at a commit sha. That is
# enough for branch-aware contents, stale-sha conflicts, three-way merges and
# per-branch history, and it keeps every file version addressable by its sha.

def _blob_sha(content: str) -> str:
    """Git's own blob id, so identical content always has the same sha."""
    data = content.encode()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()  # noqa: S324


def _commit_sha(repo_key: str) -> str:
    seq_map = STATE["_counters"]["sha_seq_per_repo"]
    seq_map[repo_key] = seq_map.get(repo_key, 0) + 1
    return hashlib.sha1(f"{repo_key}#{seq_map[repo_key]}#{time.time()}".encode()).hexdigest()  # noqa: S324


def _put_blob(repo: dict, content: str) -> str:
    sha = _blob_sha(content)
    repo["blobs"][sha] = content
    return sha


def _blob(repo: dict, sha: str) -> str:
    return repo["blobs"].get(sha, "")


def _commits_by_sha(repo: dict) -> dict[str, dict]:
    return {c["sha"]: c for c in repo["commits"]}


def _add_commit(repo: dict, *, message: str, tree: dict[str, str], parents: list[str],
                files: list[dict], author: dict | None = None) -> dict:
    """Record a commit and return it (newest first, as GitHub lists them)."""
    who = author or {"name": _actor()["login"], "email": f"{_actor()['login']}@users.noreply.github.com"}
    commit = {
        "sha": _commit_sha(repo["full_name"]),
        "commit": {
            "message": message,
            "author": {**who, "date": _now()},
            "committer": {**who, "date": _now()},
        },
        "parents": parents,
        "files": files,
        "tree": dict(tree),
    }
    repo["commits"].insert(0, commit)
    return commit


def _materialize(repo: dict) -> None:
    """Refresh ``repo["files"]`` — the default branch's tree, for state readers."""
    tree = _tree_of(repo, repo["default_branch"]) or {}
    repo["files"] = {
        path: {"content": _blob(repo, sha), "sha": sha} for path, sha in sorted(tree.items())
    }


def _move_branch(repo: dict, branch: str, sha: str) -> None:
    repo["branches"].setdefault(branch, {"name": branch, "protected": False})["sha"] = sha
    repo["pushed_at"] = _now()
    if branch == repo["default_branch"]:
        _materialize(repo)


def _resolve(repo: dict, ref: str | None) -> str | None:
    """Resolve a branch name, tag or commit-ish to a commit sha."""
    if not ref:
        ref = repo["default_branch"]
    ref = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    if ref in repo["branches"]:
        return repo["branches"][ref]["sha"]
    if ref in repo.get("tags", {}):
        return repo["tags"][ref]["sha"]
    commits = _commits_by_sha(repo)
    if ref in commits:
        return ref
    matches = [sha for sha in commits if sha.startswith(ref)] if len(ref) >= 7 else []
    return matches[0] if len(matches) == 1 else None


def _tree_at(repo: dict, sha: str | None) -> dict[str, str] | None:
    if sha is None:
        return None
    commit = _commits_by_sha(repo).get(sha)
    return dict(commit.get("tree") or {}) if commit else None


def _tree_of(repo: dict, ref: str | None) -> dict[str, str] | None:
    return _tree_at(repo, _resolve(repo, ref))


def _ancestors(repo: dict, sha: str | None) -> list[str]:
    """Commit shas reachable from ``sha``, nearest first."""
    commits = _commits_by_sha(repo)
    seen: list[str] = []
    queue = [sha] if sha else []
    while queue:
        current = queue.pop(0)
        if current is None or current in seen or current not in commits:
            continue
        seen.append(current)
        queue.extend(commits[current].get("parents") or [])
    return seen


def _merge_base(repo: dict, head: str | None, base: str | None) -> str | None:
    base_line = set(_ancestors(repo, base))
    return next((sha for sha in _ancestors(repo, head) if sha in base_line), None)


def _diff_trees(repo: dict, before: dict[str, str], after: dict[str, str]) -> list[dict]:
    """GitHub's file-change entries between two trees."""
    out: list[dict] = []
    for path in sorted(set(before) | set(after)):
        old_sha, new_sha = before.get(path), after.get(path)
        if old_sha == new_sha:
            continue
        old = _blob(repo, old_sha) if old_sha else ""
        new = _blob(repo, new_sha) if new_sha else ""
        patch = _patch(path, old, new)
        out.append({
            "sha": new_sha or old_sha,
            "filename": path,
            "status": "added" if old_sha is None else "removed" if new_sha is None else "modified",
            "additions": sum(1 for line in patch.splitlines() if line.startswith("+")),
            "deletions": sum(1 for line in patch.splitlines() if line.startswith("-")),
            "changes": sum(1 for line in patch.splitlines() if line[:1] in "+-"),
            "patch": patch,
        })
    return out


def _patch(path: str, before: str, after: str) -> str:
    """A unified diff body (hunks only, as GitHub's ``patch`` field carries)."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    )
    return "".join(line for line in lines if not line.startswith(("---", "+++")))


# --- repositories, issues, pull requests ---------------------------------

def _repo_key(owner: str, name: str) -> str:
    return f"{owner}/{name}"


def _repo(owner: str, name: str) -> dict:
    repo = STATE["repos"].get(_repo_key(owner, name))
    if repo is None:
        raise _not_found()
    return repo


def _make_repo(owner: str, name: str, *, private: bool = False, description: str | None = None,
               auto_init: bool = False, org: bool = False) -> dict:
    repo_key = _repo_key(owner, name)
    owner_rec = _user(owner)
    if org:
        owner_rec["type"] = "Organization"
    repo = {
        "id": _next_id("repo_id"),
        "name": name,
        "full_name": repo_key,
        "owner": owner_rec,
        "default_branch": "main",
        "private": private,
        "description": description,
        "fork": False,
        "created_at": _now(),
        "updated_at": _now(),
        "pushed_at": _now(),
        "branches": {},
        "tags": {},
        "commits": [],
        "blobs": {},
        "files": {},
    }
    STATE["repos"][repo_key] = repo
    tree: dict[str, str] = {}
    files: list[dict] = []
    if auto_init:
        content = f"# {name}\n" + (f"\n{description}\n" if description else "")
        sha = _put_blob(repo, content)
        tree["README.md"] = sha
        files = [{"filename": "README.md", "status": "added", "sha": sha}]
    commit = _add_commit(repo, message="Initial commit", tree=tree, parents=[], files=files)
    _move_branch(repo, "main", commit["sha"])
    return repo


def _next_number(repo_key: str) -> int:
    """Issues and pull requests share one number space per repository."""
    used = [
        record["number"]
        for store in ("issues", "pulls")
        for key, record in STATE[store].items()
        if key.startswith(f"{repo_key}#")
    ]
    return max(used, default=0) + 1


def _is_pull(record: dict) -> bool:
    return "head" in record


def _issue_like(repo_key: str, number: int) -> dict:
    """The issue *or* pull request numbered ``number``; GitHub's issue endpoints serve both."""
    key = f"{repo_key}#{number}"
    record = STATE["issues"].get(key) or STATE["pulls"].get(key)
    if record is None:
        raise _not_found()
    return record


def _repo_records(store: str, repo_key: str) -> list[dict]:
    return [v for k, v in STATE[store].items() if k.startswith(f"{repo_key}#")]


def _repo_of(record: dict) -> tuple[str, dict]:
    """The ``owner/name`` and repo record an issue or pull request belongs to."""
    repo_key = record["_repo"]
    return repo_key, STATE["repos"][repo_key]


def _touch(record: dict) -> None:
    record["updated_at"] = _now()


def _label_records(repo_key: str, names: list[str], *, color: str = "ededed") -> list[dict]:
    """Labels by name, creating any the repository doesn't have yet (as GitHub does)."""
    out = []
    for name in names:
        key = f"{repo_key}/{name}"
        label = STATE["labels"].get(key)
        if label is None:
            label = {"id": _next_id("label_id"), "name": name, "color": color,
                     "default": False, "description": None}
            STATE["labels"][key] = label
        out.append(label)
    return out


def _label_names(payload: Any) -> list[str]:
    """Label names from the several shapes clients send (list, {"labels": [...]}, objects)."""
    names = payload.get("labels") if isinstance(payload, dict) else payload
    if not isinstance(names, list):
        raise _invalid("Issue", "labels", "invalid")
    out = []
    for item in names:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str):
            raise _invalid("Issue", "labels", "invalid")
        out.append(name)
    return out


def _assignee_records(logins: list[str]) -> list[dict]:
    return [_user(login) for login in dict.fromkeys(logins) if isinstance(login, str)]


def _pull_head_sha(repo: dict, pull: dict) -> str:
    """An open PR tracks its head branch; a merged one is frozen at its merge."""
    if pull["state"] == "open":
        branch = repo["branches"].get(pull["head"]["ref"])
        if branch:
            return branch["sha"]
    return pull["head"]["sha"]


def _pull_base_sha(repo: dict, pull: dict) -> str:
    branch = repo["branches"].get(pull["base"]["ref"])
    return branch["sha"] if branch and pull["state"] == "open" else pull["base"]["sha"]


def _pull_files(repo: dict, pull: dict) -> list[dict]:
    head = _pull_head_sha(repo, pull)
    base = _pull_base_sha(repo, pull)
    fork = _merge_base(repo, head, base) or base
    return _diff_trees(repo, _tree_at(repo, fork) or {}, _tree_at(repo, head) or {})


def _pull_commits(repo: dict, pull: dict) -> list[dict]:
    """Commits on the head branch that the base doesn't already have."""
    head = _pull_head_sha(repo, pull)
    base = set(_ancestors(repo, _pull_base_sha(repo, pull)))
    commits = _commits_by_sha(repo)
    return [commits[sha] for sha in _ancestors(repo, head) if sha not in base and sha in commits]


def _merge_result(repo: dict, pull: dict) -> tuple[dict[str, str] | None, list[str]]:
    """The tree a merge would produce, plus the paths that conflict."""
    head, base = _pull_head_sha(repo, pull), _pull_base_sha(repo, pull)
    fork = _merge_base(repo, head, base) or base
    fork_tree = _tree_at(repo, fork) or {}
    head_tree, base_tree = _tree_at(repo, head) or {}, _tree_at(repo, base) or {}
    merged = dict(base_tree)
    conflicts = []
    for path in set(fork_tree) | set(head_tree):
        theirs, ours, origin = head_tree.get(path), base_tree.get(path), fork_tree.get(path)
        if theirs == origin:
            continue  # untouched on the head branch
        if ours != origin and ours != theirs:
            conflicts.append(path)
            continue
        if theirs is None:
            merged.pop(path, None)
        else:
            merged[path] = theirs
    return (None, sorted(conflicts)) if conflicts else (merged, [])


def _mergeable(repo: dict, pull: dict) -> bool | None:
    if pull["state"] != "open":
        return None
    return not _merge_result(repo, pull)[1]


# --- wire rendering ------------------------------------------------------

def _api_base(request: Request) -> str:
    """The API root as the caller addressed it.

    SDKs build follow-up requests out of the URLs in a payload — PyGithub GETs
    ``issue.url`` to complete an object — so those URLs must name the host the
    request came in on: the twin's own address when it is called directly, and
    ``https://api.github.com`` behind the sandbox's TLS proxy, which forwards
    the production Host header.
    """
    host = request.url.hostname or ""
    if host in PRODUCTION_HOSTS:
        return f"https://{host}"
    return str(request.base_url).rstrip("/")


def _user_json(base: str, user: dict | str | None) -> dict | None:
    if user is None:
        return None
    record = _user(user) if isinstance(user, str) else user
    login = record["login"]
    return {
        "login": login,
        "id": record.get("id"),
        "node_id": f"U_{record.get('id')}",
        "name": record.get("name"),
        "email": record.get("email"),
        "avatar_url": f"https://avatars.githubusercontent.com/u/{record.get('id')}?v=4",
        "gravatar_id": "",
        "url": f"{base}/users/{login}",
        "html_url": f"{HTML_BASE}/{login}",
        "repos_url": f"{base}/users/{login}/repos",
        "organizations_url": f"{base}/users/{login}/orgs",
        "type": record.get("type", "User"),
        "site_admin": False,
    }


def _repo_json(base: str, repo: dict) -> dict:
    key = repo["full_name"]
    url = f"{base}/repos/{key}"
    open_issues = sum(
        1 for store in ("issues", "pulls") for record in _repo_records(store, key)
        if record["state"] == "open"
    )
    return {
        "id": repo["id"],
        "node_id": f"R_{repo['id']}",
        "name": repo["name"],
        "full_name": key,
        "private": repo.get("private", False),
        "owner": _user_json(base, repo["owner"]),
        "html_url": f"{HTML_BASE}/{key}",
        "description": repo.get("description"),
        "fork": repo.get("fork", False),
        "url": url,
        "archive_url": url + "/{archive_format}{/ref}",
        "branches_url": url + "/branches{/branch}",
        "commits_url": url + "/commits{/sha}",
        "contents_url": url + "/contents/{+path}",
        "git_refs_url": url + "/git/refs{/sha}",
        "issues_url": url + "/issues{/number}",
        "labels_url": url + "/labels{/name}",
        "pulls_url": url + "/pulls{/number}",
        "releases_url": url + "/releases{/id}",
        "clone_url": f"{HTML_BASE}/{key}.git",
        "ssh_url": f"git@github.com:{key}.git",
        "created_at": repo.get("created_at"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "default_branch": repo["default_branch"],
        "visibility": "private" if repo.get("private") else "public",
        "archived": False,
        "disabled": False,
        "open_issues_count": open_issues,
        "open_issues": open_issues,
        "topics": repo.get("topics", []),
        "has_issues": True,
        "has_wiki": True,
        "has_projects": True,
        "has_downloads": True,
        "forks_count": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "size": len(repo.get("files", {})),
        "permissions": {"admin": True, "maintain": True, "push": True, "triage": True, "pull": True},
        "parent": repo.get("parent"),
    }


def _label_json(base: str, repo_key: str, label: dict | str) -> dict:
    record = label if isinstance(label, dict) else {"name": label}
    name = record["name"]
    stored = STATE["labels"].get(f"{repo_key}/{name}") or record
    return {
        "id": stored.get("id"),
        "node_id": f"LA_{stored.get('id')}",
        "url": f"{base}/repos/{repo_key}/labels/{name}",
        "name": name,
        "color": stored.get("color", "ededed"),
        "default": stored.get("default", False),
        "description": stored.get("description"),
    }


def _issue_json(base: str, record: dict) -> dict:
    """An issue — or a pull request seen through the issues endpoints."""
    repo_key = record["_repo"]
    number = record["number"]
    url = f"{base}/repos/{repo_key}/issues/{number}"
    is_pull = _is_pull(record)
    html_url = f"{HTML_BASE}/{repo_key}/{'pull' if is_pull else 'issues'}/{number}"
    assignees = [_user_json(base, a) for a in record.get("assignees") or []]
    out = {
        "id": record["id"],
        "node_id": f"I_{record['id']}",
        "url": url,
        "repository_url": f"{base}/repos/{repo_key}",
        "labels_url": url + "/labels{/name}",
        "comments_url": url + "/comments",
        "events_url": url + "/events",
        "html_url": html_url,
        "number": number,
        "state": record["state"],
        "state_reason": record.get("state_reason"),
        "title": record["title"],
        "body": record.get("body") or "",
        "user": _user_json(base, record.get("user")),
        "labels": [_label_json(base, repo_key, lab) for lab in record.get("labels") or []],
        "assignee": assignees[0] if assignees else None,
        "assignees": assignees,
        "milestone": None,
        "locked": record.get("locked", False),
        "active_lock_reason": None,
        "comments": record.get("comments", 0),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "closed_at": record.get("closed_at"),
        "author_association": "OWNER",
        "draft": record.get("draft", False),
    }
    if is_pull:
        out["pull_request"] = {
            "url": f"{base}/repos/{repo_key}/pulls/{number}",
            "html_url": html_url,
            "diff_url": f"{html_url}.diff",
            "patch_url": f"{html_url}.patch",
            "merged_at": record.get("merged_at"),
        }
    return out


def _pull_ref_json(base: str, repo: dict, ref: str, sha: str) -> dict:
    return {
        "label": f"{repo['owner']['login']}:{ref}",
        "ref": ref,
        "sha": sha,
        "user": _user_json(base, repo["owner"]),
        "repo": _repo_json(base, repo),
    }


def _pull_json(base: str, pull: dict) -> dict:
    repo_key, repo = _repo_of(pull)
    number = pull["number"]
    url = f"{base}/repos/{repo_key}/pulls/{number}"
    issue_url = f"{base}/repos/{repo_key}/issues/{number}"
    html_url = f"{HTML_BASE}/{repo_key}/pull/{number}"
    head_sha, base_sha = _pull_head_sha(repo, pull), _pull_base_sha(repo, pull)
    files = _pull_files(repo, pull)
    mergeable = _mergeable(repo, pull)
    assignees = [_user_json(base, a) for a in pull.get("assignees") or []]
    return {
        "id": pull["id"],
        "node_id": f"PR_{pull['id']}",
        "url": url,
        "html_url": html_url,
        "diff_url": f"{html_url}.diff",
        "patch_url": f"{html_url}.patch",
        "issue_url": issue_url,
        "commits_url": url + "/commits",
        "review_comments_url": url + "/comments",
        "review_comment_url": f"{base}/repos/{repo_key}/pulls/comments" + "{/number}",
        "comments_url": issue_url + "/comments",
        "statuses_url": f"{base}/repos/{repo_key}/statuses/{head_sha}",
        "number": number,
        "state": pull["state"],
        "locked": pull.get("locked", False),
        "title": pull["title"],
        "user": _user_json(base, pull.get("user")),
        "body": pull.get("body") or "",
        "labels": [_label_json(base, repo_key, lab) for lab in pull.get("labels") or []],
        "milestone": None,
        "active_lock_reason": None,
        "created_at": pull.get("created_at"),
        "updated_at": pull.get("updated_at"),
        "closed_at": pull.get("closed_at"),
        "merged_at": pull.get("merged_at"),
        "merge_commit_sha": pull.get("merge_commit_sha"),
        "assignee": assignees[0] if assignees else None,
        "assignees": assignees,
        "requested_reviewers": [_user_json(base, r) for r in pull.get("requested_reviewers") or []],
        "requested_teams": [],
        "head": _pull_ref_json(base, repo, pull["head"]["ref"], head_sha),
        "base": _pull_ref_json(base, repo, pull["base"]["ref"], base_sha),
        "_links": {"self": {"href": url}, "html": {"href": html_url},
                   "issue": {"href": issue_url}, "comments": {"href": issue_url + "/comments"}},
        "author_association": "OWNER",
        "draft": pull.get("draft", False),
        "merged": pull.get("merged", False),
        "mergeable": mergeable,
        "rebaseable": mergeable,
        "mergeable_state": ("unknown" if mergeable is None
                            else "draft" if pull.get("draft")
                            else "clean" if mergeable else "dirty"),
        "merged_by": _user_json(base, pull.get("merged_by")),
        "comments": pull.get("comments", 0),
        "review_comments": sum(1 for c in STATE["comments"].values()
                               if c.get("_issue") == f"{repo_key}#{number}"
                               and c.get("_kind") == "review"),
        "maintainer_can_modify": True,
        "commits": len(_pull_commits(repo, pull)),
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "changed_files": len(files),
    }


def _comment_json(base: str, comment: dict) -> dict:
    repo_key, _, number = comment["_issue"].partition("#")
    review = comment.get("_kind") == "review"
    kind = "pulls" if review else "issues"
    url = f"{base}/repos/{repo_key}/{kind}/comments/{comment['id']}"
    html_url = f"{HTML_BASE}/{repo_key}/{'pull' if review else 'issues'}/{number}#issuecomment-{comment['id']}"
    out = {
        "id": comment["id"],
        "node_id": f"IC_{comment['id']}",
        "url": url,
        "html_url": html_url,
        "body": comment.get("body") or "",
        "user": _user_json(base, comment.get("user")),
        "created_at": comment.get("created_at"),
        "updated_at": comment.get("updated_at"),
        "author_association": "OWNER",
        "issue_url": f"{base}/repos/{repo_key}/issues/{number}",
    }
    if review:
        out.update({
            "pull_request_url": f"{base}/repos/{repo_key}/pulls/{number}",
            "pull_request_review_id": comment.get("review_id"),
            "path": comment.get("path"),
            "line": comment.get("line"),
            "side": comment.get("side", "RIGHT"),
            "position": comment.get("position"),
            "commit_id": comment.get("commit_id"),
            "diff_hunk": comment.get("diff_hunk", ""),
            "in_reply_to_id": comment.get("in_reply_to_id"),
            "_links": {"self": {"href": url}, "html": {"href": html_url}},
        })
    return out


def _review_json(base: str, repo_key: str, number: int, review: dict) -> dict:
    return {
        "id": review["id"],
        "node_id": f"PRR_{review['id']}",
        "user": _user_json(base, review.get("user")),
        "body": review.get("body") or "",
        "state": review["state"],
        "html_url": f"{HTML_BASE}/{repo_key}/pull/{number}#pullrequestreview-{review['id']}",
        "pull_request_url": f"{base}/repos/{repo_key}/pulls/{number}",
        "submitted_at": review.get("submitted_at"),
        "commit_id": review.get("commit_id"),
        "author_association": "OWNER",
        "_links": {"html": {"href": f"{HTML_BASE}/{repo_key}/pull/{number}"}},
    }


def _commit_json(base: str, repo_key: str, commit: dict, *, with_files: bool = False) -> dict:
    sha = commit["sha"]
    url = f"{base}/repos/{repo_key}/commits/{sha}"
    author = commit["commit"].get("author") or {}
    out = {
        "sha": sha,
        "node_id": f"C_{sha[:8]}",
        "url": url,
        "html_url": f"{HTML_BASE}/{repo_key}/commit/{sha}",
        "comments_url": url + "/comments",
        "commit": {
            "message": commit["commit"].get("message", ""),
            "author": author,
            "committer": commit["commit"].get("committer") or author,
            "url": f"{base}/repos/{repo_key}/git/commits/{sha}",
            "tree": {"sha": sha, "url": f"{base}/repos/{repo_key}/git/trees/{sha}"},
            "comment_count": 0,
        },
        "author": _user_json(base, author.get("name") or "default-user"),
        "committer": _user_json(base, author.get("name") or "default-user"),
        "parents": [{"sha": p, "url": f"{base}/repos/{repo_key}/commits/{p}"}
                    for p in commit.get("parents") or []],
    }
    if with_files:
        files = commit.get("files") or []
        out["files"] = files
        out["stats"] = {
            "additions": sum(f.get("additions", 0) for f in files),
            "deletions": sum(f.get("deletions", 0) for f in files),
            "total": sum(f.get("changes", 0) for f in files),
        }
    return out


def _content_json(base: str, repo: dict, path: str, sha: str, ref: str, *,
                  with_content: bool = True) -> dict:
    repo_key = repo["full_name"]
    content = _blob(repo, sha)
    url = f"{base}/repos/{repo_key}/contents/{path}?ref={ref}"
    html_url = f"{HTML_BASE}/{repo_key}/blob/{ref}/{path}"
    out = {
        "type": "file",
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": sha,
        "size": len(content.encode()),
        "url": url,
        "html_url": html_url,
        "git_url": f"{base}/repos/{repo_key}/git/blobs/{sha}",
        "download_url": f"https://raw.githubusercontent.com/{repo_key}/{ref}/{path}",
        "_links": {"self": url, "git": f"{base}/repos/{repo_key}/git/blobs/{sha}", "html": html_url},
    }
    if with_content:
        out["encoding"] = "base64"
        out["content"] = base64.b64encode(content.encode()).decode()
    return out


def _ref_json(base: str, repo_key: str, ref: str, sha: str) -> dict:
    return {
        "ref": f"refs/{ref}",
        "node_id": f"REF_{sha[:8]}",
        "url": f"{base}/repos/{repo_key}/git/refs/{ref}",
        "object": {"type": "commit", "sha": sha,
                   "url": f"{base}/repos/{repo_key}/git/commits/{sha}"},
    }


def _branch_json(base: str, repo: dict, branch: dict) -> dict:
    repo_key = repo["full_name"]
    commit = _commits_by_sha(repo).get(branch["sha"])
    commit_json = (_commit_json(base, repo_key, commit) if commit
                   else {"sha": branch["sha"],
                         "url": f"{base}/repos/{repo_key}/commits/{branch['sha']}"})
    return {
        "name": branch["name"],
        "commit": commit_json,
        "protected": branch.get("protected", False),
        "protection": {"enabled": branch.get("protected", False),
                       "required_status_checks": {"enforcement_level": "off", "contexts": []}},
        "protection_url": f"{base}/repos/{repo_key}/branches/{branch['name']}/protection",
        "_links": {"self": f"{base}/repos/{repo_key}/branches/{branch['name']}",
                   "html": f"{HTML_BASE}/{repo_key}/tree/{branch['name']}"},
    }


def _release_json(base: str, release: dict) -> dict:
    repo_key = release["_repo"]
    url = f"{base}/repos/{repo_key}/releases/{release['id']}"
    return {
        "id": release["id"],
        "node_id": f"RE_{release['id']}",
        "url": url,
        "html_url": f"{HTML_BASE}/{repo_key}/releases/tag/{release['tag_name']}",
        "assets_url": url + "/assets",
        "upload_url": f"{base}/repos/{repo_key}/releases/{release['id']}/assets" + "{?name,label}",
        "tarball_url": f"{base}/repos/{repo_key}/tarball/{release['tag_name']}",
        "zipball_url": f"{base}/repos/{repo_key}/zipball/{release['tag_name']}",
        "tag_name": release["tag_name"],
        "target_commitish": release.get("target_commitish", "main"),
        "name": release.get("name"),
        "body": release.get("body") or "",
        "draft": release.get("draft", False),
        "prerelease": release.get("prerelease", False),
        "created_at": release.get("created_at"),
        "published_at": release.get("published_at"),
        "author": _user_json(base, release.get("author")),
        "assets": [],
    }


def _workflows(repo: dict) -> list[dict]:
    """Workflows are the ``.github/workflows`` files on the default branch."""
    out = []
    for path, entry in sorted((repo.get("files") or {}).items()):
        if not re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path):
            continue
        match = re.search(r"^name:\s*(.+)$", entry["content"], re.MULTILINE)
        out.append({
            "id": int(hashlib.sha1(path.encode()).hexdigest()[:8], 16),  # noqa: S324
            "name": (match.group(1).strip().strip("'\"") if match else path.rsplit("/", 1)[-1]),
            "path": path,
            "state": "active",
        })
    return out


def _workflow_json(base: str, repo_key: str, workflow: dict) -> dict:
    return {
        "id": workflow["id"],
        "node_id": f"W_{workflow['id']}",
        "name": workflow["name"],
        "path": workflow["path"],
        "state": workflow["state"],
        "created_at": _now(),
        "updated_at": _now(),
        "url": f"{base}/repos/{repo_key}/actions/workflows/{workflow['id']}",
        "html_url": f"{HTML_BASE}/{repo_key}/blob/main/{workflow['path']}",
        "badge_url": f"{HTML_BASE}/{repo_key}/workflows/{workflow['name']}/badge.svg",
    }


def _run_json(base: str, repo_key: str, run: dict) -> dict:
    return {
        **{k: v for k, v in run.items() if not k.startswith("_")},
        "url": f"{base}/repos/{repo_key}/actions/runs/{run['id']}",
        "html_url": run.get("html_url") or f"{HTML_BASE}/{repo_key}/actions/runs/{run['id']}",
        "jobs_url": f"{base}/repos/{repo_key}/actions/runs/{run['id']}/jobs",
        "rerun_url": f"{base}/repos/{repo_key}/actions/runs/{run['id']}/rerun",
        "cancel_url": f"{base}/repos/{repo_key}/actions/runs/{run['id']}/cancel",
        "repository": _repo_json(base, STATE["repos"][repo_key]) if repo_key in STATE["repos"] else None,
    }


# --- pagination ----------------------------------------------------------

def _paginated(items: list, request: Request, per_page: int = 30, page: int = 1, *,
               envelope: str | None = None, search: bool = False) -> JSONResponse:
    """One page of ``items`` plus a GitHub-style ``Link`` header.

    The official SDKs (Octokit, PyGithub) follow ``Link: ...; rel="next"`` to
    auto-paginate; without it they stop at page 1 and an agent silently works
    from a truncated list.
    """
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    total = len(items)
    last = max(1, math.ceil(total / per_page))
    window = items[(page - 1) * per_page: page * per_page]
    base = f"{_api_base(request)}{request.url.path}"

    def link(target: int) -> str:
        params = {**dict(request.query_params), "page": target, "per_page": per_page}
        return f"<{base}?{urlencode(params)}>"

    rels: list[str] = []
    if page < last:
        rels += [f'{link(page + 1)}; rel="next"', f'{link(last)}; rel="last"']
    if page > 1:
        rels += [f'{link(page - 1)}; rel="prev"', f'{link(1)}; rel="first"']
    headers = {"Link": ", ".join(rels)} if rels else None
    if envelope is None:
        content: Any = window
    else:
        content = {"total_count": total, envelope: window}
        if search:
            content["incomplete_results"] = False
    return JSONResponse(content=content, headers=headers)


# --- search --------------------------------------------------------------

_QUALIFIER = re.compile(r'(-?)([A-Za-z_]+):("[^"]*"|\S+)')


def _parse_query(q: str) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """Split a search query into ``(qualifier, value, negated)`` and free-text terms."""
    qualifiers = [(name.lower(), value.strip('"'), bool(neg))
                  for neg, name, value in _QUALIFIER.findall(q)]
    free = _QUALIFIER.sub(" ", q)
    terms = [t.strip('"').lower() for t in re.findall(r'"[^"]*"|\S+', free) if t.strip('"')]
    return qualifiers, terms


def _matches_search(record: dict, repo_key: str, qualifiers: list[tuple[str, str, bool]],
                    terms: list[str]) -> bool:
    """Apply GitHub's issue-search qualifiers to one issue or pull request."""
    fields = {"title": record["title"], "body": record.get("body") or ""}
    labels = {lab["name"].lower() for lab in record.get("labels") or []}
    assignees = {a["login"].lower() for a in record.get("assignees") or []}
    author = (record.get("user") or {}).get("login", "").lower()
    in_fields: list[str] = []
    for name, value, negated in qualifiers:
        value = value.lower()
        hit: bool
        if name == "repo":
            hit = repo_key.lower() == value
        elif name in ("user", "org", "owner"):
            hit = repo_key.split("/")[0].lower() == value
        elif name in ("is", "state", "type"):
            if value in ("issue", "pull-request", "pr"):
                hit = _is_pull(record) == (value != "issue")
            elif value == "merged":
                hit = bool(record.get("merged"))
            elif value == "draft":
                hit = bool(record.get("draft"))
            elif value in ("open", "closed"):
                hit = record["state"] == value
            else:
                continue  # is:public / is:locked and friends: not modelled
        elif name == "label":
            hit = value in labels
        elif name == "author":
            hit = author == value
        elif name == "assignee":
            hit = (not assignees) if value == "none" else value in assignees
        elif name == "no":
            hit = not labels if value == "label" else not assignees if value == "assignee" else True
        elif name == "in":
            in_fields.append(value)
            continue
        else:
            continue  # unknown qualifiers narrow nothing, as on GitHub
        if hit == negated:
            return False
    haystack = " ".join(fields[f] for f in (in_fields or ["title", "body"]) if f in fields).lower()
    return all(term in haystack for term in terms)


# --- runtime: auth, faults, rate limit, classification, views ------------

def _extract_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    auth_header = auth_header.strip()
    for prefix in ("token ", "Bearer ", "bearer "):
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):].strip()
    return None


def _bootstrap_token() -> str:
    return os.environ.get("GITHUB_BOOTSTRAP_TOKEN", DEFAULT_BOOTSTRAP_TOKEN)


def _authenticate(request: Request) -> Response | None:
    token = _extract_token(request.headers.get("authorization"))
    if token is None:
        return gh_error(401, "Requires authentication")
    if TWIN.config.get("strict_auth") and token != _bootstrap_token():
        return gh_error(401, "Bad credentials")
    return None


def _rate_snapshot() -> tuple[int, int, int, int]:
    """``(limit, remaining, used, reset epoch)`` for the current window."""
    configured = TWIN.config.get("rate_limit")
    limit = UNLIMITED if configured is None else int(configured)
    used = min(TWIN.requests, limit)
    reset_at = STATE.get("_rate", {}).get("reset_at")
    if reset_at is None:
        reset_at = time.time() + (RATE_LIMIT_RESET_S if configured is not None else 3600)
    return limit, max(0, limit - TWIN.requests), used, int(math.ceil(reset_at))


def _gh_headers(extra: dict | None = None) -> dict:
    limit, remaining, used, reset = _rate_snapshot()
    headers = {
        "X-GitHub-Media-Type": "github.v3; format=json",
        "X-GitHub-Api-Version": "2022-11-28",
        "X-GitHub-Request-Id": uuid.uuid4().hex[:16].upper(),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Used": str(used),
        "X-RateLimit-Reset": str(reset),
        "X-RateLimit-Resource": "core",
    }
    if extra:
        headers.update(extra)
    return headers


def _error(kind: str, status: int, message: str) -> Response:
    """Shape the kit's uniform faults the way GitHub reports them."""
    if kind == "rate_limited":
        rate = STATE.setdefault("_rate", {"reset_at": None})
        if rate.get("reset_at") is None:
            rate["reset_at"] = time.time() + RATE_LIMIT_RESET_S
        retry_after = max(1, int(math.ceil(rate["reset_at"] - time.time())))
        return JSONResponse(
            status_code=status,
            content={
                "message": "API rate limit exceeded for user ID 1.",
                "documentation_url": RATE_DOC_URL,
            },
            headers=_gh_headers({"Retry-After": str(retry_after), "X-RateLimit-Remaining": "0"}),
        )
    if kind == "forbidden":
        message = "Resource not accessible by integration"
    return gh_error(status, message)


def _stamp_headers(request: Request, response: Response) -> None:
    for key, value in _gh_headers().items():
        response.headers.setdefault(key, value)


_VERB_OPS: dict[str, kit.Op] = {
    "GET": "read", "HEAD": "read", "POST": "create", "PUT": "update",
    "PATCH": "update", "DELETE": "delete",
}
_SEARCH_RESOURCES = {"issues": "issues", "repositories": "repos", "code": "files",
                     "users": "users", "commits": "commits", "labels": "labels"}


def _classify(method: str, path: str, body: Any) -> tuple[kit.Op, str] | None:
    """Name what an endpoint touched, where the verb and path alone mislead.

    GitHub routes several resources through one path: labelling an issue is a
    POST under ``/issues/{n}/labels`` — an update of the issue, not a new label
    — and a commit arrives as a PUT to ``/contents/{path}``.
    """
    segments = [s for s in path.strip("/").split("/") if s]
    verb = _VERB_OPS.get(method.upper(), "other")
    if not segments:
        return None
    if segments[0] == "search":
        return "read", _SEARCH_RESOURCES.get(segments[1] if len(segments) > 1 else "", "search")
    if segments[0] == "rate_limit":
        return "read", "rate_limit"
    if segments[0] in ("user", "users", "orgs"):
        return (verb, "repos") if segments[-1] == "repos" else ("read", "users")
    if segments[0] != "repos" or len(segments) < 3:
        return None
    tail = segments[3:]
    if not tail:
        return verb, "repos"
    head = tail[0]
    if head in ("forks", "transfer"):
        return "create", "repos"
    if head == "issues":
        if "labels" in tail:
            return ("read", "labels") if verb == "read" else ("update", "issues")
        if "assignees" in tail:
            return "update", "issues"
        if "comments" in tail:
            return verb, "comments"
        return verb, "issues"
    if head == "pulls":
        if "comments" in tail:
            return verb, "comments"
        if "reviews" in tail:
            return verb, "reviews"
        if tail[-1] in ("merge", "update-branch", "requested_reviewers"):
            return ("read" if verb == "read" else "update"), "pulls"
        if tail[-1] == "files":
            return "read", "files"
        if tail[-1] == "commits":
            return "read", "commits"
        return verb, "pulls"
    if head == "labels":
        return verb, "labels"
    if head in ("contents", "readme", "_push_files"):
        if head == "contents" and method.upper() == "PUT":
            return ("update" if isinstance(body, dict) and body.get("sha") else "create"), "files"
        if head == "_push_files":
            return "update", "files"
        return verb, "files"
    if head == "git":
        kind = "tags" if "tags" in tail else "branches"
        return verb, kind
    if head == "branches":
        return "read", "branches"
    if head == "commits":
        return "read", "commits"
    if head == "releases":
        return verb, "releases"
    if head == "actions":
        if tail[1:2] == ["workflows"]:
            return ("create", "workflow_runs") if tail[-1] == "dispatches" else ("read", "workflows")
        if tail[-1] in ("rerun", "cancel"):
            return "update", "workflow_runs"
        return verb, "workflow_runs"
    return None


def _seed_content(entry: Any) -> str:
    """File content from a seed, written either as ``{"content": ...}`` or bare."""
    return str(entry.get("content", "") if isinstance(entry, dict) else entry)


def _records(state: dict, name: str) -> dict[str, dict]:
    """A state collection, skipping anything a hand-written seed left malformed."""
    raw = state.get(name)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _views(state: dict) -> dict[str, kit.View]:
    """Normalized collections, so assertions never walk the nested state."""
    repos = _records(state, "repos")
    branches, files, commits = [], [], []
    for key, repo in repos.items():
        for name, branch in (repo.get("branches") or {}).items():
            branches.append({"key": f"{key}:{name}", "repo": key, "name": name,
                             "sha": branch.get("sha"), "protected": branch.get("protected", False),
                             "default": name == repo.get("default_branch")})
        for path, entry in (repo.get("files") or {}).items():
            files.append({"key": f"{key}:{path}", "repo": key, "path": path,
                          "branch": repo.get("default_branch"), "sha": entry.get("sha"),
                          "content": entry.get("content", "")})
        for commit in repo.get("commits") or []:
            commits.append({"sha": commit.get("sha"), "repo": key,
                            "message": commit.get("commit", {}).get("message", ""),
                            "author": (commit.get("commit", {}).get("author") or {}).get("name"),
                            "date": (commit.get("commit", {}).get("author") or {}).get("date"),
                            "files": [f.get("filename") for f in commit.get("files") or []]})

    def issue_item(key: str, record: dict) -> dict:
        repo_key = record.get("_repo") or key.partition("#")[0]
        return {
            "key": key,
            "repo": repo_key,
            "number": record.get("number"),
            "title": record.get("title"),
            "body": record.get("body") or "",
            "state": record.get("state"),
            "labels": [lab.get("name") for lab in record.get("labels") or []],
            "assignees": [a.get("login") for a in record.get("assignees") or []],
            "author": (record.get("user") or {}).get("login"),
            "comments": record.get("comments", 0),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "closed_at": record.get("closed_at"),
        }

    pulls = []
    reviews = []
    for key, record in _records(state, "pulls").items():
        repo_key = record.get("_repo") or key.partition("#")[0]
        pulls.append({
            **issue_item(key, record),
            "draft": record.get("draft", False),
            "merged": record.get("merged", False),
            "merged_at": record.get("merged_at"),
            "head": (record.get("head") or {}).get("ref"),
            "base": (record.get("base") or {}).get("ref"),
            "requested_reviewers": [r.get("login") for r in record.get("requested_reviewers") or []],
        })
        for review in record.get("_reviews") or []:
            reviews.append({"id": review.get("id"), "repo": repo_key, "pull": key,
                            "state": review.get("state"), "body": review.get("body") or "",
                            "reviewer": (review.get("user") or {}).get("login"),
                            "submitted_at": review.get("submitted_at")})

    comments = []
    for comment in _records(state, "comments").values():
        repo_key, _, number = (comment.get("_issue") or "").partition("#")
        comments.append({
            "id": comment.get("id"),
            "repo": repo_key,
            "issue": comment.get("_issue"),
            "number": int(number) if number.isdigit() else None,
            "kind": comment.get("_kind", "issue"),
            "body": comment.get("body") or "",
            "author": (comment.get("user") or {}).get("login"),
            "path": comment.get("path"),
            "created_at": comment.get("created_at"),
        })

    return {
        "repos": kit.View([{
            "full_name": r.get("full_name", k), "name": r.get("name"),
            "owner": (r.get("owner") or {}).get("login"), "private": r.get("private", False),
            "default_branch": r.get("default_branch"), "description": r.get("description"),
            "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
        } for k, r in repos.items()], key="full_name", nouns=("repository", "repositories")),
        "issues": kit.View([issue_item(k, i) for k, i in _records(state, "issues").items()],
                           key="key", nouns=("issue", "issues")),
        "pulls": kit.View(pulls, key="key", nouns=("pull request", "pull requests")),
        "comments": kit.View(comments, key="id", nouns=("comment", "comments")),
        "reviews": kit.View(reviews, key="id", nouns=("review", "reviews")),
        "labels": kit.View([{"key": k, "repo": k.rsplit("/", 1)[0], "name": lab.get("name"),
                             "color": lab.get("color"), "description": lab.get("description")}
                            for k, lab in _records(state, "labels").items()],
                           key="key", nouns=("label", "labels")),
        "branches": kit.View(branches, key="key", nouns=("branch", "branches")),
        "files": kit.View(files, key="key", nouns=("file", "files")),
        "commits": kit.View(commits, key="sha", nouns=("commit", "commits")),
        "releases": kit.View([{"key": k, "repo": r.get("_repo") or k.partition("#")[0],
                               "tag_name": r.get("tag_name"), "name": r.get("name"),
                               "draft": r.get("draft", False),
                               "prerelease": r.get("prerelease", False),
                               "created_at": r.get("created_at")}
                              for k, r in _records(state, "releases").items()],
                             key="key", nouns=("release", "releases")),
        "workflow_runs": kit.View([{"key": k, "repo": k.partition("#")[0], "id": run.get("id"),
                                    "name": run.get("name"), "status": run.get("status"),
                                    "conclusion": run.get("conclusion"),
                                    "head_branch": run.get("head_branch"),
                                    "created_at": run.get("created_at")}
                                   for k, run in _records(state, "workflow_runs").items()],
                                  key="key", nouns=("workflow run", "workflow runs")),
        "users": kit.View(list(_records(state, "users").values()), key="login",
                          nouns=("user", "users")),
    }


def _after_seed(state: dict) -> None:
    """Make a hand-written seed a complete twin state.

    Seeds name what matters — a repo with files, a few issues — and this fills
    in what the wire format needs: git blobs and trees for the files each
    branch carries, back-references, ids and counters past the seeded ones.
    Anything a seed leaves out is filled in; anything it shapes oddly is left
    alone rather than crashing the seed load.
    """
    for repo_key, repo in _records(state, "repos").items():
        repo.setdefault("full_name", repo_key)
        repo.setdefault("name", repo_key.split("/")[-1])
        repo.setdefault("owner", _user(repo_key.split("/")[0]))
        repo.setdefault("default_branch", "main")
        repo.setdefault("branches", {})
        repo.setdefault("tags", {})
        repo.setdefault("commits", [])
        repo.setdefault("blobs", {})
        repo.setdefault("files", {})
        for name, branch in repo["branches"].items():
            branch.setdefault("name", name)
        # Blobs for every file a seed lists, per branch.
        trees: dict[str, dict[str, str]] = {}
        branch_files = repo.pop("branch_files", {}) or {}
        for branch, entries in {repo["default_branch"]: repo["files"], **branch_files}.items():
            trees[branch] = {path: _put_blob(repo, _seed_content(entry))
                             for path, entry in (entries or {}).items()}
        # A seed's commits are newest-first with implied linear history.
        commits = [c for c in repo["commits"] if isinstance(c, dict) and c.get("sha")]
        repo["commits"] = commits
        for index, commit in enumerate(commits):
            parent = commits[index + 1]["sha"] if index + 1 < len(commits) else None
            commit.setdefault("parents", [parent] if parent else [])
            commit.setdefault("files", [])
            commit.setdefault("commit", {"message": "Initial commit",
                                         "author": {"name": repo["owner"]["login"],
                                                    "date": repo.get("created_at") or _now()}})
        head_of = {branch.get("sha"): name for name, branch in repo["branches"].items()}
        for commit in commits:
            tree = commit.get("tree")
            if isinstance(tree, dict) and any(not isinstance(v, str) for v in tree.values()):
                # A seed may spell out an older commit's tree as {path: {content}}.
                commit["tree"] = {p: _put_blob(repo, _seed_content(e)) for p, e in tree.items()}
            elif tree is None:
                branch = head_of.get(commit["sha"])
                commit["tree"] = dict(trees.get(branch, trees.get(repo["default_branch"], {})))
        # Every branch needs a commit to point at, even if the seed gave none.
        known = _commits_by_sha(repo)
        for name, branch in repo["branches"].items():
            if branch.get("sha") not in known:
                commit = _add_commit(repo, message="Initial commit",
                                     tree=trees.get(name, {}), parents=[], files=[])
                branch["sha"] = commit["sha"]
        _materialize(repo)
        STATE["_counters"]["repo_id"] = max(STATE["_counters"]["repo_id"], int(repo.get("id") or 0))

    for store, kind in (("issues", "issue"), ("pulls", "pull")):
        for key, record in _records(state, store).items():
            repo_key, _, number = key.partition("#")
            record["_repo"] = record.get("_repo") or repo_key
            if record.get("number") is None and number.isdigit():
                record["number"] = int(number)
            record.setdefault("number", 0)
            record.setdefault("state", "open")
            record.setdefault("labels", [])
            record.setdefault("assignees", [])
            record.setdefault("comments", 0)
            record.setdefault("user", _actor())
            record.setdefault("created_at", _now())
            record.setdefault("updated_at", record["created_at"])
            record.setdefault("closed_at", None)
            if record.get("id") is None:
                record["id"] = _next_id("issue_id")
            STATE["_counters"]["issue_id"] = max(STATE["_counters"]["issue_id"], record["id"])
            # A label on an issue exists on its repository; the seed's colour wins.
            names = [lab.get("name") if isinstance(lab, dict) else lab
                     for lab in record["labels"] or []]
            if record["_repo"]:
                for applied in record["labels"]:
                    if isinstance(applied, dict) and applied.get("name"):
                        _label_records(record["_repo"], [applied["name"]],
                                       color=applied.get("color", "ededed"))
                record["labels"] = _label_records(
                    record["_repo"], [n for n in names if isinstance(n, str)])
            if kind == "pull":
                record.setdefault("merged", False)
                record.setdefault("draft", False)
                record.setdefault("requested_reviewers", [])
                record.setdefault("merged_at", None)
                record.pop("_files", None)
                record.pop("_commits", None)
                record.pop("_status", None)
                record.pop("mergeable", None)
                record.pop("mergeable_state", None)
                for comment in record.pop("_comments", []) or []:
                    comment["_issue"] = key
                    comment["_kind"] = "review"
                    comment.setdefault("id", _next_id("comment_id"))
                    state.setdefault("comments", {})[str(comment["id"])] = comment
                for review in record.setdefault("_reviews", []):
                    review.setdefault("id", _next_id("review_id"))
                    STATE["_counters"]["review_id"] = max(STATE["_counters"]["review_id"],
                                                          review["id"])

    for key, comment in _records(state, "comments").items():
        comment.setdefault("id", int(key) if key.isdigit() else _next_id("comment_id"))
        comment.setdefault("_kind", "issue")
        comment.setdefault("user", _actor())
        comment.setdefault("created_at", _now())
        comment.setdefault("updated_at", comment["created_at"])
        STATE["_counters"]["comment_id"] = max(STATE["_counters"]["comment_id"], comment["id"])

    for key, label in _records(state, "labels").items():
        label.setdefault("name", key.rsplit("/", 1)[-1])
        label.setdefault("color", "ededed")
        if label.get("id") is None:
            label["id"] = _next_id("label_id")
        STATE["_counters"]["label_id"] = max(STATE["_counters"]["label_id"], label["id"])

    for key, release in _records(state, "releases").items():
        release["_repo"] = release.get("_repo") or key.partition("#")[0]
        release.setdefault("id", _next_id("release_id"))
        STATE["_counters"]["release_id"] = max(STATE["_counters"]["release_id"], release["id"])

    for run in _records(state, "workflow_runs").values():
        STATE["_counters"]["run_id"] = max(STATE["_counters"]["run_id"], int(run.get("id") or 0))

    for login, user in _records(state, "users").items():
        user.setdefault("login", login)
        if user.get("id") is None:
            user["id"] = _next_id("user_id")
        STATE["_counters"]["user_id"] = max(STATE["_counters"]["user_id"], int(user["id"]))


TWIN = kit.install(app, kit.Twin(
    name="github",
    state=STATE,
    trace=TRACE,
    fresh_state=_fresh_state,
    seeds_dir=SEEDS_DIR,
    error=_error,
    authenticate=_authenticate,
    on_response=_stamp_headers,
    after_seed=_after_seed,
    views=_views,
    classify=_classify,
))


@app.middleware("http")
async def _refill_rate_limit(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Hand out a fresh request budget once the advertised reset time passes.

    Registered after ``kit.install`` so it runs outside the kit's pipeline —
    the budget has to be back before the kit counts this request. Without it an
    SDK that honours ``Retry-After`` (PyGithub does, by default) retries
    forever against a counter that never resets.
    """
    rate = STATE.get("_rate") or {}
    if rate.get("reset_at") is not None and time.time() >= rate["reset_at"]:
        TWIN.requests = 0
        rate["reset_at"] = None
    return await call_next(request)


@app.exception_handler(_ApiError)
async def _api_error_handler(request: Request, exc: _ApiError) -> Response:
    return gh_error(exc.status, exc.message, **exc.extra)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> Response:
    """A path that can't be parsed is a path that doesn't exist, as on GitHub."""
    if any(error.get("loc", ("",))[0] == "path" for error in exc.errors()):
        return gh_error(404, "Not Found")
    return gh_error(422, "Validation Failed", errors=[
        {"resource": "Request", "field": ".".join(str(p) for p in error.get("loc", [])[1:]),
         "code": "invalid", "message": error.get("msg")}
        for error in exc.errors()
    ])


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Unknown routes answer in GitHub's envelope, not FastAPI's ``{"detail": ...}``."""
    message = "Not Found" if exc.status_code == 404 else str(exc.detail)
    return gh_error(exc.status_code, message)


# --- users and organizations ---------------------------------------------

@app.get("/user")
def get_authenticated_user(request: Request):
    """The token's owner. SDKs call this first (PyGithub ``get_user()``, ``gh auth status``)."""
    return _user_json(_api_base(request), _actor())


@app.get("/user/repos")
def list_authenticated_user_repos(request: Request, per_page: int = 30, page: int = 1):
    base = _api_base(request)
    repos = [_repo_json(base, r) for r in STATE["repos"].values()
             if r["owner"]["login"] == _actor()["login"]]
    return _paginated(repos, request, per_page, page)


@app.post("/user/repos", status_code=201)
async def create_user_repo(request: Request):
    body = await _body(request)
    name = body.get("name")
    if not name:
        raise _invalid("Repository", "name")
    owner = body.get("owner") or _actor()["login"]
    if _repo_key(owner, name) in STATE["repos"]:
        raise _invalid("Repository", "name", "custom", message="name already exists on this account")
    repo = _make_repo(owner, name, private=bool(body.get("private")),
                      description=body.get("description"), auto_init=bool(body.get("auto_init")))
    return _repo_json(_api_base(request), repo)


@app.get("/users/{login}")
def get_user(login: str, request: Request):
    user = STATE["users"].get(login)
    if user is None:
        raise _not_found()
    return _user_json(_api_base(request), user)


@app.get("/users/{login}/repos")
def list_user_repos(login: str, request: Request, per_page: int = 30, page: int = 1):
    base = _api_base(request)
    repos = [_repo_json(base, r) for r in STATE["repos"].values() if r["owner"]["login"] == login]
    return _paginated(repos, request, per_page, page)


@app.get("/orgs/{org}")
def get_organization(org: str, request: Request):
    user = STATE["users"].get(org)
    if user is None or user.get("type") != "Organization":
        raise _not_found()
    base = _api_base(request)
    return {**(_user_json(base, user) or {}), "url": f"{base}/orgs/{org}",
            "repos_url": f"{base}/orgs/{org}/repos", "description": user.get("description")}


@app.get("/orgs/{org}/repos")
def list_org_repos(org: str, request: Request, per_page: int = 30, page: int = 1):
    if org not in STATE["users"]:
        raise _not_found()
    base = _api_base(request)
    repos = [_repo_json(base, r) for r in STATE["repos"].values() if r["owner"]["login"] == org]
    return _paginated(repos, request, per_page, page)


@app.post("/orgs/{org}/repos", status_code=201)
async def create_org_repo(org: str, request: Request):
    """Create a repo in an org; the sandbox has no membership model, so an
    unknown org is registered on first use rather than refused."""
    body = await _body(request)
    name = body.get("name")
    if not name:
        raise _invalid("Repository", "name")
    if _repo_key(org, name) in STATE["repos"]:
        raise _invalid("Repository", "name", "custom", message="name already exists on this account")
    repo = _make_repo(org, name, private=bool(body.get("private")),
                      description=body.get("description"),
                      auto_init=bool(body.get("auto_init")), org=True)
    return _repo_json(_api_base(request), repo)


@app.get("/rate_limit")
def get_rate_limit(request: Request):
    """PyGithub, Octokit and `gh` poll this before long jobs."""
    limit, remaining, used, reset = _rate_snapshot()
    core = {"limit": limit, "remaining": remaining, "used": used, "reset": reset,
            "resource": "core"}
    search = {**core, "limit": 30, "remaining": 30, "used": 0, "resource": "search"}
    graphql = {**core, "limit": UNLIMITED, "resource": "graphql"}
    return {"resources": {"core": core, "search": search, "graphql": graphql}, "rate": core}


# --- repositories ---------------------------------------------------------

@app.get("/repos/{owner}/{name}")
def get_repo(owner: str, name: str, request: Request):
    return _repo_json(_api_base(request), _repo(owner, name))


@app.patch("/repos/{owner}/{name}")
async def update_repo(owner: str, name: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    for field in ("description", "homepage", "default_branch"):
        if body.get(field) is not None:
            repo[field] = body[field]
    if "private" in body:
        repo["private"] = bool(body["private"])
    repo["updated_at"] = _now()
    return _repo_json(_api_base(request), repo)


@app.delete("/repos/{owner}/{name}", status_code=204)
def delete_repo(owner: str, name: str):
    key = _repo_key(owner, name)
    _repo(owner, name)
    del STATE["repos"][key]
    for store in ("issues", "pulls", "releases", "workflow_runs"):
        for record_key in [k for k in STATE[store] if k.startswith(f"{key}#")]:
            del STATE[store][record_key]
    for label_key in [k for k in STATE["labels"] if k.startswith(f"{key}/")]:
        del STATE["labels"][label_key]
    for comment_key in [k for k, c in STATE["comments"].items()
                        if (c.get("_issue") or "").startswith(f"{key}#")]:
        del STATE["comments"][comment_key]
    return Response(status_code=204)


@app.post("/repos/{owner}/{name}/forks", status_code=202)
async def fork_repository(owner: str, name: str, request: Request):
    source = _repo(owner, name)
    body = await _body(request)
    new_owner = body.get("organization") or _actor()["login"]
    if _repo_key(new_owner, name) in STATE["repos"]:
        raise _invalid("Repository", "name", "custom", message="name already exists on this account")
    fork = _make_repo(new_owner, name, private=source.get("private", False),
                      description=source.get("description"))
    fork["fork"] = True
    fork["blobs"] = dict(source["blobs"])
    fork["commits"] = [dict(c) for c in source["commits"]]
    fork["branches"] = {n: dict(b) for n, b in source["branches"].items()}
    fork["default_branch"] = source["default_branch"]
    fork["parent"] = {"full_name": source["full_name"], "id": source["id"]}
    _materialize(fork)
    return _repo_json(_api_base(request), fork)


@app.get("/repos/{owner}/{name}/readme")
def get_readme(owner: str, name: str, request: Request, ref: str | None = None):
    repo = _repo(owner, name)
    tree = _tree_of(repo, ref)
    if tree is None:
        raise _not_found()
    path = next((p for p in tree if p.lower().startswith("readme")), None)
    if path is None:
        raise _not_found()
    return _content_json(_api_base(request), repo, path, tree[path], ref or repo["default_branch"])


# --- file contents --------------------------------------------------------

@app.get("/repos/{owner}/{name}/contents/{path:path}")
def get_contents(owner: str, name: str, path: str, request: Request, ref: str | None = None):
    """A file, or the entries of a directory — GitHub returns an array for a directory."""
    repo = _repo(owner, name)
    tree = _tree_of(repo, ref)
    if tree is None:
        raise _not_found()
    base = _api_base(request)
    at = ref or repo["default_branch"]
    path = path.strip("/")
    if path in tree:
        return _content_json(base, repo, path, tree[path], at)
    prefix = f"{path}/" if path else ""
    children = {p[len(prefix):].split("/")[0] for p in tree if p.startswith(prefix)}
    if not children:
        raise _not_found()
    entries = []
    for child in sorted(children):
        child_path = f"{prefix}{child}"
        if child_path in tree:
            entries.append(_content_json(base, repo, child_path, tree[child_path], at,
                                         with_content=False))
        else:
            entries.append({**_content_json(base, repo, child_path, "", at, with_content=False),
                            "type": "dir", "size": 0,
                            "sha": _blob_sha(child_path), "download_url": None})
    return entries


@app.put("/repos/{owner}/{name}/contents/{path:path}")
async def create_or_update_file(owner: str, name: str, path: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    path = path.strip("/")
    message = body.get("message")
    if not message:
        raise _invalid("Contents", "message")
    if body.get("content") is None:
        raise _invalid("Contents", "content")
    try:
        content = base64.b64decode(body["content"]).decode()
    except (ValueError, UnicodeDecodeError):
        raise _invalid("Contents", "content", "invalid") from None
    branch = body.get("branch") or repo["default_branch"]
    if branch not in repo["branches"]:
        raise _ApiError(404, f"Branch {branch} not found")
    head = repo["branches"][branch]["sha"]
    tree = _tree_at(repo, head) or {}
    existing = tree.get(path)
    if existing and not body.get("sha"):
        raise _ApiError(422, 'Invalid request.\n\n"sha" wasn\'t supplied.')
    if existing and body["sha"] != existing:
        raise _ApiError(409, f"{path} does not match {body['sha']}")
    sha = _put_blob(repo, content)
    tree[path] = sha
    commit = _add_commit(repo, message=message, tree=tree, parents=[head],
                         author=body.get("committer") or body.get("author"),
                         files=[{"filename": path, "status": "modified" if existing else "added",
                                 "sha": sha}])
    _move_branch(repo, branch, commit["sha"])
    base = _api_base(request)
    return JSONResponse(status_code=200 if existing else 201, content={
        "content": _content_json(base, repo, path, sha, branch),
        "commit": _commit_json(base, repo["full_name"], commit),
    })


@app.delete("/repos/{owner}/{name}/contents/{path:path}")
async def delete_file(owner: str, name: str, path: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    path = path.strip("/")
    if not body.get("message"):
        raise _invalid("Contents", "message")
    if not body.get("sha"):
        raise _invalid("Contents", "sha")
    branch = body.get("branch") or repo["default_branch"]
    if branch not in repo["branches"]:
        raise _ApiError(404, f"Branch {branch} not found")
    head = repo["branches"][branch]["sha"]
    tree = _tree_at(repo, head) or {}
    if path not in tree:
        raise _not_found()
    if tree[path] != body["sha"]:
        raise _ApiError(409, f"{path} does not match {body['sha']}")
    removed = tree.pop(path)
    commit = _add_commit(repo, message=body["message"], tree=tree, parents=[head],
                         author=body.get("committer") or body.get("author"),
                         files=[{"filename": path, "status": "removed", "sha": removed}])
    _move_branch(repo, branch, commit["sha"])
    return {"content": None, "commit": _commit_json(_api_base(request), repo["full_name"], commit)}


@app.post("/repos/{owner}/{name}/_push_files", status_code=201)
async def push_files(owner: str, name: str, request: Request):
    """Batch commit behind the MCP ``push_files`` tool: ``{branch, message, files:[{path, content}]}``.

    Not a GitHub route — the real API needs four git-database calls for this —
    but the MCP servers agents use expose it as one tool, so the twin does too.
    """
    repo = _repo(owner, name)
    body = await _body(request)
    branch = body.get("branch") or repo["default_branch"]
    if branch not in repo["branches"]:
        raise _ApiError(404, f"Branch {branch} not found")
    files = body.get("files") or []
    if not files:
        raise _invalid("Contents", "files")
    head = repo["branches"][branch]["sha"]
    tree = _tree_at(repo, head) or {}
    entries = []
    for item in files:
        path = (item.get("path") or "").strip("/")
        if not path:
            raise _invalid("Contents", "path")
        status = "modified" if path in tree else "added"
        sha = _put_blob(repo, item.get("content", ""))
        tree[path] = sha
        entries.append({"filename": path, "status": status, "sha": sha})
    commit = _add_commit(repo, message=body.get("message") or "Update files", tree=tree,
                         parents=[head], files=entries)
    _move_branch(repo, branch, commit["sha"])
    return {"commit": _commit_json(_api_base(request), repo["full_name"], commit),
            "branch": branch, "files_pushed": len(entries)}


# --- branches, refs and commits ------------------------------------------

@app.get("/repos/{owner}/{name}/branches")
def list_branches(owner: str, name: str, request: Request, per_page: int = 30, page: int = 1,
                  protected: bool | None = None):
    repo = _repo(owner, name)
    base = _api_base(request)
    branches = [_branch_json(base, repo, b) for b in repo["branches"].values()
                if protected is None or bool(b.get("protected")) is protected]
    return _paginated(branches, request, per_page, page)


@app.get("/repos/{owner}/{name}/branches/{branch:path}")
def get_branch(owner: str, name: str, branch: str, request: Request):
    repo = _repo(owner, name)
    record = repo["branches"].get(branch)
    if record is None:
        raise _ApiError(404, "Branch not found")
    return _branch_json(_api_base(request), repo, record)


@app.get("/repos/{owner}/{name}/git/refs")
def list_refs(owner: str, name: str, request: Request, per_page: int = 100, page: int = 1):
    repo = _repo(owner, name)
    base = _api_base(request)
    refs = [_ref_json(base, repo["full_name"], f"heads/{n}", b["sha"])
            for n, b in repo["branches"].items()]
    refs += [_ref_json(base, repo["full_name"], f"tags/{n}", t["sha"])
             for n, t in repo.get("tags", {}).items()]
    return _paginated(refs, request, per_page, page)


@app.post("/repos/{owner}/{name}/git/refs", status_code=201)
async def create_ref(owner: str, name: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    ref = str(body.get("ref") or "")
    sha = body.get("sha")
    if not ref.startswith(("refs/heads/", "refs/tags/")):
        raise _invalid("Reference", "ref", "invalid")
    if not sha:
        raise _invalid("Reference", "sha")
    if _resolve(repo, sha) is None:
        raise _invalid("Reference", "sha", "custom", message="Object does not exist")
    kind, _, short = ref[len("refs/"):].partition("/")
    store = repo["branches"] if kind == "heads" else repo.setdefault("tags", {})
    if short in store:
        raise _invalid("Reference", "ref", "already_exists")
    if kind == "heads":
        _move_branch(repo, short, sha)
    else:
        store[short] = {"name": short, "sha": sha}
    return _ref_json(_api_base(request), repo["full_name"], f"{kind}/{short}", sha)


@app.get("/repos/{owner}/{name}/git/matching-refs/{ref:path}")
def list_matching_refs(owner: str, name: str, ref: str, request: Request):
    repo = _repo(owner, name)
    base = _api_base(request)
    out = []
    for kind, store in (("heads", repo["branches"]), ("tags", repo.get("tags", {}))):
        for short, record in store.items():
            if f"{kind}/{short}".startswith(ref.strip("/")):
                out.append(_ref_json(base, repo["full_name"], f"{kind}/{short}", record["sha"]))
    return out


@app.get("/repos/{owner}/{name}/git/ref/{ref:path}")
@app.get("/repos/{owner}/{name}/git/refs/{ref:path}")
def get_ref(owner: str, name: str, ref: str, request: Request):
    repo = _repo(owner, name)
    kind, _, short = ref.strip("/").partition("/")
    store = repo["branches"] if kind == "heads" else repo.get("tags", {})
    record = store.get(short)
    if record is None:
        raise _not_found()
    return _ref_json(_api_base(request), repo["full_name"], f"{kind}/{short}", record["sha"])


@app.patch("/repos/{owner}/{name}/git/refs/{ref:path}")
async def update_ref(owner: str, name: str, ref: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    kind, _, short = ref.strip("/").partition("/")
    store = repo["branches"] if kind == "heads" else repo.get("tags", {})
    if short not in store:
        raise _not_found()
    sha = body.get("sha")
    if not sha or _resolve(repo, sha) is None:
        raise _invalid("Reference", "sha", "invalid")
    if kind == "heads":
        _move_branch(repo, short, sha)
    else:
        store[short]["sha"] = sha
    return _ref_json(_api_base(request), repo["full_name"], f"{kind}/{short}", sha)


@app.delete("/repos/{owner}/{name}/git/refs/{ref:path}", status_code=204)
def delete_ref(owner: str, name: str, ref: str):
    repo = _repo(owner, name)
    kind, _, short = ref.strip("/").partition("/")
    store = repo["branches"] if kind == "heads" else repo.get("tags", {})
    if short not in store:
        raise _not_found()
    if kind == "heads" and short == repo["default_branch"]:
        raise _ApiError(422, "Cannot delete the default branch")
    del store[short]
    return Response(status_code=204)


@app.get("/repos/{owner}/{name}/commits")
def list_commits(owner: str, name: str, request: Request, sha: str | None = None,
                 path: str | None = None, author: str | None = None,
                 per_page: int = 30, page: int = 1):
    repo = _repo(owner, name)
    start = _resolve(repo, sha)
    if start is None:
        raise _not_found()
    commits = _commits_by_sha(repo)
    history = [commits[s] for s in _ancestors(repo, start)]
    if path:
        history = [c for c in history if any(f["filename"] == path for f in c.get("files") or [])]
    if author:
        history = [c for c in history
                   if (c["commit"].get("author") or {}).get("name") == author]
    base = _api_base(request)
    return _paginated([_commit_json(base, repo["full_name"], c) for c in history],
                      request, per_page, page)


@app.get("/repos/{owner}/{name}/commits/{ref}")
def get_commit(owner: str, name: str, ref: str, request: Request):
    repo = _repo(owner, name)
    sha = _resolve(repo, ref)
    commit = _commits_by_sha(repo).get(sha) if sha else None
    if commit is None:
        raise _not_found()
    return _commit_json(_api_base(request), repo["full_name"], commit, with_files=True)


@app.get("/repos/{owner}/{name}/commits/{ref}/status")
def get_combined_status(owner: str, name: str, ref: str, request: Request):
    """Combined status for a commit, derived from the workflow runs against it."""
    repo = _repo(owner, name)
    sha = _resolve(repo, ref)
    if sha is None:
        raise _not_found()
    runs = [r for r in _repo_records("workflow_runs", repo["full_name"])
            if r.get("head_sha") == sha]
    if any(r.get("status") != "completed" for r in runs):
        state = "pending"
    elif any(r.get("conclusion") not in ("success", "skipped", "neutral") for r in runs):
        state = "failure"
    else:
        state = "success" if runs else "pending"
    base = _api_base(request)
    return {
        "state": state,
        "sha": sha,
        "total_count": len(runs),
        "statuses": [{"state": ("pending" if r.get("status") != "completed"
                                else "success" if r.get("conclusion") == "success" else "failure"),
                      "context": r.get("name"), "description": r.get("conclusion"),
                      "target_url": r.get("html_url")} for r in runs],
        "repository": _repo_json(base, repo),
        "url": f"{base}/repos/{repo['full_name']}/commits/{sha}/status",
    }


@app.get("/repos/{owner}/{name}/commits/{ref}/check-runs")
def list_check_runs(owner: str, name: str, ref: str):
    repo = _repo(owner, name)
    sha = _resolve(repo, ref)
    runs = [r for r in _repo_records("workflow_runs", repo["full_name"])
            if r.get("head_sha") == sha]
    return {"total_count": len(runs),
            "check_runs": [{"id": r["id"], "name": r.get("name"), "head_sha": sha,
                            "status": r.get("status"), "conclusion": r.get("conclusion")}
                           for r in runs]}


# --- issues ---------------------------------------------------------------

def _sorted_issues(records: list[dict], sort: str, direction: str) -> list[dict]:
    keys = {
        "created": lambda r: (r.get("created_at") or "", r["number"]),
        "updated": lambda r: (r.get("updated_at") or "", r["number"]),
        "comments": lambda r: (r.get("comments", 0), r["number"]),
        "popularity": lambda r: (r.get("comments", 0), r["number"]),
        "long-running": lambda r: (r.get("created_at") or "", r["number"]),
    }
    return sorted(records, key=keys.get(sort, keys["created"]), reverse=direction != "asc")


@app.post("/repos/{owner}/{name}/issues", status_code=201)
async def create_issue(owner: str, name: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    title = body.get("title")
    if not title:
        raise _invalid("Issue", "title")
    repo_key = repo["full_name"]
    number = _next_number(repo_key)
    issue = {
        "id": _next_id("issue_id"),
        "_repo": repo_key,
        "number": number,
        "title": str(title),
        "body": body.get("body") or "",
        "state": "open",
        "state_reason": None,
        "labels": _label_records(repo_key, _label_names(body) if body.get("labels") else []),
        "assignees": _assignee_records(
            body.get("assignees") or ([body["assignee"]] if body.get("assignee") else [])),
        "comments": 0,
        "locked": False,
        "user": _actor(),
        "created_at": _now(),
        "updated_at": _now(),
        "closed_at": None,
    }
    STATE["issues"][f"{repo_key}#{number}"] = issue
    return JSONResponse(status_code=201, content=_issue_json(_api_base(request), issue))


@app.get("/repos/{owner}/{name}/issues")
def list_issues(owner: str, name: str, request: Request, state: str = "open",
                labels: str | None = None, assignee: str | None = None,
                creator: str | None = None, since: str | None = None,
                sort: str = "created", direction: str = "desc",
                per_page: int = 30, page: int = 1):
    """Issues *and* pull requests, as GitHub's issues endpoint returns both."""
    repo = _repo(owner, name)
    repo_key = repo["full_name"]
    out = []
    for record in _repo_records("issues", repo_key) + _repo_records("pulls", repo_key):
        if state != "all" and record["state"] != state:
            continue
        if labels and not set(labels.split(",")) <= {lab["name"] for lab in record["labels"]}:
            continue
        logins = {a["login"] for a in record.get("assignees") or []}
        if assignee == "none" and logins:
            continue
        if assignee not in (None, "*", "none") and assignee not in logins:
            continue
        if creator and (record.get("user") or {}).get("login") != creator:
            continue
        if since and (record.get("updated_at") or "") < since:
            continue
        out.append(record)
    base = _api_base(request)
    return _paginated([_issue_json(base, r) for r in _sorted_issues(out, sort, direction)],
                      request, per_page, page)


@app.get("/repos/{owner}/{name}/issues/comments")
def list_repo_issue_comments(owner: str, name: str, request: Request, since: str | None = None,
                             sort: str = "created", direction: str = "asc",
                             per_page: int = 30, page: int = 1):
    repo_key = _repo(owner, name)["full_name"]
    base = _api_base(request)
    comments = [c for c in STATE["comments"].values()
                if (c.get("_issue") or "").startswith(f"{repo_key}#")
                and c.get("_kind") != "review"
                and (not since or (c.get("created_at") or "") >= since)]
    comments.sort(key=lambda c: c.get("created_at") or "", reverse=direction == "desc")
    return _paginated([_comment_json(base, c) for c in comments], request, per_page, page)


def _comment(comment_id: int, kind: str) -> dict:
    comment = STATE["comments"].get(str(comment_id))
    if comment is None or comment.get("_kind", "issue") != kind:
        raise _not_found()
    return comment


@app.get("/repos/{owner}/{name}/issues/comments/{comment_id}")
def get_issue_comment(owner: str, name: str, comment_id: int, request: Request):
    _repo(owner, name)
    return _comment_json(_api_base(request), _comment(comment_id, "issue"))


@app.patch("/repos/{owner}/{name}/issues/comments/{comment_id}")
async def update_issue_comment(owner: str, name: str, comment_id: int, request: Request):
    _repo(owner, name)
    comment = _comment(comment_id, "issue")
    body = await _body(request)
    if body.get("body") is None:
        raise _invalid("IssueComment", "body")
    comment["body"] = body["body"]
    comment["updated_at"] = _now()
    return _comment_json(_api_base(request), comment)


@app.delete("/repos/{owner}/{name}/issues/comments/{comment_id}", status_code=204)
def delete_issue_comment(owner: str, name: str, comment_id: int):
    _repo(owner, name)
    comment = _comment(comment_id, "issue")
    repo_key, _, number = comment["_issue"].partition("#")
    del STATE["comments"][str(comment_id)]
    record = _issue_like(repo_key, int(number))
    record["comments"] = max(0, record.get("comments", 1) - 1)
    return Response(status_code=204)


@app.get("/repos/{owner}/{name}/issues/{number}")
def get_issue(owner: str, name: str, number: int, request: Request):
    """Serves pull requests too: on GitHub every PR is also an issue."""
    repo_key = _repo(owner, name)["full_name"]
    return _issue_json(_api_base(request), _issue_like(repo_key, number))


@app.patch("/repos/{owner}/{name}/issues/{number}")
async def update_issue(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    body = await _body(request)
    if body.get("title") is not None:
        record["title"] = body["title"]
    if body.get("body") is not None:
        record["body"] = body["body"]
    if body.get("state") in ("open", "closed"):
        record["state"] = body["state"]
        record["closed_at"] = _now() if body["state"] == "closed" else None
        record["state_reason"] = body.get("state_reason") or (
            "completed" if body["state"] == "closed" else None)
    if isinstance(body.get("labels"), list):
        record["labels"] = _label_records(repo_key, _label_names(body))
    if isinstance(body.get("assignees"), list):
        record["assignees"] = _assignee_records(body["assignees"])
    if "assignee" in body:
        record["assignees"] = _assignee_records([body["assignee"]] if body["assignee"] else [])
    _touch(record)
    base = _api_base(request)
    return _pull_json(base, record) if _is_pull(record) else _issue_json(base, record)


@app.post("/repos/{owner}/{name}/issues/{number}/comments", status_code=201)
async def create_issue_comment(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    body = await _body(request)
    if body.get("body") is None:
        raise _invalid("IssueComment", "body")
    comment = {
        "id": _next_id("comment_id"),
        "body": body["body"],
        "user": _actor(),
        "created_at": _now(),
        "updated_at": _now(),
        "_issue": f"{repo_key}#{number}",
        "_kind": "issue",
    }
    STATE["comments"][str(comment["id"])] = comment
    record["comments"] = record.get("comments", 0) + 1
    _touch(record)
    return JSONResponse(status_code=201, content=_comment_json(_api_base(request), comment))


@app.get("/repos/{owner}/{name}/issues/{number}/comments")
def list_issue_comments(owner: str, name: str, number: int, request: Request,
                        since: str | None = None, per_page: int = 30, page: int = 1):
    repo_key = _repo(owner, name)["full_name"]
    _issue_like(repo_key, number)
    key = f"{repo_key}#{number}"
    base = _api_base(request)
    comments = [c for c in STATE["comments"].values()
                if c.get("_issue") == key and c.get("_kind") != "review"
                and (not since or (c.get("created_at") or "") >= since)]
    return _paginated([_comment_json(base, c) for c in comments], request, per_page, page)


@app.get("/repos/{owner}/{name}/issues/{number}/labels")
def list_issue_labels(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    base = _api_base(request)
    return [_label_json(base, repo_key, lab) for lab in record.get("labels") or []]


@app.post("/repos/{owner}/{name}/issues/{number}/labels")
async def add_labels(owner: str, name: str, number: int, request: Request):
    """Add labels to an issue or pull request; unknown labels are created, as on GitHub."""
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    names = _label_names(await _body(request))
    existing = {lab["name"] for lab in record["labels"]}
    record["labels"] += [lab for lab in _label_records(repo_key, names)
                         if lab["name"] not in existing]
    _touch(record)
    base = _api_base(request)
    return [_label_json(base, repo_key, lab) for lab in record["labels"]]


@app.put("/repos/{owner}/{name}/issues/{number}/labels")
async def set_labels(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    body = await _body(request)
    record["labels"] = _label_records(repo_key, _label_names(body) if body else [])
    _touch(record)
    base = _api_base(request)
    return [_label_json(base, repo_key, lab) for lab in record["labels"]]


@app.delete("/repos/{owner}/{name}/issues/{number}/labels", status_code=204)
def clear_labels(owner: str, name: str, number: int):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    record["labels"] = []
    _touch(record)
    return Response(status_code=204)


@app.delete("/repos/{owner}/{name}/issues/{number}/labels/{label}")
def remove_label(owner: str, name: str, number: int, label: str, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    if label not in {lab["name"] for lab in record["labels"]}:
        raise _ApiError(404, "Label does not exist")
    record["labels"] = [lab for lab in record["labels"] if lab["name"] != label]
    _touch(record)
    base = _api_base(request)
    return [_label_json(base, repo_key, lab) for lab in record["labels"]]


@app.post("/repos/{owner}/{name}/issues/{number}/assignees", status_code=201)
async def add_assignees(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    body = await _body(request)
    existing = {a["login"] for a in record.get("assignees") or []}
    record["assignees"] = (record.get("assignees") or []) + [
        user for user in _assignee_records(body.get("assignees") or [])
        if user["login"] not in existing
    ]
    _touch(record)
    return JSONResponse(status_code=201, content=_issue_json(_api_base(request), record))


@app.delete("/repos/{owner}/{name}/issues/{number}/assignees")
async def remove_assignees(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = _issue_like(repo_key, number)
    body = await _body(request)
    drop = set(body.get("assignees") or [])
    record["assignees"] = [a for a in record.get("assignees") or [] if a["login"] not in drop]
    _touch(record)
    return _issue_json(_api_base(request), record)


# --- labels ---------------------------------------------------------------

@app.get("/repos/{owner}/{name}/labels")
def list_labels(owner: str, name: str, request: Request, per_page: int = 30, page: int = 1):
    repo_key = _repo(owner, name)["full_name"]
    base = _api_base(request)
    labels = [_label_json(base, repo_key, lab) for key, lab in STATE["labels"].items()
              if key.startswith(f"{repo_key}/")]
    return _paginated(labels, request, per_page, page)


@app.post("/repos/{owner}/{name}/labels", status_code=201)
async def create_label(owner: str, name: str, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    body = await _body(request)
    label_name = body.get("name")
    if not label_name:
        raise _invalid("Label", "name")
    if f"{repo_key}/{label_name}" in STATE["labels"]:
        raise _invalid("Label", "name", "already_exists")
    label = _label_records(repo_key, [label_name])[0]
    label["color"] = str(body.get("color") or "ededed").lstrip("#")
    label["description"] = body.get("description")
    return JSONResponse(status_code=201, content=_label_json(_api_base(request), repo_key, label))


@app.get("/repos/{owner}/{name}/labels/{label}")
def get_label(owner: str, name: str, label: str, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    if f"{repo_key}/{label}" not in STATE["labels"]:
        raise _not_found()
    return _label_json(_api_base(request), repo_key, STATE["labels"][f"{repo_key}/{label}"])


@app.patch("/repos/{owner}/{name}/labels/{label}")
async def update_label(owner: str, name: str, label: str, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    record = STATE["labels"].get(f"{repo_key}/{label}")
    if record is None:
        raise _not_found()
    body = await _body(request)
    if body.get("color"):
        record["color"] = str(body["color"]).lstrip("#")
    if "description" in body:
        record["description"] = body["description"]
    new_name = body.get("new_name")
    if new_name and new_name != label:
        del STATE["labels"][f"{repo_key}/{label}"]
        record["name"] = new_name
        STATE["labels"][f"{repo_key}/{new_name}"] = record
        for store in ("issues", "pulls"):
            for item in _repo_records(store, repo_key):
                for applied in item.get("labels") or []:
                    if applied["name"] == label:
                        applied["name"] = new_name
    return _label_json(_api_base(request), repo_key, record)


@app.delete("/repos/{owner}/{name}/labels/{label}", status_code=204)
def delete_label(owner: str, name: str, label: str):
    repo_key = _repo(owner, name)["full_name"]
    if f"{repo_key}/{label}" not in STATE["labels"]:
        raise _not_found()
    del STATE["labels"][f"{repo_key}/{label}"]
    for store in ("issues", "pulls"):
        for record in _repo_records(store, repo_key):
            record["labels"] = [lab for lab in record.get("labels") or []
                                if lab["name"] != label]
    return Response(status_code=204)


# --- pull requests --------------------------------------------------------

def _pull(repo_key: str, number: int) -> dict:
    pull = STATE["pulls"].get(f"{repo_key}#{number}")
    if pull is None:
        raise _not_found()
    return pull


def _head_ref(repo: dict, head: str) -> str:
    """``head`` may be ``branch`` or ``owner:branch``; only same-repo heads are modelled."""
    owner_part, _, ref = head.rpartition(":")
    if owner_part and owner_part != repo["owner"]["login"]:
        raise _invalid("PullRequest", "head", "invalid")
    return ref


@app.post("/repos/{owner}/{name}/pulls", status_code=201)
async def create_pull_request(owner: str, name: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    for field in ("title", "head", "base"):
        if not body.get(field):
            raise _invalid("PullRequest", field)
    repo_key = repo["full_name"]
    head = _head_ref(repo, str(body["head"]))
    base_ref = str(body["base"])
    if head not in repo["branches"]:
        raise _invalid("PullRequest", "head", "invalid")
    if base_ref not in repo["branches"]:
        raise _invalid("PullRequest", "base", "invalid")
    if any(p["state"] == "open" and p["head"]["ref"] == head and p["base"]["ref"] == base_ref
           for p in _repo_records("pulls", repo_key)):
        raise _invalid("PullRequest", "head", "custom",
                       message=f"A pull request already exists for {owner}:{head}.")
    head_sha, base_sha = repo["branches"][head]["sha"], repo["branches"][base_ref]["sha"]
    if head_sha == base_sha:
        raise _invalid("PullRequest", "head", "custom",
                       message=f"No commits between {base_ref} and {head}")
    number = _next_number(repo_key)
    pull = {
        "id": _next_id("issue_id"),
        "_repo": repo_key,
        "number": number,
        "title": str(body["title"]),
        "body": body.get("body") or "",
        "state": "open",
        "merged": False,
        "draft": bool(body.get("draft")),
        "locked": False,
        "head": {"ref": head, "sha": head_sha},
        "base": {"ref": base_ref, "sha": base_sha},
        "user": _actor(),
        "labels": [],
        "assignees": [],
        "requested_reviewers": [],
        "comments": 0,
        "created_at": _now(),
        "updated_at": _now(),
        "closed_at": None,
        "merged_at": None,
        "merge_commit_sha": None,
        "merged_by": None,
        "_reviews": [],
    }
    STATE["pulls"][f"{repo_key}#{number}"] = pull
    return JSONResponse(status_code=201, content=_pull_json(_api_base(request), pull))


@app.get("/repos/{owner}/{name}/pulls")
def list_pull_requests(owner: str, name: str, request: Request, state: str = "open",
                       head: str | None = None, base: str | None = None,
                       sort: str = "created", direction: str = "desc",
                       per_page: int = 30, page: int = 1):
    repo = _repo(owner, name)
    out = []
    for pull in _repo_records("pulls", repo["full_name"]):
        if state != "all" and pull["state"] != state:
            continue
        if head and pull["head"]["ref"] != head.rpartition(":")[2]:
            continue
        if base and pull["base"]["ref"] != base:
            continue
        out.append(pull)
    api_base = _api_base(request)
    return _paginated([_pull_json(api_base, p) for p in _sorted_issues(out, sort, direction)],
                      request, per_page, page)


@app.get("/repos/{owner}/{name}/pulls/comments/{comment_id}")
def get_review_comment(owner: str, name: str, comment_id: int, request: Request):
    _repo(owner, name)
    return _comment_json(_api_base(request), _comment(comment_id, "review"))


@app.patch("/repos/{owner}/{name}/pulls/comments/{comment_id}")
async def update_review_comment(owner: str, name: str, comment_id: int, request: Request):
    _repo(owner, name)
    comment = _comment(comment_id, "review")
    body = await _body(request)
    if body.get("body") is None:
        raise _invalid("PullRequestReviewComment", "body")
    comment["body"] = body["body"]
    comment["updated_at"] = _now()
    return _comment_json(_api_base(request), comment)


@app.delete("/repos/{owner}/{name}/pulls/comments/{comment_id}", status_code=204)
def delete_review_comment(owner: str, name: str, comment_id: int):
    _repo(owner, name)
    _comment(comment_id, "review")
    del STATE["comments"][str(comment_id)]
    return Response(status_code=204)


@app.get("/repos/{owner}/{name}/pulls/{number}")
def get_pull_request(owner: str, name: str, number: int, request: Request):
    """``Accept: application/vnd.github.diff`` returns the diff, as the real API does."""
    repo = _repo(owner, name)
    pull = _pull(repo["full_name"], number)
    accept = request.headers.get("accept", "")
    if "diff" in accept or "patch" in accept:
        return PlainTextResponse(_pull_diff_text(repo, pull),
                                 media_type="application/vnd.github.v3.diff")
    return _pull_json(_api_base(request), pull)


def _pull_diff_text(repo: dict, pull: dict) -> str:
    out = []
    for entry in _pull_files(repo, pull):
        path = entry["filename"]
        out.append(f"diff --git a/{path} b/{path}")
        out.append(entry["patch"] or "")
    return "\n".join(out)


@app.patch("/repos/{owner}/{name}/pulls/{number}")
async def update_pull_request(owner: str, name: str, number: int, request: Request):
    repo = _repo(owner, name)
    pull = _pull(repo["full_name"], number)
    body = await _body(request)
    for field in ("title", "body"):
        if body.get(field) is not None:
            pull[field] = body[field]
    if body.get("state") in ("open", "closed"):
        pull["state"] = body["state"]
        pull["closed_at"] = _now() if body["state"] == "closed" else None
    if body.get("base") and body["base"] in repo["branches"]:
        pull["base"] = {"ref": body["base"], "sha": repo["branches"][body["base"]]["sha"]}
    if "draft" in body:
        pull["draft"] = bool(body["draft"])
    _touch(pull)
    return _pull_json(_api_base(request), pull)


@app.get("/repos/{owner}/{name}/pulls/{number}/merge")
def is_merged(owner: str, name: str, number: int):
    """204 when merged, 404 otherwise — what ``PullRequest.is_merged()`` checks."""
    pull = _pull(_repo(owner, name)["full_name"], number)
    if not pull.get("merged"):
        raise _not_found()
    return Response(status_code=204)


@app.put("/repos/{owner}/{name}/pulls/{number}/merge")
async def merge_pull_request(owner: str, name: str, number: int, request: Request):
    repo = _repo(owner, name)
    pull = _pull(repo["full_name"], number)
    body = await _body(request)
    if pull["state"] != "open":
        raise _ApiError(405, "Pull Request is not mergeable")
    if pull.get("draft"):
        raise _ApiError(405, "Pull Request is still a draft")
    head_sha = _pull_head_sha(repo, pull)
    if body.get("sha") and body["sha"] != head_sha:
        raise _ApiError(409, "Head branch was modified. Review and try the merge again.")
    tree, conflicts = _merge_result(repo, pull)
    if tree is None:
        raise _ApiError(405, "Pull Request is not mergeable")
    base_ref = pull["base"]["ref"]
    base_sha = _pull_base_sha(repo, pull)
    method = body.get("merge_method") or "merge"
    if method == "merge":
        title = body.get("commit_title") or (
            f"Merge pull request #{number} from {owner}:{pull['head']['ref']}")
        detail = body.get("commit_message") or pull["title"]
        parents = [base_sha, head_sha]
    else:  # squash and rebase both land a single commit on the base branch
        title = body.get("commit_title") or f"{pull['title']} (#{number})"
        detail = body.get("commit_message") or ""
        parents = [base_sha]
    message = f"{title}\n\n{detail}" if detail else title
    commit = _add_commit(repo, message=message, tree=tree, parents=parents,
                         files=_pull_files(repo, pull))
    _move_branch(repo, base_ref, commit["sha"])
    pull.update({
        "state": "closed", "merged": True, "merged_at": _now(), "closed_at": _now(),
        "merge_commit_sha": commit["sha"], "merged_by": _actor(),
    })
    # Freeze both ends at what was merged: a merged PR keeps showing its own
    # diff, which pointing base at the merge commit would erase.
    pull["head"]["sha"] = head_sha
    pull["base"]["sha"] = base_sha
    _touch(pull)
    return {"sha": commit["sha"], "merged": True, "message": "Pull Request successfully merged"}


@app.put("/repos/{owner}/{name}/pulls/{number}/update-branch", status_code=202)
async def update_pull_request_branch(owner: str, name: str, number: int, request: Request):
    """Merge the base branch into the head branch (202, as GitHub queues the job)."""
    repo = _repo(owner, name)
    pull = _pull(repo["full_name"], number)
    head_ref = pull["head"]["ref"]
    if head_ref not in repo["branches"]:
        raise _not_found()
    head_sha, base_sha = _pull_head_sha(repo, pull), _pull_base_sha(repo, pull)
    fork = _merge_base(repo, head_sha, base_sha) or head_sha
    fork_tree = _tree_at(repo, fork) or {}
    head_tree, base_tree = _tree_at(repo, head_sha) or {}, _tree_at(repo, base_sha) or {}
    merged = dict(head_tree)
    for path, sha in base_tree.items():
        if fork_tree.get(path) != sha and head_tree.get(path) == fork_tree.get(path):
            merged[path] = sha
    commit = _add_commit(repo, message=f"Merge branch '{pull['base']['ref']}' into {head_ref}",
                         tree=merged, parents=[head_sha, base_sha], files=[])
    _move_branch(repo, head_ref, commit["sha"])
    _touch(pull)
    return JSONResponse(status_code=202, content={
        "message": "Updating pull request branch.",
        "url": f"{_api_base(request)}/repos/{repo['full_name']}/pulls/{number}",
    })


@app.get("/repos/{owner}/{name}/pulls/{number}/commits")
def list_pull_commits(owner: str, name: str, number: int, request: Request,
                      per_page: int = 30, page: int = 1):
    repo = _repo(owner, name)
    pull = _pull(repo["full_name"], number)
    base = _api_base(request)
    commits = [_commit_json(base, repo["full_name"], c) for c in _pull_commits(repo, pull)]
    return _paginated(commits, request, per_page, page)


@app.get("/repos/{owner}/{name}/pulls/{number}/files")
def list_pull_files(owner: str, name: str, number: int, request: Request,
                    per_page: int = 30, page: int = 1):
    """The head-vs-base diff, computed from the trees — never a stored list."""
    repo = _repo(owner, name)
    pull = _pull(repo["full_name"], number)
    base = _api_base(request)
    repo_key = repo["full_name"]
    head = _pull_head_sha(repo, pull)
    files = [{
        **entry,
        "blob_url": f"{HTML_BASE}/{repo_key}/blob/{head}/{entry['filename']}",
        "raw_url": f"{HTML_BASE}/{repo_key}/raw/{head}/{entry['filename']}",
        "contents_url": f"{base}/repos/{repo_key}/contents/{entry['filename']}?ref={head}",
    } for entry in _pull_files(repo, pull)]
    return _paginated(files, request, per_page, page)


@app.get("/repos/{owner}/{name}/pulls/{number}/reviews")
def list_reviews(owner: str, name: str, number: int, request: Request,
                 per_page: int = 30, page: int = 1):
    repo_key = _repo(owner, name)["full_name"]
    pull = _pull(repo_key, number)
    base = _api_base(request)
    reviews = [_review_json(base, repo_key, number, r) for r in pull["_reviews"]]
    return _paginated(reviews, request, per_page, page)


@app.post("/repos/{owner}/{name}/pulls/{number}/reviews")
async def create_review(owner: str, name: str, number: int, request: Request):
    repo = _repo(owner, name)
    repo_key = repo["full_name"]
    pull = _pull(repo_key, number)
    body = await _body(request)
    event = (body.get("event") or "PENDING").upper()
    states = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED",
              "COMMENT": "COMMENTED", "PENDING": "PENDING"}
    if event not in states:
        raise _invalid("PullRequestReview", "event", "invalid")
    if event in ("REQUEST_CHANGES", "COMMENT") and not body.get("body"):
        raise _invalid("PullRequestReview", "body")
    review = {
        "id": _next_id("review_id"),
        "user": _actor(),
        "body": body.get("body") or "",
        "state": states[event],
        "submitted_at": None if event == "PENDING" else _now(),
        "commit_id": body.get("commit_id") or _pull_head_sha(repo, pull),
    }
    pull["_reviews"].append(review)
    # A submitted review clears the reviewer's pending request, as GitHub does.
    if event != "PENDING":
        pull["requested_reviewers"] = [r for r in pull.get("requested_reviewers") or []
                                       if r["login"] != review["user"]["login"]]
    _touch(pull)
    return _review_json(_api_base(request), repo_key, number, review)


@app.get("/repos/{owner}/{name}/pulls/{number}/reviews/{review_id}")
def get_review(owner: str, name: str, number: int, review_id: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    pull = _pull(repo_key, number)
    review = next((r for r in pull["_reviews"] if r["id"] == review_id), None)
    if review is None:
        raise _not_found()
    return _review_json(_api_base(request), repo_key, number, review)


@app.get("/repos/{owner}/{name}/pulls/{number}/comments")
def list_review_comments(owner: str, name: str, number: int, request: Request,
                         per_page: int = 30, page: int = 1):
    repo_key = _repo(owner, name)["full_name"]
    _pull(repo_key, number)
    base = _api_base(request)
    comments = [_comment_json(base, c) for c in STATE["comments"].values()
                if c.get("_issue") == f"{repo_key}#{number}" and c.get("_kind") == "review"]
    return _paginated(comments, request, per_page, page)


@app.post("/repos/{owner}/{name}/pulls/{number}/comments", status_code=201)
async def create_review_comment(owner: str, name: str, number: int, request: Request):
    repo = _repo(owner, name)
    repo_key = repo["full_name"]
    pull = _pull(repo_key, number)
    body = await _body(request)
    if not body.get("body"):
        raise _invalid("PullRequestReviewComment", "body")
    reply_to = body.get("in_reply_to")
    if not reply_to and not body.get("path"):
        raise _invalid("PullRequestReviewComment", "path")
    comment = {
        "id": _next_id("comment_id"),
        "body": body["body"],
        "user": _actor(),
        "created_at": _now(),
        "updated_at": _now(),
        "path": body.get("path"),
        "line": body.get("line"),
        "side": body.get("side") or "RIGHT",
        "position": body.get("position"),
        "commit_id": body.get("commit_id") or _pull_head_sha(repo, pull),
        "in_reply_to_id": reply_to,
        "_issue": f"{repo_key}#{number}",
        "_kind": "review",
    }
    STATE["comments"][str(comment["id"])] = comment
    _touch(pull)
    return JSONResponse(status_code=201, content=_comment_json(_api_base(request), comment))


@app.get("/repos/{owner}/{name}/pulls/{number}/requested_reviewers")
def list_requested_reviewers(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    pull = _pull(repo_key, number)
    base = _api_base(request)
    return {"users": [_user_json(base, r) for r in pull.get("requested_reviewers") or []],
            "teams": []}


@app.post("/repos/{owner}/{name}/pulls/{number}/requested_reviewers", status_code=201)
async def request_reviewers(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    pull = _pull(repo_key, number)
    body = await _body(request)
    reviewers = body.get("reviewers") or []
    if not reviewers and not body.get("team_reviewers"):
        raise _invalid("PullRequest", "reviewers")
    existing = {r["login"] for r in pull.get("requested_reviewers") or []}
    pull["requested_reviewers"] = (pull.get("requested_reviewers") or []) + [
        _user(login) for login in reviewers
        if login not in existing and login != pull["user"]["login"]
    ]
    _touch(pull)
    return JSONResponse(status_code=201, content=_pull_json(_api_base(request), pull))


@app.delete("/repos/{owner}/{name}/pulls/{number}/requested_reviewers")
async def remove_requested_reviewers(owner: str, name: str, number: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    pull = _pull(repo_key, number)
    body = await _body(request)
    drop = set(body.get("reviewers") or [])
    pull["requested_reviewers"] = [r for r in pull.get("requested_reviewers") or []
                                   if r["login"] not in drop]
    _touch(pull)
    return _pull_json(_api_base(request), pull)


# --- releases -------------------------------------------------------------

@app.post("/repos/{owner}/{name}/releases", status_code=201)
async def create_release(owner: str, name: str, request: Request):
    repo = _repo(owner, name)
    body = await _body(request)
    tag = body.get("tag_name")
    if not tag:
        raise _invalid("Release", "tag_name")
    repo_key = repo["full_name"]
    if f"{repo_key}#{tag}" in STATE["releases"]:
        raise _invalid("Release", "tag_name", "already_exists")
    target = body.get("target_commitish") or repo["default_branch"]
    sha = _resolve(repo, target)
    if sha is None:
        raise _invalid("Release", "target_commitish", "invalid")
    repo.setdefault("tags", {}).setdefault(tag, {"name": tag, "sha": sha})
    release = {
        "id": _next_id("release_id"),
        "_repo": repo_key,
        "tag_name": tag,
        "target_commitish": target,
        "name": body.get("name") or tag,
        "body": body.get("body") or "",
        "draft": bool(body.get("draft")),
        "prerelease": bool(body.get("prerelease")),
        "created_at": _now(),
        "published_at": None if body.get("draft") else _now(),
        "author": _actor(),
    }
    STATE["releases"][f"{repo_key}#{tag}"] = release
    return JSONResponse(status_code=201, content=_release_json(_api_base(request), release))


@app.get("/repos/{owner}/{name}/releases")
def list_releases(owner: str, name: str, request: Request, per_page: int = 30, page: int = 1):
    repo_key = _repo(owner, name)["full_name"]
    base = _api_base(request)
    releases = [_release_json(base, r) for r in _repo_records("releases", repo_key)]
    releases.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return _paginated(releases, request, per_page, page)


@app.get("/repos/{owner}/{name}/releases/latest")
def get_latest_release(owner: str, name: str, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    published = [r for r in _repo_records("releases", repo_key) if not r.get("draft")]
    if not published:
        raise _not_found()
    latest = max(published, key=lambda r: r.get("created_at") or "")
    return _release_json(_api_base(request), latest)


@app.get("/repos/{owner}/{name}/releases/tags/{tag}")
def get_release_by_tag(owner: str, name: str, tag: str, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    release = STATE["releases"].get(f"{repo_key}#{tag}")
    if release is None:
        raise _not_found()
    return _release_json(_api_base(request), release)


@app.get("/repos/{owner}/{name}/releases/{release_id}")
def get_release(owner: str, name: str, release_id: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    release = next((r for r in _repo_records("releases", repo_key) if r["id"] == release_id), None)
    if release is None:
        raise _not_found()
    return _release_json(_api_base(request), release)


# --- actions --------------------------------------------------------------

@app.get("/repos/{owner}/{name}/actions/workflows")
def list_workflows(owner: str, name: str, request: Request, per_page: int = 30, page: int = 1):
    repo = _repo(owner, name)
    base = _api_base(request)
    workflows = [_workflow_json(base, repo["full_name"], w) for w in _workflows(repo)]
    return _paginated(workflows, request, per_page, page, envelope="workflows")


@app.get("/repos/{owner}/{name}/actions/workflows/{workflow_id}")
def get_workflow(owner: str, name: str, workflow_id: str, request: Request):
    repo = _repo(owner, name)
    workflow = next((w for w in _workflows(repo)
                     if str(w["id"]) == workflow_id or w["path"].endswith(f"/{workflow_id}")), None)
    if workflow is None:
        raise _not_found()
    return _workflow_json(_api_base(request), repo["full_name"], workflow)


@app.post("/repos/{owner}/{name}/actions/workflows/{workflow_id}/dispatches", status_code=204)
async def dispatch_workflow(owner: str, name: str, workflow_id: str, request: Request):
    """Start a run for a ``workflow_dispatch`` workflow (204, run created in the background)."""
    repo = _repo(owner, name)
    body = await _body(request)
    workflow = next((w for w in _workflows(repo)
                     if str(w["id"]) == workflow_id or w["path"].endswith(f"/{workflow_id}")), None)
    if workflow is None:
        raise _not_found()
    ref = body.get("ref") or repo["default_branch"]
    sha = _resolve(repo, ref)
    if sha is None:
        raise _invalid("WorkflowDispatch", "ref", "invalid")
    run_id = _next_id("run_id")
    STATE["workflow_runs"][f"{repo['full_name']}#{run_id}"] = {
        "id": run_id, "name": workflow["name"], "workflow_id": workflow["id"],
        "status": "queued", "conclusion": None, "event": "workflow_dispatch",
        "head_branch": ref, "head_sha": sha, "run_number": run_id,
        "created_at": _now(), "updated_at": _now(),
        "html_url": f"{HTML_BASE}/{repo['full_name']}/actions/runs/{run_id}",
    }
    return Response(status_code=204)


@app.get("/repos/{owner}/{name}/actions/workflows/{workflow_id}/runs")
def list_workflow_runs_for_workflow(owner: str, name: str, workflow_id: str, request: Request,
                                    status: str | None = None, branch: str | None = None,
                                    per_page: int = 30, page: int = 1):
    repo = _repo(owner, name)
    workflow = next((w for w in _workflows(repo)
                     if str(w["id"]) == workflow_id or w["path"].endswith(f"/{workflow_id}")), None)
    if workflow is None:
        raise _not_found()
    return _runs_page(repo, request, status, branch, per_page, page,
                      predicate=lambda r: r.get("workflow_id") == workflow["id"]
                      or r.get("name") == workflow["name"])


def _runs_page(repo: dict, request: Request, status: str | None, branch: str | None,
               per_page: int, page: int, predicate=None) -> JSONResponse:
    runs = _repo_records("workflow_runs", repo["full_name"])
    if predicate is not None:
        runs = [r for r in runs if predicate(r)]
    if status:
        runs = [r for r in runs if status in (r.get("status"), r.get("conclusion"))]
    if branch:
        runs = [r for r in runs if r.get("head_branch") == branch]
    runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    base = _api_base(request)
    return _paginated([_run_json(base, repo["full_name"], r) for r in runs],
                      request, per_page, page, envelope="workflow_runs")


@app.get("/repos/{owner}/{name}/actions/runs")
def list_workflow_runs(owner: str, name: str, request: Request, status: str | None = None,
                       branch: str | None = None, event: str | None = None,
                       per_page: int = 30, page: int = 1):
    repo = _repo(owner, name)
    return _runs_page(repo, request, status, branch, per_page, page,
                      predicate=(lambda r: r.get("event") == event) if event else None)


@app.get("/repos/{owner}/{name}/actions/runs/{run_id}")
def get_workflow_run(owner: str, name: str, run_id: int, request: Request):
    repo_key = _repo(owner, name)["full_name"]
    run = STATE["workflow_runs"].get(f"{repo_key}#{run_id}")
    if run is None:
        raise _not_found()
    return _run_json(_api_base(request), repo_key, run)


@app.post("/repos/{owner}/{name}/actions/runs/{run_id}/rerun", status_code=201)
def rerun_workflow_run(owner: str, name: str, run_id: int):
    repo_key = _repo(owner, name)["full_name"]
    run = STATE["workflow_runs"].get(f"{repo_key}#{run_id}")
    if run is None:
        raise _not_found()
    run.update({"status": "queued", "conclusion": None, "updated_at": _now(),
                "run_attempt": int(run.get("run_attempt") or 1) + 1})
    return {}


@app.post("/repos/{owner}/{name}/actions/runs/{run_id}/cancel", status_code=202)
def cancel_workflow_run(owner: str, name: str, run_id: int):
    repo_key = _repo(owner, name)["full_name"]
    run = STATE["workflow_runs"].get(f"{repo_key}#{run_id}")
    if run is None:
        raise _not_found()
    if run.get("status") == "completed":
        raise _ApiError(409, "Cannot cancel a workflow run that is completed")
    run.update({"status": "completed", "conclusion": "cancelled", "updated_at": _now()})
    return JSONResponse(status_code=202, content={})


# --- search ---------------------------------------------------------------

@app.get("/search/issues")
def search_issues(request: Request, q: str = "", sort: str = "created", order: str = "desc",
                  per_page: int = 30, page: int = 1):
    """Issue search with the qualifiers agents actually type (``repo:``, ``is:``, ``label:``)."""
    qualifiers, terms = _parse_query(q)
    matches = []
    for store in ("issues", "pulls"):
        for key, record in STATE[store].items():
            repo_key = key.split("#")[0]
            if _matches_search(record, repo_key, qualifiers, terms):
                matches.append(record)
    base = _api_base(request)
    items = [{**_issue_json(base, r), "score": 1.0} for r in _sorted_issues(matches, sort, order)]
    return _paginated(items, request, per_page, page, envelope="items", search=True)


@app.get("/search/repositories")
def search_repositories(request: Request, q: str = "", sort: str = "", order: str = "desc",
                        per_page: int = 30, page: int = 1):
    qualifiers, terms = _parse_query(q)
    base = _api_base(request)
    matches = []
    for repo in STATE["repos"].values():
        owner = repo["owner"]["login"].lower()
        if any(name in ("user", "org", "owner") and value.lower() != owner
               for name, value, _ in qualifiers):
            continue
        haystack = f"{repo['full_name']} {repo.get('description') or ''}".lower()
        if all(term in haystack for term in terms):
            matches.append({**_repo_json(base, repo), "score": 1.0})
    return _paginated(matches, request, per_page, page, envelope="items", search=True)


@app.get("/search/code")
def search_code(request: Request, q: str = "", per_page: int = 30, page: int = 1):
    qualifiers, terms = _parse_query(q)
    repo_filter = {value.lower() for name, value, _ in qualifiers if name == "repo"}
    base = _api_base(request)
    items = []
    for repo_key, repo in STATE["repos"].items():
        if repo_filter and repo_key.lower() not in repo_filter:
            continue
        for path, entry in (repo.get("files") or {}).items():
            haystack = f"{path}\n{entry['content']}".lower()
            if not all(term in haystack for term in terms):
                continue
            items.append({
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "sha": entry["sha"],
                "url": f"{base}/repos/{repo_key}/contents/{path}",
                "html_url": f"{HTML_BASE}/{repo_key}/blob/{repo['default_branch']}/{path}",
                "repository": _repo_json(base, repo),
                "score": 1.0,
            })
    return _paginated(items, request, per_page, page, envelope="items", search=True)


@app.get("/search/users")
def search_users(request: Request, q: str = "", per_page: int = 30, page: int = 1):
    _, terms = _parse_query(q)
    base = _api_base(request)
    items = [{**(_user_json(base, u) or {}), "score": 1.0} for u in STATE["users"].values()
             if all(term in u["login"].lower() for term in terms)]
    return _paginated(items, request, per_page, page, envelope="items", search=True)


# --- MCP transport -------------------------------------------------------
# Mount the GitHub MCP server at /mcp on this same FastAPI app so REST and
# MCP share the same STATE dict.

from checkpoint.mcp_servers.github_mcp import mount_on as _mount_mcp

_mount_mcp(app)
