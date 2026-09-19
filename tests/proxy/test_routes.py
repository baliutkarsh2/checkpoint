from checkpoint.fake_credentials import (
    FAKE_DISCORD_TOKEN,
    FAKE_GITHUB_TOKEN,
    FAKE_SLACK_TOKEN,
    FAKE_SUPABASE_TOKEN,
)
from checkpoint.proxy.routes import (
    Route,
    all_domains,
    auth_header_for,
    lookup,
    proxy_routes,
    register,
)


def test_lookup_github_returns_route_with_github_token():
    r = lookup("api.github.com")
    assert isinstance(r, Route)
    assert r.domain == "api.github.com"
    assert r.bootstrap_token.startswith("ghp_")


def test_lookup_unknown_host_returns_none():
    assert lookup("api.unknown.example") is None


def test_register_overwrites_twin_url():
    register("api.github.com", "http://127.0.0.1:54321")
    r = lookup("api.github.com")
    assert r.twin_url == "http://127.0.0.1:54321"
    assert r.bootstrap_token.startswith("ghp_")


def test_phase3_has_github_slack_stripe():
    # Phase 3 registered slack.com + api.stripe.com; later phases added the rest.
    domains = set(all_domains())
    assert {"api.github.com", "slack.com", "api.stripe.com"}.issubset(domains)


def test_auth_header_schemes_match_what_each_twin_accepts():
    assert auth_header_for("api.github.com") == f"token {FAKE_GITHUB_TOKEN}"
    assert auth_header_for("slack.com") == f"Bearer {FAKE_SLACK_TOKEN}"
    # Discord's token already names its scheme ("Bot ..."), so it is sent as-is.
    assert auth_header_for("discord.com") == FAKE_DISCORD_TOKEN
    assert auth_header_for("unknown.example") is None


def test_auth_header_inherits_from_the_parent_domain():
    assert auth_header_for("abcdefgh.supabase.co") == f"Bearer {FAKE_SUPABASE_TOKEN}"


def test_proxy_routes_stamp_each_twins_credential():
    routes = proxy_routes({"api.github.com": "http://127.0.0.1:18080",
                           "checkpoint.supabase.co": "http://127.0.0.1:18081"})
    assert [(r.domain, r.upstream_url, r.auth_header) for r in routes] == [
        ("api.github.com", "http://127.0.0.1:18080", f"token {FAKE_GITHUB_TOKEN}"),
        ("checkpoint.supabase.co", "http://127.0.0.1:18081", f"Bearer {FAKE_SUPABASE_TOKEN}"),
    ]
