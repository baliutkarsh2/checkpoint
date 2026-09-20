"""A docs generator: gives every module in `src/` a docstring, and says so.

Deliberately not a model. The point of this example is the *harness* — an agent
that edits files is started inside the tree it is meant to edit, and is scored
on the diff it leaves — and a deterministic agent lets you watch that happen
with no API key and no dependencies.

The only Checkpoint-specific lines are the last two, exactly as in the other
examples: the task arrives in `$CHECKPOINT_TASK` and the answer goes to stdout.
Everything above them is an ordinary script that walks the current directory,
which is where the interesting part is: Checkpoint made the current directory a
disposable copy of `fixtures/small-repo`, so this runs against a real tree and
cannot damage anything.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

CHANGELOG = Path("CHANGELOG.md")


def needs_docstring(source: str) -> bool:
    try:
        return ast.get_docstring(ast.parse(source)) is None
    except SyntaxError:
        return False


def title(module: Path) -> str:
    return module.stem.replace("_", " ")


def document(module: Path) -> bool:
    """Add a module docstring if there is none. True if the file was changed."""
    source = module.read_text(encoding="utf-8")
    if not needs_docstring(source):
        return False
    module.write_text(f'"""The {title(module)} module."""\n{source}', encoding="utf-8")
    return True


def run(task: str) -> str:
    documented = sorted(m.as_posix() for m in Path("src").rglob("*.py") if document(m))
    if not documented:
        return "Every module already had a docstring; nothing to do."
    entry = "\n".join(f"- Documented `{path}`." for path in documented)
    CHANGELOG.write_text(f"# Changelog\n\n## Unreleased\n\n{entry}\n", encoding="utf-8")
    return f"Documented {len(documented)} module(s): {', '.join(documented)}."


if __name__ == "__main__":
    print(run(os.environ["CHECKPOINT_TASK"]))
