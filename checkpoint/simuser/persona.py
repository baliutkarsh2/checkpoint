"""Persona + turn types for the simulated user."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Persona:
    """Who the simulated user is and how they behave.

    - ``goal``      the outcome they want (drives the first message and the
                    satisfied/gave-up decision).
    - ``tone``      how they write (terse, polite, frustrated, …).
    - ``patience``  how many of their own turns before they give up.
    - ``knowledge`` optional facts the user knows (an order id, a policy).
    - ``adversarial`` if true, the user applies social pressure / tries to talk
                    the agent past a policy boundary.
    """
    name: str
    goal: str
    tone: str = "neutral"
    patience: int = 4
    knowledge: str = ""
    adversarial: bool = False


@dataclass
class UserTurn:
    """The simulated user's decision for one turn."""
    message: str | None = None
    satisfied: bool = False
    gave_up: bool = False


def scenario_persona(scenario) -> Persona:
    """Derive a default persona from a scenario (its prompt is the goal)."""
    cfg = getattr(scenario, "config", None) or {}
    return Persona(
        name=str(cfg.get("persona") or "user"),
        goal=getattr(scenario, "prompt", "") or "",
        tone=str(cfg.get("tone") or "neutral"),
        patience=int(cfg.get("patience") or 4),
        adversarial=str(cfg.get("adversarial") or "").lower() in ("1", "true", "yes"),
    )
