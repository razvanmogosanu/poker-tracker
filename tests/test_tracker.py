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

from pokertracker import db, derive, stats  # noqa: E402
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


class TestBigPots(unittest.TestCase):
    """The hand-level drill-down. Every column is a claim about one hand."""

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.executescript(db.SCHEMA)
        for path in sorted(FIXTURES.glob("*.txt")):
            for hand in parse_file(path):
                db.insert_hand(cls.conn, hand, str(path))
        derive.rebuild(cls.conn)

    def row(self, player: str, hand_no: str):
        pots = stats.big_pots(self.conn, player, 50)
        for r in pots["losses"] + pots["wins"]:
            if r["site_hand_no"] == hand_no:
                return r
        self.fail(f"{player} has no drill-down row for {hand_no}")

    def test_showdown_winner_carries_the_board_and_the_shown_cards(self):
        r = self.row("Utg", "900000006")
        self.assertEqual(r["exit_street"], "showdown")
        self.assertEqual(r["outcome"], "won at showdown")
        self.assertEqual(r["hole_cards"], "Ah Kh")
        self.assertEqual(r["board"], "2c 7d 9s Kc 4h")
        self.assertIn(("Returner", "5d 5c"),
                      [(x["player"], x["cards"]) for x in r["shown"]])

    def test_showdown_loser(self):
        r = self.row("Returner", "900000006")
        self.assertEqual((r["exit_street"], r["outcome"]),
                         ("showdown", "lost at showdown"))

    def test_taking_it_down_uncontested_is_not_a_showdown_win(self):
        r = self.row("Button", "900000002")
        self.assertEqual((r["exit_street"], r["outcome"]), ("flop", "opponent folded"))

    def test_folding_to_a_cbet_exits_on_the_flop(self):
        r = self.row("Big", "900000002")
        self.assertEqual((r["exit_street"], r["outcome"]), ("flop", "folded"))

    def test_a_preflop_fold_shows_no_board(self):
        # The small blind, not the cutoff: a fold that cost nothing has a net of
        # zero and is deliberately not a "biggest pot" in either direction.
        r = self.row("Small", "900000002")
        self.assertEqual(r["exit_street"], "preflop")
        self.assertEqual(r["board"], "")

    def test_a_cash_out_is_named_rather_than_called_a_showdown(self):
        pots = stats.big_pots(self.conn, "Insurer", 50)
        rows = [r for r in pots["losses"] + pots["wins"]
                if r["site_hand_no"] == "900000008"]
        self.assertTrue(rows, "the cashing-out player should appear")
        self.assertNotIn(rows[0]["outcome"], ("folded",))

    def test_losses_and_wins_are_disjoint_and_correctly_signed(self):
        pots = stats.big_pots(self.conn, "Utg", 50)
        self.assertTrue(all(r["net_bb"] < 0 for r in pots["losses"]))
        self.assertTrue(all(r["net_bb"] > 0 for r in pots["wins"]))
        ids = [r["hand_id"] for r in pots["losses"] + pots["wins"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ordered_by_size_outwards_from_zero(self):
        pots = stats.big_pots(self.conn, "Utg", 50)
        for key in ("losses", "wins"):
            nets = [abs(r["net_bb"]) for r in pots[key]]
            self.assertEqual(nets, sorted(nets, reverse=True), key)

    def test_limit_is_respected(self):
        pots = stats.big_pots(self.conn, "Utg", 1)
        self.assertLessEqual(len(pots["losses"]), 1)
        self.assertLessEqual(len(pots["wins"]), 1)

    def test_raw_text_round_trips_from_the_source_file(self):
        pots = stats.big_pots(self.conn, "Utg", 50)
        ids = [r["hand_id"] for r in pots["losses"] + pots["wins"]]
        texts = db.hand_texts(self.conn, ids)
        self.assertTrue(texts)
        for hand_id, text in texts.items():
            self.assertTrue(text.startswith("PokerStars Hand #"))
        # Every block must be the hand that was asked for, not its neighbour.
        by_id = {r["hand_id"]: r["site_hand_no"]
                 for r in pots["losses"] + pots["wins"]}
        for hand_id, text in texts.items():
            self.assertIn("#" + by_id[hand_id], text.splitlines()[0])

    def test_missing_source_file_yields_no_text_rather_than_an_error(self):
        self.conn.execute("UPDATE hands SET source_file = ? WHERE site_hand_no = ?",
                          ("/nonexistent/history.txt", "900000001"))
        row = self.conn.execute(
            "SELECT hand_id FROM hands WHERE site_hand_no = '900000001'").fetchone()
        self.assertEqual(db.hand_texts(self.conn, [row["hand_id"]]), {})
        self.conn.rollback()


class TestSessionBoundaries(unittest.TestCase):
    """Sessions are assigned once at ingest, and must not move afterwards."""

    @staticmethod
    def build(offsets):
        """A database whose only content is hands at the given minute offsets."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        for i, minute in enumerate(offsets):
            conn.execute(
                "INSERT INTO hands (site_hand_no, played_at, n_dealt, sb, bb)"
                " VALUES (?,?,?,?,?)",
                (f"h{i}", f"2026-09-01 {10 + minute // 60:02d}:{minute % 60:02d}:00",
                 6, 1, 2),
            )
        return conn

    def sessions_of(self, conn):
        return [r["session_id"] for r in conn.execute(
            "SELECT session_id FROM hands ORDER BY played_at, hand_id")]

    def test_a_gap_of_exactly_the_threshold_does_not_split(self):
        # The rule is "longer than", so 30 minutes of nothing is still one
        # session; a player who steps away for exactly the threshold has not
        # started a second session by doing so.
        conn = self.build([0, 30, 60])
        db.assign_sessions(conn, gap_minutes=30)
        self.assertEqual(self.sessions_of(conn), [1, 1, 1])

    def test_a_longer_gap_splits(self):
        conn = self.build([0, 31, 32])
        db.assign_sessions(conn, gap_minutes=30)
        self.assertEqual(self.sessions_of(conn), [1, 2, 2])

    def test_numbering_is_contiguous_from_one(self):
        conn = self.build([0, 1, 100, 101, 300])
        n = db.assign_sessions(conn, gap_minutes=30)
        self.assertEqual(self.sessions_of(conn), [1, 1, 2, 2, 3])
        self.assertEqual(n, 3)

    def test_reassigning_is_idempotent(self):
        conn = self.build([0, 1, 100, 300])
        db.assign_sessions(conn, gap_minutes=30)
        first = self.sessions_of(conn)
        db.assign_sessions(conn, gap_minutes=30)
        self.assertEqual(self.sessions_of(conn), first)

    def test_a_later_hand_does_not_renumber_earlier_sessions(self):
        """The whole reason the column is stored rather than derived per query.

        Appending tonight's hands must leave last week's session ids alone, or
        "last session" would mean a different set of hands after every import.
        """
        conn = self.build([0, 1, 100])
        db.assign_sessions(conn, gap_minutes=30)
        before = self.sessions_of(conn)
        conn.execute(
            "INSERT INTO hands (site_hand_no, played_at, n_dealt, sb, bb)"
            " VALUES ('late', '2026-09-01 20:00:00', 6, 1, 2)")
        db.assign_sessions(conn, gap_minutes=30)
        self.assertEqual(self.sessions_of(conn)[:len(before)], before)

    def test_an_empty_database_has_no_sessions(self):
        self.assertEqual(db.assign_sessions(self.build([])), 0)


class TestStaleSchema(unittest.TestCase):
    """CREATE TABLE IF NOT EXISTS will not add a column to an existing file."""

    def test_a_fresh_database_is_not_stale(self):
        conn = db.connect(":memory:")
        self.assertIn("session_id",
                      {r["name"] for r in conn.execute("PRAGMA table_info(hands)")})

    def test_a_database_missing_a_column_is_rejected_by_name(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # The schema as it stood before session_id existed.
        conn.execute("CREATE TABLE hands (hand_id INTEGER PRIMARY KEY,"
                     " site_hand_no TEXT, played_at TEXT)")
        with self.assertRaises(db.StaleSchema) as ctx:
            db._check_schema(conn)
        self.assertIn("session_id", str(ctx.exception))


class TestPeriodFilters(unittest.TestCase):
    """A period is one predicate at the top of the pipeline, nothing more.

    So the test for it is a partition test: whatever a stat counts over the
    selected window plus whatever it counts over the prior window has to equal
    what it counts over everything, for every period on offer. If that holds,
    no individual stat needed changing.
    """

    HERO = "Btn"

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.executescript(db.SCHEMA)
        for path in sorted(FIXTURES.glob("*.txt")):
            for hand in parse_file(path):
                db.insert_hand(cls.conn, hand, str(path))
        # Spread the fixtures over three sessions on two days, so that every
        # period option has something different to select.
        rows = cls.conn.execute(
            "SELECT hand_id FROM hands ORDER BY played_at, hand_id").fetchall()
        stamps = ["2026-09-01 10:00:00", "2026-09-01 10:01:00", "2026-09-01 10:02:00",
                  "2026-09-01 18:00:00", "2026-09-01 18:01:00",
                  "2026-09-03 20:00:00", "2026-09-03 20:01:00", "2026-09-03 20:02:00",
                  "2026-09-03 21:30:00", "2026-09-03 21:31:00"]
        for r, stamp in zip(rows, stamps):
            cls.conn.execute("UPDATE hands SET played_at = ? WHERE hand_id = ?",
                             (stamp, r["hand_id"]))
        db.assign_sessions(cls.conn)
        derive.rebuild(cls.conn)
        cls.periods = stats.periods(cls.conn, cls.HERO, last_n=(2,))

    def test_more_than_one_period_is_on_offer(self):
        keys = [p.key for p in self.periods]
        self.assertEqual(keys[0], "all")
        self.assertGreater(len(keys), 1, keys)

    def test_selected_and_prior_partition_the_hand_count(self):
        total = self.periods[0].selected.hands
        for p in self.periods:
            self.assertEqual(p.selected.hands + p.prior.hands, total, p.key)

    def test_no_period_is_empty_or_the_whole_history(self):
        for p in self.periods[1:]:
            self.assertGreater(p.selected.hands, 0, p.key)
            self.assertGreater(p.prior.hands, 0, p.key)

    def test_periods_are_offered_only_once_per_boundary(self):
        starts = [p.selected.hands for p in self.periods[1:]]
        self.assertEqual(len(starts), len(set(starts)), starts)

    def test_every_rate_partitions_across_the_boundary(self):
        whole = {r["name"]: r for r in stats.preflop_stats(self.conn, self.HERO)}
        whole.update({r["name"]: r
                      for r in stats.postflop_stats(self.conn, self.HERO)})
        for p in self.periods[1:]:
            parts = {}
            for window in (p.selected, p.prior):
                for r in (stats.preflop_stats(self.conn, self.HERO, window)
                          + stats.postflop_stats(self.conn, self.HERO, window)):
                    n, d = parts.get(r["name"], (0, 0))
                    parts[r["name"]] = (n + r["num"], d + r["den"])
            for name, (num, den) in parts.items():
                self.assertEqual((num, den), (whole[name]["num"], whole[name]["den"]),
                                 f"{p.key}: {name}")

    def test_compliance_counts_partition_across_the_boundary(self):
        whole = {c["name"]: c for c in stats.compliance(self.conn, self.HERO)}
        for p in self.periods[1:]:
            sel = {c["name"]: c for c in
                   stats.compliance(self.conn, self.HERO, p.selected)}
            pri = {c["name"]: c for c in
                   stats.compliance(self.conn, self.HERO, p.prior)}
            for name, c in whole.items():
                self.assertEqual(sel[name]["num"] + pri[name]["num"], c["num"], name)
                self.assertEqual(sel[name]["den"] + pri[name]["den"], c["den"], name)

    def test_money_partitions_across_the_boundary(self):
        whole = stats.money_summary(self.conn, self.HERO)
        for p in self.periods[1:]:
            a = stats.money_summary(self.conn, self.HERO, p.selected)
            b = stats.money_summary(self.conn, self.HERO, p.prior)
            self.assertEqual(a["hands"] + b["hands"], whole["hands"], p.key)
            self.assertAlmostEqual(a["net_bb"] + b["net_bb"], whole["net_bb"],
                                   places=6, msg=p.key)

    def test_last_session_matches_the_last_session_in_the_list(self):
        period = next((p for p in self.periods if p.key == "session"), None)
        self.assertIsNotNone(period, "the fixtures should produce a last session")
        listed = stats.sessions(self.conn, self.HERO)
        self.assertEqual(period.selected.hands, listed[-1]["hands"])

    def test_the_band_is_the_tail_of_the_hand_sequence(self):
        total = self.periods[0].selected.hands
        for p in self.periods[1:]:
            lo, hi = stats.hand_index_range(self.conn, self.HERO, p.selected)
            self.assertEqual(hi, total, p.key)
            self.assertEqual(hi - lo + 1, p.selected.hands, p.key)

    def test_a_thin_denominator_is_not_reported_as_a_failure(self):
        """0 opens out of 2 opportunities is silence, not a broken plan."""
        checks = {c["name"]: c
                  for c in stats.compliance(self.conn, self.HERO, self.periods[-1].selected)}
        btn = checks["BTN open when folded to"]
        self.assertLess(btn["den"], stats.MIN_OPPS)
        self.assertEqual(btn["status"], "thin")

    def test_counts_carry_a_rate_per_hundred_hands(self):
        for c in stats.compliance(self.conn, self.HERO):
            if c["kind"] == "count" and c["den"]:
                self.assertAlmostEqual(c["per100"], 100.0 * c["num"] / c["den"])


class TestReportPeriods(unittest.TestCase):
    """The dashboard has to carry every period with it: it opens from file://."""

    @classmethod
    def setUpClass(cls):
        cls.conn = TestPeriodFilters.conn
        # Not TestPeriodFilters' hero: that one folds the button for nothing in
        # every fixture, so it has no biggest pots and the drill-down would be
        # empty for reasons that have nothing to do with periods.
        cls.hero = "Small"
        from pokertracker import report

        cls.html = report.build(cls.conn, cls.hero)
        import json as _json
        import re as _re

        m = _re.search(
            r'<script type="application/json" id="period-data">(.*?)</script>',
            cls.html, _re.S)
        assert m, "the report must inline its period payload"
        cls.payload = _json.loads(m.group(1).replace("\\u003c", "<"))

    def test_every_period_carries_a_full_set_of_regions(self):
        keys = {p["key"] for p in self.payload["periods"]}
        self.assertIn("all", keys)
        shapes = {k: sorted(v) for k, v in self.payload["regions"].items()}
        self.assertEqual(set(shapes), keys)
        self.assertEqual(len(set(map(tuple, shapes.values()))), 1,
                         "every period must supply the same regions")

    def test_the_default_view_is_all(self):
        self.assertIn('data-period="all" aria-pressed="true"', self.html)
        self.assertNotIn('aria-pressed="true" data-period', self.html)
        for p in self.payload["periods"]:
            if p["key"] != "all":
                self.assertIn(f'data-period="{p["key"]}" aria-pressed="false"',
                              self.html)

    def test_win_rate_is_suppressed_once_a_period_is_selected(self):
        self.assertIn("Win rate", self.payload["regions"]["all"]["r-tiles"])
        for key, regions in self.payload["regions"].items():
            if key == "all":
                continue
            self.assertNotIn("Win rate", regions["r-tiles"], key)
            self.assertIn("Win rate is not shown", regions["r-banner"], key)

    def test_filtered_tables_carry_a_prior_column(self):
        for key, regions in self.payload["regions"].items():
            want = key != "all"
            self.assertEqual("<th>Prior</th>" in regions["r-preflop"], want, key)
            self.assertEqual("<th>Prior</th>" in regions["r-compliance"], want, key)

    def test_the_payload_cannot_close_its_own_script_element(self):
        body = self.html.split('id="period-data">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", body)

    def test_hand_text_is_pooled_rather_than_repeated_per_period(self):
        # Every drill-down row across every period must resolve against one
        # shared pool, and the pool must not hold a hand nobody links to.
        import re as _re

        linked = set()
        for regions in self.payload["regions"].values():
            linked.update(_re.findall(r'data-hand="(\d+)"',
                                      regions["r-drilldown"]))
        self.assertTrue(linked)
        self.assertTrue(set(self.payload["raws"]).issubset(linked))


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
