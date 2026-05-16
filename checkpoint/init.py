"""`checkpoint init` — scaffold a Checkpoint integration in any agent repo.

The user does **not** write any Python. We ask for (or accept as a flag) the
shell command that already runs their agent, then write a tiny
``harness.json`` describing how to invoke it. Checkpoint takes care of
injecting the scenario task via env / arg / stdin.

What gets written (idempotent; existing files are skipped):
  - ``.checkpoint.json``           project-level defaults (clones, judge model)
  - ``harness.json``               the declarative invocation spec
  - ``scenarios/quickstart.md``    a starter scenario the user can edit
  - ``.gitignore`` entry           ``.checkpoint/`` (cache + run records)
  - ``.claude/skills/...``         optional Claude Code skill files

The legacy template-based path (anthropic/openai-agents/langchain/raw) still
works for users who want a Python harness scaffold — keep ``--template`` for
that. The new default is ``--command``-driven: no Python file is ever
written into the user's repo.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .harness_spec import DEFAULT_TASK_ENV, template_manifest


TaskVia = Literal["env", "arg", "stdin", "none"]

# Legacy Python-file templates (still supported via --template).
_HARNESS_TEMPLATES: dict[str, str] = {
    "raw": "harness.py",
    "anthropic": "anthropic_harness.py",
    "openai-agents": "openai_agents_harness.py",
    "langchain": "langchain_harness.py",
}

VALID_TEMPLATES: list[str] = list(_HARNESS_TEMPLATES)

# Files written regardless of mode (Python-harness vs zero-code).
_COMMON_SCAFFOLD: list[tuple[str, str | None]] = [
    (".claude/skills/checkpoint/SKILL.md", "skill.md"),
    (".claude/commands/checkpoint-test.md", "slash_command.md"),
    (".checkpoint.json", "checkpoint.json"),
    ("scenarios/quickstart.md", "scenario.md"),
]

TEMPLATES_DIR = Path(__file__).parent / "init_templates"

_TEMPLATE_HINTS: dict[str, str] = {
    "anthropic": "pip install anthropic",
    "openai-agents": "pip install openai-agents",
    "langchain": "pip install langchain langchain-openai requests",
}


@dataclass
class InitResult:
    target_dir: Path
    mode: Literal["zero-code", "python-template"] = "zero-code"
    template: str | None = None
    command: str | None = None
    task_via: TaskVia = "env"
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def banner(self) -> str:
        """Friendly next-steps message printed after `checkpoint init`."""
        lines = [
            f"Initialized Checkpoint in {self.target_dir}",
            "",
        ]
        if self.mode == "zero-code":
            lines.append(f"Agent command: {self.command!r}")
            lines.append(f"Task injection: via {self.task_via}")
            lines.append("")
            lines.append("Next steps:")
            lines.append("  1. export OPENAI_API_KEY=sk-...   (for the LLM judge)")
            lines.append("  2. checkpoint run scenarios/quickstart.md")
            lines.append("  3. checkpoint serve                 (browse runs in the dashboard)")
            lines.append("")
            lines.append("Edit harness.json to change the command, env, or task-injection mode.")
            lines.append("No Python file was written — your agent code stays untouched.")
        else:
            hint = _TEMPLATE_HINTS.get(self.template or "raw")
            lines.append("Next steps:")
            if hint:
                lines.append(f"  1. {hint}")
                lines.append("  2. export OPENAI_API_KEY=sk-...")
                lines.append("  3. checkpoint run scenarios/quickstart.md")
            else:
                lines.append("  1. export OPENAI_API_KEY=sk-...")
                lines.append("  2. checkpoint run scenarios/quickstart.md")
            lines.append("")
            lines.append(f"Edit harness.py to wire in your agent (template: {self.template}).")
        return "\n".join(lines)


def scaffold(
    target_dir: Path | str = ".",
    *,
    command: str | None = None,
    task_via: TaskVia = "env",
    task_env: str = DEFAULT_TASK_ENV,
    task_arg: str | None = None,
    dockerfile: str | None = None,
    name: str | None = None,
    template: str | None = None,
) -> InitResult:
    """Write the scaffold.

    Mode selection:
      - If ``command`` is set (default for v0.3+): writes ``harness.json``
        with the declarative spec. NO Python file is written.
      - If ``template`` is set: legacy path. Copies a starter ``harness.py``
        for users who want to write Python.

    Existing files are never overwritten.
    """
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    if not command and not template:
        # Default to zero-code with a placeholder command the user must edit.
        command = "python your_agent.py"

    if command and template:
        raise ValueError("pass --command OR --template, not both")

    if template and template not in _HARNESS_TEMPLATES:
        raise ValueError(
            f"Unknown template {template!r}. Choices: {', '.join(VALID_TEMPLATES)}"
        )

    mode: Literal["zero-code", "python-template"] = (
        "python-template" if template else "zero-code"
    )

    result = InitResult(
        target_dir=target,
        mode=mode,
        template=template,
        command=command,
        task_via=task_via,
    )

    # Common scaffold (skill files, .checkpoint.json, starter scenario).
    for rel_path, tpl_name in _COMMON_SCAFFOLD:
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
        result.created.append(rel_path)

    # Mode-specific files.
    if mode == "zero-code":
        _write_harness_json(target, result, command=command, task_via=task_via,
                            task_env=task_env, task_arg=task_arg,
                            dockerfile=dockerfile, name=name)
    else:
        _write_harness_py(target, result, template=template or "raw")

    # Ensure .checkpoint/ cache is gitignored — small quality-of-life.
    _ensure_gitignore_entry(target / ".gitignore", ".checkpoint/", result)

    return result


def _write_harness_json(
    target: Path,
    result: InitResult,
    *,
    command: str | None,
    task_via: TaskVia,
    task_env: str,
    task_arg: str | None,
    dockerfile: str | None,
    name: str | None,
) -> None:
    rel = "harness.json"
    dest = target / rel
    if dest.exists():
        result.skipped.append(rel)
        return
    manifest = template_manifest(
        command=command or "python your_agent.py",
        task_via=task_via,
        task_env=task_env,
        task_arg=task_arg,
        name=name or target.name,
        dockerfile=dockerfile,
    )
    dest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    result.created.append(rel)


def _write_harness_py(target: Path, result: InitResult, *, template: str) -> None:
    tpl_name = _HARNESS_TEMPLATES[template]
    src = TEMPLATES_DIR / tpl_name
    if not src.is_file():
        raise FileNotFoundError(f"Missing template: {src}")
    dest = target / "harness.py"
    if dest.exists():
        result.skipped.append("harness.py")
        return
    shutil.copyfile(src, dest)
    try:
        dest.chmod(0o755)
    except OSError:
        pass  # Windows or filesystem without exec bit — fine.
    result.created.append("harness.py")


def _ensure_gitignore_entry(path: Path, entry: str, result: InitResult) -> None:
    """Append a single line to .gitignore if it isn't already there."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if entry in existing.splitlines():
        return
    needs_newline = bool(existing) and not existing.endswith("\n")
    with path.open("a", encoding="utf-8") as fh:
        if needs_newline:
            fh.write("\n")
        fh.write(f"# Checkpoint cache + run records\n{entry}\n")
    if path.name not in [c for c in result.created]:
        result.created.append(".gitignore (appended)")
