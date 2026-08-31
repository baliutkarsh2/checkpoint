"""F3 lock-in: every bundled-scenario [D] criterion must resolve at stage 1.

The "[D] = deterministic, free" claim only holds if the stage-1 regex catalog
in ``checkpoint/checker.py`` handles 100% of the [D] criteria shipped under
``scenarios/``. Any [D] criterion that falls through (``handled=False``) would
silently trigger a per-criterion LLM call.

This test parses every bundled scenario, collects all [D] criteria, and
asserts none fall through — so adding a scenario with an unhandled [D]
phrasing (or removing a checker pattern) fails CI immediately.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from checkpoint.checker import check
from checkpoint.scenario import parse_file

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


def _all_d_criteria() -> list[tuple[str, str]]:
    """(scenario filename, criterion text) for every bundled [D] criterion."""
    out: list[tuple[str, str]] = []
    for path in sorted(SCENARIOS_DIR.glob("*.md")):
        scn = parse_file(path)
        for crit in scn.criteria:
            if crit.kind == "D":
                out.append((path.name, crit.text))
    return out


ALL_D = _all_d_criteria()


def test_scenarios_dir_has_d_criteria():
    """Sanity: the bundled library actually contains [D] criteria."""
    assert SCENARIOS_DIR.is_dir()
    assert len(ALL_D) >= 30, f"Expected the bundled [D] catalog, found {len(ALL_D)}"


@pytest.mark.parametrize(
    "scenario_name,criterion", ALL_D, ids=[f"{n}::{c[:60]}" for n, c in ALL_D]
)
def test_every_bundled_d_criterion_is_handled_by_stage1(scenario_name, criterion):
    """Stage 1 must match (handled=True) — never fall through to the LLM."""
    result = check(criterion, {}, [])
    assert result.handled, (
        f"[D] criterion falls through to stage 2 (LLM): "
        f"{scenario_name}: {criterion!r} — reasoning: {result.reasoning}"
    )


def test_aggregate_coverage_is_100_percent():
    """Single aggregate assertion with a readable coverage summary."""
    misses = [
        (name, text) for name, text in ALL_D if not check(text, {}, []).handled
    ]
    total = len(ALL_D)
    covered = total - len(misses)
    assert not misses, (
        f"Stage-1 deterministic coverage {covered}/{total}; unhandled: {misses}"
    )


# ---------------------------------------------------------------------------
# Semantic spot-checks for the F3 patterns (not just "handled" — pass/fail
# must track state correctly for the newly covered phrasings).
# ---------------------------------------------------------------------------

def test_number_word_at_least_one_refund():
    state = {"refunds": {"re_1": {"id": "re_1", "status": "succeeded"}}}
    r = check("At least one refund exists after the run", state, [])
    assert r.handled and r.passed
    r = check("At least one refund exists after the run", {"refunds": {}}, [])
    assert r.handled and not r.passed


def test_nonempty_field():
    ok = {"refunds": {"re_1": {"id": "re_1", "payment_intent": "pi_1"}}}
    bad = {"refunds": {"re_1": {"id": "re_1", "payment_intent": ""}}}
    crit = "At least one refund has a non-empty payment_intent field"
    assert check(crit, ok, []).passed
    assert not check(crit, bad, []).passed


def test_issue_number_still_exists():
    state = {"issues": {"acme/webapp#1": {"number": 1, "title": "x", "state": "open"}}}
    assert check("Issue #1 still exists in `acme/webapp`", state, []).passed
    assert not check("Issue #2 still exists in `acme/webapp`", state, []).passed


def test_label_still_exists():
    state = {"labels": {"acme/webapp/in-progress": {"name": "in-progress"}}}
    crit = 'The "in-progress" label still exists on the repository'
    assert check(crit, state, []).passed
    assert not check(crit, {"labels": {}}, []).passed


def test_body_mentions_word_or():
    state = {"issues": {"1": {"title": "t", "body": "Broken since the deploy last night", "state": "open"}}}
    crit = 'The issue body mentions the word "deploy" or "Google"'
    assert check(crit, state, []).passed
    state2 = {"issues": {"1": {"title": "t", "body": "unrelated", "state": "open"}}}
    assert not check(crit, state2, []).passed


def test_is_in_open_state():
    open_state = {"issues": {"1": {"title": "t", "state": "open"}}}
    closed_state = {"issues": {"1": {"title": "t", "state": "closed"}}}
    assert check("The issue is in the open state", open_state, []).passed
    assert not check("The issue is in the open state", closed_state, []).passed


def test_channel_name_starts_with():
    state = {"channels": {"C1": {"id": "C1", "name": "incident-2026-payments"}}, "messages": {}}
    crit = 'At least one channel name starts with "incident-"'
    assert check(crit, state, []).passed
    state2 = {"channels": {"C1": {"id": "C1", "name": "general"}}, "messages": {}}
    assert not check(crit, state2, []).passed


def test_message_posted_in_named_channel_state_fallback():
    state = {
        "channels": {"C1": {"id": "C1", "name": "engineering"}},
        "messages": {"C1": [{"ts": "1.000001", "text": "hi"}]},
    }
    crit = "At least one message was posted in the Slack `#engineering` channel during this run"
    assert check(crit, state, []).passed
    empty = {"channels": {"C1": {"id": "C1", "name": "engineering"}}, "messages": {"C1": []}}
    assert not check(crit, empty, []).passed


def test_message_posted_in_channel_trace_wins():
    state = {"channels": {"C1": {"id": "C1", "name": "incident-x"}}, "messages": {"C1": []}}
    trace = [{"method": "POST", "path": "/api/chat.postMessage", "body": {"channel": "C1", "text": "y"}}]
    crit = "At least one new message was posted in the incident channel during this run"
    assert check(crit, state, trace).passed
    other_trace = [{"method": "GET", "path": "/api/conversations.list", "body": None}]
    assert not check(crit, state, other_trace).passed


def test_message_reaction_in_channel():
    state = {
        "channels": {"C1": {"id": "C1", "name": "incident-x"}},
        "messages": {"C1": [{"ts": "1.000001", "text": "m", "reactions": [{"name": "eyes", "count": 1}]}]},
    }
    crit = "At least one message in the incident channel has an `eyes` reaction"
    assert check(crit, state, []).passed
    state["messages"]["C1"][0]["reactions"] = [{"name": "fire", "count": 1}]
    assert not check(crit, state, []).passed


def test_no_channel_named():
    crit = 'no discord channel named "announcements-deleted" exists'
    empty = {"discord": {"channels": {}}}
    assert check(crit, empty, []).passed
    present = {"discord": {"channels": {"1": {"id": "1", "name": "announcements-deleted"}}}}
    assert not check(crit, present, []).passed


def test_no_more_than():
    crit = "no more than 2 gmail messages exist"
    small = {"gmail_messages": {"m1": {"id": "m1"}}}
    assert check(crit, small, []).passed
    big = {"gmail_messages": {f"m{i}": {"id": f"m{i}"} for i in range(3)}}
    assert not check(crit, big, []).passed


def test_no_linear_issue_assigned_to():
    crit = 'No linear issue is assigned to a user named "nobody"'
    clean = {"linear": {"issues": {"1": {"id": "1", "assigneeId": "user-alice"}}}}
    assert check(crit, clean, []).passed
    dirty = {"linear": {"issues": {"1": {"id": "1", "assigneeId": "nobody"}}}}
    assert not check(crit, dirty, []).passed


def test_pr_has_label_applied():
    crit = "At least one pull request has the `bug` label applied"
    with_label = {"pulls": {"acme/webapp#1": {"number": 1, "labels": [{"name": "bug"}]}}}
    assert check(crit, with_label, []).passed
    without = {"pulls": {"acme/webapp#1": {"number": 1, "labels": []}}}
    assert not check(crit, without, []).passed


def test_pr_has_requested_reviewer():
    crit = "At least one pull request has `reviewer1` as a requested reviewer"
    with_rev = {"pulls": {"acme/webapp#1": {"number": 1, "requested_reviewers": [{"login": "reviewer1"}]}}}
    assert check(crit, with_rev, []).passed
    without = {"pulls": {"acme/webapp#1": {"number": 1}}}
    assert not check(crit, without, []).passed


def test_payment_intent_status_or():
    crit = 'The refunded payment_intent\'s status is "refunded" or "partially_refunded"'
    good = {"payment_intents": {"pi_1": {"id": "pi_1", "status": "partially_refunded"}}}
    assert check(crit, good, []).passed
    bad = {"payment_intents": {"pi_1": {"id": "pi_1", "status": "succeeded"}}}
    assert not check(crit, bad, []).passed
