"""Replay stored actions into one flag row per (hand, player).

Everything here is derived from the `actions` table, never from text, so it can
be rebuilt in seconds after a definition changes -- which is the whole point of
keeping the parser dumb.

The reason this is a Python replay rather than SQL is that the denominators are
sequential. "Faced exactly one raise with the option to act" is a statement
about the action sequence at a specific moment, not about the final pot state:
you cannot fold to a 3-bet you never faced because a fourth player folded
behind first. Replaying the sequence makes that explicit and testable.

Conventions committed to here (write them down once, or you will re-derive them
wrong in six months):

* "Won money" means COLLECTED any part of the pot, even if the hand was net
  negative after your own contribution. Used by WWSF and W$SD.
* The preflop aggressor (PFA) is the LAST player to raise preflop. A limped pot
  has no PFA and therefore no c-bet opportunity for anyone.
* Bet level: 0 = unopened (blinds only), 1 = open raise, 2 = 3-bet, 3 = 4-bet.
  Blind posts never raise the level; the first voluntary raise is the open,
  even if there were limpers before it.
* An opportunity is counted at most once per hand per player.
* `hand_class` is the made-hand bucket as of the street the player LEFT the
  hand on, not as of the river. See `_classify_rows`.
* `postflop_invested` is cents added on the flop, turn and river only. Every
  blind and ante is a preflop action, so it needs no exclusion of its own.
  `invested_flop` / `_turn` / `_river` are the same money by street and sum
  back to it.
"""

from __future__ import annotations

import sqlite3

from . import handclass
from .cards import parse_cards

STREETS_AFTER_PREFLOP = ("flop", "turn", "river")
STEAL_POSITIONS = ("CO", "BTN", "SB")

DDL = """
DROP TABLE IF EXISTS hand_player;
CREATE TABLE hand_player (
    hand_id INTEGER NOT NULL,
    player  TEXT NOT NULL,
    position TEXT,
    is_hero INTEGER NOT NULL DEFAULT 0,
    hole_combo TEXT,
    starting_stack INTEGER,
    eff_stack_bb REAL,
    -- made-hand bucket at the moment this player left the hand; NULL when
    -- there is nothing to classify (folded preflop, or cards never shown)
    hand_class TEXT,

    vpip INTEGER DEFAULT 0,
    pfr INTEGER DEFAULT 0,
    rfi_opp INTEGER DEFAULT 0,
    open_raise INTEGER DEFAULT 0,
    threebet_opp INTEGER DEFAULT 0,
    threebet INTEGER DEFAULT 0,
    faced_3bet INTEGER DEFAULT 0,
    fold_to_3bet INTEGER DEFAULT 0,
    fourbet INTEGER DEFAULT 0,
    squeeze_opp INTEGER DEFAULT 0,
    squeeze INTEGER DEFAULT 0,
    coldcall_opp INTEGER DEFAULT 0,
    coldcall INTEGER DEFAULT 0,
    ats_opp INTEGER DEFAULT 0,
    ats INTEGER DEFAULT 0,
    btn_open_opp INTEGER DEFAULT 0,
    btn_open INTEGER DEFAULT 0,
    sb_coldcall INTEGER DEFAULT 0,
    sb_complete INTEGER DEFAULT 0,
    bb_vs_steal_opp INTEGER DEFAULT 0,
    fold_bb_vs_steal INTEGER DEFAULT 0,
    threebet_vs_steal INTEGER DEFAULT 0,

    is_pfa INTEGER DEFAULT 0,
    saw_flop INTEGER DEFAULT 0,
    saw_turn INTEGER DEFAULT 0,
    saw_river INTEGER DEFAULT 0,

    cbet_flop_opp INTEGER DEFAULT 0, cbet_flop INTEGER DEFAULT 0,
    cbet_turn_opp INTEGER DEFAULT 0, cbet_turn INTEGER DEFAULT 0,
    cbet_river_opp INTEGER DEFAULT 0, cbet_river INTEGER DEFAULT 0,
    faced_cbet_flop INTEGER DEFAULT 0, fold_to_cbet_flop INTEGER DEFAULT 0,
    faced_bet_flop INTEGER DEFAULT 0, fold_to_bet_flop INTEGER DEFAULT 0,
    faced_bet_turn INTEGER DEFAULT 0, fold_to_bet_turn INTEGER DEFAULT 0,
    faced_bet_river INTEGER DEFAULT 0, fold_to_bet_river INTEGER DEFAULT 0,
    cr_opp_flop INTEGER DEFAULT 0, cr_flop INTEGER DEFAULT 0,
    cr_opp_turn INTEGER DEFAULT 0, cr_turn INTEGER DEFAULT 0,
    cr_opp_river INTEGER DEFAULT 0, cr_river INTEGER DEFAULT 0,
    donk_opp_flop INTEGER DEFAULT 0, donk_flop INTEGER DEFAULT 0,
    donk_opp_turn INTEGER DEFAULT 0, donk_turn INTEGER DEFAULT 0,
    donk_opp_river INTEGER DEFAULT 0, donk_river INTEGER DEFAULT 0,

    aggr_flop INTEGER DEFAULT 0, calls_flop INTEGER DEFAULT 0, folds_flop INTEGER DEFAULT 0,
    aggr_turn INTEGER DEFAULT 0, calls_turn INTEGER DEFAULT 0, folds_turn INTEGER DEFAULT 0,
    aggr_river INTEGER DEFAULT 0, calls_river INTEGER DEFAULT 0, folds_river INTEGER DEFAULT 0,

    -- cents this player put in after the flop was dealt. The made-hand bucket
    -- says what you held; this says what holding it cost, which is the half
    -- that separates a flop give-up from a river call. Blind posts are
    -- preflop by definition, so this is voluntary money only.
    postflop_invested INTEGER DEFAULT 0,
    -- ...and the same money split by the street it went in on, because the
    -- total cannot tell a barrel from a pay-off. Money on the river is a call
    -- made with the hand already finished; money on the turn is a bet into
    -- somebody who is not folding. They sum back to postflop_invested.
    invested_flop INTEGER DEFAULT 0,
    invested_turn INTEGER DEFAULT 0,
    invested_river INTEGER DEFAULT 0,

    wwsf INTEGER DEFAULT 0,
    wtsd INTEGER DEFAULT 0,
    wsd INTEGER DEFAULT 0,
    was_allin INTEGER DEFAULT 0,
    PRIMARY KEY (hand_id, player)
);
CREATE INDEX ix_hp_player ON hand_player(player);
CREATE INDEX ix_hp_hero ON hand_player(is_hero, position);
CREATE INDEX ix_hp_class ON hand_player(player, hand_class);
"""

