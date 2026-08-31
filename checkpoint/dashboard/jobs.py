"""Background `checkpoint run` jobs spawned from the dashboard.

Each job is a `checkpoint run <scenario>` subprocess. We capture stdout+stderr
line-by-line, store the last N lines in a ring buffer, and broadcast each line
via the SSE bus so the LiveRun page can stream it without polling.

The job manager is intentionally in-process and ephemeral. There's no
persistence — restarting `checkpoint serve` clears running jobs. That matches
what users expect from a local dev tool.

Concurrency: one global asyncio.Semaphore caps how many jobs can run at once
so a user clicking "New run" twenty times doesn't fork-bomb the host.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import sys
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .events import EventBus

log = logging.getLogger("checkpoint.dashboard.jobs")

# Match a hex run-id basename of a .json file anywhere in a line. Survives
# Rich's whitespace-wrap of the "Run record: <path>" CLI output.
_RUN_RECORD_PATH_RE = re.compile(r"([0-9a-f]{12,})\.json")

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@dataclass
class Job:
    job_id: str
    scenario: str
    cmd: list[str]
    status: JobStatus = "queued"
    started_at: str = ""
    ended_at: str | None = None
    exit_code: int | None = None
    run_id: str | None = None
    log_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=2000))
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    log_listeners: set[asyncio.Queue[dict]] = field(default_factory=set)

    def public(self) -> dict:
        return {
            "job_id": self.job_id,
            "scenario": self.scenario,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "cmd": self.cmd,
        }


class JobManager:
    MAX_CONCURRENT = 4
    LISTENER_QUEUE_MAX = 1000

    def __init__(self, bus: EventBus, project_dir: Path) -> None:
        self.bus = bus
        self.project_dir = project_dir
        self._jobs: dict[str, Job] = {}
        self._sema = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._lock = asyncio.Lock()

    async def list(self) -> list[Job]:
        async with self._lock:
            return list(self._jobs.values())

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def start(
        self,
        scenario: str,
        *,
        docker: bool = False,
        harness_dir: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        clone: str | None = None,
        runs: int | None = None,
        rate_limit: int | None = None,
        read_only: bool = False,
        no_failure_analysis: bool = False,
        seed_file: str | None = None,
        setup_file: str | None = None,
        keep_state: bool = False,
        fresh_seed: bool = False,
        docker_logs: bool = False,
    ) -> Job:
        job_id = uuid.uuid4().hex
        cmd = [
            sys.executable,
            "-m",
            "checkpoint.cli",
            "run",
            scenario,
        ]
        if docker:
            cmd.append("--docker")
        else:
            cmd.append("--no-docker")
        if harness_dir:
            if docker:
                cmd.extend(["--harness-dir", harness_dir])
            else:
                # Subprocess mode: run the directory's harness.py. `harness_dir`
                # is a server-validated agent directory (never a client command),
                # and agent discovery guarantees harness.py exists. shlex.join
                # quotes safely so the CLI's shlex.split round-trips the path.
                entry = os.path.join(harness_dir, "harness.py")
                cmd.extend(["--harness", shlex.join([sys.executable, entry])])
        if model:
            cmd.extend(["--model", model])
        if timeout is not None:
            cmd.extend(["--timeout", str(timeout)])
        if clone:
            cmd.extend(["--clone", clone])
        if runs is not None:
            cmd.extend(["--runs", str(runs)])
        if rate_limit is not None:
            cmd.extend(["--rate-limit", str(rate_limit)])
        if read_only:
            cmd.append("--read-only")
        if no_failure_analysis:
            cmd.append("--no-failure-analysis")
        if seed_file:
            cmd.extend(["--seed-file", seed_file])
        if setup_file:
            cmd.extend(["--setup-file", setup_file])
        if keep_state:
            cmd.append("--keep-state")
        if fresh_seed:
            cmd.append("--fresh-seed")
        if docker_logs:
            cmd.append("--docker-logs")

        job = Job(
            job_id=job_id,
            scenario=scenario,
            cmd=cmd,
            started_at=datetime.now(tz=UTC).isoformat(),
        )
        async with self._lock:
            self._jobs[job_id] = job

        job.task = asyncio.create_task(self._run(job), name=f"job-{job_id[:8]}")
        await self.bus.publish("job.updated", job.public())
        return job

    async def cancel(self, job_id: str) -> Job | None:
        async with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.process is None:
            return job
        if job.status not in ("queued", "running"):
            return job
        with suppress(ProcessLookupError):
            job.process.terminate()
        return job

    async def subscribe_logs(self, job_id: str) -> asyncio.Queue[dict] | None:
        async with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=self.LISTENER_QUEUE_MAX)
        # Replay buffered lines to the new subscriber.
        for line in list(job.log_buffer):
            with suppress(asyncio.QueueFull):
                q.put_nowait({"event": "log", "line": line})
        if job.status not in ("queued", "running"):
            with suppress(asyncio.QueueFull):
                q.put_nowait({
                    "event": "ended",
                    "exit_code": job.exit_code,
                    "status": job.status,
                })
        job.log_listeners.add(q)
        return q

    async def unsubscribe_logs(self, job_id: str, q: asyncio.Queue[dict]) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            job.log_listeners.discard(q)

    async def _run(self, job: Job) -> None:
        async with self._sema:
            job.status = "running"
            await self.bus.publish("job.updated", job.public())
            await self._broadcast_log(job, f"$ {shlex.join(job.cmd)}")

            try:
                # Pass through API keys but keep CWD = project_dir so relative
                # scenario paths resolve as the user expects.
                env = dict(os.environ)
                proc = await asyncio.create_subprocess_exec(
                    *job.cmd,
                    cwd=str(self.project_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                job.process = proc

                assert proc.stdout is not None
                while True:
                    line_bytes = await proc.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                    await self._broadcast_log(job, line)
                    # Find a 12+ char hex run_id followed by .json anywhere on
                    # the line — robust against Rich's terminal-width wrapping
                    # of the "Run record: <path>" line.
                    m = _RUN_RECORD_PATH_RE.search(line)
                    if m:
                        job.run_id = m.group(1)

                rc = await proc.wait()
                job.exit_code = rc
                job.status = "succeeded" if rc == 0 else "failed"
            except asyncio.CancelledError:
                if job.process is not None:
                    with suppress(ProcessLookupError):
                        job.process.terminate()
                job.status = "cancelled"
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("job %s crashed", job.job_id)
                await self._broadcast_log(job, f"[harness error] {e}")
                job.status = "failed"
                job.exit_code = -1
            finally:
                job.ended_at = datetime.now(tz=UTC).isoformat()
                await self.bus.publish("job.updated", job.public())
                await self._broadcast_end(job)

    async def _broadcast_log(self, job: Job, line: str) -> None:
        job.log_buffer.append(line)
        msg = {"event": "log", "line": line}
        dead: list[asyncio.Queue[dict]] = []
        for q in list(job.log_listeners):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            job.log_listeners.discard(q)

    async def _broadcast_end(self, job: Job) -> None:
        msg = {"event": "ended", "exit_code": job.exit_code, "status": job.status}
        for q in list(job.log_listeners):
            with suppress(asyncio.QueueFull):
                q.put_nowait(msg)
