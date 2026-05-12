"""[D] deterministic checker.

Tiny pattern set keyed to common scenario phrasings. Unhandled criteria report
handled=False so the runner can fall back to the LLM judge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class CheckResult:
    passed: bool
    reasoning: str
    handled: bool  # False => caller should defer to LLM judge


PATTERNS = [
    (re.compile(r'an?\s+issue\s+titled\s+"([^"]+)"\s+exists', re.I), "issue_titled_exists"),
    (re.compile(r"an?\s+issue\s+titled\s+'([^']+)'\s+exists", re.I), "issue_titled_exists"),
    (re.compile(r"exactly\s+(\d+)\s+issues?\s+are\s+closed", re.I), "exactly_n_closed"),
    (re.compile(r"exactly\s+(\d+)\s+issues?\s+are\s+open", re.I), "exactly_n_open"),
    (re.compile(r"at\s+least\s+(\d+)\s+issues?\s+are\s+closed", re.I), "at_least_n_closed"),
    (re.compile(r"all\s+closed\s+issues?\s+have\s+a\s+new\s+comment", re.I), "all_closed_have_comment"),
    (re.compile(r'issues?\s+with\s+(?:the\s+label\s+)?"([^"]+)"\s+remain\s+open', re.I), "label_remain_open"),
    (re.compile(r'a?\s*label\s+named\s+"([^"]+)"\s+exists', re.I), "label_exists"),
    (re.compile(r"no\s+new\s+issues?\s+(?:were|are|have\s+been)\s+created", re.I), "no_new_issues"),
    (re.compile(r"zero\s+issues?\s+(?:were|are)\s+created", re.I), "no_new_issues"),
]


def check(criterion_text: str, state: dict, trace: list) -> CheckResult:
    for pat, kind in PATTERNS:
        m = pat.search(criterion_text)
        if not m:
            continue
        return _apply(kind, m, state, trace)
    return CheckResult(False, "No deterministic pattern matched.", handled=False)


def _issues(state: dict) -> list[dict]:
    issues = state.get("issues")
    if isinstance(issues, dict):
        return list(issues.values())
    # Multi-clone shape: {clone: {issues: {...}, ...}}. Look inside the
    # github sub-state if present, else flatten across all sub-states.
    if isinstance(state.get("github"), dict):
        return list((state["github"].get("issues") or {}).values())
    out: list[dict] = []
    for v in state.values():
        if isinstance(v, dict) and isinstance(v.get("issues"), dict):
            out.extend(v["issues"].values())
    return out


def _labels(state: dict) -> dict:
    labels = state.get("labels")
    if isinstance(labels, dict):
        return labels
    if isinstance(state.get("github"), dict):
        return state["github"].get("labels") or {}
    return {}


def _apply(kind: str, m: re.Match, state: dict, trace: list) -> CheckResult:
    issues = _issues(state)

    if kind == "issue_titled_exists":
        target = m.group(1).strip().lower()
        hit = next((i for i in issues if i.get("title", "").strip().lower() == target), None)
        if hit:
            return CheckResult(True, f"Issue titled '{m.group(1)}' found (#{hit.get('number')}).", True)
        titles = [i.get("title") for i in issues]
        return CheckResult(False, f"No issue titled '{m.group(1)}'. Found: {titles}", True)

    if kind == "exactly_n_closed":
        n = int(m.group(1))
        closed = [i for i in issues if i.get("state") == "closed"]
        return CheckResult(len(closed) == n, f"{len(closed)} closed; expected exactly {n}.", True)

    if kind == "exactly_n_open":
        n = int(m.group(1))
        opn = [i for i in issues if i.get("state") == "open"]
        return CheckResult(len(opn) == n, f"{len(opn)} open; expected exactly {n}.", True)

    if kind == "at_least_n_closed":
        n = int(m.group(1))
        closed = [i for i in issues if i.get("state") == "closed"]
        return CheckResult(len(closed) >= n, f"{len(closed)} closed; expected at least {n}.", True)

    if kind == "all_closed_have_comment":
        closed = [i for i in issues if i.get("state") == "closed"]
        if not closed:
            return CheckResult(False, "No closed issues to check.", True)
        missing = [i for i in closed if i.get("comments", 0) < 1]
        return CheckResult(
            len(missing) == 0,
            f"{len(closed) - len(missing)}/{len(closed)} closed issues have at least one comment.",
            True,
        )

    if kind == "label_remain_open":
        label = m.group(1)
        with_label = [i for i in issues if any(lab.get("name") == label for lab in i.get("labels", []))]
        if not with_label:
            return CheckResult(False, f"No issues carry label '{label}'.", True)
        not_open = [i for i in with_label if i.get("state") != "open"]
        return CheckResult(
            len(not_open) == 0,
            f"{len(with_label)} carry label '{label}'; {len(not_open)} not open.",
            True,
        )

    if kind == "label_exists":
        label = m.group(1)
        found = any(lab.get("name") == label for lab in _labels(state).values())
        return CheckResult(found, f"Label '{label}' {'found' if found else 'not found'}.", True)

    if kind == "no_new_issues":
        n = len(issues)
        return CheckResult(n == 0, f"{n} issue(s) currently exist.", True)

    return CheckResult(False, f"Internal: no handler for kind={kind}.", False)
