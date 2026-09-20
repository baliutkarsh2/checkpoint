"""Translate a criterion into an assertion with an LLM — once, then cache it.

Patterns (:mod:`checkpoint.eval.nl`) only handle phrasings they match exactly, so
most real criteria reach here. A model is good at *translating* English into an
assertion and bad at being a stable verdict, which is why the translation — not
the verdict — is what it produces: the assertion is validated against the twins'
own schema, shown to the user, stored with the run, and cached, so every run of
a gate scores the criterion the same way and can be reviewed once.

A criterion that is not a claim about the world at all ("the agent was polite")
is reported as such and left to the judge.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .expr import World, evaluate
from .nl import Compiled, Schema

CACHE_PATH = Path(".checkpoint/cache/assertions.json")

# Bump when the language or the prompt changes in a way that invalidates cached
# translations.
COMPILER_VERSION = 2

_SYSTEM = """You translate one test criterion into a single assertion in Checkpoint's
assertion language. You never decide whether the criterion passed — you only translate.

The language:
  count(<list>) <op> <number>     op is == != < <= > >=
  exists(<list>)                  true when the list has at least one item
  <list>[<condition>]             keeps matching items; bare names are item fields
  <list>[*].<field>               collects one field from every item
  <list>.<field>                  a field of a list that holds exactly one item
  any(<list>, <condition>) / all(<list>, <condition>)
  lower(x) / upper(x)
  conditions combine with && || ! and compare with == != < <= > >= ~ (regex) in / contains

Lists you can read:
  <twin>.<collection>                    state after the agent ran
  seed.<twin>.<collection>               state before the agent ran
  created.<twin>.<collection>            records the agent added
  deleted.<twin>.<collection>            records the agent removed (including soft deletes)
  changed.<twin>.<collection>            records the agent modified
  trace                                  API calls: twin, method, path, status, ok, op
                                         (create/read/update/delete), resource, body, response
  egress                                 connections to hosts outside the sandbox: host, allowed
Values you can read: answer (the agent's final answer, a string), exit_code, duration (seconds).

Rules:
- Use only the collections and fields listed in the schema you are given. Never invent one.
- To check a field of one named record, put the field test inside the filter and count:
  count(github.issues[number == 1 && state == "open"]) == 1, NOT
  github.issues[number == 1].state == "open". The second form *errors* when the
  record is missing, so an agent that deleted it scores "could not be evaluated"
  instead of a failure. Only use <list>.<field> when the criterion itself has
  already established the list holds exactly one item.
- "was created"/"were deleted" mean created./deleted., NOT the total in final state.
- Quote strings exactly as the criterion writes them; comparisons are case-sensitive
  (use lower(...) or a /regex/i when the criterion clearly means any casing).
- Keep every qualifier the criterion states (repository, label, assignee, state, channel).
  If you cannot express one, do not drop it: answer with "assertion": null.
- If the criterion is a matter of judgement (tone, helpfulness, correctness of prose)
  rather than a checkable fact about state, calls or the answer, answer "assertion": null
  and say why in "reason".

Answer with JSON: {"assertion": "<assertion or null>", "reason": "<one short sentence>"}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "assertion": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["assertion", "reason"],
    "additionalProperties": False,
}


@dataclass
class CompileResult:
    compiled: Compiled | None
    """The assertion, or None when the criterion needs judgement."""
    reason: str = ""


class AssertionCache:
    """Compiled assertions on disk, keyed by criterion, schema and model."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or os.environ.get("CHECKPOINT_ASSERTION_CACHE") or CACHE_PATH)
        self._lock = threading.Lock()
        self._entries: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._entries is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._entries = data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                self._entries = {}
        return self._entries

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._load().get(key)

    def put(self, key: str, entry: dict) -> None:
        with self._lock:
            entries = self._load()
            entries[key] = entry
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
            except OSError:
                pass  # a read-only checkout still runs, just without caching


def cache_key(criterion: str, schema: Schema, model: str) -> str:
    signature = json.dumps(
        {
            "criterion": " ".join(criterion.split()),
            "model": model,
            "version": COMPILER_VERSION,
            "collections": sorted(
                f"{c.path}:{','.join(sorted(c.fields))}" for c in schema.collections
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha256(signature.encode()).hexdigest()[:32]


def compile_with_llm(
    criterion: str,
    schema: Schema,
    seed_world: World,
    *,
    model: str,
    cache: AssertionCache | None = None,
    complete: Any = None,
) -> CompileResult:
    """Translate ``criterion``; validate the result against ``schema``.

    ``seed_world`` is the state the run started from: evaluating the candidate
    against it catches assertions that name a collection or field that does not
    exist, without depending on what the agent happened to do.
    """
    cache = cache if cache is not None else AssertionCache()
    key = cache_key(criterion, schema, model)
    cached = cache.get(key)
    if cached is not None:
        assertion = cached.get("assertion")
        if not assertion:
            return CompileResult(None, cached.get("reason", "needs judgement"))
        return CompileResult(Compiled(assertion, "llm", cached.get("reason", "")), cached.get("reason", ""))

    if complete is None:
        from checkpoint.llm import complete_json as complete  # deferred: keeps import cost off runs

    attempts: list[str] = []
    for attempt in range(2):
        payload = {
            "criterion": criterion,
            "schema": _schema_payload(schema),
            "previous_attempts": attempts or None,
        }
        try:
            answer = complete(model=model, system=_SYSTEM, user=json.dumps(payload), schema=_SCHEMA)
        except Exception as e:  # noqa: BLE001 — a compiler failure must not fail the run
            return CompileResult(None, f"assertion compiler unavailable: {e}")
        assertion = (answer or {}).get("assertion")
        reason = str((answer or {}).get("reason") or "")
        if not assertion:
            cache.put(key, {"assertion": None, "reason": reason or "needs judgement"})
            return CompileResult(None, reason or "needs judgement")
        problem = validate(str(assertion), schema, seed_world)
        if problem is None:
            cache.put(key, {"assertion": str(assertion), "reason": reason, "criterion": criterion})
            return CompileResult(Compiled(str(assertion), "llm", reason), reason)
        attempts.append(f"{assertion} -> rejected: {problem}")
        if attempt == 1:
            return CompileResult(None, f"could not compile a valid assertion: {problem}")
    return CompileResult(None, "could not compile a valid assertion")


def validate(assertion: str, schema: Schema, seed_world: World) -> str | None:
    """Why ``assertion`` is not usable against this run, or None.

    Only mistakes that are wrong regardless of the agent's actions are rejected:
    an unknown collection or field, a call to an unknown function, bad syntax, or
    an expression that is not a true/false claim.
    """
    known = {c.path for c in schema.collections}
    for path in re.findall(r"\b(?:seed|created|deleted|changed)\.([a-z0-9_\-]+\.[a-z0-9_]+)", assertion):
        if path not in known:
            return f"unknown collection {path!r}; available: {', '.join(sorted(known))}"
    outcome = evaluate(assertion, seed_world)
    if outcome.status == "error" and outcome.kind == "schema":
        return outcome.detail
    return None


def _schema_payload(schema: Schema) -> list[dict]:
    return [
        {
            "list": c.path,
            "nouns": list(c.nouns),
            "fields": sorted(c.fields),
        }
        for c in schema.collections
    ]
