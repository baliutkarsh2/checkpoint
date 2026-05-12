"""`checkpoint init` — scaffold a Checkpoint integration in the current repo.

Idempotent: existing files are skipped with a notice; missing files are written.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Layout: (destination relative to target_dir, template filename)
# `None` template means "create an empty directory if missing".
SCAFFOLD: list[tuple[str, str | None]] = [
    (".claude/skills/checkpoint/SKILL.md", "skill.md"),
    (".claude/commands/checkpoint-test.md", "slash_command.md"),
    (".checkpoint.json", "checkpoint.json"),
    ("harness.py", "harness.py"),
    ("harness.json", "harness.json"),
    ("scenario.md", "scenario.md"),
]

TEMPLATES_DIR = Path(__file__).parent / "init_templates"


@dataclass
class InitResult:
    """Outcome of a scaffold run, returned to the CLI for rendering."""

    target_dir: Path
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def banner(self) -> str:
        """Friendly next-steps message printed after `checkpoint init`."""
        lines = [
            f"Initialized Checkpoint in {self.target_dir}",
            "",
            "Next steps:",
            "  1. export OPENAI_API_KEY=sk-...   (or set in your shell profile)",
            "  2. checkpoint run scenario.md",
            "  3. Open Claude Code in this repo and try /checkpoint-test",
            "",
            "Edit harness.py to wire in your agent.",
        ]
        return "\n".join(lines)


def scaffold(target_dir: Path | str = ".") -> InitResult:
    """Write all scaffold files into `target_dir`.

    Existing files are NEVER overwritten — they're reported in `skipped`.
    Caller decides whether to render `created` / `skipped` to the user.
    """
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    result = InitResult(target_dir=target)

    for rel_path, tpl_name in SCAFFOLD:
        dest = target / rel_path
        if dest.exists():
            result.skipped.append(rel_path)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if tpl_name is None:
            continue
        src = TEMPLATES_DIR / tpl_name
        if not src.is_file():
            raise FileNotFoundError(f"Missing scaffold template: {src}")
        shutil.copyfile(src, dest)
        # Make harness.py executable for nicer ergonomics.
        if rel_path.endswith("harness.py"):
            dest.chmod(0o755)
        result.created.append(rel_path)

    return result
