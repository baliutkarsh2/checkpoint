"""The `path` label on the dashboard's metrics is a route, not a URL.

Prometheus keeps one time series per label combination, so a label that carries
a run id is an unbounded label: a day of browsing fills the scrape with
single-observation series and the dashboard's own metrics become the thing that
falls over. The routes are known, so a route template decides the label.

These tests pin both halves of that — the ids that must collapse, and the
literal segments that must survive — because the two failures look opposite and
cost the same.
"""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from checkpoint.dashboard.app import _PATH_BUCKETS, _bucket_path, create_app


@pytest.fixture
def client(tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "scenarios").mkdir()
    return TestClient(create_app(runs_dir=tmp_path / "runs",
                                 scenarios_dir=tmp_path / "scenarios"))


# -- what must collapse --------------------------------------------------------


@pytest.mark.parametrize(("path", "bucket"), [
    ("/api/runs/6f1c2d3e4a5b", "/api/runs/{id}"),
    ("/api/runs/6f1c2d3e4a5b/telemetry", "/api/runs/{id}/telemetry"),
    ("/api/runs/6f1c2d3e4a5b/anonymized", "/api/runs/{id}/anonymized"),
    ("/api/gates/1234567890123456", "/api/gates/{id}"),
    ("/api/jobs/9f2/stream", "/api/jobs/{id}/stream"),
    ("/api/twins/github", "/api/twins/{id}"),
    ("/api/twins/github/reset", "/api/twins/{id}/reset"),
    ("/api/twins/github/seed/acme-webapp", "/api/twins/{id}/seed/{name}"),
    ("/runs/6f1c2d3e4a5b", "/runs/{id}"),
    ("/live/9c5d0a1b2e3f4a5b6c7d8e9f0a1b2c3d", "/live/{id}"),
])
def test_an_id_in_a_known_route_becomes_the_route(path, bucket):
    assert _bucket_path(path) == bucket


def test_a_gate_id_of_only_digits_is_still_an_id():
    """The old guess needed a letter *and* a digit in the segment.

    Gate ids are a prefix of `uuid4().hex`, so roughly one in 10^19 is all
    digits — and one in 10^19 is enough, because that is not a rate, it is a
    user whose dashboard writes a series nobody ever reads again.
    """
    assert _bucket_path("/api/gates/1234567890123456") == "/api/gates/{id}"


def test_a_short_id_is_still_an_id():
    """The old guess ignored anything under eight characters."""
    assert _bucket_path("/api/jobs/9f2") == "/api/jobs/{id}"


# -- what must survive ---------------------------------------------------------


def test_a_literal_segment_that_looks_like_an_id_is_left_alone():
    """The case the guess got wrong, and the reason the route list exists.

    `index-Kk_dH6hR.js` is eight-plus alphanumerics with a digit in it, so the
    old heuristic replaced it with `{id}` — collapsing every asset a deploy
    served into one series, which is the one place the exact name is the whole
    point of the measurement.
    """
    assert _bucket_path("/assets/index-Kk_dH6hR.js") == "/assets/index-Kk_dH6hR.js"


def test_a_literal_route_is_not_swallowed_by_the_template_beside_it():
    """`/api/twins/supported` is a route; `/api/twins/{id}` must not claim it."""
    assert _bucket_path("/api/twins/supported") == "/api/twins/supported"


@pytest.mark.parametrize("path", ["/", "/healthz", "/metrics", "/api/runs", "/api/scenarios/file"])
def test_a_route_with_no_id_is_its_own_label(path):
    assert _bucket_path(path) == path


def test_a_trailing_slash_does_not_fill_in_a_placeholder():
    """An empty segment is a malformed URL, not an id, and must not be labelled
    as the route a client never actually reached."""
    assert _bucket_path("/api/runs/") == "/api/runs/"


# -- the list and the app must not drift apart ---------------------------------


def test_every_parametrised_api_route_has_a_bucket(client):
    """Adding a route with an id and forgetting the list fails here.

    The placeholder is filled with a value the fallback guess cannot recognise,
    so a route that is missing from `_PATH_BUCKETS` comes back as itself rather
    than as a template — which is exactly the unbounded label.
    """
    missing = []
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if "{" not in path or ":path}" in path:  # the SPA catch-all takes anything
            continue
        if _bucket_path(re.sub(r"\{[^}]+\}", "x", path)) not in _PATH_BUCKETS:
            missing.append(path)
    assert missing == [], f"no metrics bucket for: {missing}"


def test_each_bucket_is_a_label_its_own_paths_produce():
    """A template nothing maps to is a template that has quietly stopped
    matching the app — the state `_PATH_BUCKETS` sat in while it was dead."""
    for template in _PATH_BUCKETS:
        assert _bucket_path(re.sub(r"\{[^}]+\}", "x", template)) == template


# -- end to end ----------------------------------------------------------------


def test_two_run_ids_produce_one_metrics_series(client):
    """The assertion that matters: what /metrics actually exposes."""
    client.get("/api/runs/6f1c2d3e4a5b")
    client.get("/api/runs/0a9b8c7d6e5f")

    body = client.get("/metrics").text

    assert "6f1c2d3e4a5b" not in body and "0a9b8c7d6e5f" not in body
    series = [line for line in body.splitlines()
              if line.startswith("checkpoint_http_requests_total")
              and 'path="/api/runs/{id}"' in line]
    assert len(series) == 1, series
    assert series[0].endswith(" 2")