_INT_COLUMNS = [
    "vpip", "pfr", "rfi_opp", "open_raise", "threebet_opp", "threebet", "faced_3bet",
    "fold_to_3bet", "fourbet", "squeeze_opp", "squeeze", "coldcall_opp", "coldcall",
    "ats_opp", "ats", "btn_open_opp", "btn_open", "sb_coldcall", "sb_complete",
    "bb_vs_steal_opp", "fold_bb_vs_steal", "threebet_vs_steal", "is_pfa",
    "saw_flop", "saw_turn", "saw_river",
    "cbet_flop_opp", "cbet_flop", "cbet_turn_opp", "cbet_turn", "cbet_river_opp",
    "cbet_river", "faced_cbet_flop", "fold_to_cbet_flop",
    "faced_bet_flop", "fold_to_bet_flop", "faced_bet_turn", "fold_to_bet_turn",
    "faced_bet_river", "fold_to_bet_river",
    "cr_opp_flop", "cr_flop", "cr_opp_turn", "cr_turn", "cr_opp_river", "cr_river",
    "donk_opp_flop", "donk_flop", "donk_opp_turn", "donk_turn", "donk_opp_river",
    "donk_river",
    "aggr_flop", "calls_flop", "folds_flop", "aggr_turn", "calls_turn", "folds_turn",
    "aggr_river", "calls_river", "folds_river",
    "postflop_invested", "invested_flop", "invested_turn", "invested_river",
    "wwsf", "wtsd", "wsd", "was_allin",
]


def _blank(player: str) -> dict:
    row = {c: 0 for c in _INT_COLUMNS}
    row["player"] = player
    # Not every hand has a hand to classify, and 0 is not "no pair".
    row["hand_class"] = None
    return row


