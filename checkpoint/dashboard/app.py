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
from ..telemetry import build_telemetry_report
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
    h = rec.get("harness") or {}
    return {
        "run_id": rec.get("run_id", ""),
        "scenario": rec.get("scenario"),
        "scenario_path": rec.get("scenario_path"),
        "satisfaction": float(rec.get("satisfaction") or 0),
        "criteria_pass": sum(1 for c in crits if c.get("passed")),
        "criteria_total": len(crits),
        "evaluator_model": rec.get("evaluator_model"),
        "timestamp": (rec.get("env") or {}).get("timestamp"),
        "exit_code": rec.get("exit_code"),
        # Agent / mode / duration — added v0.2. Older records will have None.
        "harness_name": h.get("name"),
        "harness_dir": h.get("dir"),
        "mode": h.get("mode"),
        "duration_ms": rec.get("duration_ms"),
    }


def _list_runs(
    runs_dir: Path,
    scenario: str = "",
    per_page: int = 50,
    page: int = 1,
    *,
    agent_filter: str = "",
    mode_filter: str = "",
) -> tuple[list[dict], int]:
    if not runs_dir.exists():
        return [], 0
    files = sorted(
        runs_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    scn_pattern = scenario.lower()
    agent_pattern = agent_filter.lower()
    mode_pattern = (mode_filter or "").lower()
    rows: list[dict] = []
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if scn_pattern and scn_pattern not in (rec.get("scenario") or "").lower():
            continue
        if agent_pattern:
            h = rec.get("harness") or {}
            blob = f"{h.get('name', '')} {h.get('dir', '')}".lower()
            if agent_pattern not in blob:
                continue
        if mode_pattern:
            h = rec.get("harness") or {}
            if (h.get("mode") or "").lower() != mode_pattern:
                continue
        rows.append(rec)
    total = len(rows)
    start = (page - 1) * per_page
    sliced = rows[start : start + per_page]
    return [_record_summary(r) for r in sliced], total


def _runs_for_agent(runs_dir: Path, agent: dict) -> list[dict]:
    """All run summaries that this agent produced.

    We match by the absolute or relative `dir` of the harness — that's stable
    across re-renames of the friendly `name`.
    """
    if not runs_dir.exists():
        return []
    abs_path = (agent or {}).get("abs_path") or ""
    rel_path = (agent or {}).get("path") or ""
    out: list[dict] = []
    for f in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        h = rec.get("harness") or {}
        d = h.get("dir") or ""
        if d and (d == rel_path or d == abs_path or d.endswith("/" + rel_path)):
            out.append(_record_summary(rec))
    return out


def _runs_for_scenario(runs_dir: Path, scenario_name: str) -> list[dict]:
    if not runs_dir.exists() or not scenario_name:
        return []
    pattern = scenario_name.lower()
    out: list[dict] = []
    for f in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if (rec.get("scenario") or "").lower() == pattern:
            out.append(_record_summary(rec))
    return out


def _runs_grouped_by_scenario(rows: list[dict]) -> dict[str, dict]:
    """{scenario: {runs:int, avg:float, last_score:float, last_at:str}}."""
    out: dict[str, dict] = {}
    for r in rows:
        key = r.get("scenario") or "(unknown)"
        bucket = out.setdefault(key, {"runs": 0, "scores": [], "last_score": None, "last_at": None})
        bucket["runs"] += 1
        bucket["scores"].append(float(r.get("satisfaction") or 0))
        if bucket["last_at"] is None:
            bucket["last_at"] = r.get("timestamp")
            bucket["last_score"] = float(r.get("satisfaction") or 0)
    return {
        k: {
            "runs": v["runs"],
            "avg_score": round(sum(v["scores"]) / len(v["scores"]), 1) if v["scores"] else 0,
            "last_score": v["last_score"],
            "last_at": v["last_at"],
        }
        for k, v in out.items()
    }


def _agent_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"total_runs": 0, "avg_score": 0.0, "pass_rate": 0.0, "last_at": None}
    scores = [float(r.get("satisfaction") or 0) for r in rows]
    return {
        "total_runs": len(rows),
        "avg_score": round(sum(scores) / len(scores), 1),
        "pass_rate": round(100 * sum(1 for s in scores if s >= 100) / len(scores), 1),
        "last_at": rows[0].get("timestamp"),
    }


