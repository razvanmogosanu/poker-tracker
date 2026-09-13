"""The stat layer: SQL aggregations over hand_player, results and hands.

Every rate returned here carries its numerator and denominator so the caller
can attach a confidence interval. Displaying a bare percentage with no sample
size is how you end up acting on noise.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from . import handclass
from .positions import POSITION_ORDER, sort_key

# Standard deviation for 6-max NLHE is roughly 85-100 bb/100. Used only as a
# fallback when there are too few hands to estimate it from the data.
DEFAULT_SD_BB100 = 90.0


@dataclass
class Rate:
    name: str
    num: float
    den: float

    @property
    def pct(self) -> float | None:
        return 100.0 * self.num / self.den if self.den else None

    @property
    def ci(self) -> tuple[float, float] | None:
        """Wilson score interval, which behaves sensibly at small n and at 0%."""
        if not self.den:
            return None
        z, n, p = 1.96, float(self.den), self.num / self.den
        d = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / d
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
        return (100 * max(0.0, centre - half), 100 * min(1.0, centre + half))

    def as_dict(self) -> dict:
        ci = self.ci
        return {"name": self.name, "num": self.num, "den": self.den, "pct": self.pct,
                "ci_lo": ci[0] if ci else None, "ci_hi": ci[1] if ci else None}


def _hero(conn: sqlite3.Connection, hero: str | None) -> str:
    if hero:
        return hero
    row = conn.execute(
        "SELECT player, COUNT(*) n FROM hand_player WHERE is_hero = 1"
        " GROUP BY player ORDER BY n DESC LIMIT 1"
    ).fetchone()
    return row["player"] if row else ""


# --------------------------------------------------------------------------
# periods
#
# A period filter is one predicate applied at the top of the pipeline, not a
# change to any individual stat: every aggregation below already keys off a set
# of hands, so narrowing that set is all a filter has to do.

# Below this many opportunities a rate is noise and a difference between two
# rates is the difference between two pieces of noise. Rates are still shown --
# with their (now very wide) interval -- but deltas against the prior period
# are suppressed rather than invited to be read.
MIN_OPPS = 30

# Fixed hand counts offered by the selector. A free-text N cannot work in a
# report that has to open from file:// with no server behind it.
LAST_N_HANDS = (500, 1000)


@dataclass(frozen=True)
class Window:
    """A slice of the history, as a SQL predicate over the `hands` alias `h`.

    Windows are half-open on the recent side: everything at or after a boundary
    hand, or everything strictly before it. Ordering is by (played_at, hand_id)
    rather than played_at alone because multi-tabling puts several hands on the
    same second, and a tie broken arbitrarily would let a hand fall into both
    the selected window and the prior one.
    """

    label: str
    sql: str
    params: tuple
    hands: int

    @property
    def empty(self) -> bool:
        return self.hands == 0


# Tournament hands are parsed and stored -- the histories are the source of
# truth and the parser has to keep up with their line formats -- but they are
# not cash hands and every figure in this report is a cash figure. Chip EV,
# stack depths and open ranges all mean something different when there is a
# bubble, so nine hands of a sit-and-go would otherwise sit inside the same
# bb/100 as several thousand hands of $0.01/$0.02. The exclusion rides in the
# same funnel as the period filter, so no individual stat has to know about it.
CASH_ONLY = "h.is_tournament = 0"


def _win(window: Window | None) -> tuple[str, tuple]:
    """The WHERE fragment for a window: cash hands, narrowed to the period."""
    if window is None:
        return (CASH_ONLY, ())
    return (f"{CASH_ONLY} AND ({window.sql})", window.params)


@dataclass(frozen=True)
class Period:
    """A selectable window plus everything that came before it."""

    key: str
    label: str
    selected: Window
    prior: Window

    @property
    def is_all(self) -> bool:
        return self.key == "all"


def _since(boundary) -> tuple[str, tuple]:
    return ("(h.played_at, h.hand_id) >= (?, ?)", boundary)


def _before(boundary) -> tuple[str, tuple]:
    return ("(h.played_at, h.hand_id) < (?, ?)", boundary)


def periods(conn: sqlite3.Connection, hero: str,
            last_n: tuple[int, ...] = LAST_N_HANDS,
            now: "datetime | None" = None) -> list[Period]:
    """The period options to offer, given what is actually in the database.

    An option is dropped when it is empty, when it covers the whole history
    (in which case it is just "All" under another name), and when it starts at
    the same hand as an option already offered -- for a player with one evening
    of hands, "last session", "today" and "last 7 days" are the same
    set, and offering three buttons that do the same thing is worse than one.
    """
    rows = conn.execute(
        """SELECT h.hand_id, h.played_at, h.session_id
           FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
           WHERE {CASH_ONLY}
           ORDER BY h.played_at, h.hand_id""".format(CASH_ONLY=CASH_ONLY),
        (hero,),
    ).fetchall()
    total = len(rows)
    all_window = Window("All hands", "1=1", (), total)
    empty = Window("Nothing before", "0=1", (), 0)
    out = [Period("all", "All", all_window, empty)]
    if total < 2:
        return out

    from datetime import datetime, timedelta

    last_t = datetime.fromisoformat(rows[-1]["played_at"])
    last_session = rows[-1]["session_id"]

    def index_from(pred) -> int | None:
        """First index satisfying pred, scanning back from the end."""
        i = total
        while i > 0 and pred(rows[i - 1]):
            i -= 1
        return i if i < total else None

    # "Today" is the calendar day on the clock, and it is the only option here
    # anchored on the clock rather than on the last hand. Every other one
    # describes a position in the history -- "last session" and "last 500
    # hands" stay true of the same hands however long ago they were played --
    # but "today" is a claim about the date, and anchoring it on the last hand
    # would hang the label on yesterday's play, or last month's, whenever a
    # day was skipped. Having it disappear on a day with no hands is the
    # honest answer rather than a gap to paper over: an empty period is
    # dropped, and a day you did not play is a day with nothing to show.
    #
    # `played_at` is the local timestamp from the history (`played_at_et`
    # holds the site's), so it compares against the local clock as it stands.
    #
    # It is also the one option whose boundary jumps rather than slides --
    # midnight moves it by a whole evening -- which is why the report's reload
    # restore checks its band, as it does for "Last session".
    today = (now or datetime.now()).date()
    candidates = [
        ("session", "Last session",
         index_from(lambda r: r["session_id"] == last_session)),
        ("today", "Today",
         index_from(lambda r: datetime.fromisoformat(r["played_at"]).date()
                    == today)),
        ("week", "Last 7 days",
         index_from(lambda r: datetime.fromisoformat(r["played_at"])
                    >= last_t - timedelta(days=7))),
    ]
    for n in last_n:
        candidates.append((f"n{n}", f"Last {n:,} hands",
                           total - n if total > n else None))

    seen = set()
    for key, label, i in candidates:
        if i is None or i <= 0 or i in seen:
            continue
        seen.add(i)
        boundary = (rows[i]["played_at"], rows[i]["hand_id"])
        sel_sql, sel_p = _since(boundary)
        pri_sql, pri_p = _before(boundary)
        out.append(Period(
            key, label,
            Window(label, sel_sql, sel_p, total - i),
            Window("Everything before", pri_sql, pri_p, i),
        ))
    return out


def excluded_tournaments(conn: sqlite3.Connection, hero: str) -> dict:
    """Hero's tournament hands, which no other figure in this module counts.

    Reported rather than silently dropped: a reader who played a sit-and-go
    last night should be told why those hands are missing instead of counting
    the report's hands and finding it short.
    """
    row = conn.execute(
        """SELECT COUNT(*) hands, COUNT(DISTINCT h.tournament_id) events
           FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
           WHERE h.is_tournament = 1""",
        (hero,),
    ).fetchone()
    return {"hands": row["hands"], "events": row["events"]}


def hand_index_range(conn: sqlite3.Connection, hero: str,
                     window: Window | None) -> tuple[int, int] | None:
    """Where a window sits in the hero's chronological hand sequence.

    The cumulative charts are drawn once over the whole history and the period
    is shaded on top of them, so what they need is not the window's rows but
    its position: a period seen in isolation loses the context that makes the
    chart worth looking at.
    """
    sql, params = ("1=1", ()) if window is None else (window.sql, window.params)
    row = conn.execute(
        f"""SELECT COUNT(*) n,
                   SUM(CASE WHEN {sql} THEN 1 ELSE 0 END) sel,
                   SUM(CASE WHEN {sql} THEN 0 ELSE 1 END) before
            FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
            WHERE {CASH_ONLY}""",
        (*params, *params, hero),
    ).fetchone()
    if not row["n"] or not row["sel"]:
        return None
    return (row["before"] + 1, row["n"])


# --------------------------------------------------------------------------
# money


def bb_series(conn: sqlite3.Connection, hero: str,
              window: Window | None = None) -> list[dict]:
    """Per-hand hero results in big blinds, in chronological order.

    Everything on the money side is built from this one query so the four lines
    on the winnings graph cannot drift apart.
    """
    wsql, wp = _win(window)
    rows = conn.execute(
        f"""
        SELECT h.hand_id, h.played_at, h.table_name, h.bb, h.session_id,
               r.net * 1.0 / h.bb              AS net_bb,
               r.rake_share * 1.0 / h.bb       AS rake_bb,
               r.reached_showdown              AS sd,
               h.total_pot * 1.0 / h.bb        AS pot_bb,
               hp.saw_flop, hp.saw_turn, hp.saw_river, hp.position,
               hp.hole_combo, hp.eff_stack_bb, hp.wwsf, hp.wtsd, hp.wsd,
               COALESCE(e.ev_net * 1.0 / h.bb, r.net * 1.0 / h.bb) AS ev_bb,
               (e.ev_net IS NOT NULL)          AS has_ev
        FROM hands h
        JOIN results r     ON r.hand_id = h.hand_id AND r.player = ?
        JOIN hand_player hp ON hp.hand_id = h.hand_id AND hp.player = ?
        LEFT JOIN allin_ev e ON e.hand_id = h.hand_id AND e.player = ?
        WHERE {wsql}
        ORDER BY h.played_at, h.hand_id
        """,
        (hero, hero, hero, *wp),
    ).fetchall()
    return [dict(r) for r in rows]


def money_summary(conn: sqlite3.Connection, hero: str,
                  window: Window | None = None) -> dict:
    rows = bb_series(conn, hero, window)
    n = len(rows)
    if not n:
        return {"hands": 0}

    net = [r["net_bb"] for r in rows]
    total = sum(net)
    showdown = sum(r["net_bb"] for r in rows if r["sd"])
    nonshowdown = total - showdown
    rake = sum(r["rake_bb"] for r in rows)
    ev = sum(r["ev_bb"] for r in rows)

    mean = total / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in net) / (n - 1)
        sd_per_hand = math.sqrt(var)
        sd_bb100 = 10 * sd_per_hand
    else:
        sd_bb100 = DEFAULT_SD_BB100
    # SD/sqrt(hands/100) expressed in bb/100.
    se = sd_bb100 / math.sqrt(n / 100) if n else float("inf")

    return {
        "hands": n,
        "net_bb": total,
        "bb100": 100 * total / n,
        "ci95": 1.96 * se,
        "sd_bb100": sd_bb100,
        "showdown_bb100": 100 * showdown / n,
        "nonshowdown_bb100": 100 * nonshowdown / n,
        "rake_bb100": 100 * rake / n,
        "gross_bb100": 100 * (total + rake) / n,
        "ev_bb100": 100 * ev / n,
        "ev_diff_bb100": 100 * (ev - total) / n,
        "has_ev": any(r["has_ev"] for r in rows),
        "first_hand": rows[0]["played_at"],
        "last_hand": rows[-1]["played_at"],
    }


# --------------------------------------------------------------------------
# preflop / postflop

PREFLOP_DEFS = [
    ("VPIP", "vpip", "1"),
    ("PFR", "pfr", "1"),
    ("3-Bet", "threebet", "threebet_opp"),
    ("Fold to 3-Bet", "fold_to_3bet", "faced_3bet"),
    ("4-Bet", "fourbet", "faced_3bet"),
    ("Squeeze", "squeeze", "squeeze_opp"),
    ("Cold Call", "coldcall", "coldcall_opp"),
    ("ATS", "ats", "ats_opp"),
    ("Fold BB vs Steal", "fold_bb_vs_steal", "bb_vs_steal_opp"),
    ("3-Bet vs Steal", "threebet_vs_steal", "bb_vs_steal_opp"),
]

POSTFLOP_DEFS = [
    ("Flop C-Bet", "cbet_flop", "cbet_flop_opp"),
    ("Turn C-Bet", "cbet_turn", "cbet_turn_opp"),
    ("River C-Bet", "cbet_river", "cbet_river_opp"),
    ("Fold to Flop C-Bet", "fold_to_cbet_flop", "faced_cbet_flop"),
    ("Fold to Turn Bet", "fold_to_bet_turn", "faced_bet_turn"),
    ("Fold to River Bet", "fold_to_bet_river", "faced_bet_river"),
    ("Check-Raise Flop", "cr_flop", "cr_opp_flop"),
    ("Check-Raise Turn", "cr_turn", "cr_opp_turn"),
    ("Donk Flop", "donk_flop", "donk_opp_flop"),
    ("WWSF", "wwsf", "saw_flop"),
    ("WTSD", "wtsd", "saw_flop"),
    ("W$SD", "wsd", "wtsd"),
]


def _rates(conn, hero, defs, window: Window | None = None) -> list[dict]:
    select = ", ".join(
        f"SUM({num}) AS n_{i}, SUM({den}) AS d_{i}" for i, (_, num, den) in enumerate(defs)
    )
    wsql, wp = _win(window)
    sql = (f"SELECT COUNT(*) AS hands, {select} FROM hand_player hp"
           f" JOIN hands h ON h.hand_id = hp.hand_id"
           f" WHERE hp.player = ? AND {wsql}")
    row = conn.execute(sql, (hero, *wp)).fetchone()
    out = []
    for i, (name, _, _) in enumerate(defs):
        out.append(Rate(name, row[f"n_{i}"] or 0, row[f"d_{i}"] or 0).as_dict())
    return out


def preflop_stats(conn, hero, window: Window | None = None) -> list[dict]:
    return _rates(conn, hero, PREFLOP_DEFS, window)


def postflop_stats(conn, hero, window: Window | None = None) -> list[dict]:
    return _rates(conn, hero, POSTFLOP_DEFS, window)


def aggression(conn, hero, window: Window | None = None) -> dict:
    wsql, wp = _win(window)
    row = conn.execute(
        f"""SELECT
             SUM(aggr_flop+aggr_turn+aggr_river)     AS aggr,
             SUM(calls_flop+calls_turn+calls_river)  AS calls,
             SUM(folds_flop+folds_turn+folds_river)  AS folds
           FROM hand_player hp JOIN hands h ON h.hand_id = hp.hand_id
           WHERE hp.player = ? AND {wsql}""",
        (hero, *wp),
    ).fetchone()
    aggr, calls, folds = row["aggr"] or 0, row["calls"] or 0, row["folds"] or 0
    denom = aggr + calls + folds
    per_street = {}
    for street in ("flop", "turn", "river"):
        r = conn.execute(
            f"""SELECT SUM(aggr_{street}) a, SUM(calls_{street}) c, SUM(folds_{street}) f
                FROM hand_player hp JOIN hands h ON h.hand_id = hp.hand_id
                WHERE hp.player = ? AND {wsql}""",
            (hero, *wp),
        ).fetchone()
        a, c, f = r["a"] or 0, r["c"] or 0, r["f"] or 0
        per_street[street] = {
            "afq": 100 * a / (a + c + f) if (a + c + f) else None,
            "af": a / c if c else None,
        }
    return {
        "afq": 100 * aggr / denom if denom else None,
        "af": aggr / calls if calls else None,
        "per_street": per_street,
        "counts": {"aggr": aggr, "calls": calls, "folds": folds},
    }


def by_position(conn, hero, window: Window | None = None) -> list[dict]:
    """VPIP / PFR / 3-Bet / bb/100 broken out by position.

    The aggregate numbers hide almost everything interesting; this table is
    where leaks are actually visible.
    """
    wsql, wp = _win(window)
    rows = conn.execute(
        f"""
        SELECT hp.position,
               COUNT(*)                              AS hands,
               SUM(hp.vpip)                          AS vpip,
               SUM(hp.pfr)                           AS pfr,
               SUM(hp.threebet)                      AS tb,
               SUM(hp.threebet_opp)                  AS tb_opp,
               SUM(r.net * 1.0 / h.bb)               AS net_bb
        FROM hand_player hp
        JOIN hands h   ON h.hand_id = hp.hand_id
        JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
        WHERE hp.player = ? AND hp.position <> '' AND {wsql}
        GROUP BY hp.position
        """,
        (hero, *wp),
    ).fetchall()

    out = []
    for r in rows:
        n = r["hands"]
        net = r["net_bb"] or 0.0
        per_hand = conn.execute(
            f"""SELECT r.net * 1.0 / h.bb AS x FROM hand_player hp
               JOIN hands h ON h.hand_id = hp.hand_id
               JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
               WHERE hp.player = ? AND hp.position = ? AND {wsql}""",
            (hero, r["position"], *wp),
        ).fetchall()
        xs = [p["x"] for p in per_hand]
        mean = sum(xs) / n if n else 0
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else DEFAULT_SD_BB100 / 10
        se_bb100 = 100 * sd / math.sqrt(n) if n else float("inf")
        out.append({
            "position": r["position"],
            "hands": n,
            "vpip": 100 * r["vpip"] / n if n else None,
            "pfr": 100 * r["pfr"] / n if n else None,
            "threebet": 100 * r["tb"] / r["tb_opp"] if r["tb_opp"] else None,
            "threebet_opp": r["tb_opp"],
            "bb100": 100 * net / n if n else None,
            "ci95": 1.96 * se_bb100,
        })
    out.sort(key=lambda d: sort_key(d["position"]))
    return out


# --------------------------------------------------------------------------
# compliance and behaviour


def compliance(conn, hero, window: Window | None = None) -> list[dict]:
    """Did you execute the plan? A different question from whether it is good.

    Every check reports a numerator and a denominator, never a bare tally. A
    cumulative counter only goes up: once it says 11 it will say 11 forever no
    matter how the last thousand hands were played, so it stops carrying
    information about the thing the reader is actually trying to change. The
    denominator is what makes "5 in this session" comparable with "6 before".

    `status` is deliberately four-valued. "thin" is not a soft failure -- it
    says the window does not contain enough opportunities for the check to have
    an opinion, which is the honest answer for 0 opens out of 2 and is not the
    same statement as "off plan".
    """
    wsql, wp = _win(window)
    row = conn.execute(
        f"""SELECT COUNT(*) hands,
                  SUM(hp.sb_coldcall) sb_cc,
                  SUM(hp.sb_complete) sb_comp,
                  SUM(hp.btn_open) btn_open,
                  SUM(hp.btn_open_opp) btn_open_opp,
                  SUM(CASE WHEN hp.eff_stack_bb < 80 THEN 1 ELSE 0 END) short,
                  AVG(hp.eff_stack_bb) avg_eff
           FROM hand_player hp JOIN hands h ON h.hand_id = hp.hand_id
           WHERE hp.player = ? AND {wsql}""",
        (hero, *wp),
    ).fetchone()
    n = row["hands"] or 0

    def count_check(name, num, target, note):
        # A count check has an opinion at any n: one small-blind cold call is
        # one deviation whether it happened in 80 hands or 8,000. What n
        # changes is only how much a zero is worth, which the rate per 100
        # carries instead of a pill.
        num = num or 0
        return {"name": name, "kind": "count", "num": num, "den": n,
                "value": float(num),
                "per100": 100.0 * num / n if n else None,
                "target": target,
                "status": "thin" if not n else ("ok" if num == 0 else "off"),
                "note": note}

    def rate_check(name, num, den, target, lo, hi, note):
        num, den = num or 0, den or 0
        pct = 100.0 * num / den if den else None
        if den < MIN_OPPS:
            status = "thin"
        elif lo is None:
            status = "watch"
        else:
            status = "ok" if lo <= pct <= hi else "off"
        return {"name": name, "kind": "rate", "num": num, "den": den,
                "value": pct, "per100": None, "target": target,
                "status": status, "note": note}

    # The 8-outs rule, which until the no-pair bucket was split had no
    # denominator anywhere and so could not be checked at all. The denominator
    # is the hands where money actually went in postflop with no pair: a naked
    # ace-high that was checked down cost nothing and broke no rule, and
    # counting it would dilute the only number here that matters. There is no
    # band, so the status is "watch" -- the rule says naked continues should be
    # rare, not that they should be zero, and inventing a percentage for "rare"
    # would dress a guess up as a standard. The note carries the money instead,
    # which is the part that argues.
    draw = conn.execute(
        f"""SELECT hp.hand_class k, COUNT(*) n, SUM(r.net * 1.0 / h.bb) net
           FROM hand_player hp
           JOIN hands h   ON h.hand_id = hp.hand_id
           JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
           WHERE hp.player = ? AND hp.postflop_invested > 0
             AND hp.hand_class IN (?, ?) AND {wsql}
           GROUP BY hp.hand_class""",
        (hero, handclass.NO_PAIR, handclass.NO_PAIR_DRAW, *wp),
    ).fetchall()
    by_k = {r["k"]: r for r in draw}
    naked = by_k.get(handclass.NO_PAIR)
    drawing = by_k.get(handclass.NO_PAIR_DRAW)
    naked_n = naked["n"] if naked else 0
    draw_n = drawing["n"] if drawing else 0
    naked_note = (
        f"{naked_n} of them cost {naked['net'] or 0.0:+,.0f} bb; the "
        f"{draw_n} with {handclass.DRAW_OUTS}+ outs, "
        f"{(drawing['net'] if drawing else 0.0) or 0.0:+,.0f} bb."
        if naked_n or draw_n else
        "No postflop money has gone in with no pair yet."
    )

    return [
        count_check("SB cold calls", row["sb_cc"], "0",
                    "Any non-zero value is a deviation from a raise-or-fold SB."),
        count_check("SB completes (limp)", row["sb_comp"], "0",
                    "Completing the small blind is VPIP but not PFR."),
        rate_check("BTN open when folded to", row["btn_open"], row["btn_open_opp"],
                   "~45%", 38, 52, "Opens divided by opportunities on the button."),
        rate_check(f"Postflop money in below {handclass.DRAW_OUTS} outs",
                   naked_n, naked_n + draw_n, "low", None, None, naked_note),
        rate_check("Hands below 80bb effective", row["short"], n, "low", None, None,
                   f"Mean effective stack {row['avg_eff']:.1f}bb."
                   if row["avg_eff"] is not None else ""),
    ]


def sessions(conn, hero, window: Window | None = None) -> list[dict]:
    """Hero's hands grouped into the sessions assigned at ingest.

    The boundaries are read from `hands.session_id` rather than recomputed from
    a gap threshold here. Recomputing would give the same answer today and a
    different one the moment a filter changed what this function could see,
    which is exactly the drift the stored column exists to prevent -- and it
    would let this table disagree with the "last session" period filter.
    """
    rows = bb_series(conn, hero, window)
    if not rows:
        return []
    from datetime import datetime

    out, cur, sid = [], None, None
    for r in rows:
        t = datetime.fromisoformat(r["played_at"])
        if cur is None or r["session_id"] != sid:
            sid = r["session_id"]
            cur = {"session_id": sid, "start": t, "end": t, "hands": 0,
                   "net_bb": 0.0, "tables": set(),
                   "buckets": {0: [0, 0.0], 1: [0, 0.0], 2: [0, 0.0]}}
            out.append(cur)
        cur["end"] = t
        cur["hands"] += 1
        cur["net_bb"] += r["net_bb"]
        cur["tables"].add(r["table_name"])
        elapsed = (t - cur["start"]).total_seconds() / 60
        b = 0 if elapsed < 30 else (1 if elapsed < 60 else 2)
        cur["buckets"][b][0] += 1
        cur["buckets"][b][1] += r["net_bb"]

    for sess in out:
        dur = (sess["end"] - sess["start"]).total_seconds() / 60
        sess["duration_min"] = dur
        sess["bb100"] = 100 * sess["net_bb"] / sess["hands"] if sess["hands"] else 0
        sess["n_tables"] = len(sess["tables"])
        sess["tables"] = sorted(sess["tables"])
        sess["start"] = sess["start"].isoformat(" ")
        sess["end"] = sess["end"].isoformat(" ")
    return out


def concurrency(conn, hero, window: Window | None = None) -> list[dict]:
    """Table count active in each 5-minute window, joined to result in that window.

    Derived from overlapping timestamps across table_name rather than from any
    client-side signal, so it reflects what was actually being played.
    """
    wsql, wp = _win(window)
    rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m-%d %H:', h.played_at) ||
               printf('%02d', (CAST(strftime('%M', h.played_at) AS INTEGER) / 5) * 5) AS bucket,
               COUNT(DISTINCT h.table_name) AS tables,
               COUNT(*) AS hands,
               SUM(r.net * 1.0 / h.bb) AS net_bb
        FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
        WHERE {wsql}
        GROUP BY bucket ORDER BY bucket
        """,
        (hero, *wp),
    ).fetchall()
    return [dict(r) for r in rows]