def rebuild(conn: sqlite3.Connection, progress=None) -> int:
    conn.executescript(DDL)

    hands = conn.execute(
        "SELECT hand_id, bb, button_seat, board_flop, board_turn, board_river,"
        " went_to_showdown, total_pot FROM hands ORDER BY hand_id"
    ).fetchall()

    seats_by_hand: dict[int, list] = {}
    for r in conn.execute(
        "SELECT hand_id, seat_no, player, starting_stack, position, is_hero,"
        " hole_cards, hole_combo, is_dealt_in FROM seats ORDER BY hand_id, seat_no"
    ):
        seats_by_hand.setdefault(r["hand_id"], []).append(r)

    actions_by_hand: dict[int, list] = {}
    for r in conn.execute(
        "SELECT hand_id, seq, street, player, action, amount, to_amount, is_allin,"
        " is_blind_post FROM actions ORDER BY hand_id, seq"
    ):
        actions_by_hand.setdefault(r["hand_id"], []).append(r)

    results_by_hand: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT hand_id, player, collected, reached_showdown FROM results"
    ):
        results_by_hand.setdefault(r["hand_id"], {})[r["player"]] = r

    rows = []
    for i, hand in enumerate(hands):
        rows.extend(
            _derive_hand(
                hand,
                seats_by_hand.get(hand["hand_id"], []),
                actions_by_hand.get(hand["hand_id"], []),
                results_by_hand.get(hand["hand_id"], {}),
            )
        )
        if progress and i % 5000 == 0:
            progress(i, len(hands))

    _classify_rows(rows, hands, seats_by_hand)

    cols = ["hand_id", "player", "position", "is_hero", "hole_combo",
            "starting_stack", "eff_stack_bb", "hand_class"] + _INT_COLUMNS
    conn.executemany(
        f"INSERT INTO hand_player ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(r.get(c, 0) for c in cols) for r in rows],
    )
    conn.commit()
    return len(rows)


def _classify_rows(rows, hands, seats_by_hand) -> int:
    """Attach a made-hand bucket to every row whose cards are known.

    The board used is the board as of the street the player LEFT the hand on,
    which is what `saw_flop`/`saw_turn`/`saw_river` already say. A player who
    folds the flop in a multiway pot never sees the turn and the river even
    though they are dealt, and scoring their fold against five cards would
    judge the decision on information that did not exist when it was made.

    Cards are known for the hero in every hand and for opponents only at a
    showdown, so most opponent rows stay NULL. That is fine: the bucket is a
    fact about a specific player's hand, and a row without it simply does not
    appear in the summary's denominator.

    Runs once over the whole rebuild rather than per hand, because the
    evaluator is vectorized and calling it 50,000 times would throw that away.
    """
    boards = {h["hand_id"]: (h["board_flop"], h["board_turn"], h["board_river"])
              for h in hands}
    holes = {(s["hand_id"], s["player"]): s["hole_cards"]
             for seats in seats_by_hand.values() for s in seats}

    pending, inputs = [], []
    for r in rows:
        if not r["saw_flop"]:
            continue
        hole_txt = holes.get((r["hand_id"], r["player"])) or ""
        flop, turn, river = boards.get(r["hand_id"], ("", "", ""))
        if not hole_txt or not flop:
            continue
        board_txt = " ".join(
            x for x in (flop,
                        turn if r["saw_turn"] else "",
                        river if r["saw_river"] else "") if x
        )
        try:
            hole, board = parse_cards(hole_txt), parse_cards(board_txt)
        except ValueError:
            continue  # a malformed card is a parser problem, not a crash here
        if len(hole) != 2 or not 3 <= len(board) <= 5:
            continue
        pending.append(r)
        inputs.append((hole, board))

    for r, bucket in zip(pending, handclass.classify_many(inputs)):
        r["hand_class"] = bucket
    return len(pending)


