"""Canonical trajectory built from twin trace events.

A twin records each intercepted call as a dict with (at least) ``method`` and
``path``, and usually ``status``/``body``/``response``. Traces come in two
shapes depending on how many clones ran: a flat list for one clone, or
``{clone: [events]}`` for several. `Trajectory.from_trace` normalizes both.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class TrajectoryStep:
    index: int
    method: str
    path: str
    status: int | None = None
    clone: str | None = None
    body: object = None

    @property
    def is_write(self) -> bool:
        return self.method.upper() in _WRITE_METHODS

    @property
    def is_error(self) -> bool:
        return self.status is not None and self.status >= 400

    @property
    def signature(self) -> str:
        # method + path identifies a repeated call for redundancy detection.
        return f"{self.method.upper()} {self.path}"


@dataclass
class Trajectory:
    steps: list[TrajectoryStep] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.steps)

    @classmethod
    def from_trace(cls, trace) -> Trajectory:
        events: list[tuple[str | None, dict]] = []
        if isinstance(trace, dict):
            for clone, evs in trace.items():
                for ev in evs or []:
                    events.append((clone, ev))
        elif isinstance(trace, list):
            for ev in trace:
                events.append((None, ev))

        steps: list[TrajectoryStep] = []
        for i, (clone, ev) in enumerate(events):
            if not isinstance(ev, dict):
                continue
            method = str(ev.get("method", "")).upper()
            path = str(ev.get("path", ""))
            if not method and not path:
                continue
            status = ev.get("status")
            steps.append(
                TrajectoryStep(
                    index=i,
                    method=method,
                    path=path,
                    status=int(status) if isinstance(status, (int, float)) else None,
                    clone=clone,
                    body=ev.get("body", ev.get("request_body")),
                )
            )
        return cls(steps=steps)
