"""The page's own JavaScript: the period restore across the live reload.

CLAUDE.md's "The one reload that carries the filter is the live page's own"
is a rule about code that runs in a browser, and nothing else in the suite
executes it. So report.js is run here under Node against a stub DOM just large
enough for it to load, and each scenario starts from a fresh sessionStorage.
The restore is observable from outside: a period that is applied swaps its
fragments into the page, so the test reads which region markup arrived.

Skips when `node` is not on PATH; the static checks at the bottom do not.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokertracker import report, stats  # noqa: E402

NODE = shutil.which("node")

# A browser just large enough for report.js to load. Every query finds
# nothing except the period payload and the regions it names, so the only
# effect a scenario can have is the restore under test.
HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [src, scenarioPath] = process.argv.slice(2);
const sc = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));

const store = new Map(Object.entries(sc.storage));
const sessionStorage = {
  getItem: k => store.has(k) ? store.get(k) : null,
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};
function el() {
  return {
    dataset: {}, style: {}, hidden: false, innerHTML: '', textContent: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
    getAttribute: () => null, removeAttribute() {}, appendChild() {},
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }),
  };
}
const regions = {};
const payload = el();
payload.textContent = JSON.stringify(sc.data);
const document = Object.assign(el(), {
  documentElement: el(), body: el(),
  getElementById(id) {
    if (id === 'period-data') return payload;
    if (sc.regionIds.includes(id)) {
      // A region opens holding the "all" markup, which the page reads back.
      if (!regions[id]) { regions[id] = el(); regions[id].innerHTML = sc.opening[id]; }
      return regions[id];
    }
    return null;
  },
  createElement: el, createElementNS: el, dispatchEvent() {},
});
const window = {
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener() {}, scrollY: 0, scrollTo() {},
};
class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }

vm.runInNewContext(fs.readFileSync(src, 'utf8'), {
  document, window, sessionStorage, CustomEvent, console,
  location: { reload() {} }, setInterval() {}, setTimeout() {},
  requestAnimationFrame() {}, getComputedStyle: () => ({ getPropertyValue: () => '' }),
});

const swapped = Object.fromEntries(Object.entries(regions).map(([k, v]) => [k, v.innerHTML]));
process.stdout.write(JSON.stringify({ swapped, storage: Object.fromEntries(store) }));
"""


@unittest.skipUnless(NODE, "node is not installed")
class TestPeriodRestore(unittest.TestCase):
    # Two periods that slide and one that jumps, each with its own fragment so
    # the swapped markup names the period that was applied.
    BANDS = {"session": [90, 100], "week": [40, 100], "n500": [0, 100]}

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.dir.cleanup)
        cls.tmp = Path(cls.dir.name)
        cls.src = cls.tmp / "report.js"
        cls.src.write_text(report.JS, encoding="utf-8")
        cls.harness = cls.tmp / "harness.js"
        cls.harness.write_text(HARNESS, encoding="utf-8")

    def page(self, storage: dict, bands: dict | None = None) -> dict:
        """Load the page once with this sessionStorage; what did it restore?"""
        bands = self.BANDS if bands is None else bands
        keys = ["all", *bands]
        # The shape report.build_pages ships: "all" is not in the payload,
        # it is the page's own markup, named by `ids`.
        data = {
            "periods": [{"key": k, "band": bands.get(k)} for k in keys],
            "ids": ["r-main"],
            "regions": {k: {"r-main": f"<p>{k}</p>"} for k in keys if k != "all"},
            "raws": {},
        }
        path = self.tmp / f"{self.id()}.json"
        path.write_text(json.dumps({"storage": storage, "data": data,
                                    "regionIds": ["r-main"],
                                    "opening": {"r-main": "<p>all</p>"}}),
                        encoding="utf-8")
        out = subprocess.run([NODE, str(self.harness), str(self.src), str(path)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout)
        html = res["swapped"].get("r-main")
        res["applied"] = re.sub(r"</?p>", "", html) if html else "all"
        return res

    def test_opening_the_page_lands_on_all(self):
        # A remembered period without the reload flag is a reader opening the
        # file, or reloading by hand: neither restores anything.
        res = self.page({"pt-period": "week"})
        self.assertEqual(res["applied"], "all")
        self.assertNotIn("pt-period", res["storage"])

    def test_the_live_reload_restores_a_sliding_period(self):
        res = self.page({"pt-period": "week", "pt-reloaded": "1"})
        self.assertEqual(res["applied"], "week")
        self.assertEqual(res["storage"]["pt-period"], "week")

    def test_the_reload_flag_is_consumed_on_the_way_back(self):
        # Or a later manual reload would restore the filter too.
        res = self.page({"pt-period": "week", "pt-reloaded": "1"})
        self.assertNotIn("pt-reloaded", res["storage"])

    def test_a_jumpy_period_survives_when_its_start_did_not_move(self):
        starts = json.dumps({"session": 90})
        res = self.page({"pt-period": "session", "pt-reloaded": "1",
                         "pt-jumpy-starts": starts})
        self.assertEqual(res["applied"], "session")

    def test_a_jumpy_period_whose_start_moved_falls_back_to_all(self):
        # A new session started: same button, different hands. Forget it.
        starts = json.dumps({"session": 60})
        res = self.page({"pt-period": "session", "pt-reloaded": "1",
                         "pt-jumpy-starts": starts})
        self.assertEqual(res["applied"], "all")
        self.assertNotIn("pt-period", res["storage"])

    def test_the_jumpy_starts_are_recorded_for_the_next_reload(self):
        res = self.page({})
        self.assertEqual(json.loads(res["storage"]["pt-jumpy-starts"]),
                         {"session": 90})

    def test_a_period_that_vanished_falls_back_to_all(self):
        # periods() drops options as the history grows.
        res = self.page({"pt-period": "today", "pt-reloaded": "1"})
        self.assertEqual(res["applied"], "all")
        self.assertNotIn("pt-period", res["storage"])


class TestTheScriptsAgree(unittest.TestCase):
    """Static checks on contracts that span files, so they run without Node."""

    def test_jumpy_names_real_period_keys(self):
        # A renamed key in stats.periods() would leave JUMPY guarding nothing,
        # and every test above would still pass against its own fixture keys.
        jumpy = re.search(r"const JUMPY = \[([^\]]*)\]", report.JS).group(1)
        keys = re.findall(r"'([^']+)'", jumpy)
        self.assertEqual(keys, ["session", "today"])
        src = Path(stats.__file__).read_text(encoding="utf-8")
        for k in keys:
            self.assertIn(f'("{k}", ', src)

    def test_both_scripts_use_the_same_reload_flag(self):
        flag = re.compile(r"RELOADED = '([^']+)'")
        self.assertEqual(flag.search(report.JS).group(1),
                         flag.search(report.LIVE_JS).group(1))

    def test_the_flag_is_set_only_on_the_path_that_reloads(self):
        js = report.LIVE_JS
        self.assertEqual(js.count("setItem(RELOADED"), 1)
        self.assertEqual(js.count("location.reload()"), 1)
        after = js[js.index("setItem(RELOADED"):]
        self.assertLess(after.index("location.reload()"), 80,
                        "the reload flag must sit immediately before the reload")

    def test_the_live_poll_holds_while_the_solver_is_showing(self):
        js = report.LIVE_JS
        hold = js.index("tab-solver")
        self.assertLess(hold, js.index("location.reload()"))
        self.assertIn("'pt-tab'", js)
        self.assertIn("'pt-tab'", report.JS)


if __name__ == "__main__":
    unittest.main()
