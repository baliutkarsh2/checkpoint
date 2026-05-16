"""Build /tmp/dashboard-shotgun.html — 8 design variants of the runs page side-by-side.

Each variant is a self-contained iframe srcdoc so styles can't leak between cards.
Same 5 rows of real run data across all variants.
"""
import json, html, pathlib, webbrowser, datetime

SEED = json.loads(pathlib.Path("/tmp/run_seed.json").read_text())[:5]


def reltime(ts: str) -> str:
    if not ts: return "—"
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - t
        s = int(delta.total_seconds())
        if s < 60: return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        if s < 86400: return f"{s//3600}h ago"
        return f"{s//86400}d ago"
    except Exception:
        return ts[:10]


def fmt_runtime(rt):
    if rt is None: return "—"
    if rt < 1: return f"{rt:.1f}s"
    if rt < 60: return f"{rt:.1f}s"
    return f"{int(rt//60)}m {int(rt%60)}s"


# ---------------------------------------------------------------------------
# Variant builders. Each returns a complete <html> document string.
# ---------------------------------------------------------------------------

def variant_paper_brutalist():
    rows = "".join(f"""
      <tr>
        <td class="m">{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td><b style="color:{ '#0ea83b' if r['score']>=100 else '#c89124' if r['score']>=50 else '#d73838'}">{int(r['score'])}</b></td>
        <td class="m">{r['crit_pass']}/{r['crit_total']}</td>
        <td class="m" style="color:#5a5648">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Geist',sans-serif;}}
body{{background:#f5f2ea;color:#0a0a0a;font-size:13px;padding:20px;line-height:1.45;}}
.brand{{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;margin-bottom:16px;}}
.mark{{width:14px;height:14px;background:#0a0a0a;position:relative;}}
.mark::after{{content:'';width:6px;height:6px;background:#2dff5c;position:absolute;top:4px;left:4px;}}
h1{{font-size:22px;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px;}}
.sub{{color:#5a5648;font-size:12px;margin-bottom:20px;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px;}}
.k{{border:1px solid #0a0a0a;background:#ebe7dc;box-shadow:4px 4px 0 0 #0a0a0a;padding:12px;}}
.kl{{font-family:'Geist Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.08em;color:#5a5648;}}
.kv{{font-family:'Geist Mono',monospace;font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #0a0a0a;box-shadow:4px 4px 0 0 #0a0a0a;}}
th{{font-family:'Geist Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.08em;
   color:#5a5648;text-align:left;padding:8px 10px;border-bottom:1px solid #0a0a0a;background:#ebe7dc;font-weight:500;}}
td{{padding:8px 10px;border-bottom:1px dashed #e0dccf;font-size:12px;}}
td.m{{font-family:'Geist Mono',monospace;font-size:11px;font-variant-numeric:tabular-nums;}}
</style></head><body>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<h1>Run history</h1><div class="sub">15 runs · paper brutalist (current)</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Avg</div><div class="kv" style="color:#c89124">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv">73%</div></div>
  <div class="k"><div class="kl">Failures 7d</div><div class="kv" style="color:#d73838">4</div></div>
</div>
<table><thead><tr><th>Run</th><th>Scenario</th><th>Score</th><th>Crit</th><th>When</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def variant_dark_warm():
    rows = "".join(f"""
      <tr>
        <td class="m">{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td><b style="color:{'#4ade80' if r['score']>=100 else '#fbbf24' if r['score']>=50 else '#f87171'}">{int(r['score'])}</b></td>
        <td class="m">{r['crit_pass']}/{r['crit_total']}</td>
        <td class="m" style="color:#8a8472">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Geist',sans-serif;}}
body{{background:#0a0a0b;color:#ececf0;font-size:13px;padding:20px;line-height:1.45;}}
.brand{{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;margin-bottom:16px;}}
.mark{{width:14px;height:14px;background:#ececf0;position:relative;}}
.mark::after{{content:'';width:6px;height:6px;background:#2dff5c;position:absolute;top:4px;left:4px;box-shadow:0 0 4px #2dff5c;}}
h1{{font-size:22px;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px;}}
.sub{{color:#8a8472;font-size:12px;margin-bottom:20px;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px;}}
.k{{border:1px solid #ececf0;background:#16161a;box-shadow:4px 4px 0 0 #2dff5c;padding:12px;}}
.kl{{font-family:'Geist Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.08em;color:#8a8472;}}
.kv{{font-family:'Geist Mono',monospace;font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;}}
table{{width:100%;border-collapse:collapse;background:#16161a;border:1px solid #ececf0;box-shadow:4px 4px 0 0 #2dff5c;}}
th{{font-family:'Geist Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.08em;
   color:#8a8472;text-align:left;padding:8px 10px;border-bottom:1px solid #ececf0;background:#1f1f24;font-weight:500;}}
td{{padding:8px 10px;border-bottom:1px dashed #2a2a30;font-size:12px;}}
td.m{{font-family:'Geist Mono',monospace;font-size:11px;font-variant-numeric:tabular-nums;}}
</style></head><body>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<h1>Run history</h1><div class="sub">15 runs · dark warm inversion</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Avg</div><div class="kv" style="color:#fbbf24">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv">73%</div></div>
  <div class="k"><div class="kl">Failures 7d</div><div class="kv" style="color:#f87171">4</div></div>
</div>
<table><thead><tr><th>Run</th><th>Scenario</th><th>Score</th><th>Crit</th><th>When</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def variant_cool_slate():
    rows = "".join(f"""
      <tr>
        <td class="m">{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td><b style="color:{'#34d399' if r['score']>=100 else '#fbbf24' if r['score']>=50 else '#f87171'}">{int(r['score'])}</b></td>
        <td class="m">{r['crit_pass']}/{r['crit_total']}</td>
        <td class="m" style="color:#64748b">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',sans-serif;}}
body{{background:#0b0d12;color:#e6e8ee;font-size:13px;padding:20px;line-height:1.5;}}
.brand{{display:flex;align-items:center;gap:8px;font-weight:600;font-size:14px;margin-bottom:16px;color:#e6e8ee;}}
.mark{{width:12px;height:12px;border-radius:3px;background:linear-gradient(135deg,#5eead4,#0ea5e9);}}
h1{{font-size:21px;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px;color:#fafafa;}}
.sub{{color:#8b93a3;font-size:12px;margin-bottom:20px;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px;}}
.k{{background:#14171f;border:1px solid #1f242e;border-radius:6px;padding:14px;}}
.kl{{font-family:'JetBrains Mono',monospace;font-size:10px;color:#8b93a3;letter-spacing:0.02em;}}
.kv{{font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:500;margin-top:6px;font-variant-numeric:tabular-nums;color:#e6e8ee;}}
table{{width:100%;border-collapse:collapse;background:#14171f;border:1px solid #1f242e;border-radius:6px;overflow:hidden;}}
th{{font-family:'JetBrains Mono',monospace;font-size:10px;color:#8b93a3;text-align:left;padding:9px 12px;
   border-bottom:1px solid #1f242e;background:#181c25;font-weight:500;}}
td{{padding:10px 12px;border-bottom:1px solid #1a1e26;font-size:12.5px;color:#cfd3dd;}}
td.m{{font-family:'JetBrains Mono',monospace;font-size:11px;font-variant-numeric:tabular-nums;}}
tr:hover td{{background:#181c25;}}
</style></head><body>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<h1>Run history</h1><div class="sub">15 runs · cool slate (Vercel/Linear vibe)</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Avg score</div><div class="kv" style="color:#fbbf24">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv" style="color:#34d399">73%</div></div>
  <div class="k"><div class="kl">Failures 7d</div><div class="kv" style="color:#f87171">4</div></div>
</div>
<table><thead><tr><th>RUN</th><th>SCENARIO</th><th>SCORE</th><th>CRIT</th><th>WHEN</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def variant_terminal():
    rows = "".join(f"""
      <tr>
        <td>{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td style="color:{'#00ff88' if r['score']>=100 else '#ffcc00' if r['score']>=50 else '#ff4466'}">{int(r['score']):>3}/100</td>
        <td>{r['crit_pass']}/{r['crit_total']}</td>
        <td>{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums;}}
body{{background:#0d0d0d;color:#d4d4d4;font-size:12px;padding:18px;line-height:1.55;}}
.bar{{color:#666;border-bottom:1px solid #2a2a2a;padding-bottom:8px;margin-bottom:18px;display:flex;justify-content:space-between;}}
.bar b{{color:#00ff88;}}
h1{{font-size:18px;font-weight:700;color:#fff;margin-bottom:4px;text-transform:lowercase;letter-spacing:-0.01em;}}
.sub{{color:#666;font-size:11px;margin-bottom:18px;}}
.kpis{{display:flex;gap:0;margin-bottom:20px;border:1px solid #2a2a2a;}}
.k{{flex:1;padding:10px 12px;border-right:1px solid #2a2a2a;}}
.k:last-child{{border-right:none;}}
.kl{{font-size:9px;color:#666;text-transform:uppercase;letter-spacing:0.08em;}}
.kv{{font-size:20px;font-weight:700;color:#fff;margin-top:2px;}}
table{{width:100%;border-collapse:collapse;border:1px solid #2a2a2a;}}
th{{font-size:9px;color:#666;text-align:left;padding:6px 10px;border-bottom:1px solid #2a2a2a;
   text-transform:uppercase;letter-spacing:0.06em;font-weight:500;background:#161616;}}
td{{padding:6px 10px;border-bottom:1px solid #1a1a1a;font-size:11.5px;}}
tr:hover td{{background:#161616;color:#fff;}}
</style></head><body>
<div class="bar"><span>$ <b>checkpoint</b> serve --runs</span><span>terminal · dense</span></div>
<h1>runs</h1><div class="sub">15 records · sorted by ts desc</div>
<div class="kpis">
  <div class="k"><div class="kl">total</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">avg</div><div class="kv" style="color:#ffcc00">77.3</div></div>
  <div class="k"><div class="kl">pass</div><div class="kv" style="color:#00ff88">73%</div></div>
  <div class="k"><div class="kl">fail/7d</div><div class="kv" style="color:#ff4466">4</div></div>
</div>
<table><thead><tr><th>id</th><th>scenario</th><th>score</th><th>crit</th><th>when</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def variant_editorial():
    rows = "".join(f"""
      <tr>
        <td class="m">{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td class="num"><span style="color:{'#1a7f3a' if r['score']>=100 else '#a06b1a' if r['score']>=50 else '#a3232f'}">{int(r['score'])}</span></td>
        <td class="m">{r['crit_pass']}/{r['crit_total']}</td>
        <td class="m" style="color:#7d7568">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:wght@400;500;600;700&family=Inter:wght@400;500&family=JetBrains+Mono:wght@400&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#f4f1eb;color:#1a1814;font-family:'Inter',sans-serif;font-size:13px;padding:24px;line-height:1.55;}}
.brand{{font-family:'Fraunces',serif;font-size:18px;font-weight:600;letter-spacing:-0.02em;margin-bottom:24px;
        display:flex;align-items:baseline;gap:8px;}}
.brand .meta{{font-family:'JetBrains Mono',monospace;font-size:10px;color:#7d7568;text-transform:uppercase;letter-spacing:0.08em;}}
.kicker{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:0.12em;
        color:#7d7568;margin-bottom:4px;}}
h1{{font-family:'Fraunces',serif;font-size:32px;font-weight:500;letter-spacing:-0.02em;margin-bottom:6px;line-height:1.05;}}
.sub{{color:#5a544c;font-size:13px;margin-bottom:28px;font-style:italic;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin-bottom:24px;border-top:1px solid #1a1814;border-bottom:1px solid #1a1814;}}
.k{{padding:14px 16px;border-right:1px solid #d6d0c5;}}
.k:last-child{{border-right:none;}}
.kl{{font-family:'JetBrains Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.08em;color:#7d7568;}}
.kv{{font-family:'Fraunces',serif;font-size:30px;font-weight:500;margin-top:4px;font-variant-numeric:tabular-nums;}}
table{{width:100%;border-collapse:collapse;}}
th{{font-family:'JetBrains Mono',monospace;font-size:9px;color:#7d7568;text-align:left;padding:10px 0;
   border-bottom:1px solid #1a1814;text-transform:uppercase;letter-spacing:0.08em;font-weight:500;}}
td{{padding:11px 0;border-bottom:1px solid #d6d0c5;font-size:13px;}}
td.m{{font-family:'JetBrains Mono',monospace;font-size:11px;}}
td.num{{font-family:'Fraunces',serif;font-size:18px;font-weight:500;}}
</style></head><body>
<div class="brand">checkpoint <span class="meta">— issue 015</span></div>
<div class="kicker">Recent activity</div>
<h1>Fifteen runs, four failures</h1>
<div class="sub">A retrospective of agent behavior across the last forty-eight hours.</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Average</div><div class="kv">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv">73<span style="font-size:14px">%</span></div></div>
  <div class="k"><div class="kl">Failures · 7d</div><div class="kv" style="color:#a3232f">4</div></div>
</div>
<table><thead><tr><th style="width:90px">Run</th><th>Scenario</th><th style="width:60px">Score</th><th style="width:50px">Crit</th><th style="width:70px">When</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def variant_statement_color():
    rows = "".join(f"""
      <tr>
        <td><span class="pill" style="background:{'#22c55e' if r['score']>=100 else '#eab308' if r['score']>=50 else '#ef4444'};color:#000">{int(r['score'])}</span></td>
        <td>{html.escape(r['scenario'])}</td>
        <td class="m">{r['id'][:8]}</td>
        <td class="m">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Space Grotesk',sans-serif;}}
body{{background:#fef9c3;color:#000;font-size:13px;padding:20px;line-height:1.4;}}
.brand{{display:flex;gap:8px;align-items:center;font-weight:700;font-size:16px;margin-bottom:14px;}}
.mark{{width:18px;height:18px;background:#000;display:flex;align-items:center;justify-content:center;}}
.mark::after{{content:'';width:8px;height:8px;background:#fef9c3;}}
h1{{font-size:42px;font-weight:700;letter-spacing:-0.04em;line-height:0.95;margin-bottom:6px;}}
.sub{{font-size:12px;text-transform:uppercase;letter-spacing:0.06em;font-family:'Space Mono',monospace;margin-bottom:18px;}}
.kpis{{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr;gap:8px;margin-bottom:18px;}}
.k{{background:#000;color:#fef9c3;padding:14px;}}
.k.green{{background:#22c55e;color:#000;}}
.k.red{{background:#ef4444;color:#fef9c3;}}
.k.amber{{background:#eab308;color:#000;}}
.kl{{font-family:'Space Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.08em;}}
.kv{{font-size:36px;font-weight:700;line-height:0.95;margin-top:4px;font-variant-numeric:tabular-nums;letter-spacing:-0.04em;}}
table{{width:100%;border-collapse:collapse;border:2px solid #000;background:#fff;}}
th{{font-family:'Space Mono',monospace;font-size:10px;text-align:left;padding:8px 10px;border-bottom:2px solid #000;
   text-transform:uppercase;font-weight:700;background:#fef9c3;}}
td{{padding:8px 10px;border-bottom:1px solid #000;font-size:12.5px;}}
td.m{{font-family:'Space Mono',monospace;font-size:11px;}}
.pill{{display:inline-block;padding:3px 10px;font-family:'Space Mono',monospace;font-weight:700;font-size:12px;border:2px solid #000;}}
</style></head><body>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<h1>Runs.</h1><div class="sub">// 15 records · 4 fails this week</div>
<div class="kpis">
  <div class="k"><div class="kl">Total runs</div><div class="kv">15</div></div>
  <div class="k amber"><div class="kl">Avg</div><div class="kv">77.3</div></div>
  <div class="k green"><div class="kl">Pass</div><div class="kv">73%</div></div>
  <div class="k red"><div class="kl">Fail/7d</div><div class="kv">4</div></div>
</div>
<table><thead><tr><th style="width:60px">Score</th><th>Scenario</th><th style="width:90px">Run</th><th style="width:80px">When</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def variant_glassmorphic():
    rows = "".join(f"""
      <tr>
        <td class="m">{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td><b style="color:{'#a3e635' if r['score']>=100 else '#fbbf24' if r['score']>=50 else '#fb7185'}">{int(r['score'])}</b></td>
        <td class="m">{r['crit_pass']}/{r['crit_total']}</td>
        <td class="m" style="color:rgba(255,255,255,0.5)">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',sans-serif;}}
body{{background:radial-gradient(ellipse at top left,#1e1b4b 0%,#0a0a0f 50%,#000 100%);color:#fafafa;
     font-size:13px;padding:20px;line-height:1.5;min-height:100vh;}}
body::before{{content:'';position:fixed;inset:0;background:radial-gradient(circle at 80% 20%,rgba(163,230,53,0.15),transparent 40%);pointer-events:none;}}
.brand{{display:flex;align-items:center;gap:9px;font-weight:600;font-size:14px;margin-bottom:18px;position:relative;}}
.mark{{width:14px;height:14px;border-radius:4px;background:linear-gradient(135deg,#a3e635,#22d3ee);box-shadow:0 0 12px rgba(163,230,53,0.6);}}
h1{{font-size:24px;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px;
   background:linear-gradient(180deg,#fff,#a3a3a3);-webkit-background-clip:text;color:transparent;position:relative;}}
.sub{{color:rgba(255,255,255,0.5);font-size:12px;margin-bottom:22px;position:relative;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px;position:relative;}}
.k{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);backdrop-filter:blur(20px);
    border-radius:12px;padding:14px;}}
.kl{{font-family:'JetBrains Mono',monospace;font-size:10px;color:rgba(255,255,255,0.5);}}
.kv{{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:500;margin-top:6px;font-variant-numeric:tabular-nums;}}
table{{width:100%;border-collapse:collapse;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);
       border-radius:12px;overflow:hidden;backdrop-filter:blur(20px);position:relative;}}
th{{font-family:'JetBrains Mono',monospace;font-size:10px;color:rgba(255,255,255,0.5);text-align:left;padding:10px 12px;
   border-bottom:1px solid rgba(255,255,255,0.08);font-weight:500;}}
td{{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,0.04);font-size:12.5px;}}
td.m{{font-family:'JetBrains Mono',monospace;font-size:11px;font-variant-numeric:tabular-nums;}}
</style></head><body>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<h1>Run history</h1><div class="sub">15 runs · glassmorphic / saas marketing</div>
<div class="kpis">
  <div class="k"><div class="kl">RUNS</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">AVG</div><div class="kv" style="color:#fbbf24">77.3</div></div>
  <div class="k"><div class="kl">PASS RATE</div><div class="kv" style="color:#a3e635">73%</div></div>
  <div class="k"><div class="kl">FAIL 7D</div><div class="kv" style="color:#fb7185">4</div></div>
</div>
<table><thead><tr><th>RUN</th><th>SCENARIO</th><th>SCORE</th><th>CRIT</th><th>WHEN</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def variant_ascii_print():
    def bar(score):
        n = int(score / 5)  # 0..20
        return "█" * n + "░" * (20 - n)
    rows = "".join(f"""
      <tr>
        <td>{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])[:36]}</td>
        <td>{int(r['score']):>3}</td>
        <td>{r['crit_pass']}/{r['crit_total']}</td>
        <td>{bar(r['score'])}</td>
        <td>{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums;}}
body{{background:#fafaf5;color:#000;font-size:12px;padding:24px;line-height:1.5;}}
.head{{border-top:2px solid #000;border-bottom:2px solid #000;padding:6px 0;margin-bottom:12px;
       display:flex;justify-content:space-between;font-size:11px;}}
.title{{font-size:16px;font-weight:700;margin:14px 0 2px;}}
.sub{{font-size:11px;color:#555;margin-bottom:14px;}}
.kpis{{margin-bottom:14px;font-size:11.5px;line-height:1.7;}}
.kpis b{{display:inline-block;width:120px;}}
table{{width:100%;border-collapse:collapse;font-size:11.5px;}}
th{{text-align:left;padding:4px 8px;border-bottom:1px solid #000;font-weight:700;text-transform:lowercase;}}
td{{padding:4px 8px;border-bottom:1px dotted #999;}}
.foot{{margin-top:14px;padding-top:6px;border-top:1px solid #000;font-size:10px;color:#666;display:flex;justify-content:space-between;}}
</style></head><body>
<div class="head"><span>CHECKPOINT // RUN LEDGER</span><span>page 01 / 03</span></div>
<div class="title">Recent runs report</div>
<div class="sub">printed 2026-05-15 · ascii receipt mode</div>
<div class="kpis">
  <b>total runs</b> 15<br>
  <b>average score</b> 77.3 / 100<br>
  <b>pass rate</b> 73% (11 / 15)<br>
  <b>failures last 7d</b> 4
</div>
<table>
<thead><tr><th>id</th><th>scenario</th><th>scr</th><th>crit</th><th>histogram</th><th>when</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<div class="foot"><span>** end of page **</span><span>git: 7389674</span></div>
</body></html>"""


# ---------------------------------------------------------------------------
# Comparison page
# ---------------------------------------------------------------------------

VARIANTS = [
    ("A · Paper Brutalist (current)", "Cream paper, ink, 6px offset. Indie/notebook feel.", variant_paper_brutalist()),
    ("B · Dark Warm Inversion", "Same shapes inverted: ink bg, cream text, electric green stays.", variant_dark_warm()),
    ("C · Cool Slate (Vercel/Linear)", "Slate-blue, hairline borders, gradient mark. Enterprise default.", variant_cool_slate()),
    ("D · Terminal / IDE", "Pure mono, tight density, $ prompt header. Engineer-facing.", variant_terminal()),
    ("E · Editorial / Fraunces", "Serif headlines, magazine pacing. NYT-tech-section.", variant_editorial()),
    ("F · Statement Brutalist Color", "Yellow paper, 42px display, color-block KPIs. Distinctive but loud.", variant_statement_color()),
    ("G · Glassmorphic Marketing", "Gradient bg, blur, glow. Modern SaaS marketing site energy.", variant_glassmorphic()),
    ("H · ASCII Receipt / Print", "Mono-only, dotted underlines, histogram bars. Very specific identity.", variant_ascii_print()),
]


def build_comparison():
    cards = []
    for name, blurb, htmldoc in VARIANTS:
        srcdoc = htmldoc.replace('"', '&quot;')
        cards.append(f"""
<section class="card">
  <header>
    <h2>{html.escape(name)}</h2>
    <p>{html.escape(blurb)}</p>
  </header>
  <div class="frame-wrap">
    <iframe srcdoc="{srcdoc}" title="{html.escape(name)}"></iframe>
  </div>
</section>""")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>checkpoint dashboard — aesthetic shotgun</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#1a1a1a;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;padding:24px;}}
h1{{font-size:22px;font-weight:600;margin-bottom:6px;}}
.lede{{color:#999;font-size:13px;margin-bottom:24px;max-width:800px;}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:24px;}}
@media (max-width:1100px){{.grid{{grid-template-columns:1fr;}}}}
.card{{background:#222;border:1px solid #333;border-radius:8px;overflow:hidden;}}
.card header{{padding:14px 18px;border-bottom:1px solid #333;}}
.card h2{{font-size:14px;font-weight:600;margin-bottom:4px;}}
.card p{{font-size:12px;color:#999;}}
.frame-wrap{{height:560px;background:#000;}}
iframe{{width:100%;height:100%;border:0;display:block;}}
</style></head><body>
<h1>checkpoint dashboard — aesthetic shotgun</h1>
<p class="lede">8 variants of the runs page, same data, distinct design DNA.
Same content rendered through 8 different lenses so you can pick the lane that
fits the buyer mental model. Note hard scrolling inside each frame to inspect.</p>
<div class="grid">
{''.join(cards)}
</div>
</body></html>"""


def main():
    out = pathlib.Path("/tmp/dashboard-shotgun.html")
    out.write_text(build_comparison())
    print(f"wrote {out} ({len(VARIANTS)} variants)")
    print(f"file://{out.resolve()}")
    webbrowser.open(f"file://{out.resolve()}")


if __name__ == "__main__":
    main()
