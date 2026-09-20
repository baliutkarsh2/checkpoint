"""What `checkpoint run` does with the flags it is given, and what it exits with.

The exit code is the contract CI and shell scripts read: 0 every criterion
held, 1 one of them did not, 2 the run could not be set up or scored. The rest
of this file pins the flags that decide *which* runs happen and *how* — a flag
that parses but never reaches the run is the failure mode worth guarding
against, so most tests assert on what the engine was actually asked to do.
"""
from __future__ import annotations

import json

# -- choosing what to run ------------------------------------------------------


def test_run_with_no_target_runs_every_scenario_in_the_project(project, run_cli, stub_runs):
    project.write_scenario("second")

    result = run_cli("run")

    assert result.exit_code == 0, result.output
    assert sorted(stub_runs.scenarios_run) == ["Second", "Starter"]


def test_a_tag_selects_which_scenarios_run(project, run_cli, stub_runs):
    project.write_scenario("nightly", tags=("regression",))

    result = run_cli("run", "--tag", "smoke")

    assert result.exit_code == 0, result.output
    assert stub_runs.scenarios_run == ["Starter"]


def test_a_tag_that_matches_nothing_is_an_error_not_a_silent_pass(project, run_cli, stub_runs):
    """Exiting 0 here would report a green build from zero scenarios."""
    result = run_cli("run", "--tag", "payments")

    assert result.exit_code == 2
    assert "payments" in result.output
    assert stub_runs.calls == []


def test_a_named_scenario_that_does_not_exist_is_an_error(project, run_cli, stub_runs):
    result = run_cli("run", "scenarios/absent.md")

    assert result.exit_code == 2
    assert "no such scenario" in result.output
    assert stub_runs.calls == []


def test_n_runs_each_scenario_that_many_times(project, run_cli, stub_runs):
    result = run_cli("run", "-n", "3")

    assert result.exit_code == 0, result.output
    assert stub_runs.scenarios_run == ["Starter"] * 3


def test_task_runs_what_was_typed_instead_of_any_file(project, run_cli, stub_runs):
    run_cli("run", "--task", "Close the oldest issue", "--twins", "slack")

    assert len(stub_runs.calls) == 1
    scenario = stub_runs.calls[0].scenario
    assert scenario.prompt == "Close the oldest issue"
    assert scenario.twins == ["slack"]
    assert scenario.source_path is None


# -- the state each run starts from --------------------------------------------


def test_seed_overrides_the_dataset_the_scenario_asked_for(project, run_cli, stub_runs):
    project.write_scenario("seeded", settings=("seed: small-project",))

    run_cli("run", "scenarios/seeded.md", "--seed", "large-backlog")

    assert stub_runs.calls[0].scenario.config["seed"] == "large-backlog"


def test_keep_state_drops_the_seed_so_the_twins_carry_on(project, run_cli, stub_runs):
    project.write_scenario("seeded", settings=("seed: small-project",))

    run_cli("run", "scenarios/seeded.md", "--keep-state")

    config = stub_runs.calls[0].scenario.config
    assert "seed" not in config
    assert "seed-file" not in config


# -- the agent and the sandbox -------------------------------------------------


def test_the_agent_comes_from_the_project_when_no_flag_overrides_it(project, run_cli, stub_runs):
    run_cli("run")

    assert stub_runs.calls[0].agent.command.endswith("agent.py")


def test_command_overrides_the_project_agent(project, run_cli, stub_runs):
    run_cli("run", "--command", "python other_agent.py")

    assert stub_runs.calls[0].agent.command == "python other_agent.py"


def test_the_sandbox_flags_reach_the_run_options(project, run_cli, stub_runs):
    """A sandbox flag that parses but never reaches the run is a silent lie."""
    run_cli("run", "--read-only", "--rate-limit", "5", "--egress", "none",
            "--allow-host", "api.example.com", "--timeout", "12")

    options = stub_runs.calls[0].options
    assert options.read_only is True
    assert options.faults == {"*": {"rate_limit": 5}}
    assert options.egress == "none"
    assert options.allow_hosts == ("api.example.com",)
    assert options.timeout == 12


def test_the_project_supplies_the_sandbox_settings_and_the_judge(project, run_cli, stub_runs):
    run_cli("run")

    options = stub_runs.calls[0].options
    assert options.egress == "llm"
    assert options.intercept is True
    assert options.judge_model == "stub-judge"
    assert options.faults == {}
    assert options.read_only is False


def test_with_no_project_and_no_command_run_names_both_ways_to_fix_it(
    tmp_path, monkeypatch, run_cli
):
    monkeypatch.chdir(tmp_path)

    result = run_cli("run")

    assert result.exit_code == 2
    assert 'checkpoint init --command "python my_agent.py"' in result.output
    assert "pass it for this command only" in result.output


# -- what it exits with --------------------------------------------------------


def test_a_failed_criterion_exits_1(project, run_cli, stub_runs):
    stub_runs.verdict = "fail"

    result = run_cli("run")

    assert result.exit_code == 1


def test_a_run_that_could_not_be_scored_exits_2(project, run_cli, stub_runs):
    """Unscoreable is not "failed": nobody should read it as a verdict."""
    stub_runs.verdict = "unscoreable"

    result = run_cli("run")

    assert result.exit_code == 2


# -- what it prints and records ------------------------------------------------


def test_json_puts_exactly_one_document_on_stdout(project, run_cli, stub_runs):
    project.write_scenario("second")

    result = run_cli("run", "--json")

    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["runs"] == 2
    assert summary["passed"] == 2
    assert summary["unscored"] == 0
    assert {s["scenario"] for s in summary["scenarios"]} == {"Starter", "Second"}


def test_json_reports_a_run_that_could_not_be_scored(project, run_cli, stub_runs):
    stub_runs.verdict = "unscoreable"

    result = run_cli("run", "--json")

    assert json.loads(result.stdout)["unscored"] == 1


def test_trace_out_writes_one_entry_per_run(project, run_cli, stub_runs):
    trace = project.root / "trace.json"

    run_cli("run", "-n", "2", "--trace-out", trace)

    dumped = json.loads(trace.read_text(encoding="utf-8"))
    assert len(dumped) == 2
    assert all(entry["score"] == 100.0 for entry in dumped)
    assert all(entry["criteria"][0]["text"] == "Exactly 1 issue was created" for entry in dumped)


def test_every_run_leaves_a_record_for_checkpoint_runs_to_find(project, run_cli, stub_runs):
    run_cli("run")

    records = list((project.root / ".checkpoint" / "cache" / "runs").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["scenario"] == "Starter"
    assert record["satisfaction"] == 100.0


def test_an_inline_task_is_watched_not_graded(run_cli, project):
    """`--task` exists to see what an agent does; there is nothing to score.

    Scoring a criteria-less run against an empty list produced 0/100 and exit 1,
    so a perfectly good look at an agent reported failure to the shell.
    """
    result = run_cli("run", "--task", "File something", "--twins", "github")
    assert result.exit_code == 0, result.output
    assert "0/100" not in result.output
    assert "nothing scored" in result.output
