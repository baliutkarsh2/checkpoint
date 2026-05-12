from checkpoint.proxy.routes import Route, lookup, register, all_domains


def test_lookup_github_returns_route_with_ghp_token():
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


def test_phase1_only_has_github():
    # Slack and Stripe land in Phase 3 — make sure we didn't pre-add them.
    domains = set(all_domains())
    assert domains == {"api.github.com"}
