"""SBX-02: gh CLI bridge end-to-end against a live github twin.

We spin up the github twin on a free port (real uvicorn subprocess) and then
call `checkpoint.sandbox.gh_bridge.main()` directly with argv lists. This
covers the same surface a sandbox user would hit via `gh ...`.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import httpx
import pytest

from checkpoint.sandbox import gh_bridge


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/_health", timeout=1.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


@pytest.fixture(scope="module")
def github_twin():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "checkpoint.twins.github:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_healthy(port), "github twin did not start"
        # Seed with small-project so repo / issues exist.
        httpx.post(f"http://127.0.0.1:{port}/_seed/small-project", timeout=5)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextmanager
def _env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: str(v) for k, v in kw.items()})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _first_repo_full_name(url: str) -> str | None:
    state = httpx.get(f"{url}/_state").json()
    repos = state.get("repos") or state.get("repositories")
    if not repos:
        return None
    first = next(iter(repos.values())) if isinstance(repos, dict) else repos[0]
    return first.get("full_name")


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = gh_bridge.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_auth_status(github_twin):
    with _env(CHECKPOINT_GITHUB_URL=f"http://127.0.0.1:{github_twin}"):
        rc, out, err = _run(["auth", "status"])
    assert rc == 0
    assert "Logged in to api.github.com (twin)" in out
    # gh writes the bulk of auth status to stderr — match parity.
    assert "Logged in to api.github.com (twin)" in err


def test_help_returns_zero():
    rc, out, _ = _run(["--help"])
    assert rc == 0
    assert "Supported subcommands" in out


def test_version_returns_zero():
    rc, out, _ = _run(["--version"])
    assert rc == 0
    assert "version" in out


def test_unsupported_subcommand_exits_2():
    rc, out, err = _run(["release", "create"])
    assert rc == 2
    assert "not supported" in err


def test_repo_view_human(github_twin):
    """The small-project seed should ship at least one repo."""
    url = f"http://127.0.0.1:{github_twin}"
    full = _first_repo_full_name(url)
    if not full:
        pytest.skip("seed has no repos")
    assert full and "/" in full

    with _env(CHECKPOINT_GITHUB_URL=url):
        rc, out, err = _run(["repo", "view", full])
    assert rc == 0, err
    assert full in out


def test_repo_view_json(github_twin):
    url = f"http://127.0.0.1:{github_twin}"
    full = _first_repo_full_name(url)
    if not full:
        pytest.skip("seed has no repos")

    with _env(CHECKPOINT_GITHUB_URL=url):
        rc, out, _ = _run(["repo", "view", full, "--json", "name,full_name"])
    assert rc == 0
    body = json.loads(out)
    assert body["full_name"] == full
    assert "name" in body


def test_issue_create_then_list_then_view(github_twin):
    url = f"http://127.0.0.1:{github_twin}"
    full = _first_repo_full_name(url)
    if not full:
        pytest.skip("seed has no repos")

    with _env(CHECKPOINT_GITHUB_URL=url, GH_REPO=full):
        rc, out, err = _run(["issue", "create",
                             "--title", "bridge-test",
                             "--body", "from gh_bridge"])
        assert rc == 0, err
        assert out.strip()

        # list
        rc2, out2, _ = _run(["issue", "list", "--json", "number,title,state"])
        assert rc2 == 0
        rows = json.loads(out2)
        titles = {r["title"] for r in rows}
        assert "bridge-test" in titles

        # view (by num)
        num = next(r["number"] for r in rows if r["title"] == "bridge-test")
        rc3, out3, _ = _run(["issue", "view", str(num)])
        assert rc3 == 0
        assert "bridge-test" in out3


def test_issue_comment(github_twin):
    url = f"http://127.0.0.1:{github_twin}"
    full = _first_repo_full_name(url)
    if not full:
        pytest.skip("seed has no repos")

    with _env(CHECKPOINT_GITHUB_URL=url, GH_REPO=full):
        # need an issue to comment on
        rc, out, _ = _run(["issue", "create", "--title", "for-comment", "--body", "x"])
        assert rc == 0
        rc2, out2, _ = _run(["issue", "list", "--json", "number,title"])
        rows = json.loads(out2)
        num = next(r["number"] for r in rows if r["title"] == "for-comment")
        rc3, _, _ = _run(["issue", "comment", str(num), "--body", "hello!"])
        assert rc3 == 0


def test_pr_list(github_twin):
    """pr list should succeed even on an empty repo — returns []."""
    url = f"http://127.0.0.1:{github_twin}"
    full = _first_repo_full_name(url)
    if not full:
        pytest.skip("seed has no repos")

    with _env(CHECKPOINT_GITHUB_URL=url, GH_REPO=full):
        rc, out, _ = _run(["pr", "list", "--json", "number,title,state"])
    assert rc == 0
    assert json.loads(out) == [] or isinstance(json.loads(out), list)


def test_issue_create_missing_args():
    rc, _, err = _run(["issue", "create"])
    assert rc == 2
    assert "requires" in err
