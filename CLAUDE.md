# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A PokerStars hand-history tracker: parses hand histories into normalized SQLite
tables, derives stats downstream, and renders a self-contained HTML dashboard.
Standard library plus numpy. No install step, no build step, no plotting
library, no CDN.

## Commands

```bash
python -m pokertracker.cli refresh   # import + derive + EV + write report.html
python -m pokertracker.cli serve     # same, served on :8765 with a Refresh button
python -m pokertracker.cli stats     # everything to the terminal
python -m pokertracker.cli stats --problems   # parse failures; see "Problems" below
python -m pokertracker.cli import --force     # re-read files the mtime check skipped
python -m pokertracker.cli derive    # rebuild derived stats only (no re-parse)

python -m unittest discover -s tests                          # all tests (~20s)
python -m unittest tests.test_tracker                         # one module
python -m unittest tests.test_tracker.TestPositions           # one class
python -m unittest tests.test_equity.TestEquity.test_aces_versus_kings_preflop
```

Run everything from the project root. `test_equity` is the slow one — it
enumerates C(48,5) boards for real.

Flags: `--root` (history folder), `--db`, `--hero` (overrides auto-detection),
`--port`, `--no-open`.

## Pipeline

Text flows one way, and each stage is rebuildable from the one before it
without redoing the stage before that:

```
hand histories (.txt)
  parser.py     -> Hand / Seat / Action / Result objects, invariants asserted
  positions.py  -> Seat.position, derived from the button
  db.py         -> hands / seats / actions / results (integer cents)
  derive.py     -> hand_player: one flag row per (hand, player)
  stats.py      -> SQL aggregations, every rate carrying its own CI
  charts.py     -> inline SVG
  report.py     -> the dashboard
```

The parser is deliberately dumb: it computes no statistics and derives nothing
beyond what the text literally says. That separation is the point — parsing
happens once, and everything downstream recomputes in seconds, so a stat
definition can change without re-reading the text.

**`derive.py` is a Python replay, not SQL, and that is deliberate.** The
preflop denominators are *sequential*: "faced exactly one raise with the option
to act" is a claim about a moment in the action sequence, not about the final
pot state. You cannot fold to a 3-bet you never faced because a fourth player
folded behind first. It still reads only from the `actions` table, so
`cli derive` rebuilds it without touching text.

## Invariants and problems

Every hand is checked on parse. Failures go into the `problems` table rather
than raising:

```
sum(contributed)                          == total_pot
sum(collected) + rake + cashout_residual  == total_pot
```

`stats --problems` is the early-warning system, and it works: it is how the
`folds [2h Qh]` and cash-out bugs were found. **A non-empty problems table
means a parser bug, not noise.** PokerStars keeps introducing line formats;
unrecognised lines are reported rather than swallowed, which is why
`NOISE_FRAGMENTS` in `parser.py` is an explicit list and not a catch-all regex.

`TestLiveHistory` re-parses the real history folder and fails if any hand stops
balancing, so the suite doubles as a format-regression check. It skips when the
folder is absent.

## Things that will bite you

**Money is integer cents everywhere.** Tournament chip counts are scaled by 100
too so one integer type covers both. Never introduce a float into the money
path.

**`Action.amount` vs `to_amount`.** `amount` is the chips a player *added*. For
calls and bets that equals what PokerStars prints; for raises it does not,
because PokerStars prints the increment over the previous bet level. A big
blind holding $0.02 who "raises $0.04 to $0.10" adds $0.08. `to_amount` is the
player's total street commitment after acting, and is what every raise-size and
3-bet rule needs.

**A cash-out is an insurance settlement, not a share of the pot.** Never add it
to `collected`. Two shapes occur and both have fixtures:
- The *winner* cashes out: no `collected` line is emitted at all, the pot is
  forfeited to the provider, and `cashout_residual` is the whole net pot.
- A *loser* cashes out: the winner collects the full pot as normal, the residual
  is zero, and the losing player still books a profit.

So `cashout_residual = total_pot - rake - sum(collected)`, settlements live in
`results.cashout_received`, and `net = collected + cashout_received -
contributed`. Only the real rake is spread across players.

