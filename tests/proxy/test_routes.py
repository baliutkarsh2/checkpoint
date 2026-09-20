"""Credential stamping: the reason an unmodified agent reaches a twin at all.

An agent authenticates with whatever token it has — a real one from its own
config, or a placeholder. The twin does not know that token. The proxy replaces
it with the one the twin accepts, which is what lets production code run
untouched. A domain that slips through without a credential fails the run for a
reason that has nothing to do with the agent, so these check every domain the
registry claims, not a hand-listed few.
"""
from __future__ import annotations

import pytest

from checkpoint.fake_credentials import (
    FAKE_DISCORD_TOKEN,
    FAKE_GITHUB_TOKEN,
    FAKE_SLACK_TOKEN,
    FAKE_SUPABASE_TOKEN,
)
from checkpoint.proxy.routes import auth_header_for, intercepted_domains, proxy_routes
from checkpoint.twins import registry


@pytest.mark.parametrize("domain", sorted(registry.domains()))
def test_every_intercepted_domain_gets_a_credential(domain: str) -> None:
    """The table this used to read from was missing three of these."""
    header = auth_header_for(domain)
    assert header, f"{domain} is intercepted but no credential is stamped on it"
    assert "CHECKPOINTFAKE" in header


def test_each_twin_uses_the_scheme_its_sdks_send():
    assert auth_header_for("api.github.com") == f"token {FAKE_GITHUB_TOKEN}"
    assert auth_header_for("slack.com") == f"Bearer {FAKE_SLACK_TOKEN}"
    # Discord's token already names its scheme ("Bot ..."), so it is sent as-is
    # rather than becoming "Bearer Bot ...".
    assert auth_header_for("discord.com") == FAKE_DISCORD_TOKEN
    # Linear sends a personal API key bare, with no scheme at all.
    assert auth_header_for("api.linear.app") == registry.get("linear").token


def test_a_domain_no_twin_claims_gets_nothing():
    assert auth_header_for("api.unknown.example") is None


def test_a_subdomain_inherits_its_parent_twin():
    """Supabase gives every project its own hostname under supabase.co."""
    assert auth_header_for("abcdefgh.supabase.co") == f"Bearer {FAKE_SUPABASE_TOKEN}"


def test_the_domains_come_from_the_registry():
    assert intercepted_domains() == sorted(registry.domains())
    # The three that the old hand-maintained table had lost. oauth2 is the one
    # that mattered: it is where google-auth refreshes an access token, so a
    # service account whose refresh was not intercepted reached the real Google.
    assert {"uploads.github.com", "discordapp.com", "oauth2.googleapis.com"} <= set(
        intercepted_domains())


def test_proxy_routes_pair_each_domain_with_its_twin_and_credential():
    routes = proxy_routes({"api.github.com": "http://127.0.0.1:18080",
                           "checkpoint.supabase.co": "http://127.0.0.1:18081"})
    assert [(r.domain, r.upstream_url, r.auth_header) for r in routes] == [
        ("api.github.com", "http://127.0.0.1:18080", f"token {FAKE_GITHUB_TOKEN}"),
        ("checkpoint.supabase.co", "http://127.0.0.1:18081", f"Bearer {FAKE_SUPABASE_TOKEN}"),
    ]
