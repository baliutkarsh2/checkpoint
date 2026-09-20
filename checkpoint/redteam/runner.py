"""Run an adversarial pack and report what the agent resisted.

The gate asks "does this work often enough to ship?". This asks the opposite
question about the same evidence: an attack that lands even occasionally is a
vulnerability, so a scenario has to pass *consistently* to count as resisted —
and a handful of runs cannot establish that. Anything the evidence does not
settle is reported as undecided, never as safe.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..engine import Agent, RunOptions
from ..gate import GatePolicy, run_gate
from ..llm import DEFAULT_MODEL
from ..scenario import parse_file
from .catalog import category_for


@dataclass
class RedTeamEntry:
    scenario: str
    category: str | None
    classification: str
    passes: int
    n: int
    min_runs: int = 0
    """Runs this scenario would need before resistance could be established."""

    @property
    def resisted(self) -> bool:
        """Proven to hold up: the lower confidence bound clears the bar."""
        return self.classification == "stable_pass"

    @property
    def landed(self) -> bool:
        """The attack got through at least once."""
        return self.passes < self.n

    @property
    def undecided(self) -> bool:
        """Nothing got through, but there were too few runs to call it resisted."""
        return not self.resisted and not self.landed


@dataclass
class RedTeamReport:
    entries: list[RedTeamEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def vulnerabilities(self) -> list[RedTeamEntry]:
        """Attacks that landed. Undecided scenarios are not counted as breaches."""
        return [e for e in self.entries if e.landed]

    @property
    def undecided(self) -> list[RedTeamEntry]:
        return [e for e in self.entries if e.undecided]

    @property
    def exit_code(self) -> int:
        """1 for a landed attack, 2 when the evidence settles nothing.

        Fail-closed, and for distinguishable reasons: "your agent let an attack
        through" and "you did not run this enough times to know" call for
        different actions, so they do not share an exit code.
        """
        if self.vulnerabilities:
            return 1
        return 2 if self.undecided else 0


def collect_pack(scenarios_dir: Path) -> list[Path]:
    """Every scenario under `scenarios_dir` that declares an OWASP category."""
    return [
        p for p in sorted(scenarios_dir.rglob("*.md"))
        if category_for(parse_file(p)) is not None
    ]


def run_redteam(
    pack: Sequence[Path],
    command: Sequence[str] | None = None,
    policy: GatePolicy | None = None,
    *,
    agent: Agent | None = None,
    options: RunOptions | None = None,
    judge_model: str = DEFAULT_MODEL,
    progress=None,
) -> RedTeamReport:
    """Run every scenario in ``pack`` and summarize resistance per category.

    ``agent`` and ``options`` are passed through to the gate unchanged, so the
    sandbox an attack runs in is the one the caller asked for — an adversarial
    scenario that needs ``egress="none"`` or a rate-limited twin gets it.
    """
    policy = policy or GatePolicy()
    report = RedTeamReport()
    for path in pack:
        category = category_for(parse_file(path))
        result = run_gate(path, command, policy, agent=agent, options=options,
                          judge_model=judge_model, progress=progress)
        report.errors.extend(result.errors)
        for stat in result.scenarios:
            report.entries.append(RedTeamEntry(
                scenario=stat.scenario,
                category=category,
                classification=stat.classification,
                passes=stat.passes,
                n=stat.n,
                min_runs=stat.min_runs,
            ))
    return report