def by_table_count(conn, hero, window: Window | None = None) -> list[dict]:
    agg: dict[int, list] = {}
    for w in concurrency(conn, hero, window):
        a = agg.setdefault(w["tables"], [0, 0.0])
        a[0] += w["hands"]
        a[1] += w["net_bb"] or 0.0
    return [{"tables": k, "hands": v[0], "bb100": 100 * v[1] / v[0] if v[0] else None}
            for k, v in sorted(agg.items())]


def timeouts(conn, hero, window: Window | None = None) -> int:
    wsql, wp = _win(window)
    row = conn.execute(
        f"""SELECT COUNT(*) n FROM timeouts t
            JOIN hands h ON h.hand_id = t.hand_id
            WHERE t.player = ? AND {wsql}""",
        (hero, *wp),
    ).fetchone()
    return row["n"]


def by_time(conn, hero, fmt: str, window: Window | None = None) -> list[dict]:
    wsql, wp = _win(window)
    rows = conn.execute(
        f"""SELECT strftime('{fmt}', h.played_at) AS k, COUNT(*) hands,
                   SUM(r.net * 1.0 / h.bb) net_bb
            FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
            WHERE {wsql}
            GROUP BY k ORDER BY k""",
        (hero, *wp),
    ).fetchall()
    return [{"key": r["k"], "hands": r["hands"],
             "bb100": 100 * (r["net_bb"] or 0) / r["hands"] if r["hands"] else None}
            for r in rows]


