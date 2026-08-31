"""LLM-backed scenario generator for `checkpoint scenario generate`."""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-twin capability map (F2).
#
# Generated [D] criteria must only reference operations the target twin can
# actually perform — otherwise the scenario demands state no agent can reach
# (e.g. "a channel named X exists" against a twin with no channel-creation
# endpoint). Each capability token names an operation class; the phrasing
# templates below are gated on these tokens.
# ---------------------------------------------------------------------------

_TWIN_CAPABILITIES: dict[str, set[str]] = {
    "github": {
        "create_issue", "close_issue", "comment", "create_label",
        "create_pull_request", "merge_pull_request", "create_branch",
        "create_repo",
    },
    # Slack twin supports conversations.create (F2), chat.postMessage,
    # reactions.add — so channel-creation criteria are now satisfiable.
    "slack": {
        "create_channel", "post_message", "add_reaction",
    },
    "stripe": {
        "create_customer", "create_product", "create_price", "create_refund",
        "create_invoice", "create_coupon", "create_payment_link",
        "cancel_subscription",
    },
    "linear": {
        "create_issue", "update_issue", "assign_issue", "create_project",
        "create_cycle", "create_label", "create_team", "comment",
    },
    "supabase": {
        "insert_row", "update_row", "delete_row", "create_bucket",
        "upload_object", "create_auth_user",
    },
    "discord": {
        "create_channel", "post_message", "add_reaction", "manage_roles",
        "create_webhook",
    },
    "google-workspace": {
        "send_email", "create_draft", "create_label", "modify_thread",
        "upload_drive_file",
    },
}

# [D] phrasing templates, each gated on a capability token. Templates whose
# capability none of the selected twins support are omitted from the system
# prompt, so the generator never emits an impossible criterion.
_D_TEMPLATES: list[tuple[str, str]] = [
    ("create_issue",        '"An issue titled "<title>" exists"'),
    ("create_pull_request", '"A pull request titled "<title>" exists"'),
    ("create_label",        '"A label named "<name>" exists"'),
    ("create_channel",      '"A channel named "<name>" exists"'),
    ("post_message",        '"At least N messages exist"'),
    ("add_reaction",        '"At least one message has a "<emoji>" reaction"'),
    ("create_refund",       '"At least N refunds exist"'),
    ("create_customer",     '"A customer named "<name>" exists"'),
    ("create_bucket",       '"At least 1 bucket exists named "<name>""'),
    ("insert_row",          '"At least N rows exist in <table>"'),
    ("create_auth_user",    '"At least N auth users exist"'),
    ("send_email",          '"At least N emails exist"'),
    ("create_draft",        '"At least N drafts exist"'),
    ("upload_drive_file",   '"At least N drive files exist"'),
    ("comment",             '"All closed issues have a comment"'),
]

# Twin-agnostic count phrasings (always available — they only inspect state).
_GENERIC_D_LINES = """\
  - "Exactly N <resource>s exist"  /  "At least N …"  /  "At most N …"
  - "Exactly N <resource>s are <state>"
  - "No new <resource>s"
"""

_KNOWN_CLONES = list(_TWIN_CAPABILITIES)


def _selected_twins(clone: str | None) -> list[str]:
    """Resolve a comma-separated clone spec to known twin names (all if None)."""
    if not clone:
        return list(_KNOWN_CLONES)
    out = []
    for c in clone.split(","):
        c = c.strip().lower()
        if c in _TWIN_CAPABILITIES:
            out.append(c)
    return out or list(_KNOWN_CLONES)


def _criteria_guidance(clone: str | None) -> str:
    """Build the [D]-phrasing block, constrained to supported operations."""
    caps: set[str] = set()
    for t in _selected_twins(clone):
        caps |= _TWIN_CAPABILITIES[t]
    lines = [_GENERIC_D_LINES.rstrip()]
    for cap, template in _D_TEMPLATES:
        if cap in caps:
            lines.append(f"  - {template}")
    return "\n".join(lines)


def _build_system(clone: str | None = None) -> str:
    return f"""\
Generate an evaluation scenario for an AI agent benchmark.

Return ONLY a Markdown file with these sections (in order):
  # <Title>
  ## Setup        — initial twin state in plain prose
  ## Prompt       — 2–4 sentences describing the task
  ## Success Criteria  — bullet list; each item prefixed [D] or [P]
  ## Config       — key: value block

[D] criteria are checked deterministically. Use ONLY these exact phrasings,
and ONLY reference operations the selected clone(s) support:
{_criteria_guidance(clone)}

Never require an operation the clone cannot perform (e.g. do not require a
channel to exist unless the clone supports channel creation).

[P] criteria need LLM judgment (agent reasoning, answer quality, etc.).

Known clones: {", ".join(_KNOWN_CLONES)}

Config example:
  clones: github
  seed: small-project
  runs: 1
  timeout: 60
  tags: happy-path, github
"""


# Back-compat: module-level SYSTEM (unconstrained — all twins).
SYSTEM = _build_system(None)


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
            {"role": "system", "content": _build_system(clone)},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.7,
    )
    return (resp.choices[0].message.content or "").strip()
