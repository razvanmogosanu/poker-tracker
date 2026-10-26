"""Assemble the self-contained HTML dashboard."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import charts, db, handclass, ranges, solver, stats
from .cards import RANK_CHARS, all_combos, grid_cell
from .charts import esc, fmt
from .positions import POSITION_ORDER

# Reference values drawn on the rolling-trend chart. These are targets, not
# measurements: edit them to match the strategy you are actually running.
TARGETS = {"vpip": 22.0, "pfr": 18.0, "threebet": 7.0, "btn_open": 45.0}

# How the preflop row of the street breakdown is read, in bb per hand. Under
# one open the money really is the cost of arriving and belongs in the
# background; a pot that cost more than a 3-bet was entered on purpose, and at
# that point it is the largest number in the row and the one decision in it
# nobody was forced into. These are the sizes of the bets themselves, not
# tuned cutoffs: an open is 2.5-3bb and a 3-bet pot starts around 10.
PREFLOP_ARRIVING_BB = 5.0
PREFLOP_CHOSEN_BB = 10.0


# The page's stylesheet and scripts live as real .css/.js files beside this
# module, so they get an editor and a readable diff, and are inlined at build
# time: the report is still one self-contained file.
_STATIC = Path(__file__).resolve().parent / "static"


def _asset(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


CSS = _asset("report.css")
JS = _asset("report.js")
# The refresh toolbar's poll; see serve.py. Live page only, like SOLVER_JS.
LIVE_JS = _asset("live.js")

# The Solver tab. Only the live page carries this: every number on the tab is
# computed by the server, so on a file:// page there is nothing for it to call.
# It holds no poker logic at all -- not the range syntax, not card removal, not
# a pot-odds formula. It collects a scenario, posts it, and paints the answer,
# so there is one definition of each of those and it is the tested one.
SOLVER_JS = _asset("solver.js")


_PILLS = {
    "ok": '<span class="pill good">on plan</span>',
    "off": '<span class="pill bad">off plan</span>',
    "watch": '<span class="pill none">watch</span>',
    # Not a soft failure. "0 opens out of 2 opportunities" is not a player who
    # never opens the button; it is a window that cannot answer the question,
    # and saying "off plan" there teaches the reader to distrust the pills.
    "thin": '<span class="pill thin">n too small</span>',
}


def _pill(status) -> str:
    return _PILLS.get(status, _PILLS["watch"])


def _delta_cell(new, old, unit: str, enough: bool = True) -> str:
    """One difference, unsigned by colour and blank when it would be noise."""
    if not enough or new is None or old is None:
        return ('<td class="delta none" title="Too few opportunities on one '
                'side for the difference to mean anything">&ndash;</td>')
    d = new - old
    return f'<td class="delta">{d:+.1f}{unit}</td>'


def _rate_table(rows, prior=None) -> str:
    """Rates with their intervals, and against the prior period when filtered.

    The delta column is the point of the period filter: "is the thing I am
    working on moving" is a different question from "what is my donk-flop
    frequency", and only the first one has an answer on a Tuesday. It is left
    blank whenever either side has fewer than MIN_OPPS opportunities, because
    the difference between two noisy numbers is noisier than either of them.
    """
    if prior is None:
        body = []
        for r in rows:
            pct = "-" if r["pct"] is None else f"{r['pct']:.1f}%"
            ci = ("-" if r["ci_lo"] is None or not r["den"]
                  else f"{r['ci_lo']:.1f} - {r['ci_hi']:.1f}")
            body.append(
                f"<tr><td>{esc(r['name'])}</td><td>{pct}</td>"
                f"<td class='ci'>{ci}</td>"
                f"<td class='thin'>{int(r['num'])} / {int(r['den'])}</td></tr>"
            )
        return (
            "<table><thead><tr><th>Stat</th><th>Rate</th>"
            "<th>95% CI</th><th>n</th></tr></thead><tbody>"
            + "".join(body) + "</tbody></table>"
        )

    by_name = {r["name"]: r for r in prior}
    body = []
    for r in rows:
        o = by_name.get(r["name"], {"pct": None, "den": 0, "num": 0})
        pct = "-" if r["pct"] is None else f"{r['pct']:.1f}%"
        opct = "-" if o["pct"] is None else f"{o['pct']:.1f}%"
        ci = ("-" if r["ci_lo"] is None or not r["den"]
              else f"{r['ci_lo']:.1f} - {r['ci_hi']:.1f}")
        enough = r["den"] >= stats.MIN_OPPS and o["den"] >= stats.MIN_OPPS
        body.append(
            f"<tr><td>{esc(r['name'])}</td><td>{pct}</td>"
            f"<td class='prior'>{opct}</td>"
            + _delta_cell(r["pct"], o["pct"], "", enough)
            + f"<td class='ci'>{ci}</td>"
            f"<td class='thin'>{int(r['num'])} / {int(r['den'])}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Stat</th><th>Selected</th><th>Prior</th>"
        "<th>&Delta;</th><th>95% CI (selected)</th><th>n</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table>"
    )


def _compliance_table(rows, prior=None) -> str:
    """Compliance checks as rates, not as counters that only go up.

    A cumulative count stops carrying information after the first week: once
    "SB cold calls" reads 11 it reads 11 forever, and no amount of good play
    moves it. Both the per-100-hand rate and the comparison against the prior
    period exist so the number can come down.
    """
    def value_cell(c, cls=""):
        if c["kind"] == "count":
            per = ("" if c["per100"] is None else
                   f"<small class='per100'>{c['per100']:.2f} per 100</small>")
            return f"<td class='{cls}'>{int(c['num'])}{per}</td>"
        pct = "-" if c["value"] is None else f"{c['value']:.1f}%"
        return (f"<td class='{cls}'>{pct}"
                f"<small class='per100'>{int(c['num'])} / {int(c['den'])}</small></td>")

    by_name = {c["name"]: c for c in (prior or [])}
    body = []
    for c in rows:
        cells = [f"<td>{esc(c['name'])}</td>", value_cell(c)]
        if prior is not None:
            o = by_name.get(c["name"])
            if o is None:
                cells.append("<td class='prior'>-</td>")
                cells.append(_delta_cell(None, None, "", False))
            else:
                cells.append(value_cell(o, "prior"))
                if c["kind"] == "count":
                    # Raw counts across windows of different sizes are not
                    # comparable; the per-100 rate is what "more or less than
                    # before" actually means.
                    cells.append(_delta_cell(c["per100"], o["per100"], " /100",
                                             bool(c["den"] and o["den"])))
                else:
                    enough = (c["den"] >= stats.MIN_OPPS
                              and o["den"] >= stats.MIN_OPPS)
                    cells.append(_delta_cell(c["value"], o["value"], " pp", enough))
        cells.append(f"<td class='thin'>{esc(c['target'])}</td>")
        cells.append(f"<td>{_pill(c['status'])}</td>")
        cells.append(f"<td class='thin'>{esc(c['note'])}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")

    extra = "<th>Prior</th><th>&Delta;</th>" if prior is not None else ""
    head = (f"<tr><th>Check</th><th>Selected</th>{extra}"
            f"<th>Target</th><th></th><th>Note</th></tr>")
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


HAND_COLUMNS = [
    ("Hand", "text"), ("Time, table", "text"), ("Pos", "text"), ("Cards", "text"),
    ("Board", "text"), ("Made", "num"), ("Pot (bb)", "num"), ("Net (bb)", "num"),
    ("Exit", "num"), ("Result", "text"), ("Shown", "text"),
]

# The order the streets actually happen in, so sorting the Exit column reads as
# a progression through the hand rather than alphabetically as flop/preflop/river.
_EXIT_RANK = {"preflop": 0, "flop": 1, "turn": 2, "river": 3, "showdown": 4}


def _hand_rows(rows) -> str:
    out = []
    for r in rows:
        tag = "win" if r["net_bb"] > 0 else "loss"
        shown = ", ".join(f"{x['player']} {x['cards']}" for x in r["shown"])
        # Sorting the Made column has to run weakest-to-strongest, not
        # alphabetically -- "two pair" before "weak pair" would be nonsense.
        # An unclassified hand (folded preflop) sorts below "no pair".
        made = r["hand_class"]
        made_rank = handclass.RANK.get(made, -1)
        made_cls = " nopair" if made == handclass.NO_PAIR else ""
        out.append(
            "<tr>"
            f"<td><button type=\"button\" class=\"hand-btn\" data-hand=\"{r['hand_id']}\" "
            f"data-no=\"{esc(r['site_hand_no'])}\" aria-expanded=\"false\" "
            f"title=\"Show the raw hand history\">#{esc(r['site_hand_no'])}</button>"
            f"<button type=\"button\" class=\"hand-btn analyse-btn\" "
            f"data-analyse=\"{r['hand_id']}\" "
            f"title=\"Open this hand in the Solver tab\">Analyse</button></td>"
            f"<td class=\"when\" data-v=\"{esc(r['played_at'])}\">{esc(r['played_at'][:16])}"
            f"<small>{esc(r['table_name'])}</small></td>"
            f"<td>{esc(r['position'])}</td>"
            f"<td class=\"cards\">{esc(r['hole_cards'])}</td>"
            f"<td class=\"board\">{esc(r['board'])}</td>"
            f"<td class=\"made{made_cls}\" data-v=\"{made_rank}\">{esc(made)}</td>"
            f"<td data-v=\"{r['pot_bb']:.4f}\">{r['pot_bb']:.1f}</td>"
            f"<td class=\"money {'pos' if r['net_bb'] > 0 else 'neg'}\" "
            f"data-v=\"{r['net_bb']:.4f}\">{r['net_bb']:+.1f}</td>"
            f"<td data-v=\"{_EXIT_RANK.get(r['exit_street'], 9)}\">{esc(r['exit_street'])}</td>"
            f"<td><span class=\"tag {tag}\">{esc(r['outcome'])}</span></td>"
            f"<td class=\"opp\">{esc(shown)}</td>"
            "</tr>"
        )
    return "".join(out)


def _hand_table(rows, key: str, hidden: bool) -> str:
    if not rows:
        return (f'<table class="hands" data-tbl="{key}"{" hidden" if hidden else ""}>'
                f'<tbody><tr><td class="empty">No hands in this direction yet.</td>'
                f'</tr></tbody></table>')
    head = "".join(
        f'<th class="sortable-col" data-type="{typ}">{esc(name)}</th>'
        for name, typ in HAND_COLUMNS
    )
    return (f'<table class="hands" data-tbl="{key}"{" hidden" if hidden else ""}>'
            f'<thead><tr>{head}</tr></thead><tbody>{_hand_rows(rows)}</tbody></table>')


def _drilldown(conn, hero: str, window, limit: int = 15):
    """Top losses and top wins, each row expandable to the original text.

    Returns the markup and the hand ids it references. The text itself is
    pooled across every period by the caller rather than inlined here: the
    biggest-pot lists overlap heavily, and a per-period copy would put the same
    hand in the file five times.
    """
    pots = stats.big_pots(conn, hero, limit, window)
    ids = [r["hand_id"] for r in pots["losses"] + pots["wins"]]
    n_loss, n_win = len(pots["losses"]), len(pots["wins"])
    html = f"""<div class="card" data-drilldown>
