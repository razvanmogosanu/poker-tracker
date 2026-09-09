"""Equity checks against well-known reference numbers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokertracker.cards import parse_cards  # noqa: E402
from pokertracker.equity import _pot_layers, hand_equities  # noqa: E402


class TestEquity(unittest.TestCase):
    def test_aces_versus_kings_preflop(self):
        # 81.3% is the all-four-suits-distinct case, which is the WORST one for
        # the aces because it leaves both of the kings' suits fully live. The
        # often-quoted 82.4% is a different suit configuration.
        eq, n = hand_equities({"a": "As Ad", "b": "Kh Kc"}, [])
        self.assertEqual(n, 1712304)  # C(48,5)
        self.assertAlmostEqual(eq["a"], 0.8126, places=3)
        self.assertAlmostEqual(eq["b"], 0.1874, places=3)
        self.assertAlmostEqual(eq["a"] + eq["b"], 1.0, places=9)

    def test_pair_versus_two_overcards(self):
        # The classic coin flip: a pair is a small favourite over two
        # overcards. AK here is OFFSUIT, which is worth about three points
        # less than the suited version.
        eq, _ = hand_equities({"a": "Qh Qs", "b": "Kc As"}, [])
        self.assertAlmostEqual(eq["a"], 0.5676, places=3)
        self.assertAlmostEqual(eq["b"], 0.4324, places=3)

    def test_big_slick_versus_a_small_pair(self):
        eq, _ = hand_equities({"a": "Ah Kh", "b": "7c 7d"}, [])
        self.assertAlmostEqual(eq["a"], 0.4788, places=3)

    def test_equities_sum_to_one(self):
        eq, _ = hand_equities({"a": "Ah Kh", "b": "7c 7d", "c": "Js Ts"},
                              parse_cards("2c 9d Kd"))
        self.assertAlmostEqual(sum(eq.values()), 1.0, places=9)

    def test_dead_heat_splits(self):
        # A rainbow AKQJ board with both players holding blanks: whatever the
        # river is, both make the identical best five, so it always chops.
        eq, _ = hand_equities({"a": "2c 3d", "b": "2h 3c"},
                              parse_cards("Ac Kd Qh Js"))
        self.assertAlmostEqual(eq["a"], 0.5, places=9)
        self.assertAlmostEqual(eq["b"], 0.5, places=9)


class TestPotLayers(unittest.TestCase):
    def test_single_layer(self):
        layers = _pot_layers({"a": 100, "b": 100}, ["a", "b"])
        self.assertEqual(layers, [(200, ["a", "b"])])

    def test_side_pot_excludes_the_short_stack(self):
        # a is all-in for 50, b and c contest another 100 on top.
        layers = _pot_layers({"a": 50, "b": 150, "c": 150}, ["a", "b", "c"])
        self.assertEqual(layers, [(150, ["a", "b", "c"]), (200, ["b", "c"])])

    def test_folded_money_is_dead_money_in_the_lowest_layer(self):
        layers = _pot_layers({"a": 50, "b": 50, "folder": 20}, ["a", "b"])
        self.assertEqual(layers, [(120, ["a", "b"])])

    def test_layers_account_for_every_chip(self):
        contributions = {"a": 33, "b": 91, "c": 91, "d": 7}
        layers = _pot_layers(contributions, ["a", "b", "c"])
        self.assertEqual(sum(a for a, _ in layers), sum(contributions.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
