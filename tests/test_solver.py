"""The Solver tab: range syntax, hand-versus-range equity, and the bet arithmetic.

Most of what is asserted here is an identity rather than a number. A range's
equity is the mean of its holdings' equities; the buckets partition the range;
a call at exactly the required equity is worth nothing. Those hold whatever the
cards are, so a test of them cannot be made to pass by copying an output.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokertracker import db, derive, equity, handclass, ranges, report, solver  # noqa: E402
from pokertracker.cards import combo_weight, parse_cards  # noqa: E402
from pokertracker.parser import parse_file  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class TestRangeSyntax(unittest.TestCase):
    def _n(self, text, dead=""):
        return len(ranges.combos(ranges.parse(text), parse_cards(dead)))

    def test_a_cell_is_six_four_or_twelve_holdings(self):
        self.assertEqual(self._n("AA"), 6)
        self.assertEqual(self._n("AKs"), 4)
        self.assertEqual(self._n("AKo"), 12)
        self.assertEqual(self._n("AK"), 16)

    def test_plus_and_runs(self):
        self.assertEqual(ranges.parse("QQ+"), {"QQ", "KK", "AA"})
        self.assertEqual(ranges.parse("77-55"), {"77", "66", "55"})
        self.assertEqual(ranges.parse("ATs+"), {"ATs", "AJs", "AQs", "AKs"})
        self.assertEqual(ranges.parse("A5s-A2s"), {"A5s", "A4s", "A3s", "A2s"})
        self.assertEqual(ranges.parse("T9s-87s"), {"T9s", "98s", "87s"})
        self.assertEqual(ranges.parse("KJo+"), {"KJo", "KQo"})

    def test_separators_and_case_do_not_matter(self):
        self.assertEqual(ranges.parse("qq+,aks  A5S-a2s"),
                         ranges.parse("QQ+ AKs A5s-A2s"))

    def test_a_typo_raises_instead_of_shrinking_the_range(self):
        for bad in ("AKx", "QQ+-", "AAs", "A5s-K2s", "77-AKs", "1T", "AKs-AQo"):
            with self.assertRaises(ValueError, msg=bad):
                ranges.parse(bad)

    def test_the_text_round_trips(self):
        rng = np.random.default_rng(7)
        for _ in range(50):
            cells = frozenset(c for c in ranges.CELLS if rng.random() < 0.3)
            self.assertEqual(ranges.parse(ranges.to_text(cells)), cells)
        self.assertEqual(ranges.to_text(ranges.parse("AA KK QQ AKs AQs")),
                         "QQ+, AQs+")

    def test_every_cell_is_every_hand(self):
        self.assertEqual(len(ranges.combos(ranges.CELLS)), 1326)

    def test_combos_agree_with_the_grid_weights(self):
        for key, _label, text in ranges.PRESETS:
            cells = ranges.parse(text)
            self.assertEqual(len(ranges.combos(cells)),
                             sum(combo_weight(c) for c in cells), key)

    def test_a_dead_card_removes_exactly_the_holdings_that_use_it(self):
        self.assertEqual(self._n("AA", "As"), 3)
        self.assertEqual(self._n("AKs", "As"), 3)
        self.assertEqual(self._n("AKo", "As Kd"), 7)
        got = ranges.combos(ranges.CELLS, parse_cards("As Kd 7h"))
        self.assertEqual(len(got), 49 * 48 // 2)
        self.assertFalse(np.isin(got, parse_cards("As Kd 7h")).any())

    def test_the_chart_presets_are_the_chart(self):
        """Sizes pinned so an edit to a preset is a decision, not an accident."""
        sizes = {k: len(ranges.combos(ranges.parse(t))) for k, _l, t in ranges.PRESETS}
        self.assertEqual(sizes, {
            "utg": 182, "hj": 206, "co": 318, "btn": 538, "sb": 406,
            "ip_3bet": 58, "ip_call": 128, "ip_continue": 186,
            "bb_3bet": 58, "bb_call": 260, "bb_continue": 318})
        # Calling and 3-betting are a split of one decision, not two ranges
        # that happen to overlap.
        for side in ("ip", "bb"):
            self.assertFalse(ranges.preset(f"{side}_3bet") & ranges.preset(f"{side}_call"))
            self.assertEqual(ranges.preset(f"{side}_3bet") | ranges.preset(f"{side}_call"),
                             ranges.preset(f"{side}_continue"))


class TestHandVersusRange(unittest.TestCase):
    def test_a_one_holding_range_is_the_tested_engine(self):
        """Tied to `hand_equities`, which is validated on its own."""
        for board in ("7s 4s Kc", "7s 4s Kc 2d", "7s 4s Kc 2d 9h", "Ts Js Qd"):
            b = parse_cards(board)
            want = equity.hand_equities({"h": "As Ah", "v": "Kd Kh"}, b)[0]["h"]
            got = equity.hand_vs_range(parse_cards("As Ah"), b,
                                       np.array([parse_cards("Kd Kh")]))
            self.assertEqual(got["method"], "exact")
            self.assertAlmostEqual(got["equity"], want, places=12, msg=board)

    def test_preflop_against_one_holding_is_enumerated(self):
        got = equity.hand_vs_range(parse_cards("As Ah"), [],
                                   np.array([parse_cards("Kd Kc")]))
        self.assertEqual(got["method"], "exact")
        self.assertAlmostEqual(got["equity"], 0.8126, places=3)

    def test_a_range_is_the_mean_of_its_holdings(self):
        hero, board = parse_cards("Ah Qh"), parse_cards("Qs 7h 2h")
        combos = ranges.combos(ranges.preset("btn"), hero + board)
        got = equity.hand_vs_range(hero, board, combos)
        self.assertAlmostEqual(got["equity"], float(got["per_combo"].mean()), places=12)
        self.assertAlmostEqual(got["win"] + got["tie"] / 2, got["equity"], places=12)
        # And each holding on its own gives the figure it was given in the range.
        for i in (0, len(combos) // 2, len(combos) - 1):
            alone = equity.hand_vs_range(hero, board, combos[i:i + 1])
            self.assertAlmostEqual(alone["equity"], float(got["per_combo"][i]), places=12)

    def test_the_river_is_a_count(self):
        """Nothing to enumerate: the nuts beats every holding."""
        hero, board = parse_cards("As Ks"), parse_cards("Qs Js Ts 2d 3c")
        got = equity.hand_vs_range(hero, board, ranges.combos(ranges.CELLS, hero + board))
        self.assertEqual(got["equity"], 1.0)
        self.assertEqual(got["evaluated"], 45 * 44 // 2)

    def test_a_preflop_range_is_sampled_and_says_so(self):
        hero = parse_cards("As Ah")
        combos = ranges.combos(ranges.parse("KK"), hero)
        got = equity.hand_vs_range(hero, [], combos)
        self.assertEqual(got["method"], "sampled")
        self.assertGreater(got["se"], 0)
        # AA against KK is 81-82% in every suit arrangement.
        self.assertLess(abs(got["equity"] - 0.82), 0.01 + 4 * got["se"])
        again = equity.hand_vs_range(hero, [], combos)
        self.assertEqual(got["equity"], again["equity"], "a fixed seed must repeat")

    def test_known_cards_cannot_be_in_the_range(self):
        with self.assertRaises(ValueError):
            equity.hand_vs_range(parse_cards("As Ah"), parse_cards("7s 4s Kc"),
                                 np.array([parse_cards("As Kd")]))
        with self.assertRaises(ValueError):
            equity.hand_vs_range(parse_cards("As Ah"), parse_cards("7s 4s Kc"),
                                 np.zeros((0, 2), dtype=np.int32))


class TestAnalyse(unittest.TestCase):
    SPEC = {"hero": "Ah Qh", "board": "Qs 7h 2c", "pot": 9, "bet": 3,
            "range": "22+, A2s+, KTs+, QTs+, JTs, T9s, 98s, AJo+, KQo"}

    def test_the_buckets_partition_the_range(self):
        d = solver.analyse(self.SPEC)
        self.assertEqual([b["bucket"] for b in d["buckets"]], list(handclass.CLASSES))
        self.assertEqual(sum(b["combos"] for b in d["buckets"]), d["combos"])
        self.assertAlmostEqual(sum(b["share"] for b in d["buckets"]), 1.0)
        mean = sum(b["equity"] * b["combos"] for b in d["buckets"] if b["combos"])
        self.assertAlmostEqual(mean / d["combos"], d["equity"], places=12)

    def test_the_bucket_is_about_their_cards_not_the_board(self):
        """Top pair is a queen in the hand, and hero holding one leaves fewer."""
        d = solver.analyse(self.SPEC)
        by = {b["bucket"]: b for b in d["buckets"]}
        self.assertEqual(d["hero_class"], handclass.TOP_PAIR)
        # Two queens are left. AQs, KQs, QJs, QTs: one holding per queen.
        # KQo: each queen with three kings. AQo: each with the two aces that
        # are neither its own suit nor the hero's.
        self.assertEqual(by[handclass.TOP_PAIR]["combos"], 4 * 2 + 6 + 4)
        self.assertEqual(by[handclass.TRIPS]["combos"], 1 + 3 + 3)   # QQ, 77, 22
        self.assertEqual(by[handclass.OVERPAIR]["combos"], 3 + 6)    # AA, KK

    def test_a_call_at_the_required_equity_is_worth_nothing(self):
        d = solver.analyse(self.SPEC)
        eq = d["equity"]
        # Choose the bet whose price is exactly this equity: B/(P+B) = eq.
        bet = eq * 9 / (1 - eq)
        f = solver.analyse({**self.SPEC, "bet": bet})["facing"]
        self.assertAlmostEqual(f["required_equity"], eq, places=12)
        self.assertAlmostEqual(f["ev_call"], 0.0, places=9)
        self.assertGreater(solver.analyse({**self.SPEC, "bet": bet / 2})["facing"]["ev_call"], 0)
        self.assertLess(solver.analyse({**self.SPEC, "bet": bet * 2})["facing"]["ev_call"], 0)

    def test_when_everything_folds_the_bet_wins_the_pot(self):
        b = solver.analyse({**self.SPEC, "mode": "betting", "continues": []})["betting"]
        self.assertEqual(b["fold_freq"], 1.0)
        self.assertIsNone(b["equity_when_called"])
        self.assertEqual(b["ev_bet"], 9.0)

    def test_when_nothing_folds_the_bet_is_priced_against_the_whole_range(self):
        d = solver.analyse({**self.SPEC, "mode": "betting",
                            "continues": list(handclass.CLASSES)})
        b, eq = d["betting"], d["equity"]
        self.assertEqual(b["fold_freq"], 0.0)
        self.assertAlmostEqual(b["equity_when_called"], eq, places=12)
        self.assertAlmostEqual(b["ev_bet"], eq * (9 + 2 * 3) - 3, places=9)
        self.assertAlmostEqual(b["ev_check"], eq * 9, places=9)

    def test_the_fold_frequency_is_the_unticked_holdings(self):
        keep = [handclass.TOP_PAIR, handclass.OVERPAIR, handclass.TWO_PAIR,
                handclass.TRIPS, handclass.STRAIGHT_PLUS]
        d = solver.analyse({**self.SPEC, "mode": "betting", "continues": keep})
        staying = sum(b["combos"] for b in d["buckets"] if b["bucket"] in keep)
        self.assertEqual(d["betting"]["continue_combos"], staying)
        self.assertAlmostEqual(d["betting"]["fold_freq"], 1 - staying / d["combos"])
        self.assertTrue(d["betting"]["fold_derived"])
        # Called only by better: the equity when called is below the equity
        # against everything, which is the whole reason a thin bet is thin.
        self.assertLess(d["betting"]["equity_when_called"], d["equity"])
        self.assertEqual([b["continues"] for b in d["buckets"]],
                         [b["bucket"] in keep for b in d["buckets"]])

    def test_the_default_is_that_only_naked_hands_fold(self):
        d = solver.analyse({**self.SPEC, "mode": "betting"})
        self.assertEqual([b["bucket"] for b in d["buckets"] if not b["continues"]],
                         [handclass.NO_PAIR])

    def test_preflop_has_no_buckets_and_invents_no_fold_frequency(self):
        d = solver.analyse({"hero": "As Ah", "board": "", "range": "KK",
                            "pot": 3, "bet": 9, "mode": "betting"})
        self.assertEqual(d["buckets"], [])
        self.assertIsNone(d["hero_class"])
        self.assertFalse(d["betting"]["fold_derived"])
        self.assertEqual(d["betting"]["fold_freq"], 0.0)

    def test_a_bad_scenario_is_a_sentence(self):
        for change in ({"hero": "Ah"}, {"board": "Qs 7h"}, {"board": "Ah 7h 2c"},
                       {"range": ""}, {"range": "AKx"}, {"pot": -1}, {"pot": "x"},
                       {"mode": "raising"}, {"continues": ["nuts"]},
                       {"hero": "Qs Qd", "board": "Qh Qc 2c", "range": "QQ"}):
            with self.assertRaises(ValueError, msg=str(change)):
                solver.analyse({**self.SPEC, **change})

    def test_the_answer_is_json(self):
        for spec in (self.SPEC, {**self.SPEC, "mode": "betting"}):
            json.dumps(solver.analyse(spec))


def _fixture_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for path in sorted(FIXTURES.glob("*.txt")):
        for hand in parse_file(path):
            db.insert_hand(conn, hand, str(path))
    db.assign_sessions(conn)
    derive.rebuild(conn)
    return conn


class TestFromHand(unittest.TestCase):
    def setUp(self):
        self.conn = _fixture_db()
        self.addCleanup(self.conn.close)
        self.hu = self.conn.execute(
            "SELECT hand_id FROM hands WHERE site_hand_no = '900000004'"
        ).fetchone()["hand_id"]

    def test_the_spot_is_the_pot_before_the_decision(self):
        """Heads-up: 4c raise called, then the hero bets 4c into 8c at 1c/2c."""
        d = solver.from_hand(self.conn, "ButtonSb", self.hu)
        self.assertEqual((d["street"], d["mode"], d["did"]), ("flop", "betting", "bet"))
        self.assertEqual((d["hero"], d["board"]), ("Ad Kd", "3c 8h Jd"))
        self.assertEqual((d["pot"], d["bet"]), (4.0, 2.0))
        self.assertEqual(d["streets"], ["preflop", "flop"])

    def test_an_earlier_street_has_the_board_it_had_then(self):
        d = solver.from_hand(self.conn, "ButtonSb", self.hu, "preflop")
        self.assertEqual(d["board"], "")
        self.assertEqual((d["mode"], d["pot"], d["bet"]), ("betting", 1.5, 1.5))

    def test_one_opponent_gets_a_suggestion_with_its_reason(self):
        d = solver.from_hand(self.conn, "ButtonSb", self.hu)
        self.assertEqual([o["player"] for o in d["opponents"]], ["BigBlind"])
        self.assertEqual(d["preset"], "bb_call")
        self.assertIn("called a raise", d["preset_why"])
        self.assertEqual(ranges.parse(d["range"]), ranges.preset("bb_call"))
        # And the whole thing is a scenario the solver accepts as it stands.
        self.assertEqual(solver.analyse(d)["street"], "flop")

    def test_what_it_refuses(self):
        with self.assertRaises(ValueError):
            solver.from_hand(self.conn, "ButtonSb", self.hu, "river")
        with self.assertRaises(ValueError):
            solver.from_hand(self.conn, "BigBlind", self.hu)     # cards unknown
        with self.assertRaises(ValueError):
            solver.from_hand(self.conn, "ButtonSb", -1)


class TestThePage(unittest.TestCase):
    def setUp(self):
        self.conn = _fixture_db()
        self.addCleanup(self.conn.close)

    def test_both_pages_have_the_tab(self):
        for live in (False, True):
            html = report.build(self.conn, "Btn", live=live)
            self.assertIn('data-tab="solver"', html)
            self.assertIn('id="tab-solver" hidden', html)
            self.assertIn('id="tab-stats"', html)

    def test_only_the_live_page_can_calculate(self):
        """A file:// page has no server to ask, so it carries no machinery."""
        static = report.build(self.conn, "Btn")
        live = report.build(self.conn, "Btn", live=True)
        for marker in ('id="solver"', "/api/solve", 'id="solver-data"', 'class="live"'):
            self.assertNotIn(marker, static, marker)
            self.assertIn(marker, live, marker)
        self.assertIn("Poker tracker.cmd", static)

    def test_the_grid_is_the_169_cells(self):
        live = report.build(self.conn, "Btn", live=True)
        self.assertEqual(live.count('data-cell="'), 169)
        self.assertEqual(live.count('class="sv-card"'), 52)
        self.assertEqual(live.count('data-preset="'), len(ranges.PRESETS))


