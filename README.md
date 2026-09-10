# poker-tracker

A PokerStars hand-history tracker built around the idea that **frequencies and
compliance checks tell you something after a weekend, while results take a
year**. Parses hand histories into normalized SQLite tables, derives every stat
downstream, and writes a self-contained HTML dashboard.

Pure standard library plus numpy. No install step, no plotting library, no CDN.

## Use it

**Double-click `Poker tracker` on the Desktop.** The dashboard opens in your
browser and re-parses whatever PokerStars has written every 60 seconds on its
own; the toolbar says when it last looked. No terminal involved.

A console window stays open behind it — that is the server. Leave it open while
you use the dashboard; close it to stop. Launching a second time just reopens
the browser rather than complaining that the port is busy.

The shortcut points at `Poker tracker.cmd` in this folder, which is just a
wrapper around `serve` below.

### From a terminal

```bash
python -m pokertracker.cli serve     # dashboard that refreshes itself
python -m pokertracker.cli refresh   # or just regenerate report.html once
```

`serve` opens the dashboard on `http://127.0.0.1:8765/`, where it polls the
local server every 60 seconds. Each poll runs the same pipeline; when it finds
new hands the page reloads in place, preserving your scroll position, and the
toolbar carries the time of the last check either way. A `report.html` opened
directly from disk never refreshes, because a `file://` page cannot run the
parser.

`refresh` is the only command you otherwise need: it imports anything new from
`%LOCALAPPDATA%\PokerStars.RO\HandHistory`, recomputes the derived tables,
computes all-in EV for any new all-in hands, and writes `report.html`. Files
that have not changed since the last run are skipped, so re-running mid-session
is cheap.

Other commands:

| Command | What it does |
|---|---|
| `import` | Parse histories into `poker.db` (add `--force` to re-read unchanged files) |
| `derive` | Rebuild the derived flag table after changing a stat definition |
| `stats` | Print everything to the terminal |
| `stats --period session` | The same, restricted to one period (`all`, `session`, `day`, `week`, `n500`, `n1000`) |
| `stats --problems` | List hands the parser could not balance or understand |
| `ev` | Compute all-in EV only |
| `report -o out.html` | Write the dashboard only |
| `serve --port 8765` | Serve the dashboard, refreshing itself every 60s |

Useful flags: `--root` for a different history folder, `--db` for a different
database, `--hero` to override screen-name detection.

Run the tests with `python -m unittest discover -s tests`.

## How it is put together

```
parser.py     hand-history text -> Hand/Seat/Action/Result objects
positions.py  position derived from the button, never parsed
db.py         SQLite schema and idempotent loader; assigns session ids
derive.py     replays actions into one flag row per (hand, player)
stats.py      SQL aggregations, every rate carrying its own confidence interval
evaluator.py  vectorized 7-card evaluator (numpy, table-driven)
equity.py     all-in EV by exhaustive board enumeration, with side pots
charts.py     inline SVG, theme-aware
report.py     assembles the dashboard
```

The parser is deliberately dumb and the stat layer is separate. Parsing happens
once; everything else recomputes from the database in seconds, so a stat can be
redefined without re-reading 50,000 hands of text.

Session boundaries are computed once, when hands are imported, and stored on
the hand: a gap of more than 30 minutes starts a new session. Deriving them
inside a filtered query instead would let the definition of a session move
depending on what was loaded, so "last session" would mean a different set of
hands every time you refreshed. The period filter and the session list read the
same stored column and therefore cannot disagree.

`derive.py` is a Python replay rather than SQL because the denominators are
*sequential*. "Faced exactly one raise with the option to act" is a claim about
the action sequence at a moment in time, not about the final pot state — you
cannot fold to a 3-bet you never faced because a fourth player folded behind
first.

## Money and the two invariants

All money is stored as **integer cents**. Tournament chip counts are scaled by
100 too, so one integer type covers both.

**Every figure is a cash-game figure.** Tournament hands are imported and kept,
but excluded from all of them: a sit-and-go is a different game, and a handful
of its hands would otherwise sit inside the same bb/100 as thousands of hands of
micro-stakes cash. The report footer and `stats` say how many were left out.

Every hand is checked against two identities, and any failure is recorded in the
`problems` table rather than being silently swallowed:

