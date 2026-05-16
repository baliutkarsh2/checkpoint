"""Generate a single-file HTML dashboard from .checkpoint/cache/runs/*.json."""
import glob, html, json, os, pathlib, webbrowser
from datetime import datetime

RUNS_DIR = pathlib.Path(".checkpoint/cache/runs")
OUT = pathlib.Path("dashboard.html")


def load_runs():
    runs = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        try:
            r = json.loads(path.read_text())
        except Exception:
            continue
        r["_mtime"] = path.stat().st_mtime
        first_ts = (r.get("trace") or [{}])[0].get("ts")
        r["_when"] = first_ts or datetime.fromtimestamp(r["_mtime"]).isoformat()
        runs.append(r)
    runs.sort(key=lambda r: r["_when"])
    return runs


def kpis(runs):
    if not runs:
        return {"count": 0, "avg": 0, "min": 0, "max": 0, "pass_rate": 0}
    scores = [r.get("satisfaction", 0) or 0 for r in runs]
    full_pass = sum(1 for s in scores if s >= 100)
    return {
        "count": len(runs),
        "avg": round(sum(scores) / len(scores), 1),
        "min": min(scores),
        "max": max(scores),
        "pass_rate": round(100 * full_pass / len(runs), 0),
    }


CSS = """
:root {
  --bg: #0a0a0b; --panel: #131316; --panel-2: #1a1a1f; --border: #26262d;
  --text: #ececf0; --muted: #8a8a93; --dim: #5a5a63;
  --accent: #a3e635; --pass: #4ade80; --partial: #fbbf24; --fail: #f87171;
  --font-sans: 'Inter', system-ui, -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: var(--bg); color: var(--text); font-family: var(--font-sans); font-size: 14px; line-height: 1.45; font-feature-settings: 'tnum' 1, 'cv11' 1; }
a { color: inherit; }
.app { max-width: 1280px; margin: 0 auto; padding: 32px 28px 80px; }

header { display: flex; align-items: baseline; justify-content: space-between; padding-bottom: 24px; border-bottom: 1px solid var(--border); margin-bottom: 28px; }
.brand { display: flex; align-items: baseline; gap: 12px; }
.brand .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); display: inline-block; transform: translateY(-1px); }
.brand h1 { font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }
.brand .tag { color: var(--muted); font-size: 13px; font-family: var(--font-mono); }
header .meta { color: var(--muted); font-size: 12px; font-family: var(--font-mono); }

.kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 28px; }
.kpi { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px 18px; }
.kpi .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }
.kpi .value { font-size: 26px; font-weight: 600; font-feature-settings: 'tnum' 1; letter-spacing: -0.02em; }
.kpi .sub { color: var(--dim); font-size: 12px; margin-top: 2px; font-family: var(--font-mono); }

.section-title { display: flex; align-items: baseline; justify-content: space-between; margin: 8px 0 14px; }
.section-title h2 { font-size: 13px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
.section-title .hint { font-size: 12px; color: var(--dim); font-family: var(--font-mono); }

.trend { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px 22px; margin-bottom: 28px; }
.trend-bars { display: flex; align-items: flex-end; gap: 5px; height: 92px; }
.trend-bar { flex: 1; min-width: 8px; background: var(--panel-2); border-radius: 2px 2px 0 0; position: relative; cursor: pointer; transition: opacity 0.12s; }
.trend-bar:hover { opacity: 0.85; }
.trend-bar.b-pass { background: var(--pass); }
.trend-bar.b-partial { background: var(--partial); }
.trend-bar.b-fail { background: var(--fail); }
.trend-bar.b-zero { background: var(--fail); height: 4px !important; }
.trend-axis { display: flex; justify-content: space-between; color: var(--dim); font-size: 11px; font-family: var(--font-mono); margin-top: 10px; }

.split { display: grid; grid-template-columns: 380px 1fr; gap: 16px; align-items: start; }
.runs-list { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; max-height: 640px; overflow-y: auto; }
.run-row { display: grid; grid-template-columns: 44px 1fr auto; align-items: center; gap: 10px; padding: 12px 14px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.08s; }
.run-row:last-child { border-bottom: none; }
.run-row:hover { background: var(--panel-2); }
.run-row.active { background: var(--panel-2); border-left: 2px solid var(--accent); padding-left: 12px; }
.run-row .score { font-family: var(--font-mono); font-weight: 600; font-size: 13px; text-align: right; }
.run-row .score.s-pass { color: var(--pass); }
.run-row .score.s-partial { color: var(--partial); }
.run-row .score.s-fail { color: var(--fail); }
.run-row .name { font-size: 13px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-row .id { color: var(--dim); font-size: 11px; font-family: var(--font-mono); margin-top: 2px; }

.detail { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 22px 24px; min-height: 480px; }
.detail h3 { font-size: 17px; font-weight: 600; letter-spacing: -0.01em; margin-bottom: 4px; }
.detail .sub { color: var(--muted); font-size: 12px; font-family: var(--font-mono); margin-bottom: 18px; word-break: break-all; }
.detail .badges { display: flex; gap: 8px; margin-bottom: 22px; flex-wrap: wrap; }
.badge { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 4px; font-size: 12px; font-family: var(--font-mono); border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); }
.badge.score-pass { color: var(--pass); border-color: rgba(74,222,128,0.25); }
.badge.score-partial { color: var(--partial); border-color: rgba(251,191,36,0.25); }
.badge.score-fail { color: var(--fail); border-color: rgba(248,113,113,0.25); }

.detail .subhead { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 22px 0 10px; font-weight: 500; }
.detail .subhead:first-of-type { margin-top: 0; }
.criterion { border-left: 2px solid var(--border); padding: 8px 0 8px 14px; margin-bottom: 12px; }
.criterion.c-pass { border-left-color: var(--pass); }
.criterion.c-fail { border-left-color: var(--fail); }
.criterion .head { display: flex; align-items: center; gap: 10px; }
.criterion .mark { font-family: var(--font-mono); font-size: 12px; width: 14px; text-align: center; }
.criterion .mark.m-pass { color: var(--pass); }
.criterion .mark.m-fail { color: var(--fail); }
.criterion .kind { color: var(--dim); font-family: var(--font-mono); font-size: 11px; }
.criterion .text { font-size: 13px; flex: 1; }
.criterion .reason { color: var(--muted); font-size: 12px; margin-top: 6px; margin-left: 24px; }

.answer { background: var(--panel-2); border: 1px solid var(--border); border-left: 2px solid var(--accent); border-radius: 4px; padding: 14px 16px; font-size: 13px; color: var(--text); white-space: pre-wrap; word-wrap: break-word; }

.trace-table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; }
.trace-table th { text-align: left; color: var(--muted); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.06em; padding: 8px 12px; border-bottom: 1px solid var(--border); }
.trace-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); color: var(--text); }
.trace-table tr:last-child td { border-bottom: none; }
.trace-table .method { color: var(--accent); }
.trace-table .status-2 { color: var(--pass); }
.trace-table .status-4, .trace-table .status-5 { color: var(--fail); }
.empty { color: var(--dim); font-size: 13px; text-align: center; padding: 40px 0; }
.empty.detail-empty { padding: 80px 0; }

footer { margin-top: 40px; text-align: center; color: var(--dim); font-size: 11px; font-family: var(--font-mono); }
"""


