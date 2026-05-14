"""[D] deterministic checker — stage 1.

Pattern catalog keyed to Archal-equivalent scenario phrasings, generalized
across all three demo twins (github, slack, stripe). Unhandled criteria
report ``handled=False`` so the runner can fall through to the schema-validated
stage-2 LLM-JSON parser (and ultimately the `[P]` judge).

Design:
  - Patterns are `(compiled_regex, handler_callable)` pairs.
  - Handlers take ``(match, state, trace)`` and return ``CheckResult``.
  - State key lookup is twin-agnostic: a single ``_collect()`` helper finds
    the right list across the flat (single-clone) or nested (multi-clone)
    state shapes the Phase 4 runner can produce.

Adding a new pattern: append a `(regex, handler)` pair to ``PATTERNS``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass
class CheckResult:
    passed: bool
    reasoning: str
    handled: bool  # False => caller should defer to stage 2 / [P] judge


# ---------------------------------------------------------------------------
# Resource-name → state-key resolution
# ---------------------------------------------------------------------------

# Singularised / aliased noun → (twin, state_key)
# Order matters when two twins use the same word; the first match wins.
_RESOURCE_MAP: list[tuple[str, str, str]] = [
    # github
    ("issue",         "github", "issues"),
    ("issues",        "github", "issues"),
    ("pull request",  "github", "pulls"),
    ("pull requests", "github", "pulls"),
    ("pr",            "github", "pulls"),
    ("prs",           "github", "pulls"),
    ("branch",        "github", "branches"),       # synthesized from repos
    ("branches",      "github", "branches"),
    ("workflow run",  "github", "workflow_runs"),
    ("workflow runs", "github", "workflow_runs"),
    ("label",         "github", "labels"),
    ("labels",        "github", "labels"),
    ("comment",       "github", "comments"),
    ("comments",      "github", "comments"),
    ("repo",          "github", "repos"),
    ("repos",         "github", "repos"),
    ("repository",    "github", "repos"),
    ("repositories",  "github", "repos"),
    # slack
    ("channel",       "slack",  "channels"),
    ("channels",      "slack",  "channels"),
    ("message",       "slack",  "messages"),
    ("messages",      "slack",  "messages"),
    ("reaction",      "slack",  "reactions"),     # synthesized
    ("reactions",     "slack",  "reactions"),
    # stripe
    ("customer",      "stripe", "customers"),
    ("customers",     "stripe", "customers"),
    ("product",       "stripe", "products"),
    ("products",      "stripe", "products"),
    ("price",         "stripe", "prices"),
    ("prices",        "stripe", "prices"),
    ("payment intent", "stripe", "payment_intents"),
    ("payment intents", "stripe", "payment_intents"),
    ("refund",        "stripe", "refunds"),
    ("refunds",       "stripe", "refunds"),
    ("invoice",       "stripe", "invoices"),
    ("invoices",      "stripe", "invoices"),
    ("subscription",  "stripe", "subscriptions"),
    ("subscriptions", "stripe", "subscriptions"),
    ("coupon",        "stripe", "coupons"),
    ("coupons",       "stripe", "coupons"),
    ("payment link",  "stripe", "payment_links"),
    ("payment links", "stripe", "payment_links"),
    ("dispute",       "stripe", "disputes"),
    ("disputes",      "stripe", "disputes"),
    # linear
    ("linear issue",   "linear", "issues"),
    ("linear issues",  "linear", "issues"),
    ("linear project", "linear", "projects"),
    ("linear projects", "linear", "projects"),
    ("cycle",          "linear", "cycles"),
    ("cycles",         "linear", "cycles"),
    ("sprint",         "linear", "cycles"),
    ("sprints",        "linear", "cycles"),
    ("team",           "linear", "teams"),
    ("teams",          "linear", "teams"),
    # supabase
    ("bucket",         "supabase", "buckets"),
    ("buckets",        "supabase", "buckets"),
    ("object",         "supabase", "objects"),
    ("objects",        "supabase", "objects"),
    ("auth user",      "supabase", "auth_users"),
    ("auth users",     "supabase", "auth_users"),
    # discord
    ("guild",          "discord",  "guilds"),
    ("guilds",         "discord",  "guilds"),
    ("discord channel", "discord", "channels"),
    ("discord channels", "discord", "channels"),
    ("discord message", "discord", "messages"),
    ("discord messages", "discord", "messages"),
    ("role",           "discord",  "roles"),
    ("roles",          "discord",  "roles"),
    ("webhook",        "discord",  "webhooks"),
    ("webhooks",       "discord",  "webhooks"),
    # google workspace — gmail
    ("email",          "google-workspace", "gmail_messages"),
    ("emails",         "google-workspace", "gmail_messages"),
    ("gmail message",  "google-workspace", "gmail_messages"),
    ("gmail messages", "google-workspace", "gmail_messages"),
    ("thread",         "google-workspace", "gmail_threads"),
    ("threads",        "google-workspace", "gmail_threads"),
    ("draft",          "google-workspace", "gmail_drafts"),
    ("drafts",         "google-workspace", "gmail_drafts"),
    ("gmail label",    "google-workspace", "gmail_labels"),
    ("gmail labels",   "google-workspace", "gmail_labels"),
    # google workspace — drive
    ("drive file",     "google-workspace", "drive_files"),
    ("drive files",    "google-workspace", "drive_files"),
    # cross-twin
    ("user",          "_any",   "users"),
    ("users",         "_any",   "users"),
]


def _resource_lookup(noun: str) -> tuple[str, str] | None:
    n = noun.strip().lower()
    for token, twin, key in _RESOURCE_MAP:
        if token == n:
            return twin, key
    return None


def _twin_state(state: dict, twin: str) -> dict:
    """Return the sub-state dict for a twin name across flat / nested shapes.

    For nested multi-clone state ``{clone: state}``, the right sub-dict is
    returned. For flat (single-clone) state, the whole dict is returned.
    """
    if twin == "_any":
        # Caller will iterate twins; return the wrapping state as-is.
        return state
    if isinstance(state.get(twin), dict):
        return state[twin]
    return state


def _collect(state: dict, twin: str, key: str) -> list:
    """Collect a list of items for ``state[twin][key]``.

    Handles both single-clone (flat) and multi-clone (nested) shapes. For
    ``key == "branches"`` / ``key == "reactions"``, synthesize from related
    nested data.
    """
    if twin == "_any":
        items: list = []
        for sub in state.values():
            if isinstance(sub, dict):
                items.extend(_collect(sub, "_self", key))
        # Also support flat top-level
        items.extend(_collect(state, "_self", key))
        return items

    sub = _twin_state(state, twin)
    if not isinstance(sub, dict):
        return []

    if key == "branches":
        # GitHub: branches are stored inside each repo as a "branches" dict.
        out: list = []
        for repo in (sub.get("repos") or {}).values():
            if isinstance(repo, dict):
                br = repo.get("branches") or {}
                if isinstance(br, dict):
                    out.extend(br.values())
                elif isinstance(br, list):
                    out.extend(br)
        return out

    if key == "reactions":
        # Slack: reactions are on each message.
        out = []
        msgs = sub.get("messages") or {}
        if isinstance(msgs, dict):
            for arr in msgs.values():
                if isinstance(arr, list):
                    for m in arr:
                        for r in (m.get("reactions") or []):
                            out.append(r)
        return out

    if key == "auth_users":
        # Supabase: auth_users is a dict keyed by user id.
        raw_au = sub.get("auth_users") or {}
        if isinstance(raw_au, dict):
            return list(raw_au.values())
        return raw_au if isinstance(raw_au, list) else []

    if key in ("buckets", "objects"):
        # Supabase: buckets and objects are nested under STATE["storage"].
        storage = sub.get("storage") or {}
        raw_s = storage.get(key) or {}
        if isinstance(raw_s, dict):
            return list(raw_s.values())
        return raw_s if isinstance(raw_s, list) else []

    raw = sub.get(key)
    if isinstance(raw, dict):
        # Slack messages are dict[channel_id] -> list[message]. Flatten.
        items: list = []
        for k2, v in raw.items():
            if str(k2).startswith("_"):
                continue
            if isinstance(v, list):
                items.extend(v)
            else:
                items.append(v)
        return items
    if isinstance(raw, list):
        return raw
    return []


# ---------------------------------------------------------------------------
# State-matchers / selectors
# ---------------------------------------------------------------------------

# Mapping from English state words → (item-key, expected-value)
_STATE_VALUES: dict[str, tuple[str, str]] = {
    "open":     ("state", "open"),
    "opened":   ("state", "open"),
    "closed":   ("state", "closed"),
    "merged":   ("state", "merged"),
    "deleted":  ("state", "deleted"),
    "created":  ("state", "open"),  # newly-created defaults to "open" for issues/PRs
    "pending":  ("status", "pending"),
    "succeeded": ("status", "succeeded"),
    "failed":   ("status", "failed"),
    "active":   ("status", "active"),
    "canceled": ("status", "canceled"),
    "refunded": ("status", "succeeded"),  # refunds default to succeeded
}


def _matches_state(item: dict, word: str) -> bool:
    w = word.strip().lower()
    if w == "exist" or w == "exists":
        return True
    pair = _STATE_VALUES.get(w)
    if pair is None:
        # Unknown state word; treat any item as a non-match.
        return False
    field, value = pair
    return str(item.get(field, "")).strip().lower() == value


# ---------------------------------------------------------------------------
# Title / name selectors
# ---------------------------------------------------------------------------

def _item_label(item: dict) -> str:
    for k in ("title", "name", "subject", "id", "number"):
        v = item.get(k)
        if v is not None:
            return str(v)
    return "<unknown>"


def _has_title(item: dict, needle: str) -> bool:
    needle = needle.strip().lower()
    for k in ("title", "name", "subject"):
        v = item.get(k)
        if isinstance(v, str) and v.strip().lower() == needle:
            return True
    return False


def _has_label(item: dict, label: str) -> bool:
    for lab in item.get("labels", []) or []:
        if isinstance(lab, dict) and lab.get("name") == label:
            return True
        if isinstance(lab, str) and lab == label:
            return True
    return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _count_compare(state: dict, noun: str, state_word: str | None, op: str, target: int) -> CheckResult:
    res = _resource_lookup(noun)
    if not res:
        return CheckResult(False, f"Unknown resource '{noun}'", handled=False)
    twin, key = res
    items = _collect(state, twin, key)
    if state_word:
        items = [i for i in items if isinstance(i, dict) and _matches_state(i, state_word)]
    n = len(items)
    if op == "eq":
        return CheckResult(n == target, f"Found {n} {noun}; expected exactly {target}.", True)
    if op == "gte":
        return CheckResult(n >= target, f"Found {n} {noun}; expected at least {target}.", True)
    if op == "lte":
        return CheckResult(n <= target, f"Found {n} {noun}; expected at most {target}.", True)
    return CheckResult(False, f"Unknown op '{op}'", handled=False)


def h_exactly_n_state(m: re.Match, state: dict, trace: list) -> CheckResult:
    n = int(m.group("n"))
    return _count_compare(state, m.group("noun"), m.group("st"), "eq", n)


def h_exactly_n_exist(m: re.Match, state: dict, trace: list) -> CheckResult:
    n = int(m.group("n"))
    return _count_compare(state, m.group("noun"), None, "eq", n)


def h_at_least_n_state(m: re.Match, state: dict, trace: list) -> CheckResult:
    n = int(m.group("n"))
    st = m.groupdict().get("st")
    return _count_compare(state, m.group("noun"), st, "gte", n)


def h_at_most_n_state(m: re.Match, state: dict, trace: list) -> CheckResult:
    n = int(m.group("n"))
    st = m.groupdict().get("st")
    return _count_compare(state, m.group("noun"), st, "lte", n)


def h_no_resource_state(m: re.Match, state: dict, trace: list) -> CheckResult:
    return _count_compare(state, m.group("noun"), m.groupdict().get("st"), "eq", 0)


def h_zero_resource(m: re.Match, state: dict, trace: list) -> CheckResult:
    return _count_compare(state, m.group("noun"), m.groupdict().get("st"), "eq", 0)


def h_count_equals(m: re.Match, state: dict, trace: list) -> CheckResult:
    n = int(m.group("n"))
    return _count_compare(state, m.group("noun"), None, "eq", n)


def h_exactly_n_titled(m: re.Match, state: dict, trace: list) -> CheckResult:
    """exactly N <noun> named/titled 'X' exist(s)."""
    n = int(m.group("n"))
    noun = m.group("noun")
    target = m.group("title")
    res = _resource_lookup(noun)
    if not res:
        return CheckResult(False, f"Unknown resource '{noun}'", handled=False)
    twin, key = res
    items = _collect(state, twin, key)
    matching = [i for i in items if isinstance(i, dict) and _has_title(i, target)]
    return CheckResult(
        len(matching) == n,
        f"Found {len(matching)} {noun} named/titled '{target}'; expected exactly {n}.",
        True,
    )


def h_at_least_n_titled(m: re.Match, state: dict, trace: list) -> CheckResult:
    """at least N <noun> named/titled 'X' exist(s)."""
    n = int(m.group("n"))
    noun = m.group("noun")
    target = m.group("title")
    res = _resource_lookup(noun)
    if not res:
        return CheckResult(False, f"Unknown resource '{noun}'", handled=False)
    twin, key = res
    items = _collect(state, twin, key)
    matching = [i for i in items if isinstance(i, dict) and _has_title(i, target)]
    return CheckResult(
        len(matching) >= n,
        f"Found {len(matching)} {noun} named/titled '{target}'; expected at least {n}.",
        True,
    )


def h_titled_exists(m: re.Match, state: dict, trace: list) -> CheckResult:
    noun = m.group("noun")
    target = m.group("title")
    res = _resource_lookup(noun)
    if not res:
        return CheckResult(False, f"Unknown resource '{noun}'", handled=False)
    twin, key = res
    items = _collect(state, twin, key)
    hit = next((i for i in items if isinstance(i, dict) and _has_title(i, target)), None)
    if hit:
        return CheckResult(True, f"{noun} titled/named '{target}' found.", True)
    titles = [_item_label(i) for i in items if isinstance(i, dict)]
    return CheckResult(False, f"No {noun} titled/named '{target}'. Found: {titles[:10]}", True)


def h_remain_state(m: re.Match, state: dict, trace: list) -> CheckResult:
    """`<resource> with "X" remain <state>` — every item carrying label X is in <state>."""
    noun = m.group("noun")
    label = m.group("label")
    want_state = m.group("st")
    res = _resource_lookup(noun)
    if not res:
        return CheckResult(False, f"Unknown resource '{noun}'", handled=False)
    twin, key = res
    items = _collect(state, twin, key)
    with_label = [i for i in items if isinstance(i, dict) and _has_label(i, label)]
    if not with_label:
        return CheckResult(False, f"No {noun} carry label '{label}'.", True)
    off = [i for i in with_label if not _matches_state(i, want_state)]
    return CheckResult(
        len(off) == 0,
        f"{len(with_label)} {noun} carry label '{label}'; {len(off)} not {want_state}.",
        True,
    )


def h_all_closed_have_comment(m: re.Match, state: dict, trace: list) -> CheckResult:
    items = _collect(state, "github", "issues")
    closed = [i for i in items if isinstance(i, dict) and _matches_state(i, "closed")]
    if not closed:
        return CheckResult(False, "No closed issues to check.", True)
    missing = [i for i in closed if int(i.get("comments") or 0) < 1]
    return CheckResult(
        len(missing) == 0,
        f"{len(closed) - len(missing)}/{len(closed)} closed issues have a comment.",
        True,
    )


def h_label_named_exists(m: re.Match, state: dict, trace: list) -> CheckResult:
    label = m.group("title")
    labels = _collect(state, "github", "labels")
    found = any(isinstance(lab, dict) and lab.get("name") == label for lab in labels)
    return CheckResult(found, f"Label '{label}' {'found' if found else 'not found'}.", True)


def h_no_new_resource(m: re.Match, state: dict, trace: list) -> CheckResult:
    """`no new <resource>` — same as zero of that resource."""
    return _count_compare(state, m.group("noun"), None, "eq", 0)


def _supabase_table_rows(state: dict, table: str) -> list:
    """Collect rows from a Supabase PostgREST dynamic table across flat/nested state."""
    def _from_sub(sub: dict) -> list:
        tables = sub.get("tables") or {}
        tbl = tables.get(table) or {}
        return list(tbl.get("rows") or [])

    if isinstance(state.get("supabase"), dict):
        return _from_sub(state["supabase"])
    return _from_sub(state)


def h_trace_call_count(m: re.Match, state: dict, trace: list) -> CheckResult:
    """'agent called exactly/at least/at most N METHOD /path' — trace-based."""
    method = m.group("method").upper()
    path_needle = m.group("path").rstrip("/")
    raw_op = re.sub(r"\s+", "_", m.group("op").strip().lower())  # "at_least", "at_most", "exactly"
    n = int(m.group("n"))

    count = sum(
        1 for e in trace
        if isinstance(e, dict)
        and e.get("method", "").upper() == method
        and path_needle in e.get("path", "")
    )
    if raw_op == "exactly":
        passed = count == n
    elif raw_op == "at_least":
        passed = count >= n
    else:  # at_most
        passed = count <= n
    op_label = raw_op.replace("_", " ")
    return CheckResult(
        passed,
        f"Trace has {count} {method} calls matching '{path_needle}'; expected {op_label} {n}.",
        True,
    )


def h_trace_no_call(m: re.Match, state: dict, trace: list) -> CheckResult:
    """'agent did not call DELETE /path' / 'no DELETE calls to /path'."""
    method = m.group("method").upper()
    path_needle = m.group("path").rstrip("/")
    count = sum(
        1 for e in trace
        if isinstance(e, dict)
        and e.get("method", "").upper() == method
        and path_needle in e.get("path", "")
    )
    return CheckResult(
        count == 0,
        f"Trace has {count} {method} calls matching '{path_needle}'; expected none.",
        True,
    )


def h_supabase_table_rows_gte(m: re.Match, state: dict, trace: list) -> CheckResult:
    n = int(m.group("n"))
    tbl = m.group("tbl").lower()
    rows = _supabase_table_rows(state, tbl)
    return CheckResult(
        len(rows) >= n,
        f"Table '{tbl}' has {len(rows)} rows; expected at least {n}.",
        True,
    )


def h_supabase_table_rows_eq(m: re.Match, state: dict, trace: list) -> CheckResult:
    n = int(m.group("n"))
    tbl = m.group("tbl").lower()
    rows = _supabase_table_rows(state, tbl)
    return CheckResult(
        len(rows) == n,
        f"Table '{tbl}' has {len(rows)} rows; expected exactly {n}.",
        True,
    )


# ---------------------------------------------------------------------------
# Pattern catalog
# ---------------------------------------------------------------------------

# Resource regex fragment: covers all known nouns (singular + plural + multi-word).
# Built once from `_RESOURCE_MAP` so adding a noun there auto-extends the regex.
_RESOURCE_TOKENS = sorted({t for t, _, _ in _RESOURCE_MAP}, key=len, reverse=True)
_RES = r"(?P<noun>" + "|".join(re.escape(t) for t in _RESOURCE_TOKENS) + r")"

# State word fragment.
_STATE_WORDS = sorted(_STATE_VALUES.keys(), key=len, reverse=True)
_ST = r"(?P<st>" + "|".join(_STATE_WORDS) + r")"

_FLAGS = re.I

PATTERNS: list[tuple[re.Pattern, Callable[[re.Match, dict, list], CheckResult]]] = [
    # --- Trace-based patterns (MUST come before state-based count patterns) ---
    # "agent called exactly/at least/at most N METHOD requests to /path"
    (re.compile(
        r"\bagent\s+(?:called?|made?)\s+(?P<op>exactly|at\s+least|at\s+most)\s+(?P<n>\d+)\s+"
        r"(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?:requests?\s+to|calls?\s+to)\s+(?P<path>\S+)",
        re.IGNORECASE,
    ), h_trace_call_count),
    # "METHOD /path was called exactly/at least N times"
    (re.compile(
        r"\b(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>\S+)\s+(?:was|were|is)\s+called?\s+"
        r"(?P<op>exactly|at\s+least|at\s+most)\s+(?P<n>\d+)\s+times?",
        re.IGNORECASE,
    ), h_trace_call_count),
    # "agent did not call DELETE /path"
    (re.compile(
        r"\bagent\s+did\s+not\s+(?:call|make\s+(?:a\s+)?)\s*(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?:request\s+to\s+)?(?P<path>\S+)",
        re.IGNORECASE,
    ), h_trace_no_call),
    # "no DELETE requests to /path"  /  "no DELETE calls to /path"
    (re.compile(
        r"\bno\s+(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?:requests?\s+to|calls?\s+to)\s+(?P<path>\S+)",
        re.IGNORECASE,
    ), h_trace_no_call),

    # --- Named/titled variants (MUST come before generic count patterns) ------
    # exactly N <noun> named/titled "X"
    (re.compile(rf"\bexactly\s+(?P<n>\d+)\s+{_RES}\s+(?:named|titled|called)\s+[\"'](?P<title>[^\"']+)[\"']", _FLAGS),
     h_exactly_n_titled),
    # at least N <noun> named/titled "X"
    (re.compile(rf"\bat\s+least\s+(?P<n>\d+)\s+{_RES}\s+(?:named|titled|called)\s+[\"'](?P<title>[^\"']+)[\"']", _FLAGS),
     h_at_least_n_titled),
    # at least N <noun> exist(s) named "X"  (trailing name qualifier)
    (re.compile(rf"\bat\s+least\s+(?P<n>\d+)\s+{_RES}\s+exists?\s+(?:named|titled|called)\s+[\"'](?P<title>[^\"']+)[\"']", _FLAGS),
     h_at_least_n_titled),

    # --- Generic count patterns -----------------------------------------------
    # exactly N <noun> are <state>
    (re.compile(rf"\bexactly\s+(?P<n>\d+)\s+{_RES}\s+(?:are|were|is|have\s+been)\s+{_ST}\b", _FLAGS),
     h_exactly_n_state),
    # exactly N <noun> exist(s)
    (re.compile(rf"\bexactly\s+(?P<n>\d+)\s+{_RES}\s+exists?\b", _FLAGS),
     h_exactly_n_exist),
    # exactly N <noun>   (e.g. "the repo has exactly 2 labels")
    (re.compile(rf"\bexactly\s+(?P<n>\d+)\s+{_RES}\b", _FLAGS),
     h_exactly_n_exist),
    # at least N <noun> are <state>
    (re.compile(rf"\bat\s+least\s+(?P<n>\d+)\s+{_RES}\s+(?:are|were|is|have\s+been)\s+{_ST}\b", _FLAGS),
     h_at_least_n_state),
    # at least N <noun>
    (re.compile(rf"\bat\s+least\s+(?P<n>\d+)\s+{_RES}\b", _FLAGS),
     h_at_least_n_state),
    # at most N <noun> are <state>
    (re.compile(rf"\bat\s+most\s+(?P<n>\d+)\s+{_RES}\s+(?:are|were|is|have\s+been)\s+{_ST}\b", _FLAGS),
     h_at_most_n_state),
    # at most N <noun>
    (re.compile(rf"\bat\s+most\s+(?P<n>\d+)\s+{_RES}\b", _FLAGS),
     h_at_most_n_state),
    # no <noun> are/were created/closed/...   OR   no <noun> have been ...
    (re.compile(rf"\bno\s+(?:new\s+)?{_RES}\s+(?:are|were|have\s+been)\s+{_ST}\b", _FLAGS),
     h_no_resource_state),
    # no new <noun>     (e.g. "no new issues")
    (re.compile(rf"\bno\s+new\s+{_RES}\b", _FLAGS),
     h_no_new_resource),
    # zero <noun> were/are created/...
    (re.compile(rf"\bzero\s+{_RES}\s+(?:are|were|have\s+been)\s+{_ST}\b", _FLAGS),
     h_zero_resource),
    (re.compile(rf"\bzero\s+{_RES}\b", _FLAGS),
     h_zero_resource),
    # count of <noun> equals N / <noun> count equals N
    (re.compile(rf"\bcount\s+of\s+{_RES}\s+(?:equals?|is|=)\s*(?P<n>\d+)\b", _FLAGS),
     h_count_equals),
    (re.compile(rf"\b{_RES}\s+count\s+(?:equals?|is|=)\s*(?P<n>\d+)\b", _FLAGS),
     h_count_equals),
    # a <noun> titled/named "X" exists  (also: 'X')
    (re.compile(rf"\ban?\s+{_RES}\s+(?:titled|named|called)\s+[\"'](?P<title>[^\"']+)[\"']\s+(?:exists|is\s+created|was\s+created)\b", _FLAGS),
     h_titled_exists),
    # a <noun> with title "X" exists
    (re.compile(rf"\ban?\s+{_RES}\s+with\s+(?:the\s+)?(?:title|name)\s+[\"'](?P<title>[^\"']+)[\"']\s+exists\b", _FLAGS),
     h_titled_exists),
    # <noun> "X" exists   (e.g. 'channel "engineering" exists')
    (re.compile(rf"\b{_RES}\s+[\"'](?P<title>[^\"']+)[\"']\s+exists\b", _FLAGS),
     h_titled_exists),
    # <noun>s with "X" remain <state>
    (re.compile(rf"\b{_RES}\s+with\s+[\"'](?P<label>[^\"']+)[\"']\s+remain\s+{_ST}\b", _FLAGS),
     h_remain_state),
    # all closed issues have a (new) comment
    (re.compile(r"\ball\s+closed\s+issues?\s+have\s+(?:a\s+(?:new\s+)?)?comments?\b", _FLAGS),
     h_all_closed_have_comment),
    # a label named "X" exists  (kept for back-compat with the v0 single-pattern)
    (re.compile(r"\ba?\s*label\s+named\s+[\"'](?P<title>[^\"']+)[\"']\s+exists", _FLAGS),
     h_label_named_exists),
    # Supabase: "at least N rows in <table>" / "N rows exist in <table>"
    (re.compile(r"\bat\s+least\s+(?P<n>\d+)\s+rows?\s+(?:exist|exists|in|from)\s+(?:the\s+)?(?P<tbl>\w+)\s*(?:table)?\b", re.I),
     h_supabase_table_rows_gte),
    (re.compile(r"\bexactly\s+(?P<n>\d+)\s+rows?\s+(?:exist|exists|in|from)\s+(?:the\s+)?(?P<tbl>\w+)\s*(?:table)?\b", re.I),
     h_supabase_table_rows_eq),
    (re.compile(r"\b(?:the\s+)?(?P<tbl>\w+)\s+table\s+has\s+(?:at\s+least\s+)?(?P<n>\d+)\s+rows?\b", re.I),
     h_supabase_table_rows_gte),
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check(criterion_text: str, state: dict, trace: list) -> CheckResult:
    """Try every pattern in order; first match wins."""
    text = criterion_text.strip()
    for pat, handler in PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        result = handler(m, state, trace)
        # If a handler explicitly de-handles (e.g. unknown noun), keep trying.
        if not result.handled:
            continue
        return result
    return CheckResult(False, "No deterministic pattern matched.", handled=False)


# ---------------------------------------------------------------------------
# Back-compat helpers used elsewhere in the package / tests.
# ---------------------------------------------------------------------------

def _issues(state: dict) -> list[dict]:
    return _collect(state, "github", "issues")


def _labels(state: dict) -> Iterable[dict]:
    return _collect(state, "github", "labels")
