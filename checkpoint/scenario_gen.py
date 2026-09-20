"""Draft a scenario with a model, in the format the runner actually reads.

:func:`generate` returns the Markdown for one scenario file — YAML front matter,
``## Task``, ``## Criteria`` — ready to write to ``scenarios/<name>.md``. What it
guarantees about that text:

* it parses with :func:`checkpoint.scenario.parse`, and the result has a task, at
  least one criterion, and no reported problems;
* every twin its front matter names exists in :mod:`checkpoint.twins.registry`,
  and is one of the twins the caller asked for;
* a named ``seed:`` is a seed those twins actually ship;
* every assertion pinned after ``=>`` parses as :mod:`checkpoint.eval.expr`. One
  that does not is removed and the removal noted in the file, because an
  assertion that cannot be evaluated makes the run an *error* rather than a
  failed criterion — the run ends with no verdict at all.

A draft that cannot be made to satisfy those raises :class:`ScenarioGenError`
rather than being handed back for someone to discover at run time.

What it cannot guarantee is that the criteria measure what the author meant. The
model is given the twins' real collections, the assertion grammar and the seeds
that exist — which is what makes a pinned assertion worth reading — but a
generated scenario is a draft to review, not a finished gate.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from .eval import schema_for

# The schema is described to a model in exactly one place. The criterion
# compiler owns that description; a second one here would drift from the
# language the assertions are actually checked against.
from .eval.compile import _schema_payload
from .eval.expr import ExprSyntaxError
from .eval.expr import parse as parse_assertion
from .llm import DEFAULT_MODEL, complete_text
from .scenario import parse as parse_scenario
from .twins import registry

#: One retry, with the problems fed back. A second failure is reported rather
#: than paid for again: the same prompt failing twice is a prompt problem.
_ATTEMPTS = 2


class ScenarioGenError(RuntimeError):
    """The model did not produce a scenario that can be run."""


def generate(
    description: str,
    *,
    twins: Sequence[str] | str,
    seed: str | None = None,
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
    _client_factory: Callable[[], Any] | None = None,
) -> str:
    """Draft one scenario for ``twins`` from a prose ``description``.

    ``twins`` is a list of twin names, or a comma-separated string. ``seed``
    names the dataset the twins start from; left out, the model picks one that
    exists. ``client`` (or ``_client_factory``) is the LLM seam every call site
    in this repo uses, so tests drive this without a network.

    Raises :class:`ValueError` for an unknown twin — before any model call — and
    :class:`ScenarioGenError` when the draft cannot be made usable.
    """
    if not description or not description.strip():
        raise ValueError("a scenario needs a description of the task to write")
    names = _resolve_twins(twins)
    if client is None and _client_factory is not None:
        client = _client_factory()

    system = _system(names, seed)
    request = _request(description, names, seed)
    user, problems = request, []
    for _ in range(_ATTEMPTS):
        draft = _unfence(complete_text(system=system, user=user, model=model, client=client))
        problems = _problems(draft, names)
        if not problems:
            return _drop_broken_assertions(draft).rstrip() + "\n"
        user = _retry(request, draft, problems)

    raise ScenarioGenError(
        "the model did not return a usable scenario after two attempts:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


# -- inputs -------------------------------------------------------------------


def _resolve_twins(twins: Sequence[str] | str) -> list[str]:
    """Canonical twin names, or ValueError naming what is available."""
    if isinstance(twins, str):
        raw = [part.strip() for part in twins.split(",")]
    else:
        raw = [str(name).strip() for name in twins]
    raw = [name for name in raw if name]
    available = ", ".join(registry.names())
    if not raw:
        raise ValueError(f"a scenario needs at least one twin; available: {available}")

    resolved: list[str] = []
    for name in raw:
        try:
            spec = registry.get(name)
        except registry.UnknownTwinError:
            raise ValueError(f"unknown twin {name!r}; available: {available}") from None
        if spec.name not in resolved:
            resolved.append(spec.name)
    return resolved


def _request(description: str, twins: list[str], seed: str | None) -> str:
    lines = [f"Write the scenario for this task: {description.strip()}",
             f"Twins: {', '.join(twins)}"]
    if seed:
        lines.append(f"Seed: {seed}")
    return "\n".join(lines)


def _retry(request: str, draft: str, problems: list[str]) -> str:
    return (
        f"{request}\n\nYour previous answer cannot be used:\n"
        + "\n".join(f"- {problem}" for problem in problems)
        + "\n\nRewrite the whole file, fixing every point above. "
        "Return only the Markdown.\n\nYour previous answer:\n" + draft
    )


_FENCE = re.compile(r"\A\s*```[A-Za-z]*[ \t]*\n(?P<body>.*?)\n?```\s*\Z", re.DOTALL)


def _unfence(text: str) -> str:
    """Models wrap a whole file in a code fence; the file itself is what we want."""
    # Line endings are normalized here so the assertion text the parser reports
    # is byte-identical to the line it came from, which is how a broken one is
    # found again and removed.
    text = text.replace("\r\n", "\n")
    match = _FENCE.match(text)
    return match.group("body") if match else text


# -- the prompt ---------------------------------------------------------------


_FORMAT = """\
You write one scenario for Checkpoint, a benchmark that runs an AI agent against
simulated SaaS services ("twins") and scores the state it left behind.

