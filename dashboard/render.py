"""HTML rendering — builds the whole single-page board server-side.

No client-side frameworks, no CDN: CSS is inline, charts are inline SVG,
and the page reloads itself every few minutes (wall-board friendly).
"""

import html
from datetime import datetime, timezone

from charts import line_chart, sparkline

import briefing

STYLE = """
:root{
  --bg:#0b0e14; --panel:#12161f; --panel2:#171c27; --line:#232a38;
  --text:#e6e9f0; --muted:#8b93a7; --accent:#4a9eff;
  --up:#26a69a; --down:#ef5350; --warn:#e5a50a;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
  font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  padding:20px 24px 40px}
a{color:var(--accent);text-decoration:none}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:18px}
header h1{font-size:22px;font-weight:700;letter-spacing:.5px}
header h1 span{color:var(--accent)}
.updated{color:var(--muted);font-size:13px}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);
  color:var(--muted)}
.badge.demo{color:var(--warn);border-color:var(--warn)}
h2{font-size:13px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--muted);margin:26px 0 10px;display:flex;align-items:center;gap:10px}
h2 .src{font-weight:400;letter-spacing:0;text-transform:none;font-size:11px}
h2:after{content:"";flex:1;height:1px;background:var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(235px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.card .name{color:var(--muted);font-size:12.5px;font-weight:600;letter-spacing:.3px}
.card .row{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.price{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.chg{font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}
.chg.up{color:var(--up)} .chg.down{color:var(--down)} .chg.flat{color:var(--muted)}
.card .meta{color:var(--muted);font-size:11px}
.spark{width:100%;height:38px}
.section-empty{color:var(--muted);background:var(--panel);border:1px dashed var(--line);
  border-radius:10px;padding:18px;font-size:13px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px}
.bar-row{display:grid;grid-template-columns:150px 1fr 58px;gap:10px;align-items:center;
  padding:4px 0}
.bar-label{font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.bar-label small{color:var(--muted)}
.bar-track{background:var(--panel2);border-radius:4px;height:14px;overflow:hidden}
.bar-fill{height:100%;border-radius:4px}
.bar-fill.hot{background:var(--down)} .bar-fill.warm{background:var(--warn)}
.bar-fill.cool{background:var(--up)}
.bar-val{font-size:13px;font-weight:700;text-align:right;font-variant-numeric:tabular-nums}
.big-chart{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:14px}
.chart{width:100%;height:auto}
.chart-empty{color:var(--muted);font-size:13px;padding:20px 0}
.note{color:var(--muted);font-size:12px;margin-top:8px}
.brief{display:grid;grid-template-columns:minmax(240px,1.15fr) repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:4px 0 8px}
.brief .card{gap:4px}
.brief .card.posture{border-color:#2c4f78;background:#101820}
.brief .kicker{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);font-weight:600}
.brief .posture-name{font-size:22px;font-weight:700;letter-spacing:.2px}
.brief p{font-size:12.5px;color:var(--text)}
.tone-risk{color:var(--down)} .tone-caution{color:var(--warn)} .tone-ok{color:var(--up)} .tone-muted{color:var(--muted)}
footer{margin-top:36px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);
  padding-top:14px}
.warn-inline{color:var(--warn);font-size:11px}
"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="__REFRESH__">
<title>econ — world at a glance</title>
<style>__STYLE__</style>
</head><body>
__BODY__
</body></html>"""


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_num(v, dec=2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{dec}f}"


def fmt_auto(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}" if abs(v) >= 100 else f"{v:,.4f}".rstrip("0").rstrip(".") \
        if abs(v) < 10 else f"{v:,.2f}"


def chg_html(chg, pct):
    if chg is None:
        return '<span class="chg flat">—</span>'
    cls = "up" if chg > 0 else ("down" if chg < 0 else "flat")
    arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "•")
    return (f'<span class="chg {cls}">{arrow} {fmt_num(abs(chg))} '
            f'({pct:+.2f}%)</span>')


def market_card(q):
    if not q:
        return '<div class="card"><div class="name">…</div>' \
               '<div class="section-empty">awaiting data</div></div>'
    up = (q["chg"] or 0) > 0
    src = esc(q.get("source") or "stooq")
    stale = ' · <span class="warn-inline">stale</span>' if q.get("stale") else ""
    return f"""<div class="card">
  <div class="name">{esc(q["name"])}</div>
  <div class="row"><span class="price">{fmt_auto(q["close"])}</span>
  {chg_html(q["chg"], q["chg_pct"])}</div>
  {sparkline(q.get("spark"), up=up)}
  <div class="meta">as of {esc(q.get("date") or "?")} · {src}{stale}</div>
</div>"""


