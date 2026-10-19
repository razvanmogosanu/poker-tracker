"""Hand ranges: the text syntax, the 169 cells, and the concrete card pairs.

A range is a set of the 169 cells of the standard grid -- 'AA', 'AKs', 'T9o' --
and nothing finer. Suit-specific combos and per-cell weights are what a real
solver trades in; the question this is here for is "what does a sensible
opening range look like on this board", and a cell is the unit that question is
asked in at the table.

The text form exists so a range can be typed faster than it can be clicked:

    QQ+            pairs from queens up
    77-44          a run of pairs
    AKs  AKo  AK   one cell, or both the suited and the offsuit one
    ATs+           same high card, kicker from the ten up to the king
    A5s-A2s        same high card, a run of kickers
    T9s-76s        the same gap, a run of high cards

An unknown token raises rather than being skipped. A range that silently lost a
third of itself to a typo would still produce an equity, and that number would
look exactly as trustworthy as the right one.
"""

from __future__ import annotations

import re

import numpy as np

from .cards import RANK_CHARS, RANK_OF, all_combos

CELLS = all_combos()            # grid order: row by row, aces first
_CELL_SET = frozenset(CELLS)

# The ranges from the hero's own preflop chart, copied verbatim from its VIEWS
# table. They are a plan for one player, not a claim about how opponents play:
# the Solver tab offers them as a starting point for a villain's range because
# a chart somebody actually follows is a better first guess than an invented
# one, and the grid is there to change it.
_P = "AA KK QQ JJ TT 99 88 77 66 55 44 33 22"
_AX = "AKs AQs AJs ATs A9s A8s A7s A6s A5s A4s A3s A2s"
_FACING_RAISE_3BET = "AA KK QQ JJ TT AKs AQs AKo A5s A4s"
_IP_CALL = ("99 88 77 66 55 44 33 22 AJs ATs KQs KJs KTs QJs QTs JTs T9s 98s "
            "87s AQo AJo KQo")
_BB_CALL = ("99 88 77 66 55 44 33 22 AJs ATs A9s A8s A7s A6s A3s A2s KQs KJs "
            "KTs K9s K8s K7s QJs QTs Q9s JTs J9s T9s T8s 98s 97s 87s 86s 76s "
            "75s 65s 54s AQo AJo ATo KQo KJo KTo QJo JTo")

# (key, label, range). The two facing-a-raise charts are split three ways
# because "what they 3-bet" and "what they call" are different ranges by the
# time there is a board to look at, and the union is neither.
PRESETS = [
    ("utg", "UTG open",
     _P + " AKs AQs AJs ATs A5s A4s KQs KJs KTs QJs QTs JTs T9s 98s "
     "AKo AQo AJo KQo"),
    ("hj", "HJ open",
     _P + " AKs AQs AJs ATs A9s A5s A4s KQs KJs KTs QJs QTs JTs J9s T9s 98s "
     "87s AKo AQo AJo KQo KJo"),
    ("co", "CO open",
     _P + " " + _AX + " KQs KJs KTs K9s QJs QTs Q9s JTs J9s T9s T8s 98s 97s "
     "87s 86s 76s 75s 65s AKo AQo AJo ATo KQo KJo KTo QJo QTo JTo"),
    ("btn", "BTN open",
     _P + " " + _AX + " KQs KJs KTs K9s K8s K7s K6s K5s QJs QTs Q9s Q8s Q7s "
     "JTs J9s J8s J7s T9s T8s T7s 98s 97s 96s 87s 86s 85s 76s 75s 65s 64s 54s "
     "AKo AQo AJo ATo A9o A8o A7o A6o A5o A4o A3o A2o KQo KJo KTo K9o K8o QJo "
     "QTo Q9o JTo J9o T9o 98o"),
    ("sb", "SB open",
     _P + " " + _AX + " KQs KJs KTs K9s K8s K7s K6s QJs QTs Q9s Q8s JTs J9s "
     "J8s T9s T8s 98s 97s 87s 86s 76s 65s AKo AQo AJo ATo A9o A8o A7o A5o A4o "
     "KQo KJo KTo QJo QTo JTo T9o"),
    ("ip_3bet", "IP vs raise: 3-bet", _FACING_RAISE_3BET),
    ("ip_call", "IP vs raise: call", _IP_CALL),
    ("ip_continue", "IP vs raise: both", _FACING_RAISE_3BET + " " + _IP_CALL),
    ("bb_3bet", "BB vs raise: 3-bet", _FACING_RAISE_3BET),
    ("bb_call", "BB vs raise: call", _BB_CALL),
    ("bb_continue", "BB vs raise: both", _FACING_RAISE_3BET + " " + _BB_CALL),
]

_HAND = re.compile(r"^([2-9TJQKA])([2-9TJQKA])([so]?)$")


def _cell(hi: int, lo: int, suffix: str) -> str:
    return RANK_CHARS[hi] + RANK_CHARS[lo] + suffix


def _one(token: str) -> tuple[int, int, str]:
    """'AKs' -> (12, 11, 's'). A pair comes back with an empty suffix."""
    m = _HAND.match(token)
    if not m:
        raise ValueError(f"not a hand: {token!r}")
    a, b, suffix = RANK_OF[m.group(1)], RANK_OF[m.group(2)], m.group(3)
    if a == b:
        if suffix:
            raise ValueError(f"a pair cannot be suited or offsuit: {token!r}")
        return a, b, ""
    return max(a, b), min(a, b), suffix