def score_class(s):
    if s >= 100:
        return "pass"
    if s >= 50:
        return "partial"
    return "fail"


def render(runs, k):
    runs_json = json.dumps([
        {
            "run_id": r.get("run_id"),
            "scenario": r.get("scenario") or "—",
            "scenario_path": r.get("scenario_path") or "",
            "score": r.get("satisfaction", 0) or 0,
            "criteria": r.get("criteria") or [],
            "final_answer": r.get("final_answer") or "",
            "trace": r.get("trace") or [],
            "model": r.get("evaluator_model") or "—",
            "when": r.get("_when"),
            "exit_code": r.get("exit_code"),
            "error": r.get("error"),
        } for r in runs
    ])

    rows_html = []
    for r in reversed(runs):
        s = r.get("satisfaction", 0) or 0
        cls = score_class(s)
        scenario = html.escape((r.get("scenario") or "—")[:60])
        rid = html.escape(r.get("run_id") or "")
        rows_html.append(
            f'<div class="run-row" data-rid="{rid}">'
            f'<div></div>'
            f'<div><div class="name">{scenario}</div><div class="id">{rid}</div></div>'
            f'<div class="score s-{cls}">{int(s)}</div>'
            f'</div>'
        )

    bars_html = []
    for r in runs:
        s = r.get("satisfaction", 0) or 0
        cls = score_class(s) if s > 0 else "zero"
        h = max(4, int((s / 100) * 88))
        title = f"{(r.get('scenario') or '')[:50]} — {int(s)}/100"
        bars_html.append(
            f'<div class="trend-bar b-{cls}" style="height:{h}px" title="{html.escape(title)}" data-rid="{html.escape(r.get("run_id") or "")}"></div>'
        )

    first_when = (runs[0]["_when"][:10] if runs else "—")
    last_when = (runs[-1]["_when"][:10] if runs else "—")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>checkpoint — runs dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <span class="dot"></span>
      <h1>checkpoint</h1>
      <span class="tag">agent eval · runs dashboard</span>
    </div>
    <div class="meta">generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · {k['count']} runs</div>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="label">runs</div><div class="value">{k['count']}</div><div class="sub">{first_when} → {last_when}</div></div>
    <div class="kpi"><div class="label">avg score</div><div class="value">{k['avg']}</div><div class="sub">/ 100</div></div>
    <div class="kpi"><div class="label">pass rate</div><div class="value">{int(k['pass_rate'])}%</div><div class="sub">score ≥ 100</div></div>
    <div class="kpi"><div class="label">min</div><div class="value" style="color:var(--fail)">{int(k['min'])}</div><div class="sub">worst run</div></div>
    <div class="kpi"><div class="label">max</div><div class="value" style="color:var(--pass)">{int(k['max'])}</div><div class="sub">best run</div></div>
  </div>

  <div class="section-title"><h2>score trend</h2><span class="hint">click a bar to drill in</span></div>
  <div class="trend">
    <div class="trend-bars">{''.join(bars_html) or '<div class="empty">no runs yet</div>'}</div>
    <div class="trend-axis"><span>{first_when}</span><span>{last_when}</span></div>
  </div>

  <div class="section-title"><h2>runs</h2><span class="hint">newest first</span></div>
  <div class="split">
    <div class="runs-list" id="runs-list">
      {''.join(rows_html) or '<div class="empty">no runs</div>'}
    </div>
    <div class="detail" id="detail"><div class="empty detail-empty">select a run →</div></div>
  </div>

  <footer>checkpoint · all data sourced from .checkpoint/cache/runs/*.json</footer>
</div>

<script>
const runs = {runs_json};
const byId = Object.fromEntries(runs.map(r => [r.run_id, r]));

function scoreClass(s) {{ return s >= 100 ? 'pass' : s >= 50 ? 'partial' : 'fail'; }}
function esc(s) {{ return String(s ?? '').replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]); }}

function renderDetail(rid) {{
  const r = byId[rid];
  const el = document.getElementById('detail');
  if (!r) {{ el.innerHTML = '<div class="empty detail-empty">select a run →</div>'; return; }}
  const cls = scoreClass(r.score);
  const crit = (r.criteria || []).map(c => {{
    const ok = c.passed;
    return `<div class="criterion c-${{ok?'pass':'fail'}}">`+
      `<div class="head"><span class="mark m-${{ok?'pass':'fail'}}">${{ok?'✓':'✗'}}</span>`+
      `<span class="kind">[${{esc(c.kind)}}]</span>`+
      `<span class="text">${{esc(c.text)}}</span></div>`+
      (c.reasoning ? `<div class="reason">${{esc(c.reasoning)}}</div>` : '') +
      `</div>`;
  }}).join('') || '<div class="empty">no criteria</div>';

  const trace = (r.trace || []).slice(0, 20).map(t => {{
    const st = String(t.status || '');
    const stCls = st.startsWith('2') ? 'status-2' : st.startsWith('4') ? 'status-4' : st.startsWith('5') ? 'status-5' : '';
    return `<tr><td class="method">${{esc(t.method)}}</td><td>${{esc(t.path)}}</td><td class="${{stCls}}">${{esc(t.status)}}</td></tr>`;
  }}).join('');
  const traceBlock = trace
    ? `<table class="trace-table"><thead><tr><th>method</th><th>path</th><th>status</th></tr></thead><tbody>${{trace}}</tbody></table>`
    : '<div class="empty">no API calls recorded</div>';

  const answer = r.final_answer
    ? `<div class="answer">${{esc(r.final_answer)}}</div>`
    : '<div class="empty">(agent produced no final answer)</div>';

  el.innerHTML = `
    <h3>${{esc(r.scenario)}}</h3>
    <div class="sub">${{esc(r.scenario_path)}}</div>
    <div class="badges">
      <span class="badge score-${{cls}}">${{Math.round(r.score)}}/100</span>
      <span class="badge">run · ${{esc(r.run_id)}}</span>
      <span class="badge">judge · ${{esc(r.model)}}</span>
      <span class="badge">${{esc((r.when||'').replace('T',' ').slice(0,16))}}</span>
    </div>
    <div class="subhead">criteria</div>
    ${{crit}}
    <div class="subhead">agent's final answer</div>
    ${{answer}}
    <div class="subhead">api trace (${{(r.trace||[]).length}} call${{(r.trace||[]).length===1?'':'s'}})</div>
    ${{traceBlock}}
  `;
  document.querySelectorAll('.run-row').forEach(row => row.classList.toggle('active', row.dataset.rid === rid));
}}

document.addEventListener('click', e => {{
  const row = e.target.closest('.run-row');
  if (row) {{ renderDetail(row.dataset.rid); return; }}
  const bar = e.target.closest('.trend-bar');
  if (bar) renderDetail(bar.dataset.rid);
}});

const latest = [...runs].reverse()[0];
if (latest) renderDetail(latest.run_id);
</script>
</body>
</html>
"""


def main():
    runs = load_runs()
    k = kpis(runs)
    OUT.write_text(render(runs, k))
    abs_path = OUT.resolve()
    print(f"Wrote {OUT} ({k['count']} runs, avg {k['avg']}/100)")
    print(f"file://{abs_path}")
    webbrowser.open(f"file://{abs_path}")


if __name__ == "__main__":
    main()