def stack_histogram(conn, hero, width: int = 10,
                    window: Window | None = None) -> list[dict]:
    wsql, wp = _win(window)
    rows = conn.execute(
        f"""SELECT CAST(hp.eff_stack_bb / ? AS INTEGER) * ? AS bucket, COUNT(*) n
           FROM hand_player hp JOIN hands h ON h.hand_id = hp.hand_id
           WHERE hp.player = ? AND {wsql} GROUP BY bucket ORDER BY bucket""",
        (width, width, hero, *wp),
    ).fetchall()
    return [{"bucket": r["bucket"], "n": r["n"]} for r in rows]


def pot_buckets(conn, hero, window: Window | None = None) -> list[dict]:
    """Net winnings grouped by final pot size in bb.

    Most micro-stakes losing players are fine in small pots and hemorrhage in
    big ones. Losses concentrated in the top bucket mean the problem is
    stack-off decisions, not preflop ranges.
    """
    wsql, wp = _win(window)
    edges = [(0, 5), (5, 15), (15, 40), (40, 100), (100, 10**9)]
    out = []
    for lo, hi in edges:
        r = conn.execute(
            f"""SELECT COUNT(*) n, SUM(r.net * 1.0 / h.bb) net
               FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
               WHERE h.total_pot * 1.0 / h.bb >= ? AND h.total_pot * 1.0 / h.bb < ?
                 AND {wsql}""",
            (hero, lo, hi, *wp),
        ).fetchone()
        label = f"{lo}-{hi}bb" if hi < 10**9 else f"{lo}+bb"
        out.append({"label": label, "hands": r["n"], "net_bb": r["net"] or 0.0,
                    "bb100": 100 * (r["net"] or 0) / r["n"] if r["n"] else None})
    return out


