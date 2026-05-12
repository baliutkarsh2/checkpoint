#!/usr/bin/env python3
"""Example agent harness for checkpoint.

Reads the task from CHECKPOINT_TASK and runs a small OpenAI tool-using agent
loop against the GitHub twin at CHECKPOINT_GITHUB_URL.

Contract: prints exactly one JSON line to stdout — {"text": "<final answer>"}.
Diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import sys

import httpx
from openai import OpenAI

BASE_URL = os.environ.get("CHECKPOINT_GITHUB_URL") or os.environ.get("CHECKPOINT_BASE_URL")
TASK = os.environ.get("CHECKPOINT_TASK") or os.environ.get("ARCHAL_ENGINE_TASK")
MODEL = os.environ.get("ARCHAL_ENGINE_MODEL", "gpt-4o-mini")
MAX_STEPS = int(os.environ.get("CHECKPOINT_MAX_STEPS", "10"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http(method: str, path: str, **kw) -> dict:
    url = f"{BASE_URL}{path}"
    r = httpx.request(method, url, timeout=10, **kw)
    try:
        return {"status": r.status_code, "body": r.json()}
    except Exception:
        return {"status": r.status_code, "body": r.text}


def t_create_issue(owner, repo, title, body="", labels=None):
    return http("POST", f"/repos/{owner}/{repo}/issues",
                json={"title": title, "body": body, "labels": labels or []})


def t_get_issue(owner, repo, number):
    return http("GET", f"/repos/{owner}/{repo}/issues/{number}")


def t_list_issues(owner, repo, state="open"):
    return http("GET", f"/repos/{owner}/{repo}/issues", params={"state": state})


def t_update_issue(owner, repo, number, **fields):
    return http("PATCH", f"/repos/{owner}/{repo}/issues/{number}", json=fields)


def t_add_comment(owner, repo, number, body):
    return http("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})


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
        "description": "Get a specific issue by number.",
        "parameters": {"type": "object", "required": ["owner", "repo", "number"],
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "number": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "list_issues",
        "description": "List issues in the repo, optionally filtered by state (open|closed|all).",
        "parameters": {"type": "object", "required": ["owner", "repo"],
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                           "state": {"type": "string", "enum": ["open", "closed", "all"]}}}}},
    {"type": "function", "function": {
        "name": "update_issue",
        "description": "Update an issue. Pass state='closed' to close it. Pass labels to replace label set.",
        "parameters": {"type": "object", "required": ["owner", "repo", "number"],
            "properties": {
                "owner": {"type": "string"}, "repo": {"type": "string"}, "number": {"type": "integer"},
                "title": {"type": "string"}, "body": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed"]},
                "labels": {"type": "array", "items": {"type": "string"}},
            }}}},
    {"type": "function", "function": {
        "name": "add_issue_comment",
        "description": "Add a comment to an issue.",
        "parameters": {"type": "object", "required": ["owner", "repo", "number", "body"],
            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"},
                           "number": {"type": "integer"}, "body": {"type": "string"}}}}},
]

DISPATCH = {
    "create_issue": t_create_issue,
    "get_issue": t_get_issue,
    "list_issues": t_list_issues,
    "update_issue": t_update_issue,
    "add_issue_comment": t_add_comment,
}


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
    if not TASK:
        print("Missing CHECKPOINT_TASK", file=sys.stderr)
        sys.exit(1)
    if not BASE_URL:
        print("Missing CHECKPOINT_GITHUB_URL / CHECKPOINT_BASE_URL", file=sys.stderr)
        sys.exit(1)

    client = OpenAI()
    messages = [
        {"role": "system", "content":
            "You are an agent that interacts with GitHub via tools. "
            "Use the tools to accomplish the user's task. "
            "When the task is done, return a brief final answer."},
        {"role": "user", "content": TASK},
    ]

    final_text = ""
    for step in range(MAX_STEPS):
        log(f"[step {step + 1}] calling model")
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
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            log(f"  -> {name}({args})")
            fn = DISPATCH.get(name)
            if not fn:
                result = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = fn(**args)
                except Exception as e:
                    result = {"error": str(e)}
            log(f"     <- {str(result)[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })
    else:
        final_text = final_text or "Reached max steps without finishing."

    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
