from checkpoint.proxy.routes import Route, all_domains, lookup, register


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
