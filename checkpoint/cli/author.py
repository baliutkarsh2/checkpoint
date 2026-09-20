"""Writing scenarios: ``checkpoint new`` to start one, ``checkpoint check`` to
find out what it will actually do before you spend runs on it.

``check`` is the more useful of the two. It shows the assertion behind every
criterion, so you can see that "the refund went through" compiles to something
that reads the twin's state rather than the agent's own account of itself.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click
from rich import box
from rich.table import Table

from checkpoint.scenario import KNOWN_SETTINGS, Scenario, parse_file

from ._shared import console, fail, plain, project, resolve_targets

_TEMPLATE = """\
---
twins: [{twins}]
{seed}timeout: 120
---
# {title}

## Task

{task}

## Criteria

- [D] <what should be true of the twin afterwards>
- [D!] <something that must never happen, e.g. no records were deleted>
- [T] The agent made at most 10 calls
- [P] <what the final answer must say>

<!--
  [D] checks the state the agent left behind, [T] the calls it made, [P] what it
  said. "!" marks a criterion that must pass whatever the rest score.

  `checkpoint check` shows the assertion each criterion compiles to. Pin your own
  after "=>" when you want no ambiguity and no model in the loop:

  - [D] Exactly one issue was filed  =>  count(created.github.issues) == 1

  Write criteria an idle agent would fail. "An issue exists" can already be true
  of the seed; "exactly one issue was created" cannot.
