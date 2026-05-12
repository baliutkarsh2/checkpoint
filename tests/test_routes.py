"""Phase 3 Plan 02: proxy routes registry contains all three domains with correct tokens."""
from __future__ import annotations

from checkpoint.proxy import routes


def test_github_route_registered():
    r = routes.lookup("api.github.com")
    assert r is not None
    assert r.domain == "api.github.com"
    assert r.bootstrap_token.startswith("ghp_")


def test_slack_route_registered():
    r = routes.lookup("slack.com")
    assert r is not None
    assert r.domain == "slack.com"
    assert r.bootstrap_token.startswith("xoxb-")


def test_stripe_route_registered():
    r = routes.lookup("api.stripe.com")
    assert r is not None
    assert r.domain == "api.stripe.com"
    assert r.bootstrap_token.startswith("sk_live_")


def test_unknown_domain_returns_none():
    assert routes.lookup("example.com") is None


def test_all_domains_listed():
    domains = set(routes.all_domains())
    assert {"api.github.com", "slack.com", "api.stripe.com"}.issubset(domains)


def test_register_updates_twin_url():
    routes.register("slack.com", "http://127.0.0.1:9999")
    r = routes.lookup("slack.com")
    assert r.twin_url == "http://127.0.0.1:9999"
    # Token preserved.
    assert r.bootstrap_token.startswith("xoxb-")


def test_register_new_domain_requires_token():
    import pytest
    with pytest.raises(ValueError):
        routes.register("api.example.com", "http://127.0.0.1:9999")
