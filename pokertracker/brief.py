"""A machine-readable snapshot of everything the dashboard shows.

`report.py` renders for a person and `cli stats` renders for a terminal. This
renders for a reader that would rather have the numbers than the layout -- a
coaching session held against the database instead of against a screenshot of
it.

**This module derives nothing**, the same discipline `parser.py` holds. Every
figure here is a dict some function in `stats.py` already returns, serialized
and nothing more. A review that wants a number nobody computes yet gets it
added to `stats.py`, so the report and the brief cannot drift into disagreeing
about the same quantity -- which, given that one of them is what a person reads
while the other is what advice gets built on, would be the worst possible
place for a disagreement to hide.

The second half of the file is the metric namespace: a stable way to *name* one
of those numbers so a finding recorded today can be re-measured next month.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import handclass, stats


class UnknownMetric(ValueError):
    """A metric id that does not name anything.

    Raised rather than resolved to None: a typo in a tracked finding must fail
    loudly at the moment it is written, not quietly read as "no data yet" every
    time the journal is re-measured.
    """


# --------------------------------------------------------------------------
# the metric namespace
#
# A finding says "this number is wrong". For that to still mean something after
# a few thousand more hands, the number needs a name that does not move.
# Display labels move -- they are prose, and prose gets reworded -- so ids are
# built from the flag columns in `hand_player`, which cannot be reworded
# without a schema change and a rebuild.
#
#   stat:<flag>              a preflop or postflop rate, by its numerator flag
#   check:<key>              a compliance check, by its slug
#   class:<bucket>[@lo-hi]   a made-hand bucket, optionally over a pot range
#   money:bb100              the win rate

_RANGE_SEP = "@"


def _stat_index() -> dict[str, tuple[str, object, str]]:
    """flag -> (display label, the stats function, the denominator expr)."""
    out = {}
    for defs, fn in ((stats.PREFLOP_DEFS, stats.preflop_stats),
                     (stats.POSTFLOP_DEFS, stats.postflop_stats)):
        for label, num, den in defs:
            out[num] = (label, fn, den)
    return out


STAT_IDS = tuple(f"stat:{flag}" for flag in _stat_index())
CHECK_IDS = ("check:sb-coldcall", "check:sb-complete", "check:btn-open",
             "check:naked-continues", "check:short-stacks")
MONEY_IDS = ("money:bb100",)


def metric_ids() -> list[str]:
    """Every id that resolves, for error messages and for the tests."""
    return [*STAT_IDS, *CHECK_IDS,
            *(f"class:{c}" for c in handclass.CLASSES), *MONEY_IDS]


def parse_pot_range(text: str) -> tuple[float, float | None]:
    """"15-40" -> (15.0, 40.0); "40-" -> (40.0, None); "" -> (0.0, None).

    Exclusive at the top, so ranges tile the way `hand_classes()` and
    `pot_buckets()` do and a pot at exactly 40bb is counted once.
    """
    if not text:
        return (0.0, None)
    lo, _, hi = text.partition("-")
    try:
        return (float(lo) if lo else 0.0, float(hi) if hi else None)
    except ValueError:
        raise UnknownMetric(f"bad pot range {text!r}; expected e.g. 15-40 or 40-")


def resolve(conn: sqlite3.Connection, hero: str, metric: str,
            window: stats.Window | None = None) -> dict:
    """Measure one named metric over one window.

    Returns a shape every kind of metric can fill: a numerator, a denominator,
    a value and -- where the quantity is a proportion -- its interval. The
    denominator travels with the value because a review that acts on a rate
    without looking at its sample size is the failure mode this whole project
    is arranged against.
    """
    kind, _, rest = metric.partition(":")
    if not rest:
        raise UnknownMetric(f"{metric!r} is not a metric id; expected kind:name")

    if kind == "stat":
        index = _stat_index()
        if rest not in index:
            raise UnknownMetric(
                f"unknown stat {rest!r}; one of {', '.join(sorted(index))}")
        label, fn, _den = index[rest]
        row = next(r for r in fn(conn, hero, window) if r["name"] == label)
        return {"metric": metric, "label": label, "kind": "rate", "unit": "%",
                "num": row["num"], "den": row["den"], "value": row["pct"],
                "ci_lo": row["ci_lo"], "ci_hi": row["ci_hi"]}

    if kind == "check":
        rows = {c["key"]: c for c in stats.compliance(conn, hero, window)}
        if rest not in rows:
            raise UnknownMetric(
                f"unknown check {rest!r}; one of {', '.join(sorted(rows))}")
        c = rows[rest]
        rate = stats.Rate(c["name"], c["num"], c["den"])
        ci = rate.ci if c["kind"] == "rate" else None
        return {"metric": metric, "label": c["name"], "kind": c["kind"],
                "unit": "%" if c["kind"] == "rate" else "hands",
                "num": c["num"], "den": c["den"], "value": c["value"],
                "ci_lo": ci[0] if ci else None, "ci_hi": ci[1] if ci else None,
                "status": c["status"], "target_text": c["target"],
                "note": c["note"]}

    if kind == "class":
        bucket, _, rng = rest.partition(_RANGE_SEP)
        if bucket not in handclass.CLASSES:
            raise UnknownMetric(
                f"unknown hand class {bucket!r}; one of "
                f"{', '.join(handclass.CLASSES)}")
        lo, hi = parse_pot_range(rng)
        rows = stats.hand_classes(conn, hero, lo, window, hi)
        r = next(x for x in rows if x["class"] == bucket)
        # The value is bb per hand rather than the total: a bucket's total
        # grows with every hand played and would show "improvement" purely by
        # sitting out. Per hand is the figure a correction can move.
        return {"metric": metric, "label": r["label"], "kind": "money",
                "unit": "bb/hand", "num": r["net_bb"], "den": r["hands"],
                "value": r["bb_hand"], "ci_lo": None, "ci_hi": None,
                "invested_bb_hand": r["bb_in_hand"]}

    if metric == "money:bb100":
        m = stats.money_summary(conn, hero, window)
        n = m.get("hands", 0)
        return {"metric": metric, "label": "Win rate", "kind": "money",
                "unit": "bb/100", "num": m.get("net_bb", 0.0), "den": n,
                "value": m.get("bb100") if n else None,
                "ci_lo": None, "ci_hi": None,
                "ci95": m.get("ci95") if n else None}

    raise UnknownMetric(
        f"unknown metric {metric!r}; one of {', '.join(metric_ids())}")


# --------------------------------------------------------------------------
# the brief


# The three pot ranges the made-hand section reads side by side, and the reason
# it reads three: over every flopped pot the no-pair row is mostly small pots
# folded away, over the big pots it is the row that costs a stack, and the
# middle range is the only thing that says which of those the leak actually is.
def _pot_ranges() -> list[tuple[str, str, float, float | None]]:
    return [
        ("all", "every flopped pot", 0.0, None),
        ("mid", f"{stats.MID_POT_BB:.0f}-{stats.BIG_POT_BB:.0f}bb pots",
         stats.MID_POT_BB, stats.BIG_POT_BB),
        ("big", f"pots over {stats.BIG_POT_BB:.0f}bb", stats.BIG_POT_BB, None),
    ]


def _window_dict(w: stats.Window) -> dict:
    # The label and the size, never the SQL: `dataclasses.asdict` would drag
    # the predicate and its parameters along, which say nothing to a reader and
    # would turn an implementation detail into part of the published shape.
    return {"label": w.label, "hands": w.hands}


def _sessions(conn, hero, window) -> list[dict]:
    """The session list, minus the one field that exists only to be drawn.

    `buckets` is a per-session split of hands and money by how many tables
    were open at the time -- material for a stacked bar, and half the weight
    of the whole brief. The aggregate it supports is already here as
    `by_table_count`, computed over the window rather than guessed at by
    summing fifty little dictionaries.
    """
    return [{k: v for k, v in s.items() if k != "buckets"}
            for s in stats.sessions(conn, hero, window)]


def _made_hands(conn, hero, window) -> list[dict]:
    out = []
    for key, label, lo, hi in _pot_ranges():
        rows = stats.hand_classes(conn, hero, lo, window, hi)
        split = stats.class_street_split(conn, hero, handclass.NO_PAIR,
                                         lo, window, hi)
        out.append({
            "range": key,
            "label": label,
            "min_pot_bb": lo,
            "max_pot_bb": hi,
            "classes": rows,
            # The naked half of the no-pair split, broken out by street:
            # "it was paid for" and "it was paid for on the river" call for
            # opposite fixes, and a single invested figure cannot tell them
            # apart. Preflop is in there as context, not as a finding.
            "naked_by_street": split,
            "dominant_street": stats.dominant_street(split),
            "worst_class": stats.worst_class(rows),
        })
    return out


def build(conn: sqlite3.Connection, hero: str, period_key: str = "all",
          grid: bool = False, now: datetime | None = None,
          tracked: list[dict] | None = None) -> dict:
    """Everything the report shows for one period, as plain data.

    `tracked` is the review journal's verdicts, passed in rather than read from
    disk so this module stays a pure function of the database.
    """
    available = stats.periods(conn, hero, now=now)
    period = next((p for p in available if p.key == period_key), None)
    if period is None:
        raise ValueError(
            f"unknown period {period_key!r}; available: "
            f"{', '.join(p.key for p in available)}")
    w = period.selected
    prior = None if period.is_all else period.prior

    m = stats.money_summary(conn, hero, w)
    out: dict = {
        "generated": (now or datetime.now()).isoformat(timespec="seconds"),
        "hero": hero,
        "period": {"key": period.key, "label": period.label,
                   "selected": _window_dict(w),
                   "prior": None if prior is None else _window_dict(prior)},
        "periods_available": [{"key": p.key, "label": p.label,
                               "hands": p.selected.hands} for p in available],
        # Stated rather than silent, so the hand count here can be reconciled
        # against the history folder.
        "tournaments_excluded": stats.excluded_tournaments(conn, hero),
        "money": m,
        "parse_problems": stats.problem_count(conn),
        "reliability": stats.reliability(m.get("hands", 0)),
    }
    if not m.get("hands"):
        return out

    out.update({
        "preflop": stats.preflop_stats(conn, hero, w),
        "postflop": stats.postflop_stats(conn, hero, w),
        "aggression": stats.aggression(conn, hero, w),
        "by_position": stats.by_position(conn, hero, w),
        "compliance": stats.compliance(conn, hero, w),
        "street_funnel": stats.street_funnel(conn, hero, w),
        "pot_buckets": stats.pot_buckets(conn, hero, w),
        "made_hands": _made_hands(conn, hero, w),
        "big_pots": stats.big_pots(conn, hero, 15, w),
        "sessions": _sessions(conn, hero, w),
        "timeouts": stats.timeouts(conn, hero, w),
        "by_table_count": stats.by_table_count(conn, hero, w),
        "by_hour": stats.by_time(conn, hero, "%H", w),
        "by_weekday": stats.by_time(conn, hero, "%w", w),
        "stack_histogram": stats.stack_histogram(conn, hero, 10, w),
    })
    # The 169-cell preflop grid is the one section big enough to be worth not
    # sending by default: a review asks for it when it is about opening ranges
    # and otherwise pays for it in every read.
    if grid:
        out["range_grid"] = stats.range_grid(conn, hero, None, w)

    # The prior period, for the same reason the report draws deltas: a rate is
    # only "off" against something. Only the figures a delta is drawn on -- not
    # the whole brief a second time.
    if prior is not None and not prior.empty:
        out["prior"] = {
            "window": _window_dict(prior),
            "money": stats.money_summary(conn, hero, prior),
            "preflop": stats.preflop_stats(conn, hero, prior),
            "postflop": stats.postflop_stats(conn, hero, prior),
            "compliance": stats.compliance(conn, hero, prior),
        }

    if tracked is not None:
        out["tracked"] = tracked

    # The thresholds every judgement in here should be made against, carried
    # with the data so a reader does not have to invent one. MIN_OPPS in
    # particular: below it the honest answer is that the window cannot say.
    out["constants"] = {
        "min_opps": stats.MIN_OPPS,
        "min_class_hands": stats.MIN_CLASS_HANDS,
        "mid_pot_bb": stats.MID_POT_BB,
        "big_pot_bb": stats.BIG_POT_BB,
        "draw_outs": handclass.DRAW_OUTS,
        "street_majority": stats.STREET_MAJORITY,
        "hands_for_winrate": dict(stats.RELIABILITY)["bb/100"],
    }
    return out
