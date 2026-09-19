"""Pure decision logic: route matching, the egress policy, and the routes wire format."""
from __future__ import annotations

import pytest

from checkpoint.proxy.server import (
    LLM_PROVIDER_HOSTS,
    EgressPolicy,
    InterceptProxy,
    Route,
    routes_from_json,
    routes_to_json,
)


def _proxy(ca, *routes: Route) -> InterceptProxy:
    return InterceptProxy(list(routes), EgressPolicy.open(), ca)  # never started


def test_route_matches_exact_domain(ca):
    route = Route("api.github.com", "http://127.0.0.1:1")
    proxy = _proxy(ca, route)
    assert proxy.route_for("api.github.com") is route
    assert proxy.route_for("API.GitHub.com.") is route


def test_route_matches_subdomains_of_its_domain(ca):
    route = Route("supabase.co", "http://127.0.0.1:1")
    proxy = _proxy(ca, route)
    assert proxy.route_for("abcdefgh.supabase.co") is route
    assert proxy.route_for("a.b.supabase.co") is route


def test_route_never_matches_a_lookalike_suffix(ca):
    proxy = _proxy(ca, Route("supabase.co", "http://127.0.0.1:1"))
    assert proxy.route_for("evilsupabase.co") is None
    assert proxy.route_for("supabase.co.evil.com") is None
    assert proxy.route_for("co") is None


def test_most_specific_route_wins(ca):
    parent = Route("example.test", "http://127.0.0.1:1")
    child = Route("api.example.test", "http://127.0.0.1:2")
    proxy = _proxy(ca, parent, child)
    assert proxy.route_for("api.example.test") is child
    assert proxy.route_for("v2.api.example.test") is child
    assert proxy.route_for("www.example.test") is parent


def test_ip_literals_only_match_exactly(ca):
    route = Route("1", "http://127.0.0.1:1")  # a pathological "domain"
    assert _proxy(ca, route).route_for("10.0.0.1") is None


def test_set_routes_replaces_the_table(ca):
    proxy = _proxy(ca, Route("a.test", "http://127.0.0.1:1"))
    proxy.set_routes([Route("b.test", "http://127.0.0.1:2")])
    assert proxy.route_for("a.test") is None
    assert proxy.route_for("b.test") is not None


def test_route_rejects_a_non_http_upstream():
    with pytest.raises(ValueError):
        Route("api.github.com", "ftp://127.0.0.1")
    with pytest.raises(ValueError):
        Route("", "http://127.0.0.1")


def test_open_policy_allows_everything():
    assert EgressPolicy.open().allows("anything.example", 1234)


def test_allowlist_exact_host_any_port():
    policy = EgressPolicy.allowlist(["api.openai.com"])
    assert policy.allows("api.openai.com", 443)
    assert policy.allows("API.OPENAI.COM", 8443)
    assert not policy.allows("evil.api.openai.com", 443)
    assert not policy.allows("api.openai.com.evil.com", 443)


def test_allowlist_wildcard_matches_subdomains_but_not_the_apex():
    policy = EgressPolicy.allowlist(["*.openai.azure.com"])
    assert policy.allows("myres.openai.azure.com", 443)
    assert policy.allows("a.b.openai.azure.com", 443)
    assert not policy.allows("openai.azure.com", 443)
    assert not policy.allows("evilopenai.azure.com", 443)


def test_allowlist_port_restriction():
    policy = EgressPolicy.allowlist(["localhost:11434", "[::1]:8080"])
    assert policy.allows("localhost", 11434)
    assert not policy.allows("localhost", 22)
    assert policy.allows("::1", 8080)
    assert not policy.allows("::1", 443)


def test_empty_allowlist_denies_everything():
    assert not EgressPolicy.allowlist([]).allows("api.openai.com", 443)


def test_invalid_pattern_is_rejected_up_front():
    with pytest.raises(ValueError):
        EgressPolicy.allowlist(["host:notaport"])


@pytest.mark.parametrize("host", [
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    "us-central1-aiplatform.googleapis.com", "aiplatform.googleapis.com",
    "myresource.openai.azure.com", "bedrock-runtime.us-east-1.amazonaws.com",
    "api.mistral.ai", "api.groq.com", "api.together.xyz", "api.fireworks.ai", "openrouter.ai",
    "api.deepseek.com", "api.x.ai", "api.cohere.com", "api.perplexity.ai",
    "localhost", "127.0.0.1", "::1",
])
def test_llm_provider_allowlist_covers_the_major_apis(host):
    assert EgressPolicy.allowlist(LLM_PROVIDER_HOSTS).allows(host, 443)


@pytest.mark.parametrize("host", [
    "api.github.com", "slack.com", "storage.googleapis.com", "s3.us-east-1.amazonaws.com",
])
def test_llm_provider_allowlist_is_not_a_blanket_cloud_allow(host):
    assert not EgressPolicy.allowlist(LLM_PROVIDER_HOSTS).allows(host, 443)


def test_routes_json_round_trip():
    routes = [
        Route("api.github.com", "http://127.0.0.1:18080", auth_header="token t"),
        Route("supabase.co", "http://127.0.0.1:18081", extra_headers={"apikey": "k"}),
    ]
    assert routes_from_json(routes_to_json(routes)) == routes


def test_routes_json_accepts_a_bare_url_shorthand():
    assert routes_from_json('{"api.github.com": "http://127.0.0.1:1"}') == [
        Route("api.github.com", "http://127.0.0.1:1")
    ]


@pytest.mark.parametrize("text", ['[]', '{"a.test": 1}', '{"a.test": {"nope": 1}}'])
def test_routes_json_rejects_malformed_input(text):
    with pytest.raises(ValueError):
        routes_from_json(text)
