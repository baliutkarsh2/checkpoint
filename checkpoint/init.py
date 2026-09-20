"""What ``checkpoint init`` writes into a repository.

Four files at most, and none of them is code:

    checkpoint.toml                    how to start your agent, and how to test it
    scenarios/quickstart.md            one scenario, written to be edited
    .gitignore                         one line, so runs stay out of git
    .github/workflows/checkpoint.yml   the gate, on every pull request

Checkpoint never writes a harness, a wrapper or an adapter into your repo: it
starts the command that already runs your agent and reads what comes back. The
optional files are written only when the repository already has the directory
they belong in — a repo with no ``.github`` does not acquire one here.

Nothing is ever overwritten. Running init twice is a no-op that tells you so.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .llm import DEFAULT_MODEL
from .project import CONFIG_NAME, render_template

TEMPLATES_DIR = Path(__file__).parent / "init_templates"

CI_WORKFLOW = ".github/workflows/checkpoint.yml"
SKILL_FILE = ".claude/skills/checkpoint/SKILL.md"
GITIGNORE_ENTRY = ".checkpoint/"


@dataclass
class InitResult:
    target: Path
    command: str
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    """Files that already existed and were left exactly as they were."""

    @property
    def is_new(self) -> bool:
        return CONFIG_NAME in self.created

    @property
    def next_steps(self) -> list[str]:
        if not self.created:
            return ["Everything is already set up. Try: checkpoint run"]
        return [
            "checkpoint run          try the starter scenario",
            "checkpoint check        see how each criterion will be checked",
            "checkpoint gate         the verdict to put in CI",
        ]


def scaffold(
    target_dir: Path | str = ".",
    *,
    command: str,
    task_via: str = "env",
    task_arg: str | None = None,
    model: str = DEFAULT_MODEL,
    ci: bool | None = None,
    skill: bool | None = None,
) -> InitResult:
    """Write the scaffold into ``target_dir``.

    ``ci`` and ``skill`` default to writing those files only if the repository
    already has a ``.github`` / ``.claude`` directory. Pass True or False to
    decide explicitly.
    """
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    result = InitResult(target=target, command=command)

    _write(result, CONFIG_NAME,
           render_template(command, task_via=task_via, task_arg=task_arg, model=model))
    _copy(result, "scenarios/quickstart.md", "scenario.md")
    _append_gitignore(result)

    if ci if ci is not None else (target / ".github").is_dir():
        _copy(result, CI_WORKFLOW, "ci/checkpoint.yml")
    if skill if skill is not None else (target / ".claude").is_dir():
        _copy(result, SKILL_FILE, "skill.md")
    return result


def _write(result: InitResult, rel: str, content: str) -> None:
    dest = result.target / rel
    if dest.exists():
        result.kept.append(rel)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    result.created.append(rel)


def _copy(result: InitResult, rel: str, template: str) -> None:
    source = TEMPLATES_DIR / template
    if not source.is_file():  # pragma: no cover — a packaging fault, not a user error
        raise FileNotFoundError(f"missing packaged template: {source}")
    _write(result, rel, source.read_text(encoding="utf-8"))


def _append_gitignore(result: InitResult) -> None:
    """Keep run records out of git, without disturbing what is already ignored."""
    path = result.target / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if GITIGNORE_ENTRY in existing.splitlines():
        result.kept.append(".gitignore")
        return
    with path.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(f"\n# Checkpoint run records and cache\n{GITIGNORE_ENTRY}\n")
    result.created.append(".gitignore" if not existing else ".gitignore (one line added)")


def copy_ci_workflow(target_dir: Path | str) -> Path | None:
    """Write just the CI workflow. Returns the path, or None if it existed."""
    target = Path(target_dir).resolve()
    dest = target / CI_WORKFLOW
    if dest.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATES_DIR / "ci/checkpoint.yml", dest)
    return dest
