"""HTML rendering — builds the whole single-page board server-side.

No client-side frameworks, no CDN: CSS is inline, charts are inline SVG,
and the page reloads itself every few minutes (wall-board friendly).
"""

import html
import math
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:8px}
nav.tabs{display:flex;gap:8px;margin:0 0 18px}
nav.tabs a{font-size:13px;color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:4px 10px}
nav.tabs a.on{color:var(--text);border-color:var(--accent);background:#121a28}
.sum{display:grid;gap:12px}
.sum section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.sum h3{font-size:13px;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.sum p{font-size:14.5px;margin:0 0 8px}
.sum ul{margin:0;padding-left:18px}
.sum li{margin:3px 0}
.gauge{width:min(420px,100%);height:auto;display:block;margin:4px 0 8px}
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
.stock-intro{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.stock-intro p{font-size:13px;color:var(--muted);margin-top:5px}
.stock-status{padding:11px 14px;background:var(--panel);border:1px solid var(--line);border-radius:9px;margin:12px 0;font-size:13px}
.stock-status.ok{border-color:#27685e}.stock-status.warn{border-color:#78631e}.stock-status.bad{border-color:#773c3a}
.stock-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin:12px 0}
.stock-card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px}
.stock-head{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.stock-rank{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
.stock-symbol{font-size:21px;font-weight:750;letter-spacing:.2px}
.stock-name{color:var(--muted);font-size:12px}
.stock-price{font-size:23px;font-weight:700;font-variant-numeric:tabular-nums}
.stock-myr{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.stock-myr b{color:var(--text);font-size:12px}
.stock-myr small{margin-left:4px}
.stock-currency-note,.stock-verification{color:var(--muted);font-size:11.5px;margin-top:7px}
.stock-currency-note a,.stock-verification a{text-decoration:underline}
.stock-diagnostics{border-top:1px solid var(--line);padding-top:7px;margin-top:7px;color:var(--warn);font-size:11px;overflow-wrap:anywhere}
.risk-badge{display:inline-block;border:1px solid;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700;white-space:nowrap}
.risk-low{color:var(--up);border-color:#27685e;background:#10221f}
.risk-medium{color:var(--warn);border-color:#78631e;background:#211d10}
.risk-high{color:var(--down);border-color:#773c3a;background:#251414}
.stock-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(78px,1fr));gap:7px}
.stock-stat{background:var(--panel2);border-radius:7px;padding:7px 8px}
.stock-stat small{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.stock-stat b{display:block;font-size:13px;margin-top:2px;font-variant-numeric:tabular-nums}
.stock-pct.up{color:var(--up)} .stock-pct.down{color:var(--down)} .stock-pct.flat{color:var(--muted)}
.stock-reasons{padding-left:18px;color:var(--muted);font-size:12px}
.stock-reasons li{margin:2px 0}
.stock-meta{color:var(--muted);font-size:10.5px;border-top:1px solid var(--line);padding-top:7px}
.stock-spark svg{width:100%;height:44px;display:block}
.stock-week{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}
.stock-week h3{font-size:12px;letter-spacing:1.1px;text-transform:uppercase;color:var(--muted);display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;margin-bottom:8px}
.stock-week h3 .src{font-weight:400;letter-spacing:0;text-transform:none;font-size:11px}
.stock-week-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:8px}
.stock-week-stat{background:var(--panel2);border-radius:8px;padding:8px 10px}
.stock-week-stat small{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.stock-week-stat b{display:block;font-size:15px;margin-top:2px;font-variant-numeric:tabular-nums}
.stock-week p{color:var(--muted);font-size:11.5px;margin-top:8px}
.stock-news{border-top:1px solid var(--line);padding-top:8px}
.stock-news .news-head{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.stock-news ul{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:6px}
.stock-news li{font-size:12.5px;line-height:1.35}
.stock-news a{color:var(--text)}
.stock-news a:hover{color:var(--accent);text-decoration:underline}
.stock-news small{display:block;color:var(--muted);font-size:10.5px;margin-top:1px}
.stock-news .news-empty{color:var(--muted);font-size:11.5px}
.stock-method{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 16px;margin-top:16px}
.stock-method summary{cursor:pointer;font-weight:650;font-size:13px}
.stock-method p,.stock-method li{color:var(--muted);font-size:12px;margin-top:6px}
.stock-method ul{padding-left:19px;margin-top:6px}
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


def _parse_timestamp(value):
    if isinstance(value, datetime):
        moment = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        moment = datetime.fromisoformat(raw)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _dual_market_time(value):
    """Format a timestamp in explicit Malaysia and U.S. Eastern zones."""
    if not value:
        return "not yet refreshed"
    try:
        moment = _parse_timestamp(value)
        myt = moment.astimezone(ZoneInfo("Asia/Kuala_Lumpur"))
        eastern = moment.astimezone(ZoneInfo("America/New_York"))
        return (
            f"Kuala Lumpur: {myt.strftime('%Y-%m-%d %H:%M')} MYT · "
            f"U.S. Eastern: {eastern.strftime('%Y-%m-%d %H:%M %Z')}"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return esc(value)


def _myt_timestamp(value):
    if not value:
        return "timestamp unavailable"
    try:
        moment = _parse_timestamp(value).astimezone(ZoneInfo("Asia/Kuala_Lumpur"))
        return moment.strftime("%Y-%m-%d %H:%M MYT")
    except (TypeError, ValueError, OverflowError, OSError):
        return esc(value)


def fmt_num(v, dec=2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{dec}f}"


def _safe_link(url):
    """Return an href only for absolute http(s) URLs from a third-party feed."""
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return candidate


def _news_age(value, now=None) -> str:
    """Coarse age for a headline timestamp; empty string when unparseable."""
    try:
        published = _parse_timestamp(value)
        moment = _parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
        seconds = (moment - published).total_seconds()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    if seconds < 0:
        return "just now"
    if seconds < 90 * 60:
        return f"{max(1, round(seconds / 60))} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


def _pct_span(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '<span class="stock-pct flat">—</span>'
    cls = "up" if number > 0 else ("down" if number < 0 else "flat")
    return f'<span class="stock-pct {cls}">{number:+.1f}%</span>'


def _stock_week_html(week) -> str:
    """The 7-day movement strip above the cards (descriptive, not a forecast)."""
    if not isinstance(week, dict) or not week.get("count"):
        return ""
    best = week.get("best") or {}
    worst = week.get("worst") or {}

    def named(item):
        symbol = esc(item.get("symbol") or "?")
        return f"{symbol} {_pct_span(item.get('ret7_pct'))}"

    start = esc(week.get("start_date") or "?")
    end = esc(week.get("end_date") or "?")
    window = int(week.get("window_days") or 7)
    return (
        '<section class="stock-week">'
        f'<h3>7-day price movement <span class="src">daily closes · '
        f'{start} → {end}</span></h3>'
        '<div class="stock-week-grid">'
        f'<div class="stock-week-stat"><small>Average {window}-day move</small>'
        f'<b>{_pct_span(week.get("average_ret7_pct"))}</b></div>'
        '<div class="stock-week-stat"><small>Up / down</small>'
        f'<b>{int(week.get("up") or 0)} / {int(week.get("down") or 0)}</b></div>'
        f'<div class="stock-week-stat"><small>Strongest</small><b>{named(best)}</b></div>'
        f'<div class="stock-week-stat"><small>Weakest</small><b>{named(worst)}</b></div>'
        '</div>'
        f'<p>{int(week.get("count") or 0)} displayed stock(s) · {window} calendar '
        'days ≈ five U.S. trading sessions. The window starts at the last close '
        'at or before that boundary and ends at the latest daily bar, so it is '
        'not an intraday move.</p>'
        '</section>'
    )


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



def tabs(active):
    def link(href, key, label):
        cls = "on" if active == key else ""
        return f'<a class="{cls}" href="{href}">{label}</a>'
    return ('<nav class="tabs">'
            + link("/", "board", "Board")
            + link("/summary", "summary", "Summary")
            + link("/stocks", "stocks", "Stocks")
            + "</nav>")



def inflation_gauge(value, label="10y breakeven"):
    """Semicircle meter, 0-5%. Needle is market-expected inflation, not a forecast."""
    if value is None:
        return "<p>Inflation meter needs a breakeven or CPI print.</p>"
    import math
    v = max(0.0, min(5.0, float(value)))
    ang = math.radians(180 - (v / 5.0) * 180)
    cx, cy, r = 110, 108, 78
    nx = cx + r * math.cos(ang)
    ny = cy - r * math.sin(ang)
    word = "below target" if v < 2 else ("near a 2% anchor" if v < 3 else "hot")
    return (
        f'<svg class="gauge" viewBox="0 0 220 130" role="img" aria-label="inflation meter {v:.2f} percent">'
        f'<path d="M32 108 A78 78 0 0 1 188 108" fill="none" stroke="#232a38" stroke-width="14" stroke-linecap="round"/>'
        f'<path d="M32 108 A78 78 0 0 1 94 36" fill="none" stroke="#26a69a" stroke-width="14"/>'
        f'<path d="M94 36 A78 78 0 0 1 126 36" fill="none" stroke="#e5a50a" stroke-width="14"/>'
        f'<path d="M126 36 A78 78 0 0 1 188 108" fill="none" stroke="#ef5350" stroke-width="14" stroke-linecap="round"/>'
        f'<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="#e6e9f0" stroke-width="3" stroke-linecap="round"/>'
        f'<circle cx="{cx}" cy="{cy}" r="4" fill="#e6e9f0"/>'
        f'<text x="28" y="124" fill="#8b93a7" font-size="11">0</text>'
        f'<text x="100" y="22" fill="#8b93a7" font-size="11">2.5</text>'
        f'<text x="184" y="124" fill="#8b93a7" font-size="11">5%</text>'
        f'<text x="110" y="78" fill="#e6e9f0" font-size="18" font-weight="700" text-anchor="middle">{v:.2f}%</text>'
        f'<text x="110" y="96" fill="#8b93a7" font-size="11" text-anchor="middle">{esc(label)} · {word}</text>'
        f'</svg>'
    )



def recession_gauge(spread):
    """Semicircle for the 10y-2y spread. Left is inverted, right is a normal slope."""
    if spread is None:
        return "<p>Recession meter needs the 10y-2y spread.</p>"
    import math
    # Map -1.0 (inverted, left/red) to +1.5 (steep, right/green) onto the arc.
    lo, hi = -1.0, 1.5
    v = max(lo, min(hi, float(spread)))
    frac = (v - lo) / (hi - lo)
    ang = math.radians(180 - frac * 180)
    cx, cy, r = 110, 108, 78
    nx = cx + r * math.cos(ang)
    ny = cy - r * math.sin(ang)
    if spread < 0:
        word = "inverted — recession warning"
    elif spread < 0.5:
        word = "flat — little cushion"
    else:
        word = "positive — not inverted"
    return (
        f'<svg class="gauge" viewBox="0 0 220 130" role="img" aria-label="recession meter {spread:.2f} percent">'
        f'<path d="M32 108 A78 78 0 0 1 188 108" fill="none" stroke="#232a38" stroke-width="14" stroke-linecap="round"/>'
        f'<path d="M32 108 A78 78 0 0 1 94 36" fill="none" stroke="#ef5350" stroke-width="14"/>'
        f'<path d="M94 36 A78 78 0 0 1 126 36" fill="none" stroke="#e5a50a" stroke-width="14"/>'
        f'<path d="M126 36 A78 78 0 0 1 188 108" fill="none" stroke="#26a69a" stroke-width="14" stroke-linecap="round"/>'
        f'<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="#e6e9f0" stroke-width="3" stroke-linecap="round"/>'
        f'<circle cx="{cx}" cy="{cy}" r="4" fill="#e6e9f0"/>'
        f'<text x="18" y="124" fill="#8b93a7" font-size="11">-1</text>'
        f'<text x="96" y="22" fill="#8b93a7" font-size="11">0</text>'
        f'<text x="176" y="124" fill="#8b93a7" font-size="11">+1.5</text>'
        f'<text x="110" y="78" fill="#e6e9f0" font-size="18" font-weight="700" text-anchor="middle">{spread:.2f}%</text>'
        f'<text x="110" y="96" fill="#8b93a7" font-size="11" text-anchor="middle">10y-2y · {word}</text>'
        f'</svg>'
    )


def _fmt_quote(q):
    if not q:
        return None
    name = q.get("name") or "quote"
    close = q.get("close")
    if close is None:
        return f"{name}: awaiting data"
    try:
        close_s = f"{float(close):,.2f}"
    except (TypeError, ValueError):
        close_s = str(close)
    chg = q.get("chg_pct")
    move = ""
    if chg is not None:
        try:
            move = f" {float(chg):+.1f}%"
        except (TypeError, ValueError):
            move = ""
    src = q.get("source") or ""
    when = q.get("date") or "?"
    stale = " stale" if q.get("stale") else ""
    return f"{name}: {close_s}{move} ({when}, {src}{stale})"


def build_summary_page(sections, *, demo=False, generated_at=None, refresh=300):
    """A second tab: prose summary of whatever the board has already fetched."""
    now = generated_at or datetime.now(timezone.utc).astimezone()
    banner = ('<span class="badge demo">DEMO MODE — sample data '
              '(live on the server)</span>' if demo else "")
    brief = sections.get("briefing") or {}
    indices = list(sections.get("indices") or [])
    commodities = list(sections.get("commodities") or [])
    fx = list(sections.get("fx") or [])
    cpi = sections.get("cpi") or []
    gdp = sections.get("gdp") or []

    def bullets(items, limit=12):
        lines = []
        for q in items[:limit]:
            text = _fmt_quote(q)
            if text:
                lines.append(f"<li>{esc(text)}</li>")
            elif q:
                lines.append(f"<li>{esc(q.get('name') or 'quote')}: awaiting data</li>")
        if not lines:
            return "<p>Nothing loaded yet — the refresher has not filled this panel.</p>"
        return "<ul>" + "".join(lines) + "</ul>"

    def macro_lines(rows, suffix="%"):
        if not rows:
            return "<p>Nothing loaded.</p>"
        bits = "".join(
            f"<li>{esc(r.get('name'))} {r.get('value'):+.1f}{suffix} ({esc(r.get('year') or '?')})</li>"
            for r in rows if r.get("value") is not None)
        return "<ul>" + bits + "</ul>"

    curve = sections.get("curve") or []
    curve_v = next((v for _, v in reversed(curve) if v is not None), None)
    be = sections.get("breakeven") or []
    be_v = next((v for _, v in reversed(be) if v is not None), None)
    real = sections.get("real_yield") or []
    real_v = next((v for _, v in reversed(real) if v is not None), None)
    policy = sections.get("policy_series") or {}
    fed = next((v for _, v in reversed(policy.get("DFF") or []) if v is not None), None)
    ecb = next((v for _, v in reversed(policy.get("ECBDFR") or []) if v is not None), None)

    rates = []
    if curve_v is not None:
        rates.append(f"<li>10y–2y spread {curve_v:.2f}%</li>")
    if fed is not None:
        rates.append(f"<li>Fed funds {fed:.2f}%</li>")
    if ecb is not None:
        rates.append(f"<li>ECB deposit {ecb:.2f}%</li>")
    if be_v is not None:
        rates.append(f"<li>10y breakeven {be_v:.2f}%</li>")
    if real_v is not None:
        rates.append(f"<li>10y real yield {real_v:.2f}%</li>")
    rates_html = "<ul>" + "".join(rates) + "</ul>" if rates else "<p>Rates not loaded.</p>"

    cards = brief.get("cards") or []
    read = "".join(
        f"<li><b>{esc(c.get('label'))}</b> — {esc(c.get('value'))}. {esc(c.get('text'))}</li>"
        for c in cards)
    posture = esc(brief.get("posture") or "Awaiting data")
    headline = esc(brief.get("headline") or "Not enough series loaded to summarize.")

    missing = sum(1 for q in (sections.get("indices") or []) if not q)
    cov = (f"{len(indices)} index cards, {len(commodities)} commodities, {len(fx)} FX crosses, "
           f"{len(cpi)} CPI rows, {len(gdp)} GDP rows"
           + (f", {missing} index card(s) still empty" if missing else ""))

    body = f"""<header>
  <h1>ECON<span>.</span>world</h1>
  <span class="updated">updated {esc(now.strftime("%Y-%m-%d %H:%M %Z"))}</span>
  {banner}
</header>
{tabs("summary")}
<div class="sum">
  <section>
    <h3>Posture</h3>
    <p class="posture-name tone-{esc(brief.get("tone") or "muted")}">{posture}</p>
    <p>{headline}</p>
    <p class="meta">{esc(brief.get("disclaimer") or "Read of this board. Not a forecast and not investment advice.")}</p>
  </section>
  <section>
    <h3>What the board is saying</h3>
    <ul>{read or "<li>Briefing not ready.</li>"}</ul>
  </section>
  <section>
    <h3>Inflation meter</h3>
    {inflation_gauge(be_v if be_v is not None else (cpi[0]["value"] if cpi else None), "10y breakeven" if be_v is not None else "CPI")}
    <p class="meta">Green under 2%, amber to 3%, red above. Needle is the 10y breakeven when loaded. Not a forecast.</p>
  </section>
  <section>
    <h3>Recession meter</h3>
    {recession_gauge(curve_v)}
    <p class="meta">Red is an inverted 10y-2y spread, amber is flat, green is a normal slope. Same series as the curve chart. Not a forecast.</p>
  </section>
  <section>
    <h3>Rates and inflation</h3>
    {rates_html}
  </section>
  <section>
    <h3>Indices</h3>
    {bullets(indices)}
  </section>
  <section>
    <h3>Commodities</h3>
    {bullets(commodities)}
  </section>
  <section>
    <h3>Currencies, USD base</h3>
    {bullets(fx)}
  </section>
  <section>
    <h3>Inflation, CPI % YoY</h3>
    {macro_lines(cpi)}
  </section>
  <section>
    <h3>Real GDP growth, % YoY</h3>
    {macro_lines(gdp)}
  </section>
  <section>
    <h3>Coverage</h3>
    <p>{esc(cov)}. Same cache as the board. Empty cards are missing sources, not a failed page.</p>
  </section>
</div>
<footer>Summary of data already gathered. Not a forecast and not investment advice.</footer>
"""
    return (PAGE.replace("__STYLE__", STYLE)
                .replace("__REFRESH__", str(max(60, int(refresh))))
                .replace("__BODY__", body))


def build_stocks_page(screen, *, demo=False, generated_at=None, refresh=300):
    """Render the top-three, US stock watchlist and its transparent risk rules."""
    now = generated_at or datetime.now(timezone.utc)
    screen = screen or {}
    banner = ('<span class="badge demo">DEMO MODE — synthetic stock history; not live</span>'
              if demo else "")
    status = str(screen.get("status") or "waiting")
    state_class = "ok" if status == "ok" else (
        "bad" if status in ("unavailable", "stale") else "warn")
    message = esc(screen.get("message") or "Waiting for stock data.")
    if screen.get("refresh_error"):
        state_class = "warn"
        message += " Last refresh failed; showing the last successful screen."
    quote_count = int(screen.get("usable_history_count", screen.get("quote_count")) or 0)
    history_count = int(screen.get("history_count") or quote_count)
    universe_size = int(screen.get("universe_size") or 0)
    missing_history = max(0, universe_size - quote_count)
    min_sessions = int((screen.get("filters") or {}).get("minimum_sessions", 61))
    coverage = (f"{quote_count}/{universe_size} tickers with usable history "
                f"(at least {min_sessions} sessions)"
                if universe_size else "No ticker universe loaded")
    if missing_history:
        coverage += f" · {missing_history} missing/insufficient"
    insufficient_sessions = max(0, history_count - quote_count)
    if insufficient_sessions:
        coverage += f" · {insufficient_sessions} below {min_sessions} sessions"
    if status == "ok" and (missing_history or screen.get("provider_issues")):
        state_class = "warn"
    data_date = esc(screen.get("data_date") or "—")
    data_source = esc(screen.get("source") or "not yet available")
    generated_screen = _dual_market_time(screen.get("generated_at"))
    display_cap = screen.get("max_candidates")
    try:
        display_cap = max(0, int(display_cap))
    except (TypeError, ValueError):
        display_cap = 7
    candidates = list(screen.get("candidates") or [])[:display_cap]
    currency_reference = screen.get("currency_reference") or {}
    if demo:
        currency_reference = {
            "available": False,
            "reason": "MYR conversion is disabled for synthetic demo data.",
        }

    # How often this screen actually re-checks its providers, so an unchanged
    # board is explainable: the feed is daily, not intraday.
    cadence_bits = []
    try:
        interval_min = round(int(screen.get("refresh_interval_seconds")) / 60)
    except (TypeError, ValueError):
        interval_min = 0
    if interval_min > 0:
        cadence_bits.append(f"providers re-checked at most every {interval_min} min")
    next_check = screen.get("next_check_in_seconds")
    if isinstance(next_check, (int, float)) and next_check >= 0:
        cadence_bits.append(
            "next re-check due now" if next_check < 60
            else f"next re-check in about {round(next_check / 60)} min")
    if cadence_bits:
        cadence_bits.append(
            "the underlying daily bars only change after the U.S. close, so this "
            "list is not intraday")
    cadence_html = (f'<div class="stock-currency-note">Refresh cadence: '
                    f'{esc(" · ".join(cadence_bits))}.</div>') if cadence_bits else ""

    def pct(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return '<span class="stock-pct flat">—</span>'
        cls = "up" if number > 0 else ("down" if number < 0 else "flat")
        return f'<span class="stock-pct {cls}">{number:+.1f}%</span>'

    if currency_reference.get("available"):
        try:
            fx_rate_text = f"{float(currency_reference.get('rate')):,.4f}"
        except (TypeError, ValueError):
            fx_rate_text = "—"
        rate_date = esc(currency_reference.get("rate_date") or "unknown date")
        fetched_myt = _myt_timestamp(currency_reference.get("fetched_at_utc"))
        currency_note = (
            '<p class="stock-currency-note">MYR figures are indicative share-price conversions only: '
            f"USD/MYR {fx_rate_text}, {esc(currency_reference.get('source') or 'unknown source')}, "
            f"reference date {rate_date}; fetched {fetched_myt}. "
            "Not an executable or all-in price. Broker, regulatory, stamp-duty, spread and FX "
            f"conversion costs are excluded. See <a href=\"https://www.moomoo.com/my/support/topic9_136\" "
            'target="_blank" rel="noopener noreferrer">Moomoo Malaysia’s current U.S. fee schedule</a>.</p>'
        )
    else:
        currency_reason = esc(currency_reference.get("reason") or "No fresh USD/MYR reference is available.")
        currency_note = (
            f'<p class="stock-currency-note">MYR estimate omitted: {currency_reason} '
            "Any conversion would be indicative only, not an executable or all-in price; broker, "
            "regulatory, stamp-duty, spread and FX conversion costs are excluded. See "
            '<a href="https://www.moomoo.com/my/support/topic9_136" target="_blank" '
            'rel="noopener noreferrer">Moomoo Malaysia’s current U.S. fee schedule</a>.</p>'
        )

    news = screen.get("news") if isinstance(screen.get("news"), dict) else {}
    news_items = news.get("items") if isinstance(news.get("items"), dict) else {}
    news_errors = news.get("errors") if isinstance(news.get("errors"), dict) else {}
    news_days = 7
    try:
        news_days = max(1, int(news.get("window_days") or 7))
    except (TypeError, ValueError):
        pass
    news_active = bool(news) and news.get("status") != "disabled"
    news_note = ""
    if news:
        if news.get("status") == "disabled":
            news_note = (f'<p class="stock-currency-note">'
                         f'{esc(news.get("message") or "Headlines are disabled.")}</p>')
        elif news.get("status") == "unavailable":
            news_note = (
                '<p class="stock-currency-note">Headlines unavailable: '
                f'{esc(news.get("message") or "no feed answered")} '
                'Quotes and the stock screen are unaffected.</p>')
        else:
            coverage = news.get("coverage") or {}
            providers = ", ".join(str(p) for p in coverage.get("providers") or [])
            fetched = _myt_timestamp(news.get("fetched_at_utc"))
            news_note = (
                f'<p class="stock-currency-note">Headlines: '
                f'{esc(news.get("message") or "")} '
                f'Sources: {esc(providers or "none")} · fetched {fetched}. '
                'Third-party RSS, not verified, and never used in the ranking.</p>')

    cards = []
    for rank, item in enumerate(candidates, 1):
        risk = str(item.get("risk") or "Unknown")
        risk_class = risk.lower() if risk.lower() in ("low", "medium", "high") else "medium"
        adv = item.get("avg_dollar_volume20")
        try:
            adv_text = f"${float(adv) / 1_000_000:.1f}M"
        except (TypeError, ValueError):
            adv_text = "—"
        try:
            price_text = f"${float(item.get('close')):,.2f}"
        except (TypeError, ValueError):
            price_text = "—"
        myr_price_html = ""
        if currency_reference.get("available"):
            try:
                myr_price = float(item.get("close_myr_indicative"))
                if math.isfinite(myr_price) and myr_price > 0:
                    myr_price_html = (
                        f'<div class="stock-myr">≈ <b>RM {myr_price:,.2f}</b> '
                        "<small>indicative</small></div>"
                    )
            except (TypeError, ValueError):
                pass
        try:
            score_text = f"{float(item.get('rank_score')):+.2f}"
        except (TypeError, ValueError):
            score_text = "—"
        quote_age = max(0, int(item.get("quote_age_days") or 0))
        quote_age_unit = "day" if quote_age == 1 else "days"
        reasons = "".join(f"<li>{esc(reason)}</li>" for reason in item.get("reasons") or [])
        symbol = str(item.get("symbol") or "").upper()
        bars = [row for row in item.get("recent_bars") or []
                if isinstance(row, (list, tuple)) and len(row) >= 2]
        spark_values = [row[1] for row in bars
                        if isinstance(row[1], (int, float)) and math.isfinite(row[1])]
        ret7 = item.get("ret7_pct")
        try:
            ret7_num = float(ret7)
        except (TypeError, ValueError):
            ret7_num = None
        spark_up = None if ret7_num in (None, 0) else ret7_num > 0
        spark_html = (
            f'<div class="stock-spark">{sparkline(spark_values, width=280, height=44, up=spark_up)}</div>'
            if len(spark_values) >= 2 else "")
        week_range = ""
        try:
            low, high = float(item.get("week_low")), float(item.get("week_high"))
            if math.isfinite(low) and math.isfinite(high):
                week_range = f"7-day range ${low:,.2f}–${high:,.2f} · "
        except (TypeError, ValueError):
            week_range = ""
        hits = news_items.get(symbol) or []
        if hits:
            rows_html = []
            for hit in hits[:3]:
                title = esc(hit.get("title") or "")
                link = _safe_link(hit.get("link"))
                headline = (f'<a href="{esc(link)}" target="_blank" '
                            f'rel="noopener noreferrer">{title}</a>' if link else title)
                meta = esc(hit.get("publisher") or "unknown publisher")
                age = _news_age(hit.get("published_utc"), now)
                if age:
                    meta += f" · {age}"
                rows_html.append(f"<li>{headline}<small>{meta}</small></li>")
            news_html = ('<div class="stock-news"><div class="news-head">Recent headlines</div>'
                         f'<ul>{"".join(rows_html)}</ul></div>')
        elif news_active:
            detail = str(news_errors.get(symbol) or "")[:180]
            suffix = f" Feed detail: {esc(detail)}" if detail else ""
            news_html = ('<div class="stock-news"><div class="news-head">Recent headlines</div>'
                         f'<div class="news-empty">No headline inside the last {news_days} '
                         f'days matched this ticker.{suffix}</div></div>')
        else:
            news_html = ""
        cards.append(f"""<article class="stock-card">
  <div class="stock-head">
    <div><div class="stock-rank">Rank #{rank}</div>
      <div class="stock-symbol">{esc(item.get("symbol") or "?")}</div>
      <div class="stock-name">{esc(item.get("name") or "")}</div>
    </div>
    <span class="risk-badge risk-{risk_class}">{esc(risk)} risk</span>
  </div>
  <div class="stock-head"><div><div class="stock-price">{price_text}</div>{myr_price_html}</div>
    <div class="stock-name">{esc(item.get("sector") or "")}</div>
  </div>
  {spark_html}
  <div class="stock-stats">
    <div class="stock-stat"><small>1 day</small><b>{pct(item.get("ret1_pct"))}</b></div>
    <div class="stock-stat"><small>7 days</small><b>{pct(item.get("ret7_pct"))}</b></div>
    <div class="stock-stat"><small>20 sessions</small><b>{pct(item.get("ret20_pct"))}</b></div>
    <div class="stock-stat"><small>60 sessions</small><b>{pct(item.get("ret60_pct"))}</b></div>
    <div class="stock-stat"><small>20d volatility</small><b>{fmt_num(item.get("vol20_annual_pct"), 1)}%</b></div>
    <div class="stock-stat"><small>60d drawdown</small><b>{fmt_num(item.get("drawdown60_pct"), 1)}%</b></div>
    <div class="stock-stat"><small>20d $ volume</small><b>{adv_text}</b></div>
  </div>
  <ul class="stock-reasons">{reasons}</ul>
  {news_html}
  <div class="stock-meta">{week_range}relative rank score {score_text} · U.S. daily-bar date {esc(item.get("as_of") or "?")} (Eastern Time) ·
    {esc(item.get("source") or "unknown source")} · quote age {quote_age} calendar {quote_age_unit}</div>
</article>""")

    cards_html = ("<div class='stock-grid'>" + "".join(cards) + "</div>") if cards else (
        '<div class="section-empty">No stock picks are being shown. '
        'This can mean the feeds are warming, quotes are stale, or no ticker passed the filters.</div>')
    week_html = _stock_week_html(screen.get("week"))
    fetch_error = screen.get("fetch_error") or screen.get("refresh_error")
    error_label = "Refresh issue / using last-good data" if screen.get("refresh_error") else "Data-source detail"
    error_html = (f'<p class="note">{error_label}: {esc(fetch_error)}</p>'
                  if fetch_error else "")

    diagnostics = []
    if screen.get("refresh_error"):
        diagnostics.append(f"Last refresh failed: {str(screen.get('refresh_error'))[:240]}")
    elif status in ("unavailable", "stale") and screen.get("fetch_error"):
        diagnostics.append(f"History fetch detail: {str(screen.get('fetch_error'))[:240]}")
    history_errors = screen.get("history_errors") or {}
    if isinstance(history_errors, dict):
        history_error_count = int(history_errors.get("count") or 0)
        samples = history_errors.get("samples") or {}
        if history_error_count:
            sample_text = "; ".join(
                f"{symbol}: {detail}" for symbol, detail in list(samples.items())[:4]
            ) if isinstance(samples, dict) else ""
            suffix = f" Sample: {sample_text}" if sample_text else ""
            omitted = max(0, history_error_count - len(samples)) if isinstance(samples, dict) else history_error_count
            if omitted:
                suffix += f" (+{omitted} more not shown)"
            diagnostics.append(
                f"{history_error_count} ticker(s) lacked usable history after the provider cascade.{suffix}"
            )
    provider_issues = list(screen.get("provider_issues") or [])
    for issue in provider_issues[:4]:
        name = str(issue.get("provider") or "stock data provider")
        state = "cooling down" if issue.get("state") == "cooling" else "degraded"
        retry = int(issue.get("retry_in") or 0)
        detail = (str(issue.get("reason") or "").split(" | ")[0])[:120]
        retry_text = f"; retry in about {max(1, round(retry / 60))} min" if retry > 0 else ""
        reason_text = f" — {detail}" if detail else ""
        diagnostics.append(f"{name}: {state}{retry_text}{reason_text}")
    diagnostics_html = (
        '<div class="stock-diagnostics"><b>Data notes:</b> '
        + "<br>".join(esc(line) for line in diagnostics)
        + "</div>"
    ) if diagnostics else ""
    exclusions = screen.get("exclusions") or {}
    excluded_html = " · ".join(
        f"{esc(key.replace('_', ' '))}: {int(value)}"
        for key, value in exclusions.items() if value
    ) or "none"

    body = f"""<header>
  <h1>ECON<span>.</span>world</h1>
  <span class="updated">Page time · {_dual_market_time(now)}</span>
  {banner}
</header>
{tabs("stocks")}
<section class="stock-intro">
  <h2>Short-term stock watchlist <span class="src">· U.S.-listed · every passer, up to {display_cap} names</span></h2>
  <p>Daily closing-price screen of a configured ticker universe. It looks for recent positive momentum and liquidity, then labels recent realized volatility/drawdown risk. Every ticker that clears all filters is listed, ranked by relative score. This is not a forecast, trade signal, or order recommendation.</p>
  <p>Each card adds the last seven calendar days of price movement and recent third-party headlines. Both are context for your own research: neither is scored, and neither changes which tickers appear.</p>
  <p class="stock-verification">This editable universe is not checked against Moomoo Malaysia’s live symbol availability or order eligibility. Verify each ticker in the app before relying on it; fractional-share eligibility is limited to selected U.S. securities and the list can change. See <a href="https://www.moomoo.com/my/support/topic9_193" target="_blank" rel="noopener noreferrer">Moomoo Malaysia’s fractional-share guide</a>.</p>
</section>
<div class="stock-status {state_class}"><b>{message}</b><br>
  {esc(coverage)} · latest daily-bar date {data_date} (U.S. market session date, Eastern Time) · source(s): {data_source} ·
  screen generated {generated_screen}
  {diagnostics_html}
</div>
{cadence_html}
{news_note}
{currency_note}
{week_html}
{cards_html}
<details class="stock-method">
  <summary>How the screen and risk labels work</summary>
  <ul>
    <li>Price filter: ${fmt_num((screen.get("filters") or {}).get("price_min"), 2)} to under ${fmt_num((screen.get("filters") or {}).get("price_max_exclusive"), 2)}. The $5 floor excludes penny stocks; a low share price does not mean a company is cheap.</li>
    <li>History: at least {int((screen.get("filters") or {}).get("minimum_sessions", 61))} daily sessions are required for usable 60-session analysis.</li>
    <li>Freshness: the latest daily close must be no more than {int((screen.get("filters") or {}).get("max_quote_age_days", 5))} calendar days old.</li>
    <li>Liquidity: at least ${fmt_num((screen.get("filters") or {}).get("min_avg_dollar_volume", 5_000_000) / 1_000_000, 1)}M average daily dollar volume over the last 20 sessions.</li>
    <li>Trend screen: positive 20-session return and latest close above its 20-day average. {esc(screen.get("method") or "The relative score is not a probability or expected return.")}</li>
    <li>Risk buckets: {esc(screen.get("risk_method") or "Not available.")}</li>
    <li>Display: every passing ticker is shown, ranked highest score first, up to {display_cap} names. Ranking is relative within this universe, so a longer list is a wider watchlist, not a stronger signal.</li>
    <li>7-day movement: the change from the last close at or before seven calendar days before the latest bar (about five U.S. trading sessions) to the latest close. The strip above the cards averages that window across the displayed names; it is a description of past closes, not a forecast.</li>
    <li>Headlines: one key-less RSS search per displayed ticker (Google News → Bing News → Yahoo Finance headline feed), newest first, keeping only unduplicated items published inside the last {news_days} days whose title mentions the ticker or company. Headlines are third-party context, are not verified, and never change which tickers pass or how they rank. A ticker with no matching article says so.</li>
    <li>Excluded counts: {excluded_html}. Universe size: {universe_size} tickers.</li>
  </ul>
  <p>{esc(screen.get("limitations") or "Technical data only; verify the quote and research the company before investing.")}</p>
  {error_html}
</details>
<footer>Data uses daily closes, not intraday prices. Headlines are third-party RSS links that may be delayed, syndicated or inaccurate; they are not verified and are not part of the screen. Short-term stock investing can lose money quickly. Check current price, bid/ask spread, upcoming earnings/news, company filings, and your broker’s execution details before acting. The screen does not personalize risk or guarantee performance.</footer>
"""
    return (PAGE.replace("__STYLE__", STYLE)
                .replace("__REFRESH__", str(max(60, int(refresh))))
                .replace("__BODY__", body))


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
</header>
{tabs("board")}"""]

    brief = sections.get("briefing")
    if brief is None:
        try:
            brief = briefing.build_briefing(sections)
        except Exception:
            brief = None
    if brief:
        body.append(briefing_html(brief))

    # --- Markets ------------------------------------------------------------
    body.append('<h2>World indices <span class="src">· daily close (Yahoo / FRED / CNBC / Tencent / Sina / Stooq)</span></h2>')
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

    # --- Real yield (gold) -----------------------------------------------------
    rmeta = sections.get("real_yield_meta") or ("DFII10", "US 10Y Real Yield")
    rsid, rtitle = rmeta
    rhist = sections.get("real_yield") or []
    body.append(f'<h2>Real yield <span class="src">· {esc(rsid)} · gold macro driver</span></h2>')
    if rhist:
        rvals = [v for _, v in rhist]
        latest = next((v for v in reversed(rvals) if v is not None), None)
        if latest is None:
            note = "no print"
        elif latest >= 1.5:
            note = "High real yield — historical headwind for gold"
        elif latest >= 0.5:
            note = "Positive real yield — gold needs a weaker dollar or falling yields to trend"
        elif latest >= 0:
            note = "Low positive real yield — less of a headwind"
        else:
            note = "Negative real yield — the classic gold tailwind"
        body.append(f'<div class="big-chart">{line_chart(rvals, color="#e5a50a")}'
                    f'<div class="note">{esc(rtitle)} · latest <b>{fmt_num(latest)}%</b> — '
                    f'{esc(note)}. Not a forecast.</div></div>')
    else:
        body.append('<div class="section-empty">Awaiting real-yield data…</div>')

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