def bar_rows(rows, maxv=None):
    if not rows:
        return '<div class="section-empty">awaiting World Bank data…</div>'
    mx = maxv or max((abs(r["value"]) for r in rows), default=1.0) or 1.0
    out = []
    for r in rows:
        v = r["value"]
        cls = "hot" if v >= 5 else ("warm" if v >= 2.5 else "cool")
        w = max(2.0, min(100.0, abs(v) / mx * 100.0))
        out.append(
            f'<div class="bar-row">'
            f'<span class="bar-label">{esc(r["name"])} <small>({esc(r["year"])})</small></span>'
            f'<div class="bar-track"><div class="bar-fill {cls}" style="width:{w:.0f}%"></div></div>'
            f'<span class="bar-val">{v:+.1f}%</span></div>')
    return "".join(out)


def rate_cards(pairs, series_map):
    """pairs: [(series_id, title)] with series_map: id -> history list."""
    out = []
    for sid, title in pairs:
        hist = series_map.get(sid)
        if not hist:
            out.append(f'<div class="card"><div class="name">{esc(title)}</div>'
                       f'<div class="section-empty">awaiting data…</div></div>')
            continue
        latest = next((v for _, v in reversed(hist) if v is not None), None)
        first = next((v for _, v in hist if v is not None), None)
        diff = None if (latest is None or first is None) else latest - first
        out.append(f"""<div class="card">
  <div class="name">{esc(title)}</div>
  <div class="row"><span class="price">{fmt_num(latest)}%</span>
  {chg_html(diff, abs(diff) / first * 100 if (diff and first) else 0.0) if diff is not None else ""}</div>
  {sparkline([v for _, v in hist[-90:]])}
  <div class="meta">series {esc(sid)} · window ≈ 90 obs</div>
</div>""")
    return "".join(out)


def provider_html(providers, limit=5):
    """One compact footer line explaining which upstreams are being skipped.

    Without this, an empty panel looks like a bug; with it, the board says
    "yahoo: cooling down (HTTP 429), retry in 9m" and nobody panics.
    """
    if not providers:
        return ""
    bits = []
    for p in providers[:limit]:
        mins = int(round((p.get("retry_in") or 0) / 60.0))
        reason = (p.get("reason") or "").strip()
        reason = reason.split(" | ")[0][:70]
        state = "retry in %dm" % mins if p.get("state") == "cooling" and mins else "degraded"
        bits.append(f"{esc(p.get('provider'))}: {state}"
                    + (f" — {esc(reason)}" if reason else "")
                    + (f" ({p.get('fails')} fails)" if p.get("fails") else ""))
    more = f" · +{len(providers) - limit} more" if len(providers) > limit else ""
    return ("<br><span class='warn-inline'>⧗ upstream health: "
            + " · ".join(bits) + esc(more) + "</span>")



def briefing_html(brief):
    """Top-of-board investment read. Missing inputs render as muted cards."""
    if not brief:
        return ""
    tone = esc(brief.get("tone") or "muted")
    cards = [
        f"""<div class="card posture">
  <div class="kicker">Investment briefing</div>
  <div class="posture-name tone-{tone}">{esc(brief.get("posture") or "—")}</div>
  <p>{esc(brief.get("headline") or "")}</p>
  <div class="meta">{esc(brief.get("disclaimer") or "")}</div>
</div>"""
    ]
    for c in brief.get("cards") or []:
        ct = esc(c.get("tone") or "muted")
        cards.append(
            f"""<div class="card">
  <div class="kicker">{esc(c.get("label") or "")}</div>
  <div class="price tone-{ct}">{esc(c.get("value") or "—")}</div>
  <p>{esc(c.get("text") or "")}</p>
</div>"""
        )
    return '<div class="brief">' + "".join(cards) + "</div>"


