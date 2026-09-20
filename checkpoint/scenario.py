"""Scenarios: a markdown file describing one task and how to grade it.

    ---
    twins: [github]
    seed: small-project
    ---
    # File a bug

    ## Task
    File an issue in acme/webapp titled "Login broken".

    ## Criteria
    - [D] An issue titled "Login broken" exists
    - [D!] No issues were deleted                => count(deleted.github.issues) == 0
    - [T] The agent made at most 6 calls
    - [P] The final answer explains what it filed

`[D]` checks the state the agent left behind, `[T]` the calls it made, `[P]` what
it said. `!` marks a criterion that must pass for the run to pass, whatever the
score. Anything after `=>` is an explicit assertion
(:mod:`checkpoint.eval.expr`) — the deterministic check to run, written out
instead of inferred.

Settings live in YAML front matter or a `## Config` section; both accept the
same keys.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

CriterionKind = Literal["D", "T", "P"]

DEFAULT_TIMEOUT = 180

_SECTIONS = {
    "setup": "setup",
    "context": "setup",
    "prompt": "prompt",
    "task": "prompt",
    "expected behavior": "expected",
    "expected behaviour": "expected",
    "expected": "expected",
    "success criteria": "criteria",
    "criteria": "criteria",
    "checks": "criteria",
    "config": "config",
    "settings": "config",
}

# Settings a scenario may carry, in front matter or `## Config`. Anything else
# is reported by `checkpoint check` rather than silently ignored.
KNOWN_SETTINGS = frozenset({
    "twins", "clones", "seed", "seed-file", "seed_file", "runs", "timeout",
    "tags", "faults", "judge-model", "judge_model", "owasp",
    # Read by the simulated user (`checkpoint simulate`).
    "persona", "goal", "tone", "patience", "adversarial",
})

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<body>.*)$")
_TAG = re.compile(r"^\[(?P<kind>[A-Za-z])(?P<must>!?)\]\s*(?P<rest>.*)$", re.DOTALL)
_ASSERTION = re.compile(r"\s=>\s", re.DOTALL)

# Words that mean a criterion is about final state rather than judgement, used
# only when the author did not tag it.
_STATE_WORDS = re.compile(
    r"\b(exactly|at least|at most|exists?|created|deleted|removed|closed|opened|merged|"
    r"no more than|fewer than|remain|still|none|zero|no new)\b",
    re.IGNORECASE,
)


class ScenarioError(ValueError):
    """The scenario file cannot be used as written."""


@dataclass
class Criterion:
    text: str
    kind: CriterionKind = "P"
    must_pass: bool = False
    assertion: str | None = None
    """An explicit assertion written after ``=>``; overrides any inference."""
    line: int = 0

    @property
    def label(self) -> str:
        return f"[{self.kind}{'!' if self.must_pass else ''}]"


@dataclass
class Scenario:
    title: str = ""
    setup: str = ""
    prompt: str = ""
    expected: str = ""
    criteria: list[Criterion] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    source_path: str | None = None
    problems: list[str] = field(default_factory=list)
    """Things wrong with the file that did not stop it being read."""

    # -- settings -------------------------------------------------------------

    @property
    def twins(self) -> list[str]:
        value = self.config.get("twins", self.config.get("clones"))
        return _as_list(value)

    @property
    def clones(self) -> list[str]:
        """The older name for :attr:`twins`."""
        return self.twins

    @property
    def runs(self) -> int:
        return max(1, _as_int(self.config.get("runs"), 1, "runs"))

    @property
    def timeout(self) -> int:
        return max(1, _as_int(self.config.get("timeout"), DEFAULT_TIMEOUT, "timeout"))

    @property
    def tags(self) -> list[str]:
        return _as_list(self.config.get("tags"))

    @property
    def faults(self) -> dict[str, dict]:
        faults = self.config.get("faults") or {}
        return faults if isinstance(faults, dict) else {}

    @property
    def judge_model(self) -> str | None:
        value = self.config.get("judge-model") or self.config.get("judge_model")
        return str(value) if value else None

    @property
    def must_pass(self) -> list[Criterion]:
        return [c for c in self.criteria if c.must_pass]

    @property
    def runnable(self) -> bool:
        return bool(self.prompt.strip())


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def parse(text: str, source: str | None = None) -> Scenario:
    """Read a scenario. Never raises: problems are collected on the scenario."""
    scenario = Scenario(source_path=source)
    # Authors annotate scenarios; an HTML comment is a note, not a criterion.
    # Keep the line count so reported line numbers still point at the file.
    text = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    body, front_matter = _split_front_matter(text, scenario)
    scenario.config.update(front_matter)

    title = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if title:
        scenario.title = title.group(1).strip()

    offset = 0
    parts = re.split(r"^##\s+(.+)$", body, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        content = parts[i + 1] if i + 1 < len(parts) else ""
        offset = body.find(content, offset) if content else offset
        line_no = body.count("\n", 0, max(offset, 0)) + 1
        section = _SECTIONS.get(heading)
        if section is None:
            scenario.problems.append(f"unknown section '## {parts[i].strip()}' (ignored)")
            continue
        content = content.strip("\n")
        if section == "setup":
            scenario.setup = content.strip()
        elif section == "prompt":
            scenario.prompt = content.strip()
        elif section == "expected":
            scenario.expected = content.strip()
        elif section == "criteria":
            # Several criteria sections add up; they used to replace each other.
            scenario.criteria.extend(_parse_criteria(content, scenario, line_no))
        elif section == "config":
            scenario.config.update(_parse_config(content, scenario))
    return scenario


def parse_file(path: str | Path) -> Scenario:
    p = Path(path)
    return parse(p.read_text(encoding="utf-8"), source=str(p))


def _split_front_matter(text: str, scenario: Scenario) -> tuple[str, dict]:
    if not text.lstrip().startswith("---"):
        return text, {}
    stripped = text.lstrip()
    end = stripped.find("\n---", 3)
    if end == -1:
        scenario.problems.append("front matter is not closed with ---")
        return text, {}
    raw = stripped[3:end]
    rest = stripped[end + 4:]
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        scenario.problems.append(f"front matter is not valid YAML: {e}")
        return rest, {}
    if not isinstance(data, dict):
        scenario.problems.append("front matter must be a mapping of settings")
        return rest, {}
    return rest, _normalize_keys(data)


def _parse_criteria(text: str, scenario: Scenario, start_line: int) -> list[Criterion]:
    criteria: list[Criterion] = []
    pending: list[str] = []
    pending_line = start_line

    def flush() -> None:
        if pending:
            criterion = _make_criterion(" ".join(pending).strip(), scenario, pending_line)
            if criterion is not None:
                criteria.append(criterion)
            pending.clear()

    for i, raw in enumerate(text.splitlines()):
        line_no = start_line + i
        bullet = _BULLET.match(raw)
        if bullet:
            flush()
            pending_line = line_no
            pending.append(bullet.group("body").strip())
            continue
        if pending and raw.strip() and raw[:1].isspace():
            # A wrapped continuation line: previously the rest was dropped.
            pending.append(raw.strip())
            continue
        flush()
        if raw.strip() and not raw.lstrip().startswith(("#", ">", "|")):
            scenario.problems.append(
                f"line {line_no}: {raw.strip()[:60]!r} is not a criterion (criteria start with '-')"
            )
    flush()
    return criteria


def _make_criterion(body: str, scenario: Scenario, line: int) -> Criterion | None:
    if not body:
        return None
    assertion: str | None = None
    if _ASSERTION.search(body):
        body, _, raw_assertion = _ASSERTION.split(body, maxsplit=1)[0], "=>", _ASSERTION.split(body, maxsplit=1)[1]
        assertion = raw_assertion.strip() or None
        body = body.strip()
    tag = _TAG.match(body)
    must_pass = False
    if tag:
        kind = tag.group("kind").upper()
        must_pass = tag.group("must") == "!"
        body = tag.group("rest").strip()
        if kind not in ("D", "T", "P"):
            scenario.problems.append(
                f"line {line}: unknown criterion kind [{tag.group('kind')}] "
                "(use [D] state, [T] calls, [P] judged)"
            )
            return None
    else:
        kind = "D" if _STATE_WORDS.search(body) else "P"
    if not body:
        scenario.problems.append(f"line {line}: criterion has no text")
        return None
    return Criterion(text=body, kind=kind, must_pass=must_pass, assertion=assertion, line=line)


def _parse_config(text: str, scenario: Scenario) -> dict:
    text = text.strip()
    if not text:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        scenario.problems.append(f"## Config is not valid YAML: {e}")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        scenario.problems.append("## Config must be a list of `key: value` settings")
        return {}
    return _normalize_keys(data)


def _normalize_keys(data: dict) -> dict:
    return {str(k).strip().lower(): v for k, v in data.items()}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _as_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ScenarioError(f"{name} must be a whole number, got {value!r}") from None
