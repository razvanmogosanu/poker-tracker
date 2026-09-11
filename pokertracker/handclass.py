"""Classify a made hand into the buckets a hand review actually asks about.

The evaluator answers "which of two hands wins". That is the wrong question for
a leak hunt, because it says nothing about *whose* cards made the hand. A board
of Kh Kd 7s 7c 2d gives everybody two pair; the player holding Ac 5h has ace
high and is about to pay off a full stack. So the categories here are not the
evaluator's nine, they are seven buckets defined by how the hole cards attach
to the board:

    no pair < no pair + draw < weak pair < top pair < overpair
        < two pair < trips < straight+

The rule is the same in every bucket: **a hand the board makes on its own is
not your hand.** Two pair means your hole cards made two pairs, top pair means
you paired the highest card on the board, and "no pair" means exactly what a
player means by it at the table -- you have high card, whatever the five cards
in the middle happen to add up to.

The evaluator is still what decides straights, flushes and full houses, since
those are genuinely fiddly and it is already vectorized and tested. Everything
below a straight is decided structurally, because the structural facts (which
board card did you pair, and was it the top one) are the ones that get lost in
a bare category.

"No pair" is split in two, because averaging the two halves together says
nothing true about either. A naked ace-high that fired three streets and a
flopped flush draw that got there a third of the time are opposite hands: one
is supposed to put money in and the other never is, and a single bucket
understates how bad the first is while making the second look like a leak. The
line between them is `DRAW_OUTS` (8) -- a flush draw or an open-ender, not a
gutshot -- which is exactly the rule a review gives on day one and is otherwise
impossible to check afterwards.

The board passed in is the board *as of the moment the player left the hand* --
see `derive._classify_rows`. Scoring a flop fold against the river would judge
the decision on cards that had not been dealt when it was made. Draws follow
from that and are not an exception to it: a hand that was still drawing when
the money went in is counted as drawing, and a busted draw that paid off a
river bet is naked at the moment it paid, because by then there was nothing
left to draw to.
"""

from __future__ import annotations

import numpy as np

from .evaluator import CAT_STRAIGHT, evaluate

# The number of outs at which continuing stops being a bluff and starts being a
# draw. Eight is the open-ender; nine is the flush draw; a gutshot is four and
# does not clear it. The threshold is the rule itself rather than a tuned
# parameter -- change it and `stats.compliance()` is measuring a different
# piece of advice.
DRAW_OUTS = 8

NO_PAIR = "no pair"
NO_PAIR_DRAW = "draw"
WEAK_PAIR = "weak pair"
TOP_PAIR = "top pair"
OVERPAIR = "overpair"
TWO_PAIR = "two pair"
TRIPS = "trips"
STRAIGHT_PLUS = "straight+"

# Weakest to strongest. Every consumer displays buckets in this order rather
# than in whatever order SQL hands them back, so an empty bucket still holds
# its place in the table and the eye can read down the column.
CLASSES = (NO_PAIR, NO_PAIR_DRAW, WEAK_PAIR, TOP_PAIR, OVERPAIR, TWO_PAIR,
           TRIPS, STRAIGHT_PLUS)

RANK = {c: i for i, c in enumerate(CLASSES)}

# "straight+" is honest but terse; spelled out where there is room for it.
LONG_NAME = {
    NO_PAIR: "no pair, no draw",
    NO_PAIR_DRAW: "no pair, with a draw",
    WEAK_PAIR: "weak pair",
    TOP_PAIR: "top pair",
    OVERPAIR: "overpair",
    TWO_PAIR: "two pair",
    TRIPS: "trips or a set",
    STRAIGHT_PLUS: "straight or better",
}