Return ONLY the Markdown file: no code fence, no commentary, nothing before the
front matter.

The shape of the file:

---
twins: [github]
seed: small-project
timeout: 120
tags: [github]
---
# <short title>

## Setup

<One paragraph: what the twins already hold when the agent starts, which is what
the named seed contains. Prose, not a list of records.>

## Task

<Two to four sentences, addressed to the agent. Name the records it has to touch
and say what its final answer must report.>

## Criteria

- [D] Exactly 1 issue was created  => count(created.github.issues) == 1
- [D!] No issues were deleted  => count(deleted.github.issues) == 0
- [T] The agent made at most 10 calls  => count(trace) <= 10
- [P] The final answer quotes the number of the issue it created

[D] is the state the agent left behind, [T] the calls it made, [P] what it said
(a model reads the final answer and decides). "!" after the letter marks a
criterion that must pass whatever the rest score. Write three to six criteria,
at least two of them [D], and no more than one [P]."""

_AUTHORING = """\
The rule that decides whether a scenario is worth running:

    A CRITERION MUST NOT BE SATISFIABLE BY AN AGENT THAT DID NOTHING.

The twins start already populated by the seed, so "an issue titled X exists" or
"at least 1 customer exists" can be true before the agent has done anything.
"Exactly 1 issue was created" cannot. Write [D] criteria against created.,
deleted. and changed. whenever the scenario is seeded, and keep bare existence
for the one case it belongs in: an empty seed, or a record the seed is known not
to hold.

Then:
- Keep every qualifier the task states (repository, label, assignee, channel,
  state). A criterion that drops one passes for work that was not asked for.
- One claim per criterion. Two claims joined by "and" cannot be scored apart.
- Include one [D!] guard against collateral damage, such as no records deleted."""

_LANGUAGE = r"""Pin the assertion after "=>" for every [D] and [T] criterion you can express. A
pinned assertion runs exactly as written; an unpinned criterion is translated by
a model at run time, which costs money and can translate it wrongly. Leave one
unpinned only when the language below cannot say it. [P] criteria never carry an
assertion: they are judged, not computed.

The assertion language. An assertion is one boolean expression.

Lists to read:
  <twin>.<collection>                state after the agent ran
  seed.<twin>.<collection>           state before it ran
  created.<twin>.<collection>        records it added
  deleted.<twin>.<collection>        records it removed, including soft deletes
  changed.<twin>.<collection>        records it modified
  trace                              every API call, with fields twin, method,
                                     path, status, ok, op (create/read/update/
                                     delete), resource, body, response
  egress                             connections to hosts outside the sandbox,
                                     with fields host, allowed
