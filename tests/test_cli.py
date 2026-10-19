"""The command line, driven through cli.main() exactly as a shell would.

These are smoke tests: the figures are tested where they are computed, in
stats/brief/review. What is pinned here is the plumbing -- that each command
runs, exits with the code its callers rely on, and that main() closes the one
connection it opened. They go through main() rather than the cmd_* functions
because closing that connection is main()'s job.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokertracker import cli  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class CliCase(unittest.TestCase):
    """One imported fixture database per class, shared by its tests."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.dir.cleanup)
        cls.tmp = Path(cls.dir.name)
        cls.db = str(cls.tmp / "poker.db")
        rc, cls.import_out, _ = cls.run_cli("import", "--root", str(FIXTURES))
        assert rc == 0, cls.import_out

    @classmethod
    def run_cli(cls, *argv, db=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--db", db or cls.db, *argv])
        return rc, out.getvalue(), err.getvalue()


class TestImport(CliCase):
    def test_the_fixtures_are_imported_and_the_hero_found(self):
        self.assertIn("hands imported: 12", self.import_out)
        self.assertIn("hero: Utg", self.import_out)

    def test_a_second_import_skips_unchanged_files(self):
        rc, out, _ = self.run_cli("import", "--root", str(FIXTURES))
        self.assertEqual(rc, 0)
        self.assertIn("hands imported: 0", out)

    def test_an_empty_folder_is_an_error(self):
        empty = self.tmp / "empty"
        empty.mkdir(exist_ok=True)
        rc, _, err = self.run_cli("import", "--root", str(empty))
        self.assertEqual(rc, 1)
        self.assertIn("no .txt files", err)

    def test_derive_rebuilds_from_the_database_alone(self):
        rc, out, _ = self.run_cli("derive")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"rebuilt [1-9]\d* hand_player rows")


class TestReading(CliCase):
    def test_stats(self):
        rc, out, _ = self.run_cli("stats")
        self.assertEqual(rc, 0)
        self.assertIn("VPIP", out)

    def test_stats_problems(self):
        rc, _, _ = self.run_cli("stats", "--problems")
        self.assertEqual(rc, 0)

    def test_report_writes_one_self_contained_page(self):
        out = self.tmp / "report.html"
        rc, _, _ = self.run_cli("report", "-o", str(out))
        self.assertEqual(rc, 0)
        html = out.read_text(encoding="utf-8")
        self.assertIn("</html>", html)
        self.assertNotIn("<script src=", html)

    def test_export_is_json(self):
        rc, out, _ = self.run_cli("export", "--journal",
                                  str(self.tmp / "none.jsonl"))
        self.assertEqual(rc, 0)
        self.assertIsInstance(json.loads(out), dict)

    def test_hands_lists_the_spots_behind_a_stat(self):
        rc, out, _ = self.run_cli("hands", "--metric", "stat:vpip", "--limit", "3")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 3)

    def test_hands_prints_their_text(self):
        rc, out, _ = self.run_cli("hands", "--metric", "stat:vpip",
                                  "--limit", "1", "--text")
        self.assertEqual(rc, 0)
        self.assertIn("PokerStars", out)

    def test_hands_refuses_a_compliance_check(self):
        # A rule is not a set of spots; see cli._hand_filter.
        rc, _, err = self.run_cli("hands", "--metric", "check:anything")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)

    def test_hands_refuses_an_unknown_stat(self):
        rc, _, err = self.run_cli("hands", "--metric", "stat:nonsense")
        self.assertEqual(rc, 1)
        self.assertIn("unknown stat", err)


class TestRefresh(CliCase):
    def test_refresh_writes_the_report(self):
        out = self.tmp / "refreshed.html"
        rc, text, _ = self.run_cli("refresh", "--root", str(FIXTURES),
                                   "-o", str(out))
        self.assertEqual(rc, 0, text)
        self.assertTrue(out.exists())


class TestReviewJournal(CliCase):
    def setUp(self):
        self.journal = str(self.tmp / f"{self.id()}.jsonl")

    def review(self, *argv):
        return self.run_cli("review", "--journal", self.journal, *argv)

    def test_add_list_status_close(self):
        rc, out, _ = self.review("add", "--title", "Too loose",
                                 "--metric", "stat:vpip", "--target", "20-25")
        self.assertEqual(rc, 0, out)
        fid = out.split()[0]

        rc, out, _ = self.review("list")
        self.assertEqual(rc, 0)
        self.assertIn(fid, out)

        rc, out, _ = self.review("status")
        self.assertEqual(rc, 0)
        self.assertIn(fid, out)

        rc, out, _ = self.review("close", fid)
        self.assertEqual(rc, 0)
        _, out, _ = self.review("list")
        self.assertIn("no findings", out)
        _, out, _ = self.review("list", "--all")
        self.assertIn(fid, out)

    def test_an_unknown_metric_exits_2(self):
        # A typo must fail loudly rather than read as "no data yet".
        rc, _, err = self.review("add", "--title", "x", "--metric", "stat:nope")
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)


class TestSolve(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_a_spot_on_the_river_is_answered_as_json(self):
        rc, out, _ = self.run_cli("solve", "--cards", "As Ah",
                                  "--board", "Kd 7c 2s 9h 3d", "--range", "KK,77",
                                  "--pot", "10", "--bet", "5")
        self.assertEqual(rc, 0)
        result = json.loads(out)
        # Aces into two sets: lost on every holding, and enumerated, not sampled.
        self.assertEqual(result["equity"], 0.0)
        self.assertEqual(result["method"], "exact")
        self.assertIn("caveat", result)

    def test_a_bad_range_is_an_error_not_an_answer(self):
        rc, _, err = self.run_cli("solve", "--cards", "As Ah",
                                  "--board", "Kd 7c 2s 9h 3d", "--range", "KK,banana")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)


class TestStaleSchema(unittest.TestCase):
    def test_a_stale_database_is_a_message_and_exit_2(self):
        # Leaving the directory also checks db.connect closed the handle it
        # opened before raising: on Windows the cleanup fails otherwise.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE hands (hand_id INTEGER PRIMARY KEY,"
                         " site_hand_no TEXT, played_at TEXT)")
            conn.close()
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main(["--db", str(path), "derive"])
            self.assertEqual(rc, 2)
            self.assertIn("error:", err.getvalue())


class TestTheConnectionIsClosed(unittest.TestCase):
    """main() owns the connection, so a command cannot leak it."""

    def test_the_database_file_can_be_removed_straight_after(self):
        # On Windows an open sqlite handle keeps the file locked, so this is a
        # direct check that main() closed it, there and everywhere else.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poker.db"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--db", str(path), "import",
                                           "--root", str(FIXTURES)]), 0)
                self.assertEqual(cli.main(["--db", str(path), "derive"]), 0)
            path.unlink()
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
