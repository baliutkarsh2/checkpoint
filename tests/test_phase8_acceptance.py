"""Phase 8 / Plan 05: end-to-end acceptance gates (QA-01 + QA-03).

QA-01: Multi-clone demo scenario across all 3 twins still scores green
       end-to-end through the runner.

QA-03: A scenario in Archal's verbatim documentation shape (Title /
       Setup / Prompt / Success Criteria with [D] + [P] mix / Config)
       runs against the github twin and scores >= 80.

Both tests drive the real runner (spinning up actual twin processes) but
mock the OpenAI judge so [P] criteria don't require an API key.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from checkpoint.runner import run_once
from checkpoint.scenario import parse_file


REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "scenarios"
EXAMPLE_DIR = REPO_ROOT / "examples" / "multi-clone"


# --- Minimal OpenAI fake (same shape as test_phase5_acceptance.py) ----------

@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Resp:
    choices: list


class _AlwaysPassCompletions:
    """Returns 'pass' for every criterion in every batch judge call."""

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        # Find the criteria list in the prompt; reply with one passing
        # verdict per criterion in the same order.
        user_msg = ""
        for m in kw.get("messages", []):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        # The judge prompt lists criteria as numbered/bulleted lines. We
        # don't need to parse them — just reply with a single batch that
        # passes everything; the judge does positional fallback alignment
        # if exact-text alignment misses.
        # Build a generic 10-slot results list. Each entry passes.
        results = [
            {"criterion": f"criterion-{i}", "passed": True,
             "reasoning": "Synthetic acceptance: assumed pass."}
            for i in range(10)
        ]
        # Stage-2 [D] LLM-JSON path expects a single JSON object; we return
        # one that won't match any real resource so callers fall through
        # to the [P] judge. Distinguish by inspecting the prompt content.
        if "assertion" in user_msg.lower() or "resource" in user_msg.lower():
            content = json.dumps({
                "resource": "unknown_for_fall_through",
                "selector": None,
                "operator": "exists",
                "value": None,
            })
        else:
            content = json.dumps({"results": results})
        return _Resp(choices=[_Choice(message=_Msg(content=content))])


class _AlwaysPassChat:
    def __init__(self):
        self.completions = _AlwaysPassCompletions()


class FakeOpenAIAlwaysPass:
    def __init__(self):
        self.chat = _AlwaysPassChat()


@pytest.fixture
def mock_openai(monkeypatch):
    """Stub the OpenAI client used by both checker_llm and judge."""
    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", lambda: FakeOpenAIAlwaysPass())
    # Also set a fake key so any guard that checks for one is happy.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-stub")
    yield


# --- QA-01: Multi-clone demo end-to-end --------------------------------------

def test_qa01_multi_clone_demo_runs_green(mock_openai):
    """Run the existing multi-clone demo via the real runner; assert green."""
    scenario_path = EXAMPLE_DIR / "scenarios" / "multi-clone-demo.md"
    harness_path = EXAMPLE_DIR / "multi_clone_harness.py"
    assert scenario_path.is_file(), f"missing: {scenario_path}"
    assert harness_path.is_file(), f"missing: {harness_path}"

    scenario = parse_file(scenario_path)
    # Force a short timeout to keep the test snappy.
    scenario.config["timeout"] = "60"

    result = run_once(
        scenario,
        [sys.executable, str(harness_path)],
        judge_model="gpt-4o-mini",
    )
    assert result.error is None, f"runner error: {result.error}\n{result.stderr[-500:]}"
    assert result.complete, f"run incomplete; stderr tail: {result.stderr[-500:]}"
    # Score floor: at least 80 (allow small drift on [D] regex misses).
    assert result.score >= 80.0, (
        f"multi-clone demo scored {result.score}, expected >= 80\n"
        f"criteria: {[(c.text, c.passed, c.evaluator) for c in result.criteria]}"
    )


# --- QA-03: Archal-verbatim scenario shape -----------------------------------

ARCHAL_HARNESS_SRC = '''
"""Tiny harness driving the Archal-verbatim PR scenario.

Creates the PR, then PATCHes it to embed the label + reviewer markers in
the body (the twin doesn't model labels/reviewers as separate fields
beyond what's stored in the body). Returns a JSON final answer that
references the PR number.
"""
import json
import os
import sys
import requests

from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN

GH = os.environ["CHECKPOINT_GITHUB_URL"]
TOKEN = os.environ.get("GITHUB_TOKEN", FAKE_GITHUB_TOKEN)
HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "phase8-archal-verbatim/0.1",
}

# 1. Open the PR.
r = requests.post(
    f"{GH}/repos/acme/webapp/pulls",
    json={
        "title": "Fix login bug",
        "head": "fix-login-bug",
        "base": "main",
        "body": "Labels: bug\\nRequested reviewers: reviewer1",
    },
    headers=HEADERS,
    timeout=15,
)
r.raise_for_status()
pr = r.json()
num = pr["number"]
print(f"[archal-harness] created PR #{num}", file=sys.stderr)

# 2. Apply the `bug` label and request reviewer1 via the real endpoints.
requests.post(
    f"{GH}/repos/acme/webapp/issues/{num}/labels",
    json={"labels": ["bug"]}, headers=HEADERS, timeout=15,
).raise_for_status()
requests.post(
    f"{GH}/repos/acme/webapp/pulls/{num}/requested_reviewers",
    json={"reviewers": ["reviewer1"]}, headers=HEADERS, timeout=15,
).raise_for_status()

# 3. Confirm via final-answer JSON.
print(json.dumps({
    "text": f"Opened pull request #{pr['number']} titled 'Fix login bug' "
            f"with the 'bug' label and review requested from reviewer1.",
}))
'''


def test_qa03_archal_verbatim_scenario_scores_green(mock_openai, tmp_path):
    """The Archal-verbatim scenario runs against the github twin and scores >= 80."""
    scenario_path = SCENARIOS_DIR / "archal-verbatim-github.md"
    assert scenario_path.is_file(), f"missing: {scenario_path}"

    harness = tmp_path / "harness.py"
    harness.write_text(ARCHAL_HARNESS_SRC)

    scenario = parse_file(scenario_path)
    scenario.config["timeout"] = "60"

    result = run_once(
        scenario,
        [sys.executable, str(harness)],
        judge_model="gpt-4o-mini",
    )
    assert result.error is None, f"runner error: {result.error}\n{result.stderr[-500:]}"
    assert result.complete, f"run incomplete; stderr tail: {result.stderr[-500:]}"
    assert result.score >= 80.0, (
        f"archal-verbatim scored {result.score}, expected >= 80\n"
        f"criteria: {[(c.text, c.passed, c.evaluator) for c in result.criteria]}"
    )


# --- Sanity: scenario library + bundled demo still parse coherently ---------

def test_qa01_demo_scenario_still_present() -> None:
    """Regression check: the multi-clone demo scenario hasn't been deleted."""
    p = EXAMPLE_DIR / "scenarios" / "multi-clone-demo.md"
    assert p.is_file()
    scn = parse_file(p)
    assert set(scn.clones) == {"github", "slack", "stripe"}


def test_qa03_verbatim_scenario_has_archal_shape() -> None:
    """The verbatim scenario must use Archal's exact section layout."""
    text = (SCENARIOS_DIR / "archal-verbatim-github.md").read_text()
    for section in ("# ", "## Setup", "## Prompt", "## Success Criteria", "## Config"):
        assert section in text, f"missing section: {section}"
    # Must have at least one [D] and one [P] criterion (Archal style).
    assert "[D]" in text
    assert "[P]" in text