-->
"""


@click.command("new")
@click.argument("task", nargs=-1, required=True)
@click.option("--twins", default="github", show_default=True, metavar="NAMES",
              help="Twins the scenario runs against, comma-separated.")
@click.option("--seed", default=None, metavar="NAME",
              help="Named dataset the twins start from. `checkpoint twins list` has them.")
@click.option("--out", "-o", "out_path", type=click.Path(dir_okay=False), default=None,
              metavar="PATH", help="Where to write it. [default: scenarios/<name>.md]")
@click.option("--draft", is_flag=True, default=False,
              help="Let the judge model draft the criteria, with assertions pinned "
                   "where it can. Review them — a drafted criterion is a suggestion.")
@click.option("--model", default=None, metavar="MODEL", help="Model to draft with.")
def new(task, twins, seed, out_path, draft, model):
    """Start a new scenario from a one-line description of the task.

    \b
        checkpoint new "File a bug in acme/webapp when a customer reports one"
        checkpoint new "Refund the last charge for a@b.com" --twins stripe --draft
    """
    proj = project()
    description = " ".join(task).strip()
    names = [n.strip() for n in twins.split(",") if n.strip()]
    _check_twins(names)

    path = Path(out_path) if out_path else proj.scenario_paths()[0] / f"{_slug(description)}.md"
    if path.exists():
        fail(f"{path} already exists", hint="Pass --out to write somewhere else.")

    if draft:
        content = _draft(proj, description, names, seed, model)
    else:
        content = _TEMPLATE.format(
            twins=", ".join(names),
            seed=f"seed: {seed}\n" if seed else "",
            title=_title(description),
            task=description if description.endswith((".", "?", "!")) else description + ".",
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    console.print(f"  [green]+[/green] {plain(path)}")
    console.print(f"[dim]Next: edit the criteria, then `checkpoint check {plain(path)}`.[/dim]")


@click.command("check")
@click.argument("targets", nargs=-1, type=click.Path())
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
def check(targets, as_json):
    """Check scenarios, and show how each criterion will be decided.

    TARGETS are scenario files or directories; with none, the project's
    scenarios. Reports anything that would make a run meaningless — a missing
    task, an unknown twin, a line that looks like a criterion but is not — and
    prints the assertion behind every deterministic check.

    Exits 1 if any scenario has an error.
    """
    proj = project()
    reports = [_inspect(path) for path in resolve_targets(proj, targets)]
    reports = [r for r in reports if r is not None]
    if not reports:
        fail("nothing to check: no file under those paths is a scenario")

    if as_json:
        click.echo(json.dumps(reports, indent=2))
    else:
        for report in reports:
            _render(report)
    sys.exit(0 if all(r["valid"] for r in reports) else 1)


# -- checking -----------------------------------------------------------------


def _inspect(path: Path) -> dict | None:
    """Everything wrong with one scenario, and how each criterion resolves."""
    from checkpoint.eval import schema_for
    from checkpoint.eval.nl import compile_criterion
    from checkpoint.twins import registry

    try:
        scenario = parse_file(path)
    except (OSError, ValueError) as e:
        return {"scenario": str(path), "title": "", "valid": False, "twins": [],
                "criteria": [], "errors": [f"cannot be read: {e}"], "warnings": []}
    if not scenario.runnable and not scenario.criteria:
        return None  # ordinary markdown that happens to live beside scenarios

    known = set(registry.names())
    errors = list(scenario.problems)
    warnings: list[str] = []
    if not scenario.prompt:
        errors.append("no task: add a '## Task' section saying what the agent should do")
    if not scenario.criteria:
        errors.append("no criteria: add a '## Criteria' section")
    for twin in scenario.twins:
        if twin.lower() not in known and twin.lower() not in ("gmail", "google", "gh"):
            errors.append(f"unknown twin {twin!r}; available: {', '.join(sorted(known))}")
    for key in scenario.config:
        if key not in KNOWN_SETTINGS:
            warnings.append(f"unknown setting {key!r} (ignored)")
    faults = scenario.config.get("faults")
    if isinstance(faults, dict):
        for name in faults:
            if name not in {t.lower() for t in scenario.twins}:
                warnings.append(f"faults for {name!r}, which this scenario does not run")

    workspace = _workspace_problem(scenario)
    if workspace is not None:
        errors.append(workspace)

    schema = (schema_for(scenario.twins, workspace=bool(scenario.workspace))
              if scenario.twins or scenario.workspace else None)
    criteria = []
    for criterion in scenario.criteria:
        assertion, source = criterion.assertion, "pinned" if criterion.assertion else ""
        if assertion is None and criterion.kind != "P" and schema is not None:
            compiled = compile_criterion(criterion.text, schema)
            if compiled is not None:
                assertion, source = compiled.assertion, compiled.source
        if assertion is None and criterion.kind != "P":
            warnings.append(
                f"{criterion.label} {criterion.text[:60]!r} has no deterministic check yet: "
                "the judge model will compile one at run time. Pin one with '=> <assertion>' "
                "to keep it free and repeatable.")
        criteria.append({
            "text": criterion.text, "kind": criterion.kind,
            "must_pass": criterion.must_pass, "assertion": assertion,
            "source": source or "judge",
        })

    return {"scenario": str(path), "title": scenario.title, "valid": not errors,
            "twins": list(scenario.twins), "runs": scenario.runs,
            "criteria": criteria, "errors": errors, "warnings": warnings}


def _workspace_problem(scenario: Scenario) -> str | None:
    """A `workspace:` that will not resolve, reported before a run wastes time on it."""
    from checkpoint.engine.run import scenario_workspace
    from checkpoint.engine.sandbox import SandboxError

    try:
        scenario_workspace(scenario)
    except SandboxError as e:
        return str(e)
    return None


_SOURCE_LABEL = {"pinned": "pinned", "pattern": "pattern", "llm": "compiled", "judge": "judged"}


def _render(report: dict) -> None:
    name = Path(report["scenario"]).name
    console.print()
    console.print(f"[bold]{plain(report['title'] or name)}[/bold]  "
                  f"[dim]{plain(name)} · {', '.join(report['twins']) or 'no twins'}[/dim]")

    if report["criteria"]:
        table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
        table.add_column("", width=5)
        table.add_column("Criterion", overflow="fold")
        table.add_column("Decided by", style="dim", overflow="fold")
        for c in report["criteria"]:
            label = f"[{c['kind']}{'!' if c['must_pass'] else ''}]"
            how = c["assertion"] or ("the judge model reads the final answer"
                                     if c["kind"] == "P" else "compiled at run time")
            table.add_row(label, plain(c["text"]),
                          f"{_SOURCE_LABEL.get(c['source'], c['source'])}: {plain(how)}")
        console.print(table)

    for message in report["errors"]:
        console.print(f"  [red]error:[/red] {plain(message)}")
    for message in report["warnings"]:
        console.print(f"  [yellow]warning:[/yellow] {plain(message)}")
    if report["valid"] and not report["warnings"]:
        judged = sum(1 for c in report["criteria"] if c["source"] == "judge")
        note = f", {judged} judged by a model" if judged else ", every check deterministic"
        console.print(f"  [green]ready[/green][dim]{note}.[/dim]")


# -- drafting -----------------------------------------------------------------


def _draft(proj, description: str, twins: list[str], seed: str | None, model: str | None) -> str:
    from checkpoint.scenario_gen import generate

    try:
        return generate(description, twins=twins, seed=seed, model=proj.judge_model(model))
    except Exception as e:  # noqa: BLE001 — a drafting failure is a message, not a stack
        fail(f"could not draft the scenario: {e}",
             hint="Write it by hand instead: drop --draft and edit the template.")
        raise


def _check_twins(names: list[str]) -> None:
    from checkpoint.twins import registry

    for name in names:
        try:
            registry.get(name)
        except registry.UnknownTwinError as e:
            fail(str(e))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "scenario"


def _title(text: str) -> str:
    return (text[:1].upper() + text[1:]).rstrip(".")
