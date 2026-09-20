---
name: review
description: Run a poker review against the live hand-history database - re-measure the open findings in the review journal, look for new leaks, and record what you find. Use when the user asks for a review, a coaching session, a leak check, "how am I doing", or whether something they have been working on has improved.
---

# Reviewing the hero's play

You are the coach. The database is right there, so do not review a screenshot
of the report — query the thing itself.

## 1. Refresh, then read

```bash
python -m pokertracker.cli refresh          # import + derive + EV, and keeps report.html current
python -m pokertracker.cli export -o /tmp/brief.json
python -m pokertracker.cli review status
```

`export` writes the whole dashboard as JSON, including a `tracked` section
holding what `review status` prints. `--period <key>` narrows it; the keys the
history supports are listed in the brief under `periods_available`. `--grid`
adds the 169-cell preflop range grid, which is worth asking for only when the
review is about opening ranges.

If `refresh` reports parse problems, say so and stop treating the numbers as
final: a non-empty problems table is a parser bug, not noise.

## 2. Lead with the open findings, and be honest about them

The journal is the reason this is a review and not a fresh diagnosis. Start
there, one line each: what was diagnosed, what it read then, what it reads now
**over the hands played since**, and the verdict.

The verdicts mean exactly what they say:

| verdict | what it means |
|---|---|
| `ok` | inside the target band over the hands since |
| `off` | outside it |
| `watch` | no band was set, because there is no defensible one — read the money instead |
| `thin` | fewer than `MIN_OPPS` (30) opportunities since; **the window cannot answer the question** |

`thin` is not a soft "no better". Say "not enough hands yet" and move on.
Telling somebody they have not improved when what actually happened is that
they played forty hands is the single worst thing this loop can do.

`moved` is a separate field from `verdict` and both are worth reporting: a rate
that went 71% → 63% against a 60% band is still off plan *and* is also the
thing that is working. Say both.

## 3. Then look for what is new

Work from the brief. The sections that carry the most signal:

- `made_hands` — three pot ranges side by side. The `no pair` row split into
  naked and drawing is usually where the money is; `naked_by_street` says
  whether it went in on the flop (stop betting) or the river (start folding),
  and `dominant_street` only names a street that holds an outright majority.
- `compliance` — did they execute the plan, which is a different question from
  whether the plan is good.
- `preflop` / `postflop` with `prior` beside them when a period is selected.
- `street_funnel`, `by_position`, `big_pots`.

Then go and look at the actual hands:

```bash
python -m pokertracker.cli hands --metric "class:no pair@40-" --invested --limit 5 --text
python -m pokertracker.cli hands --metric stat:fold_to_3bet --matched --limit 5
```

`--metric` takes `stat:<flag>` or `class:<bucket>[@lo-hi]`. `--text` prints the
original history and refuses above 25 hands, because the raw text is not stored
and every hand printed is a file read.

Anything the brief does not answer, query directly — the schema is in
`pokertracker/db.SCHEMA` and every stat is a callable:

```bash
python -c "
from pokertracker import db, stats
conn = db.connect('poker.db'); hero = db.detect_hero(conn)
print(stats.by_position(conn, hero))"
```

## 4. Rules you do not get to bend

- **Never read a win rate over a short window.** It needs ~100k hands; a
  session is ±150 bb/100 of noise. Frequencies are what a short window is for.
  The report suppresses the win-rate tile under a filter for this reason.
- **Every rate comes with its denominator.** Below `MIN_OPPS` (30) the answer
  is "thin", not a number with a caveat attached.
- **Below `MIN_CLASS_HANDS` (5) a made-hand bucket states its money but not a
  diagnosis.** The loss is real; the explanation is one cooler.
- **Cash games only.** Tournament hands are excluded from every figure and
  `tournaments_excluded` says how many.
- Do not invent thresholds. The ones that exist are in the brief's `constants`.

## 5. Close the loop

A review that does not get written down is a review that happens again from
scratch next month.

```bash
python -m pokertracker.cli review add \
  --title "Second-barrelling turn with no pair" \
  --metric "class:no pair@15-40" \
  --target "" \
  --note "37% of the naked money goes in on the turn in medium pots."

python -m pokertracker.cli review close 2026-09-20-1 --note "Held under 60% over 2,400 hands."
```

- `--metric` is `stat:<flag>`, `check:<key>`, `class:<bucket>[@lo-hi]` or
  `money:bb100`. Run `review add --help`; an unknown id is rejected on the spot
  and lists the valid ones.
- `--target` is a band: `60`, `<=60`, `>=8`, `38-52`. **Leave it empty when
  there is no defensible band.** Inventing a percentage to make a finding look
  rigorous dresses a guess up as a standard — the verdict becomes `watch` and
  the note carries the money, which is the part that argues.
- Keep the journal short. Three or four open findings is a plan; twelve is a
  list nobody works through.
- Record a finding *once*. Before adding, check `review list` for one that
  already says it.