<div class="chip-row"><span class="chip-label">Show</span>
<button type="button" class="chip" data-tbl="losses" aria-pressed="true">
Top {n_loss} losses</button>
<button type="button" class="chip" data-tbl="wins" aria-pressed="false">
Top {n_win} wins</button></div>
{_hand_table(pots['losses'], 'losses', False)}
{_hand_table(pots['wins'], 'wins', True)}
<p class="note">Click any column heading to re-sort; click a hand number to read
the original PokerStars text.</p>
<div class="raw-panel" hidden><div class="raw-head"><span></span></div><pre></pre></div>
</div>"""
    return html, ids


def _handclass_banner(big_rows) -> str:
    """The section compressed to one sentence, or nothing to say.

    The money is stated whenever a bucket lost any, because a realized loss is
    a fact rather than an estimate of anything. What a thin bucket cannot
    support is the second sentence -- the one that turns the number into a
    diagnosis -- so below `stats.MIN_CLASS_HANDS` that sentence is replaced
    rather than the figure being hidden. A filtered period is where this bites:
    one cooler is enough to put a bucket at the bottom of a short list.
    """
    worst = stats.worst_class(big_rows)
    total = sum(r["hands"] for r in big_rows)
    if not worst:
        if not total:
            return ""
        return ('<div class="banner">No made-hand bucket is losing money in '
                f'pots over {stats.BIG_POT_BB:.0f}bb. Over {total:,} such '
                'hands that is a small sample, not a verdict.</div>')

    if worst["hands"] >= stats.MIN_CLASS_HANDS:
        verdict = ('That is the bucket costing you the most in pots big enough '
                   'to matter. Nothing else on this page names it: the pot-size '
                   'buckets say the money leaves in big pots, and the '
                   'biggest-pots list makes you read fifteen hands to find out '
                   'why.')
    else:
        verdict = (f'Fewer than {stats.MIN_CLASS_HANDS} hands, so read it as a '
                   'hand to go and look at rather than as a pattern. Switch to '
                   '<b>All</b> for the version that can carry a conclusion.')
    return (f'<div class="banner"><b>Pots over {stats.BIG_POT_BB:.0f}bb reached '
            f'with {esc(worst["label"])}: {worst["hands"]:,} '
            f'hand{"" if worst["hands"] == 1 else "s"}, '
            f'{worst["net_bb"]:+,.0f} bb.</b> {verdict}</div>')


def _preflop_weight(street: str, bb_in_hand) -> str:
    """How loudly a preflop cell should be read, from its own size.

    Fixed dimming was wrong in the one column that matters. Preflop is the
    cost of arriving only while it is about one open; at 15bb a hand it is a
    3-bet pot, which is the largest number in the row and the only money in it
    that nobody was forced to put in -- and dimming that actively hid it. So
    the weight comes from the value, per cell, and the same row can be
    background in one pot range and the finding in another.

    Every other street is left alone: those are all decisions, and there is no
    size at which a turn barrel stops being one.
    """
    if street != "preflop" or bb_in_hand is None:
        return ""
    if bb_in_hand < PREFLOP_ARRIVING_BB:
        return " context"
    if bb_in_hand >= PREFLOP_CHOSEN_BB:
        return " chosen"
    return ""


def _naked_street_table(conn, hero: str, window, ranges) -> str:
    """The naked no-pair row broken out by the street the money went in on.

    The made-hand table says the bucket was paid for; this says when, and the
    when is what picks the fix. River money is a call made after the hand is
    over and the answer is to fold; turn money is a barrel into somebody who
    was never folding and the answer is not to bet. Both look identical in a
    single `bb in` figure.

    Preflop is carried so the rows add back to that figure. It is not part of
    the finding -- nothing in this section is a preflop decision -- but a
    column that does not reconcile with the one above it reads as a bug.
    """
    cols = [stats.class_street_split(conn, hero, handclass.NO_PAIR, lo, window, hi)
            for _, lo, hi in ranges]
    if not cols[0][0]["hands"]:
        return ""

    body = []
    for i, street in enumerate(stats.INVESTED_STREETS):
        cells = []
        for c in cols:
            r = c[i]
            edge = " grp" if cells else ""
            per = "-" if r["bb_in_hand"] is None else f'{r["bb_in_hand"]:,.1f}'
            share = "-" if r["share"] is None else f'{100 * r["share"]:.0f}%'
            weight = _preflop_weight(street, r["bb_in_hand"])
            cells.append(f'<td class="num{edge}{weight}">{per}</td>')
            cells.append(f'<td class="num muted{weight}">{share}</td>')
        body.append(f"<tr><td>{esc(street)}</td>{''.join(cells)}</tr>")

    heads = "".join(
        f'<th colspan="2"{" class=\"grp\"" if i else ""}>{esc(name)}</th>'
        for i, (name, _, _) in enumerate(ranges))
    subs = "".join(f'<th{" class=\"grp\"" if i else ""}>bb in</th><th>share</th>'
                   for i in range(len(ranges)))
    big = cols[-1]
    top = stats.dominant_street(big)
    post = [r for r in big if r["street"] in stats.POSTFLOP_STREETS]
    post_total = sum(r["bb_in"] for r in post)
    split = " / ".join(
        f'{100 * r["bb_in"] / post_total:.0f}%' if post_total else "-" for r in post)

    if top:
        reading = {
            "flop": "Most of it goes in on the flop, which is a continuation "
                    "bet that keeps getting called rather than a hand being "
                    "played badly later.",
            "turn": "Most of it goes in on the turn: that is a second barrel "
                    "into somebody who was never going to fold, and the fix is "
                    "not betting it.",
            "river": "Most of it goes in on the river, where the hand is "
                     "already over: that is paying people off, and the fix is "
                     "folding.",
        }[top["street"]]
    else:
        reading = ("No street holds a majority, so the money is spread and "
                   "both readings apply at once -- the turn share is barrelling "
                   "into hands that do not fold, the river share is paying off. "
                   "They are different fixes and neither one alone accounts for "
                   "this row.")

    return f"""<p class="panel-head" style="margin-top:22px">Where the
