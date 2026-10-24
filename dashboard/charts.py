"""Tiny server-side SVG chart helpers — no JS, no CDN, works offline on LAN."""

import html


def sparkline(values, width=150, height=38, color="#4a9eff", up=None):
    """Compact filled sparkline. `up` (True/False) overrides the line colour."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return f'<svg class="spark" width="{width}" height="{height}"></svg>'
    if up is True:
        color = "#26a69a"
    elif up is False:
        color = "#ef5350"
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 3.0
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * (width - 2 * pad) / (len(vals) - 1)
        y = pad + (height - 2 * pad) * (1.0 - (v - lo) / span)
        pts.append((round(x, 1), round(y, 1)))
    pline = " ".join(f"{x},{y}" for x, y in pts)
    area = f"M {pts[0][0]},{height - 1} L " + " L ".join(f"{x},{y}" for x, y in pts) + \
           f" L {pts[-1][0]},{height - 1} Z"
    last_x, last_y = pts[-1]
    return (
        f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img">'
        f'<path d="{area}" fill="{color}" opacity="0.12"/>'
        f'<polyline points="{pline}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.2" fill="{color}"/></svg>'
    )


def line_chart(values, width=760, height=240, color="#4a9eff",
               zero_line=True, unit="", fmt=None):
    """Larger chart with optional dashed zero line and min/max labels."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return '<div class="chart-empty">not enough data</div>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    # Headroom so the line doesn't kiss the edges
    pad_y = span * 0.08
    lo_p, hi_p = lo - pad_y, hi + pad_y
    span_p = (hi_p - lo_p) or 1.0

    pad_l, pad_r, pad_t, pad_b = 6, 64, 10, 10
    pts = []
    n = len(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        x = pad_l + i * (width - pad_l - pad_r) / (n - 1)
        y = pad_t + (height - pad_t - pad_b) * (1.0 - (v - lo_p) / span_p)
        pts.append((round(x, 1), round(y, 1)))
    pline = " ".join(f"{x},{y}" for x, y in pts)
    last_x, last_y = pts[-1]
    last_v = vals[-1]

    def _lbl(v):
        if fmt:
            return fmt(v)
        return f"{v:,.2f}{unit}"

    parts = [f'<svg class="chart" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img">']
    if zero_line and lo_p < 0 < hi_p:
        zy = pad_t + (height - pad_t - pad_b) * (1.0 - (0 - lo_p) / span_p)
        parts.append(f'<line x1="{pad_l}" y1="{zy:.1f}" x2="{width - pad_r}" y2="{zy:.1f}" '
                     f'stroke="#8b93a7" stroke-width="1" stroke-dasharray="4 4" opacity="0.6"/>')
        parts.append(f'<text x="{width - pad_r + 4}" y="{zy + 3:.1f}" fill="#8b93a7" '
                     f'font-size="11">0</text>')
    parts.append(f'<path d="M {pline.replace(" ", " L ")}" fill="none" stroke="{color}" '
                 f'stroke-width="2" stroke-linejoin="round"/>')
    parts.append(f'<circle cx="{last_x}" cy="{last_y}" r="3.2" fill="{color}"/>')
    parts.append(f'<text x="{last_x + 6:.1f}" y="{last_y + 4:.1f}" fill="#e6e9f0" '
                 f'font-size="12" font-weight="600">{_lbl(last_v)}</text>')
    parts.append(f'<text x="{width - pad_r + 4}" y="{pad_t + 8}" fill="#8b93a7" '
                 f'font-size="11">{_lbl(hi)}</text>')
    parts.append(f'<text x="{width - pad_r + 4}" y="{height - pad_b}" fill="#8b93a7" '
                 f'font-size="11">{_lbl(lo)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _segments(points):
    """Split [(x, y)] into runs of consecutive x values (gaps break the line)."""
    runs, current, prev_x = [], [], None
    for x, y in points:
        if y is None:
            if current:
                runs.append(current)
                current = []
            prev_x = None
            continue
        if prev_x is not None and x - prev_x > 1.5:
            if current:
                runs.append(current)
            current = []
        current.append((x, y))
        prev_x = x
    if current:
        runs.append(current)
    return runs


def dual_line_chart(pairs, *, labels=("Series A", "Series B"), width=600, height=250,
                    colors=("#4a9eff", "#e5a50a"), unit="%", zero_line=True,
                    fmt=None):
    """Two year-indexed series on one shared axis, with a compact legend.

    `pairs` is ``[(series_a, series_b), ...]`` where each series is a list of
    ``(year, value)`` pairs; missing years are gaps, never interpolated. Years
    are used directly on the x-axis so two countries with different reporting
    years still line up honestly.
    """
    clean = []
    for series in pairs or []:
        points = []
        for point in series or []:
            try:
                x, y = int(point[0]), point[1]
            except (TypeError, ValueError, IndexError):
                continue
            if y is None:
                continue
            try:
                y = float(y)
            except (TypeError, ValueError):
                continue
            if y != y or y in (float("inf"), float("-inf")):
                continue
            points.append((x, y))
        points.sort()
        clean.append(points)

    all_years = sorted({x for points in clean for x, _ in points})
    all_values = [y for points in clean for _, y in points]
    if len(all_years) < 2 or not all_values:
        return '<div class="chart-empty">not enough data to chart</div>'

    x_lo, x_hi = all_years[0], all_years[-1]
    y_lo, y_hi = min(all_values), max(all_values)
    if y_lo == y_hi:
        y_lo, y_hi = y_lo - 1.0, y_hi + 1.0
    pad = (y_hi - y_lo) * 0.12
    y_lo_p, y_hi_p = y_lo - pad, y_hi + pad
    if zero_line:
        # Keep zero on the chart when it is close to the data range, so a fall
        # below zero reads as "this crossed zero" rather than a cosmetic dip.
        if y_lo < 0 < y_hi:
            y_lo_p, y_hi_p = min(y_lo_p, 0.0), max(y_hi_p, 0.0)
        elif 0 < y_lo and y_lo_p < y_lo * 0.5:
            y_lo_p = min(y_lo_p, 0.0)

    pad_l, pad_r, pad_t, pad_b = 6, 54, 34, 22
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def sx(x):
        if x_hi == x_lo:
            return pad_l + plot_w / 2.0
        return pad_l + (x - x_lo) / (x_hi - x_lo) * plot_w

    def sy(y):
        return pad_t + plot_h * (1.0 - (y - y_lo_p) / (y_hi_p - y_lo_p))

    def _lbl(value):
        if fmt:
            return fmt(value)
        return f"{value:,.1f}{unit}" if unit else f"{value:,.1f}"

    parts = [f'<svg class="chart" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img">']

    # Legend (colour swatch + name), plus the last point of each series.
    legend_x = pad_l + 2
    for index, points in enumerate(clean):
        color = colors[index % len(colors)]
        name = labels[index] if index < len(labels) else f"Series {index + 1}"
        if not points:
            continue
        last_x, last_y = points[-1]
        parts.append(
            f'<rect x="{legend_x}" y="12" width="10" height="3" rx="1.5" fill="{color}"/>'
            f'<text x="{legend_x + 15}" y="17" fill="#e6e9f0" font-size="12" '
            f'font-weight="600">{_esc(name)}</text>'
            f'<text x="{sx(last_x) + 6:.1f}" y="{sy(last_y) + 4:.1f}" fill="{color}" '
            f'font-size="11.5" font-weight="700">{_lbl(last_y)}</text>')
        legend_x += 15 + 7.2 * len(name) + 62

    if zero_line and y_lo_p < 0 < y_hi_p:
        zy = sy(0.0)
        parts.append(f'<line x1="{pad_l}" y1="{zy:.1f}" x2="{width - pad_r}" y2="{zy:.1f}" '
                     f'stroke="#8b93a7" stroke-width="1" stroke-dasharray="4 4" '
                     f'opacity="0.55"/>')
        parts.append(f'<text x="{width - pad_r + 4}" y="{zy + 3:.1f}" '
                     f'fill="#8b93a7" font-size="10.5">0</text>')

    for index, points in enumerate(clean):
        color = colors[index % len(colors)]
        for run in _segments(points):
            if len(run) == 1:
                x, y = run[0]
                parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.4" '
                             f'fill="{color}"/>')
                continue
            path = " ".join(
                f"{'M' if i == 0 else 'L'} {sx(x):.1f},{sy(y):.1f}"
                for i, (x, y) in enumerate(run))
            parts.append(f'<path d="{path}" fill="none" stroke="{color}" '
                         f'stroke-width="2.2" stroke-linejoin="round" '
                         f'stroke-linecap="round"/>')

    # Year labels: first and last only — a wall-board, not an axis dump.
    parts.append(f'<text x="{sx(x_lo):.1f}" y="{height - 6}" fill="#8b93a7" '
                 f'font-size="10.5">{x_lo}</text>')
    parts.append(f'<text x="{sx(x_hi):.1f}" y="{height - 6}" fill="#8b93a7" '
                 f'font-size="10.5" text-anchor="end">{x_hi}</text>')
    parts.append("</svg>")
    return "".join(parts)

