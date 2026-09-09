"""Card encoding and hole-card notation.

Cards are encoded as ints 0..51 with ``card = rank * 4 + suit``.
Ranks are 0..12 for deuce..ace; suits are 0..3 for c,d,h,s.
"""

from __future__ import annotations

RANK_CHARS = "23456789TJQKA"
SUIT_CHARS = "cdhs"

RANK_OF = {c: i for i, c in enumerate(RANK_CHARS)}
SUIT_OF = {c: i for i, c in enumerate(SUIT_CHARS)}


def card_to_int(text: str) -> int:
    """'9d' -> int. Raises ValueError on anything that is not a card."""
    if len(text) != 2:
        raise ValueError(f"bad card {text!r}")
    try:
        return RANK_OF[text[0].upper()] * 4 + SUIT_OF[text[1].lower()]
    except KeyError:
        raise ValueError(f"bad card {text!r}") from None


def int_to_card(card: int) -> str:
    return RANK_CHARS[card // 4] + SUIT_CHARS[card % 4]


def parse_cards(text: str) -> list[int]:
    """'[7s 4s Kc]' or '7s 4s Kc' -> [int, ...]"""
    return [card_to_int(t) for t in text.strip().strip("[]").split()]


def cards_to_str(cards) -> str:
    return " ".join(int_to_card(c) for c in cards)


def hole_notation(cards) -> str:
    """Two hole cards -> one of the 169 canonical combos, e.g. 'AKs', 'JJ', 'T7o'."""
    if cards is None or len(cards) != 2:
        return ""
    a, b = sorted(cards, key=lambda c: c // 4, reverse=True)
    ra, rb = a // 4, b // 4
    if ra == rb:
        return RANK_CHARS[ra] * 2
    suited = "s" if a % 4 == b % 4 else "o"
    return RANK_CHARS[ra] + RANK_CHARS[rb] + suited


# The 13x13 grid as conventionally drawn: pairs on the diagonal, suited above
# it (row = higher rank), offsuit below.
def grid_cell(row: int, col: int) -> str:
    """Row/col are 0=ace .. 12=deuce, matching the standard range chart layout."""
    hi, lo = 12 - row, 12 - col
    if row == col:
        return RANK_CHARS[hi] * 2
    if row < col:
        return RANK_CHARS[hi] + RANK_CHARS[lo] + "s"
    return RANK_CHARS[lo] + RANK_CHARS[hi] + "o"


def all_combos() -> list[str]:
    return [grid_cell(r, c) for r in range(13) for c in range(13)]


def combo_weight(combo: str) -> int:
    """How many of the 1326 starting hands this combo represents."""
    if len(combo) == 2:
        return 6
    return 4 if combo.endswith("s") else 12
