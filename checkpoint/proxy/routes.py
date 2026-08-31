"""Domain -> twin URL + bootstrap token registry.

Hardcoded for Phase 1: only api.github.com. Adding slack.com / api.stripe.com
in Phase 3 is a one-file change to the seeded dict below.

Tokens match SCOPE §3 / REQUIREMENTS.md GH-02 / SL-02 / ST-03 exactly so an
Archal-authored harness using the real bootstrap-token sees no diff.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from checkpoint.fake_credentials import (
    FAKE_DISCORD_TOKEN,
    FAKE_GITHUB_TOKEN,
    FAKE_GOOGLE_WORKSPACE_TOKEN,
    FAKE_LINEAR_TOKEN,
    FAKE_SLACK_TOKEN,
    FAKE_STRIPE_KEY,
    FAKE_SUPABASE_TOKEN,
)

# Per SCOPE §3 / REQUIREMENTS.md GH-02 / SL-02 / ST-03.
GITHUB_BOOTSTRAP_TOKEN = FAKE_GITHUB_TOKEN
SLACK_BOOTSTRAP_TOKEN = FAKE_SLACK_TOKEN
STRIPE_BOOTSTRAP_TOKEN = FAKE_STRIPE_KEY
LINEAR_BOOTSTRAP_TOKEN = FAKE_LINEAR_TOKEN
SUPABASE_BOOTSTRAP_TOKEN = FAKE_SUPABASE_TOKEN
DISCORD_BOOTSTRAP_TOKEN = FAKE_DISCORD_TOKEN
GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN = FAKE_GOOGLE_WORKSPACE_TOKEN


@dataclass(frozen=True)
class Route:
    domain: str
    twin_url: str  # filled in by the runner before the sidecar starts
    bootstrap_token: str


# Seeded with placeholder twin URLs — runner overwrites via register().
_ROUTES: dict[str, Route] = {
    "api.github.com": Route(
        domain="api.github.com",
        twin_url="",
        bootstrap_token=GITHUB_BOOTSTRAP_TOKEN,
    ),
    "slack.com": Route(
        domain="slack.com",
        twin_url="",
        bootstrap_token=SLACK_BOOTSTRAP_TOKEN,
    ),
    "api.stripe.com": Route(
        domain="api.stripe.com",
        twin_url="",
        bootstrap_token=STRIPE_BOOTSTRAP_TOKEN,
    ),
    "api.linear.app": Route(
        domain="api.linear.app",
        twin_url="",
        bootstrap_token=LINEAR_BOOTSTRAP_TOKEN,
    ),
    "supabase.co": Route(
        domain="supabase.co",
        twin_url="",
        bootstrap_token=SUPABASE_BOOTSTRAP_TOKEN,
    ),
    "discord.com": Route(
        domain="discord.com",
        twin_url="",
        bootstrap_token=DISCORD_BOOTSTRAP_TOKEN,
    ),
    "gmail.googleapis.com": Route(
        domain="gmail.googleapis.com",
        twin_url="",
        bootstrap_token=GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN,
    ),
    "www.googleapis.com": Route(
        domain="www.googleapis.com",
        twin_url="",
        bootstrap_token=GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN,
    ),
}


def register(domain: str, twin_url: str, bootstrap_token: str | None = None) -> None:
    existing = _ROUTES.get(domain)
    if existing is None:
        if not bootstrap_token:
            raise ValueError(f"register({domain}): bootstrap_token required for new domain")
        _ROUTES[domain] = Route(domain=domain, twin_url=twin_url, bootstrap_token=bootstrap_token)
        return
    _ROUTES[domain] = replace(
        existing,
        twin_url=twin_url,
        bootstrap_token=bootstrap_token or existing.bootstrap_token,
    )


def lookup(host: str) -> Route | None:
    return _ROUTES.get(host)


def all_domains() -> list[str]:
    return list(_ROUTES.keys())
