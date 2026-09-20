"""The README has to stay true, not stay short.

Its old guard asserted a line count and the presence of headings, which a stale
README passes as easily as an accurate one. These check the claims instead:
every command it tells you to run exists, every file it links to is there, and
none of the vocabulary we removed has crept back in.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from checkpoint.cli import SECTIONS

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
TEXT = README.read_text(encoding="utf-8")

COMMANDS = {entry.name for _, entries in SECTIONS for entry in entries}

# Words that named something Checkpoint no longer has. A reappearance means a
# doc was written against the old surface, or an old file came back.
RETIRED = {
    "harness.json": "agents are configured in checkpoint.toml, not a harness manifest",
    ".checkpoint.json": "replaced by checkpoint.toml",
    "--harness": "replaced by --command",
    "--docker": "Docker run mode was removed; interception is in-process",
    "checkpoint serve": "renamed to `checkpoint view`",
    "checkpoint validate": "renamed to `checkpoint check`",
    "checkpoint clone": "renamed to `checkpoint twins`",
    "checkpoint config": "removed; configuration lives in checkpoint.toml",
    "checkpoint whoami": "removed; `checkpoint doctor` reports the same things",
}


def test_every_command_the_readme_names_exists() -> None:
    # `from checkpoint import ...` is Python, not an invocation.
    named = set(re.findall(r"(?<!from )checkpoint ([a-z][a-z-]*)", TEXT)) - {"import"}
    unknown = named - COMMANDS
    assert not unknown, (
        f"README names commands that do not exist: {sorted(unknown)}. "
        f"Available: {sorted(COMMANDS)}"
    )


def test_every_command_appears_in_the_readme() -> None:
    """A command nobody can find is a command nobody uses."""
    missing = [name for name in COMMANDS if f"checkpoint {name}" not in TEXT]
    assert not missing, f"README never mentions: {sorted(missing)}"


@pytest.mark.parametrize("target", sorted(set(re.findall(r"\]\((?!https?:)([^)#]+)\)", TEXT))))
def test_relative_links_resolve(target: str) -> None:
    assert (ROOT / target).exists(), f"README links to {target}, which does not exist"


@pytest.mark.parametrize("phrase,why", sorted(RETIRED.items()))
def test_retired_vocabulary_stays_out(phrase: str, why: str) -> None:
    assert phrase not in TEXT, f"README still says {phrase!r}: {why}"


def test_install_command_names_the_distribution_not_the_squatted_name() -> None:
    """Installing the bare name gets somebody else's package, not this one."""
    installs = re.findall(r"pip install ([^\n`]+)", TEXT)
    for spec in installs:
        first = spec.split()[0].strip('"')
        assert first != "checkpoint", (
            "the bare name `checkpoint` on PyPI is an unrelated project; "
            "install `checkpoint-agents` or the git URL"
        )
