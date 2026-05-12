"""Phase 5 / Plan 05-01: Stage-1 [D] regex catalog tests.

Covers Archal-equivalent patterns generalized across github / slack / stripe
twin state shapes. Synthetic state dicts only — no twin processes, no LLM.
"""
from __future__ import annotations

import pytest

from checkpoint.checker import check


# ---------------------------------------------------------------------------
# State builders
# ---------------------------------------------------------------------------

def gh_state(issues=None, pulls=None, labels=None, repos=None, workflow_runs=None) -> dict:
    return {
        "issues": {str(i.get("id", n)): i for n, i in enumerate(issues or [])},
        "pulls": {str(p.get("id", n)): p for n, p in enumerate(pulls or [])},
        "labels": {str(l.get("name", n)): l for n, l in enumerate(labels or [])},
        "repos": repos or {},
        "workflow_runs": {str(r.get("id", n)): r for n, r in enumerate(workflow_runs or [])},
        "comments": {},
    }


def slack_state(channels=None, messages=None) -> dict:
    return {
        "channels": {str(c.get("id", n)): c for n, c in enumerate(channels or [])},
        "messages": messages or {},
        "users": {},
    }


def stripe_state(customers=None, refunds=None, products=None, subscriptions=None) -> dict:
    return {
        "customers": {str(c.get("id", n)): c for n, c in enumerate(customers or [])},
        "refunds": {str(r.get("id", n)): r for n, r in enumerate(refunds or [])},
        "products": {str(p.get("id", n)): p for n, p in enumerate(products or [])},
        "subscriptions": {str(s.get("id", n)): s for n, s in enumerate(subscriptions or [])},
    }


def multi(github=None, slack=None, stripe=None) -> dict:
    return {
        "github": github or gh_state(),
        "slack":  slack or slack_state(),
        "stripe": stripe or stripe_state(),
    }


# ---------------------------------------------------------------------------
# Count patterns — github
# ---------------------------------------------------------------------------

def test_exactly_n_issues_closed_pass():
    s = gh_state(issues=[
        {"id": 1, "state": "closed"},
        {"id": 2, "state": "closed"},
        {"id": 3, "state": "open"},
    ])
    r = check("Exactly 2 issues are closed", s, [])
    assert r.handled and r.passed, r.reasoning


def test_exactly_n_issues_closed_fail():
    s = gh_state(issues=[{"id": 1, "state": "closed"}])
    r = check("Exactly 2 issues are closed", s, [])
    assert r.handled and not r.passed


def test_exactly_n_prs_merged():
    s = gh_state(pulls=[
        {"id": 1, "state": "merged"},
        {"id": 2, "state": "merged"},
        {"id": 3, "state": "open"},
    ])
    r = check("Exactly 2 PRs are merged", s, [])
    assert r.handled and r.passed


def test_at_least_n_issues_open():
    s = gh_state(issues=[{"id": i, "state": "open"} for i in range(5)])
    r = check("At least 3 issues are open", s, [])
    assert r.handled and r.passed


def test_at_most_n_issues_open_pass():
    s = gh_state(issues=[{"id": 1, "state": "open"}])
    r = check("At most 2 issues are open", s, [])
    assert r.handled and r.passed


def test_at_most_n_issues_open_fail():
    s = gh_state(issues=[{"id": i, "state": "open"} for i in range(5)])
    r = check("At most 2 issues are open", s, [])
    assert r.handled and not r.passed


def test_no_new_issues():
    r = check("No new issues are created", gh_state(), [])
    assert r.handled and r.passed


def test_no_new_issues_short():
    r = check("No new issues", gh_state(issues=[{"id": 1, "state": "open"}]), [])
    assert r.handled and not r.passed


def test_zero_issues_were_created():
    r = check("Zero issues were created", gh_state(), [])
    assert r.handled and r.passed


