"""Validate the vectorized evaluator against an independent brute-force one.

The reference below is written deliberately differently -- plain Python, best
five of seven by enumeration, tuple comparison -- so that a shared
misunderstanding is unlikely to cancel out. What matters is that the two agree
on the ORDERING of every pair of hands, not that they produce the same numbers.
"""

from __future__ import annotations

import random
import sys
import unittest
from collections import Counter
from itertools import combinations
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokertracker.cards import card_to_int  # noqa: E402
from pokertracker.evaluator import evaluate  # noqa: E402


def score5(cards) -> tuple:
    ranks = sorted((c // 4 for c in cards), reverse=True)
    suits = [c % 4 for c in cards]
    cnt = Counter(ranks)
    flush = len(set(suits)) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight_high = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [12, 3, 2, 1, 0]:
            straight_high = 3
    groups = sorted(cnt.items(), key=lambda kv: (-kv[1], -kv[0]))
    shape = [c for _, c in groups]
    order = [r for r, _ in groups]

    if flush and straight_high is not None:
        return (8, straight_high)
    if shape[0] == 4:
        return (7, *order)
    if shape[:2] == [3, 2]:
        return (6, *order)
    if flush:
        return (5, *ranks)
    if straight_high is not None:
        return (4, straight_high)
    if shape[0] == 3:
        return (3, *order)
    if shape[:2] == [2, 2]:
        return (2, *order)
    if shape[0] == 2:
        return (1, *order)
    return (0, *ranks)


def reference7(cards) -> tuple:
    return max(score5(c) for c in combinations(cards, 5))


def sign(x) -> int:
    return (x > 0) - (x < 0)


class TestEvaluator(unittest.TestCase):
    def test_agrees_with_reference_on_random_hands(self):
        rng = random.Random(20260908)
        hands = [rng.sample(range(52), 7) for _ in range(4000)]
        fast = evaluate(np.array(hands, dtype=np.int32))
        slow = [reference7(h) for h in hands]

        # Every pair must be ordered identically by both evaluators.
        mismatches = 0
        for i in range(0, len(hands) - 1, 2):
            j = i + 1
            if sign(int(fast[i]) - int(fast[j])) != sign(
                    (slow[i] > slow[j]) - (slow[i] < slow[j])):
                mismatches += 1
        self.assertEqual(mismatches, 0)

    def test_category_boundaries(self):
        def ev(text):
            return int(evaluate(np.array(
                [[card_to_int(c) for c in text.split()]], dtype=np.int32))[0])

        royal = ev("As Ks Qs Js Ts 2c 3d")
        wheel_sf = ev("As 2s 3s 4s 5s Kc Qd")
        quads = ev("9c 9d 9h 9s Ac Kd 2h")
        boat = ev("9c 9d 9h Ac Ad 2h 3s")
        flush = ev("As Js 9s 5s 3s 2c 4d")
        straight = ev("9c 8d 7h 6s 5c Ad Kh")
        wheel = ev("Ac 2d 3h 4s 5c Kd Qh")
        trips = ev("9c 9d 9h Ac Kd 2h 3s")
        two_pair = ev("9c 9d Ac Ad Kh 2s 3c")
        pair = ev("9c 9d Ac Kd Qh 2s 3c")
        high = ev("Ac Kd Qh Js 9c 3s 2d")

        order = [high, pair, two_pair, trips, wheel, straight, flush,
                 boat, quads, wheel_sf, royal]
        self.assertEqual(order, sorted(order), "categories must rank in order")
        self.assertLess(wheel, straight, "five-high straight is the weakest")
        self.assertLess(wheel_sf, royal)

    def test_kickers_are_compared(self):
        def ev(text):
            return int(evaluate(np.array(
                [[card_to_int(c) for c in text.split()]], dtype=np.int32))[0])

        # Same pair of nines, different kickers.
        self.assertGreater(ev("9c 9d Ac Kd Qh 2s 3c"), ev("9c 9d Ac Kd Jh 2s 3c"))
        # Playing the board: identical sevens must tie.
        self.assertEqual(ev("2c 3d As Ks Qs Js Ts"), ev("2h 3s As Ks Qs Js Ts"))

    def test_five_and_six_card_hands_agree_with_the_reference(self):
        """Scoring a flop or a turn is the same arithmetic with fewer cards.

        The classifier needs a hand scored before the river, so the evaluator
        accepts five and six cards as well. Nothing about the encoding changes:
        the reference here is the same brute force, over whatever it was given.
        """
        rng = random.Random(20260910)
        for k in (5, 6):
            hands = [rng.sample(range(52), k) for _ in range(1500)]
            fast = evaluate(np.array(hands, dtype=np.int32))
            slow = [max(score5(c) for c in combinations(h, 5)) for h in hands]
            mismatches = 0
            for i in range(0, len(hands) - 1, 2):
                j = i + 1
                if sign(int(fast[i]) - int(fast[j])) != sign(
                        (slow[i] > slow[j]) - (slow[i] < slow[j])):
                    mismatches += 1
            self.assertEqual(mismatches, 0, f"{k}-card hands")

    def test_a_hand_keeps_its_score_as_dead_cards_are_added(self):
        """Scores from different card counts stay comparable.

        A pair of kings on the flop and the same pair of kings at the river are
        the same hand, and the classifier compares a five-card board against a
        seven-card holding, so an absent kicker must be zero rather than
        something that reorders the tiebreak.
        """
        def ev(text):
            return int(evaluate(np.array(
                [[card_to_int(c) for c in text.split()]], dtype=np.int32))[0])

        self.assertEqual(ev("Kc Kd 9h 5s 2c") >> 20, ev("Kc Kd 9h 5s 2c 3d 4h") >> 20)
        # Six cards to a flush: the sixth must not become a kicker.
        self.assertEqual(ev("As Ks Qs Js 9s 2d") >> 20, 5)

    def test_fewer_than_five_cards_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(np.zeros((1, 4), dtype=np.int32))

    def test_seven_card_uses_best_five(self):
        def ev(text):
            return int(evaluate(np.array(
                [[card_to_int(c) for c in text.split()]], dtype=np.int32))[0])

        # Trips on board plus a pair in hand is a full house, not trips.
        self.assertEqual(
            ev("5c 5d 5h Kc Kd 2s 3h") >> 20, 6)
        # Six to a flush still scores as the best five.
        self.assertEqual(ev("As Ks Qs Js 9s 8s 2d") >> 20, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
