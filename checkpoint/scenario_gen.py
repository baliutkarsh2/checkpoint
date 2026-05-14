"""LLM-backed scenario generator for `checkpoint scenario generate`."""
from __future__ import annotations

SYSTEM = """\
Generate an evaluation scenario for an AI agent benchmark.

Return ONLY a Markdown file with these sections (in order):
  # <Title>
  ## Setup        — initial twin state in plain prose
  ## Prompt       — 2–4 sentences describing the task
  ## Success Criteria  — bullet list; each item prefixed [D] or [P]
  ## Config       — key: value block

[D] criteria are checked deterministically. Use these exact phrasings:
  - "An issue titled "<title>" exists"
  - "Exactly N <resource>s exist"  /  "At least N …"  /  "At most N …"
  - "Exactly N <resource>s are <state>"
  - "All <state> <resource>s have a comment"
  - "A channel named "<name>" exists"
  - "A label named "<name>" exists"
  - "No new <resource>s"

[P] criteria need LLM judgment (agent reasoning, answer quality, etc.).

Known clones: github, slack, stripe, linear, supabase, discord, google-workspace

Config example:
  clones: github
  seed: small-project
  runs: 1
  timeout: 60
  tags: happy-path, github
"""


def _default_factory():
    from openai import OpenAI
    return OpenAI()


def generate(
    description: str,
    *,
    clone: str | None = None,
    model: str = "gpt-4o-mini",
    _client_factory=None,
) -> str:
    """Return raw Markdown for a new scenario given a prose description."""
    user_msg = description
    if clone:
        user_msg += f"\n\nUse clone(s): {clone}"

    client = (_client_factory or _default_factory)()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.7,
    )
    return (resp.choices[0].message.content or "").strip()
