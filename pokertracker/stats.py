"""The stat layer: SQL aggregations over hand_player, results and hands.

Every rate returned here carries its numerator and denominator so the caller
can attach a confidence interval. Displaying a bare percentage with no sample
size is how you end up acting on noise.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

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
# money


def bb_series(conn: sqlite3.Connection, hero: str) -> list[dict]:
    """Per-hand hero results in big blinds, in chronological order.

    Everything on the money side is built from this one query so the four lines
    on the winnings graph cannot drift apart.
    """
    rows = conn.execute(
        """
        SELECT h.hand_id, h.played_at, h.table_name, h.bb,
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
        ORDER BY h.played_at, h.hand_id
        """,
        (hero, hero, hero),
    ).fetchall()
    return [dict(r) for r in rows]


def money_summary(conn: sqlite3.Connection, hero: str) -> dict:
    rows = bb_series(conn, hero)
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


def _rates(conn, hero, defs, where="", params=()) -> list[dict]:
    select = ", ".join(
        f"SUM({num}) AS n_{i}, SUM({den}) AS d_{i}" for i, (_, num, den) in enumerate(defs)
    )
    sql = f"SELECT COUNT(*) AS hands, {select} FROM hand_player WHERE player = ? {where}"
    row = conn.execute(sql, (hero, *params)).fetchone()
    out = []
    for i, (name, _, _) in enumerate(defs):
        out.append(Rate(name, row[f"n_{i}"] or 0, row[f"d_{i}"] or 0).as_dict())
    return out


def preflop_stats(conn, hero, where="", params=()) -> list[dict]:
    return _rates(conn, hero, PREFLOP_DEFS, where, params)


def postflop_stats(conn, hero, where="", params=()) -> list[dict]:
    return _rates(conn, hero, POSTFLOP_DEFS, where, params)


def aggression(conn, hero) -> dict:
    row = conn.execute(
        """SELECT
             SUM(aggr_flop+aggr_turn+aggr_river)     AS aggr,
             SUM(calls_flop+calls_turn+calls_river)  AS calls,
             SUM(folds_flop+folds_turn+folds_river)  AS folds
           FROM hand_player WHERE player = ?""",
        (hero,),
    ).fetchone()
    aggr, calls, folds = row["aggr"] or 0, row["calls"] or 0, row["folds"] or 0
    denom = aggr + calls + folds
    per_street = {}
    for street in ("flop", "turn", "river"):
        r = conn.execute(
            f"""SELECT SUM(aggr_{street}) a, SUM(calls_{street}) c, SUM(folds_{street}) f
                FROM hand_player WHERE player = ?""",
            (hero,),
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


def by_position(conn, hero) -> list[dict]:
    """VPIP / PFR / 3-Bet / bb/100 broken out by position.

    The aggregate numbers hide almost everything interesting; this table is
    where leaks are actually visible.
    """
    rows = conn.execute(
        """
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
        WHERE hp.player = ? AND hp.position <> ''
        GROUP BY hp.position
        """,
        (hero,),
    ).fetchall()

    out = []
    for r in rows:
        n = r["hands"]
        net = r["net_bb"] or 0.0
        per_hand = conn.execute(
            """SELECT r.net * 1.0 / h.bb AS x FROM hand_player hp
               JOIN hands h ON h.hand_id = hp.hand_id
               JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
               WHERE hp.player = ? AND hp.position = ?""",
            (hero, r["position"]),
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


def compliance(conn, hero) -> list[dict]:
    """Did you execute the plan? A different question from whether it is good."""
    row = conn.execute(
        """SELECT COUNT(*) hands,
                  SUM(sb_coldcall) sb_cc,
                  SUM(sb_complete) sb_comp,
                  SUM(btn_open) btn_open,
                  SUM(btn_open_opp) btn_open_opp,
                  SUM(CASE WHEN eff_stack_bb < 80 THEN 1 ELSE 0 END) short,
                  AVG(eff_stack_bb) avg_eff
           FROM hand_player WHERE player = ?""",
        (hero,),
    ).fetchone()
    n = row["hands"] or 0
    checks = [
        {"name": "SB cold calls", "value": row["sb_cc"] or 0, "target": "0",
         "unit": "count", "ok": (row["sb_cc"] or 0) == 0,
         "note": "Any non-zero value is a deviation from a raise-or-fold SB."},
        {"name": "SB completes (limp)", "value": row["sb_comp"] or 0, "target": "0",
         "unit": "count", "ok": (row["sb_comp"] or 0) == 0,
         "note": "Completing the small blind is VPIP but not PFR."},
        {"name": "BTN open when folded to", "value":
            100 * row["btn_open"] / row["btn_open_opp"] if row["btn_open_opp"] else None,
         "target": "~45%", "unit": "pct",
         "ok": None if not row["btn_open_opp"] else
               38 <= 100 * row["btn_open"] / row["btn_open_opp"] <= 52,
         "note": f"{row['btn_open'] or 0}/{row['btn_open_opp'] or 0} opportunities."},
        {"name": "Hands below 80bb effective", "value":
            100 * row["short"] / n if n else None,
         "target": "low", "unit": "pct", "ok": None,
         "note": f"Mean effective stack {row['avg_eff']:.1f}bb."
                 if row["avg_eff"] is not None else ""},
    ]
    return checks


def sessions(conn, hero, gap_minutes: int = 30) -> list[dict]:
    """Split hero's hands into sessions on a gap larger than gap_minutes."""
    rows = bb_series(conn, hero)
    if not rows:
        return []
    from datetime import datetime

    out, cur = [], None
    prev_t = None
    for r in rows:
        t = datetime.fromisoformat(r["played_at"])
        if cur is None or (t - prev_t).total_seconds() > gap_minutes * 60:
            cur = {"start": t, "end": t, "hands": 0, "net_bb": 0.0, "tables": set(),
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
        prev_t = t

    for s in out:
        dur = (s["end"] - s["start"]).total_seconds() / 60
        s["duration_min"] = dur
        s["bb100"] = 100 * s["net_bb"] / s["hands"] if s["hands"] else 0
        s["n_tables"] = len(s["tables"])
        s["tables"] = sorted(s["tables"])
        s["start"] = s["start"].isoformat(" ")
        s["end"] = s["end"].isoformat(" ")
    return out


def concurrency(conn, hero) -> list[dict]:
    """Table count active in each 5-minute window, joined to result in that window.

    Derived from overlapping timestamps across table_name rather than from any
    client-side signal, so it reflects what was actually being played.
    """
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m-%d %H:', h.played_at) ||
               printf('%02d', (CAST(strftime('%M', h.played_at) AS INTEGER) / 5) * 5) AS bucket,
               COUNT(DISTINCT h.table_name) AS tables,
               COUNT(*) AS hands,
               SUM(r.net * 1.0 / h.bb) AS net_bb
        FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
        GROUP BY bucket ORDER BY bucket
        """,
        (hero,),
    ).fetchall()
    return [dict(r) for r in rows]


def by_table_count(conn, hero) -> list[dict]:
    agg: dict[int, list] = {}
    for w in concurrency(conn, hero):
        a = agg.setdefault(w["tables"], [0, 0.0])
        a[0] += w["hands"]
        a[1] += w["net_bb"] or 0.0
    return [{"tables": k, "hands": v[0], "bb100": 100 * v[1] / v[0] if v[0] else None}
            for k, v in sorted(agg.items())]


def timeouts(conn, hero) -> int:
    row = conn.execute("SELECT COUNT(*) n FROM timeouts WHERE player = ?", (hero,)).fetchone()
    return row["n"]


def by_time(conn, hero, fmt: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT strftime('{fmt}', h.played_at) AS k, COUNT(*) hands,
                   SUM(r.net * 1.0 / h.bb) net_bb
            FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
            GROUP BY k ORDER BY k""",
        (hero,),
    ).fetchall()
    return [{"key": r["k"], "hands": r["hands"],
             "bb100": 100 * (r["net_bb"] or 0) / r["hands"] if r["hands"] else None}
            for r in rows]


