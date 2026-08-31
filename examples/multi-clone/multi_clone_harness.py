#!/usr/bin/env python3
"""Multi-clone acceptance harness — drives GitHub + Slack + Stripe in one run.

Reads CHECKPOINT_GITHUB_URL / CHECKPOINT_SLACK_URL / CHECKPOINT_STRIPE_URL,
performs one realistic action per twin, then writes a final JSON answer.

No LLM. Used by example/scenarios/multi-clone-demo.md.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

GH = os.environ.get("CHECKPOINT_GITHUB_URL")
SL = os.environ.get("CHECKPOINT_SLACK_URL")
ST = os.environ.get("CHECKPOINT_STRIPE_URL")
ARCHAL_OUT = Path(os.environ.get("ARCHAL_OUT_DIR", "/tmp/archal-out"))

from checkpoint.fake_credentials import (
    FAKE_GITHUB_TOKEN,
    FAKE_SLACK_TOKEN,
    FAKE_STRIPE_KEY,
)

GH_TOKEN = os.environ.get("GITHUB_TOKEN", FAKE_GITHUB_TOKEN)
SL_TOKEN = os.environ.get("SLACK_TOKEN", FAKE_SLACK_TOKEN)
ST_TOKEN = os.environ.get("STRIPE_API_KEY", FAKE_STRIPE_KEY)


def step(label, resp):
    print(f"[multi] {label} -> {resp.status_code}", file=sys.stderr)
    return resp


def github_steps() -> dict:
    h = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json",
         "User-Agent": "checkpoint-multi/0.1"}
    out = {"errors": []}
    # 1. create a launch issue on the seeded acme/webapp repo.
    r = step("gh.create_issue", requests.post(
        f"{GH}/repos/acme/webapp/issues",
        headers=h,
        json={"title": "Launch coordination",
              "body": "Tracking issue for the cross-team launch."},
        timeout=15,
    ))
    if r.status_code in (200, 201):
        out["issue"] = r.json().get("number")
    else:
        out["errors"].append(f"create_issue: {r.status_code} {r.text[:120]}")
    # 2. comment on the issue we just opened (or any existing issue if creation
    #    fell through).
    issue_num = out.get("issue") or 1
    r = step("gh.comment", requests.post(
        f"{GH}/repos/acme/webapp/issues/{issue_num}/comments",
        headers=h,
        json={"body": "Slack thread + Stripe refund both wired up. Ready to ship."},
        timeout=15,
    ))
    if r.status_code not in (200, 201):
        out["errors"].append(f"comment: {r.status_code}")
    return out


def slack_steps() -> dict:
    h = {"Authorization": f"Bearer {SL_TOKEN}", "Accept": "application/json",
         "User-Agent": "checkpoint-multi/0.1"}
    out = {"errors": []}
    # 1. list channels.
    r = step("sl.conversations.list", requests.get(f"{SL}/api/conversations.list", headers=h, timeout=15))
    eng = None
    if r.status_code == 200 and r.json().get("ok"):
        eng = next((c for c in r.json().get("channels", []) if c.get("name") == "engineering"), None)
    if eng is None:
        out["errors"].append("no #engineering channel found")
    # 2. post a coordination message.
    if eng:
        r = step("sl.post", requests.post(
            f"{SL}/api/chat.postMessage",
            headers={**h, "Content-Type": "application/json"},
            json={"channel": eng["id"], "text": "Launch coordination thread open in GitHub; refund processed."},
            timeout=15,
        ))
        if r.status_code == 200 and r.json().get("ok"):
            out["message_ts"] = r.json().get("ts")
        else:
            out["errors"].append(f"post: {r.status_code} {r.text[:120]}")
        # 3. add an `eyes` reaction so the team knows it's been seen.
        if out.get("message_ts"):
            r = step("sl.reactions.add", requests.post(
                f"{SL}/api/reactions.add",
                headers={**h, "Content-Type": "application/json"},
                json={"channel": eng["id"], "name": "eyes", "timestamp": out["message_ts"]},
                timeout=15,
            ))
            if r.status_code != 200 or not r.json().get("ok"):
                out["errors"].append(f"reaction: {r.status_code}")
    return out


def stripe_steps() -> dict:
    h = {"Authorization": f"Bearer {ST_TOKEN}", "Accept": "application/json",
         "User-Agent": "checkpoint-multi/0.1"}
    out = {"errors": []}
    # 1. find a successful payment intent (pi_001 in the small-business seed).
    r = step("st.payment_intents", requests.get(f"{ST}/v1/payment_intents", headers=h, timeout=15))
    pi_id = None
    if r.status_code == 200:
        pis = r.json().get("data", [])
        succeeded = [p for p in pis if p.get("status") == "succeeded"]
        if succeeded:
            pi_id = succeeded[0]["id"]
    if not pi_id:
        out["errors"].append("no succeeded payment_intent found")
        return out
    # 2. refund it.
    r = step("st.refund", requests.post(
        f"{ST}/v1/refunds",
        headers={**h, "Content-Type": "application/x-www-form-urlencoded"},
        data={"payment_intent": pi_id},
        timeout=15,
    ))
    if r.status_code == 200:
        out["refund"] = r.json().get("id")
        out["payment_intent"] = pi_id
    else:
        out["errors"].append(f"refund: {r.status_code} {r.text[:120]}")
    return out


def main():
    if not (GH and SL and ST):
        print(json.dumps({"text": "missing one of the CHECKPOINT_*_URL env vars",
                          "have": {"github": bool(GH), "slack": bool(SL), "stripe": bool(ST)}}))
        sys.exit(2)

    gh = github_steps()
    sl = slack_steps()
    st = stripe_steps()

    summary = {
        "github_issue": gh.get("issue"),
        "slack_message_ts": sl.get("message_ts"),
        "stripe_refund": st.get("refund"),
        "stripe_payment_intent": st.get("payment_intent"),
        "errors": gh.get("errors", []) + sl.get("errors", []) + st.get("errors", []),
    }
    answer = (
        f"Coordinated launch: opened GitHub issue #{summary['github_issue']}, "
        f"posted Slack message {summary['slack_message_ts']} in #engineering, "
        f"processed refund {summary['stripe_refund']} for payment intent "
        f"{summary['stripe_payment_intent']}."
    )

    try:
        ARCHAL_OUT.mkdir(parents=True, exist_ok=True)
        (ARCHAL_OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    except Exception:
        pass

    print(json.dumps({"text": answer, "summary": summary}))


if __name__ == "__main__":
    main()
