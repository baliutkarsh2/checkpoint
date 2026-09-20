"""What `checkpoint runs` can tell you about runs that already happened.

The records under ``.checkpoint/cache/runs`` are the only evidence a verdict
leaves behind, so these commands are what anyone reaches for when a gate
blocked and nobody knows why. The fixture below writes the records a real run
would have written; every test then reads them back through the command.

Two things here are guarantees rather than conveniences: an id that matches no
run exits 1 rather than printing an empty report, and ``--anonymize`` really
removes the values that identify a person or unlock an account, because the
whole point of that flag is attaching a record to a public bug report.
"""
from __future__ import annotations

import json

import pytest

from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN
from checkpoint.run_record import build_record, write_record
from checkpoint.runner import CriterionResult

REFUND = "refund flow"
TRIAGE = "issue triage"

ISSUED = "The refund was issued"
NOT_DELETED = "No charges were deleted"

# A leak of each shape the exporter promises to remove, in an answer that still
# reads like something an agent would say.
LEAKY_ANSWER = (
    f"Emailed ada.lovelace@example.org, using token {FAKE_GITHUB_TOKEN} "
    f"and key sk-CHECKPOINTFAKE00000000."
)

TRACE = [
    {"twin": "github", "method": "GET", "path": "/repos/acme/webapp/issues",
     "status": 200, "ts": "2026-05-12T10:00:01Z"},
    {"twin": "stripe", "method": "POST", "path": "/v1/refunds",
     "status": 201, "ts": "2026-05-12T10:00:02Z"},
    {"twin": "github", "method": "POST", "path": "/repos/acme/webapp/issues/7/comments",
     "status": 201, "ts": "2026-05-12T10:00:03Z"},
]


def _criteria(issued: bool) -> list[CriterionResult]:
    """The verdicts a scored run carries, as the runner builds them."""
    return [
        CriterionResult(
            text=ISSUED, kind="D", passed=issued, evaluator="assertion:pattern",
            reasoning="the refund is in the twin's state" if issued
                      else "no refund reached stripe",
            assertion="count(created.stripe.refunds) == 1"),
        CriterionResult(
            text=NOT_DELETED, kind="D", passed=True, evaluator="assertion:pinned",
            reasoning="nothing was deleted", must_pass=True,
            assertion="count(deleted.stripe.charges) == 0"),
    ]


def _record(run_id: str, scenario: str, score: float, *, timestamp: str,
            criteria: list[CriterionResult], trace: list[dict] = (),
            answer: str = "done") -> dict:
    return build_record(
        scenario_name=scenario, scenario_path=f"scenarios/{scenario.replace(' ', '-')}.md",
        satisfaction=score, criteria=criteria, evaluator_model="stub-judge",
        evaluator_model_source="config", final_answer=answer, trace=list(trace),
        state={"stripe": {"refunds": [{"id": "re_1"}]}}, run_id=run_id, timestamp=timestamp,
        duration_ms=1234.0, twins=["github", "stripe"],
    )


@pytest.fixture
def recorded_runs(project) -> dict[str, dict]:
    """Four runs on disk, written oldest first as they would really arrive.

    Three of one scenario, so a trend has something to average and one
    criterion that both passes and fails to call flaky; one of another, so the
    scenario filter has something to leave out. The last one written is the one
    every command falls back to.
    """
    records = {
        "baseline": _record("aaaa11112222", REFUND, 100.0, timestamp="2026-05-12T10:00:00Z",
                            criteria=_criteria(issued=True), trace=TRACE),
        "regressed": _record("bbbb33334444", REFUND, 50.0, timestamp="2026-05-12T11:00:00Z",
                             criteria=_criteria(issued=False)),
        "recovered": _record("dddd77778888", REFUND, 100.0, timestamp="2026-05-12T11:30:00Z",
                             criteria=_criteria(issued=True)),
        "other": _record("cccc55556666", TRIAGE, 100.0, timestamp="2026-05-12T12:00:00Z",
                         criteria=[], answer=LEAKY_ANSWER),
    }
    for record in records.values():
        write_record(record)
    return records


# -- runs list -----------------------------------------------------------------


def test_runs_list_shows_the_newest_run_first(recorded_runs, run_cli):
    result = run_cli("runs", "list", "--json")

    assert [row["run_id"] for row in json.loads(result.stdout)] == [
        "cccc55556666", "dddd77778888", "bbbb33334444", "aaaa11112222"]


