#!/usr/bin/env python3
"""Anthropic tool-use agent (Claude).

Mirrors examples/agents/openai-tools but uses Anthropic's `messages.create`
tool-use protocol (tool_use blocks + tool_result blocks). Same real-SDK
pattern: PyGithub, supabase-py, slack_sdk against production URLs.
"""
from __future__ import annotations

import json
import os
import sys

from anthropic import Anthropic

TASK = os.environ["CHECKPOINT_TASK"]
MODEL = os.environ.get("CHECKPOINT_AGENT_MODEL", "claude-sonnet-4-6")
MAX_STEPS = int(os.environ.get("CHECKPOINT_MAX_STEPS", "12"))

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


TRACE: list[dict] = []
LLM_CALLS = 0
TOOL_CALLS = 0


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── tool implementations ─────────────────────────────────────────────────────

def t_github_create_issue(owner, repo, title, body=""):
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        i = r.create_issue(title=title, body=body)
        TRACE.append({"_clone": "github", "method": "POST",
                      "path": f"/repos/{owner}/{repo}/issues", "status": 201})
        _log(f"[github] created issue #{i.number}: {title}")
        return {"number": i.number, "url": i.html_url, "state": i.state}
    except Exception as e:
        return {"error": str(e)}


def t_github_list_issues(owner, repo, state="open"):
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        return {"issues": [{"number": i.number, "title": i.title, "state": i.state}
                           for i in r.get_issues(state=state)]}
    except Exception as e:
        return {"error": str(e)}


def t_supabase_insert(table, record):
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        res = _sb().table(table).insert(record).execute()
        _log(f"[supabase] inserted into {table}: {list(record.keys())}")
        return {"inserted": res.data}
    except Exception as e:
        return {"error": str(e)}


def t_slack_post_message(channel, text):
    global TOOL_CALLS
    TOOL_CALLS += 1
    try:
        r = _slack().chat_postMessage(channel=channel, text=text)
        _log(f"[slack] posted to #{channel}")
        return {"ok": r["ok"], "ts": r.get("ts")}
    except Exception as e:
        return {"error": str(e)}


DISPATCH = {
    "github_create_issue": t_github_create_issue,
    "github_list_issues": t_github_list_issues,
    "supabase_insert": t_supabase_insert,
    "slack_post_message": t_slack_post_message,
}

TOOLS = [
    {"name": "github_create_issue",
     "description": "Create a GitHub issue.",
     "input_schema": {"type": "object", "required": ["owner", "repo", "title"],
                      "properties": {
                          "owner": {"type": "string"}, "repo": {"type": "string"},
                          "title": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "github_list_issues",
     "description": "List issues in a GitHub repo.",
     "input_schema": {"type": "object", "required": ["owner", "repo"],
                      "properties": {
                          "owner": {"type": "string"}, "repo": {"type": "string"},
                          "state": {"type": "string"}}}},
    {"name": "supabase_insert",
     "description": "Insert a row into a Supabase table.",
     "input_schema": {"type": "object", "required": ["table", "record"],
                      "properties": {
                          "table": {"type": "string"},
                          "record": {"type": "object"}}}},
    {"name": "slack_post_message",
     "description": "Post a message to a Slack channel.",
     "input_schema": {"type": "object", "required": ["channel", "text"],
                      "properties": {
                          "channel": {"type": "string"}, "text": {"type": "string"}}}},
]


def _content_to_text(content) -> str:
    """Anthropic responses are lists of blocks; flatten to a UI-friendly string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    out_parts: list[str] = []
    for b in content:
        bt = getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)
        if bt == "text":
            out_parts.append(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else ""))
        elif bt == "tool_use":
            name = getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None)
            inp = getattr(b, "input", None) or (b.get("input") if isinstance(b, dict) else None)
            out_parts.append(f"[tool_use {name}({json.dumps(inp, default=str)})]")
        elif bt == "tool_result":
            content_inner = getattr(b, "content", None) or (b.get("content") if isinstance(b, dict) else None)
            out_parts.append(f"[tool_result {content_inner}]")
    return "\n".join(p for p in out_parts if p)


def _msg_to_dict(m: dict) -> dict:
    """Normalize an Anthropic-style message into the dashboard's chat shape."""
    return {"role": m.get("role", ""), "content": _content_to_text(m.get("content"))}


def main():
    global LLM_CALLS
    client = Anthropic()
    messages: list[dict] = [{"role": "user", "content": TASK}]
    tool_calls_log: list[dict] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    final_text = ""

    for step in range(MAX_STEPS):
        LLM_CALLS += 1
        _log(f"[agent] step {step + 1}/{MAX_STEPS}")
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=TOOLS,
            messages=messages,
            system=(
                "You are an autonomous agent that coordinates tasks across "
                "GitHub, Supabase, and Slack. Use the tools precisely. "
                "Your final response must name every entity you touched "
                "(issue numbers, IDs, channel names)."
            ),
        )
        # Anthropic exposes usage on the response object.
        u = getattr(resp, "usage", None)
        if u is not None:
            inp = getattr(u, "input_tokens", 0) or 0
            outp = getattr(u, "output_tokens", 0) or 0
            usage_total["prompt_tokens"] += inp
            usage_total["completion_tokens"] += outp
            usage_total["total_tokens"] += inp + outp

        # Echo the assistant message back into history.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            # Concatenate all text blocks for the final answer.
            final_text = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
            )
            break

        # Run every tool_use block in this turn, batch results into one user message.
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            fn = DISPATCH.get(block.name)
            args = block.input or {}
            try:
                result = fn(**args) if fn else {"error": f"unknown tool: {block.name}"}
                status = "error" if isinstance(result, dict) and "error" in result else "ok"
            except Exception as e:
                result = {"error": f"tool call failed: {e}"}
                status = "error"
            tool_calls_log.append({
                "name": block.name, "input": args, "output": result, "status": status,
            })
            tool_results.append({"type": "tool_result",
                                 "tool_use_id": block.id,
                                 "content": json.dumps(result)})
        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = final_text or "Reached max steps."

    out_dir = os.environ.get("ARCHAL_OUT_DIR", "/archal-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/metrics.json", "w") as f:
            json.dump({
                "version": 1, "llmCallCount": LLM_CALLS,
                "toolCallCount": TOOL_CALLS, "toolErrorCount": 0,
                "exitReason": "completed", "provider": "anthropic", "model": MODEL,
                "promptTokens": usage_total["prompt_tokens"],
                "completionTokens": usage_total["completion_tokens"],
                "totalTokens": usage_total["total_tokens"],
            }, f)
        with open(f"{out_dir}/agent-trace.json", "w") as f:
            json.dump({
                "version": 2, "final": final_text, "events": TRACE,
                "messages": [_msg_to_dict(m) for m in messages],
                "tool_calls": tool_calls_log,
                "usage": usage_total,
                "provider": "anthropic", "model": MODEL,
            }, f, default=str)
    except OSError as e:
        _log(f"[archal-out] write failed: {e}")

    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
