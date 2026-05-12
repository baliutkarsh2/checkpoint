"""Parse Archal-compatible scenario markdown files."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

CriterionKind = Literal["D", "P"]

_D_KEYWORDS = re.compile(
    r"\b(exactly|at\s+least|at\s+most|exists?|created|closed|opened|merged|deleted|"
    r"remain|equals?|no\s+new|zero|none)\b",
    re.IGNORECASE,
)

_SECTION_ALIASES = {
    "setup": "setup",
    "prompt": "prompt",
    "task": "prompt",
    "expected behavior": "expected",
    "expected": "expected",
    "success criteria": "criteria",
    "checks": "criteria",
    "config": "config",
}


@dataclass
class Criterion:
    text: str
    kind: CriterionKind


@dataclass
class Scenario:
    title: str = ""
    setup: str = ""
    prompt: str = ""
    expected: str = ""
    criteria: list[Criterion] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    source_path: str | None = None

    @property
    def clones(self) -> list[str]:
        v = self.config.get("clones", "")
        return [c.strip() for c in v.split(",") if c.strip()]

    @property
    def runs(self) -> int:
        try:
            return max(1, int(self.config.get("runs", 1)))
        except (TypeError, ValueError):
            return 1

    @property
    def timeout(self) -> int:
        try:
            return int(self.config.get("timeout", 180))
        except (TypeError, ValueError):
            return 180


def parse(text: str, source: str | None = None) -> Scenario:
    s = Scenario(source_path=source)

    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if m:
        s.title = m.group(1).strip()

    parts = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        content = parts[i + 1] if i + 1 < len(parts) else ""
        section = _SECTION_ALIASES.get(heading)
        if not section:
            continue
        content = content.strip()
        if section == "setup":
            s.setup = content
        elif section == "prompt":
            s.prompt = content
        elif section == "expected":
            s.expected = content
        elif section == "criteria":
            s.criteria = _parse_criteria(content)
        elif section == "config":
            s.config = _parse_config(content)
    return s


def parse_file(path: str | Path) -> Scenario:
    p = Path(path)
    return parse(p.read_text(encoding="utf-8"), source=str(p))


def _parse_criteria(text: str) -> list[Criterion]:
    out: list[Criterion] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        body = line.lstrip("-* ").strip()
        kind: CriterionKind
        if body.startswith("[D]"):
            kind = "D"
            body = body[3:].strip()
        elif body.startswith("[P]"):
            kind = "P"
            body = body[3:].strip()
        else:
            kind = "D" if _D_KEYWORDS.search(body) else "P"
        if body:
            out.append(Criterion(text=body, kind=kind))
    return out


def _parse_config(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    return out