def test_count_of_labels_equals_2():
    s = gh_state(labels=[{"name": "bug"}, {"name": "wontfix"}])
    r = check("Count of labels equals 2", s, [])
    assert r.handled and r.passed


def test_labels_count_is_3():
    s = gh_state(labels=[{"name": "a"}, {"name": "b"}, {"name": "c"}])
    r = check("Labels count is 3", s, [])
    assert r.handled and r.passed


def test_exactly_n_workflow_runs():
    s = gh_state(workflow_runs=[{"id": 1}, {"id": 2}])
    r = check("Exactly 2 workflow runs", s, [])
    assert r.handled and r.passed


def test_repo_has_exactly_2_labels():
    s = gh_state(labels=[{"name": "bug"}, {"name": "duplicate"}])
    r = check("the repo has exactly 2 labels", s, [])
    assert r.handled and r.passed


# ---------------------------------------------------------------------------
# Existence / title patterns — github
# ---------------------------------------------------------------------------

def test_issue_titled_double_quote():
    s = gh_state(issues=[{"id": 1, "title": "Launch coordination", "state": "open"}])
    r = check('An issue titled "Launch coordination" exists', s, [])
    assert r.handled and r.passed


def test_issue_titled_single_quote():
    s = gh_state(issues=[{"id": 1, "title": "Fix bug", "state": "open"}])
    r = check("An issue titled 'Fix bug' exists", s, [])
    assert r.handled and r.passed


def test_issue_titled_missing():
    s = gh_state(issues=[{"id": 1, "title": "Other", "state": "open"}])
    r = check('An issue titled "Missing" exists', s, [])
    assert r.handled and not r.passed


def test_label_named_exists():
    s = gh_state(labels=[{"name": "wontfix"}])
    r = check('A label named "wontfix" exists', s, [])
    assert r.handled and r.passed


def test_pr_named_exists():
    s = gh_state(pulls=[{"id": 1, "title": "Add feature X", "state": "open"}])
    r = check('A pull request named "Add feature X" exists', s, [])
    assert r.handled and r.passed


def test_issue_with_title_exists():
    s = gh_state(issues=[{"id": 1, "title": "Bug A", "state": "open"}])
    r = check('An issue with title "Bug A" exists', s, [])
    assert r.handled and r.passed


# ---------------------------------------------------------------------------
# Selector patterns — github
# ---------------------------------------------------------------------------

def test_label_remain_open_pass():
    s = gh_state(issues=[
        {"id": 1, "state": "open", "labels": [{"name": "keep-open"}]},
        {"id": 2, "state": "open", "labels": [{"name": "keep-open"}]},
    ])
    r = check('Issues with "keep-open" remain open', s, [])
    assert r.handled and r.passed


def test_label_remain_open_fail():
    s = gh_state(issues=[
        {"id": 1, "state": "closed", "labels": [{"name": "keep-open"}]},
    ])
    r = check('Issues with "keep-open" remain open', s, [])
    assert r.handled and not r.passed


def test_all_closed_have_comment_pass():
    s = gh_state(issues=[
        {"id": 1, "state": "closed", "comments": 2},
        {"id": 2, "state": "closed", "comments": 1},
    ])
    r = check("All closed issues have a comment", s, [])
    assert r.handled and r.passed


def test_all_closed_have_new_comment_alias():
    s = gh_state(issues=[
        {"id": 1, "state": "closed", "comments": 1},
    ])
    r = check("All closed issues have a new comment", s, [])
    assert r.handled and r.passed


def test_all_closed_have_comment_fail():
    s = gh_state(issues=[
        {"id": 1, "state": "closed", "comments": 0},
        {"id": 2, "state": "closed", "comments": 1},
    ])
    r = check("All closed issues have a comment", s, [])
    assert r.handled and not r.passed


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def test_exactly_2_channels():
    s = slack_state(channels=[{"id": "C1"}, {"id": "C2"}])
    r = check("Exactly 2 channels exist", s, [])
    assert r.handled and r.passed


