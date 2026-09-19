"""Domain -> twin URL + bootstrap token registry.

Every SaaS domain Checkpoint can intercept, with the bootstrap token its twin
accepts. The Docker runner fills in twin URLs per run and turns the entries it
needs into intercept-proxy routes with :func:`proxy_routes`.

Tokens match SCOPE §3 / REQUIREMENTS.md GH-02 / SL-02 / ST-03 exactly so an
Archal-authored harness using the real bootstrap-token sees no diff.
"""
from __future__ import annotations

from collections.abc import Mapping
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

from .server import Route as ProxyRoute

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


def auth_header_for(domain: str) -> str | None:
    """The ``Authorization`` value the twin behind ``domain`` accepts.

    Matches exactly, then by parent domain (``x.supabase.co`` -> ``supabase.co``).
    GitHub documents the ``token <t>`` scheme; a token that already carries its
    own scheme (Discord's ``Bot <t>``) is sent as-is; everything else is a
    bearer token.
    """
    labels = domain.lower().rstrip(".").split(".")
    for i in range(len(labels)):
        route = _ROUTES.get(".".join(labels[i:]))
        if route is not None:
            token = route.bootstrap_token
            if route.domain == "api.github.com":
                return f"token {token}"
            return token if " " in token else f"Bearer {token}"
    return None


def proxy_routes(upstreams: Mapping[str, str]) -> list[ProxyRoute]:
    """Intercept-proxy routes for ``{domain: twin_url}``, each stamping the twin's credential."""
    return [
        ProxyRoute(domain, twin_url, auth_header=auth_header_for(domain))
        for domain, twin_url in upstreams.items()
    ]