<b>{esc(handclass.LONG_NAME[handclass.NO_PAIR])}</b> money goes, by street</p>
<table><thead>
<tr><th rowspan="2">Street</th>{heads}</tr>
<tr>{subs}</tr>
</thead><tbody>{"".join(body)}</tbody></table>
<p class="note">Per hand, over the same hands as the row above, so the four
add back to its <b>bb in</b>. Postflop, pots over {stats.BIG_POT_BB:.0f}bb
split {split} across flop, turn and river. {reading} The preflop cell is read
by its own size: under {PREFLOP_ARRIVING_BB:.0f}bb it is about one open and is
dimmed as the cost of arriving, and at {PREFLOP_CHOSEN_BB:.0f}bb or more it is
a 3-bet pot you chose to build with a hand that flopped nothing, which is the
loudest number here and is marked as such.</p>"""


def _handclass_card(conn, hero: str, window) -> str:
    """Net by made-hand bucket over three pot ranges, big pots called out.

    Three ranges rather than one. Over every flopped pot the "no pair" row is
    mostly small pots that were folded and is close to noise; over the big pots
    it is the row that costs a stack at a time. The middle range is what says
    which of those two the bucket really is -- 40bb is a high enough bar that
    only a handful of pots clear it, so the big-pot column alone cannot
    distinguish a bleed that runs through the medium pots from one that stops
    at the small ones. Showing only a filtered range would hide how often the
    bucket is reached, and showing only the unfiltered one would bury the
    money, so all three are here and the banner says which to read first.

    Every range carries invested bb per hand beside its net, because the net on
    its own does not say how the money left: at -9 bb a no-pair hand is either
    a flop float given up cheaply or a river call that should not have been
    made, and those are opposite mistakes.
    """
    ranges = [
        ("Every flopped pot", 0.0, None),
        (f"Pots {stats.MID_POT_BB:.0f}-{stats.BIG_POT_BB:.0f}bb",
         stats.MID_POT_BB, stats.BIG_POT_BB),
        (f"Pots over {stats.BIG_POT_BB:.0f}bb", stats.BIG_POT_BB, None),
    ]
    cols = [stats.hand_classes(conn, hero, lo, window, hi) for _, lo, hi in ranges]
    all_rows, big_rows = cols[0], cols[-1]
    by_class = [{r["class"]: r for r in c} for c in cols]

    if not any(r["hands"] for r in all_rows):
        return ('<div class="card"><p class="empty">No hands have reached a flop '
                'with known cards yet.</p></div>')

    def money(v, places=1):
        cls = "pos" if v >= 0 else "neg"
        return f'<td class="num {cls}">{v:+,.{places}f}</td>'

    def plain(v, places=1):
        return ('<td class="num">-</td>' if v is None
                else f'<td class="num">{v:,.{places}f}</td>')

    body = []
    worst = stats.worst_class(big_rows)
    for r in all_rows:
        mark = ' class="spotlight"' if worst and r["class"] == worst["class"] else ""
        cells = []
        for lookup in by_class:
            c = lookup[r["class"]]
            edge = " grp" if cells else ""
            cells.append(f'<td class="num{edge}">{c["hands"]:,}</td>')
            cells.append(money(c["net_bb"]))
            cells.append(plain(c["bb_in_hand"]))
        body.append(f"<tr{mark}><td>{esc(r['label'])}</td>{''.join(cells)}</tr>")

    banner = _handclass_banner(big_rows)
    heads = "".join(
        f'<th colspan="3"{" class=\"grp\"" if i else ""}>{esc(name)}</th>'
        for i, (name, _, _) in enumerate(ranges))
    subs = "".join(f'<th{" class=\"grp\"" if i else ""}>Hands</th>'
                   "<th>Net bb</th><th>bb in</th>" for i in range(len(ranges)))

    return f"""<div class="card">{banner}