def _scenario_stats(rows: list[dict]) -> dict:
    return _agent_stats(rows)


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
        description="Harness dir for docker mode, or --harness command/path for subprocess mode.",
    )
    model: str | None = Field(default=None, description="Evaluator model passed as --model.")
    timeout: int | None = Field(default=None, ge=1, description="Harness timeout seconds.")
    clone: str | None = Field(default=None, description="Clone override passed as --clone.")
    runs: int | None = Field(default=None, ge=1, le=100, description="Number of runs.")
    rate_limit: int | None = Field(default=None, ge=1, description="Twin request cap.")
    read_only: bool = Field(default=False, description="Fail if twin state changes.")
    no_failure_analysis: bool = Field(default=False, description="Skip LLM failure analysis.")
    seed_file: str | None = Field(default=None, description="Seed file override.")
    setup_file: str | None = Field(default=None, description="Setup prose file override.")
    keep_state: bool = Field(default=False, description="Do not re-apply scenario seeds.")
    fresh_seed: bool = Field(default=False, description="Force seed re-application.")
    docker_logs: bool = Field(default=False, description="Stream docker harness logs to stderr.")

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
        agent: str = "",
        mode: str = "",
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=500),
    ):
        rows, total = _list_runs(runs_dir, scenario, per_page, page,
                                 agent_filter=agent, mode_filter=mode)
        return {"rows": rows, "total": total, "page": page, "per_page": per_page}

    @app.get("/api/runs/{run_id}", tags=["runs"])
    def api_run_detail(run_id: str):
        record = _load_record(runs_dir, run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return record

    @app.get("/api/runs/{run_id}/telemetry", tags=["runs"])
    def api_run_telemetry(run_id: str):
        record = _load_record(runs_dir, run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return build_telemetry_report(record)

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

    @app.get("/api/agents/{agent_id}", tags=["agents"])
    def api_agent_detail(agent_id: str):
        """One agent + its full README + a roll-up of every run it produced.

        We accept the agent's slug (e.g. ``examples--agents--openai-tools``)
        and resolve it back to a discovered entry.
        """
        agents_list = agent_discovery.discover(project_dir or Path.cwd())
        agent = next((a for a in agents_list if a["id"] == agent_id), None)
        if agent is None:
            raise HTTPException(404, f"agent {agent_id!r} not found")
        readme_path = Path(agent["abs_path"]) / "README.md"
        readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
        # Roll up every run that mentions this agent.
        runs = _runs_for_agent(runs_dir, agent)
        scenarios = _runs_grouped_by_scenario(runs)
        return {
            "agent": agent,
            "readme": readme,
            "runs": runs,
            "by_scenario": scenarios,
            "stats": _agent_stats(runs),
        }

    @app.get("/api/scenarios/file", tags=["scenarios"])
    def api_scenario_detail(path: str):
        """One scenario: parsed sections + raw markdown + run history.

        Path is relative to scenarios_dir (matches the dropdown values
        returned by /api/scenarios).
        """
        # Resolve safely under scenarios_dir to defeat traversal attempts.
        target = (scenarios_dir / path).resolve()
        try:
            target.relative_to(scenarios_dir.resolve())
        except ValueError:
            raise HTTPException(400, "scenario path escapes scenarios_dir")
        if not target.is_file():
            raise HTTPException(404, f"scenario {path!r} not found")
        try:
            scn = parse_file(target)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"could not parse scenario: {e}")
        runs = _runs_for_scenario(runs_dir, scn.title or target.stem)
        return {
            "path": path,
            "abs_path": str(target),
            "title": scn.title,
            "prompt": scn.prompt,
            "setup": scn.setup,
            "expected": scn.expected,
            "criteria": [
                {"text": c.text, "kind": c.kind} for c in scn.criteria
            ],
            "config": scn.config,
            "clones": scn.clones,
            "raw": target.read_text(encoding="utf-8"),
            "runs": runs,
            "stats": _scenario_stats(runs),
        }

    # -----------------------------------------------------------------------
    # Clones (live registry + management proxies)
    # -----------------------------------------------------------------------

    @app.post("/api/clones/{clone_id}", tags=["clones"], status_code=201)
    def api_clone_start(clone_id: str):
        from .. import clone_manager
        try:
            entry = clone_manager.start(clone_id)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e))
        return {"id": clone_id, **entry}

    @app.delete("/api/clones/{clone_id}", tags=["clones"])
    def api_clone_stop(clone_id: str):
        from .. import clone_manager
        was_running = clone_manager.stop(clone_id)
        return {"id": clone_id, "was_running": was_running}

    @app.post("/api/clones/{clone_id}/seed/{seed_name}", tags=["clones"])
    def api_clone_seed(clone_id: str, seed_name: str):
        from .. import clone_manager
        try:
            return clone_manager.seed(clone_id, seed_name)
        except (KeyError, RuntimeError) as e:
            raise HTTPException(404, str(e))

    @app.post("/api/clones/{clone_id}/reset", tags=["clones"])
    def api_clone_reset(clone_id: str):
        from .. import clone_manager
        try:
            return clone_manager.reset(clone_id)
        except (KeyError, RuntimeError) as e:
            raise HTTPException(404, str(e))

    @app.get("/api/clones/{clone_id}/tools", tags=["clones"])
    def api_clone_tools(clone_id: str):
        from .. import clone_manager
        try:
            return clone_manager.tools(clone_id)
        except (KeyError, RuntimeError) as e:
            raise HTTPException(404, str(e))

    @app.get("/api/clones/supported", tags=["clones"])
    def api_clones_supported():
        """Static list of clones the system knows how to spawn."""
        from ..clone_manager import TWIN_APPS
        return [{"id": k, "module": v} for k, v in sorted(TWIN_APPS.items())]

    # -----------------------------------------------------------------------
    # CLI-parity endpoints: doctor, config, validate, anonymized export.
    # -----------------------------------------------------------------------

    @app.get("/api/doctor", tags=["system"])
    def api_doctor():
        """Run the same checks as `checkpoint doctor` and return structured JSON."""
        from ..diagnostics import all_passed, run_checks
        checks = run_checks(cwd=Path.cwd())
        return {
            "all_passed": all_passed(checks),
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "detail": c.detail,
                    "fix": c.fix,
                }
                for c in checks
            ],
        }

    @app.get("/api/config", tags=["system"])
    def api_config(reveal_env: bool = False):
        """Read the user's ~/.checkpoint/config.json + known-keys catalog."""
        from ..user_config import KNOWN_KEYS, UserConfig
        cfg = UserConfig.load()
        return {
            "path": str(cfg.path),
            "exists": cfg.path.exists(),
            "values": (
                {k: cfg.get(k, resolve_env=True) for k in cfg.flatten()}
                if reveal_env else cfg.flatten()
            ),
            "known_keys": KNOWN_KEYS,
        }

    @app.put("/api/config/{key:path}", tags=["system"])
    def api_config_set(key: str, body: dict):
        from ..user_config import UserConfig
        cfg = UserConfig.load()
        cfg.set(key, body.get("value"))
        cfg.save()
        return {"key": key, "value": cfg.get(key, resolve_env=False)}

    @app.delete("/api/config/{key:path}", tags=["system"])
    def api_config_unset(key: str):
        from ..user_config import UserConfig
        cfg = UserConfig.load()
        removed = cfg.unset(key)
        if removed:
            cfg.save()
        return {"key": key, "removed": removed}

    @app.post("/api/scenarios/validate", tags=["scenarios"])
    def api_scenario_validate(body: dict):
        """Parse + lint a scenario. Accepts either {"path": "..."} (existing
        file) or {"raw": "..."} (markdown text). Returns the parsed sections
        + any warnings/errors."""
        from ..scenario import parse, parse_file
        raw = body.get("raw")
        path = body.get("path")
        warnings: list[str] = []
        errors: list[str] = []
        try:
            if raw is not None:
                scn = parse(raw)
            elif path:
                target = (scenarios_dir / path).resolve()
                try:
                    target.relative_to(scenarios_dir.resolve())
                except ValueError:
                    raise HTTPException(400, "scenario path escapes scenarios_dir")
                if not target.is_file():
                    raise HTTPException(404, f"scenario {path!r} not found")
                scn = parse_file(target)
            else:
                raise HTTPException(400, "provide either {raw} or {path}")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            errors.append(f"parse error: {e}")
            return {"ok": False, "errors": errors, "warnings": warnings,
                    "scenario": None}
        # Lint
        if not scn.prompt:
            errors.append("missing `## Prompt` (or `## Task`) section")
        if not scn.criteria:
            warnings.append("no `## Success Criteria` — the run will score 0/0")
        if scn.config.get("clones") and not scn.clones:
            warnings.append("`clones:` config value is empty after parsing")
        return {
            "ok": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "scenario": {
                "title": scn.title,
                "prompt": scn.prompt,
                "setup": scn.setup,
                "expected": scn.expected,
                "criteria": [{"text": c.text, "kind": c.kind} for c in scn.criteria],
                "clones": scn.clones,
                "config": scn.config,
            },
        }

    @app.get("/api/runs/{run_id}/anonymized", tags=["runs"])
    def api_run_anonymized(run_id: str):
        """Same record as /api/runs/:id, but with emails / GitHub PATs / OpenAI
        keys regex-redacted. Use for sharing in bug reports."""
        record = _load_record(runs_dir, run_id)
        if record is None:
            raise HTTPException(404, "run not found")
        from ..cli import _anonymize_record
        return _anonymize_record(record)

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
            model=body.model,
            timeout=body.timeout,
            clone=body.clone,
            runs=body.runs,
            rate_limit=body.rate_limit,
            read_only=body.read_only,
            no_failure_analysis=body.no_failure_analysis,
            seed_file=body.seed_file,
            setup_file=body.setup_file,
            keep_state=body.keep_state,
            fresh_seed=body.fresh_seed,
            docker_logs=body.docker_logs,
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
