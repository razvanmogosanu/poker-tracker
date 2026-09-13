"""Inline-SVG chart rendering.

No plotting library and no CDN: every chart is a string of SVG that references
CSS custom properties for colour, so the whole report is one self-contained file
that follows the reader's light/dark theme.

Colour assignments were checked with the data-viz palette validator. The
winnings chart's green/red pair sits in the colour-vision-deficiency warn band,
so each of its lines also carries a distinct stroke pattern and a direct label:
identity is never carried by colour alone.
"""

from __future__ import annotations

import html
import json
import math

from .cards import grid_cell

W, H = 900, 400
PAD = {"t": 24, "r": 96, "b": 44, "l": 62}


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def nice_ticks(lo: float, hi: float, count: int = 5):
    """Round tick values covering [lo, hi], plus the expanded domain."""
    if not math.isfinite(lo) or not math.isfinite(hi):
        lo, hi = 0.0, 1.0
    if hi - lo < 1e-12:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / max(1, count)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next((m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag), 10 * mag)
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    ticks, v = [], start
    while v <= end + step * 0.5:
        ticks.append(round(v, 10))
        v += step
    return ticks, start, end


def fmt(v, places: int = 0) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.{places}f}"


class Frame:
    """Maps data coordinates onto the SVG box and draws the shared chrome."""

    def __init__(self, x0, x1, y0, y1, w=W, h=H, pad=None):
        self.w, self.h = w, h
        self.pad = dict(PAD, **(pad or {}))
        self.x0, self.x1, self.y0, self.y1 = x0, x1, y0, y1

    @property
    def left(self):
        return self.pad["l"]

    @property
    def right(self):
        return self.w - self.pad["r"]

    @property
    def top(self):
        return self.pad["t"]

    @property
    def bottom(self):
        return self.h - self.pad["b"]

    def sx(self, x):
        if self.x1 == self.x0:
            return self.left
        return self.left + (x - self.x0) / (self.x1 - self.x0) * (self.right - self.left)

    def sy(self, y):
        if self.y1 == self.y0:
            return self.bottom
        return self.bottom - (y - self.y0) / (self.y1 - self.y0) * (self.bottom - self.top)

    def grid(self, yticks, xticks=None, xfmt=fmt, yfmt=fmt, ylabel="", xlabel=""):
        out = []
        for t in yticks:
            y = self.sy(t)
            zero = " zero" if abs(t) < 1e-9 else ""
            out.append(
                f'<line class="grid{zero}" x1="{self.left}" y1="{y:.1f}" '
                f'x2="{self.right}" y2="{y:.1f}"/>'
            )
            out.append(
                f'<text class="tick" x="{self.left - 8}" y="{y + 4:.1f}" '
                f'text-anchor="end">{esc(yfmt(t))}</text>'
            )
        for t in (xticks or []):
            out.append(
                f'<text class="tick" x="{self.sx(t):.1f}" y="{self.bottom + 18}" '
                f'text-anchor="middle">{esc(xfmt(t))}</text>'
            )
        out.append(
            f'<line class="axis" x1="{self.left}" y1="{self.bottom}" '
            f'x2="{self.right}" y2="{self.bottom}"/>'
        )
        if ylabel:
            mid = (self.top + self.bottom) / 2
            out.append(
                f'<text class="axis-label" transform="translate(14,{mid:.0f}) '
                f'rotate(-90)" text-anchor="middle">{esc(ylabel)}</text>'
            )
        if xlabel:
            xmid = (self.left + self.right) / 2
            out.append(
                f'<text class="axis-label" x="{xmid:.0f}" y="{self.h - 6}" '
                f'text-anchor="middle">{esc(xlabel)}</text>'
            )
        return "".join(out)


# A direct label's own line height, plus a hair. Labels closer than this
# overlap into an unreadable smudge.
LABEL_GAP = 13.0


