"""Docker-mode multi-clone harness.

Calls real-looking https://slack.com and https://api.stripe.com URLs. The TLS
sidecar intercepts both, routes slack.com -> the slack twin, api.stripe.com
-> the stripe twin. The harness has no idea any of this is happening.
"""
import json, os, sys
import requests

SL_TOKEN = os.environ.get("SLACK_TOKEN", "xoxb-fake")
ST_TOKEN = os.environ.get("STRIPE_API_KEY", "sk_live_fake")
ARCHAL_OUT = os.environ.get("ARCHAL_OUT_DIR", "/archal-out")

SLACK = "https://slack.com"
STRIPE = "https://api.stripe.com"

slack_h = {"Authorization": f"Bearer {SL_TOKEN}", "Accept": "application/json"}
stripe_h = {"Authorization": f"Bearer {ST_TOKEN}", "Accept": "application/json"}

errors = []


def log(label, r):
    print(f"[multi] {label} -> {r.status_code}", file=sys.stderr)
    return r


# 1. Read the most recent message in #engineering
r = log("slack.conversations.list", requests.get(f"{SLACK}/api/conversations.list", headers=slack_h, timeout=15))
eng = None
if r.ok and r.json().get("ok"):
    eng = next((c for c in r.json().get("channels", []) if c.get("name") == "engineering"), None)
if eng is None:
    errors.append("no #engineering channel")
last_msg_text = ""
if eng:
    r = log("slack.history", requests.get(f"{SLACK}/api/conversations.history",
            params={"channel": eng["id"], "limit": 1}, headers=slack_h, timeout=15))
    if r.ok and r.json().get("ok"):
        msgs = r.json().get("messages", [])
        if msgs:
            last_msg_text = msgs[0].get("text", "")

# 2. Find a succeeded payment intent and refund it
r = log("stripe.payment_intents", requests.get(f"{STRIPE}/v1/payment_intents", headers=stripe_h, timeout=15))
pi_id = None
if r.ok:
    succeeded = [p for p in r.json().get("data", []) if p.get("status") == "succeeded"]
    if succeeded:
        pi_id = succeeded[0]["id"]

refund_id = None
if pi_id:
    r = log("stripe.refund", requests.post(f"{STRIPE}/v1/refunds",
            headers={**stripe_h, "Content-Type": "application/x-www-form-urlencoded"},
            data={"payment_intent": pi_id}, timeout=15))
    if r.ok:
        refund_id = r.json().get("id")
    else:
        errors.append(f"refund: {r.status_code} {r.text[:120]}")
else:
    errors.append("no succeeded payment_intent")

# 3. Reply in Slack confirming the refund
post_ts = None
if eng and refund_id:
    text = f"Refund issued — id {refund_id} for payment intent {pi_id}. You should see it on your statement within 5-10 business days."
    r = log("slack.post", requests.post(f"{SLACK}/api/chat.postMessage",
            headers={**slack_h, "Content-Type": "application/json"},
            json={"channel": eng["id"], "text": text}, timeout=15))
    if r.ok and r.json().get("ok"):
        post_ts = r.json().get("ts")
    else:
        errors.append(f"post: {r.status_code} {r.text[:120]}")

answer = (f"Processed refund {refund_id} (payment intent {pi_id}); "
          f"posted confirmation to #engineering (ts {post_ts}).")

try:
    import pathlib
    out = pathlib.Path(ARCHAL_OUT)
    out.mkdir(parents=True, exist_ok=True)
    (out / "agent-trace.json").write_text(json.dumps({
        "version": 1, "final": answer,
        "events": [{"refund": refund_id, "post_ts": post_ts, "errors": errors}],
    }))
    (out / "metrics.json").write_text(json.dumps({
        "version": 1, "llmCallCount": 0, "toolCallCount": 4 + (1 if refund_id else 0),
        "toolErrorCount": len(errors), "exitReason": "completed",
        "provider": "none", "model": "fake",
    }))
except Exception as e:
    print(f"[archal-out failed: {e}]", file=sys.stderr)

print(json.dumps({"text": answer}))