Values to read:
  answer                             the agent's final answer (a string)
  task                               the prompt it was given (a string)
  exit_code, duration                process facts; duration is in seconds

Selecting:
  <list>[<condition>]                keeps matching items; inside the brackets a
                                     bare name is a field of the item
  <list>[*].<field>                  collects one field from every item
  <list>.<field>                     a field of a list holding exactly one item.
                                     It ERRORS when the list is empty, so never
                                     guard a record with it — fold the field
                                     test into the filter and count instead.

Operators: == != < <= > >=, ~ and !~ (regex, written /pattern/flags), in and
not in and contains (membership, or substring for strings), && || ! (also
written and / or / not).
Functions: count(list), exists(list), any(list, condition),
all(list, condition), lower(s), upper(s), len(list or string).

Assertions that are correct today:
  count(created.github.issues) == 1
  count(deleted.github.issues) == 0
  exists(github.issues[title == "Login broken"])
  count(github.issues[number == 1 && state == "closed"]) == 1
  count(github.issues[number == 1 && "bug" in labels]) == 1
  count(trace[method == "DELETE"]) == 0
  count(trace) <= 10
  answer ~ /#\d+/

Comparisons are case-sensitive: use lower(...) or a /pattern/i regex when any
casing will do. Use only the collections and fields listed below, never an
invented one."""


def _system(twins: list[str], seed: str | None) -> str:
    parts = [_FORMAT, _AUTHORING, _LANGUAGE, _collections(twins), _seeds(twins, seed)]
    return "\n\n".join(part for part in parts if part)


def _collections(twins: list[str]) -> str:
    rows = _schema_payload(schema_for(twins))
    if not rows:
        return ""
    lines = [f"Collections {', '.join(twins)} expose (list | words for it | fields):"]
    incomplete = False
    for row in rows:
        fields = ", ".join(row["fields"])
        incomplete = incomplete or not fields
        lines.append(f"  {row['list']} | {', '.join(row['nouns']) or '-'} | {fields or '?'}")
    if incomplete:
        # A twin only has to declare the fields a criterion can filter on, and
        # most declare none until records exist. Counting a delta needs no field
        # name at all, which is also the assertion an idle agent cannot satisfy.
        lines.append(
            '"?" means the twin does not declare its field names up front. There, prefer\n'
            "  count(created.<twin>.<collection>) over a filter on a field you are guessing at."
        )
    return "\n".join(lines)


def _seeds(twins: list[str], seed: str | None) -> str:
    lines = []
    for name in twins:
        available = _seed_names(name)
        if available:
            lines.append(f"  {name}: {', '.join(available)}")
    if not lines:
        return ""
    head = ["Seeds that exist. The front matter's `seed:` must name one of these, "
            "or be left out."]
    if len(twins) > 1:
        head.append("With more than one twin, write `seed: <twin>=<name>, <twin>=<name>`.")
    tail = [f"Use `seed: {seed}`."] if seed else []
    return "\n".join([*head, *lines, *tail])


def _seed_names(twin: str) -> list[str]:
    try:
        return registry.get(twin).seed_names()
    except registry.UnknownTwinError:
        return []


# -- validation ---------------------------------------------------------------


# The parser still accepts the previous format's spellings, so an old-shaped
# draft would pass every other check and land in the repo in a format the
# templates, the docs and `checkpoint new` no longer write.
_RETIRED_SECTIONS = {"prompt": "Task", "success criteria": "Criteria"}
_HEADING = re.compile(r"^##[ \t]+(?P<name>.+?)[ \t]*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _problems(text: str, twins: list[str]) -> list[str]:
    """Everything that would make this draft unusable, phrased so a model can fix it."""
    scenario = parse_scenario(text)
    problems = list(scenario.problems)

    if not scenario.prompt.strip():
        problems.append("there is no '## Task' section, so the agent is given nothing to do")
    if not scenario.criteria:
        problems.append("there is no '## Criteria' section, so there is nothing to score")

    # A heading inside an HTML comment is a note to the author, not a section:
    # read the file the way the parser does before judging its shape.
    for match in _HEADING.finditer(_COMMENT.sub("", text)):
        heading = match.group("name").strip().lower()
        if heading in _RETIRED_SECTIONS:
            problems.append(
                f"'## {match.group('name').strip()}' is the old format; "
                f"that section is now '## {_RETIRED_SECTIONS[heading]}'"
            )
    if scenario.config.get("clones") is not None:
        problems.append("'clones:' is the old spelling of the setting; it is now 'twins:'")

    problems.extend(_twin_problems(scenario.twins, twins))
    problems.extend(_seed_problems(scenario.config.get("seed"), twins))
    return problems


def _twin_problems(named: list[str], expected: list[str]) -> list[str]:
    if not named:
        return ["the front matter has no 'twins:' setting, so no twin is started"]
    problems, resolved = [], []
    for name in named:
        try:
            spec = registry.get(name)
        except registry.UnknownTwinError as e:
            problems.append(f"the front matter names an unknown twin: {e}")
            continue
        resolved.append(spec.name)
        if spec.name not in expected:
            problems.append(
                f"the front matter names the {spec.name!r} twin, but this scenario "
                f"runs against {', '.join(expected)}"
            )
    missing = [name for name in expected if name not in resolved]
    if missing:
        problems.append(f"the front matter is missing twins: {', '.join(missing)}")
    return problems


def _seed_problems(raw: Any, twins: list[str]) -> list[str]:
    """A named seed must be one the twin ships, or the run fails at set-up."""
    if not raw or not twins:
        return []
    text = str(raw).strip()
    # A bare name seeds the first twin; per-twin seeds are `<twin>=<name>` pairs.
    pieces = text.split(",") if "=" in text else [f"{twins[0]}={text}"]
    problems = []
    for piece in pieces:
        twin, _, name = piece.partition("=")
        twin, name = twin.strip().lower(), name.strip()
        available = _seed_names(twin)
        if name and available and name not in available:
            problems.append(
                f"seed {name!r} does not exist for {twin}; available: {', '.join(available)}"
            )
    return problems


# -- pinned assertions --------------------------------------------------------


_PINNED = re.compile(
    r"^(?P<criterion>[ \t]*(?:[-*+]|\d+[.)])[ \t]+.*?)\s=>\s(?P<assertion>\S.*?)[ \t]*$"
)


def _drop_broken_assertions(text: str) -> str:
    """Remove pinned assertions that do not parse, and say so in the file.

    A criterion without an assertion is still scored — one is compiled at run
    time — whereas a criterion with an unparseable one is an error, and a run
    with an error has no verdict. So the criterion stays and the assertion goes,
    with a note, rather than the scenario going out broken.
    """
    broken: dict[str, str] = {}
    for criterion in parse_scenario(text).criteria:
        if criterion.assertion and criterion.assertion not in broken:
            try:
                parse_assertion(criterion.assertion)
            except ExprSyntaxError as e:
                broken[criterion.assertion] = str(e)
    if not broken:
        return text

    lines, removed = [], set()
    for line in text.splitlines():
        match = _PINNED.match(line)
        if match is not None and match.group("assertion") in broken:
            removed.add(match.group("assertion"))
            lines.append(match.group("criterion").rstrip())
        else:
            lines.append(line)
    listing = "\n".join(
        f"    {'removed' if assertion in removed else 'still above, remove it by hand'}: "
        f"{assertion}\n      {why}"
        for assertion, why in broken.items()
    )
    return "\n".join(lines).rstrip() + (
        "\n\n<!--\n"
        "  These pinned assertions do not parse, and an assertion that cannot be\n"
        "  evaluated makes the whole run an error rather than a failed check. The\n"
        "  criteria they were attached to are still scored - a model translates\n"
        "  them at run time. Write the assertion yourself after \"=>\" to keep the\n"
        "  check free and repeatable.\n\n"
        f"{listing}\n"
        "-->\n"
    )
