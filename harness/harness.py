#!/usr/bin/env python3
"""
Real agent harness — GitHub + Supabase via official SDKs.

In local dev the sidecar intercepts:
  https://api.github.com  -> GitHub twin
  https://*.supabase.co   -> Supabase twin

The harness uses PyGithub and supabase-py pointed at production URLs.
It has no knowledge of twins, local ports, or checkpoint internals.
"""
from __future__ import annotations

import json
import os
import sys

from github import Auth, Github, GithubException
from openai import OpenAI

# ── configuration injected by checkpoint runner ───────────────────────────────
TASK = os.environ["CHECKPOINT_TASK"]
MODEL = os.environ.get("ARCHAL_ENGINE_MODEL", "gpt-4o-mini")
MAX_STEPS = int(os.environ.get("CHECKPOINT_MAX_STEPS", "12"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SUPABASE_URL = "https://checkpoint.supabase.co"   # sidecar intercepts -> supabase twin
SUPABASE_KEY = os.environ.get("SUPABASE_BOOTSTRAP_TOKEN", "")

# Lazy singletons — only instantiated when a tool that needs them is called.
_gh_client: Github | None = None
_sb_client = None


def _gh() -> Github:
    global _gh_client
    if _gh_client is None:
        _gh_client = Github(auth=Auth.Token(GITHUB_TOKEN))
    return _gh_client


def _sb():
    global _sb_client
    if _sb_client is None:
        from supabase import create_client
        _sb_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb_client

TRACE: list[dict] = []
LLM_CALLS = 0
TOOL_CALLS = 0


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── GitHub tools ───────────────────────────────────────────────────────────────

def github_create_issue(owner: str, repo: str, title: str, body: str = "") -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        issue = r.create_issue(title=title, body=body)
        TRACE.append({"_clone": "github", "method": "POST",
                      "path": f"/repos/{owner}/{repo}/issues", "status": 201})
        _log(f"[github] created issue #{issue.number}: {title}")
        return {"number": issue.number, "state": issue.state, "url": issue.html_url}
    except GithubException as e:
        TRACE.append({"_clone": "github", "method": "POST",
                      "path": f"/repos/{owner}/{repo}/issues", "status": e.status})
        return {"error": str(e)}


def github_list_issues(owner: str, repo: str, state: str = "open") -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        issues = [
            {"number": i.number, "title": i.title, "state": i.state}
            for i in r.get_issues(state=state)
        ]
        TRACE.append({"_clone": "github", "method": "GET",
                      "path": f"/repos/{owner}/{repo}/issues", "status": 200})
        _log(f"[github] listed {len(issues)} issues")
        return {"issues": issues}
    except GithubException as e:
        return {"error": str(e)}


def github_get_issue(owner: str, repo: str, number: int) -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        issue = r.get_issue(number)
        TRACE.append({"_clone": "github", "method": "GET",
                      "path": f"/repos/{owner}/{repo}/issues/{number}", "status": 200})
        return {"number": issue.number, "title": issue.title,
                "state": issue.state, "body": issue.body}
    except GithubException as e:
        return {"error": str(e)}


# ── Supabase tools ─────────────────────────────────────────────────────────────

def supabase_select(table: str, column: str = "*", eq_col: str = "", eq_val: str = "") -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        q = _sb().table(table).select(column)
        if eq_col and eq_val:
            q = q.eq(eq_col, eq_val)
        res = q.execute()
        TRACE.append({"_clone": "supabase", "method": "GET",
                      "path": f"/rest/v1/{table}", "status": 200})
        _log(f"[supabase] select {table} -> {len(res.data)} rows")
        return {"rows": res.data}
    except Exception as e:
        return {"error": str(e)}


def supabase_insert(table: str, record: dict) -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        res = _sb().table(table).insert(record).execute()
        TRACE.append({"_clone": "supabase", "method": "POST",
                      "path": f"/rest/v1/{table}", "status": 201})
        _log(f"[supabase] inserted into {table}: {list(record.keys())}")
        return {"inserted": res.data}
    except Exception as e:
        return {"error": str(e)}


def supabase_update(table: str, eq_col: str, eq_val: str, updates: dict) -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        res = _sb().table(table).update(updates).eq(eq_col, eq_val).execute()
        TRACE.append({"_clone": "supabase", "method": "PATCH",
                      "path": f"/rest/v1/{table}", "status": 200})
        _log(f"[supabase] updated {table} where {eq_col}={eq_val}: {updates}")
        return {"updated": res.data}
    except Exception as e:
        return {"error": str(e)}


def supabase_create_bucket(bucket_id: str, public: bool = False) -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        res = _sb().storage.create_bucket(bucket_id, options={"public": public})
        TRACE.append({"_clone": "supabase", "method": "POST",
                      "path": "/storage/v1/bucket", "status": 200})
        _log(f"[supabase] created bucket: {bucket_id} (public={public})")
        return {"name": bucket_id, "public": public}
    except Exception as e:
        return {"error": str(e)}


# ── OpenAI tool schema ─────────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "github_create_issue",
        "description": "Create a GitHub issue.",
        "parameters": {"type": "object", "required": ["owner", "repo", "title"],
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            }}}},
    {"type": "function", "function": {
        "name": "github_list_issues",
        "description": "List GitHub issues in a repo.",
        "parameters": {"type": "object", "required": ["owner", "repo"],
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
            }}}},
    {"type": "function", "function": {
        "name": "github_get_issue",
        "description": "Get a single GitHub issue by number.",
        "parameters": {"type": "object", "required": ["owner", "repo", "number"],
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "number": {"type": "integer"},
            }}}},
    {"type": "function", "function": {
        "name": "supabase_select",
        "description": "Query rows from a Supabase table.",
        "parameters": {"type": "object", "required": ["table"],
            "properties": {
                "table": {"type": "string"},
                "column": {"type": "string", "default": "*"},
                "eq_col": {"type": "string", "description": "Filter column name"},
                "eq_val": {"type": "string", "description": "Filter value (string)"},
            }}}},
    {"type": "function", "function": {
        "name": "supabase_insert",
        "description": "Insert a row into a Supabase table. All column values must be nested under the 'record' key as a JSON object.",
        "parameters": {"type": "object", "required": ["table", "record"],
            "properties": {
                "table": {"type": "string"},
                "record": {"type": "object", "description": "Key-value pairs of column names to values to insert. Example: {\"name\": \"Widget\", \"price\": 9.99}"},
            }}}},
    {"type": "function", "function": {
        "name": "supabase_update",
        "description": "Update rows in a Supabase table by equality filter.",
        "parameters": {"type": "object", "required": ["table", "eq_col", "eq_val", "updates"],
            "properties": {
                "table": {"type": "string"},
                "eq_col": {"type": "string"},
                "eq_val": {"type": "string"},
                "updates": {"type": "object"},
            }}}},
    {"type": "function", "function": {
        "name": "supabase_create_bucket",
        "description": "Create a Supabase storage bucket.",
        "parameters": {"type": "object", "required": ["bucket_id"],
            "properties": {
                "bucket_id": {"type": "string"},
                "public": {"type": "boolean", "default": False},
            }}}},
]

