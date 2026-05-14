"""Tests for checker patterns added in Phase 9:
  - at-least-N-named (bug fix: was silently dropping the name constraint)
  - Supabase dynamic table row patterns
"""
from __future__ import annotations

import pytest
from checkpoint.checker import check


def supabase_state(tables=None, buckets=None, auth_users=None) -> dict:
    return {
        "tables": tables or {},
        "auth_users": auth_users or {},
        "storage": {
            "buckets": {b["id"]: b for b in (buckets or [])},
            "objects": {},
        },
    }


# ---------------------------------------------------------------------------
# Bug fix: at-least-N-named must respect the name, not just count
# ---------------------------------------------------------------------------

def test_at_least_1_bucket_named_pass():
    state = supabase_state(buckets=[
        {"id": "receipts", "name": "receipts", "public": False},
    ])
    r = check('At least 1 bucket exists named "receipts"', state, [])
    assert r.handled
    assert r.passed


def test_at_least_1_bucket_named_wrong_name_fail():
    """Bug: previously this would pass even with the wrong bucket name."""
    state = supabase_state(buckets=[
        {"id": "photos", "name": "photos", "public": False},
    ])
    r = check('At least 1 bucket exists named "receipts"', state, [])
    assert r.handled
    assert not r.passed


def test_at_least_1_bucket_named_empty_fail():
    state = supabase_state()
    r = check('At least 1 bucket exists named "receipts"', state, [])
    assert r.handled
    assert not r.passed


def test_at_least_n_named_with_inline_qualifier():
    state = {
        "channels": {
            "1": {"id": "1", "name": "incidents"},
            "2": {"id": "2", "name": "general"},
        }
    }
    r = check('at least 1 channel named "incidents" exists', state, [])
    assert r.handled
    assert r.passed


def test_at_least_n_named_channel_wrong_name():
    state = {"channels": {"1": {"id": "1", "name": "alerts"}}}
    r = check('at least 1 channel named "incidents" exists', state, [])
    assert r.handled
    assert not r.passed


def test_exactly_1_channel_named_exists():
    state = {
        "channels": {
            "1": {"id": "1", "name": "ops"},
            "2": {"id": "2", "name": "dev"},
        }
    }
    r = check('exactly 1 channel named "ops" exists', state, [])
    assert r.handled
    assert r.passed


# ---------------------------------------------------------------------------
# Supabase dynamic table row patterns
# ---------------------------------------------------------------------------

def test_supabase_table_rows_gte_pass():
    state = supabase_state(tables={
        "tasks": {"rows": [{"id": 1}, {"id": 2}, {"id": 3}]}
    })
    r = check("at least 2 rows in tasks", state, [])
    assert r.handled
    assert r.passed


def test_supabase_table_rows_gte_fail():
    state = supabase_state(tables={"tasks": {"rows": [{"id": 1}]}})
    r = check("at least 3 rows in tasks", state, [])
    assert r.handled
    assert not r.passed


def test_supabase_table_rows_eq_pass():
    state = supabase_state(tables={"products": {"rows": [{"id": 1}, {"id": 2}]}})
    r = check("exactly 2 rows in products", state, [])
    assert r.handled
    assert r.passed


def test_supabase_table_rows_eq_fail():
    state = supabase_state(tables={"products": {"rows": [{"id": 1}]}})
    r = check("exactly 2 rows in products", state, [])
    assert r.handled
    assert not r.passed


def test_supabase_table_rows_nested_state():
    """Multi-clone state: supabase is nested under 'supabase' key."""
    state = {
        "supabase": supabase_state(tables={
            "orders": {"rows": [{"id": "a"}, {"id": "b"}]}
        }),
        "github": {},
    }
    r = check("at least 1 rows in orders", state, [])
    assert r.handled
    assert r.passed


def test_supabase_table_has_n_rows_syntax():
    state = supabase_state(tables={"users": {"rows": [{"id": 1}, {"id": 2}]}})
    r = check("the users table has 2 rows", state, [])
    assert r.handled
    assert r.passed


def test_supabase_table_has_n_rows_fail():
    state = supabase_state(tables={"users": {"rows": []}})
    r = check("the users table has 1 rows", state, [])
    assert r.handled
    assert not r.passed
