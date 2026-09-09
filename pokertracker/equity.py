"""All-in EV by exhaustive board enumeration.

Worth building second, not first, but it materially reduces the noise in the
win-rate graph. At every hand that got all-in with cards to come, each player's
share of the pot is replaced by equity x pot, computed by enumerating every
remaining board rather than sampling.

Side pots are handled per layer: a player can only win the layers they are
eligible for, and equity for each layer is computed using only that layer's
contenders. Money contributed by players who folded is dead money and lands in
the lowest layers, which is where it belongs.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from itertools import chain, combinations
from math import comb

import numpy as np

from .cards import parse_cards
from .evaluator import evaluate

CHUNK = 100_000
BOARD_SIZE = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}


@lru_cache(maxsize=8)
def _combo_index(deck_size: int, k: int) -> np.ndarray:
    """All C(deck_size, k) index tuples as an array. Cached: it is reused a lot."""
    total = comb(deck_size, k)
    flat = np.fromiter(
        chain.from_iterable(combinations(range(deck_size), k)),
        dtype=np.int8, count=total * k,
    )
    return flat.reshape(total, k)


def _pot_layers(contributions: dict[str, int], contenders: list[str]):
    """Split the pot into layers. Returns [(amount, [eligible players]), ...]."""
    thresholds = sorted({contributions.get(p, 0) for p in contenders})
    layers, prev = [], 0
    for th in thresholds:
        if th <= prev:
            continue
        amount = sum(min(c, th) - min(c, prev) for c in contributions.values())
        eligible = [p for p in contenders if contributions.get(p, 0) >= th]
        if amount > 0 and eligible:
            layers.append([amount, eligible])
        prev = th
    # Dead money above the highest contender's stack has nowhere else to go.
    shortfall = sum(contributions.values()) - sum(a for a, _ in layers)
    if shortfall > 0 and layers:
        layers[-1][0] += shortfall
    return [(a, e) for a, e in layers]


def hand_equities(hole: dict[str, str], board: list[int]) -> tuple[dict[str, float], int]:
    """Each player's average share of the pot over every completion of `board`.

    Ties split evenly. Enumeration is exhaustive: C(48,5) is 1.7M boards for
    two players preflop, which the chunked evaluator handles in a few seconds.
    Returns (equity by player, number of boards enumerated).
    """
    players = list(hole)
    hole_cards = {p: np.array(parse_cards(hole[p]), dtype=np.int32) for p in players}
    used = set(board)
    for cards in hole_cards.values():
        used.update(int(c) for c in cards)
    deck = np.array([c for c in range(52) if c not in used], dtype=np.int32)

    to_come = 5 - len(board)
    combos = _combo_index(len(deck), to_come) if to_come > 0 \
        else np.zeros((1, 0), dtype=np.int8)
    n_boards = combos.shape[0]
    known = np.array(board, dtype=np.int32)

    win_sums = dict.fromkeys(players, 0.0)
    for start in range(0, n_boards, CHUNK):
        idx = combos[start:start + CHUNK]
        m = idx.shape[0]
        drawn = deck[idx.astype(np.int32)] if to_come > 0 \
            else np.zeros((m, 0), dtype=np.int32)
        full_board = np.hstack([np.tile(known, (m, 1)), drawn]) if known.size else drawn

        scores = np.empty((len(players), m), dtype=np.int32)
        for i, p in enumerate(players):
            scores[i] = evaluate(
                np.hstack([np.tile(hole_cards[p], (m, 1)), full_board])
            )
        winners = scores == scores.max(axis=0)
        shares = winners / winners.sum(axis=0)
        for i, p in enumerate(players):
            win_sums[p] += float(shares[i].sum())

    return {p: win_sums[p] / n_boards for p in players}, n_boards


def _layer_equities(hole: dict[str, str], board: list[int], layers) -> dict:
    """Equity per pot layer, using only the players eligible for that layer."""
    cache: dict[tuple, dict] = {}
    out = []
    for amount, eligible in layers:
        key = tuple(sorted(eligible))
        if key not in cache:
            sub = {p: hole[p] for p in eligible}
            cache[key] = hand_equities(sub, board)[0] if len(sub) > 1 else {eligible[0]: 1.0}
        out.append((amount, cache[key]))
    return out


def compute_for_hand(conn: sqlite3.Connection, hand_id: int) -> list[tuple] | None:
    h = conn.execute(
        "SELECT * FROM hands WHERE hand_id = ?", (hand_id,)
    ).fetchone()
    if not h or not h["went_to_showdown"]:
        return None

    allin = conn.execute(
        "SELECT COUNT(*) n FROM actions WHERE hand_id = ? AND is_allin = 1", (hand_id,)
    ).fetchone()["n"]
    if not allin:
        return None

    last_street = conn.execute(
        "SELECT street FROM actions WHERE hand_id = ? ORDER BY seq DESC LIMIT 1",
        (hand_id,),
    ).fetchone()["street"]
    known_n = BOARD_SIZE.get(last_street, 5)
    if known_n >= 5:
        return None  # all-in on the river: nothing left to enumerate

    board_all = parse_cards(" ".join(
        x for x in (h["board_flop"], h["board_turn"], h["board_river"]) if x))
    board = board_all[:known_n]

    rows = conn.execute(
        """SELECT r.player, r.contributed, r.reached_showdown, s.hole_cards
           FROM results r LEFT JOIN seats s
             ON s.hand_id = r.hand_id AND s.player = r.player
           WHERE r.hand_id = ?""",
        (hand_id,),
    ).fetchall()

    contributions = {r["player"]: r["contributed"] for r in rows}
    contenders = [r["player"] for r in rows if r["reached_showdown"]]
    hole = {r["player"]: r["hole_cards"] for r in rows if r["reached_showdown"]}
    if len(contenders) < 2 or any(not v for v in hole.values()):
        return None  # cannot compute without every contender's cards

    layers = _pot_layers(contributions, contenders)
    if not layers:
        return None

    # Rake, cash-out fee and the cash-out provider's cut all come off the pot
    # before anyone is paid, so EV is measured against the net pot.
    net_pot = h["total_pot"] - h["rake"] - h["cashout_fee"] - h["cashout_residual"]
    scale = net_pot / h["total_pot"] if h["total_pot"] else 0.0

    per_layer = _layer_equities(hole, board, layers)
    ev_collected = {p: 0.0 for p in contenders}
    weighted, total_amount = {p: 0.0 for p in contenders}, 0
    for amount, eq in per_layer:
        total_amount += amount
        for p, e in eq.items():
            ev_collected[p] += e * amount * scale
            weighted[p] += e * amount

    out = []
    for p in contenders:
        equity = weighted[p] / total_amount if total_amount else 0.0
        ev_net = int(round(ev_collected[p] - contributions.get(p, 0)))
        out.append((hand_id, p, equity, ev_net))
    return out


def compute_all(conn: sqlite3.Connection, force: bool = False,
                progress: bool = False) -> int:
    if force:
        conn.execute("DELETE FROM allin_ev")
        conn.commit()

    candidates = [r["hand_id"] for r in conn.execute(
        """SELECT DISTINCT h.hand_id FROM hands h
           JOIN actions a ON a.hand_id = h.hand_id AND a.is_allin = 1
           WHERE h.went_to_showdown = 1
             AND (? OR h.hand_id NOT IN (SELECT hand_id FROM allin_ev))
           ORDER BY h.hand_id""",
        (1 if force else 0,),
    )]

    written = 0
    for i, hand_id in enumerate(candidates, 1):
        rows = compute_for_hand(conn, hand_id)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO allin_ev (hand_id, player, equity, ev_net)"
                " VALUES (?,?,?,?)", rows,
            )
            written += len(rows)
        if progress:
            print(f"\r  all-in hands {i}/{len(candidates)}", end="", flush=True)
        if i % 25 == 0:
            conn.commit()
    conn.commit()
    if progress and candidates:
        print()
    return written
