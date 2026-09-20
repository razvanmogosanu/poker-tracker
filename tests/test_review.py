"""The JSON brief, the metric namespace, and the review journal.

The journal is the one piece of state in this project that the hand histories
cannot reproduce, so most of what is defended here is about not losing it and
not lying with it: a finding names a number in a way that survives rewording,
progress is measured on hands the finding has not already seen, and a window
too small to answer says so rather than reporting no improvement.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokertracker import brief, db, derive, handclass, review, stats  # noqa: E402
from pokertracker.parser import parse_file  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def build_db(stamps: list[str] | None = None) -> sqlite3.Connection:
    """Every fixture in one in-memory database, optionally re-stamped."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for path in sorted(FIXTURES.glob("*.txt")):
        for hand in parse_file(path):
            db.insert_hand(conn, hand, str(path))
    rows = conn.execute(
        "SELECT hand_id FROM hands ORDER BY played_at, hand_id").fetchall()
    if stamps is None:
        stamps = [f"2026-09-0{1 + i // 5} 1{i % 5}:00:00" for i in range(len(rows))]
    for r, stamp in zip(rows, stamps):
        conn.execute("UPDATE hands SET played_at = ? WHERE hand_id = ?",
                     (stamp, r["hand_id"]))
    db.assign_sessions(conn)
    derive.rebuild(conn)
    return conn


class TestTheBriefDerivesNothing(unittest.TestCase):
    """Every figure in the brief is one `stats.py` already returns.

    The brief and the report are two renderings of the same numbers, and the
    one thing that must never happen is that they disagree -- one is what a
    person reads and the other is what advice gets built on.
    """

    HERO = "Btn"

    @classmethod
    def setUpClass(cls):
        cls.conn = build_db()
        cls.b = brief.build(cls.conn, cls.HERO)

    def test_the_figures_are_the_stats_functions_verbatim(self):
        self.assertEqual(self.b["preflop"],
                         stats.preflop_stats(self.conn, self.HERO, None))
        self.assertEqual(self.b["postflop"],
                         stats.postflop_stats(self.conn, self.HERO, None))
        self.assertEqual(self.b["compliance"],
                         stats.compliance(self.conn, self.HERO, None))
        self.assertEqual(self.b["money"],
                         stats.money_summary(self.conn, self.HERO, None))
        self.assertEqual(self.b["by_position"],
                         stats.by_position(self.conn, self.HERO, None))

    def test_no_rate_travels_without_its_sample_size(self):
        # Displaying a bare percentage with no denominator is how a reader ends
        # up acting on noise, and a machine reader has no eyes to squint with.
        for section in ("preflop", "postflop"):
            for r in self.b[section]:
                self.assertIn("num", r, r["name"])
                self.assertIn("den", r, r["name"])
                self.assertIn("ci_lo", r, r["name"])

    def test_the_three_pot_ranges_tile_rather_than_overlap(self):
        by_range = {r["range"]: r for r in self.b["made_hands"]}
        self.assertEqual(by_range["mid"]["max_pot_bb"],
                         by_range["big"]["min_pot_bb"])
        for key, lo, hi in (("all", 0.0, None),
                            ("mid", stats.MID_POT_BB, stats.BIG_POT_BB),
                            ("big", stats.BIG_POT_BB, None)):
            self.assertEqual(by_range[key]["min_pot_bb"], lo)
            self.assertEqual(by_range[key]["max_pot_bb"], hi)

    def test_it_survives_a_round_trip_through_json(self):
        # It is written to a file and read by something else; a value that
        # cannot be serialized is a broken export, not a detail.
        again = json.loads(json.dumps(self.b, default=str))
        self.assertEqual(again["hero"], self.HERO)
        self.assertEqual(again["money"]["hands"], self.b["money"]["hands"])

    def test_the_window_is_published_without_its_sql(self):
        # The predicate and its parameters say nothing to a reader and would
        # turn an implementation detail into part of the published shape.
        self.assertEqual(set(self.b["period"]["selected"]), {"label", "hands"})

    def test_the_thresholds_travel_with_the_data(self):
        c = self.b["constants"]
        self.assertEqual(c["min_opps"], stats.MIN_OPPS)
        self.assertEqual(c["draw_outs"], handclass.DRAW_OUTS)
        self.assertEqual(c["big_pot_bb"], stats.BIG_POT_BB)

    def test_an_unknown_period_is_refused(self):
        with self.assertRaises(ValueError):
            brief.build(self.conn, self.HERO, "not-a-period")


