

# --- a setting that parses has to be a setting something reads --------------

def test_every_known_setting_is_read_by_something():
    """The bug this file keeps catching: accepted by validation, read by nobody.

    `checkpoint check` reports an unknown setting, which means a *known* one
    looks supported. Three were not: `judge-model` had a property nothing
    called, `goal` was never consulted by the simulated user, and `seed_file`
    was a second spelling only one reader knew. Each parsed, validated, and did
    nothing.

    This asks the weaker but mechanical question — does the name appear
    anywhere it could be read? — so a setting added to KNOWN_SETTINGS without a
    reader fails here rather than in someone's scenario.
    """
    from pathlib import Path

    from checkpoint.scenario import KNOWN_SETTINGS

    source = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (Path(__file__).resolve().parent.parent / "checkpoint").rglob("*.py")
        if p.name != "scenario.py"  # where they are declared, not where they are used
    )
    def is_read(setting: str) -> bool:
        # Either by key — cfg.get("goal") — or through the property named after
        # it, which is how `judge-model` reaches the engine.
        return (f'"{setting}"' in source or f"'{setting}'" in source
                or f".{setting.replace('-', '_')}" in source)

    unread = sorted(s for s in KNOWN_SETTINGS if not is_read(s))
    assert not unread, (
        f"these scenario settings validate but nothing outside scenario.py reads "
        f"them, so writing one does nothing: {unread}")


def test_both_spellings_of_a_setting_reach_the_same_reader():
    """`seed_file` and `seed-file` are one setting, not two."""
    from checkpoint.scenario import parse

    body = "# t\n\n## Task\nx\n\n## Criteria\n- [D] y\n"
    hyphen = parse(f"---\nseed-file: ./a.json\njudge-model: m\n---\n{body}")
    under = parse(f"---\nseed_file: ./a.json\njudge_model: m\n---\n{body}")

    assert hyphen.config.get("seed-file") == under.config.get("seed-file") == "./a.json"
    assert hyphen.judge_model == under.judge_model == "m"
    assert not hyphen.problems and not under.problems


def test_a_scenario_can_pin_the_judge_it_needs():
    """Documented as overriding the project setting, and it did not."""
    from checkpoint.engine import RunOptions
    from checkpoint.scenario import parse

    scenario = parse("---\njudge-model: pinned-model\n---\n# t\n\n## Task\nx\n\n"
                     "## Criteria\n- [P] y\n")
    assert scenario.judge_model == "pinned-model"

    # The engine prefers it over the resolved default, unless one was named.
    opts = RunOptions(judge_model="project-default")
    chosen = opts.judge_model if opts.judge_model_pinned else (
        scenario.judge_model or opts.judge_model)
    assert chosen == "pinned-model"

    pinned = RunOptions(judge_model="from-flag", judge_model_pinned=True)
    chosen = pinned.judge_model if pinned.judge_model_pinned else (
        scenario.judge_model or pinned.judge_model)
    assert chosen == "from-flag", "an explicit --model must still win"
