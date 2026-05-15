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

    # Last AI message is the final answer.
    last = result["messages"][-1]
    final_text = getattr(last, "content", "") or ""
    out_dir = os.environ.get("ARCHAL_OUT_DIR", "/archal-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
        # Count messages as a proxy for steps.
        with open(f"{out_dir}/metrics.json", "w") as f:
            json.dump({"version": 1, "llmCallCount": len(result["messages"]) // 2,
                       "toolCallCount": sum(
                           1 for m in result["messages"]
                           if getattr(m, "type", "") == "tool"),
                       "toolErrorCount": 0, "exitReason": "completed",
                       "provider": "openai", "model": MODEL}, f)
        with open(f"{out_dir}/agent-trace.json", "w") as f:
            json.dump({"version": 1, "final": final_text, "events": []}, f)
    except OSError as e:
        _log(f"[archal-out] write failed: {e}")

    print(json.dumps({"text": final_text}))


if __name__ == "__main__":
    main()