def stack_histogram(conn, hero, width: int = 10) -> list[dict]:
    rows = conn.execute(
        """SELECT CAST(eff_stack_bb / ? AS INTEGER) * ? AS bucket, COUNT(*) n
           FROM hand_player WHERE player = ? GROUP BY bucket ORDER BY bucket""",
        (width, width, hero),
    ).fetchall()
    return [{"bucket": r["bucket"], "n": r["n"]} for r in rows]


def pot_buckets(conn, hero) -> list[dict]:
    """Net winnings grouped by final pot size in bb.

    Most micro-stakes losing players are fine in small pots and hemorrhage in
    big ones. Losses concentrated in the top bucket mean the problem is
    stack-off decisions, not preflop ranges.
    """
    edges = [(0, 5), (5, 15), (15, 40), (40, 100), (100, 10**9)]
    out = []
    for lo, hi in edges:
        r = conn.execute(
            """SELECT COUNT(*) n, SUM(r.net * 1.0 / h.bb) net
               FROM hands h JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
               WHERE h.total_pot * 1.0 / h.bb >= ? AND h.total_pot * 1.0 / h.bb < ?""",
            (hero, lo, hi),
        ).fetchone()
        label = f"{lo}-{hi}bb" if hi < 10**9 else f"{lo}+bb"
        out.append({"label": label, "hands": r["n"], "net_bb": r["net"] or 0.0,
                    "bb100": 100 * (r["net"] or 0) / r["n"] if r["n"] else None})
    return out