def stack_labels(targets: list[float], lo: float, hi: float,
                 gap: float = LABEL_GAP) -> list[float]:
    """Nudge direct labels apart, keeping each as near its line as it can be.

    Direct labels sit at the end of the line they name, which is the whole
    point of them -- identity never rests on colour. But two series that end
    at the same value put two labels in the same place, and on the real
    history "Total" and "All-in adj. EV" do exactly that whenever luck is
    near zero, which is the normal case rather than an odd one.

    Order is preserved, so a label never crosses the line above or below it
    and cannot end up naming the wrong one. Returned in the caller's order.
    """
    order = sorted(range(len(targets)), key=lambda i: targets[i])
    ys = [targets[i] for i in order]
    for j in range(1, len(ys)):
        ys[j] = max(ys[j], ys[j - 1] + gap)
    # Pushing down can run off the bottom; if it did, take the whole stack
    # back up and resolve the collisions in the other direction.
    if ys and ys[-1] > hi:
        ys = [y - (ys[-1] - hi) for y in ys]
        for j in range(len(ys) - 2, -1, -1):
            ys[j] = min(ys[j], ys[j + 1] - gap)
        if ys[0] < lo:
            ys = [y + (lo - ys[0]) for y in ys]
    out = [0.0] * len(targets)
    for pos, i in enumerate(order):
        out[i] = ys[pos]
    return out


def svg(body: str, w=W, h=H, cls="chart") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="xMidYMid meet" role="img">{body}</svg>')


# ---------------------------------------------------------------------------
# 1. cumulative winnings


def cumulative_winnings(rows, has_ev: bool) -> str:
    """The chart trackers exist for: total, showdown, non-showdown and EV."""
    if not rows:
        return '<p class="empty">No hands yet.</p>'

    n = len(rows)
    total, sd, nsd, ev = [], [], [], []
    t = s = ns = e = 0.0
    for r in rows:
        t += r["net_bb"]
        e += r["ev_bb"]
        if r["sd"]:
            s += r["net_bb"]
        else:
            ns += r["net_bb"]
        total.append(t)
        sd.append(s)
        nsd.append(ns)
        ev.append(e)

    series = [
        ("Total", total, "total", ""),
        ("Showdown", sd, "showdown", "10 4"),
        ("Non-showdown", nsd, "nonshowdown", "3 3"),
    ]
    if has_ev:
        series.append(("All-in adj. EV", ev, "ev", "6 3"))

    allv = [v for _, vals, _, _ in series for v in vals] + [0.0]
    yticks, ylo, yhi = nice_ticks(min(allv), max(allv), 6)
    xticks, _, xhi = nice_ticks(0, n, 6)
    f = Frame(0, max(xhi, 1), ylo, yhi)

    out = [f.grid(yticks, xticks, xfmt=lambda v: fmt(v, 0),
                  ylabel="Cumulative bb", xlabel="Hands played")]
    # The period filter shades this band instead of redrawing the chart over a
    # shorter axis. A single session plotted on its own is unreadable noise;
    # the reason to look at this chart at all is where the selected hands sit
    # relative to everything that came before them.
    out.append(f'<rect class="period-band" x="{f.left}" y="{f.top}" width="0" '
               f'height="{f.bottom - f.top:.1f}"/>')
    # Direct labels at the end of each line, so identity never rests on colour,
    # spread apart where two lines finish on top of each other.
    label_ys = stack_labels([f.sy(vals[-1]) + 4 for _, vals, _, _ in series],
                            f.top, f.bottom)
    for (name, vals, key, dash), label_y in zip(series, label_ys):
        pts = " ".join(f"{f.sx(i + 1):.1f},{f.sy(v):.1f}" for i, v in enumerate(vals))
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<polyline class="line {key}" points="{pts}"{dash_attr}/>')
        out.append(
            f'<text class="direct-label {key}" x="{f.right + 6}" '
            f'y="{label_y:.1f}">{esc(name)}</text>'
        )

    payload = json.dumps({
        "left": f.left, "right": f.right, "top": f.top, "bottom": f.bottom,
        # The x axis is padded out to a round tick past the last hand, so the
        # period band needs the domain to place itself, not just the pixels.
        "n": n, "x0": f.x0, "x1": f.x1,
        "series": [{"name": nm, "key": k, "values": [round(v, 2) for v in vals]}
                   for nm, vals, k, _ in series],
    })
    out.append(f'<line class="crosshair" x1="0" y1="{f.top}" x2="0" '
               f'y2="{f.bottom}" style="display:none"/>')
    out.append(f'<rect class="hover-target" x="{f.left}" y="{f.top}" '
               f'width="{f.right - f.left}" height="{f.bottom - f.top}"/>')

    return ('<div class="chart-wrap" data-chart="winnings" data-payload="'
            + esc(payload) + '">' + svg("".join(out))
            + '<div class="tooltip" hidden></div></div>')


