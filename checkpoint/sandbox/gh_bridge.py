"""SBX-02: gh CLI -> GitHub-twin REST bridge.

A *minimal* `gh` clone. Covers ~10 subcommands an agent actually uses inside
the sandbox image. Anything else exits 2 with a "not supported" message —
on purpose. The full GitHub CLI is L-effort and out of scope.

Subcommands supported:
  - gh auth status
  - gh repo view [owner/repo] [--json fields]
  - gh issue list [-R owner/repo] [--json fields]
  - gh issue create -R owner/repo --title T --body B
  - gh issue view <num> -R owner/repo [--json fields]
  - gh issue comment <num> -R owner/repo --body B
  - gh pr list [-R owner/repo] [--json fields]
  - gh pr create -R owner/repo --title T --body B --head H --base B
  - gh pr view <num> -R owner/repo [--json fields]
  - gh pr merge <num> -R owner/repo

Twin URL: `CHECKPOINT_GITHUB_URL` (default ``http://127.0.0.1:8000``).
Bootstrap token: `GITHUB_TOKEN` (default to the canonical twin token).
Default repo context: `GH_REPO` env var (mimics ``gh``'s GH_REPO).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx


DEFAULT_TOKEN = "ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt"


# ---------------------------------------------------------------------------
# Tiny argv parser (we deliberately avoid argparse — gh's flag shapes don't
# fit argparse cleanly across subcommands, and our subset is small).
# ---------------------------------------------------------------------------


def _pop_flag_value(argv: list[str], *names: str) -> str | None:
    """Find any of `names` in argv; pop it + its value; return the value."""
    for n in names:
        if n in argv:
            i = argv.index(n)
            if i + 1 < len(argv):
                val = argv[i + 1]
                del argv[i : i + 2]
                return val
            del argv[i : i + 1]
            return ""
    return None


def _pop_bool_flag(argv: list[str], *names: str) -> bool:
    """Pop a boolean flag (`--json` or similar) if present."""
    for n in names:
        if n in argv:
            argv.remove(n)
            return True
    return False


def _resolve_repo(argv: list[str]) -> tuple[str, str] | None:
    """Return (owner, name) parsed from -R/--repo or argv[0], else None.

    Also consumes the flag/positional it took.
    """
    val = _pop_flag_value(argv, "-R", "--repo")
    if val is None:
        # Some commands take owner/repo as a positional.
        if argv and "/" in argv[0] and not argv[0].startswith("-"):
            val = argv.pop(0)
    if val is None:
        val = os.environ.get("GH_REPO", "")
    if "/" not in val:
        return None
    owner, _, name = val.partition("/")
    return owner, name


def _base_url() -> str:
    return os.environ.get("CHECKPOINT_GITHUB_URL", "http://127.0.0.1:8000").rstrip("/")


def _headers() -> dict[str, str]:
    tok = os.environ.get("GITHUB_TOKEN", DEFAULT_TOKEN)
    return {"Authorization": f"token {tok}", "Accept": "application/vnd.github+json"}


def _get(path: str) -> tuple[int, Any]:
    r = httpx.get(f"{_base_url()}{path}", headers=_headers(), timeout=10.0)
    return r.status_code, _safe_json(r)


def _post(path: str, body: dict) -> tuple[int, Any]:
    r = httpx.post(f"{_base_url()}{path}", headers=_headers(), json=body, timeout=10.0)
    return r.status_code, _safe_json(r)


def _put(path: str, body: dict | None = None) -> tuple[int, Any]:
    r = httpx.put(f"{_base_url()}{path}", headers=_headers(), json=body or {}, timeout=10.0)
    return r.status_code, _safe_json(r)


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}


def _project_json(obj: Any, fields_csv: str | None) -> Any:
    """Mimic `gh ... --json a,b,c` projection.

    If `fields_csv` is None, return obj unchanged. Else:
      - dict -> dict with only the requested fields (missing -> null)
      - list -> list of projected dicts
    Unknown structures pass through.
    """
    if fields_csv is None:
        return obj
    fields = [f.strip() for f in fields_csv.split(",") if f.strip()]
    if isinstance(obj, list):
        return [_project_json(item, fields_csv) for item in obj]
    if isinstance(obj, dict):
        return {f: obj.get(f) for f in fields}
    return obj


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _err(msg: str) -> int:
    sys.stderr.write(f"gh: {msg}\n")
    return 2


def _emit_json(obj: Any) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0


def cmd_auth(argv: list[str]) -> int:
    if argv and argv[0] == "status":
        url = _base_url()
        # gh's real output goes to stderr; we mirror that for parity.
        sys.stderr.write(
            f"github.com\n"
            f"  ✓ Logged in to api.github.com (twin) as agent ({url})\n"
            f"  ✓ Git operations protocol: https\n"
            f"  ✓ Token: gho_*****************\n"
        )
        # ALSO print a stdout line so tests can match on stdout if they want.
        print(f"Logged in to api.github.com (twin) as agent at {url}")
        return 0
    return _err(f"auth: subcommand '{argv[0] if argv else ''}' not supported")


def cmd_repo(argv: list[str]) -> int:
    if not argv:
        return _err("repo: missing subcommand")
    sub = argv.pop(0)
    if sub == "view":
        json_fields = _pop_flag_value(argv, "--json")
        repo = _resolve_repo(argv)
        if not repo:
            return _err("repo view: owner/repo required (positional or -R)")
        owner, name = repo
        status, body = _get(f"/repos/{owner}/{name}")
        if status >= 400:
            return _err(f"repo view: HTTP {status}: {body}")
        if json_fields is not None:
            return _emit_json(_project_json(body, json_fields))
        # human-readable blurb
        print(f"{body.get('full_name', f'{owner}/{name}')}")
        print(body.get("description", "") or "")
        print(f"stars: {body.get('stargazers_count', 0)}  forks: {body.get('forks_count', 0)}")
        return 0
    return _err(f"repo: subcommand '{sub}' not supported")


def cmd_issue(argv: list[str]) -> int:
    if not argv:
        return _err("issue: missing subcommand")
    sub = argv.pop(0)
    if sub == "list":
        json_fields = _pop_flag_value(argv, "--json")
        repo = _resolve_repo(argv)
        if not repo:
            return _err("issue list: owner/repo required (-R or GH_REPO)")
        owner, name = repo
        status, body = _get(f"/repos/{owner}/{name}/issues")
        if status >= 400:
            return _err(f"issue list: HTTP {status}: {body}")
        if json_fields is not None:
            return _emit_json(_project_json(body, json_fields))
        for it in body if isinstance(body, list) else []:
            print(f"#{it.get('number')}\t{it.get('state', '?'):>6}\t{it.get('title', '')}")
        return 0
    if sub == "create":
        title = _pop_flag_value(argv, "--title", "-t")
        body_text = _pop_flag_value(argv, "--body", "-b") or ""
        repo = _resolve_repo(argv)
        if not (repo and title):
            return _err("issue create: requires -R owner/repo and --title")
        owner, name = repo
        status, body = _post(f"/repos/{owner}/{name}/issues",
                             {"title": title, "body": body_text})
        if status >= 400:
            return _err(f"issue create: HTTP {status}: {body}")
        print(body.get("html_url") or f"#{body.get('number')}")
        return 0
    if sub == "view":
        json_fields = _pop_flag_value(argv, "--json")
        if not argv:
            return _err("issue view: issue number required")
        num = argv.pop(0)
        repo = _resolve_repo(argv)
        if not repo:
            return _err("issue view: -R owner/repo required")
        owner, name = repo
        status, body = _get(f"/repos/{owner}/{name}/issues/{num}")
        if status >= 400:
            return _err(f"issue view: HTTP {status}: {body}")
        if json_fields is not None:
            return _emit_json(_project_json(body, json_fields))
        print(f"#{body.get('number')} {body.get('title', '')}")
        print(f"state: {body.get('state', '?')}")
        print()
        print(body.get("body") or "")
        return 0
    if sub == "comment":
        if not argv:
            return _err("issue comment: issue number required")
        num = argv.pop(0)
        body_text = _pop_flag_value(argv, "--body", "-b") or ""
        repo = _resolve_repo(argv)
        if not repo:
            return _err("issue comment: -R owner/repo required")
        owner, name = repo
        status, body = _post(f"/repos/{owner}/{name}/issues/{num}/comments",
                             {"body": body_text})
        if status >= 400:
            return _err(f"issue comment: HTTP {status}: {body}")
        print(body.get("html_url") or "ok")
        return 0
    return _err(f"issue: subcommand '{sub}' not supported")


def cmd_pr(argv: list[str]) -> int:
    if not argv:
        return _err("pr: missing subcommand")
    sub = argv.pop(0)
    if sub == "list":
        json_fields = _pop_flag_value(argv, "--json")
        repo = _resolve_repo(argv)
        if not repo:
            return _err("pr list: -R owner/repo required")
        owner, name = repo
        status, body = _get(f"/repos/{owner}/{name}/pulls")
        if status >= 400:
            return _err(f"pr list: HTTP {status}: {body}")
        if json_fields is not None:
            return _emit_json(_project_json(body, json_fields))
        for it in body if isinstance(body, list) else []:
            print(f"#{it.get('number')}\t{it.get('state', '?'):>6}\t{it.get('title', '')}")
        return 0
    if sub == "create":
        title = _pop_flag_value(argv, "--title", "-t")
        body_text = _pop_flag_value(argv, "--body", "-b") or ""
        head = _pop_flag_value(argv, "--head", "-H")
        base = _pop_flag_value(argv, "--base", "-B") or "main"
        repo = _resolve_repo(argv)
        if not (repo and title and head):
            return _err("pr create: requires -R, --title, --head")
        owner, name = repo
        status, body = _post(
            f"/repos/{owner}/{name}/pulls",
            {"title": title, "body": body_text, "head": head, "base": base},
        )
        if status >= 400:
            return _err(f"pr create: HTTP {status}: {body}")
        print(body.get("html_url") or f"#{body.get('number')}")
        return 0
    if sub == "view":
        json_fields = _pop_flag_value(argv, "--json")
        if not argv:
            return _err("pr view: PR number required")
        num = argv.pop(0)
        repo = _resolve_repo(argv)
        if not repo:
            return _err("pr view: -R owner/repo required")
        owner, name = repo
        status, body = _get(f"/repos/{owner}/{name}/pulls/{num}")
        if status >= 400:
            return _err(f"pr view: HTTP {status}: {body}")
        if json_fields is not None:
            return _emit_json(_project_json(body, json_fields))
        print(f"#{body.get('number')} {body.get('title', '')}")
        print(f"state: {body.get('state', '?')}  head: {body.get('head', {}).get('ref', '?')}  base: {body.get('base', {}).get('ref', '?')}")
        return 0
    if sub == "merge":
        if not argv:
            return _err("pr merge: PR number required")
        num = argv.pop(0)
        # gh accepts --squash/--rebase/--merge; we don't differentiate.
        _pop_bool_flag(argv, "--squash", "-s")
        _pop_bool_flag(argv, "--rebase", "-r")
        _pop_bool_flag(argv, "--merge", "-m")
        repo = _resolve_repo(argv)
        if not repo:
            return _err("pr merge: -R owner/repo required")
        owner, name = repo
        status, body = _put(f"/repos/{owner}/{name}/pulls/{num}/merge", {})
        if status >= 400:
            return _err(f"pr merge: HTTP {status}: {body}")
        print(body.get("message") or "Merged!")
        return 0
    return _err(f"pr: subcommand '{sub}' not supported")


HANDLERS = {
    "auth": cmd_auth,
    "repo": cmd_repo,
    "issue": cmd_issue,
    "pr": cmd_pr,
}


def _print_help() -> None:
    print(
        "gh (checkpoint sandbox bridge)\n"
        "\n"
        "Supported subcommands:\n"
        "  auth status\n"
        "  repo view [owner/repo] [--json fields]\n"
        "  issue list/create/view/comment\n"
        "  pr list/create/view/merge\n"
    )


def main(argv: list[str]) -> int:
    # Strip global flags we silently ignore (`--version`, `-v`).
    if "--version" in argv or "-v" in argv:
        print("gh version 0.0.1-checkpoint-sandbox")
        return 0
    if not argv or argv[0] in ("--help", "-h", "help"):
        _print_help()
        return 0
    cmd = argv.pop(0)
    handler = HANDLERS.get(cmd)
    if handler is None:
        return _err(f"'{cmd}' not supported in sandbox bridge")
    return handler(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