def _derive_hand(hand, seats, actions, results) -> list[dict]:  # noqa: C901
    dealt = [s for s in seats if s["is_dealt_in"]]
    if not dealt:
        return []
    bb = hand["bb"] or 1
    pos_of = {s["player"]: (s["position"] or "") for s in dealt}
    flags = {s["player"]: _blank(s["player"]) for s in dealt}

    acts = [a for a in actions if a["action"] != "returns"]

    # Street at which each player folded, if any.
    folded_at: dict[str, str] = {}
    for a in acts:
        if a["action"] == "fold":
            folded_at.setdefault(a["player"], a["street"])
        if a["is_allin"] and a["player"] in flags:
            flags[a["player"]]["was_allin"] = 1

    # ---------------- preflop -------------------------------------------
    bet_level = 0
    opener = None
    cold_callers = 0
    voluntary: set[str] = set()
    any_voluntary = False
    seen_3bet_spot: set[str] = set()
    seen_faced_3bet: set[str] = set()
    last_raiser = None

    for a in acts:
        if a["street"] != "preflop":
            break
        p = a["player"]
        if p not in flags:
            continue
        if a["is_blind_post"]:
            continue
        F = flags[p]
        pos = pos_of.get(p, "")
        facing = bet_level

        # --- opportunities, recorded from the state the player actually faced
        if facing == 0 and not any_voluntary:
            F["rfi_opp"] = 1
            if pos in STEAL_POSITIONS:
                F["ats_opp"] = 1
            if pos == "BTN":
                F["btn_open_opp"] = 1
        if facing == 1 and p not in seen_3bet_spot:
            seen_3bet_spot.add(p)
            F["threebet_opp"] = 1
            if cold_callers >= 1:
                F["squeeze_opp"] = 1
            if p not in voluntary and pos not in ("SB", "BB"):
                F["coldcall_opp"] = 1
            if (pos == "BB" and cold_callers == 0
                    and pos_of.get(opener, "") in STEAL_POSITIONS):
                F["bb_vs_steal_opp"] = 1
        if facing == 2 and p == opener and p not in seen_faced_3bet:
            seen_faced_3bet.add(p)
            F["faced_3bet"] = 1

        # --- the action itself
        if a["action"] == "raise":
            F["vpip"] = 1
            F["pfr"] = 1
            if facing == 0:
                F["open_raise"] = 1
                opener = p
                if not any_voluntary:
                    if pos in STEAL_POSITIONS:
                        F["ats"] = 1
                    if pos == "BTN":
                        F["btn_open"] = 1
            elif facing == 1:
                F["threebet"] = 1
                if cold_callers >= 1:
                    F["squeeze"] = 1
                if F["bb_vs_steal_opp"]:
                    F["threebet_vs_steal"] = 1
            elif facing == 2 and p == opener:
                F["fourbet"] = 1
            bet_level = facing + 1
            cold_callers = 0
            last_raiser = p
            voluntary.add(p)
            any_voluntary = True
        elif a["action"] in ("call", "bet"):
            F["vpip"] = 1
            if facing >= 1:
                if p not in voluntary and pos not in ("SB", "BB"):
                    F["coldcall"] = 1
                if pos == "SB" and p not in voluntary:
                    F["sb_coldcall"] = 1
                cold_callers += 1
            elif pos == "SB":
                # Completing the small blind in an unraised pot: VPIP but not
                # PFR. Should be zero under a raise-or-fold strategy, which
                # makes it a compliance check rather than a statistic.
                F["sb_complete"] = 1
            voluntary.add(p)
            any_voluntary = True
        elif a["action"] == "fold":
            if facing == 2 and p == opener:
                F["fold_to_3bet"] = 1
            if F["bb_vs_steal_opp"] and facing == 1:
                F["fold_bb_vs_steal"] = 1

    pfa = last_raiser
    if pfa in flags:
        flags[pfa]["is_pfa"] = 1

    # ---------------- streets seen --------------------------------------
    boards = {"flop": hand["board_flop"], "turn": hand["board_turn"],
              "river": hand["board_river"]}
    blocked = {"flop": ("preflop",), "turn": ("preflop", "flop"),
               "river": ("preflop", "flop", "turn")}
    for p, F in flags.items():
        for street in STREETS_AFTER_PREFLOP:
            if boards[street] and folded_at.get(p) not in blocked[street]:
                F[f"saw_{street}"] = 1

    # ---------------- postflop ------------------------------------------
    # Money added after the flop, before any of the street-by-street logic
    # below, because it is a plain sum over the actions and depends on none of
    # it. `amount` and not `to_amount`: this is chips added, and for a raise
    # PokerStars prints the increment over the previous level rather than the
    # total, which is exactly the trap `to_amount` exists to avoid elsewhere.
    #
    # This is the one place that reads `actions` rather than `acts`, because it
    # is the one place a "returns" row matters. An uncalled bet comes straight
    # back and was never invested in anything; the row carries the negative
    # amount that cancels the bet, so summing over it is what keeps this at or
    # below what the player actually contributed. Everything else here is
    # counting decisions, where a return is not one.
    # The split by street rides along here rather than in its own pass: it is
    # the same sum with the street kept instead of thrown away, and a returned
    # bet cancels on the street it was made on.
    for a in actions:
        if a["street"] in STREETS_AFTER_PREFLOP and a["player"] in flags:
            amt = a["amount"] or 0
            flags[a["player"]]["postflop_invested"] += amt
            flags[a["player"]][f"invested_{a['street']}"] += amt

    order = _postflop_order(dealt, hand["button_seat"])
    for street in STREETS_AFTER_PREFLOP:
        if not boards[street]:
            continue
        street_acts = [a for a in acts if a["street"] == street]
        in_hand = [p for p in order if flags.get(p, {}).get(f"saw_{street}")]
        pfa_here = pfa if pfa in in_hand else None
        pfa_idx = in_hand.index(pfa_here) if pfa_here else None

        bettor = None
        checked: set[str] = set()
        cr_pending: set[str] = set()

        for a in street_acts:
            p = a["player"]
            if p not in flags:
                continue
            F = flags[p]
            act = a["action"]
            facing_bet = bettor is not None

            # donk: out of position vs the PFA and first into the pot
            if (not facing_bet and pfa_here and p != pfa_here and p in in_hand
                    and pfa_idx is not None and in_hand.index(p) < pfa_idx
                    and not F[f"donk_opp_{street}"]):
                F[f"donk_opp_{street}"] = 1
                if act == "bet":
                    F[f"donk_{street}"] = 1

            # c-bet: the PFA reaches their turn with the pot still unbet
            if p == pfa_here and not facing_bet and not F[f"cbet_{street}_opp"]:
                eligible = (
                    street == "flop"
                    or (street == "turn" and F["cbet_flop"])
                    or (street == "river" and F["cbet_flop"] and F["cbet_turn"])
                )
                if eligible:
                    F[f"cbet_{street}_opp"] = 1
                    if act == "bet":
                        F[f"cbet_{street}"] = 1

            if facing_bet:
                if not F[f"faced_bet_{street}"]:
                    F[f"faced_bet_{street}"] = 1
                    if act == "fold":
                        F[f"fold_to_bet_{street}"] = 1
                if street == "flop" and bettor == pfa_here and not F["faced_cbet_flop"]:
                    F["faced_cbet_flop"] = 1
                    if act == "fold":
                        F["fold_to_cbet_flop"] = 1
                if p in checked and not F[f"cr_opp_{street}"]:
                    F[f"cr_opp_{street}"] = 1
                    if act == "raise":
                        F[f"cr_{street}"] = 1

            if act == "check":
                checked.add(p)
            elif act in ("bet", "raise"):
                F[f"aggr_{street}"] += 1
                bettor = p
            elif act == "call":
                F[f"calls_{street}"] += 1
            elif act == "fold":
                F[f"folds_{street}"] += 1

    # ---------------- results-derived -----------------------------------
    stacks = {s["player"]: s["starting_stack"] for s in dealt}
    for s in dealt:
        p = s["player"]
        F = flags[p]
        others = [v for k, v in stacks.items() if k != p]
        eff = min(stacks[p], max(others)) if others else stacks[p]
        F["position"] = s["position"]
        F["is_hero"] = s["is_hero"]
        F["hole_combo"] = s["hole_combo"]
        F["starting_stack"] = s["starting_stack"]
        F["eff_stack_bb"] = round(eff / bb, 2)
        F["hand_id"] = hand["hand_id"]

        res = results.get(p)
        collected = res["collected"] if res else 0
        showdown = bool(res["reached_showdown"]) if res else False
        if F["saw_flop"] and collected > 0:
            F["wwsf"] = 1
        if F["saw_flop"] and showdown:
            F["wtsd"] = 1
        if showdown and collected > 0:
            F["wsd"] = 1

    return list(flags.values())


def _postflop_order(dealt, button_seat: int) -> list[str]:
    """Players in postflop acting order: first seat after the button, clockwise."""
    ordered = sorted(dealt, key=lambda s: s["seat_no"])
    n = len(ordered)
    start = 0
    for i, s in enumerate(ordered):
        if s["seat_no"] > button_seat:
            start = i
            break
    else:
        start = 0
    return [ordered[(start + i) % n]["player"] for i in range(n)]
