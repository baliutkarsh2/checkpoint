"""``checkpoint runs`` — what happened, after the fact.

Every run leaves a record: the score, each criterion and why the judge decided
it that way, every API call the agent made, and the state the twins were left
in. These commands read those records — to find a run, to see what the agent
did, to tell two builds apart, and to hand one to someone else.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich import box
from rich.table import Table

from checkpoint.run_record import RUNS_DIR

from ._shared import console, fail, mark, plain, score_color

_FILTER_WINDOW = 500
"""Rows to read from the index when ``--scenario`` has to be matched in Python."""


@click.group("runs")
def runs() -> None:
    """Past runs: list, show, compare, export.

    \b
        checkpoint runs list                # what has run lately
        checkpoint runs show                # the last run, in full
        checkpoint runs trace               # the calls it made
        checkpoint runs compare OLD NEW     # what changed between two runs

    Every command that takes a RUN_ID defaults to the most recent run, so the
    usual case needs no argument. Ids come from `checkpoint runs list`, and any
    unique prefix of one will do.
    """


@runs.command("list")
@click.option("-n", "--limit", type=int, default=20, show_default=True,
              help="Runs to show.")
@click.option("--scenario", default=None, metavar="SUBSTRING",
              help="Only runs whose scenario name contains this.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON array and nothing else.")
def list_runs(limit: int, scenario: str | None, as_json: bool) -> None:
    """Recent runs, newest first.

    \b
        checkpoint runs list
        checkpoint runs list -n 100 --scenario refund

    The first column is the id every other command here takes.
    """
    records = _recent(limit, scenario)
    if as_json:
        click.echo(json.dumps([_summary(r) for r in records], indent=2, default=str))
        return
    if not records:
        console.print(f"[dim]No runs match {scenario!r}.[/dim]" if scenario else
                      "[dim]No runs yet — `checkpoint run` records one.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Run", style="dim")
    table.add_column("Scenario", overflow="fold")
    table.add_column("Score", justify="right")
    table.add_column("Criteria", justify="right")
    table.add_column("When")
    for record in records:
        score = _score(record)
        passed, total = _criteria_count(record)
        color = score_color(score)
        table.add_row(_short(record), plain(record.get("scenario") or "-"),
                      f"[{color}]{score:.0f}[/{color}]", f"{passed}/{total}", _when(record))
    console.print(table)


@runs.command("show")
@click.argument("run_id", required=False)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print the whole record as JSON and nothing else.")
def show_run(run_id: str | None, as_json: bool) -> None:
    """One run in full: what it scored, and why each criterion held or did not.

    RUN_ID defaults to the most recent run.

    \b
        checkpoint runs show
        checkpoint runs show 4f1c2a9b --json | jq .state
    """
    record = _record(run_id)
    if as_json:
        click.echo(json.dumps(record, indent=2, default=str))
        return

    score = _score(record)
    passed, total = _criteria_count(record)
    facts = [f"{passed}/{total} criteria"]
    if record.get("evaluator_model"):
        facts.append(f"judge {record['evaluator_model']}")
    if record.get("duration_ms") is not None:
        facts.append(f"{float(record['duration_ms']) / 1000:.1f}s")
    facts.append(_when(record))

    console.print()
    console.print(f"[bold]{record.get('scenario') or 'untitled'}[/bold]  "
                  f"[dim]run {_short(record)}[/dim]")
    color = score_color(score)
    console.print(f"[{color}]{score:.0f}/100[/{color}]  [dim]{' · '.join(facts)}[/dim]")
    if record.get("error"):
        console.print(f"[red]{record['error']}[/red]")

    criteria = record.get("criteria") or []
    if criteria:
        table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
        table.add_column(width=4, justify="left")
        table.add_column(width=5)
        table.add_column(overflow="fold")
        table.add_column(style="dim", overflow="fold")
        for c in criteria:
            status = c.get("status") or ("pass" if c.get("passed") else "fail")
            label = f"[{c.get('kind', '?')}{'!' if c.get('must_pass') else ''}]"
            why = c.get("reasoning") or ""
            if c.get("assertion") and status != "pass":
                why = f"{why}\n{c['assertion']}" if why else c["assertion"]
            table.add_row(mark(status), label, plain(c.get("text", "")), plain(why))
        console.print(table)

    analysis = record.get("failure_analysis") or {}
    if analysis:
        console.print()
        console.print("[bold]Why it failed[/bold]")
        for criterion, why in analysis.items():
            console.print(f"  [yellow]{plain(criterion)}[/yellow]")
            console.print(f"  [dim]{plain(why)}[/dim]")

    states = _twin_states(record)
    if states:
        console.print()
        console.print("[bold]Twin state when the run ended[/bold]")
        for name, state in states:
            console.print(f"  [dim]{plain(name)}[/dim]  {plain(_state_line(state))}")

    calls = len(_events(record, None))
    console.print()
    console.print(f"[dim]{calls} API call{'' if calls == 1 else 's'} — "
                  f"`checkpoint runs trace {_short(record)}` lists them.[/dim]")


@runs.command("trace")
@click.argument("run_id", required=False)
@click.option("--twin", default=None, metavar="NAME", help="Only calls to this twin.")
@click.option("-n", "--limit", type=int, default=50, show_default=True,
              help="Calls to show.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print the matching calls as JSON and nothing else.")
def trace_run(run_id: str | None, twin: str | None, limit: int, as_json: bool) -> None:
    """Every API call the agent made, in the order it made them.

    This is the evidence behind a verdict: the requests that reached the twins
    and the status each one came back with. RUN_ID defaults to the most recent
    run.

    \b
        checkpoint runs trace
        checkpoint runs trace --twin github -n 200
    """
    record = _record(run_id)
    events = _events(record, twin)
    if as_json:
        click.echo(json.dumps(events[:limit], indent=2, default=str))
        return
    if not events:
        console.print(f"[dim]The agent made no calls to the {twin} twin.[/dim]" if twin else
                      "[dim]The agent made no API calls in this run.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("Twin")
    table.add_column("Method")
    table.add_column("Path", overflow="fold")
    table.add_column("Status", justify="right")
    for i, event in enumerate(events[:limit], start=1):
        status = event.get("status", event.get("status_code"))
        shown = "-" if status is None else str(status)
        if isinstance(status, int) and status >= 400:
            shown = f"[red]{shown}[/red]"
        table.add_row(str(i), plain(event.get("twin") or "-"),
                      plain(event.get("method") or event.get("type") or "-"),
                      plain(event.get("path") or event.get("url") or "-"), shown)
    console.print(table)
    if len(events) > limit:
        console.print(f"[dim]{len(events) - limit} more — raise the limit with -n.[/dim]")


@runs.command("compare")
@click.argument("run_a")
@click.argument("run_b")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
def compare_runs(run_a: str, run_b: str, as_json: bool) -> None:
    """What changed between two runs, criterion by criterion.

    RUN_A is the baseline and RUN_B the candidate: a criterion that passed in A
    and fails in B is a regression, and the other way round is a fix. Two runs
    of the same build differ too, so read a lone regression with `runs trend`
    beside it.

    \b
        checkpoint runs compare 4f1c2a9b 90ab12cd
    """
    from checkpoint.compare_diff import build_compare_diff

    rec_a = _record(run_a)
    rec_b = _record(run_b)
    diff = build_compare_diff(rec_a, rec_b)
    if as_json:
        click.echo(json.dumps(diff, indent=2, default=str))
        return

    delta = diff["delta"]
    color = "green" if delta > 0 else ("red" if delta < 0 else "dim")
    console.print()
    for label, record in (("baseline ", rec_a), ("candidate", rec_b)):
        score = _score(record)
        console.print(f"[dim]{label}[/dim] {_short(record)}  "
                      f"[{score_color(score)}]{score:.0f}/100[/{score_color(score)}]  "
                      f"[dim]{record.get('scenario') or '-'}[/dim]")
    console.print(f"[{color}]{delta:+g} points[/{color}]")

    _print_changes("Regressions  [dim]passed before, fails now[/dim]",
                   diff["regressions"], "fail")
    _print_changes("Fixes  [dim]failed before, passes now[/dim]", diff["fixes"], "pass")
    for title, key, field in (("Only in the candidate", "added", "candidate_passed"),
                              ("Only in the baseline", "removed", "baseline_passed")):
        if diff[key]:
            console.print()
            console.print(f"[bold]{title}[/bold]")
            for entry in diff[key]:
                console.print(f"  {mark('pass' if entry[field] else 'fail')} "
                              f"{plain(entry['text'])}")
    if not diff["regressions"] and not diff["fixes"]:
        console.print()
        console.print("[dim]No criterion changed its verdict.[/dim]")


@runs.command("trend")
@click.argument("scenario", required=False, default="")
@click.option("-n", "--limit", type=int, default=50, show_default=True,
              help="Runs to read.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
def trend_runs(scenario: str, limit: int, as_json: bool) -> None:
    """How often each criterion has passed lately, and which ones are flaky.

    SCENARIO is a substring of a scenario name; with none, every recent run
    counts. A criterion that both passes and fails across these runs is flaky:
    it is the one to fix before any single run means anything.

    \b
        checkpoint runs trend
        checkpoint runs trend refund -n 200
    """
    from checkpoint.analytics import compute_trend, detect_flaky, load_runs_for_scenario

    records = load_runs_for_scenario(scenario or "", RUNS_DIR, limit)
    trend = compute_trend(records)
    flaky = detect_flaky(trend)
    if as_json:
        click.echo(json.dumps({**trend, "flaky_criteria": flaky}, indent=2, default=str))
        return
    if not records:
        console.print(f"[dim]No runs match {scenario!r}.[/dim]" if scenario else
                      "[dim]No runs yet — `checkpoint run` records one.[/dim]")
        return

    console.print()
    console.print(f"[bold]{scenario or 'every scenario'}[/bold]  "
                  f"[dim]{trend['run_count']} runs · average {trend['avg_score']}/100 · "
                  f"range {trend['min_score']}-{trend['max_score']}[/dim]")
    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Criterion", overflow="fold")
    table.add_column("Kind", width=4)
    table.add_column("Passed", justify="right")
    table.add_column("Runs", justify="right")
    table.add_column("")
    for text, stats in sorted(trend["criteria"].items()):
        rate = stats["pass_rate"]
        color = "green" if rate >= 0.8 else ("yellow" if rate >= 0.5 else "red")
        table.add_row(text, str(stats["kind"]), f"[{color}]{rate:.0%}[/{color}]",
                      str(stats["total"]), "[yellow]flaky[/yellow]" if text in flaky else "")
    console.print(table)
    if flaky:
        console.print(f"[dim]{len(flaky)} flaky criteri{'on' if len(flaky) == 1 else 'a'}: "
                      f"a passing run proves little until these are steady.[/dim]")


@runs.command("export")
@click.argument("run_id", required=False)
@click.option("-o", "--output", required=True, type=click.Path(dir_okay=False),
              metavar="PATH", help="File to write the record to.")
@click.option("--anonymize", is_flag=True, default=False,
              help="Replace emails and credentials with placeholders first.")
def export_run(run_id: str | None, output: str, anonymize: bool) -> None:
    """Write a run's record to a JSON file.

    The record is everything Checkpoint knows about the run — criteria, trace,
    twin state, the agent's own output — so it travels with a bug report.
    --anonymize rewrites the values that identify a person or unlock an account
    to fixed placeholders, keeping the shape of the data so the trace is still
    worth reading.

    \b
        checkpoint runs export -o run.json
        checkpoint runs export 4f1c2a9b -o bug-report.json --anonymize
    """
    record = _record(run_id)
    if anonymize:
        record = _anonymize(record)
    try:
        Path(output).write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        fail(f"could not write {output}: {e}", code=1)
    console.print(f"[green]Wrote run {_short(record)} to {output}"
                  f"{' (anonymized)' if anonymize else ''}.[/green]")


@runs.command("otel")
@click.argument("spans_file", metavar="FILE", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
def otel(spans_file: str, as_json: bool) -> None:
    """Read an agent's path out of an OpenTelemetry trace.

    For an agent already instrumented with the GenAI semantic conventions and
    running somewhere Checkpoint did not start it — staging, production, another
    team's CI. The model and tool spans become the same path metrics a `[T]`
    criterion is scored against, so "how many steps did it take, how many calls
    failed" is one question whether the trace came from a twin or a collector.

    FILE is an exported OTLP document, a `{"spans": [...]}` object, or a bare
    list of spans.

    
        checkpoint runs otel traces.json
    """
    from checkpoint.trajectory import compute_metrics, from_otel_spans, spans_from_export

    try:
        data = json.loads(Path(spans_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail(f"could not read {spans_file}: {e}", code=1)
        return

    spans = spans_from_export(data)
    if not spans:
        fail(f"{spans_file} holds no spans",
             hint="Expected resourceSpans, a spans array, or a list of spans.", code=1)
    trajectory = from_otel_spans(spans)
    metrics = compute_metrics(trajectory)

    if as_json:
        click.echo(json.dumps({
            "spans": len(spans),
            "steps": len(trajectory),
            "metrics": metrics.as_dict(),
            "path": [f"{step.method} {step.path}" for step in trajectory.steps],
        }, indent=2, default=str))
        return

    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for key, value in metrics.as_dict().items():
        if isinstance(value, dict):
            value = ", ".join(f"{k}:{v}" for k, v in value.items()) or "-"
        table.add_row(plain(key.replace("_", " ")), plain(value))
    console.print(table)
    console.print(f"[dim]{len(spans)} span(s) read from {plain(spans_file)}.[/dim]")


# -- finding records -----------------------------------------------------------


def _record(run_id: str | None) -> dict:
    """The record RUN_ID names, or the last run. Exits with advice when neither.

    An id may be given whole or as any unique prefix of one, because the ids
    `runs list` prints are the ones people paste back.
    """
    from checkpoint.run_record import load_last_run

    record = _by_id(run_id) if run_id else load_last_run()
    if record is not None:
        return record
    if run_id:
        fail(f"no run matches {run_id!r}",
             hint="`checkpoint runs list` shows the ids you have.", code=1)
    fail("no runs recorded yet",
         hint="Run `checkpoint run` first — every run keeps a record.", code=1)
    raise SystemExit(1)  # unreachable; keeps type checkers honest


def _by_id(run_id: str) -> dict | None:
    """One record, from the index when it has it, else from its own file."""
    store = _store()
    if store is not None:
        try:
            record = store.get_run(run_id)
            if record is not None:
                return record
        except Exception:  # noqa: BLE001 — the files below are the durable copy
            pass
        finally:
            store.close()

    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        matches = sorted(RUNS_DIR.glob(f"{run_id}*.json")) if RUNS_DIR.is_dir() else []
        if len(matches) > 1:
            fail(f"{run_id!r} could mean {len(matches)} different runs",
                 hint="Did you mean " + ", ".join(p.stem for p in matches[:6]) + "?", code=1)
        if not matches:
            return None
        path = matches[0]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _recent(limit: int, scenario: str | None) -> list[dict]:
    """The newest records, through the index when there is a working one."""
    store = _store()
    if store is not None:
        try:
            return _from_store(store, limit, scenario)
        except Exception:  # noqa: BLE001 — a broken index must not hide the runs
            pass
        finally:
            store.close()
    return _from_files(limit, scenario)


def _store():
    """The SQLite index of the run records, brought up to date first.

    The JSON files are the durable artifact — a run writes one as it finishes,
    and the dashboard reads them. Nothing writes to SQLite at that moment, so
    the index is imported from the files whenever it has fallen behind: an
    index that knows half your runs is worse than none, and a migration
    somebody has to remember to run is one nobody runs.

    Returns ``None`` when SQLite cannot be used at all, and every caller then
    reads the files instead.
    """
    store = None
    try:
        from checkpoint.store import SqliteRunStore, migrate_json_runs

        store = SqliteRunStore()
        if store.count_runs() < _json_run_count():
            migrate_json_runs(RUNS_DIR, store)
        return store
    except Exception:  # noqa: BLE001 — no database is a reason to read files, not to fail
        if store is not None:
            store.close()
        return None


def _json_run_count() -> int:
    return sum(1 for _ in RUNS_DIR.glob("*.json")) if RUNS_DIR.is_dir() else 0


def _from_store(store, limit: int, scenario: str | None) -> list[dict]:
    # The index keys scenario names exactly while --scenario is a substring, so
    # the filter runs here, over a wider window of rows.
    rows = store.list_runs(limit=max(limit, _FILTER_WINDOW) if scenario else limit)
    out: list[dict] = []
    for row in rows:
        record = store.get_run(row["run_id"])
        if record is None or not _matches(record, scenario):
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


def _from_files(limit: int, scenario: str | None) -> list[dict]:
    if not RUNS_DIR.is_dir():
        return []
    files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _matches(record, scenario):
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


def _matches(record: dict, scenario: str | None) -> bool:
    return not scenario or scenario.lower() in (record.get("scenario") or "").lower()


# -- reading records -----------------------------------------------------------


def _events(record: dict, twin: str | None) -> list[dict]:
    """The API calls in a record, oldest first, each naming the twin it hit.

    A run against one twin records a flat list; a run against several records
    ``{twin: [calls]}``, and records written before the twins were named call
    that key ``clone``.
    """
    trace = record.get("trace") or []
    events: list[dict] = []
    if isinstance(trace, dict):
        for name, calls in trace.items():
            for event in calls or []:
                events.append({**event, "twin": event.get("twin") or name}
                              if isinstance(event, dict) else {"twin": name, "raw": event})
    else:
        for event in trace:
            events.append(dict(event) if isinstance(event, dict) else {"raw": event})

    for event in events:
        event["twin"] = (event.get("twin") or event.get("_twin")
                         or event.get("clone") or event.get("_clone") or "")
    # Merging several twins' logs only orders them correctly by their timestamps.
    if all(event.get("ts") for event in events):
        events.sort(key=lambda event: str(event["ts"]))
    return [e for e in events if e["twin"] == twin] if twin else events


def _twin_states(record: dict) -> list[tuple[str, dict]]:
    """What each twin was holding, as (twin, state) pairs.

    A run against several twins keys its state by twin name; a run against one
    stores that twin's collections at the top level. Only the names tell the
    two apart — ``{"issues": ...}`` is shaped exactly like ``{"github": ...}``.
    """
    from checkpoint.twins import registry

    state = record.get("state") or {}
    if not isinstance(state, dict) or not state:
        return []
    named = [n for n in (record.get("twins") or []) if n]
    if (set(state) <= set(named or registry.names())
            and all(isinstance(v, dict) for v in state.values())):
        return list(state.items())
    return [(_only_twin(record) or "state", state)]


def _only_twin(record: dict) -> str:
    """The twin a single-twin run used, from its own list or from its calls."""
    named = [n for n in (record.get("twins") or []) if n]
    if len(named) == 1:
        return named[0]
    seen = {event["twin"] for event in _events(record, None) if event["twin"]}
    return seen.pop() if len(seen) == 1 else ""


def _state_line(state: dict) -> str:
    """One twin's state as counts: what it held, and how much of each."""
    if state.get("_truncated"):
        return f"too large to record ({state.get('_size', 0)} bytes)"
    # Keys starting with an underscore are the twin's own knobs, not anything
    # the agent put there.
    counts = [f"{key} {len(value)}" for key, value in state.items()
              if isinstance(value, (list, dict)) and not key.startswith("_")]
    return " · ".join(counts[:8]) or "nothing"


