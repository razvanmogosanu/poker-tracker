"""The Solver tab's arithmetic: one hand against a range, and what a bet is worth.

This is not a solver. It finds no strategy and knows nothing about what an
opponent *should* do; it takes a range somebody chose, enumerates the boards,
and does the arithmetic a player does at the table with exact numbers in place
of guessed ones. The tab carries the name because that is what the question is
called, and every result carries `CAVEAT` because that is what the answer is.

Three things are computed, each from the one before:

    equity          hero's share of the pot against every holding in the range
    the range       those holdings sorted into the made-hand buckets of
                    `handclass`, on this board, with hero's equity against each
    the bet         EV of calling, or of betting into the buckets that continue

**The fold frequency is derived, never typed in.** "They fold 60% of the time"
is the number everybody wants to enter and nobody knows. Which hands continue
is something a player can actually have an opinion about -- top pair calls,
ace-high does not -- and once those are ticked the fold frequency is a count of
holdings and the equity when called is the equity against exactly those. The
two move together, as they do at the table, which a typed percentage would
never make them do.

Amounts here are big blinds, as floats. That is not the money path: nothing in
this module is a record of chips that moved, and `from_hand` converts the
ledger's integer cents only at the edge, on the way out.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from . import equity, handclass, ranges
from .cards import cards_to_str, parse_cards

CAVEAT = ("Equity and EV against the range you chose, assuming no further "
          "betting. This is arithmetic on your assumptions, not solver output.")

MODES = ("facing_bet", "betting")
STREET_OF = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}

# What continues against a bet until the reader says otherwise: everything but
# the naked no-pair hands. It is a starting point and it is echoed back in the
# result, so the assumption is on the screen beside the number it produced.
DEFAULT_CONTINUES = tuple(c for c in handclass.CLASSES if c != handclass.NO_PAIR)


def _amount(spec: dict, key: str) -> float:
    try:
        value = float(spec.get(key) or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{key} is not a number: {spec.get(key)!r}") from None
    if value < 0 or value != value or value == float("inf"):
        raise ValueError(f"{key} must be zero or more")
    return value


def analyse(spec: dict) -> dict:
    """Equity, the range by bucket, and the EV of the bet in question.

    `spec` keys: `hero` ('As Kd'), `board` ('' or three to five cards),
    `range` (text, see `ranges.parse`), `pot` and `bet` in big blinds, `mode`,
    and optionally `continues`, the buckets that do not fold to a bet.

    `pot` is the pot hero is looking at when they act. Facing a bet it already
    includes that bet and `bet` is what it costs to call; betting, `bet` is
    what hero puts in.
    """
    hero = parse_cards(str(spec.get("hero") or ""))
    board = parse_cards(str(spec.get("board") or ""))
    if len(hero) != 2:
        raise ValueError("pick two hole cards")
    if len(board) not in STREET_OF:
        raise ValueError("a board is empty, or three to five cards")
    if len(set(hero + board)) != len(hero) + len(board):
        raise ValueError("a card appears twice")
    mode = spec.get("mode") or "facing_bet"
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    pot, bet = _amount(spec, "pot"), _amount(spec, "bet")

    cells = ranges.parse(str(spec.get("range") or ""))
    if not cells:
        raise ValueError("the range is empty")
    combos = ranges.combos(cells, hero + board)
    if not len(combos):
        raise ValueError("the range is empty once your cards and the board "
                         "are removed")

    eq = equity.hand_vs_range(hero, board, combos)
    out = {
        "hero": cards_to_str(hero),
        "board": cards_to_str(board),
        "street": STREET_OF[len(board)],
        "range": ranges.to_text(cells),
        "cells": sorted(cells, key=ranges.CELLS.index),
        "combos": int(len(combos)),
        "equity": eq["equity"],
        "win": eq["win"],
        "tie": eq["tie"],
        "method": eq["method"],
        "se": eq["se"],
        "evaluated": eq["evaluated"],
        "hero_class": handclass.classify(hero, board),
        "mode": mode,
        "pot": pot,
        "bet": bet,
        "caveat": CAVEAT,
    }

    # Buckets need a board: before the flop nobody has a made hand to sort.
    labels = (handclass.classify_many([(tuple(c), board) for c in combos.tolist()])
              if board else None)
    asked = spec.get("continues")
    continues = set(DEFAULT_CONTINUES if asked is None else asked)
    unknown = continues - set(handclass.CLASSES)
    if unknown:
        raise ValueError(f"unknown bucket {sorted(unknown)[0]!r}")

    per = eq["per_combo"]
    out["buckets"] = []
    if labels is not None:
        got = np.array(labels)
        for name in handclass.CLASSES:
            mask = got == name
            n = int(mask.sum())
            out["buckets"].append({
                "bucket": name,
                "label": handclass.LONG_NAME[name],
                "combos": n,
                "share": n / len(combos),
                "equity": float(per[mask].mean()) if n else None,
                "continues": name in continues,
            })

    if mode == "facing_bet":
        out["facing"] = _facing(eq["equity"], pot, bet)
    else:
        staying = (np.isin(np.array(labels), sorted(continues))
                   if labels is not None else None)
        out["betting"] = _betting(eq["equity"], per, staying, pot, bet)
    return out


def _facing(eq: float, pot: float, bet: float) -> dict:
    """Calling `bet` to win `pot`, which already holds the bet being faced."""
    return {
        # The share of the final pot the call has to win to break even.
        "required_equity": bet / (pot + bet) if pot + bet > 0 else 0.0,
        "ev_call": eq * pot - (1 - eq) * bet,
        "ev_fold": 0.0,
    }


def _betting(eq: float, per, staying, pot: float, bet: float) -> dict:
    """Betting `bet` into `pot` against the holdings that do not fold.

    `staying` is a mask over the range, or None before the flop, where there
    are no buckets to tick and so no fold frequency that would not be invented;
    the bet is then reported as called by the whole range, and says so.
    """
    if staying is None:
        fold, called_eq, n_stay = 0.0, eq, len(per)
    else:
        n_stay = int(staying.sum())
        fold = 1 - n_stay / len(per)
        called_eq = float(per[staying].mean()) if n_stay else None
    when_called = (called_eq * (pot + 2 * bet) - bet) if n_stay else 0.0
    return {
        "fold_freq": fold,
        "fold_derived": staying is not None,
        "continue_combos": n_stay,
        "equity_when_called": called_eq,
        "ev_bet": fold * pot + (1 - fold) * when_called,
        # Checked down against the whole range: the alternative the bet has to
        # beat, under the same no-further-betting assumption as everything else.
        "ev_check": eq * pot,
        # How often a bet with no equity at all has to work.
        "breakeven_fold": bet / (pot + bet) if pot + bet > 0 else 0.0,
    }


# ---- a scenario from a real hand

_STREETS = ("preflop", "flop", "turn", "river")
_OPEN_PRESET = {"UTG": "utg", "UTG+1": "utg", "LJ": "utg", "HJ": "hj",
                "CO": "co", "BTN": "btn", "SB": "sb"}


def _suggest(position: str, actions, villain: str):
    """Which chart range fits what the opponent did preflop, if any does.

    The charts are the hero's own plan. Reading an opponent through them is an
    assumption, so the reason comes back with the key and the tab shows both.
    Anything the charts do not describe -- a limp, a 4-bet, a blind that just
    checked -- gets no suggestion rather than the nearest one.
    """
    level, role = 0, None
    for a in actions:
        if a["street"] != "preflop" or a["is_blind_post"]:
            continue
        if a["action"] == "raise":
            level += 1
            if a["player"] == villain:
                role = ("open", "3bet", "more")[min(level, 3) - 1]
        elif a["action"] == "call" and a["player"] == villain and role is None:
            role = "call" if level == 1 else ("limp" if level == 0 else "more")
    where = "bb" if position == "BB" else "ip"
    if role == "open" and position in _OPEN_PRESET:
        return _OPEN_PRESET[position], f"they opened from {position}"
    if role == "3bet":
        return f"{where}_3bet", f"they 3-bet from {position}"
    if role == "call":
        return f"{where}_call", f"they called a raise from {position}"
    return None, ""


def from_hand(conn: sqlite3.Connection, hero: str, hand_id: int,
              street: str = "") -> dict:
    """A real hand as a scenario: the spot the hero was in on `street`.

    The spot is the hero's *last* action on that street -- a check followed by
    a call is a decision about the call -- and the pot is everything that had
    gone in before it. With no `street`, the last street the hero acted on.

    The range is only suggested when exactly one opponent is left. Three
    players' ranges are not one range, and filling the grid with one of them
    would put a number on the screen that answers a question nobody asked.
    """
    h = conn.execute("SELECT * FROM hands WHERE hand_id = ?", (hand_id,)).fetchone()
    if h is None:
        raise ValueError(f"no hand {hand_id}")
    seats = {s["player"]: s for s in conn.execute(
        "SELECT * FROM seats WHERE hand_id = ?", (hand_id,))}
    if hero not in seats or not seats[hero]["hole_cards"]:
        raise ValueError("the hero's cards are not known for this hand")
    actions = conn.execute(
        "SELECT * FROM actions WHERE hand_id = ? ORDER BY seq", (hand_id,)).fetchall()

    acted = [s for s in _STREETS if any(
        a["player"] == hero and a["street"] == s and not a["is_blind_post"]
        and a["action"] != "returns" for a in actions)]
    if not acted:
        raise ValueError("the hero made no decision in this hand")
    street = street or acted[-1]
    if street not in acted:
        raise ValueError(f"the hero did not act on the {street}")

    spot = [a for a in actions if a["player"] == hero and a["street"] == street
            and not a["is_blind_post"] and a["action"] != "returns"][-1]
    before = [a for a in actions if a["seq"] < spot["seq"]]
    here = [a for a in before if a["street"] == street]
    pot = sum(a["amount"] for a in before)
    level = max((a["to_amount"] for a in here), default=0)
    mine = max((a["to_amount"] for a in here if a["player"] == hero), default=0)
    to_call = max(0, level - mine)

    bb = h["bb"]
    if spot["action"] in ("bet", "raise"):
        mode, bet = "betting", spot["amount"]
    elif to_call:
        mode, bet = "facing_bet", to_call
    else:
        mode, bet = "betting", 0        # a check: no bet yet, the tab can try one

    folded = {a["player"] for a in before if a["action"] == "fold"}
    live = [p for p, s in seats.items()
            if s["is_dealt_in"] and p != hero and p not in folded]
    board = " ".join(x for x in (h["board_flop"], h["board_turn"], h["board_river"])
                     if x).split()
    n_board = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}[street]

    out = {
        "hand_id": hand_id,
        "site_hand_no": h["site_hand_no"],
        "hero": seats[hero]["hole_cards"],
        "hero_position": seats[hero]["position"] or "",
        "board": " ".join(board[:n_board]),
        "street": street,
        "streets": acted,
        "mode": mode,
        "pot": round(pot / bb, 2),
        "bet": round(bet / bb, 2),
        "did": spot["action"],
        "opponents": [{"player": p, "position": seats[p]["position"] or "",
                       "shown": seats[p]["hole_cards"] or ""} for p in live],
        "preset": None,
        "preset_why": "",
    }
    if len(live) == 1:
        key, why = _suggest(seats[live[0]]["position"] or "", actions, live[0])
        out["preset"], out["preset_why"] = key, why
        if key:
            out["range"] = ranges.to_text(ranges.preset(key))
    return out
