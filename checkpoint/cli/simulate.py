"""``checkpoint simulate`` — the conversation, not the single prompt.

One prompt tests one exchange. Real users clarify, push back, change their mind
and run out of patience, and an agent that handles turn one can still lose the
thread by turn four. This drives a persona-shaped simulated user through a whole
conversation against one sandbox, so the agent's actions accumulate turn over
turn, then scores the state it left behind.

The user is played by a model, which is a worse proxy for a person than it
looks: it gives up earlier and varies less than a human would. Every run
therefore reports a calibration confidence next to the score, and neither number
is evidence that a real customer would have gone the same way.
"""
from __future__ import annotations

import json
import sys

import click
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from ._shared import (
    agent_options,
    console,
    mark,
    project,
    resolve_agent,
    resolve_options,
    sandbox_options,
    score_color,
)

#: The score a conversation must reach for the goal to count as met — the same
#: bar a single run clears to count as a pass under the default gate policy.
_GOAL_MET = 80.0


@click.command("simulate")
@click.argument("scenario_path", metavar="SCENARIO",
                type=click.Path(exists=True, dir_okay=False))
@agent_options
@sandbox_options
@click.option("--goal", default=None, metavar="TEXT",
              help="What the user is trying to get done. [default: the scenario's task]")
@click.option("--persona", "persona_name", default=None, metavar="NAME",
              help="Who the user is. [default: the scenario's `persona`, else 'user']")
@click.option("--tone", default=None, metavar="TONE",
              help="How the user writes: terse, polite, frustrated, ...")
@click.option("--patience", type=int, default=None, metavar="TURNS",
              help="The user's own turns before they give up. [default: 4]")
@click.option("--adversarial", is_flag=True, default=False,
              help="The user applies social pressure to get past a policy boundary.")
@click.option("--max-turns", type=int, default=6, show_default=True, metavar="N",
              help="Hard cap on the conversation.")
@click.option("--model", default=None, metavar="MODEL",
              help="Model for the simulated user and for [P] criteria.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
def simulate(scenario_path, command, url, task_via, task_env, task_arg, cwd, intercept,
             egress, allow_hosts, rate_limit, read_only, goal, persona_name, tone,
             patience, adversarial, max_turns, model, as_json):
    """Hold a multi-turn conversation with your agent and score the outcome.

    SCENARIO supplies the twins, the setup and the success criteria; its task
    becomes the user's goal unless --goal says otherwise. The conversation runs
    in one sandbox, so what the agent did on turn two is still there on turn
    five, and the criteria are checked against the final state.

    \b
        checkpoint simulate scenarios/refund.md
        checkpoint simulate scenarios/refund.md --tone frustrated --patience 2
        checkpoint simulate scenarios/refund.md --adversarial

    Exits 0 when the goal was met, 1 otherwise.
    """
    from checkpoint.scenario import parse_file
    from checkpoint.simuser import Persona
    from checkpoint.simuser import simulate as converse
    from checkpoint.simuser.persona import scenario_persona

    proj = project()
    scenario = parse_file(scenario_path)
    agent = resolve_agent(proj, command, url=url, task_via=task_via, task_env=task_env,
                          task_arg=task_arg, cwd=cwd)
    options = resolve_options(
        proj, judge_model=model, intercept=intercept, egress=egress,
        allow_hosts=allow_hosts, rate_limit=rate_limit, read_only=read_only)

    base = scenario_persona(scenario)
    persona = Persona(
        name=persona_name or base.name,
        goal=goal or base.goal,
        tone=tone or base.tone,
        patience=patience if patience is not None else base.patience,
        adversarial=adversarial or base.adversarial,
    )

    result = converse(scenario, None, persona, max_turns=max_turns,
                      judge_model=options.judge_model, agent=agent, options=options)
    met = result.error is None and result.result is not None and result.score >= _GOAL_MET

    if as_json:
        click.echo(json.dumps(_as_dict(result, persona, max_turns, met), indent=2, default=str))
    else:
        _render(result, persona, met)
    sys.exit(0 if met else 1)


# -- output -------------------------------------------------------------------


def _render(result, persona, met: bool) -> None:
    console.print()
    console.print(f"[bold]{escape(persona.name)}[/bold]  [dim]{escape(persona.tone)}"
                  f"{', adversarial' if persona.adversarial else ''} · "
                  f"wants: {escape(persona.goal)}[/dim]")
    _transcript(result.transcript)

    if result.error:
        console.print(f"  [red]{result.error}[/red]")

    criteria = result.result.criteria if result.result else []
    if criteria:
        table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
        table.add_column(width=4, justify="left")
        table.add_column(width=5)
        table.add_column(overflow="fold")
        table.add_column(style="dim", overflow="fold")
        for c in criteria:
            why = c.reasoning or ""
            if c.assertion and c.status != "pass":
                why = f"{why}\n{c.assertion}" if why else c.assertion
            label = f"[{c.kind}{'!' if c.must_pass else ''}]"
            table.add_row(mark(c.status), label, c.text, why)
        console.print(table)

    outcome = ("goal met" if result.satisfied else
               "user gave up" if result.gave_up else "ran out of turns")
    color = "green" if met else "yellow"
    console.print(Panel.fit(
        f"[bold {color}]{outcome}[/bold {color}]  [dim]{result.turns} turn(s)[/dim]\n"
        f"score        [{score_color(result.score)}]{result.score:.0f}/100[/"
        f"{score_color(result.score)}]\n"
        f"calibration  {result.calibration:.2f}\n"
        "[dim]Calibration is how human-shaped this conversation was, not how\n"
        "accurate the score is. A simulated user gives up earlier and varies\n"
        "less than a person, so both numbers carry that doubt.[/dim]",
        title="simulate", border_style=color))


def _transcript(transcript) -> None:
    """The conversation, with the two speakers kept apart at a glance."""
    for turn in transcript:
        speaker = turn.get("role") == "user"
        who = "[cyan]user [/cyan]" if speaker else "[magenta]agent[/magenta]"
        # An agent's own words are data: a stray bracket in them is not markup.
        body = escape((turn.get("content") or "").strip())
        first, *rest = body.splitlines() or [""]
        console.print(f"  {who}  {first}", highlight=False)
        for line in rest:
            console.print(f"         {line}", highlight=False)
    console.print()


def _as_dict(result, persona, max_turns: int, met: bool) -> dict:
    return {
        "persona": persona.name,
        "goal": persona.goal,
        "tone": persona.tone,
        "adversarial": persona.adversarial,
        "turns": result.turns,
        "max_turns": max_turns,
        "satisfied": result.satisfied,
        "gave_up": result.gave_up,
        "goal_met": met,
        "score": result.score,
        "calibration": result.calibration,
        "error": result.error,
        "transcript": result.transcript,
        "criteria": [{
            "text": c.text,
            "kind": c.kind,
            "status": c.status,
            "passed": c.passed,
            "reasoning": c.reasoning,
        } for c in (result.result.criteria if result.result else [])],
    }