# ---------------------------------------------------------------------------
# 2. bb/100 by position, with confidence intervals


def position_bars(rows) -> str:
    if not rows:
        return '<p class="empty">No hands yet.</p>'
    lo = min([r["bb100"] - r["ci95"] for r in rows] + [0])
    hi = max([r["bb100"] + r["ci95"] for r in rows] + [0])
    yticks, ylo, yhi = nice_ticks(lo, hi, 6)
    f = Frame(0, len(rows), ylo, yhi, h=360)
    out = [f.grid(yticks, ylabel="bb/100")]

    band = (f.right - f.left) / len(rows)
    for i, r in enumerate(rows):
        cx = f.left + band * (i + 0.5)
        bw = min(52, band * 0.5)
        v = r["bb100"]
        y0, y1 = f.sy(0), f.sy(v)
        cls = "pos" if v >= 0 else "neg"
        out.append(
            f'<rect class="bar {cls}" x="{cx - bw / 2:.1f}" y="{min(y0, y1):.1f}" '
            f'width="{bw:.1f}" height="{max(abs(y1 - y0), 1):.1f}" rx="4">'
            f'<title>{esc(r["position"])}: {fmt(v, 1)} bb/100 over '
            f'{r["hands"]} hands (95% CI +/- {fmt(r["ci95"], 0)})</title></rect>'
        )
        elo, ehi = f.sy(v - r["ci95"]), f.sy(v + r["ci95"])
        out.append(
            f'<line class="ci" x1="{cx:.1f}" y1="{elo:.1f}" x2="{cx:.1f}" '
            f'y2="{ehi:.1f}"/>'
            f'<line class="ci" x1="{cx - 6:.1f}" y1="{elo:.1f}" x2="{cx + 6:.1f}" '
            f'y2="{elo:.1f}"/>'
            f'<line class="ci" x1="{cx - 6:.1f}" y1="{ehi:.1f}" x2="{cx + 6:.1f}" '
            f'y2="{ehi:.1f}"/>'
        )
        out.append(
            f'<text class="tick" x="{cx:.1f}" y="{f.bottom + 18}" '
            f'text-anchor="middle">{esc(r["position"])}</text>'
        )
        out.append(
            f'<text class="tick small" x="{cx:.1f}" y="{f.bottom + 32}" '
            f'text-anchor="middle">n={r["hands"]}</text>'
        )
    return svg("".join(out), h=360)


# ---------------------------------------------------------------------------
# 3. range heatmap


def _grid_cells(side: str) -> str:
    cell, pad = 36, 10
    cells = []
    for row in range(13):
        for col in range(13):
            combo = grid_cell(row, col)
            x = pad + col * cell
            y = pad + row * cell
            shape = "pair" if row == col else ("suited" if row < col else "offsuit")
            cells.append(
                f'<g class="cellg" data-combo="{combo}" data-side="{side}">'
                f'<rect class="cell {shape}" x="{x}" y="{y}" width="{cell - 2}" '
                f'height="{cell - 2}" rx="3"/>'
                f'<text class="cell-label" x="{x + (cell - 2) / 2:.1f}" '
                f'y="{y + (cell - 2) / 2 + 4:.1f}" text-anchor="middle">{combo}</text>'
                f'</g>'
            )
    size = cell * 13 + pad * 2
    return svg("".join(cells), w=size, h=size, cls="chart grid13")


