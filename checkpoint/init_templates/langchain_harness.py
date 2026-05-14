#!/usr/bin/env python3
"""Checkpoint harness — LangChain ReAct agent with MCP tools.

The twin exposes its full REST API, which this harness wraps as LangChain
tools via a thin adapter. Alternatively, point a custom LangChain toolkit
at the MCP endpoint ({CHECKPOINT_<CLONE>_URL}/mcp/).

Requirements:
    pip install langchain langchain-openai requests

Env vars set by Checkpoint:
    CHECKPOINT_TASK          - the scenario prompt
    CHECKPOINT_GITHUB_URL    - twin base URL
    GITHUB_TOKEN             - bootstrap auth token
    OPENAI_API_KEY           - for the LLM

Contract:
    Print exactly one JSON line to stdout: {"text": "<answer>"}
    Exit 0 on success.
"""
from __future__ import annotations

import json
import os
import sys

TASK = os.environ.get("CHECKPOINT_TASK") or os.environ.get("ARCHAL_ENGINE_TASK") or ""
CLONE_URL = os.environ.get("CHECKPOINT_GITHUB_URL", "")
CLONE_TOKEN = os.environ.get("GITHUB_TOKEN", "")

try:
    import requests
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
except ImportError as exc:
    print(json.dumps({"text": f"Missing dependency: {exc} — run: pip install langchain langchain-openai requests"}))
    sys.exit(1)


def _auth_headers() -> dict:
    return {"Authorization": f"token {CLONE_TOKEN}", "Accept": "application/vnd.github+json"}


@tool
def list_repos() -> str:
    """List repositories accessible to the authenticated user."""
    if not CLONE_URL:
        return "[]"
    r = requests.get(f"{CLONE_URL}/user/repos", headers=_auth_headers(), timeout=10)
    return r.text


@tool
def create_issue(owner: str, repo: str, title: str, body: str = "") -> str:
    """Create a new GitHub issue in owner/repo with the given title and body."""
    if not CLONE_URL:
        return "{}"
    r = requests.post(
        f"{CLONE_URL}/repos/{owner}/{repo}/issues",
        json={"title": title, "body": body},
        headers=_auth_headers(),
        timeout=10,
    )
    return r.text


@tool
def get_issue(owner: str, repo: str, issue_number: int) -> str:
    """Get details of a specific issue by number."""
    if not CLONE_URL:
        return "{}"
    r = requests.get(
        f"{CLONE_URL}/repos/{owner}/{repo}/issues/{issue_number}",
        headers=_auth_headers(),
        timeout=10,
    )
    return r.text


TOOLS = [list_repos, create_issue, get_issue]


def main() -> None:
    print(f"[harness] task: {TASK[:200]}", file=sys.stderr)

    llm = ChatOpenAI(model="gpt-4o", temperature=0)

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful agent. Use the provided tools to complete tasks."),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])

    agent = create_tool_calling_agent(llm, TOOLS, prompt)
    executor = AgentExecutor(agent=agent, tools=TOOLS, verbose=False)

    result = executor.invoke({"input": TASK})
    answer = str(result.get("output", ""))

    print(json.dumps({"text": answer}))


if __name__ == "__main__":
    main()
