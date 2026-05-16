#!/usr/bin/env python3
"""LangChain ReAct agent — wraps real SDK calls as LangChain tools.

For "I already use LangChain" customers. Demonstrates that Checkpoint is
framework-agnostic: the agent is a stock LangChain ReAct loop with
@tool-decorated functions; nothing in here knows about Checkpoint.
"""
from __future__ import annotations

import json
import os
import sys

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

TASK = os.environ["CHECKPOINT_TASK"]
MODEL = os.environ.get("CHECKPOINT_AGENT_MODEL", "gpt-4o-mini")

_clients: dict[str, object] = {}


def _gh():
    if "gh" not in _clients:
        from github import Auth, Github
        _clients["gh"] = Github(auth=Auth.Token(os.environ.get("GITHUB_TOKEN", "")))
    return _clients["gh"]


def _linear_url() -> str:
    # Linear SDK is GraphQL; for an example we'll just hit the REST-shaped MCP
    # bridge twin endpoints directly via httpx for clarity.
    return os.environ.get("CHECKPOINT_LINEAR_URL", "https://api.linear.app")


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── tools (real SDKs / real URLs) ────────────────────────────────────────────

@tool
def github_create_issue(owner: str, repo: str, title: str, body: str = "") -> str:
    """Create a GitHub issue. Returns the issue number and URL."""
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        issue = r.create_issue(title=title, body=body)
        _log(f"[github] created issue #{issue.number}")
        return json.dumps({"number": issue.number, "url": issue.html_url})
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def github_list_issues(owner: str, repo: str, state: str = "open") -> str:
    """List GitHub issues in a repo. state: open | closed | all."""
    try:
        r = _gh().get_repo(f"{owner}/{repo}")
        out = [{"number": i.number, "title": i.title, "state": i.state}
               for i in r.get_issues(state=state)]
        return json.dumps({"issues": out})
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def linear_create_issue(team_id: str, title: str, description: str = "") -> str:
    """Create a Linear issue. Returns the issue identifier."""
    import httpx
    body = {
        "query": (
            "mutation IssueCreate($input: IssueCreateInput!) { "
            "  issueCreate(input: $input) { issue { id identifier title } } "
            "}"
        ),
        "variables": {"input": {"teamId": team_id, "title": title, "description": description}},
    }
    try:
        r = httpx.post(
            f"{_linear_url()}/graphql",
            json=body,
            headers={"Authorization": os.environ.get("LINEAR_BOOTSTRAP_TOKEN", "")},
            timeout=10,
        )
        out = r.json()
        _log(f"[linear] created: {out}")
        return json.dumps(out)
    except Exception as e:
        return json.dumps({"error": str(e)})


TOOLS = [github_create_issue, github_list_issues, linear_create_issue]


def _lc_msg_to_dict(m) -> dict:
    """Normalize a LangChain message into {role, content} for the chat view."""
    if isinstance(m, tuple) and len(m) == 2:
        return {"role": m[0], "content": m[1]}
    role = getattr(m, "type", None) or "message"
    # Map LC's "human" to "user", "ai" to "assistant" — the dashboard expects
    # the OpenAI-shaped role names.
    role = {"human": "user", "ai": "assistant"}.get(role, role)
    content = getattr(m, "content", "")
    if not isinstance(content, str):
        try:
            content = json.dumps(content, default=str)
        except Exception:
            content = str(content)
    return {"role": role, "content": content}


def _lc_tool_call(m) -> dict | None:
    """Extract tool-call info from an LC ToolMessage / AIMessage."""
    if getattr(m, "type", "") == "tool":
        return {
            "name": getattr(m, "name", "") or "(tool)",
            "input": {},  # LC ToolMessage doesn't carry input separately
            "output": getattr(m, "content", ""),
            "status": "ok",
        }
    return None


def main():
    llm = ChatOpenAI(model=MODEL, temperature=0)
    agent = create_react_agent(llm, TOOLS)
    result = agent.invoke({
        "messages": [
            ("system", "You are an autonomous agent that coordinates work "
                       "across GitHub and Linear. Use the tools precisely. "
                       "Your final message must name every entity you touched."),
            ("user", TASK),
        ]
    })

    msgs_raw = result.get("messages", [])
    messages = [_lc_msg_to_dict(m) for m in msgs_raw]
    tool_calls_log = [tc for tc in (_lc_tool_call(m) for m in msgs_raw) if tc]
    # LangChain doesn't expose usage as cleanly; best-effort sum across AIMessages.
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for m in msgs_raw:
        u = getattr(m, "usage_metadata", None)
        if not u:
            continue
        usage_total["prompt_tokens"] += u.get("input_tokens", 0) or 0
        usage_total["completion_tokens"] += u.get("output_tokens", 0) or 0
        usage_total["total_tokens"] += u.get("total_tokens", 0) or 0

    # Last AI message is the final answer.
    last = msgs_raw[-1] if msgs_raw else None
    final_text = getattr(last, "content", "") or ""
    if not isinstance(final_text, str):
        final_text = json.dumps(final_text, default=str)

    out_dir = os.environ.get("ARCHAL_OUT_DIR", "/archal-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/metrics.json", "w") as f:
            json.dump({
                "version": 1,
                "llmCallCount": sum(1 for m in msgs_raw if getattr(m, "type", "") == "ai"),
                "toolCallCount": len(tool_calls_log),
                "toolErrorCount": 0,
                "exitReason": "completed",
                "provider": "openai", "model": MODEL,
                "promptTokens": usage_total["prompt_tokens"],
                "completionTokens": usage_total["completion_tokens"],
                "totalTokens": usage_total["total_tokens"],
            }, f)
        with open(f"{out_dir}/agent-trace.json", "w") as f:
            json.dump({
                "version": 2, "final": final_text, "events": [],
                "messages": messages,
                "tool_calls": tool_calls_log,
                "usage": usage_total,
                "provider": "openai+langchain", "model": MODEL,
            }, f, default=str)
    except OSError as e:
        _log(f"[archal-out] write failed: {e}")

    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
