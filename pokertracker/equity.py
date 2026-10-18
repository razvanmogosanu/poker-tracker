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


# The most (runout, holding) pairs `hand_vs_range` will enumerate before it
# samples instead. Every postflop question fits with room to spare -- a flop
# against all 1,225 other holdings is 1.3M -- and so does a hand against a
# single holding preflop. What does not fit is a hand against a *range*
# preflop: 2.1M boards times several hundred holdings.
ENUMERATE_UP_TO = 5_000_000
# Boards drawn in total when sampling, spread evenly across the holdings.
SAMPLE_BOARDS = 400_000
SAMPLE_SEED = 20260901


def hand_vs_range(hole, board, combos) -> dict:
    """One hand's equity against every holding in a range.

    `hole` is two card ints, `board` zero or three to five, and `combos` an
    (N, 2) array of opposing holdings that already excludes the hole cards and
    the board (`ranges.combos` does this). Every holding counts once: a range
    is a set of holdings, and the cards that remove some of them have been
    applied before this is called.

    Returns the overall equity with its win and tie parts, and `per_combo`,
    the equity against each holding in order, which is what lets the caller
    say how a hand does against one slice of a range without asking again.

    **This is the one place in the module that may sample.** Everything that
    fits under `ENUMERATE_UP_TO` is enumerated like the rest of the file, and
    that is every flop, turn and river. A hand against a range preflop does
    not fit, so it draws `SAMPLE_BOARDS` boards from a fixed seed -- the same
    number for each holding, so the weighting is still exact and only the
    boards are sampled -- and reports the standard error it paid for that.
    `method` says which happened and a caller must show it: a sampled 54.1%
    and an enumerated 54.1% are not the same kind of number.
    """
    hero = np.array([int(c) for c in hole], dtype=np.int32)
    known = np.array([int(c) for c in board], dtype=np.int32)
    combos = np.asarray(combos, dtype=np.int32).reshape(-1, 2)
    n_combos = combos.shape[0]
    if hero.size != 2 or known.size not in (0, 3, 4, 5):
        raise ValueError("expected two hole cards and a board of 0, 3, 4 or 5")
    if n_combos == 0:
        raise ValueError("the range is empty once the known cards are removed")
    used = np.concatenate([hero, known])
    if len(set(used.tolist())) != used.size or np.isin(combos, used).any():
        raise ValueError("a card appears twice")

    to_come = 5 - known.size
    deck = np.array([c for c in range(52) if c not in set(used.tolist())],
                    dtype=np.int32)

    if comb(deck.size, to_come) * n_combos <= ENUMERATE_UP_TO:
        win, tie, cnt = _enumerate_vs_range(hero, known, combos, deck, to_come)
        method, se = "exact", 0.0
    else:
        win, tie, cnt, se = _sample_vs_range(hero, known, combos, to_come)
        method = "sampled"

    total = float(cnt.sum())
    return {
        "equity": float((win + tie / 2).sum()) / total,
        "win": float(win.sum()) / total,
        "tie": float(tie.sum()) / total,
        "per_combo": (win + tie / 2) / cnt,
        "method": method,
        "se": se,
        "evaluated": int(total),
    }


def _enumerate_vs_range(hero, known, combos, deck, to_come):
    """Win, tie and count per holding over every runout that holding allows."""
    n_combos = combos.shape[0]
    runouts = deck[_combo_index(deck.size, to_come).astype(np.int32)] \
        if to_come > 0 else np.zeros((1, 0), dtype=np.int32)
    win = np.zeros(n_combos)
    tie = np.zeros(n_combos)
    cnt = np.zeros(n_combos)

    step = max(1, CHUNK // n_combos)
    for start in range(0, runouts.shape[0], step):
        drawn = runouts[start:start + step]                       # (m, to_come)
        m = drawn.shape[0]
        full = np.hstack([np.tile(known, (m, 1)), drawn]) if known.size else drawn
        hero_score = evaluate(np.hstack([np.tile(hero, (m, 1)), full]))

        # Every holding on every runout in this slice, one rectangular call.
        rows = np.hstack([np.tile(combos, (m, 1)), np.repeat(full, n_combos, axis=0)])
        vill = evaluate(rows).reshape(m, n_combos)
        # The deck still holds the range's own cards, so a runout can deal one
        # of them. Those pairs are not hands that can happen and are left out.
        live = ~(combos[None, :, :, None] == drawn[:, None, None, :]).any(axis=(2, 3))

        win += ((hero_score[:, None] > vill) & live).sum(axis=0)
        tie += ((hero_score[:, None] == vill) & live).sum(axis=0)
        cnt += live.sum(axis=0)
    return win, tie, cnt


def _sample_vs_range(hero, known, combos, to_come):
    """The same three arrays from a fixed-seed sample, plus a standard error."""
    n_combos = combos.shape[0]
    per = max(1, SAMPLE_BOARDS // n_combos)
    rng = np.random.default_rng(SAMPLE_SEED)
    win = np.zeros(n_combos)
    tie = np.zeros(n_combos)
    shares = []

    step = max(1, CHUNK // per)
    for start in range(0, n_combos, step):
        part = combos[start:start + step]
        held = np.repeat(part, per, axis=0)                       # (m, 2)
        m = held.shape[0]
        # A random order over the whole deck with the dead cards pushed to the
        # back: the first `to_come` are a uniform draw without replacement.
        keys = rng.random((m, 52))
        rows = np.arange(m)[:, None]
        keys[rows, held] = 2.0
        keys[:, hero] = 2.0
        if known.size:
            keys[:, known] = 2.0
        drawn = np.argpartition(keys, to_come, axis=1)[:, :to_come].astype(np.int32)
        full = np.hstack([np.tile(known, (m, 1)), drawn]) if known.size else drawn

        hero_score = evaluate(np.hstack([np.tile(hero, (m, 1)), full]))
        vill = evaluate(np.hstack([held, full]))
        w = (hero_score > vill).reshape(-1, per)
        t = (hero_score == vill).reshape(-1, per)
        win[start:start + step] = w.sum(axis=1)
        tie[start:start + step] = t.sum(axis=1)
        shares.append((w + t / 2).ravel())

    share = np.concatenate(shares)
    se = float(share.std(ddof=1) / np.sqrt(share.size)) if share.size > 1 else 0.0
    return win, tie, np.full(n_combos, float(per)), se


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