<p class="panel-head">Net bb in pots over {stats.BIG_POT_BB:.0f}bb, by made hand</p>
{charts.hand_classes(big_rows)}
<table style="margin-top:14px"><thead>
<tr><th rowspan="2">Made hand</th>{heads}</tr>
<tr>{subs}</tr>
</thead><tbody>{"".join(body)}</tbody></table>
<p class="note">The bucket is your hand as of the street you <em>left</em> the
hand on, so a flop fold is judged on the flop rather than on a river you never
saw. A hand the board makes on its own does not count as yours: holding A5 on
K K 7 7 2 is ace high here, not two pair, which is exactly the hand this table
exists to find. <b>bb in</b> is the average you put in per hand in that range:
it is what separates a cheap fold from a paid-off one, since the same net can
be a flop give-up or a river call. The two no-pair rows split on
{handclass.DRAW_OUTS} outs -- a flush draw or an open-ender, not a gutshot -- because a hand
that is drawing is supposed to put money in and a naked one never is, and
averaging them together flatters the bluffs and punishes the semi-bluffs.</p>
{_naked_street_table(conn, hero, window, ranges)}
</div>"""


def _compact_grid(grid: dict) -> list:
    """[n, vpip, bb100] per cell, 0 if never dealt. See charts.range_heatmap."""
    return [[v["n"], v["vpip"],
             None if v["bb100"] is None else round(v["bb100"], 1)]
            if (v := grid.get(c)) else 0
            for c in all_combos()]


def _rate_of(rows, name: str):
    r = next((x for x in rows if x["name"] == name), None)
    return None if r is None or r["pct"] is None else r["pct"]


def _headline(conn, hero: str, period, m, pre, generated: str, stake_txt: str):
    """The sub-line, the tiles and the banner above the first chart."""
    filtered = not period.is_all
    n_sessions = len(stats.sessions(conn, hero, period.selected))
    # Win rate is deliberately absent from a filtered view: one session is
    # 100-300 hands and its interval is wider than any result it could show, so
    # a tile there would only invite the reader to try to read it. Hand count
    # and the frequencies are what the window can support.
    if filtered:
        tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="k">Hands</div>
        <div class="v">{m['hands']:,}</div>
        <div class="m">{n_sessions} session{'' if n_sessions == 1 else 's'}
        &middot; {m['hands'] * 100 // max(1, period.selected.hands + period.prior.hands)}%
        of the sample</div></div>
      <div class="tile"><div class="k">VPIP</div>
        <div class="v">{fmt(_rate_of(pre, 'VPIP'), 1)}%</div>
        <div class="m">voluntarily put in pot</div></div>
      <div class="tile"><div class="k">PFR</div>
        <div class="v">{fmt(_rate_of(pre, 'PFR'), 1)}%</div>
        <div class="m">preflop raise</div></div>
      <div class="tile"><div class="k">3-Bet</div>
        <div class="v">{fmt(_rate_of(pre, '3-Bet'), 1)}%</div>
        <div class="m">of opportunities</div></div>
      <div class="tile"><div class="k">Rake paid</div>
        <div class="v">{m['rake_bb100']:.1f}</div>
        <div class="m">bb/100</div></div>
    </div>"""
        banner = (
            '<div class="banner"><b>Win rate is not shown for a filtered '
            f'period, on purpose.</b> Over {m["hands"]:,} hands the 95% interval '
            f'on bb/100 is roughly &plusmn;{m["ci95"]:.0f}, which is wider than '
            'any result it could report, so the number would be pure noise '
            'wearing a decimal point. Frequencies converge in thousands of '
            'hands rather than hundreds &mdash; those are what this view is '
            'for. Switch back to <b>All</b> for the win rate.</div>'
        )
        sub = (f"{esc(period.label)} &middot; {m['hands']:,} hands &middot; "
               f"{esc(m['first_hand'][:16])} to {esc(m['last_hand'][:16])} "
               f"&middot; compared against the {period.prior.hands:,} hands before it")
    else:
        ev_tile = ""
        if m["has_ev"]:
            ev_tile = (
                f'<div class="tile"><div class="k">All-in adjusted</div>'
                f'<div class="v">{m["ev_bb100"]:+.1f}</div>'
                f'<div class="m">bb/100 &middot; luck {m["ev_diff_bb100"]:+.1f}</div></div>'
            )
        tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="k">Win rate</div>
        <div class="v">{m['bb100']:+.1f}</div>
        <div class="m">bb/100 &plusmn; {m['ci95']:.0f} (95% CI)</div></div>
      {ev_tile}
      <div class="tile"><div class="k">Hands</div>
        <div class="v">{m['hands']:,}</div>
        <div class="m">{n_sessions} session{'' if n_sessions == 1 else 's'}</div></div>
      <div class="tile"><div class="k">Rake paid</div>
        <div class="v">{m['rake_bb100']:.1f}</div>
        <div class="m">bb/100 &middot; gross {m['gross_bb100']:+.1f}</div></div>
      <div class="tile"><div class="k">Std deviation</div>
        <div class="v">{m['sd_bb100']:.0f}</div>
        <div class="m">bb/100</div></div>
    </div>"""
        ci_lo, ci_hi = m["bb100"] - m["ci95"], m["bb100"] + m["ci95"]
        banner = (
            f'<div class="banner"><b>Read the interval, not the number.</b> '
            f'At {m["hands"]:,} hands your measured {m["bb100"]:+.1f} bb/100 is '
            f'statistically indistinguishable from anything between '
            f'{ci_lo:+.1f} and {ci_hi:+.1f}. Win rate is the last thing that '
            f'becomes reliable, not the first &mdash; judge yourself on the '
            f'frequencies and the compliance checks until the sample catches up.</div>'
        )
        sub = (f"{m['hands']:,} hands {esc(stake_txt)} &middot; "
               f"{esc(m['first_hand'][:16])} to {esc(m['last_hand'][:16])} &middot; "
               f"generated {esc(generated)}")
    return sub, tiles, banner


def _position_card(pos_rows) -> str:
    pos_table = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}%</td><td>{}%</td><td>{}</td>"
        "<td>{:+.1f}</td><td class='ci'>&plusmn;{:.0f}</td></tr>".format(
            esc(r["position"]), r["hands"], fmt(r["vpip"], 1), fmt(r["pfr"], 1),
            "-" if r["threebet"] is None else f"{r['threebet']:.1f}%",
            r["bb100"], r["ci95"])
        for r in pos_rows
    )
    return (
        f'<div class="card">{charts.position_bars(pos_rows)}'
        f'<details><summary>Table view</summary>'
        f'<table><thead><tr><th>Position</th><th>Hands</th><th>VPIP</th>'
        f'<th>PFR</th><th>3-Bet</th><th>bb/100</th><th>95% CI</th></tr></thead>'
        f'<tbody>{pos_table}</tbody></table></details></div>'
    )


def _load_card(conn, hero: str, w) -> str:
    tables = stats.by_table_count(conn, hero, w)
    hours = stats.by_time(conn, hero, "%H", w)
    days = stats.by_time(conn, hero, "%w", w)
    table_rows = "".join(
        "<tr><td>{} table{}</td><td>{:,}</td><td>{}</td></tr>".format(
            r["tables"], "" if r["tables"] == 1 else "s", r["hands"],
            "-" if r["bb100"] is None else f"{r['bb100']:+.1f}")
        for r in tables
    )
    DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    day_rows = "".join(
        "<tr><td>{}</td><td>{:,}</td><td>{}</td></tr>".format(
            DAYS[int(r["key"])], r["hands"],
            "-" if r["bb100"] is None else f"{r['bb100']:+.1f}")
        for r in days
    )
    hour_rows = "".join(
        "<tr><td>{}:00</td><td>{:,}</td><td>{}</td></tr>".format(
            r["key"], r["hands"],
            "-" if r["bb100"] is None else f"{r['bb100']:+.1f}")
        for r in hours
    )
    return (
        '<div class="card"><h3>Concurrent tables</h3>'
        '<table><thead><tr><th>Load</th><th>Hands</th><th>bb/100</th></tr>'
        f'</thead><tbody>{table_rows}</tbody></table>'
        '<h3>By day of week</h3>'
        '<table><thead><tr><th>Day</th><th>Hands</th><th>bb/100</th></tr>'
        f'</thead><tbody>{day_rows}</tbody></table>'
        '<h3>By hour of day</h3>'
        '<table><thead><tr><th>Hour</th><th>Hands</th><th>bb/100</th></tr>'
        f'</thead><tbody>{hour_rows}</tbody></table>'
        f'<h3>Timeouts</h3><p class="note">{stats.timeouts(conn, hero, w)} '
        'hand(s) where you timed out &mdash; a proxy for attention '
        'overload.</p></div>'
    )


def _sample_card(hands: int) -> str:
    rel_rows = "".join(
        "<tr><td>{}</td><td>{:,}</td><td class='thin'>{:,}</td><td>{}</td></tr>".format(
            esc(r["stat"]), r["have"], r["needed"],
            '<span class="pill good">usable</span>' if r["ready"]
            else f'<span class="pill none">{r["pct"]:.0f}%</span>')
        for r in stats.reliability(hands)
    )
    return (
        '<div class="card"><table><thead><tr><th>Stat</th><th>Hands</th>'
        f'<th>Needed</th><th></th></tr></thead><tbody>{rel_rows}'
        '</tbody></table></div>'
    )


def _fragments(conn, hero: str, period, positions, generated: str, stake_txt: str):
    """Every part of the page that a period filter changes.

    Each fragment is rendered here, once per period, and swapped into place in
    the browser. The report has to open from file:// with no server behind it,
    so the alternative -- asking the pipeline to recompute on selection -- is
    not available.
    """
    w = period.selected
    prior_w = None if period.is_all else period.prior
    filtered = not period.is_all

    m = stats.money_summary(conn, hero, w)
    pre = stats.preflop_stats(conn, hero, w)
    post = stats.postflop_stats(conn, hero, w)
    ag = stats.aggression(conn, hero, w)
    comp = stats.compliance(conn, hero, w)
    pre_prior = stats.preflop_stats(conn, hero, prior_w) if filtered else None
    post_prior = stats.postflop_stats(conn, hero, prior_w) if filtered else None
    comp_prior = stats.compliance(conn, hero, prior_w) if filtered else None

    pos_rows = stats.by_position(conn, hero, w)
    funnel_rows = stats.street_funnel(conn, hero, w)
    buckets = stats.pot_buckets(conn, hero, w)
    stacks = stats.stack_histogram(conn, hero, 10, w)
    drill_html, drill_ids = _drilldown(conn, hero, w, 15 if period.is_all else 10)
    handclass_html = _handclass_card(conn, hero, w)
    sub, tiles, banner = _headline(conn, hero, period, m, pre, generated, stake_txt)

    wwsf = next((r for r in post if r["name"] == "WWSF"), None)
    wtsd = next((r for r in post if r["name"] == "WTSD"), None)
    wsd = next((r for r in post if r["name"] == "W$SD"), None)

    grids = {"sel": {k: _compact_grid(stats.range_grid(
        conn, hero, None if k == "All" else k, w)) for k in positions}}
    if filtered:
        grids["pri"] = {k: _compact_grid(stats.range_grid(
            conn, hero, None if k == "All" else k, prior_w)) for k in positions}

    # The winnings chart is redrawn over the selected hands alone, starting
    # from zero at the window's first hand, so the lines say what happened
    # between t0 and t1 rather than where t1 left the career total. The
    # rolling chart and the session list still show the whole history.
    winnings = charts.cumulative_winnings(
        stats.bb_series(conn, hero, w), m["has_ev"],
        xlabel="Hands in period" if filtered else "Hands played")

    regions = {
        "r-sub": sub,
        "r-tiles": tiles,
        "r-banner": banner,
        "r-winnings": f'<div class="card">{_legend(m["has_ev"])}{winnings}</div>',
        "r-position": _position_card(pos_rows),
        "r-preflop": f'<div class="card">{_rate_table(pre, pre_prior)}</div>',
        "r-postflop": (
            f'<div class="card">{_rate_table(post, post_prior)}'
            f'<table style="margin-top:14px"><thead><tr><th>Aggression</th>'
            f'<th>Flop</th><th>Turn</th><th>River</th><th>All streets</th></tr>'
            f'</thead><tbody>'
            f"<tr><td>Frequency</td>{_street_cells(ag, 'afq', '%')}"
            f"<td>{fmt(ag['afq'], 1)}%</td></tr>"
            f"<tr><td>Factor</td>{_street_cells(ag, 'af', '')}"
            f"<td>{fmt(ag['af'], 2)}</td></tr>"
            f'</tbody></table></div>{_triangle_note(wwsf, wtsd, wsd)}'
        ),
        "r-compliance":
            f'<div class="card">{_compliance_table(comp, comp_prior)}</div>',
        "r-funnel": f'<div class="card">{charts.funnel(funnel_rows)}</div>',
        "r-buckets": f'<div class="card">{charts.pot_buckets(buckets)}</div>',
        "r-handclass": handclass_html,
        "r-drilldown": drill_html,
        "r-stacks": f'<div class="card">{charts.stack_histogram(stacks)}</div>',
        "r-load": _load_card(conn, hero, w),
        "r-sample": _sample_card(m["hands"]),
    }
    return regions, grids, drill_ids


_SUIT_SYMBOL = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}


def _solver_panel(live: bool) -> str:
    """The Solver tab. The static page gets the explanation and no machinery.

    Everything on the tab is computed by `solver.analyse` through the server,
    so a page opened from disk has nothing to ask. It still shows the tab,
    because a tab that exists on one page and not the other reads as a bug,
    and says what to start.
    """
    intro = (
        '<p class="note">Pick your hand, a board and the range you put them '
        'on, and this works out your equity, what their range is made of on '
        'that board, and what calling or betting is worth. '
        f'{esc(solver.CAVEAT)}</p>')
    if not live:
        return intro + (
            '<div class="card"><p class="empty">The Solver calculates in '
            'Python, so it needs the tracker running. Start <b>Poker '
            'tracker.cmd</b> (or <code>python -m pokertracker.cli serve</code>) '
            'and use the page it opens.</p></div>')

    def slot(key: str, label: str) -> str:
        return (f'<button type="button" class="sv-slot" data-slot="{key}" '
                f'aria-label="{label}" aria-pressed="false"></button>')

    slots = (
        '<small>You</small>' + slot("h0", "Your first card")
        + slot("h1", "Your second card")
        + '<span class="sv-gap"></span><small>Flop</small>'
        + "".join(slot(f"b{i}", f"Flop card {i + 1}") for i in range(3))
        + '<span class="sv-gap"></span><small>Turn</small>' + slot("b3", "Turn")
        + '<small>River</small>' + slot("b4", "River"))
    deck = "".join(
        f'<button type="button" class="sv-card" data-card="{r}{s}" '
        f'data-suit="{s}">{r}{_SUIT_SYMBOL[s]}</button>'
        for s in "shdc" for r in reversed(RANK_CHARS))
    grid = "".join(
        f'<button type="button" class="sv-cell{" pair" if row == col else ""}" '
        f'data-cell="{grid_cell(row, col)}" aria-pressed="false">'
        f'{grid_cell(row, col)}</button>'
        for row in range(13) for col in range(13))
    presets = "".join(
        f'<button type="button" class="chip" data-preset="{esc(key)}" '
        f'aria-pressed="false">{esc(label)}</button>'
        for key, label, _text in ranges.PRESETS)
    data = _json_script(
        {"presets": [{"key": k, "label": label, "range": ranges.to_text(ranges.parse(t))}
                     for k, label, t in ranges.PRESETS]},
        "solver-data")

    return intro + f"""<div class="card" id="solver">
