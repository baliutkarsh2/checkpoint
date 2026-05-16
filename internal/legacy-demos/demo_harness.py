"""Minimal harness: file the bug-report issue the scenario asks for."""
import json, os, sys, httpx

base = os.environ["CHECKPOINT_GITHUB_URL"]
token = os.environ["GITHUB_TOKEN"]

r = httpx.post(
    f"{base}/repos/acme/webapp/issues",
    headers={"Authorization": f"token {token}"},
    json={
        "title": "Login broken after deploy",
        "body": (
            "Sign in with Google stops responding after the latest deploy.\n\n"
            "Repro: on /login, click 'Sign in with Google' — no network call fires."
        ),
    },
    timeout=10.0,
)
r.raise_for_status()
issue = r.json()
sys.stdout.write(json.dumps({"text": f"Filed issue #{issue['number']} on acme/webapp."}))