class TestTheMetricNamespace(unittest.TestCase):
    """A finding names a number, and the name has to outlive the wording."""

    HERO = "Btn"

    @classmethod
    def setUpClass(cls):
        cls.conn = build_db()

    def test_every_advertised_id_resolves(self):
        for mid in brief.metric_ids():
            with self.subTest(metric=mid):
                r = brief.resolve(self.conn, self.HERO, mid)
                self.assertEqual(r["metric"], mid)
                self.assertIn("num", r)
                self.assertIn("den", r)

    def test_every_stat_and_check_has_an_id(self):
        # Nothing the report shows should be untrackable, so the namespace has
        # to keep up with the definitions rather than list a chosen few.
        ids = set(brief.metric_ids())
        for defs in (stats.PREFLOP_DEFS, stats.POSTFLOP_DEFS):
            for _label, num, _den in defs:
                self.assertIn(f"stat:{num}", ids)
        for c in stats.compliance(self.conn, self.HERO):
            self.assertIn(f"check:{c['key']}", ids)

    def test_a_compliance_key_is_not_its_prose(self):
        # The names are sentences and one of them interpolates a constant;
        # a finding pointing at a check by name would be orphaned by an edit.
        for c in stats.compliance(self.conn, self.HERO):
            self.assertNotEqual(c["key"], c["name"])
            self.assertRegex(c["key"], r"^[a-z0-9-]+$")

    def test_a_typo_raises_rather_than_reading_as_no_data(self):
        for bad in ("stat:fold_to_4bet", "check:sb-cold-call", "class:middle pair",
                    "money:winrate", "nonsense", "fold_to_3bet"):
            with self.subTest(metric=bad):
                with self.assertRaises(brief.UnknownMetric):
                    brief.resolve(self.conn, self.HERO, bad)

    def test_a_pot_range_is_exclusive_at_the_top(self):
        self.assertEqual(brief.parse_pot_range("15-40"), (15.0, 40.0))
        self.assertEqual(brief.parse_pot_range("40-"), (40.0, None))
        self.assertEqual(brief.parse_pot_range(""), (0.0, None))
        with self.assertRaises(brief.UnknownMetric):
            brief.parse_pot_range("big")

    def test_a_ranged_class_metric_reads_that_range(self):
        wide = brief.resolve(self.conn, self.HERO, f"class:{handclass.NO_PAIR}")
        narrow = brief.resolve(self.conn, self.HERO,
                               f"class:{handclass.NO_PAIR}@40-")
        self.assertGreaterEqual(wide["den"], narrow["den"])


class TestTargets(unittest.TestCase):
    """The band has to be machine-comparable or the verdict is just an opinion."""

    def test_the_grammar(self):
        self.assertEqual(review.parse_target("<=60"), (None, 60.0))
        self.assertEqual(review.parse_target(">=8"), (8.0, None))
        self.assertEqual(review.parse_target("38-52"), (38.0, 52.0))
        self.assertEqual(review.parse_target("0"), (0.0, 0.0))
        self.assertEqual(review.parse_target("=45"), (45.0, 45.0))

    def test_no_band_is_an_answer_and_not_a_missing_one(self):
        # The 8-outs rule says naked continues should be rare, not zero, and
        # turning "rare" into a percentage would dress a guess up as a standard.
        self.assertIsNone(review.parse_target(""))
        self.assertIsNone(review.parse_target(None))

    def test_nonsense_is_rejected_when_it_is_written(self):
        for bad in ("low", "~45%", "under 60", "60%"):
            with self.subTest(target=bad):
                with self.assertRaises(review.JournalError):
                    review.parse_target(bad)


