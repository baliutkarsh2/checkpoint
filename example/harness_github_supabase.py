#!/usr/bin/env python3
"""
Custom multi-tool agent harness: GitHub + Supabase.

checkpoint injects these env vars before running this script:
  CHECKPOINT_TASK          — the scenario's ## Prompt text
  CHECKPOINT_GITHUB_URL    — base URL for the GitHub twin  (e.g. http://127.0.0.1:54321)
  CHECKPOINT_SUPABASE_URL  — base URL for the Supabase twin
  GITHUB_TOKEN             — bootstrap token for the GitHub twin
  SUPABASE_BOOTSTRAP_TOKEN — bootstrap token for the Supabase twin

The agent uses OpenAI tool-calling to decide which API to call. It never
needs to know the URLs are local twins — it just uses HTTP the same way it
would against real GitHub / Supabase.
"""
from __future__ import annotations

import json
import os
import sys

import httpx
from openai import OpenAI

# ── env ──────────────────────────────────────────────────────────────────────
TASK = os.environ["CHECKPOINT_TASK"]
MODEL = os.environ.get("CHECKPOINT_MODEL", "gpt-4o-mini")
MAX_STEPS = int(os.environ.get("CHECKPOINT_MAX_STEPS", "12"))

GITHUB_BASE = os.environ.get("CHECKPOINT_GITHUB_URL", "https://api.github.com")
SUPABASE_BASE = os.environ.get("CHECKPOINT_SUPABASE_URL", "https://your-project.supabase.co")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SUPABASE_TOKEN = os.environ.get("SUPABASE_BOOTSTRAP_TOKEN", "")

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
SB_HEADERS = {
    "Authorization": f"Bearer {SUPABASE_TOKEN}",
    "apikey": SUPABASE_TOKEN,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

TRACE: list[dict] = []


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _trace(service: str, method: str, path: str, status: int):
    TRACE.append({"_clone": service, "method": method, "path": path, "status": status})


# ── GitHub tool implementations ───────────────────────────────────────────────

def github_create_issue(owner: str, repo: str, title: str, body: str = "") -> dict:
    r = httpx.post(
        f"{GITHUB_BASE}/repos/{owner}/{repo}/issues",
        headers=GH_HEADERS,
        json={"title": title, "body": body},
        timeout=15,
    )
    _trace("github", "POST", f"/repos/{owner}/{repo}/issues", r.status_code)
    _log(f"[github] create_issue -> {r.status_code}")
    return {"status": r.status_code, "body": r.json()}


def github_list_issues(owner: str, repo: str, state: str = "open") -> dict:
    r = httpx.get(
        f"{GITHUB_BASE}/repos/{owner}/{repo}/issues",
        headers=GH_HEADERS,
        params={"state": state},
        timeout=15,
    )
    _trace("github", "GET", f"/repos/{owner}/{repo}/issues", r.status_code)
    _log(f"[github] list_issues -> {r.status_code}")
    return {"status": r.status_code, "body": r.json()}


# ── Supabase tool implementations ─────────────────────────────────────────────

def supabase_select(table: str, filters: str = "") -> dict:
    """Query a Supabase table. filters is a PostgREST query string, e.g. 'name=eq.Mouse Pad XL'"""
    params = {}
    if filters:
        for part in filters.split("&"):
            if "=" in part:
                k, _, v = part.partition("=")
                params[k] = v
    r = httpx.get(
        f"{SUPABASE_BASE}/rest/v1/{table}",
        headers={**SB_HEADERS, "Prefer": ""},
        params=params,
        timeout=15,
    )
    _trace("supabase", "GET", f"/rest/v1/{table}", r.status_code)
    _log(f"[supabase] select {table} -> {r.status_code}")
    try:
        return {"status": r.status_code, "rows": r.json()}
    except Exception:
        return {"status": r.status_code, "rows": []}


def supabase_insert(table: str, record: dict) -> dict:
    """Insert a row into a Supabase table. Returns the inserted row."""
    r = httpx.post(
        f"{SUPABASE_BASE}/rest/v1/{table}",
        headers=SB_HEADERS,
        json=record,
        timeout=15,
    )
    _trace("supabase", "POST", f"/rest/v1/{table}", r.status_code)
    _log(f"[supabase] insert {table} -> {r.status_code}")
    try:
        return {"status": r.status_code, "body": r.json()}
    except Exception:
        return {"status": r.status_code, "body": {}}


def supabase_update(table: str, filters: str, updates: dict) -> dict:
    """Update rows in a Supabase table matching the filter."""
    params = {}
    for part in filters.split("&"):
        if "=" in part:
            k, _, v = part.partition("=")
            params[k] = v
    r = httpx.patch(
        f"{SUPABASE_BASE}/rest/v1/{table}",
        headers=SB_HEADERS,
        params=params,
        json=updates,
        timeout=15,
    )
    _trace("supabase", "PATCH", f"/rest/v1/{table}", r.status_code)
    _log(f"[supabase] update {table} -> {r.status_code}")
    try:
        return {"status": r.status_code, "body": r.json()}
    except Exception:
        return {"status": r.status_code, "body": {}}


# ── Tool schema (OpenAI function-calling format) ──────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "github_create_issue",
            "description": "Create a GitHub issue in a repository.",
            "parameters": {
                "type": "object",
                "required": ["owner", "repo", "title"],
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner (e.g. 'acme')"},
                    "repo": {"type": "string", "description": "Repository name (e.g. 'webapp')"},
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body / description"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_issues",
            "description": "List open (or all) issues in a GitHub repository.",
            "parameters": {
                "type": "object",
                "required": ["owner", "repo"],
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supabase_select",
            "description": "Query rows from a Supabase (PostgREST) table.",
            "parameters": {
                "type": "object",
                "required": ["table"],
                "properties": {
                    "table": {"type": "string", "description": "Table name, e.g. 'products'"},
                    "filters": {
                        "type": "string",
                        "description": "PostgREST filter string, e.g. 'name=eq.Mouse Pad XL'. Leave empty for all rows.",
                        "default": "",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supabase_insert",
            "description": "Insert a new row into a Supabase table.",
            "parameters": {
                "type": "object",
                "required": ["table", "record"],
                "properties": {
                    "table": {"type": "string", "description": "Table name"},
                    "record": {
                        "type": "object",
                        "description": "The row data to insert as a JSON object",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supabase_update",
            "description": "Update rows in a Supabase table that match a filter.",
            "parameters": {
                "type": "object",
                "required": ["table", "filters", "updates"],
                "properties": {
                    "table": {"type": "string"},
                    "filters": {
                        "type": "string",
                        "description": "PostgREST filter string, e.g. 'id=eq.3'",
                    },
                    "updates": {
                        "type": "object",
                        "description": "Fields to update",
                    },
                },
            },
        },
    },
]

DISPATCH = {
    "github_create_issue": github_create_issue,
    "github_list_issues": github_list_issues,
    "supabase_select": supabase_select,
    "supabase_insert": supabase_insert,
    "supabase_update": supabase_update,
}


# ── Agent loop ────────────────────────────────────────────────────────────────

def _assistant_msg(msg) -> dict:
    out: dict = {"role": "assistant"}
    if msg.content:
        out["content"] = msg.content
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return out


def main():
    client = OpenAI()
    messages = [
        {
            "role": "system",
            "content": (
                "You are an autonomous agent that coordinates tasks across GitHub and Supabase. "
                "Use the provided tools to complete the user's request. "
                "When finished, write a brief final answer that explicitly states what was done — "
                "include any issue numbers or IDs created."
            ),
        },
        {"role": "user", "content": TASK},
    ]

    final_text = ""
    llm_calls = 0

    for step in range(MAX_STEPS):
        llm_calls += 1
        _log(f"[agent] step {step + 1}/{MAX_STEPS} — calling LLM...")
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        messages.append(_assistant_msg(msg))

        if not msg.tool_calls:
            final_text = msg.content or ""
            _log(f"[agent] done — final answer: {final_text[:120]}")
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            _log(f"[agent] tool call: {name}({list(args.keys())})")
            fn = DISPATCH.get(name)
            if fn is None:
                result = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = fn(**args)
                except Exception as e:
                    result = {"error": str(e)}
                    _log(f"[agent] tool error: {e}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })
    else:
        final_text = final_text or "Reached max steps without completing the task."

    # Print the final answer — checkpoint reads this from stdout
    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