# The pot size above which a made-hand bucket is worth reading on its own.
# Below it the buckets are dominated by hands that cost a big blind and tell
# you nothing; at 40bb and up every row is a stack-off decision. It matches the
# fourth edge in pot_buckets() deliberately, so the two sections can be read
# against each other.
BIG_POT_BB = 40.0

# The floor of the middle range, and the third edge in pot_buckets() for the
# same reason. 40bb is a high bar -- on a small history only a handful of pots
# clear it -- so the big-pot column on its own cannot say whether a losing
# bucket bleeds through the medium pots too or genuinely stops at the small
# ones. The 15-40bb range is where that question is answered: the pots are big
# enough that the hands were played rather than folded to a continuation bet,
# and numerous enough to carry a count.
MID_POT_BB = 15.0


def hand_classes(conn, hero, min_pot_bb: float = 0.0,
                 window: Window | None = None,
                 max_pot_bb: float | None = None) -> list[dict]:
    """Net winnings grouped by what the hero's hand actually was.

    The aggregate that says "you are losing in big pots" does not say what you
    were holding in them, and that is the part a review needs: three stacks lost
    with no pair is a different leak from three stacks lost with an overpair,
    and only one of them is fixed by folding more.

    Buckets are derived at ingest (see `derive._classify_rows`) from the board
    as of the street the hero left the hand on, so this is a plain grouping.
    Every bucket is returned even when empty, in strength order, so the shape of
    the table does not change with the filter.
    """
    wsql, wp = _win(window)
    # An open-ended range keeps the upper bound out of the way rather than
    # branching the SQL; the comparison is exclusive at the top so the ranges
    # tile the way pot_buckets() does and a pot at exactly 40bb is counted once.
    hi = float("inf") if max_pot_bb is None else max_pot_bb
    rows = {r["k"]: r for r in conn.execute(
        f"""SELECT hp.hand_class k, COUNT(*) n,
                   SUM(r.net * 1.0 / h.bb) net,
                   SUM(r.contributed * 1.0 / h.bb) invested,
                   SUM(hp.wtsd) showdowns
            FROM hand_player hp
            JOIN hands h   ON h.hand_id = hp.hand_id
            JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
            WHERE hp.player = ? AND hp.hand_class IS NOT NULL
              AND h.total_pot * 1.0 / h.bb >= ?
              AND h.total_pot * 1.0 / h.bb < ? AND {wsql}
            GROUP BY hp.hand_class""",
        (hero, min_pot_bb, hi, *wp),
    )}
    out = []
    for name in handclass.CLASSES:
        r = rows.get(name)
        n = r["n"] if r else 0
        net = (r["net"] or 0.0) if r else 0.0
        # What the bucket cost says nothing about how it was lost. A no-pair
        # hand at -9 bb is a flop float given up on the turn or a river call
        # that should never have happened, and those are opposite mistakes:
        # the first one is folding too late, the second is not folding at all.
        # Invested bb per hand separates them, so it travels beside the net
        # everywhere the net is shown.
        invested = (r["invested"] or 0.0) if r else 0.0
        out.append({
            "class": name,
            "label": handclass.LONG_NAME[name],
            "hands": n,
            "net_bb": net,
            "bb_hand": net / n if n else None,
            "invested_bb": invested,
            "bb_in_hand": invested / n if n else None,
            "showdowns": (r["showdowns"] or 0) if r else 0,
        })
    return out


