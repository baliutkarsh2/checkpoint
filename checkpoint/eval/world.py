"""Build the evaluation world from a run.

The twins describe their own collections (``/_views``: items, primary key, and
the field a soft delete sets), so the difference between the seed and the final
snapshot is computed per collection rather than guessed from HTTP verbs. That
diff is what makes "no issues were deleted" answerable — including for twins
that archive instead of deleting, which the old checker always scored as pass.
"""
from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any

from .expr import World
from .nl import Schema


def build_world(
    *,
    seed_views: Mapping[str, Mapping[str, dict]],
    final_views: Mapping[str, Mapping[str, dict]],
    trace: list[dict],
    answer: str = "",
    task: str = "",
    egress: list[dict] | None = None,
    exit_code: int | None = 0,
    duration: float = 0.0,
) -> World:
    return World(
        seed=_items(seed_views),
        final=_items(final_views),
        keys=_meta(final_views or seed_views, "key", "id"),
        tombstones=_meta(final_views or seed_views, "tombstone", None),
        trace=list(trace),
        egress=list(egress or []),
        answer=answer,
        task=task,
        exit_code=exit_code,
        duration=duration,
    )


def _items(views: Mapping[str, Mapping[str, dict]]) -> dict[str, dict[str, list[dict]]]:
    return {
        twin: {name: list(view.get("items") or []) for name, view in colls.items()}
        for twin, colls in views.items()
    }


def _meta(views: Mapping[str, Mapping[str, dict]], field: str, default: Any) -> dict[str, dict[str, Any]]:
    return {
        twin: {name: view.get(field, default) or default for name, view in colls.items()}
        for twin, colls in views.items()
    }


def schema_for(twins: Sequence[str], *, seed: str | None = None) -> Schema:
    """The collections a scenario's twins expose, without starting them.

    Lets ``checkpoint check``, the dashboard and the scenario generator tell an
    author what a criterion can refer to before anything runs.

    Field names come from a seeded copy of each twin's state, never from the
    live twin. A twin at rest holds nothing, so reading it directly would
    describe ``github.issues`` as a collection with no fields at all — enough to
    say the collection exists, not enough to compile "the issue is still open".
    With no ``seed`` named, every bundled seed contributes, so the result is
    every field a collection can carry rather than the ones one dataset happens
    to use.
    """
    from checkpoint.twins import registry

    views: dict[str, dict[str, dict]] = {}
    for name in twins:
        try:
            spec = registry.get(name)
            module = importlib.import_module(spec.app.partition(":")[0])
        except (KeyError, ImportError):
            continue
        twin = getattr(module, "TWIN", None)
        if twin is None:
            continue
        views[spec.name] = _sampled_views(twin, seed)
    return Schema.from_views(views)


def _sampled_views(twin: Any, seed: str | None) -> dict[str, dict]:
    """One twin's collections, with fields pooled over the seeds that apply."""
    names = [seed] if seed else twin.seed_names()
    samples = [twin.views_for(twin.seed(name)) for name in names]
    samples.append(twin.views_for())  # the empty twin, so nothing is missed
    merged: dict[str, dict] = {}
    for sample in samples:
        for collection, view in sample.items():
            described = view.to_json()
            existing = merged.get(collection)
            if existing is None:
                # Items are for field discovery only; a schema describes shape.
                merged[collection] = {**described, "items": []}
            else:
                existing["fields"] = sorted(set(existing["fields"]) | set(described["fields"]))
    return merged
