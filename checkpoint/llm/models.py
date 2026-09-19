"""The default model, in one place.

Every LLM use in Checkpoint — the judge, the criterion parser, the scenario and
red-team generators, the failure analyser, the simulated user — falls back to
this id when the user does not pass one. It lives alone in this module so that
"what does Checkpoint call by default" has exactly one answer to change.

``gpt-5.6-luna`` is OpenAI's cost-optimised current model ($0.20/$1.20 per MTok
against $4/$20 for the flagship ``gpt-5.6-sol``), and it supports strict
structured outputs on Chat Completions — which is what makes a verdict
attributable to the criterion that produced it. A release gate runs the judge
once per criterion per repeat, so the cheap model is the one that can be run
often enough to give the gate a distribution; users who want more judgment can
pass ``--judge-model gpt-5.6-sol`` or ``--judge-model claude-sonnet-5``.

Model ids verified against https://developers.openai.com/api/docs/models and
https://platform.claude.com/docs/en/about-claude/models/overview.
"""
from __future__ import annotations

DEFAULT_MODEL = "gpt-5.6-luna"

# Shown in the "no credential" error as a model from another provider, so the
# suggestion is a working command rather than a placeholder.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