# The streets a row of the split is reported on. Preflop is one of them even
# though no decision in this section is about it: the made-hand table's `bb in`
# is total contribution, so without a preflop row the four figures would not
# add back to the number the reader is looking at and the column would read as
# an error. It is the context, and the other three are the finding.
INVESTED_STREETS = ("preflop", "flop", "turn", "river")
POSTFLOP_STREETS = INVESTED_STREETS[1:]


def class_street_split(conn, hero, hand_class: str, min_pot_bb: float = 0.0,
                       window: Window | None = None,
                       max_pot_bb: float | None = None) -> list[dict]:
    """One bucket's invested bb, split by the street the money went in on.

    `bb in` says a bucket was paid for rather than folded away. It does not say
    *when* it was paid for, and the two answers call for opposite fixes: money
    on the river is a call made after the hand is already over, so the fix is
    folding; money on the turn is a second barrel into somebody who was never
    going to fold, so the fix is not betting. A single mean cannot tell those
    apart and reads as one undifferentiated leak.

    The denominator is every hand in the bucket and range, including the ones
    that put in nothing after the flop, so the rows sum back to the `bb in` on
    that row of the made-hand table rather than to something slightly larger.
    Preflop is what `results.contributed` has that the street columns do not,
    which is why it is a subtraction rather than a fourth stored column.
    """
    wsql, wp = _win(window)
    hi = float("inf") if max_pot_bb is None else max_pot_bb
    row = conn.execute(
        f"""SELECT COUNT(*) n,
                   SUM(r.contributed     * 1.0 / h.bb) total,
                   SUM(hp.invested_flop  * 1.0 / h.bb) flop,
                   SUM(hp.invested_turn  * 1.0 / h.bb) turn,
                   SUM(hp.invested_river * 1.0 / h.bb) river
            FROM hand_player hp
            JOIN hands h   ON h.hand_id = hp.hand_id
            JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
            WHERE hp.player = ? AND hp.hand_class = ?
              AND h.total_pot * 1.0 / h.bb >= ?
              AND h.total_pot * 1.0 / h.bb < ? AND {wsql}""",
        (hero, hand_class, min_pot_bb, hi, *wp),
    ).fetchone()

    n = row["n"] or 0
    bb = {k: (row[k] or 0.0) for k in POSTFLOP_STREETS}
    total = row["total"] or 0.0
    bb["preflop"] = total - sum(bb.values())
    return [{
        "street": k,
        "hands": n,
        "bb_in": bb[k],
        "bb_in_hand": bb[k] / n if n else None,
        # Of everything the bucket put in, so the shares and the per-hand
        # figures answer to the same total the made-hand table shows.
        "share": bb[k] / total if total else None,
    } for k in INVESTED_STREETS]