def _suffixes(suffix: str) -> tuple[str, ...]:
    return (suffix,) if suffix else ("s", "o")


def _expand(token: str) -> set[str]:
    plus = token.endswith("+")
    body = token[:-1] if plus else token
    if "-" in body:
        if plus:
            raise ValueError(f"a run cannot also be open-ended: {token!r}")
        left, _, right = body.partition("-")
        (h1, l1, s1), (h2, l2, s2) = _one(left), _one(right)
        if s1 != s2:
            raise ValueError(f"a run must stay suited or offsuit: {token!r}")
        if h1 == l1 or h2 == l2:
            if not (h1 == l1 and h2 == l2):
                raise ValueError(f"a run cannot mix pairs and non-pairs: {token!r}")
            return {_cell(r, r, "") for r in range(min(h1, h2), max(h1, h2) + 1)}
        if h1 == h2:                                 # A5s-A2s
            return {_cell(h1, k, s) for s in _suffixes(s1)
                    for k in range(min(l1, l2), max(l1, l2) + 1)}
        if h1 - l1 == h2 - l2:                       # T9s-76s
            gap = h1 - l1
            return {_cell(h, h - gap, s) for s in _suffixes(s1)
                    for h in range(min(h1, h2), max(h1, h2) + 1)}
        raise ValueError(
            f"a run needs the same high card or the same gap: {token!r}")

    hi, lo, suffix = _one(body)
    if not plus:
        return {_cell(hi, lo, s) for s in (_suffixes(suffix) if hi != lo else ("",))}
    if hi == lo:
        return {_cell(r, r, "") for r in range(hi, 13)}
    return {_cell(hi, k, s) for s in _suffixes(suffix) for k in range(lo, hi)}


def parse(text: str) -> frozenset[str]:
    """Range text -> the set of grid cells it names."""
    cells: set[str] = set()
    for raw in re.split(r"[,\s]+", text.strip()):
        if not raw:
            continue
        # Ranks upper, the suited/offsuit letter lower, so 'aks' and 'AKS' work.
        # Anything else is left as typed, so the error quotes what was typed.
        token = "".join(ch.upper() if ch in "tjqka" else
                        ch.lower() if ch in "SO" else ch for ch in raw)
        cells |= _expand(token)
    return frozenset(cells)


def _runs(values: list[int]) -> list[tuple[int, int]]:
    """Sorted ints -> maximal consecutive runs as (high, low), highest first."""
    out: list[tuple[int, int]] = []
    for v in sorted(values, reverse=True):
        if out and out[-1][1] == v + 1:
            out[-1] = (out[-1][0], v)
        else:
            out.append((v, v))
    return out


def to_text(cells) -> str:
    """The compact text for a set of cells; `parse(to_text(x)) == x`."""
    cells = set(cells)
    unknown = cells - _CELL_SET
    if unknown:
        raise ValueError(f"not a grid cell: {sorted(unknown)[0]!r}")
    parts: list[str] = []

    pairs = [RANK_OF[c[0]] for c in cells if len(c) == 2]
    for hi, lo in _runs(pairs):
        if hi == lo:
            parts.append(_cell(hi, hi, ""))
        elif hi == 12:
            parts.append(_cell(lo, lo, "") + "+")
        else:
            parts.append(f"{_cell(hi, hi, '')}-{_cell(lo, lo, '')}")

    for suffix in ("s", "o"):
        for high in range(12, 0, -1):
            kickers = [RANK_OF[c[1]] for c in cells
                       if len(c) == 3 and c[2] == suffix and RANK_OF[c[0]] == high]
            for hi, lo in _runs(kickers):
                if hi == lo:
                    parts.append(_cell(high, hi, suffix))
                elif hi == high - 1:
                    parts.append(_cell(high, lo, suffix) + "+")
                else:
                    parts.append(f"{_cell(high, hi, suffix)}-{_cell(high, lo, suffix)}")
    return ", ".join(parts)


def _cell_combos(cell: str) -> list[tuple[int, int]]:
    hi = RANK_OF[cell[0]]
    lo = RANK_OF[cell[1]]
    if len(cell) == 2:
        return [(hi * 4 + a, hi * 4 + b) for a in range(4) for b in range(a + 1, 4)]
    if cell[2] == "s":
        return [(hi * 4 + s, lo * 4 + s) for s in range(4)]
    return [(hi * 4 + a, lo * 4 + b) for a in range(4) for b in range(4) if a != b]


def combos(cells, dead=()) -> np.ndarray:
    """Every concrete two-card holding in `cells`, as an (N, 2) array.

    A holding that uses a `dead` card -- the hero's own, or one on the board --
    is dropped. That is the whole of card removal: holding the ace of spades
    takes three of the six AA combos out of anybody else's range.
    """
    wanted = set(cells)
    unknown = wanted - _CELL_SET
    if unknown:
        raise ValueError(f"not a grid cell: {sorted(unknown)[0]!r}")
    gone = set(int(c) for c in dead)
    out = [pair for cell in CELLS if cell in wanted
           for pair in _cell_combos(cell)
           if pair[0] not in gone and pair[1] not in gone]
    return np.array(out, dtype=np.int32).reshape(-1, 2)


def preset(key: str) -> frozenset[str]:
    for k, _label, text in PRESETS:
        if k == key:
            return parse(text)
    raise ValueError(f"unknown preset {key!r}")
