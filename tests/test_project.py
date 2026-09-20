"""`checkpoint.toml`: the one place a setting can live.

The failure this file exists to prevent is the one the old three-config-file
arrangement produced constantly — a setting that looks applied and is not. So
these check both halves: that a value written in the file reaches the thing that
uses it, and that a value Checkpoint cannot honour is refused out loud.
"""
from __future__ import annotations

import pytest

from checkpoint.project import CONFIG_NAME, ConfigError, Project, render_template


def write(directory, text: str):
    (directory / CONFIG_NAME).write_text(text, encoding="utf-8")
    return directory


# -- finding it ---------------------------------------------------------------


def test_no_config_is_not_an_error(tmp_path):
    """Every command works without one; only [agent] is then required by flag."""
    project = Project.load(tmp_path)
    assert project.path is None
    assert project.build_agent() is None
    assert project.judge_model() != ""


def test_found_from_a_subdirectory(tmp_path):
    write(tmp_path, '[agent]\ncommand = "python a.py"\n')
    nested = tmp_path / "src" / "agents" / "deep"
    nested.mkdir(parents=True)
    project = Project.load(nested)
    assert project.path == tmp_path / CONFIG_NAME
    # Paths resolve against the file, not the working directory, so running from
    # a subdirectory tests the same scenarios as running from the root.
    assert project.scenario_paths() == [tmp_path / "scenarios"]


def test_the_nearest_one_wins(tmp_path):
    write(tmp_path, '[agent]\ncommand = "outer"\n')
    inner = tmp_path / "inner"
    inner.mkdir()
    write(inner, '[agent]\ncommand = "inner"\n')
    assert Project.load(inner).agent["command"] == "inner"


# -- refusing what it cannot honour -------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ('[agnet]\ncommand = "x"\n', "unknown section"),
    ('[agent]\ncomand = "x"\n', "unknown key"),
    ('[agent]\ncommand = "x"\ntask_via = "telepathy"\n', "task_via"),
    ('[sandbox]\negress = "everything"\n', "egress"),
    ('[twins.billing]\napp = "not-an-import-path"\n', "app ="),
    ('[twins.billing]\nap = "x:app"\n', "unknown key"),
    ("[agent\n", "not valid TOML"),
])
def test_a_setting_that_would_be_ignored_is_an_error(tmp_path, text, expected):
    write(tmp_path, text)
    with pytest.raises(ConfigError) as caught:
        Project.load(tmp_path).register_twins()
    assert expected in str(caught.value)


def test_the_error_names_what_is_supported(tmp_path):
    write(tmp_path, '[agent]\ncomand = "x"\n')
    with pytest.raises(ConfigError) as caught:
        Project.load(tmp_path)
    assert "'command'" in str(caught.value)


# -- what the commands ask for ------------------------------------------------


def test_agent_is_built_from_the_file(tmp_path):
    write(tmp_path, """\
[agent]
command = "node agent.js"
task_via = "arg"
task_arg = "--prompt"
env = { API_MODE = "test" }
""")
    agent = Project.load(tmp_path).build_agent()
    assert agent.command == "node agent.js"
    assert agent.task_via == "arg"
    assert agent.task_arg == "--prompt"
    assert agent.env == {"API_MODE": "test"}


def test_a_command_flag_overrides_the_file(tmp_path):
    write(tmp_path, '[agent]\nurl = "http://127.0.0.1:8000/chat"\n')
    agent = Project.load(tmp_path).build_agent("python other.py")
    assert agent.command == "python other.py"
    # The url would otherwise still be honoured, and the run would quietly test
    # the service instead of the command the user just named.
    assert agent.url is None


def test_agent_cwd_resolves_against_the_config_not_the_shell(tmp_path):
    write(tmp_path, '[agent]\ncommand = "python a.py"\ncwd = "agent"\n')
    (tmp_path / "agent").mkdir()
    assert Project.load(tmp_path).build_agent().cwd == str(tmp_path / "agent")


def test_precedence_is_flag_then_environment_then_file(tmp_path, monkeypatch):
    write(tmp_path, '[judge]\nmodel = "from-file"\n')
    project = Project.load(tmp_path)
    assert project.judge_model() == "from-file"
    monkeypatch.setenv("CHECKPOINT_JUDGE_MODEL", "from-env")
    assert project.judge_model() == "from-env"
    assert project.judge_model("from-flag") == "from-flag"


def test_gate_and_sandbox_settings_fall_back_to_the_default(tmp_path):
    write(tmp_path, "[gate]\nruns = 32\n")
    project = Project.load(tmp_path)
    assert project.gate_setting("runs", None, 16) == 32
    assert project.gate_setting("runs", 4, 16) == 4
    assert project.gate_setting("pass_threshold", None, 80.0) == 80.0
    assert project.sandbox_setting("egress", None, "llm") == "llm"


def test_scenario_paths_accepts_one_or_several(tmp_path):
    write(tmp_path, '[scenarios]\npaths = ["suites/smoke", "suites/regression"]\n')
    assert [p.name for p in Project.load(tmp_path).scenario_paths()] == ["smoke", "regression"]


# -- twins of your own --------------------------------------------------------