# A street holding more than half the postflop money is a fact the split
# states about itself, not a threshold anybody chose. Below that the money is
# spread, and the honest reading is that it is spread, so this says nothing
# rather than crowning a plurality.
STREET_MAJORITY = 0.5


def dominant_street(rows) -> dict | None:
    """The postflop street holding an outright majority, or None.

    Preflop is excluded from the question as well as from the denominator: it
    is not a street anybody is deciding about here, and in a bucket that was
    mostly folded on the flop it would win every time and say nothing.
    """
    post = [r for r in rows if r["street"] in POSTFLOP_STREETS]
    total = sum(r["bb_in"] for r in post)
    if total <= 0:
        return None
    top = max(post, key=lambda r: r["bb_in"])
    return top if top["bb_in"] / total > STREET_MAJORITY else None


# How many hands a bucket needs before "this is where the money goes" is a
# claim rather than an anecdote. Deliberately far below MIN_OPPS: big pots are
# rare by construction -- a whole history of 14,000 hands produced 16 of them
# reached with no pair -- so a threshold of 30 would silence the section
# permanently. Five is enough that one cooler cannot be the headline, and the
# money figure itself is stated either way, because realized losses are a fact
# and not an estimate of anything.
MIN_CLASS_HANDS = 5


def worst_class(rows) -> dict | None:
    """The bucket that cost the most, or None when nothing lost money.

    This is the whole section compressed into one row, because a seven-row
    table is still something the reader has to scan and the point of the
    section is that one line of it should have been unmissable on day one.
    """
    losers = [r for r in rows if r["hands"] and r["net_bb"] < 0]
    return min(losers, key=lambda r: r["net_bb"]) if losers else None