class TestTheEndpoints(unittest.TestCase):
    def setUp(self):
        from pokertracker import serve
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        tmp = Path(self.dir.name)
        root = tmp / "history"
        root.mkdir()
        (root / "heads_up.txt").write_text(
            (FIXTURES / "heads_up.txt").read_text(encoding="utf-8"), encoding="utf-8")
        path = str(tmp / "poker.db")
        serve.run_refresh(path, str(root))
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), serve.make_handler(path, str(root), "ButtonSb"))
        self.loop = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.loop.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _call(self, path, body=None):
        req = urllib.request.Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_solve_is_analyse(self):
        status, d = self._call("/api/solve", TestAnalyse.SPEC)
        self.assertEqual(status, 200)
        self.assertEqual(d, json.loads(json.dumps(solver.analyse(TestAnalyse.SPEC))))

    def test_a_bad_scenario_is_a_400_with_the_sentence(self):
        status, d = self._call("/api/solve", {**TestAnalyse.SPEC, "board": "Ah 7h 2c"})
        self.assertEqual(status, 400)
        self.assertIn("twice", d["error"])
        status, d = self._call("/api/range", {"range": "AKx"})
        self.assertEqual(status, 400)
        self.assertIn("AKx", d["error"])

    def test_range_returns_cells_and_canonical_text(self):
        status, d = self._call("/api/range", {"range": "AA KK QQ aks"})
        self.assertEqual(status, 200)
        self.assertEqual(d, {"cells": ["AA", "AKs", "KK", "QQ"],
                             "range": "QQ+, AKs", "combos": 22})
        self.assertEqual(self._call("/api/range", {"range": ""})[1]["combos"], 0)

    def test_hand_is_from_hand(self):
        status, d = self._call("/api/hand?id=1")
        self.assertEqual(status, 200)
        self.assertEqual((d["hero"], d["street"], d["preset"]),
                         ("Ad Kd", "flop", "bb_call"))
        self.assertEqual(self._call("/api/hand?id=1&street=river")[0], 400)
        self.assertEqual(self._call("/api/hand?id=999")[0], 400)
        self.assertEqual(self._call("/api/hand")[0], 400)

    def _stop(self, origin):
        req = urllib.request.Request(self.url + "/api/stop", data=b"",
                                     headers={"Origin": origin})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_another_site_cannot_stop_the_server(self):
        self.assertEqual(self._stop("https://example.com"), 403)
        self.assertEqual(self._call("/api/range", {"range": "AA"})[0], 200)

    def test_the_page_stops_the_server(self):
        """The launcher leaves no window, so the page's button is the off switch."""
        self.assertEqual(self._stop(self.url), 200)
        self.loop.join(timeout=5)
        self.assertFalse(self.loop.is_alive(), "serve_forever still running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
