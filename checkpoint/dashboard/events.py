"""In-process pub/sub event bus + filesystem watcher.

The dashboard publishes named events ("run.created", "run.updated",
"clones.changed", "job.updated") to any number of asyncio subscribers. The
SSE endpoint in app.py creates one subscriber per connected client.

A background watcher polls RUNS_DIR + the clone registry for changes (mtime +
filename diff) and publishes events when it detects them. Polling beats
filesystem-watch APIs here because:
  - It's cross-platform (Windows, macOS, Linux behave identically)
  - Adds no external dependency (watchdog isn't worth ~MB for one path)
  - The poll interval (1s) is well below human perception
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("checkpoint.dashboard.events")


@dataclass
class Event:
    name: str
    data: dict[str, Any]


class EventBus:
    """Many-subscribers, last-N-buffered async pub/sub.

    Each subscriber gets its own asyncio.Queue. Slow subscribers are dropped
    when their queue overflows so a misbehaving client can't backpressure
    publishers.
    """

    QUEUE_MAX = 256

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[Event]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.QUEUE_MAX)
        async with self._lock:
            self._subs.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        async with self._lock:
            self._subs.discard(q)

    async def publish(self, name: str, data: dict[str, Any] | None = None) -> None:
        evt = Event(name=name, data=data or {})
        async with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # Slow subscriber: drop them. They'll reconnect via SSE retry.
                log.warning("dropping slow SSE subscriber (queue full)")
                with suppress(Exception):
                    self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


class FilesystemWatcher:
    """Polls runs_dir + clone registry; publishes events on changes."""

    def __init__(
        self,
        bus: EventBus,
        runs_dir: Path,
        clone_registry_path: Path | None,
        poll_interval: float = 1.0,
    ) -> None:
        self.bus = bus
        self.runs_dir = runs_dir
        self.clone_registry_path = clone_registry_path
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="dashboard-fs-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        prev_runs: dict[str, float] = self._snapshot_runs()
        prev_registry = self._read_registry()
        log.info(
            "fs watcher started: runs=%s registry=%s",
            self.runs_dir,
            self.clone_registry_path,
        )
        while not self._stop.is_set():
            try:
                cur_runs = self._snapshot_runs()
                created = set(cur_runs) - set(prev_runs)
                updated = {k for k in cur_runs if k in prev_runs and cur_runs[k] != prev_runs[k]}
                if created:
                    for run_id in created:
                        await self.bus.publish("run.created", {"run_id": run_id})
                if updated:
                    for run_id in updated:
                        await self.bus.publish("run.updated", {"run_id": run_id})
                prev_runs = cur_runs

                cur_registry = self._read_registry()
                if cur_registry != prev_registry:
                    await self.bus.publish(
                        "clones.changed", {"count": len(cur_registry)}
                    )
                    prev_registry = cur_registry
            except Exception as e:  # noqa: BLE001
                log.warning("fs watcher tick failed: %s", e)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass

    def _snapshot_runs(self) -> dict[str, float]:
        if not self.runs_dir.exists():
            return {}
        out: dict[str, float] = {}
        for p in self.runs_dir.glob("*.json"):
            try:
                out[p.stem] = p.stat().st_mtime
            except OSError:
                continue
        return out

    def _read_registry(self) -> dict[str, Any]:
        if not self.clone_registry_path or not self.clone_registry_path.exists():
            return {}
        try:
            return json.loads(self.clone_registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
