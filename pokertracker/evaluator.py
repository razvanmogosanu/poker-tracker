"""Vectorized 7-card hand evaluator.

Scores are plain integers, higher is better, comparable only against each other:

    score = category << 20 | tiebreak

where category runs 0 (high card) .. 8 (straight flush) and tiebreak packs up
to five ranks into five nibbles, most significant first, each stored as rank+1
so that "absent" is 0 and short tiebreak lists compare correctly.

Everything is table-driven and evaluated a chunk of boards at a time, which is
what makes exhaustive C(48,5) enumeration practical in numpy.
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# lookup tables over the 13-bit rank mask


def _build_tables() -> tuple[np.ndarray, np.ndarray]:
    straight = np.full(8192, -1, dtype=np.int8)
    top5 = np.zeros(8192, dtype=np.int32)
    wheel = (1 << 12) | 0b1111  # A,2,3,4,5
    for mask in range(8192):
        for high in range(12, 3, -1):
            need = 0
            for i in range(5):
                need |= 1 << (high - i)
            if mask & need == need:
                straight[mask] = high
                break
        else:
            if mask & wheel == wheel:
                straight[mask] = 3  # five-high straight

        packed, shift = 0, 16
        for rank in range(12, -1, -1):
            if mask >> rank & 1:
                packed |= (rank + 1) << shift
                shift -= 4
                if shift < 0:
                    break
        top5[mask] = packed
    return straight, top5


STRAIGHT, TOP5 = _build_tables()

CAT_HIGH, CAT_PAIR, CAT_TWO_PAIR, CAT_TRIPS = 0, 1, 2, 3
CAT_STRAIGHT, CAT_FLUSH, CAT_BOAT, CAT_QUADS, CAT_SF = 4, 5, 6, 7, 8

CATEGORY_NAMES = [
    "high card", "pair", "two pair", "three of a kind", "straight",
    "flush", "full house", "four of a kind", "straight flush",
]


def _hi(mask: np.ndarray) -> np.ndarray:
    """Highest rank present in a mask, as rank+1 (0 when the mask is empty)."""
    return (TOP5[mask] >> 16) & 0xF


def _bit(rank_plus_one: np.ndarray) -> np.ndarray:
    """rank+1 -> its bit in a rank mask; 0 maps to no bit at all."""
    return np.where(rank_plus_one > 0,
                    np.left_shift(1, np.maximum(rank_plus_one - 1, 0)), 0)


def evaluate(cards: np.ndarray) -> np.ndarray:
    """Score an (N, k) array of card ints (rank*4 + suit). Returns (N,) int32.

    k is five to seven. Seven is the case this is built and validated for; five
    and six fall out of the same arithmetic, because every step below counts
    ranks and suits rather than assuming how many cards it was handed, and are
    what lets a hand be scored on a flop or a turn as well as at a showdown.
    Scores from different k are still directly comparable -- a pair of kings is
    a pair of kings whether or not the river has been dealt -- because the
    tiebreak stores absent kickers as zero.
    """
    cards = np.asarray(cards)
    if cards.ndim != 2 or not 5 <= cards.shape[1] <= 7:
        raise ValueError("expected an (N, 5..7) array of cards")
    n = cards.shape[0]
    ranks = (cards // 4).astype(np.int32)
    suits = (cards % 4).astype(np.int32)

    idx = np.arange(13, dtype=np.int32)
    counts = (ranks[:, :, None] == idx[None, None, :]).sum(axis=1)
    rmask = ((counts > 0).astype(np.int32) << idx).sum(axis=1)

    suit_counts = (suits[:, :, None] == np.arange(4)[None, None, :]).sum(axis=1)
    flush_suit = suit_counts.argmax(axis=1)
    has_flush = suit_counts.max(axis=1) >= 5
    in_flush = suits == flush_suit[:, None]
    fmask = np.bitwise_or.reduce(np.where(in_flush, 1 << ranks, 0), axis=1)

    straight_high = STRAIGHT[rmask].astype(np.int32)
    sf_high = np.where(has_flush, STRAIGHT[fmask].astype(np.int32), -1)

    m4 = ((counts == 4).astype(np.int32) << idx).sum(axis=1)
    m3 = ((counts == 3).astype(np.int32) << idx).sum(axis=1)
    m2 = ((counts == 2).astype(np.int32) << idx).sum(axis=1)
    n_trips = (counts == 3).sum(axis=1)
    n_pairs = (counts == 2).sum(axis=1)

    # --- tiebreaks per category
    q = _hi(m4)
    tie_quads = (q << 16) | (_hi(rmask & ~_bit(q)) << 12)

    t = _hi(m3)
    boat_pair = _hi((m3 & ~_bit(t)) | m2)
    tie_boat = (t << 16) | (boat_pair << 12)

    tie_flush = TOP5[fmask]
    tie_straight = (straight_high + 1) << 16
    tie_sf = (sf_high + 1) << 16

    tie_trips = (t << 16) | ((TOP5[rmask & ~_bit(t)] >> 12) << 8)

    hp = _hi(m2)
    lp = _hi(m2 & ~_bit(hp))
    tie_two_pair = (hp << 16) | (lp << 12) | (_hi(rmask & ~_bit(hp) & ~_bit(lp)) << 8)

    p = _hi(m2)
    tie_pair = (p << 16) | ((TOP5[rmask & ~_bit(p)] >> 8) << 4)

    tie_high = TOP5[rmask]

    is_boat = (n_trips >= 2) | ((n_trips == 1) & (n_pairs >= 1))
    conditions = [
        sf_high >= 0,
        m4 != 0,
        is_boat,
        has_flush,
        straight_high >= 0,
        n_trips == 1,
        n_pairs >= 2,
        n_pairs == 1,
    ]
    categories = [CAT_SF, CAT_QUADS, CAT_BOAT, CAT_FLUSH, CAT_STRAIGHT,
                  CAT_TRIPS, CAT_TWO_PAIR, CAT_PAIR]
    ties = [tie_sf, tie_quads, tie_boat, tie_flush, tie_straight,
            tie_trips, tie_two_pair, tie_pair]

    cat = np.select(conditions, categories, default=CAT_HIGH).astype(np.int32)
    tie = np.select(conditions, ties, default=tie_high).astype(np.int32)
    assert cat.shape == (n,)
    return (cat << 20) | tie


def evaluate_chunked(cards: np.ndarray, chunk: int = 200_000) -> np.ndarray:
    out = np.empty(cards.shape[0], dtype=np.int32)
    for start in range(0, cards.shape[0], chunk):
        out[start:start + chunk] = evaluate(cards[start:start + chunk])
    return out