**Position is derived from the button, never parsed, and never from the blind
posted.** Labels come from distance back from the button so short tables
truncate correctly: 6-handed is BTN/CO/HJ/UTG plus the blinds, 5-handed drops
UTG, 4-handed drops HJ. Heads-up the button *is* the small blind and acts first
preflop. A dead blind posted out of position does not move anyone's position.
This is the highest-risk logic in the codebase because a wrong answer still
looks plausible — every table shape that breaks it has a fixture.

**Dealt-in comes from the summary block, not the seat lines.** The summary lists
every seat but only gives a verb to seats that were dealt. Seat-line flags are
the fallback, and a disagreement is recorded as a problem.

**Rake per player is an estimate** — PokerStars reports it per pot. It is spread
in proportion to contribution.

**Schema changes need a DB rebuild.** `db.SCHEMA` uses `CREATE TABLE IF NOT
EXISTS`, so a new column will not appear in an existing `poker.db`. There are no
migrations: delete `poker.db` (plus `-wal`/`-shm`) and re-import. Re-parsing is
fast and the histories are the source of truth.

## Conventions worth preserving

Definitions that are easy to re-derive wrong are written down in comments at the
point of use, not just in the README:

- "Won money" (WWSF, W$SD) means *collected any part of the pot*, even when the
  hand was net negative.
- Bet level: 0 unopened, 1 open, 2 three-bet, 3 four-bet. Blind posts never
  raise the level, so the first voluntary raise is the open even after limpers.
- The preflop aggressor is the *last* preflop raiser; a limped pot has no PFA
  and therefore no c-bet opportunity for anyone.
- An opportunity is counted at most once per hand per player.

## Evaluator and equity

`evaluator.py` scores 7-card hands as `category << 20 | tiebreak`, where the
tiebreak packs up to five ranks into five nibbles as rank+1 so "absent" is 0.
Scores are comparable only against each other. It is table-driven and
vectorized; `evaluate()` takes an `(N, 7)` array.

It is validated in `test_evaluator.py` against an independently written
brute-force reference (best five of seven, tuple comparison) on *ordering*, not
on values. Keep that property if you change the encoding.

`equity.py` enumerates exhaustively, not by sampling, chunked through numpy.
Side pots are computed per layer using only that layer's contenders; money from
folded players is dead money in the lowest layers.

Note for sanity checks: **AA vs KK preflop is 81.3% with all four suits
distinct.** The widely quoted 82.4% is a different suit configuration. Test
expectations here were wrong once; the code was not.

## Report

`report.build(conn, hero, live=False)`. `live=True` adds the Refresh toolbar and
its JavaScript, and is used only by `serve.py` — a `file://` page cannot run the
parser, so the on-disk `report.html` deliberately has no button.

Charts are hand-written SVG referencing CSS custom properties, so the whole
report is one file that follows the reader's light/dark theme. Colours were run
through the dataviz skill's palette validator rather than chosen by eye; the
winnings chart's green/red pair sits in the colour-vision-deficiency warn band,
so each line also carries a distinct stroke pattern and a direct label. Do not
add a series whose identity rests on hue alone, and re-run the validator if you
change the palette.

`TARGETS` at the top of `report.py` are strategy reference lines drawn on the
rolling-discipline chart, not measurements.

**Raw hand text is not stored in the database.** The histories are the source of
truth and duplicating them would roughly double `poker.db` for no gain, so
`db.hand_texts()` re-reads the source file and re-splits it on demand. That is
only worth doing for the few dozen hands the biggest-pots drill-down shows;
never call it over a whole result set. A source file that has since moved
yields no text rather than an error, and the report says so in the panel.

## Sample size

The report attaches a confidence interval to every displayed rate, and the
sample-size section exists to stop the reader acting on noise. Win rate needs
~100k hands to mean anything; VPIP/PFR settle after 1–2k. When adding a stat,
carry its numerator and denominator through to the display rather than a bare
percentage — `stats.Rate` exists for this.
