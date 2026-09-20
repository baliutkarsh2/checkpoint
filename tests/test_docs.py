"""The docs have to stay true too, not just the README.

`tests/test_readme.py` holds the front page to its claims, and the pages under
`docs/` were held to nothing — which is how one came to say the dashboard
bundle is gitignored when it is committed, and another kept describing a file
layout that had moved. Both were found by reading, which does not scale and
does not run in CI.

These are the checks that can be made mechanically: every command a page tells
you to run exists, every relative link resolves, no page names a command or a
file Checkpoint has removed, and no page quotes an assertion in the shape the
evaluator treats as an error rather than a failure.

Two of those sweeps — links and commands — run over more than `docs/`, because
`docs/` was never the whole story. `examples/` and `packages/` ship to users as
material to copy, and the root pages are the first thing anyone reads; an audit
found real factual errors sitting in all three, which had survived precisely
because no test ever opened them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import checkpoint.project as project
from checkpoint.cli import SECTIONS
from tests.test_readme import RETIRED

ROOT = Path(__file__).resolve().parent.parent

# rglob, not glob. These sweeps are the only mechanical check these pages get,
# and a page that stops being swept stops being checked *silently*: CI still
# passes, so nothing announces the loss. Under `docs/*.md`, the day somebody
# filed a page under docs/guides/ it would have dropped out of the command,
# link, vocabulary and assertion checks all at once. The other corpus sweeps in
# this repo (tests/test_bundled_scenarios.py, tests/test_scenario_seeds_resolve.py)
# already use rglob for the same reason.
DOCS = sorted((ROOT / "docs").rglob("*.md"))

# node_modules is a vendored tree; its READMEs are not ours to police.
SHIPPED = sorted(
    page
    for directory in ("examples", "packages")
    for page in (ROOT / directory).rglob("*.md")
    if "node_modules" not in page.parts
)

# Root-level pages, minus README.md: tests/test_readme.py already holds that
# one to a stricter standard, and sweeping it twice only doubles the failures.
ROOT_PAGES = sorted(page for page in ROOT.glob("*.md") if page.name != "README.md")

# Pages whose `checkpoint <command>` invocations must name a command that
# exists. CHANGELOG.md is deliberately absent: recording that `checkpoint
# serve` was renamed is a changelog's whole job, so naming a removed command
# there is accurate rather than stale — the same exemption the vocabulary
# sweep below relies on.
COMMAND_PAGES = DOCS + SHIPPED + [p for p in ROOT_PAGES if p.name != "CHANGELOG.md"]

# Relative links are checked wherever they live. A dead link in CONTRIBUTING.md
# costs a first-time contributor exactly what one in docs/ costs a user.
LINK_PAGES = DOCS + SHIPPED + ROOT_PAGES

COMMANDS = {entry.name for _, entries in SECTIONS for entry in entries}

# Subcommands of a group: `checkpoint runs show`, `checkpoint twins start`. The
# command sweep below stops at the group, so these need naming here to avoid
# reading "show" as a top-level command that does not exist.
GROUPS = {"runs", "twins", "redteam", "cert"}


def _text(path: Path) -> str:
    # Several files in this tree are CRLF. read_text with an explicit encoding
    # copes; reading bytes and decoding by hand, or letting the platform pick
    # cp1252, mangles the em-dashes these pages are full of.
    return path.read_text(encoding="utf-8")


def _rel(path: Path) -> str:
    """Name the path, not the basename — four different pages are README.md."""
    return path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("page", COMMAND_PAGES, ids=_rel)
def test_every_command_a_page_names_exists(page: Path) -> None:
    """A page that tells you to run a command Checkpoint does not have is a dead end.

    Scoped on purpose to the literal `checkpoint <subcommand>` shape. These
    pages also tell you to run `docker`, `fly`, `git` and `npm`, whose
    subcommands and flags are not ours to validate, and a sweep that checked
    every flag named in prose would fail on `--detach` forever.
    """
    named = set(re.findall(r"(?<!from )checkpoint ([a-z][a-z-]*)", _text(page)))
    unknown = named - COMMANDS - {"import"}
    assert not unknown, (
        f"{_rel(page)} names commands that do not exist: {sorted(unknown)}. "
        f"Available: {sorted(COMMANDS)}")


@pytest.mark.parametrize("page", LINK_PAGES, ids=_rel)
def test_relative_links_resolve(page: Path) -> None:
    """A link to a file that moved is indistinguishable from one to a file that never existed."""
    targets = set(re.findall(r"\]\((?!https?:|mailto:)([^)#]+)\)", _text(page)))
    missing = [t for t in targets if not (page.parent / t).resolve().exists()]
    assert not missing, f"{_rel(page)} links to files that do not exist: {sorted(missing)}"


@pytest.mark.parametrize("page", DOCS, ids=_rel)
def test_retired_vocabulary_stays_out(page: Path) -> None:
    text = _text(page)
    # A changelog-shaped page is allowed to name what was removed; that is what
    # it is for. The docs describe the product as it is now.
    found = {phrase: why for phrase, why in RETIRED.items() if phrase in text}
    assert not found, (
        f"{_rel(page)} still refers to things Checkpoint removed: "
        + "; ".join(f"{p!r} ({w})" for p, w in sorted(found.items())))


@pytest.mark.parametrize("page", DOCS, ids=_rel)
def test_no_page_leaves_an_erroring_assertion_unexplained(page: Path) -> None:
    """Reading a field off a selection is the trap; a page may show it, once, to name it.

    `github.issues[...].state == "open"` ERRORs when the agent deleted the
    record, and an error makes the gate INCONCLUSIVE where it should BLOCK. The
    docs taught this shape as the way to guard a record once, which is how it
    reached forty-five bundled criteria.

    Showing it deliberately is different from recommending it, and scenarios.md
    does exactly that: the trap, then the counted form that fixes it. So the
    rule is not "never appears" but "never appears without its fix" — the same
    collection has to be shown counted further down the page.
    """
    lines = _text(page).splitlines()
    unexplained = []
    for i, line in enumerate(lines):
        match = re.search(r"\b([a-z_]+(?:\.[a-z_]+)+)\[[^\]]*\]\.[a-z_]+\s*(?:==|!=|~|<|>)", line)
        if not match or re.search(r"\b(count|any|all|exists)\s*\(", line):
            continue
        collection = match.group(1)
        fixed = any(f"count({collection}[" in later for later in lines[i:])
        if not fixed:
            unexplained.append(line.strip())
    assert not unexplained, (
        f"{_rel(page)} shows an assertion that errors rather than fails when the "
        f"record is missing, and never shows the counted form that fixes it:\n  "
        + "\n  ".join(unexplained))


def test_the_index_lists_every_page() -> None:
    """A page nobody links to is a page nobody reads."""
    index = _text(ROOT / "docs" / "README.md")
    # Compare the path relative to docs/, not the basename: that is the form
    # the index links by, and it keeps working when a page moves into a
    # subdirectory instead of quietly matching on a shared filename.
    pages = {p: p.relative_to(ROOT / "docs").as_posix() for p in DOCS}
    missing = [rel for p, rel in pages.items() if p.name != "README.md" and rel not in index]
    assert not missing, f"docs/README.md never links to: {sorted(missing)}"


# --- the configuration reference -------------------------------------------

REFERENCE = ROOT / "docs" / "configuration.md"


def _documented_keys() -> set[str]:
    """Every key named in a table row of the reference."""
    return set(re.findall(r"^\| `([a-z_]+)`", _text(REFERENCE), re.M))


SETTINGS = sorted(
    (section, key)
    for section, keys in {**project._SECTIONS, "twins.<name>": project._TWIN_KEYS}.items()
    for key in keys
)


@pytest.mark.parametrize("section,key", SETTINGS, ids=lambda v: v if isinstance(v, str) else v)
def test_every_setting_the_loader_accepts_is_documented(section: str, key: str) -> None:
    """A setting nobody can find is a setting nobody uses.

    checkpoint.toml rejects unknown keys, so a reader cannot discover a setting
    by guessing — the reference is the only way to know it exists. Five of them
    were reachable and undocumented before this page was written.
    """
    assert key in _documented_keys(), (
        f"[{section}] {key} is accepted by checkpoint.toml but absent from "
        f"docs/configuration.md, so nobody can find it")


def test_the_reference_documents_nothing_that_does_not_exist() -> None:
    """The other direction: a documented setting the loader would reject."""
    real = set().union(*project._SECTIONS.values(), project._TWIN_KEYS)
    # Environment variables and prose keys share the table shape; only compare
    # against names that look like settings, which is what `real` bounds.
    invented = {k for k in _documented_keys() if k.islower() and "_" in k or k.isalpha()} - real
    # Anything left must be a genuine non-setting row (there are none today).
    assert not invented - {"path", "paths"}, (
        f"docs/configuration.md documents settings checkpoint.toml would "
        f"reject: {sorted(invented)}")
