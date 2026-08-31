"""Wire-fidelity fixes: Stripe nested/array form parsing + GitHub Link pagination.

These cover the two concrete places the twins diverged from the official SDKs:
Stripe's bracket-path form encoding (`items[0][price]`, `expand[]`) and GitHub's
`Link` header that Octokit/PyGithub follow to auto-paginate.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import github as gh
from checkpoint.twins.stripe import _assign_bracket_path, _bracket_segments

# --- Stripe bracket-path form parsing ------------------------------------

def test_bracket_segments():
    assert _bracket_segments("amount") == ["amount"]
    assert _bracket_segments("metadata[foo]") == ["metadata", "foo"]
    assert _bracket_segments("items[0][price]") == ["items", "0", "price"]
    assert _bracket_segments("expand[]") == ["expand", ""]


def test_assign_nested_object():
    out: dict = {}
    _assign_bracket_path(out, "metadata[order_id]", "ord_123")
    _assign_bracket_path(out, "metadata[source]", "cli")
    assert out == {"metadata": {"order_id": "ord_123", "source": "cli"}}


def test_assign_indexed_line_items():
    # The exact shape the Stripe SDK sends for line items — the old one-level
    # parser mangled the nested key into garbage.
    out: dict = {}
    _assign_bracket_path(out, "items[0][price]", "price_A")
    _assign_bracket_path(out, "items[0][quantity]", "2")
    _assign_bracket_path(out, "items[1][price]", "price_B")
    assert out == {
        "items": {
            "0": {"price": "price_A", "quantity": "2"},
            "1": {"price": "price_B"},
        }
    }


def test_assign_repeated_array():
    out: dict = {}
    _assign_bracket_path(out, "expand[]", "customer")
    _assign_bracket_path(out, "expand[]", "latest_charge")
    assert out == {"expand": ["customer", "latest_charge"]}


# --- GitHub Link-header pagination ---------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    gh.STATE.clear()
    gh.STATE.update(gh._fresh_state())
    gh.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(gh.app)


H = {"Authorization": f"token {gh.DEFAULT_BOOTSTRAP_TOKEN}"}


def test_issues_paginate_with_link_header(client):
    client.post("/user/repos", json={"name": "webapp"}, headers=H)
    for i in range(35):
        r = client.post(
            "/repos/default-user/webapp/issues",
            json={"title": f"issue {i}"},
            headers=H,
        )
        assert r.status_code == 201

    # Page 1: exactly per_page items and a rel="next" Link header.
    r1 = client.get("/repos/default-user/webapp/issues?state=all&per_page=30&page=1", headers=H)
    assert r1.status_code == 200
    assert len(r1.json()) == 30
    link = r1.headers.get("Link", "")
    assert 'rel="next"' in link and "page=2" in link
    assert 'rel="last"' in link

    # Page 2: the remaining 5, with a rel="prev" back-link and no rel="next".
    r2 = client.get("/repos/default-user/webapp/issues?state=all&per_page=30&page=2", headers=H)
    assert len(r2.json()) == 5
    assert 'rel="prev"' in r2.headers.get("Link", "")
    assert 'rel="next"' not in r2.headers.get("Link", "")


def test_small_list_has_no_link_header(client):
    """A list that fits on one page must not advertise pagination."""
    client.post("/user/repos", json={"name": "webapp"}, headers=H)
    client.post("/repos/default-user/webapp/issues", json={"title": "only one"}, headers=H)
    r = client.get("/repos/default-user/webapp/issues?state=all", headers=H)
    assert len(r.json()) == 1
    assert "Link" not in r.headers