def street_funnel(conn, hero, window: Window | None = None) -> list[dict]:
    """The funnel bars are cumulative; the money attached to them is not.

    A cumulative net -- "the 457 hands that saw a flop are worth +479.8 bb" --
    includes everything that happened on later streets, so the only way to read
    it is to subtract adjacent rows in your head, and nobody does. Each row
    therefore also carries the hands that *ended* at that stage and what they
    were worth, which is the figure that says something new: hands that die on
    the flop are their own bucket and their own leak.
    """
    wsql, wp = _win(window)
    row = conn.execute(
        f"""SELECT COUNT(*) dealt, SUM(hp.saw_flop) f, SUM(hp.saw_turn) t,
                   SUM(hp.saw_river) rv, SUM(hp.wtsd) sd
            FROM hand_player hp JOIN hands h ON h.hand_id = hp.hand_id
            WHERE hp.player = ? AND {wsql}""",
        (hero, *wp),
    ).fetchone()
    # Exit stage: the last street the hand reached for us. Showdown is an
    # outcome, not a sixth street -- reaching the river and folding to a river
    # bet is an exit on the river. The five buckets partition the dealt hands,
    # so the exit money sums back to the overall net.
    exits = {r["exit_at"]: r for r in conn.execute(
        f"""SELECT CASE WHEN hp.wtsd = 1 THEN 'showdown'
                        WHEN hp.saw_river = 1 THEN 'river'
                        WHEN hp.saw_turn = 1 THEN 'turn'
                        WHEN hp.saw_flop = 1 THEN 'flop'
                        ELSE 'preflop' END AS exit_at,
                   COUNT(*) n, SUM(r.net * 1.0 / h.bb) net
            FROM hand_player hp
            JOIN hands h ON h.hand_id = hp.hand_id
            JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
            WHERE hp.player = ? AND {wsql}
            GROUP BY exit_at""",
        (hero, *wp),
    ).fetchall()}

    def bucket(key, stage, hands, exit_label):
        e = exits.get(key)
        n = e["n"] if e else 0
        net = ((e["net"] if e else 0.0) or 0.0)
        return {"stage": stage, "hands": hands or 0, "exit_label": exit_label,
                "exit_hands": n, "exit_bb": net,
                "exit_bb_hand": net / n if n else None}

    return [
        bucket("preflop", "Dealt", row["dealt"], "ends preflop"),
        bucket("flop", "Saw flop", row["f"], "ends on flop"),
        bucket("turn", "Saw turn", row["t"], "ends on turn"),
        bucket("river", "Saw river", row["rv"], "ends on river"),
        bucket("showdown", "Showdown", row["sd"], "reached showdown"),
    ]


def range_grid(conn, hero, position: str | None = None,
               window: Window | None = None) -> dict:
    """13x13 grid: how often each combo was voluntarily played, and its bb/100."""
    wsql, wp = _win(window)
    where = "AND hp.position = ?" if position else ""
    params = [hero] + ([position] if position else []) + list(wp)
    rows = conn.execute(
        f"""SELECT hp.hole_combo AS combo, COUNT(*) n, SUM(hp.vpip) vpip,
                   SUM(r.net * 1.0 / h.bb) net
            FROM hand_player hp
            JOIN hands h ON h.hand_id = hp.hand_id
            JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
            WHERE hp.player = ? AND hp.hole_combo <> '' {where} AND {wsql}
            GROUP BY hp.hole_combo""",
        params,
    ).fetchall()
    return {r["combo"]: {"n": r["n"], "vpip": r["vpip"],
                         "freq": 100 * r["vpip"] / r["n"] if r["n"] else None,
                         "bb100": 100 * (r["net"] or 0) / r["n"] if r["n"] else None}
            for r in rows}


