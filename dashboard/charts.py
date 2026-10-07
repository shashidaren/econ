"""Tiny server-side SVG chart helpers — no JS, no CDN, works offline on LAN."""


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