<div class="banner" id="sv-hand" hidden></div>
<div class="sv-cols">
<div class="sv-col">
<h3>Your hand and the board</h3>
<div class="sv-slots">{slots}</div>
<div class="sv-deck">{deck}</div>
<div class="chip-row" style="margin-top:16px"><span class="chip-label">Spot</span>
<button type="button" class="chip" data-mode="facing_bet" aria-pressed="true">Facing a bet</button>
<button type="button" class="chip" data-mode="betting" aria-pressed="false">Betting</button></div>
<label class="sv-field"><span id="sv-pot-label">Pot (bb)</span>
<input id="sv-pot" type="number" min="0" step="0.5" value="6"></label>
<label class="sv-field"><span id="sv-bet-label">To call (bb)</span>
<input id="sv-bet" type="number" min="0" step="0.5" value="3"></label>
</div>
<div class="sv-col">
<h3>Their range</h3>
<div class="chip-row"><span class="chip-label">Your chart</span>{presets}</div>
<div class="sv-grid">{grid}</div>
<label class="sv-field wide"><span>As text &mdash; e.g. QQ+, AKs, A5s-A2s, KTs+
&middot; <span id="sv-count">0 holdings</span></span>
<input id="sv-range" type="text" spellcheck="false" autocomplete="off"></label>
</div>
</div>
<div class="sv-actions">
<button type="button" class="sv-go" id="sv-go">Calculate</button>
<button type="button" class="chip" id="sv-clear">Clear</button>
<span class="status" id="sv-status"></span>
</div>
<div id="sv-out"></div>
</div>
{data}"""


def _json_script(payload: dict, element_id: str) -> str:
    """Inline JSON that cannot terminate its own script element."""
    text = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="{element_id}">{text}</script>'


def _tourney_note(tourn) -> str:
    if not tourn["hands"]:
        return ""
    return (
        f" Cash games only: {tourn['hands']:,} tournament "
        f"{'hand' if tourn['hands'] == 1 else 'hands'} across "
        f"{tourn['events']:,} "
        f"{'event' if tourn['events'] == 1 else 'events'} are imported but "
        "excluded from every figure above.")


def _period_bar(meta) -> str:
    chips = "".join(
        f'<button type="button" class="chip" data-period="{esc(p["key"])}" '
        f'aria-pressed="{"true" if p["key"] == "all" else "false"}">'
        f'{esc(p["label"])}'
        + ("" if p["key"] == "all" else
           f' <span class="chip-count">({p["hands"]:,})</span>')
        + '</button>'
        for p in meta
    )
    return (
        '<div class="chip-row period-bar" role="group" '
        'aria-label="Filter by period">'
        '<span class="chip-label">Period</span>' + chips + '</div>'
        '<p class="period-note">Filters the tables, the frequencies and the '
        'compliance checks, and adds a comparison against everything before '
        'the selected window. The winnings chart redraws over the selected '
        'hands, starting from zero; the rolling chart and the session list '
        'always show the whole history, with the selected period shaded.</p>'
    )


def _legend(has_ev: bool) -> str:
    return (
        '<div class="legend">'
        '<span><i style="border-color:var(--total)"></i>Total (net)</span>'
        '<span><i style="border-color:var(--showdown);border-top-style:dashed"></i>'
        'Showdown &mdash; blue line</span>'
        '<span><i style="border-color:var(--nonshowdown);border-top-style:dotted"></i>'
        'Non-showdown &mdash; red line</span>'
        + ('<span><i style="border-color:var(--ev);border-top-style:dashed"></i>'
           'All-in adjusted EV</span>' if has_ev else "")
        + '</div>'
    )


def _session_rows(sess) -> str:
    return "".join(
        "<tr><td>{}</td><td>{:.0f} min</td><td>{:,}</td><td>{}</td>"
        "<td>{:+.1f}</td></tr>".format(
            esc(s["start"][:16]), s["duration_min"], s["hands"],
            s["n_tables"], s["bb100"])
        for s in sess
    )


def _problems_block(conn, n_problems: int) -> str:
    if not n_problems:
        return ""
    items = "".join(
        f"<li><code>{esc(p['site_hand_no'])}</code> &mdash; {esc(p['problem'])}</li>"
        for p in stats.problems(conn, 40))
    return (
        f'<h2>Parse problems</h2><div class="banner">{n_problems} problem(s) '
        f'recorded. Every one is a hand whose arithmetic did not balance or '
        f'whose text was not understood &mdash; treat them as parser bugs, not '
        f'noise.</div><div class="card"><ul>{items}</ul></div>')


def build(conn: sqlite3.Connection, hero: str, live: bool = False) -> str:
    return build_pages(conn, hero)[live]


def build_pages(conn: sqlite3.Connection, hero: str) -> dict[bool, str]:
    """The static page and the live one, keyed by `live`, from one render.

    Every figure on the two is the same; only the toolbar, the live scripts and
    the Solver panel differ. `serve.run_refresh` needs both after new hands
    arrive -- the file on disk and the page about to reload -- and rendering
    the periods twice was most of what a refresh cost.
    """
    m = stats.money_summary(conn, hero)
    if not m.get("hands"):
        empty = "<h1>No hands imported</h1>"
        return {False: empty, True: empty}

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    tourney_note = _tourney_note(stats.excluded_tournaments(conn, hero))
    # The most recent *cash* hand: the report is a cash report, and a night that
    # happened to end on a sit-and-go would otherwise put "$20.00/$40.00" in the
    # header. The predicate is stats.CASH_ONLY so there is one definition of
    # which hands this report is about.
    stake = conn.execute(
        f"SELECT sb, bb, currency FROM hands h WHERE {stats.CASH_ONLY}"
        " ORDER BY played_at DESC LIMIT 1").fetchone()
    stake_txt = f"${stake['sb'] / 100:.2f}/${stake['bb'] / 100:.2f}" if stake else ""

    # These two are drawn once over the whole history no matter what is
    # selected, with the period shaded on the rolling chart: a rolling rate
    # needs its window of hands before the first one it can plot.
    roll_window = 1000 if m["hands"] >= 1000 else max(25, m["hands"] // 3)
    roll = stats.rolling(conn, hero, roll_window)
    sess = stats.sessions(conn, hero)
    n_problems = stats.problem_count(conn)

    positions = ["All"] + [p for p in POSITION_ORDER
                           if stats.range_grid(conn, hero, p)]

    periods = stats.periods(conn, hero)
    regions, grids, hand_ids = {}, {}, []
    meta = []
    for period in periods:
        r, g, ids = _fragments(conn, hero, period, positions, generated, stake_txt)
        regions[period.key] = r
        grids[period.key] = g
        hand_ids.extend(ids)
        band = (None if period.is_all
                else stats.hand_index_range(conn, hero, period.selected))
        meta.append({"key": period.key, "label": period.label,
                     "hands": period.selected.hands, "band": band})

    raws = db.hand_texts(conn, sorted(set(hand_ids)))
    # "all" is left out: it is the page's own markup, and the browser reads it
    # back from the region ids rather than carrying the same fragments twice.
    payload = _json_script(
        {"periods": meta, "ids": list(regions["all"]),
         "regions": {k: v for k, v in regions.items() if k != "all"},
         "raws": {str(k): v for k, v in raws.items()}},
        "period-data")

    period_bar = _period_bar(meta) if len(periods) > 1 else ""

    roll_caveat = ("" if roll_window >= 1000 else
                   " The window is narrowed from the usual 1,000 because the "
                   "sample is smaller than that, so the line is correspondingly "
                   "noisier.")
    # The page stamps the hand count it was rendered from, so the poll can ask
    # "is what I am showing still current" rather than "did my own call import
    # anything". Those are different questions the moment a second poller
    # exists -- another tab, or a `cli refresh` in the terminal -- because
    # whichever refresh runs first does the importing and every other one is
    # told nothing arrived. The loser of that race used to go stale and stay
    # stale. It counts every hand, tournaments included, to match the figure
    # `serve.run_refresh` reports back.
    rendered_hands = conn.execute("SELECT COUNT(*) n FROM hands").fetchone()["n"]
    a = regions["all"]
    tabs = (
        '<div class="tabs" role="tablist">'
        '<button type="button" class="tab" role="tab" data-tab="stats" '
        'aria-selected="true">Stats</button>'
        '<button type="button" class="tab" role="tab" data-tab="solver" '
        'aria-selected="false">Solver</button></div>')

    def page(live: bool) -> str:
        toolbar = (f"""