class TestTheJournalIsALog(unittest.TestCase):
    """Events in, state out -- and a line that does not parse is reported."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "reviews.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def _open(self, fid="a", **kw):
        rec = {"op": "open", "id": fid, "opened": "2026-09-01T10:00:00",
               "title": "t", "metric": "stat:vpip", "target": "", "note": "",
               "value_at_open": 20.0, "num_at_open": 2, "den_at_open": 10,
               "unit": "%", "boundary": ["2026-09-01 10:00:00", 1], **kw}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_a_missing_journal_is_empty_rather_than_an_error(self):
        self.assertEqual(review.load(self.path), [])
        self.assertEqual(review.events(self.path), [])

    def test_replaying_the_log_gives_the_current_state(self):
        self._open("a")
        self._open("b")
        review.close("a", "done", self.path)
        by_id = {f["id"]: f for f in review.load(self.path)}
        self.assertEqual(by_id["a"]["status"], "closed")
        self.assertEqual(by_id["a"]["closed_note"], "done")
        self.assertEqual(by_id["b"]["status"], "open")
        # The close is kept as an event, not folded away: when a finding was
        # dealt with is part of the history.
        self.assertEqual(len(review.events(self.path)), 3)

    def test_a_corrupt_line_is_reported_not_skipped(self):
        # Same posture as the problems table: a half-written line is a crashed
        # write, and dropping it loses a finding with nobody the wiser.
        self._open("a")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"op": "open", "id": "b"\n')
        with self.assertRaises(review.JournalError):
            review.load(self.path)

    def test_a_close_without_an_open_is_refused(self):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"op": "close", "id": "ghost"}) + "\n")
        with self.assertRaises(review.JournalError):
            review.load(self.path)

    def test_closing_twice_is_refused(self):
        self._open("a")
        review.close("a", path=self.path)
        with self.assertRaises(review.JournalError):
            review.close("a", path=self.path)

    def test_closing_something_that_does_not_exist_is_refused(self):
        with self.assertRaises(review.JournalError):
            review.close("nope", path=self.path)


class TestProgressIsMeasuredOnHandsSince(unittest.TestCase):
    """The load-bearing claim of the whole journal.

    Re-measuring a corrected leak across the whole history mixes the decisions
    being corrected back in with the corrections, so the rate barely moves and
    the reader concludes nothing changed when it did.
    """

    HERO = "Btn"

    @classmethod
    def setUpClass(cls):
        cls.conn = build_db()
        cls.rows = cls.conn.execute(
            f"""SELECT h.played_at, h.hand_id FROM hands h
                JOIN results r ON r.hand_id = h.hand_id AND r.player = ?
                WHERE {stats.CASH_ONLY}
                ORDER BY h.played_at, h.hand_id""",
            (cls.HERO,),
        ).fetchall()

    def test_the_boundary_hand_is_on_the_old_side_of_it(self):
        # The boundary is the last hand already seen, so the window measuring
        # the correction must not contain it. Off by one hand is not a
        # boundary.
        total = len(self.rows)
        mid = self.rows[total // 2]
        w = stats.window_after(self.conn, self.HERO,
                               (mid["played_at"], mid["hand_id"]))
        self.assertEqual(w.hands, total - (total // 2) - 1)

    def test_since_the_last_hand_is_nothing_at_all(self):
        last = stats.last_hand(self.conn, self.HERO)
        w = stats.window_after(self.conn, self.HERO, last)
        self.assertEqual(w.hands, 0)

    def test_the_two_sides_of_a_boundary_partition_the_history(self):
        total = len(self.rows)
        mid = self.rows[total // 2]
        boundary = (mid["played_at"], mid["hand_id"])
        after = stats.window_after(self.conn, self.HERO, boundary)
        before = stats.periods(self.conn, self.HERO)[0].selected
        self.assertEqual(before.hands, total)
        self.assertLess(after.hands, total)

    def test_status_counts_only_what_came_after(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reviews.jsonl"
            total = len(self.rows)
            mid = self.rows[total // 2]
            rec = {"op": "open", "id": "x", "opened": "2026-09-01T10:00:00",
                   "title": "t", "metric": "stat:vpip", "target": "", "note": "",
                   "value_at_open": 50.0, "num_at_open": 1, "den_at_open": 2,
                   "unit": "%",
                   "boundary": [mid["played_at"], mid["hand_id"]]}
            path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            (r,) = review.status(self.conn, self.HERO, path)
            self.assertEqual(r["hands_since"], total - (total // 2) - 1)
            self.assertEqual(r["now"]["den"], r["hands_since"])
            self.assertLess(r["now"]["den"], total)


class TestAThinWindowSaysSo(unittest.TestCase):
    """"Not enough hands to say" is not a softer way of saying "no better"."""

    HERO = "Btn"

    @classmethod
    def setUpClass(cls):
        cls.conn = build_db()

    def _status(self, path, **kw):
        rec = {"op": "open", "id": "x", "opened": "2026-09-01T10:00:00",
               "title": "t", "metric": "stat:vpip", "target": "<=10",
               "note": "", "value_at_open": 50.0, "num_at_open": 1,
               "den_at_open": 2, "unit": "%",
               "boundary": ["2026-01-01 00:00:00", 0], **kw}
        path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        (r,) = review.status(self.conn, self.HERO, path)
        return r

    def test_below_min_opps_there_is_no_verdict_either_way(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reviews.jsonl"
            # The whole fixture history is well under MIN_OPPS, so a boundary
            # before all of it still cannot answer the question.
            r = self._status(path)
            self.assertLess(r["now"]["den"], stats.MIN_OPPS)
            self.assertEqual(r["verdict"], "thin")
            self.assertNotEqual(r["verdict"], "off")

    def test_a_finding_with_no_band_watches_rather_than_judges(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reviews.jsonl"
            r = self._status(path, target="")
            self.assertIn(r["verdict"], ("thin", "watch"))
            self.assertIsNone(r["moved"])

    def test_a_metric_that_stops_resolving_is_reported_not_fatal(self):
        # A renamed check should surface as one broken finding, not take the
        # whole status listing down with it.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reviews.jsonl"
            r = self._status(path, metric="stat:gone_away")
            self.assertIn("error", r)
            self.assertEqual(r["id"], "x")


class TestVerdicts(unittest.TestCase):
    """Whether it is fixed and whether it moved are different questions."""

    def test_distance_is_zero_inside_the_band(self):
        self.assertEqual(review._distance(45.0, (38.0, 52.0)), 0.0)
        self.assertEqual(review._distance(30.0, (38.0, 52.0)), 8.0)
        self.assertEqual(review._distance(60.0, (38.0, 52.0)), 8.0)
        self.assertEqual(review._distance(60.0, (None, 52.0)), 8.0)
        self.assertEqual(review._distance(60.0, (38.0, None)), 0.0)


class TestAddStampsTheMoment(unittest.TestCase):
    HERO = "Btn"

    def setUp(self):
        self.conn = build_db()
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "reviews.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_it_records_what_the_metric_reads_now(self):
        at = datetime(2026, 9, 20, 18, 0, 0)
        rec = review.add(self.conn, self.HERO, "too loose", "stat:vpip",
                         "<=25", "note", self.path, now=at)
        live = brief.resolve(self.conn, self.HERO, "stat:vpip")
        self.assertEqual(rec["value_at_open"], live["value"])
        self.assertEqual(rec["den_at_open"], live["den"])
        self.assertEqual(rec["id"], "2026-09-20-1")
        self.assertEqual(list(rec["boundary"]),
                         list(stats.last_hand(self.conn, self.HERO)))

    def test_ids_do_not_collide_within_a_day(self):
        at = datetime(2026, 9, 20, 18, 0, 0)
        a = review.add(self.conn, self.HERO, "a", "stat:vpip", "", "", self.path, at)
        b = review.add(self.conn, self.HERO, "b", "stat:pfr", "", "", self.path, at)
        self.assertNotEqual(a["id"], b["id"])

    def test_a_bad_metric_is_rejected_before_anything_is_written(self):
        with self.assertRaises(brief.UnknownMetric):
            review.add(self.conn, self.HERO, "t", "stat:nope", path=self.path)
        self.assertFalse(self.path.exists())

    def test_a_bad_target_is_rejected_before_anything_is_written(self):
        with self.assertRaises(review.JournalError):
            review.add(self.conn, self.HERO, "t", "stat:vpip", "lowish",
                       path=self.path)
        self.assertFalse(self.path.exists())


class TestTheJournalIsNotInTheDatabase(unittest.TestCase):
    """The rule the whole design hangs on.

    Everything else here is rebuildable from the hand histories, so the
    documented cure for a schema change is to delete `poker.db` and re-import.
    A judgement about how somebody is playing is not rebuildable, so it must
    not live anywhere that instruction reaches.
    """

    def test_the_schema_holds_no_review_tables(self):
        self.assertNotIn("review", db.SCHEMA.lower())
        self.assertNotIn("finding", db.SCHEMA.lower())

    def test_the_default_journal_is_a_file_beside_the_database(self):
        self.assertEqual(review.DEFAULT_JOURNAL.suffix, ".jsonl")
        self.assertNotEqual(review.DEFAULT_JOURNAL.suffix, ".db")


if __name__ == "__main__":
    unittest.main()