```
sum(contributed)                            == total_pot
sum(collected) + rake + cashout_residual    == total_pot   (residual is 0
                                                            unless someone
                                                            cashed out)
```

**All-In Cash Out is the case that breaks the naive reading of the second one.**
A cash-out payment is an *insurance settlement from the provider*, not a share
of the pot, so it must never be added to `collected`. Two shapes occur, and both
are covered by fixtures:

*The winner cashes out.* PokerStars emits no `collected` line at all; the player
forfeits the pot to the provider and is paid a settlement instead.

```
Total pot $2.81 | Rake $0.14
Insurer cashed out the hand for $2.52 | Cash Out Fee $0.05
```

Nothing is collected, so the residual is the whole net pot, $2.67. The player
received $2.52 of that — $0.05 declared fee plus a $0.10 provider spread.

*A loser cashes out.* The winner still collects the entire pot as normal, and
the losing player is paid anyway:

```
Total pot $2.33 | Rake $0.12
Winner collected $2.21 from pot
Insurer cashed out the hand for $1.20 | Cash Out Fee $0.02
```

`2.21 + 0.12 = 2.33` balances exactly, so the residual is zero — and the player
who lost $1.15 in the pot still books a $0.05 profit on the hand. Adding the
settlement to `collected` would double-count the pot here.

So the model is: `cashout_residual = total_pot - rake - sum(collected)`, which
is the share forfeited to the provider; it must be zero when nobody cashed out
and never negative. Settlements live in `results.cashout_received`, and
`net = collected + cashout_received - contributed`. Only the real rake is spread
across players — the fee and the spread are borne by whoever cashed out and are
already in their net.

## Definitions worth writing down

These are the choices you will otherwise re-derive wrong in six months.

- **"Won money"** (WWSF, W$SD) means *collected any part of the pot*, even if
  the hand was net negative after your own contribution.
- **`Action.amount`** is the chips a player *added*. For calls and bets that
  equals the printed number; for raises it does not. A big blind holding $0.02
  who "raises $0.04 to $0.10" adds $0.08. **`to_amount`** is always the player's
  total street commitment afterwards, which is the figure every raise-size and
  3-bet rule actually needs.
- **Bet level**: 0 unopened, 1 open, 2 three-bet, 3 four-bet. Blind posts never
  raise the level, so the first voluntary raise is the open even after limpers.
- **The preflop aggressor** is the last preflop raiser. A limped pot has no PFA
  and therefore no c-bet opportunity for anybody.
- **A dead small blind** (`posts small & big blinds $0.03`) contributes 3c to
  the pot but only the 2c big blind is live money, so calling a raise to 6c
  costs 4c.
- **Rake per player is an estimate.** PokerStars reports rake per pot. It is
  attributed in proportion to contribution; charging it all to the winner would
  make positional win rates meaningless.
- **A fold can show cards** (`Slosh419: folds [2h Qh]`). Those cards are
  captured, not discarded.
- **Position is by seat, never by the blind posted.** A player posting a dead
  blind out of position keeps the position their seat gives them.

## Position derivation

The highest-risk logic in the tracker, because a wrong answer still looks
plausible. Labels come from **distance back from the button** — never from seat
number — so short tables truncate correctly:

| Dealt in | Ladder |
|---|---|
| 6 | BTN, CO, HJ, UTG + SB, BB |
| 5 | BTN, CO, HJ + SB, BB (no UTG) |
| 4 | BTN, CO + SB, BB (no HJ) |
| 2 | BTN (posts the small blind, acts first preflop), BB |

Short-handed tables are the common case, not the exception. Empty seats, players
sitting out, players who joined mid-hand and anyone "allowed to play after the
button" are not dealt in; the summary block is the authority on that, because it
lists every seat but only gives a verb to the ones that were dealt.

The fixtures in `tests/fixtures/` cover exactly the shapes that break this:
six-handed, five-handed with a gap in the seating, four-handed, heads-up, a
second live big blind, and a dead small blind.

## All-in EV

At every hand that got all-in with cards to come, each player's result is
replaced by equity × pot. Enumeration is **exhaustive**, not sampled — C(48,5)
is 1.7M boards for two players preflop, which the chunked numpy evaluator
handles in a few seconds.

