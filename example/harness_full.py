#!/usr/bin/env python3
"""Phase 2 acceptance harness — no LLM. Drives the GitHub twin through:
  1. create repo
  2. push files on a feature branch
  3. open a PR
  4. list workflow runs (no-op, exercises the surface)
  5. search code
  6. merge the PR

Writes a final JSON answer to stdout. Used by example/scenarios/github-full-surface.md.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests

BASE = os.environ.get("CHECKPOINT_GITHUB_URL") or "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt")
ARCHAL_OUT = Path(os.environ.get("ARCHAL_OUT_DIR", "/archal-out"))

H = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "checkpoint-acceptance/0.1",
}

OWNER = "acme"
REPO = "webapp"
BRANCH = "feature/launch"


def post(path, **kw):
    return requests.post(f"{BASE}{path}", headers=H, timeout=15, **kw)


def get(path, **kw):
    return requests.get(f"{BASE}{path}", headers=H, timeout=15, **kw)


def put(path, **kw):
    return requests.put(f"{BASE}{path}", headers=H, timeout=15, **kw)


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def step(label, resp):
    print(f"[full] {label} -> {resp.status_code}", file=sys.stderr)
    return resp


def main():
    errors: list[str] = []

    # 1. create repo
    r = step("create repo", post("/user/repos", json={"name": REPO, "owner": OWNER}))
    if r.status_code not in (201, 422):
        errors.append(f"create repo: {r.status_code} {r.text[:120]}")

    # need main sha
    branches = get(f"/repos/{OWNER}/{REPO}/branches").json()
    main_sha = next((b["commit"]["sha"] for b in branches if b["name"] == "main"), None)

    # 2. create feature branch
    if main_sha:
        r = step(
            "create branch",
            post(
                f"/repos/{OWNER}/{REPO}/git/refs",
                json={"ref": f"refs/heads/{BRANCH}", "sha": main_sha},
            ),
        )
        if r.status_code not in (201, 422):
            errors.append(f"create branch: {r.status_code}")

    # 3. push files
    r = step(
        "push files",
        post(
            f"/repos/{OWNER}/{REPO}/_push_files",
            json={
                "branch": BRANCH,
                "message": "Launch initial files",
                "files": [
                    {"path": "README.md", "content": "# webapp\nLaunched.\n"},
                    {"path": "src/app.py", "content": "print('hello launch')\n"},
                ],
            },
        ),
    )
    if r.status_code != 201:
        errors.append(f"push: {r.status_code}")

    # 4. open PR
    r = step(
        "create PR",
        post(
            f"/repos/{OWNER}/{REPO}/pulls",
            json={
                "title": "Launch initial version",
                "head": BRANCH,
                "base": "main",
                "body": "Initial launch PR",
            },
        ),
    )
    pr_number = None
    if r.status_code == 201:
        pr_number = r.json()["number"]
    else:
        errors.append(f"create PR: {r.status_code} {r.text[:120]}")

    # 5. list workflow runs (just exercises the endpoint)
    step("list workflow runs", get(f"/repos/{OWNER}/{REPO}/actions/runs"))

    # 6. search code
    step("search code", get("/search/code", params={"q": "launch"}))

    # 7. merge PR
    if pr_number:
        r = step(
            "merge PR",
            put(
                f"/repos/{OWNER}/{REPO}/pulls/{pr_number}/merge",
                json={"commit_message": "Merge launch PR"},
            ),
        )
        if r.status_code != 200:
            errors.append(f"merge: {r.status_code}")

    final = (
        f"Created repo {OWNER}/{REPO}, pushed 2 files, "
        f"opened and merged PR #{pr_number} (Launch initial version)."
    )

    # /archal-out artifacts (best effort)
    try:
        ARCHAL_OUT.mkdir(parents=True, exist_ok=True)
        (ARCHAL_OUT / "metrics.json").write_text(
            json.dumps(
                {
                    "version": 1, "llmCallCount": 0, "toolCallCount": 7,
                    "toolErrorCount": len(errors), "exitReason": "completed",
                    "provider": "none", "model": "acceptance",
                }
            )
        )
        (ARCHAL_OUT / "agent-trace.json").write_text(
            json.dumps({"version": 1, "final": final, "events": []})
        )
    except OSError:
        pass

    print(json.dumps({"text": final}))
    if errors:
        print("\n".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
