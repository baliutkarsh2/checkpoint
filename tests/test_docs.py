"""The docs have to stay true too, not just the README.

`tests/test_readme.py` holds the front page to its claims, and the seven pages
under `docs/` were held to nothing — which is how one came to say the dashboard
bundle is gitignored when it is committed, and another kept describing a file
layout that had moved. Both were found by reading, which does not scale and does
not run in CI.

These are the checks that can be made mechanically: every command a page tells
you to run exists, every relative link resolves, no page names a command or a
file Checkpoint has removed, and no page quotes an assertion in the shape the
evaluator treats as an error rather than a failure.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import checkpoint.project as project
from checkpoint.cli import SECTIONS
from tests.test_readme import RETIRED

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted((ROOT / "docs").glob("*.md"))
COMMANDS = {entry.name for _, entries in SECTIONS for entry in entries}

# Subcommands of a group: `checkpoint runs show`, `checkpoint twins start`. The
# command sweep below stops at the group, so these need naming here to avoid
# reading "show" as a top-level command that does not exist.
GROUPS = {"runs", "twins", "redteam", "cert"}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", DOCS, ids=lambda p: p.name)
def test_every_command_a_page_names_exists(page: Path) -> None:
    named = set(re.findall(r"(?<!from )checkpoint ([a-z][a-z-]*)", _text(page)))
    unknown = named - COMMANDS - {"import"}
    assert not unknown, (
        f"{page.name} names commands that do not exist: {sorted(unknown)}. "
        f"Available: {sorted(COMMANDS)}")


@pytest.mark.parametrize("page", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(page: Path) -> None:
    targets = set(re.findall(r"\]\((?!https?:|mailto:)([^)#]+)\)", _text(page)))
    missing = [t for t in targets if not (page.parent / t).resolve().exists()]
    assert not missing, f"{page.name} links to files that do not exist: {sorted(missing)}"


@pytest.mark.parametrize("page", DOCS, ids=lambda p: p.name)
def test_retired_vocabulary_stays_out(page: Path) -> None:
    text = _text(page)
    # A changelog-shaped page is allowed to name what was removed; that is what
    # it is for. The docs describe the product as it is now.
    found = {phrase: why for phrase, why in RETIRED.items() if phrase in text}
    assert not found, (
        f"{page.name} still refers to things Checkpoint removed: "
        + "; ".join(f"{p!r} ({w})" for p, w in sorted(found.items())))


@pytest.mark.parametrize("page", DOCS, ids=lambda p: p.name)
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
        f"{page.name} shows an assertion that errors rather than fails when the "
        f"record is missing, and never shows the counted form that fixes it:\n  "
        + "\n  ".join(unexplained))


def test_the_index_lists_every_page() -> None:
    """A page nobody links to is a page nobody reads."""
    index = _text(ROOT / "docs" / "README.md")
    missing = [p.name for p in DOCS if p.name != "README.md" and p.name not in index]
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