def test_runs_list_prints_the_id_and_score_of_each_run(recorded_runs, run_cli):
    result = run_cli("runs", "list")

    assert result.exit_code == 0, result.output
    assert "aaaa11112222" in result.output
    assert REFUND in result.output
    assert "50" in result.output


def test_runs_list_filters_by_a_substring_of_the_scenario_name(recorded_runs, run_cli):
    result = run_cli("runs", "list", "--scenario", "refund", "--json")

    rows = json.loads(result.stdout)
    assert {row["scenario"] for row in rows} == {REFUND}
    assert len(rows) == 3


def test_runs_list_honours_the_limit(recorded_runs, run_cli):
    result = run_cli("runs", "list", "-n", "2", "--json")

    assert [row["run_id"] for row in json.loads(result.stdout)] == [
        "cccc55556666", "dddd77778888"]


def test_runs_list_summarises_each_run_without_the_whole_record(recorded_runs, run_cli):
    row = json.loads(run_cli("runs", "list", "--json").stdout)[0]

    assert row["criteria_passed"] == 0 and row["criteria_total"] == 0
    assert row["judge_model"] == "stub-judge"
    assert "trace" not in row


def test_runs_list_with_nothing_recorded_says_so(project, run_cli):
    result = run_cli("runs", "list")

    assert result.exit_code == 0, result.output
    assert "No runs yet" in result.output


# -- runs show -----------------------------------------------------------------


def test_runs_show_defaults_to_the_most_recent_run(recorded_runs, run_cli):
    result = run_cli("runs", "show")

    assert result.exit_code == 0, result.output
    assert TRIAGE in result.output
    assert "cccc55556666" in result.output


def test_runs_show_accepts_a_unique_prefix_of_an_id(recorded_runs, run_cli):
    """The ids `runs list` prints are the ones people paste back, half-typed."""
    result = run_cli("runs", "show", "bbbb")

    assert result.exit_code == 0, result.output
    assert "50/100" in result.output


def test_runs_show_says_why_a_criterion_failed(recorded_runs, run_cli):
    result = run_cli("runs", "show", "bbbb33334444")

    assert ISSUED in result.output
    assert "no refund reached stripe" in result.output
    assert "count(created.stripe.refunds) == 1" in result.output


def test_runs_show_json_is_one_document_holding_the_whole_record(recorded_runs, run_cli):
    record = json.loads(run_cli("runs", "show", "aaaa11112222", "--json").stdout)

    assert record["run_id"] == "aaaa11112222"
    assert len(record["criteria"]) == 2
    assert record["trace"] == TRACE
    assert record["state"]


def test_an_unknown_run_id_exits_1_and_says_where_to_find_one(recorded_runs, run_cli):
    result = run_cli("runs", "show", "nosuchrun")

    assert result.exit_code == 1
    assert "checkpoint runs list" in result.output


# -- runs trace ----------------------------------------------------------------


def test_runs_trace_lists_the_calls_the_agent_made(recorded_runs, run_cli):
    result = run_cli("runs", "trace", "aaaa11112222")

    assert result.exit_code == 0, result.output
    assert "/v1/refunds" in result.output
    assert "POST" in result.output


def test_runs_trace_can_be_narrowed_to_one_twin(recorded_runs, run_cli):
    events = json.loads(run_cli("runs", "trace", "aaaa11112222",
                                "--twin", "github", "--json").stdout)

    assert [e["path"] for e in events] == [
        "/repos/acme/webapp/issues", "/repos/acme/webapp/issues/7/comments"]


def test_runs_trace_honours_the_limit(recorded_runs, run_cli):
    events = json.loads(run_cli("runs", "trace", "aaaa11112222", "-n", "1", "--json").stdout)

    assert len(events) == 1
    assert events[0]["method"] == "GET"  # oldest first, whatever order they were stored in


def test_runs_trace_on_a_run_that_called_nothing_says_so(recorded_runs, run_cli):
    result = run_cli("runs", "trace", "cccc55556666")

    assert result.exit_code == 0, result.output
    assert "no API calls" in result.output


# -- runs compare --------------------------------------------------------------


