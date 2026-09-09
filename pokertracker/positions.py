"""Position derivation.

Position is derived, never parsed. This is the highest-risk logic in the
tracker: every positional stat depends on it and a wrong answer still looks
plausible, so the rules are spelled out rather than inferred.

Rules
-----
* Start from the button seat. Walk dealt-in seats in ascending seat order with
  wraparound; seats that are empty, sitting out, or not dealt in do not exist
  for this purpose.
* Label by DISTANCE FROM THE BUTTON, never by seat number. Walking backwards
  from the button the ladder is BTN, CO, HJ, LJ, UTG+n, ..., UTG; walking
  forwards it is SB then BB.
* Short-handed tables are the common case. The ladder is simply truncated:
  with 5 dealt in there is no UTG, with 4 there is no HJ.
* Heads-up inverts everything. The button is the small blind, acts first
  preflop and last postflop. It is labelled BTN here; the other player is BB.
* A player who posts a dead blind out of position still takes the position
  their seat gives them. The blind posted is not the signal; the button is.
"""

from __future__ import annotations

from .parser import Hand

POSITION_ORDER = ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN", "SB", "BB"]
_ORDER_INDEX = {p: i for i, p in enumerate(POSITION_ORDER)}


def position_ladder(n_dealt: int) -> list[str]:
    """Names for the non-blind seats, ordered by distance back from the button.

    Index 0 is the button itself, index 1 the cutoff, and so on. The last entry
    is always UTG, which is what makes short tables truncate correctly.
    """
    if n_dealt <= 2:
        return ["BTN"]
    k = n_dealt - 2  # seats that are not the two blinds
    if k <= 4:
        return ["BTN", "CO", "HJ", "UTG"][:k]
    middle = [f"UTG+{i}" for i in range(k - 5, 0, -1)]
    return ["BTN", "CO", "HJ", "LJ"] + middle + ["UTG"]


def assign_positions(hand: Hand) -> None:
    """Fill Seat.position for every dealt-in seat, in place."""
    dealt = sorted((s for s in hand.seats if s.is_dealt_in), key=lambda s: s.seat_no)
    n = len(dealt)
    if n == 0:
        return
    if n == 1:
        dealt[0].position = "BTN"
        return

    btn_idx = _button_index(dealt, hand.button_seat)

    if n == 2:
        # Heads-up: the button posts the small blind and is first to act
        # preflop, last on every later street.
        dealt[btn_idx].position = "BTN"
        dealt[(btn_idx + 1) % 2].position = "BB"
        _sanity_check(hand, dealt)
        return

    ladder = position_ladder(n)
    for distance, name in enumerate(ladder):
        dealt[(btn_idx - distance) % n].position = name
    dealt[(btn_idx + 1) % n].position = "SB"
    dealt[(btn_idx + 2) % n].position = "BB"
    _sanity_check(hand, dealt)


def _button_index(dealt, button_seat: int) -> int:
    """Index of the button within the dealt-in seats.

    The button can sit on an empty seat (dead button). In that case it belongs
    to the nearest dealt-in seat at or before it, walking backwards.
    """
    for i, s in enumerate(dealt):
        if s.seat_no == button_seat:
            return i
    candidates = [i for i, s in enumerate(dealt) if s.seat_no < button_seat]
    return candidates[-1] if candidates else len(dealt) - 1


def _sanity_check(hand: Hand, dealt) -> None:
    """Compare derived blinds against posted blinds and record disagreements.

    A dead blind makes these legitimately disagree, so this only flags cases
    where nothing unusual was posted — which is exactly when a mismatch means
    the button or the dealt-in set was read wrong.
    """
    posts = [a for a in hand.actions if a.is_blind_post]
    if any(a.amount not in (hand.sb, hand.bb, hand.ante) for a in posts):
        return  # dead or combined blind: mismatch is expected
    bb_posters = {a.player for a in posts if a.amount == hand.bb and hand.bb}
    if len(bb_posters) != 1:
        return
    derived = {s.player for s in dealt if s.position == "BB"}
    if derived and bb_posters != derived:
        hand.problems.append(
            f"position check: BB posted by {bb_posters} but derived as {derived} "
            f"(button seat {hand.button_seat})"
        )


def sort_key(position: str) -> int:
    return _ORDER_INDEX.get(position, 99)
