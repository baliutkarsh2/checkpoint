"""A calibration confidence for a simulated conversation.

LLM-simulated users are imperfect proxies for humans: they give up faster,
persist less, and vary less than real people ("Lost in Simulation", 2026). So a
simulation result should not be reported as ground truth. `compute_calibration`
returns a confidence in [0, 1] estimating how human-like *this* conversation's
shape was — a transparent caveat on the score, not a claim of accuracy.

The heuristic is intentionally simple and legible; it is not a substitute for
validating personas against real transcripts, which is the proper fix. Down-
weighting low-confidence runs (or routing them to human review) is left to the
caller.
"""
from __future__ import annotations

from .persona import Persona


def compute_calibration(
    turns: int,
    max_turns: int,
    persona: Persona,
    satisfied: bool,
    gave_up: bool,
) -> float:
    if satisfied:
        # Resolving in a single turn is suspicious — either trivially easy or the
        # simulated user didn't really check. Multi-turn success is more credible.
        confidence = 0.7 if turns <= 1 else 0.9
    elif gave_up:
        # Giving up immediately is the classic simulated-user failure mode.
        confidence = 0.5 if turns <= 1 else 0.7
    else:
        # Hit the turn cap without a natural conclusion — inconclusive shape.
        confidence = 0.5

    if persona.adversarial:
        # Adversarial social pressure is harder to simulate faithfully.
        confidence *= 0.9

    return round(max(0.0, min(1.0, confidence)), 2)