def build_page(sections, *, demo=False, generated_at=None, refresh=300,
               errors=None, providers=None):
    """sections: dict with keys indices, commodities, fx, cpi, gdp,
    policy_pairs, policy_series, curve, breakeven."""
    if errors is None:
        errors = sections.get("errors") or []
    if providers is None:
        providers = sections.get("providers") or []
    now = generated_at or datetime.now(timezone.utc).astimezone()
    banner = ('<span class="badge demo">DEMO MODE — sample data '
              '(live on the server)</span>' if demo else "")

    body = [f"""<header>
  <h1>ECON<span>.</span>world</h1>
  <span class="updated">updated {esc(now.strftime("%Y-%m-%d %H:%M %Z"))}</span>
  {banner}
</header>"""]

    brief = sections.get("briefing")
    if brief is None:
        try:
            brief = briefing.build_briefing(sections)
        except Exception:
            brief = None
    if brief:
        body.append(briefing_html(brief))

    # --- Markets ------------------------------------------------------------
    body.append('<h2>World indices <span class="src">· daily close (Yahoo / FRED / CNBC / Stooq)</span></h2>')
    if sections["indices"]:
        body.append('<div class="grid">' + "".join(market_card(q) for q in sections["indices"]) + "</div>")
    else:
        body.append('<div class="section-empty">Fetching index history… first load takes '
                    'a few seconds (server warms its cache in the background).</div>')

    body.append('<h2>Commodities <span class="src">· the pulse of the real economy</span></h2>')
    if sections["commodities"]:
        body.append('<div class="grid">' + "".join(market_card(q) for q in sections["commodities"]) + "</div>")
    else:
        body.append('<div class="section-empty">Awaiting commodity data…</div>')

    # --- FX ------------------------------------------------------------------
    body.append('<h2>Currencies <span class="src">· USD base · ECB reference rates</span></h2>')
    if sections["fx"]:
        body.append('<div class="grid">' + "".join(market_card(q) for q in sections["fx"]) + "</div>")
    else:
        body.append('<div class="section-empty">Awaiting FX data…</div>')

    # --- Macro bars ----------------------------------------------------------
    body.append('<h2>Macro — inflation &amp; growth <span class="src">· World Bank, annual</span></h2>')
    body.append('<div class="cols">'
                '<div><div class="card"><div class="name">Inflation — CPI, % YoY</div>'
                + bar_rows(sections["cpi"]) + "</div></div>"
                '<div><div class="card"><div class="name">Real GDP growth, % YoY</div>'
                + bar_rows(sections["gdp"], maxv=8.0) + "</div></div>"
                "</div>")

    # --- Policy rates --------------------------------------------------------
    body.append('<h2>Central-bank policy rates <span class="src">· FRED / NY Fed / ECB</span></h2>')
    body.append('<div class="grid">' +
                rate_cards(sections["policy_pairs"], sections["policy_series"]) + "</div>")

    # --- Yield curve ---------------------------------------------------------
    sid, title, years = sections["curve_meta"]
    hist = sections["curve"]
    body.append(f'<h2>Yield curve <span class="src">· {esc(sid)} · last {years}y</span></h2>')
    if hist:
        vals = [v for _, v in hist]
        latest = next((v for v in reversed(vals) if v is not None), None)
        inverted = latest is not None and latest < 0
        color = "#ef5350" if inverted else "#26a69a"
        note = ("⚠ Inverted — spread below zero (classic recession warning)"
                if inverted else "Positive — curve not inverted")
        body.append(f'<div class="big-chart">{line_chart(vals, color=color)}'
                    f'<div class="note">{esc(title)} · latest <b>{fmt_num(latest)}%</b> — '
                    f'{note}</div></div>')
    else:
        body.append('<div class="section-empty">Awaiting yield-curve data…</div>')

    # --- Breakeven inflation ---------------------------------------------------
    bsid, btitle = sections["breakeven_meta"]
    bhist = sections["breakeven"]
    body.append(f'<h2>Breakeven inflation <span class="src">· {esc(bsid)}</span></h2>')
    if bhist:
        bvals = [v for _, v in bhist]
        latest = next((v for v in reversed(bvals) if v is not None), None)
        body.append(f'<div class="big-chart">{line_chart(bvals, color="#4a9eff")}'
                    f'<div class="note">{esc(btitle)} · market-implied avg inflation over 10y · '
                    f'latest <b>{fmt_num(latest)}%</b></div></div>')
    else:
        body.append('<div class="section-empty">Awaiting breakeven data…</div>')

    # --- Footer ----------------------------------------------------------------
    warn = ""
    if errors:
        warn = "<br><span class='warn-inline'>⚠ source issues: " + esc("; ".join(errors)) + "</span>"
    prov = provider_html(providers)
    body.append(f"""<footer>
Sources: Yahoo Finance / FRED / Stooq (quotes) · Frankfurter/ECB (FX) · World Bank (macro aggregates) ·
FRED / NY Fed / ECB / US Treasury (rates &amp; curve). Data for information only — not investment advice.{warn}{prov}
</footer>""")

    return (PAGE.replace("__STYLE__", STYLE)
                .replace("__REFRESH__", str(max(60, int(refresh))))
                .replace("__BODY__", "".join(body)))
