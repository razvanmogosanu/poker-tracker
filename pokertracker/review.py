"""The review journal: what was diagnosed, and whether it has moved since.

Everything else in this project is rebuildable from the hand histories, which
is why the documented cure for a schema change is to delete `poker.db` and
re-import. Review findings are the exception: a judgement about how somebody
is playing cannot be re-derived from the text it was made about. So the
journal lives in its own file, outside the database, in git -- because the
first person to add a column would otherwise delete the entire coaching
history by following the instructions.

It is a log of events rather than a table of state, for the same reason the
histories are: a close is a thing that happened on a date, and replaying the
log gives both the current status and how it got there. Two shapes, one per
line:

    {"op": "open",  "id": ..., "metric": ..., "boundary": [...], ...}
    {"op": "close", "id": ..., "closed": ..., "note": ...}

The load-bearing field is `boundary` -- the last hand played when the finding
was recorded. Progress is measured over the hands played *since*, never over
the whole history: re-measuring a corrected leak across everything mixes the
bad decisions back in with the corrections, the rate barely moves, and the
reader concludes nothing changed when it did. It is the same reasoning that
makes `stats.Period` pair a selected window with the prior one rather than
with the total.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import brief, stats

DEFAULT_JOURNAL = Path(__file__).resolve().parents[1] / "reviews.jsonl"


class JournalError(RuntimeError):
    """A journal that does not say what it appears to say.

    Reported, never skipped -- the same posture as the `problems` table. A
    half-written line is a bug or a crashed write, and silently dropping it
    would lose a finding without anybody noticing.
    """


# --------------------------------------------------------------------------
# targets
#
# A target has to be machine-comparable or the verdict is just the model's
# opinion again. The grammar is small on purpose and anything outside it is
# rejected at the moment a finding is written, rather than producing a verdict
# nobody can reproduce.

def parse_target(text: str | None) -> tuple[float | None, float | None] | None:
    """"<=60" -> (None, 60); ">=8" -> (8, None); "38-52" -> (38, 52); "0" -> (0, 0).

    None means no band, which is a legitimate answer and not a missing one:
    the 8-outs check has no defensible percentage either, and the status it
    gets is `watch`.
    """
    if text is None:
        return None
    t = text.strip().replace(" ", "")
    if not t:
        return None
    try:
        if t.startswith("<="):
            return (None, float(t[2:]))
        if t.startswith(">="):
            return (float(t[2:]), None)
        if t.startswith("<"):
            return (None, float(t[1:]))
        if t.startswith(">"):
            return (float(t[1:]), None)
        if t.startswith("="):
            v = float(t[1:])
            return (v, v)
        if "-" in t[1:]:
            lo, _, hi = t[1:].rpartition("-")
            return (float(t[0] + lo), float(hi))
        v = float(t)
        return (v, v)
    except ValueError:
        raise JournalError(
            f"cannot read target {text!r}; expected one of: 60, <=60, >=8, "
            f"38-52, or nothing at all for a leak with no defensible band")


def _distance(value: float, band: tuple[float | None, float | None]) -> float:
    """How far outside the band a value sits; 0 when it is inside."""
    lo, hi = band
    if lo is not None and value < lo:
        return lo - value
    if hi is not None and value > hi:
        return value - hi
    return 0.0


# --------------------------------------------------------------------------
# the log

def events(path: str | Path = DEFAULT_JOURNAL) -> list[dict]:
    """Every line, in the order it was written. Missing file means no events."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise JournalError(f"{p}:{i}: not valid JSON ({e})")
        if not isinstance(rec, dict) or "op" not in rec or "id" not in rec:
            raise JournalError(f"{p}:{i}: every line needs an 'op' and an 'id'")
        out.append(rec)
    return out


def load(path: str | Path = DEFAULT_JOURNAL) -> list[dict]:
    """Replay the log into the current findings, oldest first."""
    findings: dict[str, dict] = {}
    for rec in events(path):
        op, fid = rec["op"], rec["id"]
        if op == "open":
            findings[fid] = {**rec, "status": "open",
                             "closed": None, "closed_note": ""}
        elif op == "close":
            if fid not in findings:
                raise JournalError(
                    f"close for {fid!r}, which was never opened")
            findings[fid].update(status="closed", closed=rec.get("closed"),
                                 closed_note=rec.get("note", ""))
        else:
            raise JournalError(f"unknown op {op!r} for {fid!r}")
    return list(findings.values())