def range_heatmap(period_grids: dict, positions) -> str:
    """13x13 grids, recoloured client-side, as one panel or two.

    Two panels rather than one when a period is selected: the question a period
    filter is asked is "did the range drift", and drift is invisible in a single
    grid no matter how carefully it is coloured. Both panels are painted from
    one shared scale, because two grids on independent scales would show a
    difference in colour that is not a difference in the data.

    Cells arrive as compact `[n, vpip, bb100]` triples rather than named
    objects. Every period carries a grid per position for both panels, and the
    field names cost more bytes than the numbers do.
    """
    options = "".join(
        f'<button type="button" class="chip" data-pos="{esc(k)}">{esc(k)}</button>'
        for k in positions
    )
    panels = (
        '<div class="panel" data-side="sel">'
        '<div class="panel-head">Selected</div>' + _grid_cells("sel") + '</div>'
        '<div class="panel" data-side="pri" hidden>'
        '<div class="panel-head">Prior</div>' + _grid_cells("pri") + '</div>'
    )
    return (
        '<div class="heatmap" data-payload="' + esc(json.dumps(period_grids)) + '">'
        '<div class="chip-row" role="group" aria-label="Filter by position">'
        '<span class="chip-label">Position</span>' + options + '</div>'
        '<div class="chip-row" role="group" aria-label="Colour metric">'
        '<span class="chip-label">Colour</span>'
        '<button type="button" class="chip metric" data-metric="freq">'
        'Frequency played</button>'
        '<button type="button" class="chip metric" data-metric="bb100">bb/100</button>'
        '</div><div class="panels">' + panels + '</div>'
        '<div class="scale-legend"></div>'
        '<div class="tooltip" hidden></div></div>'
    )


# ---------------------------------------------------------------------------
# 4. rolling stat trend


def rolling_trend(rows, targets: dict) -> str:
    if not rows:
        return ('<p class="empty">Not enough hands for a rolling window yet '
                '(needs at least one full window).</p>')
    keys = [("vpip", "VPIP", "s1"), ("pfr", "PFR", "s2"), ("threebet", "3-Bet", "s3")]
    vals = [r[k] for k, _, _ in keys for r in rows if r[k] is not None]
    tvals = [v for v in targets.values() if v is not None]
    if not vals:
        return '<p class="empty">Not enough hands yet.</p>'
    yticks, ylo, yhi = nice_ticks(min(vals + tvals + [0]), max(vals + tvals), 5)
    xticks, _, xhi = nice_ticks(rows[0]["hand"], rows[-1]["hand"], 6)
    f = Frame(rows[0]["hand"], max(xhi, rows[-1]["hand"]), ylo, yhi, h=340)
    out = [f.grid(yticks, xticks, yfmt=lambda v: f"{v:.0f}%",
                  ylabel="Frequency", xlabel="Hands played")]
    out.append(f'<rect class="period-band" x="{f.left}" y="{f.top}" width="0" '
               f'height="{f.bottom - f.top:.1f}"/>')

    for key, _, cls in keys:
        target = targets.get(key)
        if target is not None:
            y = f.sy(target)
            out.append(f'<line class="target {cls}" x1="{f.left}" y1="{y:.1f}" '
                       f'x2="{f.right}" y2="{y:.1f}"/>')
    for key, name, cls in keys:
        pts = " ".join(f"{f.sx(r['hand']):.1f},{f.sy(r[key]):.1f}"
                       for r in rows if r[key] is not None)
        if not pts:
            continue
        out.append(f'<polyline class="line {cls}" points="{pts}"/>')
        last = next(r for r in reversed(rows) if r[key] is not None)
        out.append(
            f'<text class="direct-label {cls}" x="{f.right + 6}" '
            f'y="{f.sy(last[key]) + 4:.1f}">{esc(name)}</text>'
        )
    # x is a hand number here, not an index, so the band needs the domain as
    # well as the pixel extent to place itself.
    payload = json.dumps({"left": f.left, "right": f.right,
                          "x0": f.x0, "x1": f.x1})
    return ('<div class="chart-wrap" data-chart="rolling" data-payload="'
            + esc(payload) + '">' + svg("".join(out), h=340) + '</div>')


# ---------------------------------------------------------------------------
# 5. street funnel


