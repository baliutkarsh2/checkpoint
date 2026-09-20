"""Drafting a scenario: what `generate` promises about the Markdown it returns.

Every test drives the generator with a canned model response, so what is under
test is the validation around the call, not the model.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from checkpoint.eval.expr import parse as parse_assertion
from checkpoint.scenario import parse as parse_scenario
from checkpoint.scenario_gen import ScenarioGenError, generate
from checkpoint.twins import registry


class FakeClient:
    """OpenAI-shaped client: replays canned responses and records every call."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses) or [""]
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        content = self._responses[index]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    def message(self, role: str, call: int = 0) -> str:
        return next(m["content"] for m in self.calls[call]["messages"] if m["role"] == role)


CURRENT = """\
---
twins: [github]
seed: small-project
timeout: 120
tags: [github]
---
# File the login bug

## Setup

The `small-project` seed: an `acme/webapp` repository with two open issues.

## Task

Create an issue in `acme/webapp` titled "Login broken" describing the failure,
and quote its number in your final answer.

## Criteria

- [D] Exactly 1 issue was created  => count(created.github.issues) == 1
- [D!] No issues were deleted  => count(deleted.github.issues) == 0
- [T] The agent made at most 10 calls  => count(trace) <= 10
- [P] The final answer quotes the number of the issue it created
"""

OLD_FORMAT = """\
# Test Scenario

## Setup

A GitHub repository with a couple of open issues.

## Prompt

Create an issue titled "Test issue" on acme/webapp.

## Success Criteria

- [D] An issue titled "Test issue" exists
- [P] The final answer mentions the issue number

## Config

clones: github
seed: small-project
runs: 1
"""

NO_CRITERIA = """\
---
twins: [github]
seed: small-project
---
# File the login bug

## Task

Create an issue in `acme/webapp` titled "Login broken".
"""

BROKEN_ASSERTION = CURRENT.replace(
    "- [D] Exactly 1 issue was created  => count(created.github.issues) == 1",
    "- [D] Exactly 1 issue was created  => count(created.github.issues == 1",
)


# -- a usable draft -----------------------------------------------------------


def test_a_generated_scenario_is_runnable():
    client = FakeClient(CURRENT)
    text = generate("file the login bug", twins=["github"], client=client)

    scenario = parse_scenario(text)
    assert scenario.problems == []
    assert scenario.prompt
    assert len(scenario.criteria) >= 3
    assert scenario.twins == ["github"]
    assert all(name in registry.names() for name in scenario.twins)
    assert text.endswith("\n")


def test_every_pinned_assertion_in_the_output_parses():
    client = FakeClient(CURRENT)
    text = generate("file the login bug", twins=["github"], client=client)

    pinned = [c.assertion for c in parse_scenario(text).criteria if c.assertion]
    assert pinned, "the draft should keep its pinned assertions"
    for assertion in pinned:
        parse_assertion(assertion)  # raises ExprSyntaxError if the draft went out broken


def test_the_client_factory_seam_still_works():
    client = FakeClient(CURRENT)
    generate("file the login bug", twins=["github"], _client_factory=lambda: client)
    assert len(client.calls) == 1


def test_twins_may_be_a_comma_separated_string():
    client = FakeClient(CURRENT)
    generate("file the login bug", twins="github", client=client)
    assert "github" in client.message("user")


# -- the prompt the model is given --------------------------------------------


def test_the_prompt_carries_the_real_vocabulary():
    client = FakeClient(CURRENT)
    generate("file the login bug", twins=["github"], seed="small-project", client=client)
    system = client.message("system")

    assert "github.issues" in system                      # collections that exist
    assert "created.<twin>.<collection>" in system        # the delta roots
    assert "count(list), exists(list)" in system          # the functions
    assert "small-project" in system                      # a seed that exists
    assert "stale-issues" in system
    assert "AGENT THAT DID NOTHING" in system             # the authoring rule


# -- validation ---------------------------------------------------------------


def test_the_old_format_is_retried_once_and_then_rejected():
    client = FakeClient(OLD_FORMAT)
    with pytest.raises(ScenarioGenError) as raised:
        generate("file the login bug", twins=["github"], client=client)

    assert len(client.calls) == 2
    message = str(raised.value)
    assert "## Task" in message and "## Criteria" in message
    assert "clones:" in message


def test_a_draft_without_criteria_is_retried_once_and_then_rejected():
    client = FakeClient(NO_CRITERIA)
    with pytest.raises(ScenarioGenError) as raised:
        generate("file the login bug", twins=["github"], client=client)

    assert len(client.calls) == 2
    assert "## Criteria" in str(raised.value)


def test_the_retry_feeds_the_problems_back_and_a_fixed_draft_is_accepted():
    client = FakeClient(OLD_FORMAT, CURRENT)
    text = generate("file the login bug", twins=["github"], client=client)

    assert len(client.calls) == 2
    retry = client.message("user", call=1)
    assert "'## Prompt' is the old format" in retry
    assert OLD_FORMAT.splitlines()[0] in retry  # the rejected draft goes back too
    assert parse_scenario(text).criteria


def test_front_matter_naming_another_twin_is_rejected():
    client = FakeClient(CURRENT.replace("twins: [github]", "twins: [slack]"))
    with pytest.raises(ScenarioGenError) as raised:
        generate("file the login bug", twins=["github"], client=client)
    assert "'slack'" in str(raised.value)


def test_a_seed_that_does_not_exist_is_rejected():
    client = FakeClient(CURRENT.replace("seed: small-project", "seed: no-such-seed"))
    with pytest.raises(ScenarioGenError) as raised:
        generate("file the login bug", twins=["github"], client=client)
    assert "no-such-seed" in str(raised.value)


def test_unknown_twins_raise_before_any_model_call():
    client = FakeClient(CURRENT)
    with pytest.raises(ValueError) as raised:
        generate("file the login bug", twins=["gitlab"], client=client)

    assert client.calls == []
    assert "gitlab" in str(raised.value)
    assert "github" in str(raised.value)  # the message names what is available


def test_no_twins_at_all_raises_before_any_model_call():
    client = FakeClient(CURRENT)
    with pytest.raises(ValueError):
        generate("file the login bug", twins=[], client=client)
    assert client.calls == []


# -- pinned assertions --------------------------------------------------------


def test_an_assertion_that_does_not_parse_is_dropped_not_emitted():
    client = FakeClient(BROKEN_ASSERTION)
    text = generate("file the login bug", twins=["github"], client=client)

    assert "count(created.github.issues == 1" not in text.split("<!--")[0]
    assert "- [D] Exactly 1 issue was created" in text  # the criterion survives
    assert "count(deleted.github.issues) == 0" in text  # the sound ones are untouched

    scenario = parse_scenario(text)
    assert [c.assertion for c in scenario.criteria if c.text.startswith("Exactly 1")] == [None]
    for criterion in scenario.criteria:
        if criterion.assertion:
            parse_assertion(criterion.assertion)


def test_a_dropped_assertion_is_reported_in_the_file():
    client = FakeClient(BROKEN_ASSERTION)
    text = generate("file the login bug", twins=["github"], client=client)

    note = text.split("<!--", 1)[1]
    assert "count(created.github.issues == 1" in note
    assert "removed" in note


def test_a_markdown_fence_around_the_whole_file_is_stripped():
    client = FakeClient(f"```markdown\n{CURRENT}```")
    text = generate("file the login bug", twins=["github"], client=client)
    assert text.startswith("---\n")
    assert "```" not in text