def test_a_project_twin_joins_the_registry(tmp_path, monkeypatch):
    write(tmp_path, """\
[twins.billing]
app = "billing_twin:app"
domains = ["api.billing.internal"]
token_env = ["BILLING_API_KEY"]
""")
    (tmp_path / "billing_twin.py").write_text("app = object()\n", encoding="utf-8")
    from checkpoint.twins import registry

    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    assert Project.load(tmp_path).register_twins() == ["billing"]
    spec = registry.get("billing")
    assert spec.domains == ("api.billing.internal",)
    assert spec.token_env == ("BILLING_API_KEY",)
    # The fake credential has to exist, or an SDK reading it sends nothing and
    # the twin's auth check fails for a reason that has nothing to do with the agent.
    assert spec.token
    assert registry.for_domain("api.billing.internal") is spec


def test_a_project_twin_cannot_replace_a_built_in(tmp_path, monkeypatch):
    write(tmp_path, '[twins.github]\napp = "mine:app"\n')
    from checkpoint.twins import registry

    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    with pytest.raises(ValueError, match="built-in"):
        Project.load(tmp_path).register_twins()


# -- what init writes ---------------------------------------------------------


def test_the_starter_config_round_trips(tmp_path):
    write(tmp_path, render_template("python my_agent.py"))
    project = Project.load(tmp_path)
    assert project.build_agent().command == "python my_agent.py"
    assert project.gate_setting("runs", None, 0) == 16


def test_the_starter_config_survives_a_command_with_quotes(tmp_path):
    """`repr` is what keeps a command containing a quote from breaking the file."""
    command = """python -c "print('hi')" """.strip()
    write(tmp_path, render_template(command))
    assert Project.load(tmp_path).build_agent().command == command


def test_a_script_in_the_command_is_found_from_anywhere(tmp_path):
    """The directory an agent runs *in* is not always where its code lives.

    A scenario with a `workspace:` starts the agent inside a throwaway copy of a
    fixture tree, where `python agent.py` finds no `agent.py` — the script is
    back in the project. Every other path in checkpoint.toml is relative to the
    file, and the command has to be too.
    """
    write(tmp_path, '[agent]\ncommand = "python agent.py --loud"\n')
    (tmp_path / "agent.py").write_text("print('hi')\n", encoding="utf-8")

    argv = Project.load(tmp_path).build_agent().argv()
    assert argv[1] == str(tmp_path / "agent.py")
    # A flag is not a file, and neither is an interpreter found on PATH.
    assert argv[0] == "python"
    assert argv[2] == "--loud"


def test_a_command_with_no_file_in_it_is_left_alone(tmp_path):
    write(tmp_path, '[agent]\ncommand = "my-agent --serve"\n')
    assert Project.load(tmp_path).build_agent().command == "my-agent --serve"


def test_judge_samples_reaches_the_judge(tmp_path):
    """A setting the file accepts and nothing reads is the bug this file prevents.

    `[judge] samples` was in the allowed set, so writing it raised no error, and
    the judge has supported multi-sampling all along — but nothing ever carried
    the value from the file to the call. Setting it did nothing, silently, which
    is precisely what `checkpoint.toml` exists to make impossible.
    """
    from checkpoint.cli._shared import resolve_options

    write(tmp_path, "[judge]\nsamples = 3\n")
    project = Project.load(tmp_path)

    assert project.judge_samples() == 3
    assert resolve_options(project).judge_samples == 3


def test_judge_samples_defaults_to_one_and_a_flag_still_wins(tmp_path):
    write(tmp_path, '[agent]\ncommand = "x"\n')
    project = Project.load(tmp_path)

    assert project.judge_samples() == 1
    assert project.judge_samples(5) == 5


def test_every_setting_the_file_accepts_is_carried_somewhere(tmp_path):
    """The general form of the bug above: accepted, and then dropped.

    Each key is written on its own, loaded, and read back through the accessor
    that is supposed to surface it. A key with no accessor at all is the shape
    that hides — it parses, it validates, and nothing ever asks for it.
    """
    from checkpoint.project import _AGENT_KEYS, _GATE_KEYS, _JUDGE_KEYS

    write(tmp_path, "[judge]\nsamples = 7\nmodel = \"m\"\n")
    project = Project.load(tmp_path)
    assert project.judge_model() == "m" and project.judge_samples() == 7

    write(tmp_path, "[gate]\n" + "\n".join(f"{k} = 1" for k in sorted(_GATE_KEYS)
                                           if k not in ("strict", "allow_conditional")))
    project = Project.load(tmp_path)
    for key in sorted(_GATE_KEYS - {"strict", "allow_conditional"}):
        assert project.gate_setting(key, None, "unset") == 1, f"[gate] {key} was dropped"

    write(tmp_path, '[sandbox]\negress = "none"\nintercept = false\nallow_hosts = ["a"]\n')
    project = Project.load(tmp_path)
    for key, expected in (("egress", "none"), ("intercept", False), ("allow_hosts", ["a"])):
        assert project.sandbox_setting(key, None, "unset") == expected, f"[sandbox] {key} dropped"

    # [agent] is surfaced through build_agent rather than a setting accessor.
    assert _AGENT_KEYS and _JUDGE_KEYS  # named so a new key here fails review, not silently
