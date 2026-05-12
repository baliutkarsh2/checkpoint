#!/usr/bin/env python3
"""Slack acceptance harness — no LLM. Drives the Slack twin through:
  1. list channels
  2. find the active #incident-* channel
  3. post a status message in it
  4. add a reaction (eyes) to the message we just posted

Writes a final JSON answer to stdout. Used by example/scenarios/slack-incident-triage.md.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

BASE = os.environ.get("CHECKPOINT_SLACK_URL") or os.environ.get("CHECKPOINT_BASE_URL") or "https://slack.com"
TOKEN = os.environ.get("SLACK_TOKEN", "xoxb-123456789012-234567890123-AbCdEfGhIjKlMnOpQrStUvWx")
ARCHAL_OUT = Path(os.environ.get("ARCHAL_OUT_DIR", "/archal-out"))

H = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "checkpoint-slack-acceptance/0.1",
}


def get(path, **kw):
    return requests.get(f"{BASE}{path}", headers=H, timeout=15, **kw)


def post(path, **kw):
    return requests.post(f"{BASE}{path}", headers=H, timeout=15, **kw)


def step(label, resp):
    print(f"[slack] {label} -> {resp.status_code}", file=sys.stderr)
    return resp


def main():
    errors: list[str] = []

    # 1. list channels
    r = step("conversations.list", get("/api/conversations.list"))
    if r.status_code != 200 or not r.json().get("ok"):
        errors.append(f"list: {r.status_code} {r.text[:120]}")
    channels = r.json().get("channels", [])

    # 2. find incident channel
    incident = next((c for c in channels if c.get("name", "").startswith("incident-")), None)
    if incident is None:
        errors.append("no incident-* channel found")

    posted_ts = None
    if incident:
        # 3. post status message
        r = step(
            "post status",
            post("/api/chat.postMessage", json={
                "channel": incident["id"],
                "text": "Status update: rolling back signing cert; error rate trending down. Customer support paged ~40 affected merchants. Postmortem owner assigned.",
                "user": "U00000005",
            }),
        )
        body = r.json()
        if not body.get("ok"):
            errors.append(f"post: {body}")
        posted_ts = body.get("ts")

        # 4. add reaction
        if posted_ts:
            r = step(
                "react",
                post("/api/reactions.add", json={
                    "channel": incident["id"],
                    "timestamp": posted_ts,
                    "name": "eyes",
                    "user": "U00000003",
                }),
            )
            if not r.json().get("ok"):
                errors.append(f"react: {r.json()}")

    final = (
        f"Triaged active incident in #{incident['name']}: "
        f"posted status update and acknowledged with reaction."
        if incident else "no incident found"
    )

    try:
        ARCHAL_OUT.mkdir(parents=True, exist_ok=True)
        (ARCHAL_OUT / "metrics.json").write_text(json.dumps({
            "version": 1, "llmCallCount": 0,
            "toolCallCount": 4 if incident else 1,
            "toolErrorCount": len(errors), "exitReason": "completed",
            "provider": "none", "model": "slack-acceptance",
        }))
        (ARCHAL_OUT / "agent-trace.json").write_text(json.dumps({
            "version": 1, "final": final,
            "events": [{"channel": incident["name"] if incident else None, "ts": posted_ts}],
        }))
    except OSError:
        pass

    print(json.dumps({"text": final}))
    if errors:
        print("\n".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
