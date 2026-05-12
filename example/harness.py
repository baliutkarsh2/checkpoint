#!/usr/bin/env python3
"""Example harness — talks to https://api.github.com directly.

The TLS sidecar intercepts the call, swaps the Authorization header to the
twin's bootstrap token, and forwards to the local GitHub twin. The harness
has no idea any of this is happening — which is the entire point.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from openai import OpenAI

GITHUB_BASE = "https://api.github.com"
TASK = os.environ.get("ARCHAL_ENGINE_TASK") or os.environ.get("CHECKPOINT_TASK")
MODEL = os.environ.get("ARCHAL_ENGINE_MODEL", "gpt-4o-mini")
MAX_STEPS = int(os.environ.get("CHECKPOINT_MAX_STEPS", "10"))
ARCHAL_OUT = Path(os.environ.get("ARCHAL_OUT_DIR", "/archal-out"))

# Deliberately a fake/wrong token — the sidecar must overwrite it for the call to succeed.
HEADERS = {
    "Authorization": "Bearer ignored-fake-user-token",
    "Accept": "application/vnd.github+json",
    "User-Agent": "checkpoint-example-harness/0.1",
}

TOOL_CALLS = 0
LLM_CALLS = 0
TRACE_EVENTS: list = []


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def gh(method: str, path: str, **kw) -> dict:
    global TOOL_CALLS
    TOOL_CALLS += 1
    r = requests.request(method, f"{GITHUB_BASE}{path}", headers=HEADERS, timeout=15, **kw)
    try:
        body = r.json()
    except Exception:
        body = r.text
    TRACE_EVENTS.append({"method": method, "path": path, "status": r.status_code})
    return {"status": r.status_code, "body": body}


def t_create_issue(owner, repo, title, body="", labels=None):
    return gh("POST", f"/repos/{owner}/{repo}/issues",
              json={"title": title, "body": body, "labels": labels or []})


def t_get_issue(owner, repo, number):
    return gh("GET", f"/repos/{owner}/{repo}/issues/{number}")


def t_list_issues(owner, repo, state="open"):
    return gh("GET", f"/repos/{owner}/{repo}/issues", params={"state": state})


def t_update_issue(owner, repo, number, **fields):
    return gh("PATCH", f"/repos/{owner}/{repo}/issues/{number}", json=fields)


def t_add_comment(owner, repo, number, body):
    return gh("POST", f"/repos/{owner}/{repo}/issues/{number}/comments",
              json={"body": body})


TOOLS = [
    {"type": "function", "function": {
        "name": "create_issue",
        "description": "Create a new issue in the given GitHub repo.",
        "parameters": {"type": "object", "required": ["owner", "repo", "title"],
            "properties": {
                "owner": {"type": "string"}, "repo": {"type": "string"},
                "title": {"type": "string"}, "body": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
            }}}},
    {"type": "function", "function": {
        "name": "get_issue",
        "description": "Get an issue by number.",
        "parameters": {"type": "object", "required": ["owner", "repo", "number"],
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                           "number": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "list_issues",
        "description": "List issues, optionally filtered by state.",
        "parameters": {"type": "object", "required": ["owner", "repo"],
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                           "state": {"type": "string", "enum": ["open", "closed", "all"]}}}}},
    {"type": "function", "function": {
        "name": "update_issue",
        "description": "Update an issue (state, title, body, labels).",
        "parameters": {"type": "object", "required": ["owner", "repo", "number"],
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                           "number": {"type": "integer"}, "title": {"type": "string"},
                           "body": {"type": "string"},
                           "state": {"type": "string", "enum": ["open", "closed"]},
                           "labels": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {
        "name": "add_issue_comment",
        "description": "Add a comment to an issue.",
        "parameters": {"type": "object", "required": ["owner", "repo", "number", "body"],
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                           "number": {"type": "integer"}, "body": {"type": "string"}}}}},
]

DISPATCH = {
    "create_issue": t_create_issue, "get_issue": t_get_issue,
    "list_issues": t_list_issues, "update_issue": t_update_issue,
    "add_issue_comment": t_add_comment,
}


def _write_outputs(final_text: str):
    try:
        ARCHAL_OUT.mkdir(parents=True, exist_ok=True)
        (ARCHAL_OUT / "metrics.json").write_text(json.dumps({
            "version": 1, "llmCallCount": LLM_CALLS, "toolCallCount": TOOL_CALLS,
            "toolErrorCount": 0, "exitReason": "completed", "provider": "openai",
            "model": MODEL,
        }))
        (ARCHAL_OUT / "agent-trace.json").write_text(json.dumps({
            "version": 1, "final": final_text, "events": TRACE_EVENTS,
        }))
    except OSError as e:
        log(f"[archal-out write failed: {e}]")


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
    if not TASK:
        print("Missing ARCHAL_ENGINE_TASK", file=sys.stderr)
        sys.exit(1)

    client = OpenAI()
    messages = [
        {"role": "system", "content":
            "You are an agent that interacts with GitHub via tools. Use the tools "
            "to accomplish the user's task. When done, return a brief final answer."},
        {"role": "user", "content": TASK},
    ]

    final_text = ""
    for step in range(MAX_STEPS):
        LLM_CALLS += 1
        resp = client.chat.completions.create(
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
            if not fn:
                result = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = fn(**args)
                except Exception as e:
                    result = {"error": str(e)}
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result),
            })
    else:
        final_text = final_text or "Reached max steps without finishing."

    _write_outputs(final_text)
    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