DISPATCH = {
    "github_create_issue": github_create_issue,
    "github_list_issues": github_list_issues,
    "github_get_issue": github_get_issue,
    "supabase_select": supabase_select,
    "supabase_insert": supabase_insert,
    "supabase_update": supabase_update,
    "supabase_create_bucket": supabase_create_bucket,
}


# ── Agent loop ────────────────────────────────────────────────────────────────

def _assistant_msg(msg) -> dict:
    out: dict = {"role": "assistant"}
    if msg.content:
        out["content"] = msg.content
    if msg.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]
    return out


def main():
    global LLM_CALLS
    oai = OpenAI()
    messages = [
        {"role": "system", "content": (
            "You are an autonomous agent that coordinates tasks across GitHub and Supabase. "
            "Use tools to complete the user's request precisely. "
            "When done, write a final answer stating exactly what was created or modified, "
            "including issue numbers, IDs, and any other identifiers."
        )},
        {"role": "user", "content": TASK},
    ]

    final_text = ""
    for step in range(MAX_STEPS):
        LLM_CALLS += 1
        _log(f"[agent] step {step + 1}/{MAX_STEPS}")
        resp = oai.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto",
        )
        msg = resp.choices[0].message
        messages.append(_assistant_msg(msg))

        if not msg.tool_calls:
            final_text = msg.content or ""
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            fn = DISPATCH.get(name)
            try:
                result = fn(**args) if fn else {"error": f"unknown tool: {name}"}
            except Exception as e:
                result = {"error": f"tool call failed: {e}"}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result)})
    else:
        final_text = final_text or "Reached max steps."

    # Write /archal-out artifacts for checkpoint to read.
    out_dir = os.environ.get("ARCHAL_OUT_DIR", "/archal-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/metrics.json", "w") as f:
            json.dump({
                "version": 1, "llmCallCount": LLM_CALLS, "toolCallCount": TOOL_CALLS,
                "toolErrorCount": 0, "exitReason": "completed",
                "provider": "openai", "model": MODEL,
            }, f)
        with open(f"{out_dir}/agent-trace.json", "w") as f:
            json.dump({"version": 1, "final": final_text, "events": TRACE}, f)
    except OSError as e:
        _log(f"[archal-out] write failed: {e}")

    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
