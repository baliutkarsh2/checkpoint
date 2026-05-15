"""FastAPI dashboard for checkpoint.

Architecture:
  /            -> SPA (Vite-built React bundle in static/)
  /api/*       -> JSON
  /api/events  -> SSE event stream
  /api/jobs/*  -> background `checkpoint run` jobs
  /api/docs    -> OpenAPI Swagger UI
  /healthz     -> liveness probe
  /metrics     -> Prometheus exposition format

Jinja2 page rendering is gone — the SPA owns all UI. The JSON API stays
backwards-compatible with the previous one (same field names) so external
scripts that called /api/* still work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..analytics import compute_trend, detect_flaky, load_runs_for_scenario
from ..compare_diff import build_compare_diff
from ..scenario import parse_file
from . import agents as agent_discovery
from .events import EventBus, FilesystemWatcher
from .jobs import JobManager
from .metrics import Metrics
from .middleware import (
    AccessLogMiddleware,
    BearerAuthMiddleware,
    RateLimitMiddleware,
    ReadOnlyJobsMiddleware,
    RequestIdMiddleware,
)

log = logging.getLogger("checkpoint.dashboard")

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Pure helpers — same behavior as the previous dashboard, just refactored
# to return data structures instead of HTML strings.
# ---------------------------------------------------------------------------

def _load_record(runs_dir: Path, run_id: str) -> dict | None:
    p = runs_dir / f"{run_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def _record_summary(rec: dict) -> dict:
    """Project a full record to the lighter summary shape used by /api/runs.

    Keeps responses small: a 100-run page is ~10kb instead of ~10mb if
    every record's full trace+state were inlined.
    """
    crits = rec.get("criteria") or []
    return {
        "run_id": rec.get("run_id", ""),
        "scenario": rec.get("scenario"),
        "satisfaction": float(rec.get("satisfaction") or 0),
        "criteria_pass": sum(1 for c in crits if c.get("passed")),
        "criteria_total": len(crits),
        "evaluator_model": rec.get("evaluator_model"),
        "timestamp": (rec.get("env") or {}).get("timestamp"),
        "exit_code": rec.get("exit_code"),
    }


def _list_runs(
    runs_dir: Path, scenario: str = "", per_page: int = 50, page: int = 1
) -> tuple[list[dict], int]:
    if not runs_dir.exists():
        return [], 0
    files = sorted(
        runs_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    pattern = scenario.lower()
    rows: list[dict] = []
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if pattern and pattern not in (rec.get("scenario") or "").lower():
            continue
        rows.append(rec)
    total = len(rows)
    start = (page - 1) * per_page
    sliced = rows[start : start + per_page]
    return [_record_summary(r) for r in sliced], total


def _build_summary(runs_dir: Path) -> dict:
    if not runs_dir.exists():
        return {
            "total_runs": 0,
            "avg_score_30d": 0,
            "pass_rate_30d": 0,
            "recent_fail_count": 0,
        }
    files = list(runs_dir.glob("*.json"))
    total = len(files)
    cutoff_30d = datetime.now(tz=timezone.utc) - timedelta(days=30)
    cutoff_7d = datetime.now(tz=timezone.utc) - timedelta(days=7)
    scores_30d: list[float] = []
    fail_7d = 0
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        ts_str = (rec.get("env") or {}).get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = None
        sat = float(rec.get("satisfaction", 0))
        if ts and ts >= cutoff_30d:
            scores_30d.append(sat)
        if ts and ts >= cutoff_7d and sat < 100:
            fail_7d += 1
    avg_30d = round(sum(scores_30d) / len(scores_30d), 1) if scores_30d else 0
    pass_rate_30d = (
        round(100 * sum(1 for s in scores_30d if s >= 100) / len(scores_30d), 1)
        if scores_30d
        else 0
    )
    return {
        "total_runs": total,
        "avg_score_30d": avg_30d,
        "pass_rate_30d": pass_rate_30d,
        "recent_fail_count": fail_7d,
    }


def _load_clones(registry_path: Path | None) -> list[dict]:
    if not registry_path or not registry_path.exists():
        return []
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8", errors="replace"))
        return [{"id": k, **v} for k, v in data.items()]
    except (json.JSONDecodeError, OSError):
        return []


def _build_scenario_summaries(scenarios_dir: Path) -> tuple[list[dict], dict]:
    try:
        from ..checker import PATTERNS as _checker_patterns
    except ImportError:
        _checker_patterns = []

    summaries: list[dict] = []
    total_d = 0
    stage1_hits = 0
    total_p = 0
    if not scenarios_dir.exists():
        return [], {"total_d": 0, "stage1_hits": 0, "stage1_pct": 0, "total_p": 0}
    for md in sorted(scenarios_dir.rglob("*.md")):
        try:
            scn = parse_file(md)
        except Exception:  # noqa: BLE001
            continue
        if not (scn.prompt or scn.criteria):
            continue
        d_crits = [c for c in scn.criteria if c.kind == "D"]
        p_crits = [c for c in scn.criteria if c.kind == "P"]
        d_hits = sum(
            1 for c in d_crits if any(pat.search(c.text) for pat, _ in _checker_patterns)
        )
        total_d += len(d_crits)
        stage1_hits += d_hits
        total_p += len(p_crits)
        cov_pct = round(100 * d_hits / len(d_crits)) if d_crits else 0
        summaries.append(
            {
                "title": scn.title or md.stem,
                "path": str(md.relative_to(scenarios_dir)),
                "clones": ", ".join(scn.clones) if scn.clones else "",
                "tags": scn.config.get("tags", "") or "",
                "d_count": len(d_crits),
                "p_count": len(p_crits),
                "coverage_pct": cov_pct,
            }
        )
    coverage = {
        "total_d": total_d,
        "stage1_hits": stage1_hits,
        "stage1_pct": round(100 * stage1_hits / total_d, 1) if total_d else 0,
        "total_p": total_p,
    }
    return summaries, coverage


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class StartJobBody(BaseModel):
    scenario: str = Field(..., description="Path to a scenario .md file.")
    docker: bool = False
    harness: str | None = Field(
        default=None,
        description="Optional --harness-dir to pass to checkpoint run --docker.",
    )

    # No extra_args / passthrough flags here on purpose. The dashboard is
    # local-dev tooling and we keep the public API surface minimal so a
    # `--host 0.0.0.0` developer cannot turn this into a flag-injection
    # vector against the spawned `checkpoint run` subprocess.
    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    runs_dir: Path,
    scenarios_dir: Path,
    clone_registry_path: Path | None = None,
    project_dir: Path | None = None,
    judge_model_default: str = "gpt-4o-mini",
) -> FastAPI:
    bus = EventBus()
    watcher = FilesystemWatcher(bus, runs_dir, clone_registry_path)
    project_dir = project_dir or scenarios_dir.parent
    jobs = JobManager(bus, project_dir=project_dir)
    metrics = Metrics()

    try:
        import checkpoint as _ckpt_mod
        _version = getattr(_ckpt_mod, "__version__", "0.0.1")
    except ImportError:
        _version = "0.0.1"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await watcher.start()
        try:
            yield
        finally:
            await watcher.stop()

    app = FastAPI(
        title="checkpoint dashboard",
        version=_version,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
        description=(
            "Local dashboard + JSON API for checkpoint. The SPA frontend lives "
            "at `/` (built bundle in dashboard/static/); this OpenAPI doc covers "
            "the JSON surface that the SPA — and any external script — consumes."
        ),
    )

    # Middleware order matters: outermost runs first on request, last on response.
    # Auth + read-only run BEFORE access log so blocked requests still log.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RateLimitMiddleware, max_writes=30, window_s=10.0)
    app.add_middleware(ReadOnlyJobsMiddleware)
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # -----------------------------------------------------------------------
    # Health, metrics, meta
    # -----------------------------------------------------------------------

    @app.get("/healthz", tags=["system"])
    def healthz():
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse, tags=["system"])
    async def prometheus_metrics():
        metrics.set_sse_subscribers(bus.subscriber_count)
        return metrics.render()

    @app.get("/api/meta", tags=["system"])
    def meta(request: Request):
        return {
            "version": _version,
            "host": request.headers.get("host", "127.0.0.1"),
            "runs_dir": str(runs_dir),
            "scenarios_dir": str(scenarios_dir),
            "judge_model_default": judge_model_default,
        }

    # -----------------------------------------------------------------------
    # Runs
    # -----------------------------------------------------------------------

    @app.get("/api/runs", tags=["runs"])
    def api_runs(
        scenario: str = "",
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=500),
    ):
        rows, total = _list_runs(runs_dir, scenario, per_page, page)
        return {"rows": rows, "total": total, "page": page, "per_page": per_page}

    @app.get("/api/runs/{run_id}", tags=["runs"])
    def api_run_detail(run_id: str):
        record = _load_record(runs_dir, run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return record

    @app.get("/api/summary", tags=["runs"])
    def api_summary():
        return _build_summary(runs_dir)

    # -----------------------------------------------------------------------
    # Scenarios
    # -----------------------------------------------------------------------

    @app.get("/api/scenarios", tags=["scenarios"])
    def api_scenarios(path: str | None = None):
        target = Path(path).resolve() if path else scenarios_dir
        scenario_list, coverage = _build_scenario_summaries(target)
        return {"scenarios": scenario_list, "coverage": coverage}

    # -----------------------------------------------------------------------
    # Reports + compare
    # -----------------------------------------------------------------------

    @app.get("/api/report", tags=["reports"])
    def api_report(scenario: str = "", limit: int = Query(50, ge=1, le=500)):
        runs = load_runs_for_scenario(scenario, runs_dir, limit=limit)
        trend = compute_trend(runs)
        flaky = detect_flaky(trend)
        return {**trend, "flaky_criteria": flaky}

    @app.get("/api/compare", tags=["reports"])
    def api_compare(a: str, b: str):
        rec_a = _load_record(runs_dir, a)
        rec_b = _load_record(runs_dir, b)
        if not rec_a or not rec_b:
            raise HTTPException(status_code=404, detail="one or both runs not found")
        return {"rec_a": rec_a, "rec_b": rec_b, "diff": build_compare_diff(rec_a, rec_b)}

    # -----------------------------------------------------------------------
    # Live clones
    # -----------------------------------------------------------------------

    @app.get("/api/clones", tags=["clones"])
    def api_clones():
        return _load_clones(clone_registry_path)

    @app.get("/api/agents", tags=["agents"])
    def api_agents():
        """Auto-discovered harness directories the RunLauncher can pick from.

        Scans examples/agents/, harness/, and agents/ under project_dir for
        any directory containing both a Dockerfile and a harness.py. Result
        is cached for 5s to avoid hammering the FS on dropdown re-renders.
        """
        return agent_discovery.discover(project_dir or Path.cwd())

    # -----------------------------------------------------------------------
    # Jobs (start/list/get/cancel + log SSE)
    # -----------------------------------------------------------------------

    @app.post("/api/jobs", status_code=201, tags=["jobs"])
    async def start_job(body: StartJobBody):
        # Resolve the scenario path. The SPA dropdown returns paths relative
        # to scenarios_dir (e.g. "github-happy-path.md") but the spawned
        # `checkpoint run` runs with cwd=project_dir, so a bare filename won't
        # be found. Try in order: cwd-relative, scenarios_dir-relative,
        # absolute. Reject early with a clear 400 instead of letting the CLI
        # error out 30s later.
        candidates = [
            (project_dir or Path.cwd()) / body.scenario,
            scenarios_dir / body.scenario,
            Path(body.scenario),
        ]
        resolved: Path | None = None
        for c in candidates:
            if c.is_file():
                resolved = c.resolve()
                break
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"scenario not found: {body.scenario!r}. "
                    f"Tried {[str(c) for c in candidates]}"
                ),
            )
        # Pass the absolute path through so the spawned subprocess finds it
        # regardless of cwd.
        job = await jobs.start(
            str(resolved),
            docker=body.docker,
            harness_dir=body.harness,
        )
        return job.public()

    @app.get("/api/jobs", tags=["jobs"])
    async def list_jobs():
        return [j.public() for j in await jobs.list()]

    @app.get("/api/jobs/{job_id}", tags=["jobs"])
    async def get_job(job_id: str):
        job = await jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job.public()

    @app.delete("/api/jobs/{job_id}", tags=["jobs"])
    async def cancel_job(job_id: str):
        job = await jobs.cancel(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job.public()

    @app.get("/api/jobs/{job_id}/stream", tags=["jobs"])
    async def stream_job(job_id: str, request: Request):
        q = await jobs.subscribe_logs(job_id)
        if q is None:
            raise HTTPException(404, "job not found")

        async def gen() -> AsyncIterator[dict]:
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        # SSE comment as a heartbeat — keeps proxies happy.
                        yield {"event": "ping", "data": "1"}
                        continue
                    evt = msg.get("event", "log")
                    payload = {k: v for k, v in msg.items() if k != "event"}
                    yield {"event": evt, "data": json.dumps(payload)}
                    if evt == "ended":
                        return
            finally:
                await jobs.unsubscribe_logs(job_id, q)

        return EventSourceResponse(gen())

    # -----------------------------------------------------------------------
    # Global event stream (run/clone updates)
    # -----------------------------------------------------------------------

    @app.get("/api/events", tags=["events"])
    async def events(request: Request):
        q = await bus.subscribe()

        async def gen() -> AsyncIterator[dict]:
            try:
                # Initial hello so the client sees an open connection promptly.
                yield {"event": "hello", "data": json.dumps({"ts": time.time()})}
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": "1"}
                        continue
                    yield {"event": evt.name, "data": json.dumps(evt.data)}
            finally:
                await bus.unsubscribe(q)

        return EventSourceResponse(gen())

    # -----------------------------------------------------------------------
    # SPA static files + catch-all for client-side routes
    # -----------------------------------------------------------------------

    if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
        # Serve hashed asset bundles directly.
        app.mount(
            "/assets",
            StaticFiles(directory=str(STATIC_DIR / "assets")),
            name="assets",
        )

        for fname in ("favicon.svg", "favicon.ico", "robots.txt"):
            fpath = STATIC_DIR / fname
            if fpath.exists():
                _register_static_file(app, f"/{fname}", fpath)

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            # Anything else falls through to index.html so React Router can
            # handle deep links like /runs/abcd or /report?scenario=x.
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            return FileResponse(STATIC_DIR / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def spa_missing():
            return JSONResponse(
                {
                    "error": "SPA bundle not built",
                    "hint": (
                        "Run `npm install && npm run build` in checkpoint/dashboard/web/, "
                        "then restart `checkpoint serve`."
                    ),
                },
                status_code=503,
            )

    # -----------------------------------------------------------------------
    # Per-request metrics observation (keep this LAST so middleware ran)
    # -----------------------------------------------------------------------

    @app.middleware("http")
    async def _metrics_mw(request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        response: Response = await call_next(request)
        # Bucket the path so we don't blow up cardinality on /api/runs/<uuid>.
        bucket = _bucket_path(request.url.path)
        metrics.observe_http(
            request.method, bucket, response.status_code, time.perf_counter() - start
        )
        return response

    return app


def _register_static_file(app: FastAPI, route: str, fpath: Path) -> None:
    """Helper so the closure over `fpath` is per-file, not loop-shared."""

    @app.get(route, include_in_schema=False)
    async def _serve():
        return FileResponse(fpath)


_PATH_BUCKETS = (
    "/api/runs/{id}",
    "/api/jobs/{id}",
    "/api/jobs/{id}/stream",
    "/runs/{id}",
    "/live/{id}",
)


def _bucket_path(path: str) -> str:
    """Reduce path cardinality for metrics. Replace UUIDs with {id}."""
    parts = path.split("/")
    out: list[str] = []
    for p in parts:
        if len(p) >= 8 and any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            out.append("{id}")
        else:
            out.append(p)
    return "/".join(out)