<div class="toolbar">
  <span id="refresh-status" class="status" data-hands="{rendered_hands}">Checking every 60s</span>
</div>""" if live else "")
        stop = ('<button type="button" id="stop-tracker" class="chip stop">'
                'Stop tracker</button>' if live else "")
        live_js = LIVE_JS + SOLVER_JS if live else ""
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Poker tracker &mdash; {esc(hero)}</title>
<style>{CSS}</style></head>
<body{' class="live"' if live else ''}><div class="wrap">

<div class="head"><h1>{esc(hero)}</h1>{stop}</div>
{tabs}
<div id="tab-stats">
<p class="sub" id="r-sub">{a['r-sub']}</p>
{toolbar}
{period_bar}
<div id="r-tiles">{a['r-tiles']}</div>
<div id="r-banner">{a['r-banner']}</div>

<h2>1. Cumulative winnings</h2>
<p class="note">This single chart is the reason trackers exist; everything else
supports it. The red line is not "profit from bluffing": it is every pot taken down uncontested minus every chip lost in a
hand that never reached showdown. A slightly negative red line is normal at
micro stakes; a sharply negative one usually means you are opening and
c-betting into people who never fold.</p>
<div id="r-winnings">{a['r-winnings']}</div>

<h2>2. bb/100 by position</h2>
<p class="note">Error bars are 95% confidence intervals. They will be
embarrassingly wide, and that is the point &mdash; the aggregate number hides
almost everything interesting, and this is where leaks become visible.</p>
<div id="r-position">{a['r-position']}</div>

<h2>3. Preflop</h2>
<p class="note">Every denominator is opportunities, not hands. A big blind check
is never VPIP; a big blind call of a raise is. Completing the small blind counts
as VPIP but not PFR.</p>
<div id="r-preflop">{a['r-preflop']}</div>

<h2>4. Postflop</h2>
<p class="note">"Won money" means collected any part of the pot, even if the hand
was net negative after your own contribution.</p>
<div id="r-postflop">{a['r-postflop']}</div>

<h2>5. Compliance checks</h2>
<p class="note">These answer whether you executed the plan, which is a different
question from whether the plan is good &mdash; and a much more useful one early
on, because they converge in a weekend rather than a year. Counts carry a rate
per 100 hands beside them: a cumulative tally only goes up, so it stops telling
you anything about the last week once it has a few entries in it.</p>
<div id="r-compliance">{a['r-compliance']}</div>

<h2>6. Starting-hand ranges</h2>
<p class="note">How often each combo was voluntarily played, filtered by
position &mdash; a single aggregated grid is meaningless, because button and
UTG ranges differ by design. With a period selected the grid splits in two, so
range drift shows up directly. The bb/100 view is noisy below 100k hands and
mostly decorative, but it does surface the "I keep losing money with A-rag
offsuit" pattern.</p>
<div class="card">{charts.range_heatmap(grids, positions)}</div>

<h2>7. Rolling preflop discipline</h2>
<p class="note">Trailing {roll_window:,}-hand window against target reference
lines. This is how you verify the discipline is holding rather than decaying
over weeks.{roll_caveat}</p>
<div class="card">{charts.rolling_trend(roll, TARGETS)}</div>

<h2>8. Street funnel</h2>
<p class="note">Where in the hand your money actually moves. The bars and
the reached count are cumulative -- the funnel shape is what they are for --
but the money is not: each row is the net of the hands that <em>ended</em> at
that stage, so no subtraction is needed to read it. The flop row is the one
that appears nowhere else in this report.</p>
<div id="r-funnel">{a['r-funnel']}</div>

<h2>9. Pot-size buckets</h2>
<p class="note">Most micro-stakes losing players are fine in small pots and
hemorrhage in big ones. If the losses concentrate in the top bucket, the problem
is stack-off decisions, not preflop ranges.</p>
<div id="r-buckets">{a['r-buckets']}</div>

<h2>10. Made-hand strength</h2>
<p class="note">The pot-size buckets above say the money leaves in big pots.
This says what you were holding when it left. Losing a stack with an overpair
and losing a stack with no pair are different mistakes and only one of them is
fixed by folding more &mdash; and the row that matters is usually visible
without reading a single hand.</p>
<div id="r-handclass">{a['r-handclass']}</div>

<h2>11. Biggest pots</h2>
<p class="note">Every other section ends in "go look at those hands"; this is
where you look. The largest losses and largest wins by net big blinds, with the
board, the exit street, and whatever the opponent showed. A pattern here &mdash;
stacking off with one pair, folding rivers in the biggest pots, the same
position over and over &mdash; carries more information than any aggregate on
this page, because these are the hands the win rate is actually made of. The
<b>Made</b> column is the bucket from the section above; sort by it to read the
list as a strength ladder rather than as a money ladder.</p>
<div id="r-drilldown">{a['r-drilldown']}</div>

<h2>12. Sessions</h2>
<p class="note">Each point is one session, split on a gap of more than
{db.SESSION_GAP_MINUTES} minutes. The boundaries are assigned once when the
hands are imported and stored on the hand, so "last session" means the same set
of hands however the report is filtered. This list always covers the whole
history.</p>
<div class="card">{charts.session_scatter(sess)}
<details><summary>Table view</summary>
<table><thead><tr><th>Start</th><th>Duration</th><th>Hands</th><th>Tables</th>
<th>bb/100</th></tr></thead><tbody>{_session_rows(sess)}</tbody></table></details></div>

<h2>13. Effective stacks</h2>
<p class="note">A one-off diagnostic. Once the distribution sits at 100bb you can
retire this chart. Bars below 80bb are the ones to worry about.</p>
<div id="r-stacks">{a['r-stacks']}</div>

<h2>14. Table load, timing and attention</h2>
<div id="r-load">{a['r-load']}</div>

<h2>15. Sample size</h2>
<p class="note">Frequency stats converge far faster than results because they
are bounded proportions. This is the argument for building a tracker around
frequencies and compliance checks rather than around win rate &mdash; and the
reason this table recomputes with the filter rather than staying put.</p>
<div id="r-sample">{a['r-sample']}</div>

{_problems_block(conn, n_problems)}

<footer>Generated by pokertracker from PokerStars hand histories.
Rake is attributed to players in proportion to contribution, which is an
estimate: PokerStars reports rake per pot, not per player.{tourney_note}</footer>
</div>
<div id="tab-solver" hidden>
{_solver_panel(live)}
</div>
</div>
{payload}
<script>{JS}{live_js}</script>
</body></html>"""

    return {live: page(live) for live in (False, True)}


