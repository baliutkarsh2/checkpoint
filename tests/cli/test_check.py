"""What `checkpoint check` tells an author before they spend runs on a scenario.

The point of the command is that it shows the *assertion* each criterion
compiles to, so "the refund went through" can be seen to read the twin's state
rather than the agent's account of itself. The other half is the error/warning
split: something that would make a run meaningless is an error and exits 1,
something that is merely ignored is a warning and does not.
"""
from __future__ import annotations

import json

# -- how each criterion will be decided ----------------------------------------


def test_check_prints_the_assertion_behind_a_criterion(project, run_cli):
    result = run_cli("check", "scenarios/starter.md")

    assert result.exit_code == 0, result.output
    assert "count(created.github.issues) == 1" in result.output
    assert "pattern" in result.output


def test_a_pinned_assertion_is_used_as_written(project, run_cli):
    project.write_scenario(
        "pinned",
        criteria=('[D] The issue is still open => github.issues[state == "open"]',))

    result = run_cli("check", "scenarios/pinned.md", "--json")

    criterion = json.loads(result.stdout)[0]["criteria"][0]
    assert criterion["assertion"] == 'github.issues[state == "open"]'
    assert criterion["source"] == "pinned"


def test_a_judged_criterion_says_a_model_will_read_the_answer(project, run_cli):
    project.write_scenario("judged", criteria=("[P] The final answer quotes the issue number",))

    result = run_cli("check", "scenarios/judged.md")

    assert result.exit_code == 0, result.output
    assert "the judge model reads the final answer" in result.output


def test_a_criterion_with_no_deterministic_check_is_warned_about(project, run_cli):
    """It still runs, but it costs a model call and can decide differently twice."""
    project.write_scenario(
        "vague", criteria=("[D] The agent behaved unrecognisably well xyzzy",))

    result = run_cli("check", "scenarios/vague.md")

    assert result.exit_code == 0, result.output
    assert "no deterministic check" in result.output


# -- errors and warnings -------------------------------------------------------


def test_an_unknown_twin_is_an_error_and_exits_1(project, run_cli):
    """A scenario naming a service that cannot start would run against nothing."""
    project.write_scenario("wrong-twin", twins=("salesforce",))

    result = run_cli("check", "scenarios/wrong-twin.md")

    assert result.exit_code == 1
    assert "error" in result.output
    assert "salesforce" in result.output


def test_a_scenario_with_no_task_is_an_error(project, run_cli):
    (project.scenarios / "taskless.md").write_text(
        "# Taskless\n\n## Criteria\n\n- [D] Exactly 1 issue was created\n", encoding="utf-8")

    result = run_cli("check", "scenarios/taskless.md")

    assert result.exit_code == 1
    assert "no task" in result.output


def test_an_unknown_setting_is_a_warning_and_exits_0(project, run_cli):
    """It is ignored rather than fatal, but silence would hide the typo."""
    project.write_scenario("odd-setting", settings=("bananas: 3",))

    result = run_cli("check", "scenarios/odd-setting.md")

    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    assert "bananas" in result.output


def test_one_broken_scenario_fails_the_whole_check(project, run_cli):
    project.write_scenario("wrong-twin", twins=("salesforce",))

    result = run_cli("check")

    assert result.exit_code == 1
    assert "Starter" in result.output  # the good one is still reported


# -- workspaces ----------------------------------------------------------------


def test_file_criteria_compile_without_a_model(project, run_cli):
    """A workspace scenario must be checkable before anything is copied or run."""
    (project.root / "fixtures" / "repo" / "src").mkdir(parents=True)
    (project.root / "fixtures" / "repo" / "src" / "app.py").write_text("x = 1\n",
                                                                       encoding="utf-8")
    project.write_scenario(
        "coding", twins=(), settings=("workspace: ../fixtures/repo",),
        criteria=("[D] Exactly 1 file was created",
                  "[D] No files were deleted",
                  "[D] src/app.py was changed",
                  '[D] A file named "CHANGELOG.md" exists'))

    result = run_cli("check", "scenarios/coding.md", "--json")

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)[0]
    assert [c["assertion"] for c in report["criteria"]] == [
        "count(created.workspace.files) == 1",
        "count(deleted.workspace.files) == 0",
        'exists(changed.workspace.files[path == "src/app.py"])',
        'exists(workspace.files[path == "CHANGELOG.md"])',
    ]
    assert {c["source"] for c in report["criteria"]} == {"pattern"}, "no model was needed"


def test_a_workspace_that_does_not_exist_is_an_error_and_exits_1(project, run_cli):
    """Reported like any other setup mistake, rather than run and scored zero."""
    project.write_scenario("missing-tree", settings=("workspace: fixtures/nope",))

    result = run_cli("check", "scenarios/missing-tree.md")

    assert result.exit_code == 1
    assert "workspace directory not found" in result.output


def test_workspace_is_a_known_setting(project, run_cli):
    (project.root / "tree").mkdir()
    project.write_scenario("with-tree", settings=("workspace: ../tree",))

    result = run_cli("check", "scenarios/with-tree.md")

    assert result.exit_code == 0, result.output
    assert "unknown setting" not in result.output


# -- what counts as a scenario -------------------------------------------------


def test_ordinary_markdown_beside_the_scenarios_is_ignored(project, run_cli):
    (project.scenarios / "README.md").write_text(
        "# How we write scenarios\n\nSome prose.\n", encoding="utf-8")

    result = run_cli("check")

    assert result.exit_code == 0, result.output
    assert "README" not in result.output


def test_a_directory_with_no_scenario_in_it_says_so(project, run_cli, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "README.md").write_text("# notes\n", encoding="utf-8")

    result = run_cli("check", notes)

    assert result.exit_code == 2
    assert "no file under those paths is a scenario" in result.output


# -- output shape --------------------------------------------------------------


def test_json_is_one_document_describing_every_criterion(project, run_cli):
    result = run_cli("check", "scenarios/starter.md", "--json")

    reports = json.loads(result.stdout)
    assert len(reports) == 1
    report = reports[0]
    assert report["valid"] is True
    assert report["twins"] == ["github"]
    assert report["errors"] == []
    assert [c["text"] for c in report["criteria"]] == ["Exactly 1 issue was created"]
    assert all({"kind", "must_pass", "assertion", "source"} <= set(c) for c in report["criteria"])


def test_checking_several_scenarios_reports_each_one(project, run_cli):
    project.write_scenario("second")

    reports = json.loads(run_cli("check", "--json").stdout)

    assert {r["title"] for r in reports} == {"Starter", "Second"}