def funnel(stages) -> str:
    """Cumulative bars, incremental money.

    The bars answer "how far did the hands get"; the columns answer "what did
    the hands that stopped here cost", which is the question a cumulative
    column could only answer by subtraction.
    """
    if not stages or not stages[0]["hands"]:
        return '<p class="empty">No hands yet.</p>'
    top = stages[0]["hands"]
    rowh, gap, head = 44, 12, 26
    h = head + len(stages) * (rowh + gap) + 12
    left, right = 132, 500
    cols = [(596, "Reached"), (690, "Ended here"), (790, "Net bb"), (884, "bb/hand")]
    out = [f'<text class="tick small" x="{x}" y="{head - 10}" '
           f'text-anchor="end">{esc(t)}</text>' for x, t in cols]
    for i, s in enumerate(stages):
        y = head + i * (rowh + gap)
        mid = y + rowh / 2 + 4
        frac = s["hands"] / top if top else 0
        width = max(2.0, frac * (right - left))
        cls = "pos" if s["exit_bb"] >= 0 else "neg"
        out.append(
            f'<rect class="funnel step{i}" x="{left}" y="{y}" width="{width:.1f}" '
            f'height="{rowh}" rx="4"><title>{esc(s["stage"])}: {s["hands"]:,} hands '
            f'({frac * 100:.1f}%). {s["exit_hands"]:,} of these {esc(s["exit_label"])}, '
            f'worth {fmt(s["exit_bb"], 1)} bb '
            f'({fmt(s["exit_bb_hand"], 2)} bb/hand)</title></rect>'
        )
        out.append(f'<text class="funnel-label" x="{left - 12}" '
                   f'y="{mid - 6}" text-anchor="end">{esc(s["stage"])}</text>')
        out.append(f'<text class="tick small" x="{left - 12}" y="{mid + 9}" '
                   f'text-anchor="end">{esc(s["exit_label"])}</text>')
        out.append(f'<text class="funnel-value" x="{cols[0][0]}" y="{mid}" '
                   f'text-anchor="end">{s["hands"]:,} ({frac * 100:.0f}%)</text>')
        out.append(f'<text class="funnel-value" x="{cols[1][0]}" y="{mid}" '
                   f'text-anchor="end">{s["exit_hands"]:,}</text>')
        out.append(f'<text class="funnel-money {cls}" x="{cols[2][0]}" y="{mid}" '
                   f'text-anchor="end">{fmt(s["exit_bb"], 1)}</text>')
        out.append(f'<text class="funnel-money {cls}" x="{cols[3][0]}" y="{mid}" '
                   f'text-anchor="end">{fmt(s["exit_bb_hand"], 2)}</text>')
    return svg("".join(out), h=h)


# ---------------------------------------------------------------------------
# 6. pot-size buckets


def pot_buckets(rows) -> str:
    if not rows or not any(r["hands"] for r in rows):
        return '<p class="empty">No hands yet.</p>'
    vals = [r["net_bb"] for r in rows]
    yticks, ylo, yhi = nice_ticks(min(vals + [0]), max(vals + [0]), 5)
    f = Frame(0, len(rows), ylo, yhi, h=340)
    out = [f.grid(yticks, ylabel="Net bb")]
    band = (f.right - f.left) / len(rows)
    for i, r in enumerate(rows):
        cx = f.left + band * (i + 0.5)
        bw = min(70, band * 0.55)
        y0, y1 = f.sy(0), f.sy(r["net_bb"])
        cls = "pos" if r["net_bb"] >= 0 else "neg"
        out.append(
            f'<rect class="bar {cls}" x="{cx - bw / 2:.1f}" y="{min(y0, y1):.1f}" '
            f'width="{bw:.1f}" height="{max(abs(y1 - y0), 1):.1f}" rx="4">'
            f'<title>{esc(r["label"])}: {fmt(r["net_bb"], 1)} bb over '
            f'{r["hands"]} hands</title></rect>'
        )
        out.append(f'<text class="tick" x="{cx:.1f}" y="{f.bottom + 18}" '
                   f'text-anchor="middle">{esc(r["label"])}</text>')
        out.append(f'<text class="tick small" x="{cx:.1f}" y="{f.bottom + 32}" '
                   f'text-anchor="middle">n={r["hands"]}</text>')
    return svg("".join(out), h=340)


# ---------------------------------------------------------------------------
# 7. made-hand strength