def _street_cells(ag, key: str, suffix: str) -> str:
    out = []
    for street in ("flop", "turn", "river"):
        v = ag["per_street"][street][key]
        out.append(f"<td>{'-' if v is None else f'{v:.1f}{suffix}'}</td>")
    return "".join(out)


def _triangle_note(wwsf, wtsd, wsd) -> str:
    """WWSF, WTSD and W$SD read together as a diagnostic triangle."""
    if not (wwsf and wtsd and wwsf["den"]):
        return ""
    a, b = wwsf["pct"], wtsd["pct"]
    c = wsd["pct"] if wsd and wsd["den"] else None
    msg = []
    if a is not None and b is not None:
        if a < 45 and b > 32:
            msg.append("Low WWSF with high WTSD: too passive, calling down and "
                       "giving up the initiative.")
        if b > 32 and c is not None and c < 48:
            msg.append("High WTSD with low W$SD: calling too wide on later streets.")
        if a > 50 and b < 25:
            msg.append("High WWSF with low WTSD: winning uncontested pots, which "
                       "is fine, but check the red line supports it.")
    if not msg:
        msg.append("Nothing stands out yet &mdash; these three need 5,000 to "
                   "10,000 hands before they mean much.")
    return ('<div class="banner"><b>Diagnostic triangle.</b> '
            + " ".join(msg) + "</div>")
