"""Scoring a run: compile each criterion into an explicit assertion, then evaluate it.

A criterion is written in English. Before it can decide a verdict it becomes an
assertion over the run's world (:mod:`checkpoint.eval.expr`) — by pattern
(:mod:`checkpoint.eval.nl`), by the LLM compiler, or written by hand in the
scenario. The assertion is shown with the result and stored in the run record,
so a wrong translation is visible instead of silent, and an assertion that
cannot be evaluated is an error rather than a quiet failure.
"""
from .expr import Outcome, World, evaluate
from .nl import Collection, Compiled, Schema, compile_criterion
from .world import build_world, schema_for

__all__ = [
    "Collection",
    "Compiled",
    "Outcome",
    "Schema",
    "World",
    "build_world",
    "compile_criterion",
    "evaluate",
    "schema_for",
]