def street_funnel(conn, hero) -> list[dict]:
    row = conn.execute(
        """SELECT COUNT(*) dealt, SUM(saw_flop) f, SUM(saw_turn) t, SUM(saw_river) rv,
                  SUM(wtsd) sd FROM hand_player WHERE player = ?""",
        (hero,),
    ).fetchone()
    money = {}
    for label, cond in [("dealt", "1=1"), ("flop", "hp.saw_flop=1"),
                        ("turn", "hp.saw_turn=1"), ("river", "hp.saw_river=1"),
                        ("showdown", "hp.wtsd=1")]:
        m = conn.execute(
            f"""SELECT SUM(r.net * 1.0 / h.bb) net FROM hand_player hp
                JOIN hands h ON h.hand_id = hp.hand_id
                JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
                WHERE hp.player = ? AND {cond}""",
            (hero,),
        ).fetchone()
        money[label] = m["net"] or 0.0
    return [
        {"stage": "Dealt", "hands": row["dealt"], "net_bb": money["dealt"]},
        {"stage": "Saw flop", "hands": row["f"] or 0, "net_bb": money["flop"]},
        {"stage": "Saw turn", "hands": row["t"] or 0, "net_bb": money["turn"]},
        {"stage": "Saw river", "hands": row["rv"] or 0, "net_bb": money["river"]},
        {"stage": "Showdown", "hands": row["sd"] or 0, "net_bb": money["showdown"]},
    ]


def range_grid(conn, hero, position: str | None = None) -> dict:
    """13x13 grid: how often each combo was voluntarily played, and its bb/100."""
    where = "AND hp.position = ?" if position else ""
    params = [hero] + ([position] if position else [])
    rows = conn.execute(
        f"""SELECT hp.hole_combo AS combo, COUNT(*) n, SUM(hp.vpip) vpip,
                   SUM(r.net * 1.0 / h.bb) net
            FROM hand_player hp
            JOIN hands h ON h.hand_id = hp.hand_id
            JOIN results r ON r.hand_id = hp.hand_id AND r.player = hp.player
            WHERE hp.player = ? AND hp.hole_combo <> '' {where}
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


def big_pots(conn: sqlite3.Connection, hero: str, limit: int = 15) -> dict:
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
               hp.position, hp.hole_combo,
               hp.saw_flop, hp.saw_turn, hp.saw_river,
               s.hole_cards,
               EXISTS (SELECT 1 FROM actions a
                       WHERE a.hand_id = h.hand_id AND a.player = hp.player
                         AND a.action = 'fold') AS folded
        FROM hands h
        JOIN results r      ON r.hand_id = h.hand_id AND r.player = ?
        JOIN hand_player hp ON hp.hand_id = h.hand_id AND hp.player = ?
        LEFT JOIN seats s   ON s.hand_id = h.hand_id AND s.player = ?
        ORDER BY net_bb {dir}, h.hand_id
        LIMIT ?
    """
    out = {}
    for key, direction in (("losses", "ASC"), ("wins", "DESC")):
        rows = conn.execute(sql.format(dir=direction),
                            (hero, hero, hero, limit)).fetchall()
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
        "board": board,
        "pot_bb": r["pot_bb"],
        "net_bb": r["net_bb"],
        "exit_street": _exit_street(r),
        "outcome": _outcome(r),
        "shown": [{"player": s["player"], "cards": s["hole_cards"]} for s in shown],
    }
