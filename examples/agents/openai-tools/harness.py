#!/usr/bin/env python3
"""OpenAI function-calling agent — the most common production agent shape.

Uses real SDKs (PyGithub, supabase-py, slack_sdk) against production URLs.
The Checkpoint TLS sidecar intercepts and routes to twins.
"""
from __future__ import annotations

import json
import os
import sys

from openai import OpenAI

# ── runtime config ────────────────────────────────────────────────────────────

TASK = os.environ["CHECKPOINT_TASK"]
MODEL = os.environ.get("CHECKPOINT_AGENT_MODEL", "gpt-4o-mini")
MAX_STEPS = int(os.environ.get("CHECKPOINT_MAX_STEPS", "12"))

# Lazy SDK init — only spend time on imports / clients we'll actually call.
_clients: dict[str, object] = {}


def _gh():
    if "gh" not in _clients:
        from github import Auth, Github
        _clients["gh"] = Github(auth=Auth.Token(os.environ.get("GITHUB_TOKEN", "")))
    return _clients["gh"]


def _sb():
    if "sb" not in _clients:
        from supabase import create_client
        _clients["sb"] = create_client(
            "https://checkpoint.supabase.co",
            os.environ.get("SUPABASE_BOOTSTRAP_TOKEN", ""),
        )
    return _clients["sb"]


def _slack():
    if "slack" not in _clients:
        from slack_sdk import WebClient
        _clients["slack"] = WebClient(token=os.environ.get("SLACK_TOKEN", ""))
    return _clients["slack"]


# ── trace + counter telemetry the runner reads from /archal-out ───────────────

TRACE: list[dict] = []
LLM_CALLS = 0
TOOL_CALLS = 0


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── tools ─────────────────────────────────────────────────────────────────────

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
    except Exception as e:
        return {"error": str(e)}


def github_list_issues(owner: str, repo: str, state: str = "open") -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        issues = [{"number": i.number, "title": i.title, "state": i.state}
                  for i in r.get_issues(state=state)]
        return {"issues": issues}
    except Exception as e:
        return {"error": str(e)}


def supabase_select(table: str, column: str = "*",
                    eq_col: str = "", eq_val: str = "") -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        q = _sb().table(table).select(column)
        if eq_col and eq_val:
            q = q.eq(eq_col, eq_val)
        res = q.execute()
        _log(f"[supabase] select {table} -> {len(res.data)} rows")
        return {"rows": res.data}
    except Exception as e:
        return {"error": str(e)}


def supabase_insert(table: str, record: dict) -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        res = _sb().table(table).insert(record).execute()
        _log(f"[supabase] inserted into {table}: {list(record.keys())}")
        return {"inserted": res.data}
    except Exception as e:
        return {"error": str(e)}


def slack_post_message(channel: str, text: str) -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _slack().chat_postMessage(channel=channel, text=text)
        _log(f"[slack] posted to #{channel}")
        return {"ok": r["ok"], "ts": r.get("ts")}
    except Exception as e:
        return {"error": str(e)}


# ── OpenAI tool schema ────────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "github_create_issue",
        "description": "Create a GitHub issue.",
        "parameters": {"type": "object",
                       "required": ["owner", "repo", "title"],
                       "properties": {
                           "owner": {"type": "string"}, "repo": {"type": "string"},
                           "title": {"type": "string"}, "body": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "github_list_issues",
        "description": "List GitHub issues in a repo.",
        "parameters": {"type": "object",
                       "required": ["owner", "repo"],
                       "properties": {
                           "owner": {"type": "string"}, "repo": {"type": "string"},
                           "state": {"type": "string", "enum": ["open", "closed", "all"]}}}}},
    {"type": "function", "function": {
        "name": "supabase_select",
        "description": "Query rows from a Supabase table.",
        "parameters": {"type": "object", "required": ["table"],
                       "properties": {
                           "table": {"type": "string"}, "column": {"type": "string"},
                           "eq_col": {"type": "string"}, "eq_val": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "supabase_insert",
        "description": "Insert a row into a Supabase table. Nest column values under 'record'.",
        "parameters": {"type": "object", "required": ["table", "record"],
                       "properties": {
                           "table": {"type": "string"},
                           "record": {"type": "object"}}}}},
    {"type": "function", "function": {
        "name": "slack_post_message",
        "description": "Post a message to a Slack channel.",
        "parameters": {"type": "object", "required": ["channel", "text"],
                       "properties": {
                           "channel": {"type": "string"}, "text": {"type": "string"}}}}},
]

DISPATCH = {
    "github_create_issue": github_create_issue,
    "github_list_issues": github_list_issues,
    "supabase_select": supabase_select,
    "supabase_insert": supabase_insert,
    "slack_post_message": slack_post_message,
}


# ── agent loop ────────────────────────────────────────────────────────────────

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
            "You are an autonomous agent that coordinates tasks across GitHub, "
            "Supabase, and Slack via tool calls. Use the tools precisely. When "
            "done, write a final answer that names every entity you created or "
            "modified (issue numbers, IDs, channel names)."
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

    out_dir = os.environ.get("ARCHAL_OUT_DIR", "/archal-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/metrics.json", "w") as f:
            json.dump({"version": 1, "llmCallCount": LLM_CALLS,
                       "toolCallCount": TOOL_CALLS, "toolErrorCount": 0,
                       "exitReason": "completed", "provider": "openai",
                       "model": MODEL}, f)
        with open(f"{out_dir}/agent-trace.json", "w") as f:
            json.dump({"version": 1, "final": final_text, "events": TRACE}, f)
    except OSError as e:
        _log(f"[archal-out] write failed: {e}")

    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