def hand_classes(rows) -> str:
    """Net bb by what the hero's hand actually was, weakest bucket first.

    Deliberately the same bar form as the pot-size buckets: the two answer the
    same question one level apart -- where the money goes, and what was in your
    hand when it went there -- and a reader who has learned to read one should
    not have to learn to read the other.
    """
    if not rows or not any(r["hands"] for r in rows):
        return '<p class="empty">No classified hands in this range yet.</p>'
    vals = [r["net_bb"] for r in rows]
    yticks, ylo, yhi = nice_ticks(min(vals + [0]), max(vals + [0]), 5)
    f = Frame(0, len(rows), ylo, yhi, h=340)
    out = [f.grid(yticks, ylabel="Net bb")]
    band = (f.right - f.left) / len(rows)
    for i, r in enumerate(rows):
        cx = f.left + band * (i + 0.5)
        bw = min(70, band * 0.55)
        y0, y1 = f.sy(0), f.sy(r["net_bb"])
        cls = "pos" if r["net_bb"] >= 0 else "neg"
        per = "" if r["bb_hand"] is None else f", {fmt(r['bb_hand'], 1)} bb/hand"
        out.append(
            f'<rect class="bar {cls}" x="{cx - bw / 2:.1f}" y="{min(y0, y1):.1f}" '
            f'width="{bw:.1f}" height="{max(abs(y1 - y0), 1):.1f}" rx="4">'
            f'<title>{esc(r["label"])}: {fmt(r["net_bb"], 1)} bb over '
            f'{r["hands"]} hand{"" if r["hands"] == 1 else "s"}{per}</title></rect>'
        )
        out.append(f'<text class="tick" x="{cx:.1f}" y="{f.bottom + 18}" '
                   f'text-anchor="middle">{esc(r["class"])}</text>')
        out.append(f'<text class="tick small" x="{cx:.1f}" y="{f.bottom + 32}" '
                   f'text-anchor="middle">n={r["hands"]}</text>')
    return svg("".join(out), h=340)


# ---------------------------------------------------------------------------
# 8. session scatter


def session_scatter(sessions) -> str:
    if not sessions:
        return '<p class="empty">No sessions yet.</p>'
    xs = [s["duration_min"] for s in sessions]
    ys = [s["bb100"] for s in sessions]
    xticks, xlo, xhi = nice_ticks(0, max(xs + [1]), 5)
    yticks, ylo, yhi = nice_ticks(min(ys + [0]), max(ys + [0]), 5)
    f = Frame(xlo, xhi, ylo, yhi, h=340)
    out = [f.grid(yticks, xticks, ylabel="bb/100",
                  xlabel="Session duration (minutes)")]
    for s in sessions:
        r = 5 + min(11.0, math.sqrt(max(s["hands"], 1)) * 0.9)
        cls = "pos" if s["bb100"] >= 0 else "neg"
        out.append(
            f'<circle class="dot {cls}" cx="{f.sx(s["duration_min"]):.1f}" '
            f'cy="{f.sy(s["bb100"]):.1f}" r="{r:.1f}">'
            f'<title>{esc(s["start"][:16])}: {s["hands"]} hands, '
            f'{s["n_tables"]} table(s), {fmt(s["bb100"], 1)} bb/100 over '
            f'{s["duration_min"]:.0f} min</title></circle>'
        )
    return svg("".join(out), h=340)


# ---------------------------------------------------------------------------
# 9. stack-size histogram


def stack_histogram(rows, width: int = 10) -> str:
    if not rows:
        return '<p class="empty">No hands yet.</p>'
    top = max(r["n"] for r in rows)
    yticks, ylo, yhi = nice_ticks(0, top, 4)
    lo = min(r["bucket"] for r in rows)
    hi = max(r["bucket"] for r in rows) + width
    f = Frame(lo, hi, ylo, yhi, h=300)
    out = [f.grid(yticks, [r["bucket"] for r in rows], ylabel="Hands",
                  xlabel="Effective stack at hand start (bb)")]
    bw = (f.right - f.left) / max(1, (hi - lo) / width)
    for r in rows:
        x, y = f.sx(r["bucket"]), f.sy(r["n"])
        cls = "neg" if r["bucket"] < 80 else "pos"
        out.append(
            f'<rect class="bar {cls}" x="{x + 1:.1f}" y="{y:.1f}" '
            f'width="{max(bw - 2, 2):.1f}" height="{f.bottom - y:.1f}" rx="3">'
            f'<title>{r["bucket"]}-{r["bucket"] + width}bb: {r["n"]} hands</title>'
            f'</rect>'
        )
    x80 = f.sx(80)
    if f.left <= x80 <= f.right:
        out.append(f'<line class="target ref" x1="{x80:.1f}" y1="{f.top}" '
                   f'x2="{x80:.1f}" y2="{f.bottom}"/>')
        out.append(f'<text class="tick small" x="{x80 + 5:.1f}" '
                   f'y="{f.top + 12}">80bb</text>')
    return svg("".join(out), h=300)
