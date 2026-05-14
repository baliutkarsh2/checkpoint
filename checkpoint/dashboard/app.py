"""FastAPI dashboard for checkpoint."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..analytics import compute_trend, detect_flaky, load_runs_for_scenario
from ..compare_diff import build_compare_diff
from ..scenario import parse_file

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _score_color(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "var(--ink-3)"
    if s >= 90:
        return "var(--pass)"
    elif s >= 70:
        return "var(--warn)"
    return "var(--fail)"


def _truncate_mid(text: str, maxlen: int = 80) -> str:
    if len(text) <= maxlen:
        return text
    half = (maxlen - 3) // 2
    return text[:half] + "..." + text[-(maxlen - half - 3):]


def _load_record(runs_dir: Path, run_id: str) -> dict | None:
    p = runs_dir / f"{run_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _list_runs(
    runs_dir: Path, scenario: str = "", limit: int = 50, page: int = 1
) -> tuple[list[dict], int]:
    if not runs_dir.exists():
        return [], 0
    files = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    pattern = scenario.lower()
    all_rows: list[dict] = []
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if pattern and pattern not in (rec.get("scenario") or "").lower():
            continue
        all_rows.append(rec)
    total = len(all_rows)
    start = (page - 1) * limit
    return all_rows[start : start + limit], total


def _build_summary(runs_dir: Path) -> dict:
    if not runs_dir.exists():
        return {"total_runs": 0, "avg_score_30d": 0, "pass_rate_30d": 0, "recent_fail_count": 0}
    files = list(runs_dir.glob("*.json"))
    total = len(files)
    cutoff_30d = datetime.now(tz=timezone.utc) - timedelta(days=30)
    cutoff_7d = datetime.now(tz=timezone.utc) - timedelta(days=7)
    scores_30d: list[float] = []
    fail_7d = 0
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        ts_str = (rec.get("env") or {}).get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
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
    except Exception:
        return []


def _build_scenario_summaries(scenarios_dir: Path) -> tuple[list[dict], dict]:
    try:
        from ..checker import PATTERNS as _checker_patterns
    except Exception:
        _checker_patterns = []

    summaries: list[dict] = []
    total_d = 0
    stage1_hits = 0
    total_p = 0
    for md in sorted(scenarios_dir.rglob("*.md")):
        try:
            scn = parse_file(md)
        except Exception:
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
        summaries.append({
            "title": scn.title or md.stem,
            "path": str(md.relative_to(scenarios_dir)),
            "clones": ", ".join(scn.clones) if scn.clones else None,
            "tags": scn.config.get("tags", None),
            "d_count": len(d_crits),
            "p_count": len(p_crits),
            "coverage_pct": cov_pct,
        })
    coverage = {
        "total_d": total_d,
        "stage1_hits": stage1_hits,
        "stage1_pct": round(100 * stage1_hits / total_d, 1) if total_d else 0,
        "total_p": total_p,
    }
    return summaries, coverage


def create_app(
    runs_dir: Path,
    scenarios_dir: Path,
    clone_registry_path: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="checkpoint dashboard", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["short_id"] = lambda s: (s or "")[:12]
    templates.env.filters["score_color"] = _score_color
    templates.env.filters["truncate_mid"] = _truncate_mid

    try:
        import checkpoint as _ckpt_mod
        _version = getattr(_ckpt_mod, "__version__", "0.0.1")
    except Exception:
        _version = "0.0.1"

    def _base_ctx(request: Request) -> dict:
        return {
            "request": request,
            "version": _version,
            "host": request.headers.get("host", "127.0.0.1"),
        }

    def _tpl(name: str, request: Request, ctx: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, {**_base_ctx(request), **ctx}, status_code=status_code)

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # -------------------------------------------------------------------------
    # Page routes
    # -------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, scenario: str = "", limit: int = 50, page: int = 1):
        runs, total = _list_runs(runs_dir, scenario, limit, page)
        return _tpl("runs.html", request, {
            "active": "runs",
            "runs": runs,
            "total": total,
            "page": page,
            "per_page": limit,
            "scenario_filter": scenario,
            "live_clones": _load_clones(clone_registry_path),
            "summary": _build_summary(runs_dir),
        })

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail_page(request: Request, run_id: str):
        record = _load_record(runs_dir, run_id)
        if record is None:
            return _tpl("404.html", request, {"message": f"Run '{run_id}' not found."}, status_code=404)
        return _tpl("run_detail.html", request, {"active": "runs", "record": record})

    @app.get("/scenarios", response_class=HTMLResponse)
    def scenarios_page(request: Request):
        scenario_list, coverage = _build_scenario_summaries(scenarios_dir)
        return _tpl("scenarios.html", request, {
            "active": "scenarios",
            "scenarios": scenario_list,
            "scenarios_dir": str(scenarios_dir),
            "coverage": coverage,
        })

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request, scenario: str = "", limit: int = 50):
        runs = load_runs_for_scenario(scenario, runs_dir, limit=limit)
        trend = compute_trend(runs)
        flaky = detect_flaky(trend)
        return _tpl("report.html", request, {
            "active": "report",
            "trend": trend,
            "flaky": flaky,
            "scenario_pattern": scenario,
        })

    @app.get("/compare", response_class=HTMLResponse)
    def compare_page(request: Request, a: str = "", b: str = ""):
        rec_a = _load_record(runs_dir, a) if a else None
        rec_b = _load_record(runs_dir, b) if b else None
        if not rec_a or not rec_b:
            return _tpl("404.html", request, {"message": "One or both run records not found."}, status_code=404)
        diff = build_compare_diff(rec_a, rec_b)
        return _tpl("compare.html", request, {
            "active": "runs",
            "rec_a": rec_a,
            "rec_b": rec_b,
            "diff": diff,
        })

    # -------------------------------------------------------------------------
    # API routes
    # -------------------------------------------------------------------------

    @app.get("/api/runs")
    def api_runs(scenario: str = "", limit: int = 50, page: int = 1):
        rows, total = _list_runs(runs_dir, scenario, limit, page)
        return {"rows": rows, "total": total, "page": page, "per_page": limit}

    @app.get("/api/runs/{run_id}")
    def api_run_detail(run_id: str):
        record = _load_record(runs_dir, run_id)
        if record is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return record

    @app.get("/api/report")
    def api_report(scenario: str = "", limit: int = 50):
        runs = load_runs_for_scenario(scenario, runs_dir, limit=limit)
        trend = compute_trend(runs)
        flaky = detect_flaky(trend)
        return {**trend, "flaky_criteria": flaky}

    @app.get("/api/scenarios")
    def api_scenarios():
        scenario_list, _coverage = _build_scenario_summaries(scenarios_dir)
        return scenario_list

    @app.get("/api/compare")
    def api_compare(a: str = "", b: str = ""):
        rec_a = _load_record(runs_dir, a) if a else None
        rec_b = _load_record(runs_dir, b) if b else None
        if not rec_a or not rec_b:
            return JSONResponse({"error": "not found"}, status_code=404)
        return build_compare_diff(rec_a, rec_b)

    @app.get("/api/clones")
    def api_clones():
        return _load_clones(clone_registry_path)

    @app.get("/api/summary")
    def api_summary():
        return _build_summary(runs_dir)

    return app
