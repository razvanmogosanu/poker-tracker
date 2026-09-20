---
name: quiz
description: Quiz the hero on their own hands - replay real spots from the hand history up to the decision point, take an answer, then reveal what they actually did and what it cost. Use when the user asks to be quizzed, tested, drilled, or to practise the leaks they are working on.
---

# Quizzing the hero on their own hands

The hands are real and they are theirs. That is the whole point: a spot they
misplayed three days ago argues better than any constructed example, because
the answer they gave once is on record.

## Pick the spots from what they are working on

```bash
python -m pokertracker.cli review status     # what is open
python -m pokertracker.cli hands --metric "class:no pair@40-" --invested --limit 6 --text
```

Draw from the open findings first — a quiz is practice for the leak, not a
general trivia round. If the journal is empty, run `/review` first, or pick
from the biggest losses in `export`'s `big_pots`.

`--metric` takes `stat:<flag>` (e.g. `stat:fold_to_3bet`, `stat:cbet_turn`) or
`class:<bucket>[@lo-hi]`. Add `--matched` / `--unmatched` to split on whether
the stat fired, `--invested` to keep only hands with postflop money in them,
and `--period` to stay recent. `--text` prints the original history and is
capped at 25 hands.

## Run it one hand at a time

1. **Show the spot up to the decision, and not one line further.** Stacks,
   positions, their hole cards, the action so far, the board as of that
   street. Cut the history at the point they had to act. Everything after it —
   the rest of the street, the runout, the showdown, the net — is the answer.
2. **Ask for an action and a size**, and for one sentence on why. The reason is
   the part being trained.
3. **Then reveal**: what they actually did, how the hand finished, and what it
   cost or won.
4. **Say which of the three answers matters.** What they just said, what they
   did at the table, and what the spot is worth are three different things,
   and the interesting case is when the first two disagree.

Do not stack six hands into one message. One spot, one answer, then the next.

## Keep it honest

- **A single hand is not evidence of a leak**, and the reverse: playing a spot
  well in a quiz does not close a finding. Findings close on
  `review status`, over hands played — never on a quiz answer.
- **Do not reveal the result early**, including through tone. "Are you sure?"
  is a tell.
- **Results-oriented scoring is the failure mode.** A correct fold that would
  have won is still correct; a call that got there is still a call. Judge the
  decision at the moment it was made — which is the same rule the made-hand
  buckets follow, measuring a hand at the street the player left it on rather
  than against a board that had not been dealt yet.
- **The board in the text runs out even when they folded.** If they folded the
  flop, the turn and river in the history were never part of their decision.
  Do not show them, and do not score against them.
- If they ask how they are doing overall, that is `/review`, not this.