def _bucket(hole, board, cat: int, plays_board: bool) -> str:
    """The structural rules, given the evaluator's category for hole+board.

    `plays_board` is true when the five cards in the middle already are the
    player's best five -- the hole cards add nothing, so no bucket above them
    can be claimed and the hand falls through to what the hole cards are worth
    on their own, which is nothing.
    """
    if cat >= CAT_STRAIGHT and not plays_board:
        # straight, flush, full house, quads, straight flush
        return STRAIGHT_PLUS

    board_ranks = [c // 4 for c in board]
    top = max(board_ranks)
    a, b = hole[0] // 4, hole[1] // 4

    if a == b:                                   # a pocket pair
        if a in board_ranks:
            return TRIPS                         # a set
        return OVERPAIR if a > top else WEAK_PAIR

    paired = [r for r in (a, b) if r in board_ranks]
    if len(paired) == 2:
        return TWO_PAIR
    if len(paired) == 1:
        r = paired[0]
        if board_ranks.count(r) >= 2:
            return TRIPS                         # trips on a paired board
        return TOP_PAIR if r == top else WEAK_PAIR
    return NO_PAIR


def count_outs(hole, board) -> int:
    """Cards still in the deck that would make this hand a straight or better.

    Only straights and flushes count. Pairing an overcard is six outs and is
    not what anybody means by a draw -- the question this answers is whether
    there was a hand *coming*, which is what justifies putting money in with
    nothing yet.

    The count is honest about whose draw it is, the same way the buckets are
    honest about whose hand it is. On a four-flush turn with no heart in your
    hand, the fifth heart makes a flush that is on the board and that everybody
    at the table has; that is not an out, and the check is the same `plays the
    board` comparison used for made hands. It is only needed from four board
    cards up, because four cards in the middle cannot be a straight or a flush
    on their own, so on the flop every such card necessarily uses a hole card.

    Returns 0 on a five-card board: the hand is complete and there is nothing
    left to draw to. It is only meaningful for a hand that is not there yet --
    ask it about a made flush and every live card qualifies -- which is why
    `classify_many` asks it only of the hands it bucketed as no pair.
    """
    return int(_outs_many([(hole, board)])[0])


def _outs_many(pairs) -> np.ndarray:
    """Outs for many (hole, board) pairs, batched by board length.

    One evaluator call per length rather than one per candidate card: each hand
    contributes 52 rows (the dead ones are masked out afterwards rather than
    packed around, so the array stays rectangular and the indexing stays
    something a reader can follow).
    """
    out = np.zeros(len(pairs), dtype=np.int32)
    by_len: dict[int, list[int]] = {}
    for i, (hole, board) in enumerate(pairs):
        if 3 <= len(board) <= 4:
            by_len.setdefault(len(board), []).append(i)

    deck = np.arange(52, dtype=np.int16)
    for n, idx in by_len.items():
        known = np.array([list(pairs[i][0]) + list(pairs[i][1]) for i in idx],
                         dtype=np.int16)                      # (m, 2 + n)
        m = len(idx)
        live = ~(known[:, :, None] == deck[None, None, :]).any(axis=1)  # (m, 52)

        cand = np.repeat(deck[None, :], m, axis=0).reshape(-1, 1)       # (m*52, 1)
        hand = np.concatenate(
            [np.repeat(known, 52, axis=0), cand], axis=1)               # (m*52, 3+n)
        scores = evaluate(hand)
        made = (scores >> 20).astype(np.int32) >= CAT_STRAIGHT

        if n == 4:
            board_only = np.concatenate(
                [np.repeat(np.array([list(pairs[i][1]) for i in idx],
                                    dtype=np.int16), 52, axis=0), cand], axis=1)
            made &= scores != evaluate(board_only)

        out[idx] = (made.reshape(m, 52) & live).sum(axis=1)
    return out


def classify(hole, board) -> str | None:
    """One hand. `hole` is two card ints, `board` is three to five.

    Returns None when the hand cannot be classified -- no hole cards known, or
    the player folded before a flop was dealt.
    """
    out = classify_many([(hole, board)])
    return out[0]


def classify_many(pairs) -> list[str | None]:
    """Classify many (hole, board) pairs at once.

    Scoring is batched by board length so the evaluator sees one rectangular
    array per length instead of one call per hand; the structural rules are
    plain Python because they are a handful of integer comparisons and would
    cost more to vectorize than they cost to run.
    """
    out: list[str | None] = [None] * len(pairs)
    by_len: dict[int, list[int]] = {}
    for i, (hole, board) in enumerate(pairs):
        if hole is None or board is None or len(hole) != 2 or not 3 <= len(board) <= 5:
            continue
        by_len.setdefault(len(board), []).append(i)

    for n, idx in by_len.items():
        full = np.array([list(pairs[i][0]) + list(pairs[i][1]) for i in idx],
                        dtype=np.int16)
        scores = evaluate(full)
        cats = (scores >> 20).astype(np.int32)
        if n == 5:
            # Only a five-card board can be a complete hand on its own, so this
            # is the only length at which "playing the board" is possible.
            boards = np.array([list(pairs[i][1]) for i in idx], dtype=np.int16)
            plays = scores == evaluate(boards)
        else:
            plays = np.zeros(len(idx), dtype=bool)
        for j, i in enumerate(idx):
            out[i] = _bucket(pairs[i][0], pairs[i][1], int(cats[j]), bool(plays[j]))

    # The draw split is computed only for the hands it can apply to. Everything
    # above no pair already has something worth the money, so what it might
    # also be drawing to does not change the bucket -- and the outs count is 47
    # evaluator rows per hand, which is not worth spending on hands that will
    # not use it.
    naked = [i for i, b in enumerate(out) if b == NO_PAIR]
    if naked:
        outs = _outs_many([pairs[i] for i in naked])
        for i, k in zip(naked, outs):
            if k >= DRAW_OUTS:
                out[i] = NO_PAIR_DRAW
    return out
