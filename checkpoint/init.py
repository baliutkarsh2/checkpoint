"""`checkpoint init` — scaffold a Checkpoint integration in the current repo.

Idempotent: existing files are skipped with a notice; missing files are written.

Templates
---------
``raw`` (default)
    Plain ``requests``-based stub — framework-agnostic starting point.
``anthropic``
    Claude agent via the Anthropic SDK with MCP tool-use.
``openai-agents``
    GPT agent via the OpenAI Agents SDK with MCPServerStreamableHttp.
``langchain``
    ReAct agent via LangChain + langchain-openai.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Harness template to use for each --template value.
_HARNESS_TEMPLATES: dict[str, str] = {
    "raw": "harness.py",
    "anthropic": "anthropic_harness.py",
    "openai-agents": "openai_agents_harness.py",
    "langchain": "langchain_harness.py",
}

VALID_TEMPLATES: list[str] = list(_HARNESS_TEMPLATES)

# Layout: (destination relative to target_dir, template filename)
# `None` template means "create an empty directory if missing".
_SCAFFOLD_BASE: list[tuple[str, str | None]] = [
    (".claude/skills/checkpoint/SKILL.md", "skill.md"),
    (".claude/commands/checkpoint-test.md", "slash_command.md"),
    (".checkpoint.json", "checkpoint.json"),
    ("harness.json", "harness.json"),
    ("scenario.md", "scenario.md"),
]

TEMPLATES_DIR = Path(__file__).parent / "init_templates"

# Install hints shown after init so users know what to pip-install.
_TEMPLATE_HINTS: dict[str, str] = {
    "anthropic": "pip install anthropic",
    "openai-agents": "pip install openai-agents",
    "langchain": "pip install langchain langchain-openai requests",
}


@dataclass
class InitResult:
    """Outcome of a scaffold run, returned to the CLI for rendering."""

    target_dir: Path
    template: str = "raw"
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def banner(self) -> str:
        """Friendly next-steps message printed after `checkpoint init`."""
        lines = [
            f"Initialized Checkpoint in {self.target_dir}",
            "",
            "Next steps:",
        ]
        hint = _TEMPLATE_HINTS.get(self.template)
        if hint:
            lines.append(f"  1. {hint}")
            lines.append("  2. export OPENAI_API_KEY=sk-...   (for the evaluator)")
            lines.append("  3. checkpoint run scenario.md")
        else:
            lines.append("  1. export OPENAI_API_KEY=sk-...   (or set in your shell profile)")
            lines.append("  2. checkpoint run scenario.md")
            lines.append("  3. Open Claude Code in this repo and try /checkpoint-test")
        lines.append("")
        lines.append("Edit harness.py to wire in your agent.")
        return "\n".join(lines)


def scaffold(target_dir: Path | str = ".", template: str = "raw") -> InitResult:
    """Write all scaffold files into `target_dir`.

    ``template`` selects which harness template to copy as ``harness.py``.
    Valid values: ``raw``, ``anthropic``, ``openai-agents``, ``langchain``.

    Existing files are NEVER overwritten — they're reported in ``skipped``.
    Caller decides whether to render ``created`` / ``skipped`` to the user.
    """
    if template not in _HARNESS_TEMPLATES:
        raise ValueError(
            f"Unknown template {template!r}. Valid choices: {', '.join(VALID_TEMPLATES)}"
        )

    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    result = InitResult(target_dir=target, template=template)

    harness_tpl = _HARNESS_TEMPLATES[template]
    scaffold_entries = _SCAFFOLD_BASE + [("harness.py", harness_tpl)]

    for rel_path, tpl_name in scaffold_entries:
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
        if rel_path.endswith("harness.py"):
            dest.chmod(0o755)
        result.created.append(rel_path)

    return result
