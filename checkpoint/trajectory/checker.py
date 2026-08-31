"""Evaluate `[T]` trajectory criteria against the agent's call path.

Deterministic and free — no LLM. Returns ``(passed, reasoning)`` where
``passed is None`` means the phrasing wasn't recognized (so the caller can fall
back). Supported phrasings (case-insensitive):

  - at most / no more than N [tool|api|http] calls
  - at least N calls
  - no failed / errored calls
  - no redundant calls
  - did not call / never called <METHOD>
  - at most N writes
"""
from __future__ import annotations

import re

from .metrics import TrajectoryMetrics
from .model import Trajectory

_WORD = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _num(tok: str) -> int:
    tok = tok.strip().lower()
    return _WORD.get(tok, int(tok)) if (tok.isdigit() or tok in _WORD) else 0


_N = r"(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)"


def check(criterion: str, traj: Trajectory, m: TrajectoryMetrics) -> tuple[bool | None, str]:
    c = criterion.strip().lower()

    mt = re.search(rf"(?:at most|no more than|<=|≤)\s+{_N}\s+(?:tool |api |http )?calls?", c)
    if mt:
        n = _num(mt.group(1))
        return (m.total_calls <= n, f"Made {m.total_calls} calls; allowed at most {n}.")

    mt = re.search(rf"(?:at least|>=|≥)\s+{_N}\s+(?:tool |api |http )?calls?", c)
    if mt:
        n = _num(mt.group(1))
        return (m.total_calls >= n, f"Made {m.total_calls} calls; required at least {n}.")

    mt = re.search(rf"(?:at most|no more than|<=|≤)\s+{_N}\s+writes?", c)
    if mt:
        n = _num(mt.group(1))
        return (m.write_calls <= n, f"Made {m.write_calls} writes; allowed at most {n}.")

    if re.search(r"no (?:failed|errored|error)\s+(?:api |http )?calls?", c):
        return (m.error_calls == 0, f"{m.error_calls} call(s) returned an error status.")

    if re.search(r"no redundant\s+(?:api |http )?calls?", c):
        return (m.redundant_calls == 0, f"{m.redundant_calls} redundant (repeated) call(s).")

    mt = re.search(r"(?:did not|didn't|never)\s+call(?:ed)?\s+(get|post|put|patch|delete)", c)
    if mt:
        method = mt.group(1).upper()
        count = m.methods.get(method, 0)
        return (count == 0, f"Agent made {count} {method} call(s).")

    return (None, "")
