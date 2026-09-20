"""``checkpoint cert`` and ``checkpoint report`` — the verdict as evidence.

A gate verdict is a word on somebody's terminal until it is signed. A
certificate seals the verdict together with what produced it — the scenarios,
their pass rates and intervals, the policy that judged them, the agent and the
commit — so a reviewer who was not in the room can check that nothing was edited
afterwards, and can read what was actually tested instead of taking SHIP on
trust. The assurance report turns that certificate, plus a red-team run, into
the document a security or compliance reviewer asks for.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from ._shared import console, fail, verdict_color

_GRADE_COLOR = {"APPROVED": "green", "CONDITIONAL": "yellow", "REJECTED": "red"}


@click.group("cert")
def cert() -> None:
    """Read and verify signed gate certificates.

    There is no `cert issue`: a certificate is issued by the run that earned it,
    so that no certificate can exist without the evidence behind it.

    \b
        checkpoint gate --certificate build.cert.json   # issue
        checkpoint cert verify build.cert.json          # check
        checkpoint report --certificate build.cert.json # write it up
    """


@cert.command("verify")
@click.argument("cert_file", metavar="FILE", type=click.Path(exists=True, dir_okay=False))
def verify(cert_file):
    """Check a certificate's signature and expiry, and show what it claims.

    Exits 1 if the signature does not verify or the certificate has expired. An
    altered certificate proves nothing at all, and an expired one describes a
    build that is no longer the one in front of you.
    """
    from checkpoint.gate.certificate import is_expired
    from checkpoint.gate.certificate import verify as verify_signature

    certificate = _load(cert_file)
    signed = verify_signature(certificate)
    expired = is_expired(certificate)
    ok = signed and not expired

    subject = certificate.get("subject") or {}
    verdict = str(certificate.get("verdict", "?"))
    lines = [
        f"[bold]{'VALID' if signed else 'INVALID'}[/bold]  signature",
        f"verdict:  [{verdict_color(verdict)}]{verdict}[/{verdict_color(verdict)}]",
        f"agent:    {subject.get('agent', '?')}",
        f"gate id:  {certificate.get('gate_id', '?')}",
        f"issued:   {certificate.get('issued_at', '?')}",
        f"expires:  {certificate.get('expires_at', '?')}"
        + ("  [red](EXPIRED)[/red]" if expired else ""),
    ]
    console.print(Panel.fit("\n".join(lines), title="checkpoint cert verify",
                            border_style="green" if ok else "red"))
    _scenarios(certificate)
    sys.exit(0 if ok else 1)


@cert.command("show")
@click.argument("cert_file", metavar="FILE", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print the certificate exactly as stored.")
def show(cert_file, as_json):
    """Print what a certificate says, whether or not it verifies.

    `cert verify` decides; this one only reads, so it still works on a
    certificate that failed verification — which is when you most want to see
    what it claimed.
    """
    certificate = _load(cert_file)
    if as_json:
        click.echo(json.dumps(certificate, indent=2))
        return

    subject = certificate.get("subject") or {}
    policy = certificate.get("policy") or {}
    signature = certificate.get("signature") or {}
    verdict = str(certificate.get("verdict", "?"))
    console.print(Panel.fit(
        f"[bold {verdict_color(verdict)}]{verdict}[/bold {verdict_color(verdict)}]\n"
        f"agent:    {subject.get('agent', '?')}\n"
        f"command:  {subject.get('command') or subject.get('harness') or '?'}\n"
        f"commit:   {subject.get('commit_sha') or 'n/a'}\n"
        f"model:    {subject.get('model') or 'n/a'}\n"
        f"gate id:  {certificate.get('gate_id', '?')}\n"
        f"issued:   {certificate.get('issued_at', '?')}\n"
        f"expires:  {certificate.get('expires_at', '?')}\n"
        f"policy:   {policy.get('runs', '?')} runs, pass at "
        f"{policy.get('pass_threshold', '?')}, ship at {policy.get('ship_min', '?')}\n"
        f"signed:   {signature.get('alg', 'not signed')}",
        title=str(certificate.get("schema", "certificate")), border_style="white"))
    _scenarios(certificate)

    for entry in (certificate.get("evidence") or {}).get("skipped") or []:
        console.print(f"[dim]skipped {entry.get('path', '?')}: {entry.get('reason', '?')}[/dim]")
    console.print("[dim]Run `checkpoint cert verify` to check the signature.[/dim]")


@click.command("report")
@click.option("--certificate", "cert_path", required=True, metavar="PATH",
              type=click.Path(exists=True, dir_okay=False),
              help="A signed certificate from `checkpoint gate --certificate`.")
@click.option("--redteam", "redteam_path", default=None, metavar="PATH",
              type=click.Path(exists=True, dir_okay=False),
              help="A red-team report from `checkpoint redteam --json`.")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None, metavar="PATH",
              help="Also write the report as markdown here.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Print one JSON object and nothing else.")
def report(cert_path, redteam_path, out_path, as_json):
    """Build an Agent Assurance Report from a signed certificate.

    The verdict, the statistics behind it, the attacks that were run, and the
    cross-references a reviewer asks for (OWASP Agentic, NIST AI RMF, the EU AI
    Act's logging duties) — assembled into one document.

    \b
        checkpoint gate --certificate build.cert.json
        checkpoint redteam --json > redteam.json
        checkpoint report --certificate build.cert.json --redteam redteam.json \\
            --out assurance.md

    A certificate whose signature does not verify is graded REJECTED however
    good its numbers look, because those numbers may not be the ones that were
    signed. Exits 1 on REJECTED.
    """
    from checkpoint.compliance import build_assurance, render_markdown
    from checkpoint.gate.certificate import verify as verify_signature

    certificate = _load(cert_path)
    redteam = _load(redteam_path) if redteam_path else None

    assurance = build_assurance(certificate, redteam,
                                signature_valid=verify_signature(certificate))
    markdown = render_markdown(assurance)
    if out_path:
        try:
            Path(out_path).write_text(markdown, encoding="utf-8")
        except OSError as e:
            fail(f"cannot write {out_path}: {e}", code=1)

    grade = assurance["overall"]
    if as_json:
        click.echo(json.dumps(assurance, indent=2))
    else:
        console.print(markdown, highlight=False)
        color = _GRADE_COLOR.get(grade, "white")
        console.print(Panel.fit(f"[bold {color}]{grade}[/bold {color}]",
                                title="assurance", border_style=color))
        if out_path:
            console.print(f"[dim]Written to {out_path}.[/dim]")
    sys.exit(1 if grade == "REJECTED" else 0)


# -- reading ------------------------------------------------------------------


def _load(path) -> dict:
    """A JSON document, or a clean exit — never a traceback over a bad file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail(f"cannot read {path}: {e}", code=1)
        raise
    if not isinstance(data, dict):
        fail(f"{path} does not contain a JSON object", code=1)
        raise
    return data


def _scenarios(certificate: dict) -> None:
    """What the certificate covers. Absent from a hand-made or truncated one."""
    scenarios = (certificate.get("evidence") or {}).get("scenarios") or []
    if not scenarios:
        return
    table = Table(box=box.SIMPLE, show_edge=False)
    table.add_column("Scenario", overflow="fold")
    table.add_column("Pass", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("CI", justify="center")
    table.add_column("Reading")
    for s in scenarios:
        rate = float(s.get("pass_rate") or 0.0)
        table.add_row(
            str(s.get("scenario", "?")),
            f"{s.get('passes', '?')}/{s.get('n', '?')}",
            f"{rate:.0%}",
            f"[{float(s.get('ci_low') or 0.0):.0%}, {float(s.get('ci_high') or 0.0):.0%}]",
            str(s.get("classification", "?")).replace("_", " "),
        )
    console.print(table)