def test_runs_compare_names_the_criterion_that_regressed(recorded_runs, run_cli):
    result = run_cli("runs", "compare", "aaaa11112222", "bbbb33334444")

    assert result.exit_code == 0, result.output
    assert "Regressions" in result.output
    assert ISSUED in result.output
    assert NOT_DELETED not in result.output  # unchanged criteria are not the news


def test_runs_compare_reads_the_other_direction_as_a_fix(recorded_runs, run_cli):
    result = run_cli("runs", "compare", "bbbb33334444", "dddd77778888")

    assert "Fixes" in result.output
    assert ISSUED in result.output


def test_runs_compare_json_is_one_document_carrying_the_delta(recorded_runs, run_cli):
    diff = json.loads(run_cli("runs", "compare", "aaaa11112222", "bbbb33334444",
                              "--json").stdout)

    assert diff["delta"] == -50.0
    assert [d["text"] for d in diff["regressions"]] == [ISSUED]
    assert diff["fixes"] == []


def test_runs_compare_with_a_run_that_does_not_exist_exits_1(recorded_runs, run_cli):
    result = run_cli("runs", "compare", "aaaa11112222", "nosuchrun")

    assert result.exit_code == 1


# -- runs trend ----------------------------------------------------------------


def test_runs_trend_flags_a_criterion_that_sometimes_passes(recorded_runs, run_cli):
    """A flaky criterion is the one to fix first: until it is steady, no single
    run proves anything about the build."""
    trend = json.loads(run_cli("runs", "trend", "refund", "--json").stdout)

    assert trend["run_count"] == 3
    assert trend["flaky_criteria"] == [ISSUED]
    assert trend["criteria"][ISSUED]["pass_rate"] == pytest.approx(0.667, abs=0.001)
    assert trend["criteria"][NOT_DELETED]["pass_rate"] == 1.0
    assert (trend["avg_score"], trend["min_score"], trend["max_score"]) == (
        pytest.approx(83.3), 50.0, 100.0)


def test_runs_trend_covers_every_scenario_when_none_is_named(recorded_runs, run_cli):
    trend = json.loads(run_cli("runs", "trend", "--json").stdout)

    assert trend["run_count"] == 4


def test_runs_trend_matches_a_scenario_name_whatever_the_case(recorded_runs, run_cli):
    trend = json.loads(run_cli("runs", "trend", "REFUND", "--json").stdout)

    assert trend["run_count"] == 3


def test_two_runs_are_never_enough_to_call_a_criterion_flaky(recorded_runs, run_cli):
    """One pass and one fail is a coin toss, not evidence of flakiness."""
    trend = json.loads(run_cli("runs", "trend", "refund", "-n", "2", "--json").stdout)

    assert trend["run_count"] == 2
    assert trend["criteria"][ISSUED]["pass_rate"] == 0.5
    assert trend["flaky_criteria"] == []


def test_runs_trend_with_no_matching_run_says_so_rather_than_failing(recorded_runs, run_cli):
    result = run_cli("runs", "trend", "no-such-scenario")

    assert result.exit_code == 0, result.output
    assert "No runs match" in result.output


# -- runs export ---------------------------------------------------------------


def test_runs_export_writes_the_record_to_the_named_file(recorded_runs, run_cli, project):
    out = project.root / "bug-report.json"

    result = run_cli("runs", "export", "aaaa11112222", "-o", out)

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8"))["run_id"] == "aaaa11112222"


def test_export_anonymize_removes_emails_and_credentials(recorded_runs, run_cli, project):
    out = project.root / "shareable.json"

    run_cli("runs", "export", "cccc55556666", "-o", out, "--anonymize")

    text = out.read_text(encoding="utf-8")
    assert "ada.lovelace@example.org" not in text
    assert FAKE_GITHUB_TOKEN not in text
    assert "sk-CHECKPOINTFAKE00000000" not in text
    assert "user@example.com" in text
    assert "ghp-REDACTED" in text and "sk-REDACTED" in text


def test_anonymizing_keeps_the_record_readable(recorded_runs, run_cli, project):
    """Redaction that flattened the trace would make the export useless."""
    out = project.root / "shareable.json"

    run_cli("runs", "export", "aaaa11112222", "-o", out, "--anonymize")

    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["trace"] == TRACE
    assert [c["text"] for c in record["criteria"]] == [ISSUED, NOT_DELETED]