def _append(path: str | Path, record: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _next_id(findings: list[dict], today: str) -> str:
    n = 1 + sum(1 for f in findings if f["id"].startswith(today))
    return f"{today}-{n}"


def add(conn: sqlite3.Connection, hero: str, title: str, metric: str,
        target: str | None = None, note: str = "",
        path: str | Path = DEFAULT_JOURNAL,
        now: datetime | None = None) -> dict:
    """Record a finding, stamping what the metric reads right now.

    The metric is resolved before anything is written, so a typo fails here
    rather than months later when the finding is re-measured.
    """
    parse_target(target)          # validate now, not at read time
    at = now or datetime.now()
    measured = brief.resolve(conn, hero, metric, None)
    boundary = stats.last_hand(conn, hero)
    if boundary is None:
        raise JournalError(
            "no cash hands for hero; there is nothing to measure a finding "
            "against yet")
    rec = {
        "op": "open",
        "id": _next_id(load(path), at.date().isoformat()),
        "opened": at.isoformat(timespec="seconds"),
        "title": title,
        "metric": metric,
        "target": target or "",
        "note": note,
        "value_at_open": measured["value"],
        "num_at_open": measured["num"],
        "den_at_open": measured["den"],
        "unit": measured["unit"],
        # The last hand played, as (played_at, hand_id) -- the pair, because
        # multi-tabling puts several hands on one second and a boundary broken
        # arbitrarily would let a hand fall on both sides of it.
        "boundary": list(boundary),
    }
    _append(path, rec)
    return rec


def close(finding_id: str, note: str = "",
          path: str | Path = DEFAULT_JOURNAL,
          now: datetime | None = None) -> dict:
    findings = {f["id"]: f for f in load(path)}
    if finding_id not in findings:
        still_open = [f["id"] for f in findings.values() if f["status"] == "open"]
        raise JournalError(f"no finding {finding_id!r}; open ones are "
                           f"{', '.join(still_open) or 'none'}")
    if findings[finding_id]["status"] == "closed":
        raise JournalError(f"{finding_id} is already closed")
    rec = {"op": "close", "id": finding_id,
           "closed": (now or datetime.now()).isoformat(timespec="seconds"),
           "note": note}
    _append(path, rec)
    return rec


# --------------------------------------------------------------------------
# progress

def status(conn: sqlite3.Connection, hero: str,
           path: str | Path = DEFAULT_JOURNAL,
           include_closed: bool = False) -> list[dict]:
    """Re-measure every open finding over the hands played since it was made.

    The verdict vocabulary is the one `stats.compliance()` already uses, and
    `thin` carries the same weight here that it does there: it says the hands
    played since cannot answer the question. That is not a softer version of
    "no better", and reporting it as one is how a coaching loop starts telling
    somebody they have not improved when what actually happened is that they
    played forty hands.
    """
    out = []
    for f in load(path):
        if f["status"] != "open" and not include_closed:
            continue
        w = stats.window_after(conn, hero, f["boundary"], "Since diagnosis")
        try:
            now = brief.resolve(conn, hero, f["metric"], w)
        except brief.UnknownMetric as e:
            # A metric that no longer resolves is a real event -- a renamed
            # check, a dropped stat -- and belongs in the output rather than
            # taking the whole status listing down with it.
            out.append({**_head(f), "hands_since": w.hands, "error": str(e)})
            continue
        band = parse_target(f.get("target"))
        then_v, now_v = f["value_at_open"], now["value"]

        if now["den"] < stats.MIN_OPPS:
            verdict = "thin"
        elif band is None:
            verdict = "watch"
        elif _distance(now_v, band) == 0:
            verdict = "ok"
        else:
            verdict = "off"

        # Whether it moved is a separate question from whether it is fixed, and
        # both are worth saying: a rate that went 71% -> 63% against a 60% band
        # is still off plan and is also the thing working.
        if band is None or then_v is None or now_v is None:
            moved = None
        else:
            before, after = _distance(then_v, band), _distance(now_v, band)
            moved = ("closer" if after < before
                     else "further" if after > before else "same")

        out.append({
            **_head(f),
            "hands_since": w.hands,
            "then": {"value": then_v, "num": f["num_at_open"],
                     "den": f["den_at_open"], "at": f["opened"]},
            "now": {"value": now_v, "num": now["num"], "den": now["den"],
                    "ci_lo": now.get("ci_lo"), "ci_hi": now.get("ci_hi")},
            "delta": None if (then_v is None or now_v is None) else now_v - then_v,
            "verdict": verdict,
            "moved": moved,
        })
    return out


def _head(f: dict) -> dict:
    return {"id": f["id"], "title": f["title"], "metric": f["metric"],
            "target": f.get("target", ""), "unit": f.get("unit", ""),
            "opened": f["opened"], "note": f.get("note", ""),
            "status": f["status"]}