def _criteria_count(record: dict) -> tuple[int, int]:
    criteria = record.get("criteria") or []
    return sum(1 for c in criteria if c.get("passed")), len(criteria)


def _score(record: dict) -> float:
    return float(record.get("satisfaction") or 0)


def _short(record: dict) -> str:
    return str(record.get("run_id") or "?")[:12]


def _when(record: dict) -> str:
    return (record.get("env") or {}).get("timestamp") or "-"


def _summary(record: dict) -> dict:
    passed, total = _criteria_count(record)
    return {
        "run_id": record.get("run_id"),
        "scenario": record.get("scenario"),
        "scenario_path": record.get("scenario_path"),
        "score": record.get("satisfaction"),
        "criteria_passed": passed,
        "criteria_total": total,
        "judge_model": record.get("evaluator_model"),
        "duration_ms": record.get("duration_ms"),
        "timestamp": _when(record),
        "error": record.get("error"),
    }


def _print_changes(title: str, entries: list[dict], status: str) -> None:
    if not entries:
        return
    console.print()
    console.print(f"[bold]{title}[/bold]")
    for entry in entries:
        console.print(f"  {mark(status)} {plain(entry['text'])}")


# -- sharing a record ----------------------------------------------------------

_PII = (
    (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "user@example.com"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "ghp-REDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9-_]{16,}\b"), "sk-REDACTED"),
)


def _anonymize(record: dict) -> dict:
    """A copy of the record with the values that identify someone replaced.

    Conservative substitutions only, so the shape survives and the trace stays
    useful for debugging: emails become user@example.com, GitHub tokens
    ghp-REDACTED, and OpenAI-shaped keys sk-REDACTED.
    """
    text = json.dumps(record, default=str)
    for pattern, replacement in _PII:
        text = pattern.sub(replacement, text)
    return json.loads(text)