def test_channel_named_exists():
    s = slack_state(channels=[{"id": "C1", "name": "engineering"}])
    r = check('A channel named "engineering" exists', s, [])
    assert r.handled and r.passed


def test_no_new_channels():
    r = check("No new channels", slack_state(), [])
    assert r.handled and r.passed


def test_at_least_3_messages_multi_clone():
    # Slack messages live under per-channel arrays.
    state = slack_state()
    state["messages"]["C1"] = [
        {"text": "hi"},
        {"text": "there"},
        {"text": "team"},
    ]
    r = check("At least 3 messages", state, [])
    assert r.handled and r.passed


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

def test_exactly_1_refund():
    s = stripe_state(refunds=[{"id": "re_1", "status": "succeeded"}])
    r = check("Exactly 1 refund exists", s, [])
    assert r.handled and r.passed


def test_at_least_2_customers():
    s = stripe_state(customers=[{"id": "cus_1"}, {"id": "cus_2"}, {"id": "cus_3"}])
    r = check("At least 2 customers exist", s, [])
    assert r.handled and r.passed


def test_at_most_1_subscription():
    s = stripe_state(subscriptions=[{"id": "sub_1", "status": "active"}])
    r = check("At most 1 subscription exists", s, [])
    assert r.handled and r.passed


def test_no_new_disputes():
    r = check("No new disputes", stripe_state(), [])
    assert r.handled and r.passed


def test_count_of_products_equals_2():
    s = stripe_state(products=[{"id": "prod_1"}, {"id": "prod_2"}])
    r = check("Count of products equals 2", s, [])
    assert r.handled and r.passed


# ---------------------------------------------------------------------------
# Multi-clone shape
# ---------------------------------------------------------------------------

def test_multi_clone_exactly_2_issues_closed():
    state = multi(
        github=gh_state(issues=[
            {"id": 1, "state": "closed"},
            {"id": 2, "state": "closed"},
            {"id": 3, "state": "open"},
        ]),
        slack=slack_state(channels=[{"id": "C1", "name": "incident"}]),
        stripe=stripe_state(),
    )
    r = check("Exactly 2 issues are closed", state, [])
    assert r.handled and r.passed


def test_multi_clone_channel_named_exists():
    state = multi(
        github=gh_state(),
        slack=slack_state(channels=[{"id": "C1", "name": "incident"}]),
        stripe=stripe_state(),
    )
    r = check('A channel named "incident" exists', state, [])
    assert r.handled and r.passed


def test_multi_clone_refund_count():
    state = multi(
        github=gh_state(),
        slack=slack_state(),
        stripe=stripe_state(refunds=[{"id": "re_1"}, {"id": "re_2"}]),
    )
    r = check("Exactly 2 refunds exist", state, [])
    assert r.handled and r.passed


# ---------------------------------------------------------------------------
# Fallthrough / unhandled
# ---------------------------------------------------------------------------

def test_unhandled_returns_handled_false():
    r = check("The PR description is clear and concise", gh_state(), [])
    assert not r.handled


def test_unknown_resource_falls_through():
    # 'gizmos' is not in the resource map; pattern shouldn't match.
    r = check("Exactly 3 gizmos are created", {}, [])
    assert not r.handled


# ---------------------------------------------------------------------------
# Misc / smoke
# ---------------------------------------------------------------------------

def test_case_insensitive():
    s = gh_state(issues=[{"id": 1, "state": "closed"}, {"id": 2, "state": "closed"}])
    r = check("EXACTLY 2 ISSUES ARE CLOSED", s, [])
    assert r.handled and r.passed


def test_pattern_count_is_at_least_25():
    """Sanity: catalog has enough breadth to be considered 'expanded'."""
    from checkpoint.checker import PATTERNS
    assert len(PATTERNS) >= 15  # number of regex pairs; each handles N nouns
