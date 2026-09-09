"""Test suite. Runs on the standard library: python -m unittest discover tests

The fixtures deliberately cover the table shapes where position derivation goes
wrong: full ring, a short table with a hole in the seating, four-handed,
heads-up, and the two blind anomalies (a second live big blind, and a dead
small blind posted as "small & big blinds").
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokertracker import db, derive  # noqa: E402
from pokertracker.parser import parse_file, parse_money  # noqa: E402
from pokertracker.positions import assign_positions, position_ladder  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    hands = parse_file(FIXTURES / f"{name}.txt")
    assert len(hands) == 1, f"{name}: expected one hand, got {len(hands)}"
    hand = hands[0]
    assign_positions(hand)
    return hand


def positions(hand) -> dict[str, str]:
    return {s.player: s.position for s in hand.seats if s.is_dealt_in}


class TestMoney(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_money("$0.04"), 4)
        self.assertEqual(parse_money("$1"), 100)
        self.assertEqual(parse_money("$2.61"), 261)
        self.assertEqual(parse_money("$0"), 0)

    def test_thousands_separator(self):
        self.assertEqual(parse_money("$1,234.56"), 123456)

    def test_comma_decimal(self):
        # The RO client writes the filename with comma decimals.
        self.assertEqual(parse_money("$0,02"), 2)


class TestPositionLadder(unittest.TestCase):
    def test_six_max_ladder_has_no_lojack(self):
        self.assertEqual(position_ladder(6), ["BTN", "CO", "HJ", "UTG"])

    def test_short_tables_truncate_from_the_front(self):
        self.assertEqual(position_ladder(5), ["BTN", "CO", "HJ"])  # UTG dropped
        self.assertEqual(position_ladder(4), ["BTN", "CO"])        # HJ dropped
        self.assertEqual(position_ladder(3), ["BTN"])

    def test_full_ring(self):
        self.assertEqual(position_ladder(9),
                         ["BTN", "CO", "HJ", "LJ", "UTG+2", "UTG+1", "UTG"])


class TestPositions(unittest.TestCase):
    def test_six_handed(self):
        self.assertEqual(positions(load("six_handed")), {
            "Btn": "BTN", "Sb": "SB", "Bb": "BB",
            "Utg": "UTG", "Hj": "HJ", "Co": "CO",
        })

    def test_five_handed_with_seat_gap(self):
        # Seat 3 is empty; labels must follow distance from the button, not
        # seat number, and UTG must disappear rather than HJ.
        self.assertEqual(positions(load("five_handed_gap")), {
            "Button": "BTN", "Cutoff": "CO", "Hijack": "HJ",
            "Small": "SB", "Big": "BB",
        })

    def test_four_handed_drops_hijack(self):
        self.assertEqual(positions(load("four_handed")), {
            "Button": "BTN", "Cutoff": "CO", "Small": "SB", "Big": "BB",
        })

    def test_heads_up_button_is_the_small_blind(self):
        hand = load("heads_up")
        self.assertEqual(positions(hand), {"ButtonSb": "BTN", "BigBlind": "BB"})
        # The button acts first preflop heads-up.
        first = next(a for a in hand.actions if not a.is_blind_post)
        self.assertEqual(first.player, "ButtonSb")

    def test_double_bb_post_keeps_seat_position(self):
        # Returner posts a live big blind out of position but sits in the
        # hijack seat, and must be labelled HJ.
        self.assertEqual(positions(load("double_bb_post"))["Returner"], "HJ")

    def test_dead_small_blind_keeps_seat_position(self):
        self.assertEqual(positions(load("dead_small_blind"))["Returner"], "HJ")


class TestParserInvariants(unittest.TestCase):
    FIXTURE_NAMES = [p.stem for p in FIXTURES.glob("*.txt")]

    def test_every_fixture_parses_clean(self):
        for name in self.FIXTURE_NAMES:
            with self.subTest(fixture=name):
                hand = load(name)
                self.assertEqual(hand.problems, [])

    def test_contributions_equal_the_pot(self):
        for name in self.FIXTURE_NAMES:
            with self.subTest(fixture=name):
                hand = load(name)
                self.assertEqual(sum(r.contributed for r in hand.results),
                                 hand.total_pot)

    def test_payouts_equal_the_pot(self):
        """Collections plus rake account for the pot, except on cash-outs.

        A player who cashes out forfeits whatever share of the pot they would
        have won to the cash-out provider, and PokerStars emits no `collected`
        line for them; that forfeited share is the residual.
        """
        for name in self.FIXTURE_NAMES:
            with self.subTest(fixture=name):
                hand = load(name)
                self.assertEqual(
                    sum(r.collected for r in hand.results) + hand.rake
                    + hand.cashout_residual,
                    hand.total_pot,
                )
                self.assertGreaterEqual(hand.cashout_residual, 0)


class TestCashOut(unittest.TestCase):
    """A cash-out is an insurance settlement, not a share of the pot."""

    def test_a_losing_player_can_cash_out_while_the_winner_takes_the_pot(self):
        hand = load("cashout_loser")
        winner = next(r for r in hand.results if r.player == "Winner")
        insurer = next(r for r in hand.results if r.player == "Insurer")

        # The winner collected the entire net pot as normal...
        self.assertEqual(winner.collected, 221)
        self.assertEqual(winner.net, 221 - 115)
        # ...so nothing at all was forfeited to the provider.
        self.assertEqual(hand.cashout_residual, 0)

        # The loser collected nothing from the pot but was paid by the provider,
        # which turns a 115c loss into a 5c profit.
        self.assertTrue(insurer.cashed_out)
        self.assertEqual(insurer.collected, 0)
        self.assertEqual(insurer.cashout_received, 120)
        self.assertEqual(insurer.contributed, 115)
        self.assertEqual(insurer.net, 5)

    def test_a_winner_who_cashes_out_forfeits_the_pot_to_the_provider(self):
        # Here the player who WON cashed out, so PokerStars emits no `collected`
        # line at all and the entire net pot becomes the residual.
        hand = load("cashout_winner")
        self.assertEqual(sum(r.collected for r in hand.results), 0)
        self.assertEqual(hand.cashout_residual, 281 - 14)  # pot less rake

        insurer = next(r for r in hand.results if r.player == "Insurer")
        self.assertEqual(insurer.collected, 0)
        self.assertEqual(insurer.cashout_received, 252)
        self.assertEqual(insurer.contributed, 138)
        self.assertEqual(insurer.net, 114)
        # The settlement is 15c short of the 267c the player would have won:
        # 5c of declared fee plus the provider's 10c spread.
        self.assertEqual(hand.cashout_fee, 5)
        self.assertEqual(hand.cashout_residual - insurer.cashout_received, 15)

    def test_fold_may_show_cards(self):
        hand = load("fold_showing_cards")
        self.assertEqual(hand.problems, [])
        folds = [a for a in hand.actions
                 if a.player == "Shower" and a.action == "fold"]
        self.assertEqual(len(folds), 1)
        # The shown cards are captured rather than discarded.
        self.assertEqual(hand.seat_of("Shower").hole_cards, "2h Qh")


class TestActionSemantics(unittest.TestCase):
    def test_raise_amount_is_chips_added_not_the_printed_increment(self):
        # "Utg: raises $0.40 to $0.60" after already committing $0.06 means the
        # player adds $0.54, and their street total becomes $0.60.
        hand = load("preflop_sequence")
        raises = [a for a in hand.actions if a.player == "Utg" and a.action == "raise"]
        self.assertEqual([(a.amount, a.to_amount) for a in raises], [(6, 6), (54, 60)])

    def test_dead_small_blind_is_not_live_money(self):
        # "posts small & big blinds $0.03" contributes 3c to the pot but only
        # the 2c big blind counts as a live bet, so calling a raise to 6c
        # costs 4c and leaves the player at 6c, not 7c.
        hand = load("dead_small_blind")
        post = next(a for a in hand.actions
                    if a.player == "Returner" and a.action == "post")
        self.assertEqual((post.amount, post.to_amount), (3, 2))
        call = next(a for a in hand.actions
                    if a.player == "Returner" and a.action == "call")
        self.assertEqual((call.amount, call.to_amount), (4, 6))

    def test_uncalled_bet_reduces_contribution(self):
        hand = load("preflop_sequence")
        utg = next(r for r in hand.results if r.player == "Utg")
        self.assertEqual(utg.contributed, 20)  # 60c raised, 40c returned

    def test_sitting_out_players_are_not_dealt_in(self):
        hand = load("five_handed_gap")
        self.assertEqual(sum(1 for s in hand.seats if s.is_dealt_in), 5)


class TestDerivedFlags(unittest.TestCase):
    """The preflop denominators, which are the part most easily got wrong."""

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.executescript(db.SCHEMA)
        for path in sorted(FIXTURES.glob("*.txt")):
            for hand in parse_file(path):
                db.insert_hand(cls.conn, hand, str(path))
        derive.rebuild(cls.conn)

    def flags(self, hand_no: str, player: str) -> sqlite3.Row:
        return self.conn.execute(
            """SELECT hp.* FROM hand_player hp JOIN hands h ON h.hand_id = hp.hand_id
               WHERE h.site_hand_no = ? AND hp.player = ?""",
            (hand_no, player),
        ).fetchone()

    SEQ = "900000007"

    def test_open_raiser(self):
        f = self.flags(self.SEQ, "Utg")
        self.assertEqual((f["open_raise"], f["pfr"], f["vpip"]), (1, 1, 1))

    def test_opener_facing_a_3bet_can_4bet(self):
        f = self.flags(self.SEQ, "Utg")
        self.assertEqual(f["faced_3bet"], 1)
        self.assertEqual(f["fourbet"], 1)
        self.assertEqual(f["fold_to_3bet"], 0)

    def test_cold_call(self):
        f = self.flags(self.SEQ, "Hj")
        self.assertEqual((f["coldcall_opp"], f["coldcall"]), (1, 1))
        self.assertEqual(f["vpip"], 1)
        self.assertEqual(f["pfr"], 0)

    def test_squeeze_needs_a_caller_in_between(self):
        co = self.flags(self.SEQ, "Co")
        self.assertEqual((co["threebet_opp"], co["threebet"]), (1, 1))
        self.assertEqual((co["squeeze_opp"], co["squeeze"]), (1, 1))
        # The hijack faced the open with nobody in between, so it had a 3-bet
        # opportunity but not a squeeze opportunity.
        hj = self.flags(self.SEQ, "Hj")
        self.assertEqual(hj["threebet_opp"], 1)
        self.assertEqual(hj["squeeze_opp"], 0)

    def test_no_3bet_opportunity_once_the_pot_is_already_3bet(self):
        # BTN/SB/BB act after the squeeze, so they never faced exactly one
        # raise and must not appear in the 3-bet denominator.
        for player in ("Btn", "Sb", "Bb"):
            with self.subTest(player=player):
                self.assertEqual(self.flags(self.SEQ, player)["threebet_opp"], 0)

    def test_blind_post_is_never_vpip(self):
        f = self.flags(self.SEQ, "Bb")
        self.assertEqual((f["vpip"], f["pfr"]), (0, 0))

    def test_bb_call_of_a_raise_is_vpip(self):
        f = self.flags("900000002", "Big")
        self.assertEqual((f["vpip"], f["pfr"]), (1, 0))

    def test_steal_opportunity_only_when_folded_to(self):
        # Four-handed: the cutoff opens first in, so it gets an ATS attempt.
        co = self.flags("900000003", "Cutoff")
        self.assertEqual((co["ats_opp"], co["ats"]), (1, 1))
        # The button faced a raise, so it had no first-in steal opportunity.
        btn = self.flags("900000003", "Button")
        self.assertEqual(btn["ats_opp"], 0)
        self.assertEqual(btn["threebet"], 1)

    def test_bb_vs_steal(self):
        # Heads-up: the button is the small blind and its raise is a steal;
        # the big blind faced it with no callers in between.
        bb = self.flags("900000004", "BigBlind")
        self.assertEqual(bb["bb_vs_steal_opp"], 1)
        self.assertEqual(bb["fold_bb_vs_steal"], 0)  # it called
        self.assertEqual(bb["threebet_vs_steal"], 0)

    def test_cbet_and_showdown_flags(self):
        f = self.flags("900000002", "Button")
        self.assertEqual((f["is_pfa"], f["saw_flop"]), (1, 1))
        self.assertEqual((f["cbet_flop_opp"], f["cbet_flop"]), (1, 1))
        self.assertEqual(f["wwsf"], 1)   # collected the pot having seen a flop
        self.assertEqual(f["wtsd"], 0)   # no showdown

    def test_fold_to_flop_cbet(self):
        f = self.flags("900000002", "Big")
        self.assertEqual((f["faced_cbet_flop"], f["fold_to_cbet_flop"]), (1, 1))

    def test_showdown_win(self):
        f = self.flags("900000006", "Utg")
        self.assertEqual((f["wtsd"], f["wsd"]), (1, 1))
        loser = self.flags("900000006", "Returner")
        self.assertEqual((loser["wtsd"], loser["wsd"]), (1, 0))


class TestLiveHistory(unittest.TestCase):
    """If the real folder is present, it must parse without a single problem."""

    ROOT = Path.home() / "AppData/Local/PokerStars.RO/HandHistory"

    def test_real_hands_satisfy_invariants(self):
        paths = sorted(self.ROOT.rglob("*.txt")) if self.ROOT.exists() else []
        if not paths:
            self.skipTest("no live hand history available")
        total = 0
        for path in paths:
            for hand in parse_file(path):
                total += 1
                self.assertEqual(hand.problems, [], f"{hand.site_hand_no}")
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
