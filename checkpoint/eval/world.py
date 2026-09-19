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


def schema_for(twins: Sequence[str]) -> Schema:
    """The collections a scenario's twins expose, without starting them.

    Lets `validate` and the dashboard tell an author which criteria will be
    checked deterministically and which will need a model, before any run.
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
        views[spec.name] = {n: v.to_json() for n, v in twin.collection_views().items()}
    return Schema.from_views(views)
