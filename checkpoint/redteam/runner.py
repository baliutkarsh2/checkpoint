"""Run an adversarial pack and report resistance per OWASP category."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..gate import GatePolicy, run_gate
from ..scenario import parse_file
from .catalog import category_for


@dataclass
class RedTeamEntry:
    scenario: str
    category: str | None
    classification: str
    passes: int
    n: int

    @property
    def resisted(self) -> bool:
        # The agent must *consistently* resist. A flaky or failing adversarial
        # scenario means the attack lands at least sometimes — a vulnerability.
        return self.classification == "stable_pass"


@dataclass
class RedTeamReport:
    entries: list[RedTeamEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def vulnerabilities(self) -> list[RedTeamEntry]:
        return [e for e in self.entries if not e.resisted]

    @property
    def exit_code(self) -> int:
        return 1 if self.vulnerabilities else 0


def collect_pack(scenarios_dir: Path) -> list[Path]:
    """Every scenario under `scenarios_dir` that declares an OWASP category."""
    return [
        p for p in sorted(scenarios_dir.rglob("*.md"))
        if category_for(parse_file(p)) is not None
    ]


def run_redteam(
    pack: list[Path],
    harness_cmd: list[str],
    policy: GatePolicy,
    *,
    judge_model: str = "gpt-4o-mini",
    progress=None,
) -> RedTeamReport:
    report = RedTeamReport()
    for path in pack:
        category = category_for(parse_file(path))
        result = run_gate(path, harness_cmd, policy, judge_model=judge_model, progress=progress)
        report.errors.extend(result.errors)
        for stat in result.scenarios:
            report.entries.append(
                RedTeamEntry(
                    scenario=stat.scenario,
                    category=category,
                    classification=stat.classification,
                    passes=stat.passes,
                    n=stat.n,
                )
            )
    return report
