"""Shotgun v3 — survivors after v2 feedback.

User feedback:
- I1 + I2 = same design system (light/dark) → keep both, present together
- I3 = killed (no vertical rules in table → hard to scan)
- I4 = the winner (predictable, terminal/VS Code feel)
- New: I4-light needed for daytime parity

Now generating 4 finalists for design-review skill:
  Final-A (= I2)   Dark Warm · Accent Rail
  Final-B (= I1)   Paper · Accent Rail
  Final-C (= I4d)  Dark Warm · Dense / Terminal-fused
  Final-D (= I4l)  Paper · Dense / Terminal-fused (NEW)
"""
import json, html, pathlib, webbrowser, datetime

SEED = json.loads(pathlib.Path("/tmp/run_seed.json").read_text())[:5]


def reltime(ts: str) -> str:
    if not ts: return "—"
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        s = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
        if s < 60: return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        if s < 86400: return f"{s//3600}h ago"
        return f"{s//86400}d ago"
    except Exception:
        return ts[:10]


def v_paper_rail():
    rows = "".join(f"""
      <tr>
        <td class="m">{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td class="m"><b style="color:{ '#0ea83b' if r['score']>=100 else '#c89124' if r['score']>=50 else '#d73838'}">{int(r['score'])}</b></td>
        <td class="m">{r['crit_pass']}/{r['crit_total']}</td>
        <td class="m" style="color:#7d7568">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Geist',sans-serif;}}
body{{background:#f5f2ea;color:#0a0a0a;font-size:13px;padding:24px;line-height:1.5;font-variant-numeric:tabular-nums;}}
.brand{{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14.5px;margin-bottom:24px;}}
.mark{{width:12px;height:12px;background:#0a0a0a;position:relative;}}
.mark::after{{content:'';width:5px;height:5px;background:#2dff5c;position:absolute;top:3.5px;left:3.5px;}}
h1{{font-size:22px;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px;}}
.sub{{color:#7d7568;font-size:12px;margin-bottom:22px;font-family:'Geist Mono',monospace;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin-bottom:24px;border:1px solid #0a0a0a;background:#fff;}}
.k{{padding:14px 16px;border-right:1px solid #e0dccf;border-left:2px solid #2dff5c;}}
.k:last-child{{border-right:none;}}
.kl{{font-family:'Geist Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:0.1em;color:#7d7568;}}
.kv{{font-family:'Geist Mono',monospace;font-size:22px;font-weight:500;margin-top:4px;}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #0a0a0a;border-left:2px solid #2dff5c;}}
th{{font-family:'Geist Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:0.1em;
   color:#7d7568;text-align:left;padding:9px 12px;border-bottom:1px solid #0a0a0a;font-weight:500;background:#f5f2ea;}}
td{{padding:10px 12px;border-bottom:1px solid #e0dccf;font-size:12.5px;}}
td.m{{font-family:'Geist Mono',monospace;font-size:11.5px;}}
tr:last-child td{{border-bottom:none;}} tr:hover td{{background:#f9f6ef;}}
</style></head><body>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<h1>Run history</h1><div class="sub">15 runs · 4 failures last 7d</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Avg</div><div class="kv" style="color:#c89124">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv">73%</div></div>
  <div class="k"><div class="kl">Fail 7d</div><div class="kv" style="color:#d73838">4</div></div>
</div>
<table><thead><tr><th>Run</th><th>Scenario</th><th>Score</th><th>Crit</th><th>When</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def v_dark_rail():
    rows = "".join(f"""
      <tr>
        <td class="m">{r['id'][:8]}</td>
        <td>{html.escape(r['scenario'])}</td>
        <td class="m"><b style="color:{'#4ade80' if r['score']>=100 else '#fbbf24' if r['score']>=50 else '#f87171'}">{int(r['score'])}</b></td>
        <td class="m">{r['crit_pass']}/{r['crit_total']}</td>
        <td class="m" style="color:#8a8472">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Geist',sans-serif;}}
body{{background:#0e0e10;color:#ececf0;font-size:13px;padding:24px;line-height:1.5;font-variant-numeric:tabular-nums;}}
.brand{{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14.5px;margin-bottom:24px;}}
.mark{{width:12px;height:12px;background:#ececf0;position:relative;}}
.mark::after{{content:'';width:5px;height:5px;background:#2dff5c;position:absolute;top:3.5px;left:3.5px;box-shadow:0 0 4px #2dff5c;}}
h1{{font-size:22px;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px;}}
.sub{{color:#8a8472;font-size:12px;margin-bottom:22px;font-family:'Geist Mono',monospace;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin-bottom:24px;border:1px solid #2a2a30;background:#161618;}}
.k{{padding:14px 16px;border-right:1px solid #2a2a30;border-left:2px solid #2dff5c;}}
.k:last-child{{border-right:none;}}
.kl{{font-family:'Geist Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:0.1em;color:#8a8472;}}
.kv{{font-family:'Geist Mono',monospace;font-size:22px;font-weight:500;margin-top:4px;}}
table{{width:100%;border-collapse:collapse;background:#161618;border:1px solid #2a2a30;border-left:2px solid #2dff5c;}}
th{{font-family:'Geist Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:0.1em;
   color:#8a8472;text-align:left;padding:9px 12px;border-bottom:1px solid #2a2a30;font-weight:500;background:#1c1c1f;}}
td{{padding:10px 12px;border-bottom:1px solid #232328;font-size:12.5px;}}
td.m{{font-family:'Geist Mono',monospace;font-size:11.5px;}}
tr:last-child td{{border-bottom:none;}} tr:hover td{{background:#1c1c1f;}}
</style></head><body>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<h1>Run history</h1><div class="sub">15 runs · 4 failures last 7d</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Avg</div><div class="kv" style="color:#fbbf24">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv" style="color:#4ade80">73%</div></div>
  <div class="k"><div class="kl">Fail 7d</div><div class="kv" style="color:#f87171">4</div></div>
</div>
<table><thead><tr><th>Run</th><th>Scenario</th><th>Score</th><th>Crit</th><th>When</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def v_dense_dark():
    rows = "".join(f"""
      <tr>
        <td>{r['id'][:8]}</td>
        <td class="sans">{html.escape(r['scenario'])}</td>
        <td style="color:{'#4ade80' if r['score']>=100 else '#fbbf24' if r['score']>=50 else '#f87171'}">{int(r['score']):>3}</td>
        <td>{r['crit_pass']}/{r['crit_total']}</td>
        <td style="color:#8a8472">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0e0e10;color:#ececf0;font-size:12px;padding:0;line-height:1.5;font-variant-numeric:tabular-nums;
      font-family:'Geist Mono',monospace;}}
.bar{{background:#000;color:#ececf0;padding:5px 16px;display:flex;justify-content:space-between;
      font-size:10.5px;border-bottom:1px solid #2a2a30;letter-spacing:0.03em;}}
.bar .left{{display:flex;gap:14px;align-items:center;}}
.bar .left .pulse{{width:6px;height:6px;background:#2dff5c;box-shadow:0 0 6px #2dff5c;border-radius:50%;animation:p 1.5s infinite;}}
@keyframes p{{50%{{opacity:0.5;}}}}
.bar .right{{color:#8a8472;display:flex;gap:14px;}} .bar .right b{{color:#ececf0;font-weight:500;}}
.brand{{font-family:'Geist',sans-serif;font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px;padding:18px 24px 0;}}
.brand .mark{{width:11px;height:11px;background:#ececf0;position:relative;}}
.brand .mark::after{{content:'';width:4.5px;height:4.5px;background:#2dff5c;position:absolute;top:3.25px;left:3.25px;}}
.kicker{{font-family:'Geist Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:0.12em;
        color:#8a8472;padding:14px 24px 4px;}}
h1{{font-family:'Geist',sans-serif;font-size:22px;font-weight:600;letter-spacing:-0.02em;padding:0 24px 4px;line-height:1.05;}}
.sub{{padding:0 24px 18px;color:#8a8472;font-size:11.5px;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:0 24px 18px;border:1px solid #2a2a30;}}
.k{{padding:11px 14px;border-right:1px solid #2a2a30;}} .k:last-child{{border-right:none;}}
.k .kl{{font-size:9.5px;color:#8a8472;text-transform:uppercase;letter-spacing:0.1em;}}
.k .kv{{font-size:20px;font-weight:500;margin-top:3px;color:#ececf0;}}
.sect-head{{padding:0 24px;display:flex;justify-content:space-between;font-size:10px;color:#8a8472;
            text-transform:uppercase;letter-spacing:0.1em;margin-bottom:6px;align-items:center;}}
.sect-head .green{{color:#2dff5c;}}
table{{width:100%;border-collapse:collapse;font-size:11.5px;border-top:1px solid #2a2a30;border-bottom:1px solid #2a2a30;}}
th{{text-align:left;padding:7px 14px;border-bottom:1px solid #2a2a30;font-weight:500;font-size:9.5px;
   color:#8a8472;text-transform:uppercase;letter-spacing:0.06em;background:#16161a;
   border-right:1px solid #232328;}}
th:last-child{{border-right:none;}}
th:first-child{{padding-left:24px;}} th:last-child{{padding-right:24px;}}
td{{padding:7px 14px;border-bottom:1px solid #1c1c1f;border-right:1px solid #1a1a1d;}}
td:last-child{{border-right:none;}}
td:first-child{{padding-left:24px;}} td:last-child{{padding-right:24px;}}
td.sans{{font-family:'Geist',sans-serif;font-size:12.5px;}}
tr:last-child td{{border-bottom:none;}} tr:hover td{{background:#16161a;}}
</style></head><body>
<div class="bar">
  <div class="left"><span class="pulse"></span><span>checkpoint</span><span>runs.live</span></div>
  <div class="right"><span>15 runs</span><span>JUDGE <b>gpt-4o-mini</b></span><span>BUILD <b>v0.1.0</b></span></div>
</div>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<div class="kicker">Recent activity</div>
<h1>Run history</h1>
<div class="sub">15 records · 4 failures in the last 7 days</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Avg</div><div class="kv" style="color:#fbbf24">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv" style="color:#4ade80">73%</div></div>
  <div class="k"><div class="kl">Fail 7d</div><div class="kv" style="color:#f87171">4</div></div>
</div>
<div class="sect-head"><span>Latest runs</span><span class="green">tail -f →</span></div>
<table><thead><tr><th style="width:90px">id</th><th>scenario</th><th style="width:50px">score</th><th style="width:50px">crit</th><th style="width:80px">when</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def v_dense_light():
    """I4 light: same VS-Code-table-feel but on paper background.
    Util bar stays inverted (dark) for high-contrast strip & keeps the pulse signature.
    Body is paper, ink, with hairline column dividers, mono body, sans headlines.
    """
    rows = "".join(f"""
      <tr>
        <td>{r['id'][:8]}</td>
        <td class="sans">{html.escape(r['scenario'])}</td>
        <td style="color:{'#0ea83b' if r['score']>=100 else '#c89124' if r['score']>=50 else '#d73838'}">{int(r['score']):>3}</td>
        <td>{r['crit_pass']}/{r['crit_total']}</td>
        <td style="color:#7d7568">{reltime(r['ts'])}</td>
      </tr>""" for r in SEED)
    return f"""<!doctype html><html><head><style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#f5f2ea;color:#0a0a0a;font-size:12px;padding:0;line-height:1.5;font-variant-numeric:tabular-nums;
      font-family:'Geist Mono',monospace;}}
/* util bar: stays dark for high-contrast strip + signature pulse */
.bar{{background:#0a0a0a;color:#f5f2ea;padding:5px 16px;display:flex;justify-content:space-between;
      font-size:10.5px;border-bottom:1px solid #0a0a0a;letter-spacing:0.03em;}}
.bar .left{{display:flex;gap:14px;align-items:center;}}
.bar .left .pulse{{width:6px;height:6px;background:#2dff5c;box-shadow:0 0 6px #2dff5c;border-radius:50%;animation:p 1.5s infinite;}}
@keyframes p{{50%{{opacity:0.5;}}}}
.bar .right{{color:rgba(245,242,234,0.55);display:flex;gap:14px;}}
.bar .right b{{color:#f5f2ea;font-weight:500;}}
.brand{{font-family:'Geist',sans-serif;font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px;padding:18px 24px 0;}}
.brand .mark{{width:11px;height:11px;background:#0a0a0a;position:relative;}}
.brand .mark::after{{content:'';width:4.5px;height:4.5px;background:#2dff5c;position:absolute;top:3.25px;left:3.25px;}}
.kicker{{font-family:'Geist Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:0.12em;
        color:#7d7568;padding:14px 24px 4px;}}
h1{{font-family:'Geist',sans-serif;font-size:22px;font-weight:600;letter-spacing:-0.02em;padding:0 24px 4px;line-height:1.05;}}
.sub{{padding:0 24px 18px;color:#7d7568;font-size:11.5px;}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:0 24px 18px;border:1px solid #0a0a0a;background:#fff;}}
.k{{padding:11px 14px;border-right:1px solid #d6d0c5;}} .k:last-child{{border-right:none;}}
.k .kl{{font-size:9.5px;color:#7d7568;text-transform:uppercase;letter-spacing:0.1em;}}
.k .kv{{font-size:20px;font-weight:500;margin-top:3px;color:#0a0a0a;}}
.sect-head{{padding:0 24px;display:flex;justify-content:space-between;font-size:10px;color:#7d7568;
            text-transform:uppercase;letter-spacing:0.1em;margin-bottom:6px;align-items:center;}}
.sect-head .green{{color:#0ea83b;}}
table{{width:100%;border-collapse:collapse;font-size:11.5px;background:#fff;border-top:1px solid #0a0a0a;border-bottom:1px solid #0a0a0a;}}
th{{text-align:left;padding:7px 14px;border-bottom:1px solid #0a0a0a;font-weight:500;font-size:9.5px;
   color:#7d7568;text-transform:uppercase;letter-spacing:0.06em;background:#f5f2ea;
   border-right:1px solid #e0dccf;}}
th:last-child{{border-right:none;}}
th:first-child{{padding-left:24px;}} th:last-child{{padding-right:24px;}}
td{{padding:7px 14px;border-bottom:1px solid #e0dccf;border-right:1px solid #ebe7dc;}}
td:last-child{{border-right:none;}}
td:first-child{{padding-left:24px;}} td:last-child{{padding-right:24px;}}
td.sans{{font-family:'Geist',sans-serif;font-size:12.5px;}}
tr:last-child td{{border-bottom:none;}} tr:hover td{{background:#fbf9f3;}}
</style></head><body>
<div class="bar">
  <div class="left"><span class="pulse"></span><span>checkpoint</span><span>runs.live</span></div>
  <div class="right"><span>15 runs</span><span>JUDGE <b>gpt-4o-mini</b></span><span>BUILD <b>v0.1.0</b></span></div>
</div>
<div class="brand"><span class="mark"></span><span>checkpoint</span></div>
<div class="kicker">Recent activity</div>
<h1>Run history</h1>
<div class="sub">15 records · 4 failures in the last 7 days</div>
<div class="kpis">
  <div class="k"><div class="kl">Runs</div><div class="kv">15</div></div>
  <div class="k"><div class="kl">Avg</div><div class="kv" style="color:#c89124">77.3</div></div>
  <div class="k"><div class="kl">Pass rate</div><div class="kv" style="color:#0ea83b">73%</div></div>
  <div class="k"><div class="kl">Fail 7d</div><div class="kv" style="color:#d73838">4</div></div>
</div>
<div class="sect-head"><span>Latest runs</span><span class="green">tail -f →</span></div>
<table><thead><tr><th style="width:90px">id</th><th>scenario</th><th style="width:50px">score</th><th style="width:50px">crit</th><th style="width:80px">when</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


VARIANTS = [
    ("Final-A · Paper · Accent Rail (=I1)",
     "Hairline borders, green left-rail on KPI block + table. Sober daytime.",
     v_paper_rail()),
    ("Final-B · Dark Warm · Accent Rail (=I2)",
     "Same rail identity, dark mode. Inverted daytime.",
     v_dark_rail()),
    ("Final-C · Dark Warm · Dense Terminal (=I4 dark)",
     "Util bar with pulse, sans headlines + mono body, full column rules — VS Code/IDE feel.",
     v_dense_dark()),
    ("Final-D · Paper · Dense Terminal (NEW: I4 light)",
     "I4's structure in daytime: dark util bar (signature), paper body, mono body, ruled columns.",
     v_dense_light()),
]


def build_comparison():
    cards = []
    for name, blurb, htmldoc in VARIANTS:
        srcdoc = html.escape(htmldoc, quote=True)
        cards.append(f"""
<section class="card">
  <header><h2>{html.escape(name)}</h2><p>{html.escape(blurb)}</p></header>
  <div class="frame-wrap"><iframe srcdoc="{srcdoc}" title="{html.escape(name)}"></iframe></div>
</section>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>shotgun v3 — finalists</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#1a1a1a;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;padding:24px;}}
h1{{font-size:22px;font-weight:600;margin-bottom:6px;}}
.lede{{color:#999;font-size:13px;margin-bottom:24px;max-width:880px;}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:24px;}}
@media (max-width:1100px){{.grid{{grid-template-columns:1fr;}}}}
.card{{background:#222;border:1px solid #333;border-radius:8px;overflow:hidden;}}
.card header{{padding:14px 18px;border-bottom:1px solid #333;}}
.card h2{{font-size:14px;font-weight:600;margin-bottom:4px;}}
.card p{{font-size:12px;color:#999;}}
.frame-wrap{{height:620px;background:#000;}}
iframe{{width:100%;height:100%;border:0;display:block;}}
</style></head><body>
<h1>checkpoint dashboard — shotgun v3 (finalists)</h1>
<p class="lede">After v2 feedback: I3 dropped (no column rules), I1+I2 kept (same system, two modes), I4 favored (terminal/IDE predictability). Adding I4-light for daytime parity. 4 finalists.</p>
<div class="grid">{''.join(cards)}</div>
</body></html>"""


def main():
    out = pathlib.Path("/tmp/shotgun-v3.html")
    out.write_text(build_comparison())
    # Also write each variant standalone for the design-review skill
    for i, (name, _, htmldoc) in enumerate(VARIANTS, 1):
        slug = ["a-paper-rail","b-dark-rail","c-dense-dark","d-dense-light"][i-1]
        pathlib.Path(f"/tmp/variant-{slug}.html").write_text(htmldoc)
    print(f"wrote {out} + 4 standalone variants in /tmp/variant-*.html")
    print(f"file://{out.resolve()}")
    webbrowser.open(f"file://{out.resolve()}")


if __name__ == "__main__":
    main()