Side pots are computed per layer, using only the players eligible for that
layer. Money from players who folded is dead money and lands in the lowest
layers. Rake, the cash-out fee and the cash-out residual all come off before
anyone is paid, so EV is measured against the net pot.

The evaluator is validated in `tests/test_evaluator.py` against an independent
brute-force implementation (best five of seven by enumeration, tuple
comparison), checking that both agree on the *ordering* of every pair — and the
equity numbers themselves are pinned to known references.

One thing worth knowing: AA vs KK preflop is **81.3%** when all four suits are
distinct. The commonly quoted 82.4% is a different suit configuration; the
all-distinct case is the worst one for the aces because it leaves both of the
kings' suits fully live.

## Reading the dashboard

The four-line winnings chart is the reason trackers exist. The rest supports it.

- **Red line** is not "profit from bluffing". It is every pot taken down
  uncontested minus every chip lost in a hand that never reached showdown. A
  slightly negative red line is normal at micro stakes; a sharply negative one
  usually means you are opening and c-betting into people who never fold.
- **WWSF / WTSD / W$SD** form a diagnostic triangle. Low WWSF with high WTSD is
  too passive. High WTSD with low W$SD is calling too wide on later streets.
  High WWSF with low WTSD is fine, but check the red line supports it.
- **Pot-size buckets** are the fastest read in the report. Losses concentrated
  in the top bucket mean the problem is stack-off decisions, not preflop ranges.
- **The period selector** at the top filters the tables, the frequencies and
  the compliance checks, and adds a comparison against everything before the
  selected window. It answers "is the thing I am working on moving", which is
  the only question available on a given day: `Donk Flop 32.2% selected, 38.5%
  prior, -6.3`. It never defaults to anything but All.

  Two things it deliberately does not do. It does not show a win rate while a
  period is selected — over one session that number carries an interval of
  roughly +/-150 bb/100, so displaying it would only invite you to read it — and
  it does not show a delta where either side has fewer than 30 opportunities,
  because the difference between two noisy rates is noisier than either. A
  compliance check with too few opportunities reads **n too small** rather than
  failing: 0 button opens out of 2 is silence, not a broken plan.

  Compliance counts also carry a rate per 100 hands and a prior-period figure,
  because a cumulative counter only goes up. Once "SB cold calls" reads 11 it
  reads 11 forever, however well you play afterwards; "0 this session, 11
  before" is the version that can improve.

  The cumulative winnings chart, the rolling-discipline chart and the session
  list always show the whole history, with the selected period shaded — a
  single session on its own axis tells you nothing except where it sits. The
  13x13 grid splits into selected and prior panels on a shared colour scale, so
  range drift is visible directly.

- **Biggest pots** is where every other section ends up. Each aggregate finding
  is really an instruction to go and look at some hands, and this is the list:
  the fifteen largest losses and fifteen largest wins by net big blinds, with
  position, hole cards, board, exit street, outcome and whatever the opponent
  showed. Any column re-sorts, and clicking a hand number opens the original
  PokerStars text inline. The board is blank when the hand was folded preflop,
  because cards that arrived after the fold are not part of the decision.

Every displayed rate carries a 95% confidence interval, and the win rate carries
one prominently, because at 10,000 hands a measured +5 bb/100 is statistically
indistinguishable from −13.

| Stat | Usable at |
|---|---|
| VPIP, PFR | 1,000–2,000 hands |
| 3-Bet, ATS, flop C-Bet | 3,000–5,000 |
| Fold to 3-Bet, WWSF, WTSD | 5,000–10,000 |
| Turn/river stats, W$SD | 15,000+ |
| bb/100 | 100,000+ |

Which is the argument for judging yourself on the compliance checks — SB cold
calls, SB limps, BTN open percentage — rather than on the graph. Those answer
whether you executed the plan, which is a different question from whether the
plan is good, and a much more useful one early on.

Edit `TARGETS` in `report.py` to match the strategy you are actually running;
the rolling-discipline chart draws them as reference lines.

## Chart colours

Palette assignments were checked with a colour-vision-deficiency validator
rather than chosen by eye. The winnings chart's green/red pair sits in the CVD
warning band, so each line also carries a distinct stroke pattern and a direct
label — identity is never carried by colour alone. The report follows the
reader's light/dark theme.