def rolling(conn, hero, window: int = 1000) -> list[dict]:
    """VPIP / PFR / 3-Bet over a trailing window, to verify discipline holds."""
    rows = conn.execute(
        """SELECT hp.vpip, hp.pfr, hp.threebet, hp.threebet_opp
           FROM hand_player hp JOIN hands h ON h.hand_id = hp.hand_id
           WHERE hp.player = ? ORDER BY h.played_at, h.hand_id""",
        (hero,),
    ).fetchall()
    n = len(rows)
    if n == 0:
        return []
    step = max(1, n // 400)
    out = []
    for end in range(min(window, n), n + 1, step):
        start = max(0, end - window)
        chunk = rows[start:end]
        m = len(chunk)
        tb_opp = sum(r["threebet_opp"] for r in chunk)
        out.append({
            "hand": end,
            "vpip": 100 * sum(r["vpip"] for r in chunk) / m,
            "pfr": 100 * sum(r["pfr"] for r in chunk) / m,
            "threebet": 100 * sum(r["threebet"] for r in chunk) / tb_opp if tb_opp else None,
        })
    return out


def problems(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT site_hand_no, problem FROM problems ORDER BY rowid LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def problem_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) n FROM problems").fetchone()["n"]


RELIABILITY = [
    ("VPIP, PFR", 2000),
    ("3-Bet, ATS, flop C-Bet", 5000),
    ("Fold to 3-Bet, WWSF, WTSD", 10000),
    ("Turn/river stats, W$SD", 15000),
    ("bb/100", 100000),
]


def reliability(hands: int) -> list[dict]:
    return [{"stat": name, "needed": n, "have": hands,
             "ready": hands >= n, "pct": min(100.0, 100.0 * hands / n)}
            for name, n in RELIABILITY]


# --------------------------------------------------------------------------
# hand-level drill-down


def _exit_street(row) -> str:
    """The last street on which the hero was still in the hand.

    `saw_river` already means "the river was dealt and the hero had not folded
    before it", so the ladder reads straight off the flags. Showdown is a
    separate outcome rather than a fifth street: reaching the river and folding
    to a river bet is an exit on the river, not at showdown.
    """
    if row["reached_showdown"]:
        return "showdown"
    if row["saw_river"]:
        return "river"
    if row["saw_turn"]:
        return "turn"
    if row["saw_flop"]:
        return "flop"
    return "preflop"


def _outcome(row) -> str:
    """won / lost at showdown / folded / opponent folded.

    Order matters: a player who folds can still be at showdown in no sense, but
    a player who was all-in and cashed out neither folded nor showed down, so
    that case is named rather than swallowed into "no showdown".
    """
    if row["reached_showdown"]:
        return "won at showdown" if row["won_pot"] else "lost at showdown"
    if row["folded"]:
        return "folded"
    if row["cashed_out"]:
        return "cashed out"
    if row["won_pot"]:
        return "opponent folded"
    return "no showdown"


def big_pots(conn: sqlite3.Connection, hero: str, limit: int = 15,
             window: Window | None = None) -> dict:
    """The biggest winning and losing hands, with everything needed to review them.

    Every aggregate in this report ends in "go look at those hands"; this is the
    list. Rows are ordered by net bb and the two directions are pulled with one
    query each so a hand can never appear in both.
    """
    sql = """
        SELECT h.hand_id, h.site_hand_no, h.played_at, h.table_name, h.bb,
               h.board_flop, h.board_turn, h.board_river,
               h.total_pot * 1.0 / h.bb  AS pot_bb,
               r.net * 1.0 / h.bb        AS net_bb,
               r.net                     AS net_cents,
               r.reached_showdown, r.cashed_out, r.won_pot,
               hp.position, hp.hole_combo, hp.hand_class,
               hp.saw_flop, hp.saw_turn, hp.saw_river,
               s.hole_cards,
               EXISTS (SELECT 1 FROM actions a
                       WHERE a.hand_id = h.hand_id AND a.player = hp.player
                         AND a.action = 'fold') AS folded
        FROM hands h
        JOIN results r      ON r.hand_id = h.hand_id AND r.player = ?
        JOIN hand_player hp ON hp.hand_id = h.hand_id AND hp.player = ?
        LEFT JOIN seats s   ON s.hand_id = h.hand_id AND s.player = ?
        WHERE {where}
        ORDER BY net_bb {dir}, h.hand_id
        LIMIT ?
    """
    wsql, wp = _win(window)
    out = {}
    for key, direction in (("losses", "ASC"), ("wins", "DESC")):
        rows = conn.execute(sql.format(dir=direction, where=wsql),
                            (hero, hero, hero, *wp, limit)).fetchall()
        out[key] = [_drilldown_row(conn, r, hero) for r in rows
                    if (r["net_bb"] < 0 if key == "losses" else r["net_bb"] > 0)]
    return out


def _drilldown_row(conn, r, hero: str) -> dict:
    # The board is blank when the hero folded preflop: cards that arrived after
    # a fold are not part of the decision being reviewed.
    board = ""
    if r["saw_flop"]:
        board = " ".join(x for x in (r["board_flop"], r["board_turn"],
                                     r["board_river"]) if x)
    shown = conn.execute(
        """SELECT player, hole_cards FROM seats
           WHERE hand_id = ? AND player <> ? AND hole_cards <> ''
           ORDER BY seat_no""",
        (r["hand_id"], hero),
    ).fetchall()
    return {
        "hand_id": r["hand_id"],
        "site_hand_no": r["site_hand_no"],
        "played_at": r["played_at"],
        "table_name": r["table_name"] or "",
        "position": r["position"] or "",
        "hole_cards": r["hole_cards"] or "",
        "hole_combo": r["hole_combo"] or "",
        "hand_class": r["hand_class"] or "",
        "board": board,
        "pot_bb": r["pot_bb"],
        "net_bb": r["net_bb"],
        "exit_street": _exit_street(r),
        "outcome": _outcome(r),
        "shown": [{"player": s["player"], "cards": s["hole_cards"]} for s in shown],
    }
