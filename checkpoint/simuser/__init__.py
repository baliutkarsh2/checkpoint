"""Multi-turn simulated users.

A single prompt tests a single exchange. Real support and assistant agents live
across a conversation: the user pushes back, clarifies, changes their mind, gets
impatient. This package drives a persona-shaped simulated user through a
multi-turn conversation with the agent against stateful twins, so the agent's
actions accumulate turn over turn — then scores whether the goal was actually met.

Because LLM-simulated users are imperfect proxies for humans (see "Lost in
Simulation", 2026), each run reports a calibration confidence rather than
pretending the simulation is ground truth.
"""
from .engine import SimResult, simulate
from .persona import Persona, UserTurn
from .user import LLMSimulatedUser, ScriptedUser

__all__ = [
    "Persona", "UserTurn", "LLMSimulatedUser", "ScriptedUser",
    "SimResult", "simulate",
]
