#!/usr/bin/env python3
"""An incident-triage agent: a tool-calling loop over the GitHub and Slack SDKs.

Run it on its own with a real GitHub token and a real Slack bot token and it
files real issues in a real repository. Run it under Checkpoint and the same
calls land in the twins instead, because Checkpoint intercepts the hostnames
rather than asking this file to change.
"""
from __future__ import annotations

import json
import os

from github import Auth, Github
from openai import OpenAI
from slack_sdk import WebClient

MODEL = os.environ.get("AGENT_MODEL", "gpt-5.6-luna")
MAX_STEPS = 12

SYSTEM = """You triage incidents for the acme engineering team.
You have GitHub and Slack. File the issue first, then tell the team about it and
quote the issue number you got back. Finish with one short paragraph naming
every issue you filed and every channel you posted to."""

gh = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
llm = OpenAI()


# --- tools --------------------------------------------------------------------

def list_issues(repo: str, state: str = "open") -> dict:
    return {"issues": [
        {"number": i.number, "title": i.title, "state": i.state}
        for i in gh.get_repo(repo).get_issues(state=state)
    ]}


def file_issue(repo: str, title: str, body: str) -> dict:
    issue = gh.get_repo(repo).create_issue(title=title, body=body)
    return {"number": issue.number, "url": issue.html_url}


def post_message(channel: str, text: str) -> dict:
    response = slack.chat_postMessage(channel=channel, text=text)
    return {"ts": response["ts"], "channel": response["channel"]}


TOOLS = [
    {"type": "function", "function": {
        "name": "list_issues",
        "description": "List issues in a repository, newest first.",
        "parameters": {
            "type": "object",
            "required": ["repo"],
            "properties": {
                "repo": {"type": "string", "description": "owner/name, e.g. acme/webapp"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "file_issue",
        "description": "Open a new issue in a repository.",
        "parameters": {
            "type": "object",
            "required": ["repo", "title", "body"],
            "properties": {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "post_message",
        "description": "Post a message to a Slack channel.",
        "parameters": {
            "type": "object",
            "required": ["channel", "text"],
            "properties": {
                "channel": {"type": "string", "description": "Channel name, e.g. #engineering"},
                "text": {"type": "string"},
            },
        },
    }},
]

DISPATCH = {"list_issues": list_issues, "file_issue": file_issue, "post_message": post_message}


# --- the loop -----------------------------------------------------------------

def run(task: str) -> str:
    messages: list = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task},
    ]
    for _ in range(MAX_STEPS):
        reply = llm.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        message = reply.choices[0].message
        messages.append(message)
        if not message.tool_calls:
            return message.content or ""
        for call in message.tool_calls:
            arguments = json.loads(call.function.arguments or "{}")
            try:
                result = DISPATCH[call.function.name](**arguments)
            except Exception as e:
                # The model can recover from a refused or malformed call, so the
                # error goes back into the conversation instead of ending the run.
                result = {"error": str(e)}
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": json.dumps(result),
            })
    return "Ran out of steps before finishing."


if __name__ == "__main__":
    print(run(os.environ["CHECKPOINT_TASK"]))
