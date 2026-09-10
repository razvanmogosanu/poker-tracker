"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db, derive, stats

DEFAULT_ROOT = Path.home() / "AppData/Local/PokerStars.RO/HandHistory"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "poker.db"


def cmd_import(args) -> int:
    conn = db.connect(args.db)
    paths = db.discover(args.root)
    if not paths:
        print(f"no .txt files under {args.root}", file=sys.stderr)
        return 1
    print(f"scanning {len(paths)} file(s) under {args.root}")
    res = db.import_paths(conn, paths, hero_hint=args.hero or "", force=args.force)
    print(f"  files read: {res['files']}  skipped (unchanged): {res['skipped']}")
    print(f"  hands imported: {res['hands']}")
    n = derive.rebuild(conn)
    print(f"  derived rows: {n}")
    bad = stats.problem_count(conn)
    if bad:
        print(f"  ! {bad} problem(s) recorded; see `stats --problems`")
    hero = db.detect_hero(conn)
    print(f"  hero: {hero}")
    return 0


def cmd_refresh(args) -> int:
    """Import, re-derive, compute EV and write the report in one go."""
    rc = cmd_import(args)
    if rc:
        return rc
    conn = db.connect(args.db)
    from . import equity, report

    n = equity.compute_all(conn, force=False, progress=True)
    if n:
        print(f"  all-in EV rows: {n}")
    hero = args.hero or db.detect_hero(conn)
    out = Path(args.output)
    out.write_text(report.build(conn, hero), encoding="utf-8")
    print(f"  report: {out.resolve()}")
    return 0


def cmd_serve(args) -> int:
    from . import serve as serve_mod

    conn = db.connect(args.db)
    hero = args.hero or db.detect_hero(conn)
    conn.close()
    serve_mod.serve(args.db, args.root, hero, args.port, not args.no_open)
    return 0


def cmd_derive(args) -> int:
    conn = db.connect(args.db)
    n = derive.rebuild(conn)
    print(f"rebuilt {n} hand_player rows")
    return 0


def _fmt_pct(v, ci=None) -> str:
    if v is None:
        return "     -"
    s = f"{v:5.1f}%"
    if ci and ci[0] is not None:
        s += f"  [{ci[0]:4.1f}-{ci[1]:4.1f}]"
    return s


def _fmt_delta(new, old, unit: str, enough: bool) -> str:
    if not enough or new is None or old is None:
        return "        "
    return f"{new - old:+7.1f}{unit}"


def cmd_stats(args) -> int:
    conn = db.connect(args.db)
    hero = args.hero or db.detect_hero(conn)
    if not hero:
        print("no hands imported yet", file=sys.stderr)
        return 1

    if args.problems:
        for p in stats.problems(conn, 200):
            print(f"{p['site_hand_no']}: {p['problem']}")
        return 0

    available = stats.periods(conn, hero)
    period = next((x for x in available if x.key == args.period), None)
    if period is None:
        print(f"unknown period {args.period!r}; available: "
              f"{', '.join(x.key for x in available)}", file=sys.stderr)
        return 1
    w = period.selected
    prior = None if period.is_all else period.prior

    m = stats.money_summary(conn, hero, w)
    if not m.get("hands"):
        print("no hands for hero", hero, file=sys.stderr)
        return 1

    print(f"\nHero: {hero}   {m['hands']} hands   {m['first_hand']} .. {m['last_hand']}")
    tourn = stats.excluded_tournaments(conn, hero)
    if tourn["hands"]:
        # Said out loud, so the hand count here can be reconciled with the
        # number of hands sitting in the history folder.
        print(f"Cash games only: {tourn['hands']} tournament hands "
              f"({tourn['events']} events) excluded")
    if prior is not None:
        print(f"Period: {period.label}   compared against the "
              f"{prior.hands} hands before it")
    print("-" * 68)
    # Win rate over a filtered window is noise with a decimal point on it, and
    # the report suppresses it outright for that reason. Here it is kept but
    # labelled, because a terminal reader asked for exactly this window.
    print(f"  Win rate        {m['bb100']:+8.2f} bb/100   +/- {m['ci95']:.1f} (95% CI)")
    if m["has_ev"]:
        print(f"  All-in adj. EV  {m['ev_bb100']:+8.2f} bb/100   "
              f"(luck {m['ev_diff_bb100']:+.2f})")
    print(f"  Showdown        {m['showdown_bb100']:+8.2f} bb/100  (blue line)")
    print(f"  Non-showdown    {m['nonshowdown_bb100']:+8.2f} bb/100  (red line)")
    print(f"  Rake paid       {m['rake_bb100']:8.2f} bb/100   "
          f"(gross {m['gross_bb100']:+.2f})")
    print(f"  Std deviation   {m['sd_bb100']:8.1f} bb/100")

    for label, fn in (("Preflop", stats.preflop_stats),
                      ("Postflop", stats.postflop_stats)):
        print(f"\n{label}")
        print("-" * 68)
        old = {x["name"]: x for x in fn(conn, hero, prior)} if prior else {}
        for r in fn(conn, hero, w):
            ci = (r["ci_lo"], r["ci_hi"])
            line = f"  {r['name']:<20} {_fmt_pct(r['pct'], ci)}   n={int(r['den'])}"
            if prior:
                o = old.get(r["name"], {"pct": None, "den": 0})
                enough = (r["den"] >= stats.MIN_OPPS
                          and o["den"] >= stats.MIN_OPPS)
                prev = "     -" if o["pct"] is None else f"{o['pct']:5.1f}%"
                line += (f"   prior {prev}"
                         f"  {_fmt_delta(r['pct'], o['pct'], '', enough)}")
            print(line)
    ag = stats.aggression(conn, hero, w)
    if ag["afq"] is not None:
        print(f"  {'Aggression Freq':<20} {ag['afq']:5.1f}%")
        print(f"  {'Aggression Factor':<20} "
              f"{ag['af']:5.2f}" if ag["af"] else "  Aggression Factor      -")

    print("\nBy position")
    print("-" * 68)
    print(f"  {'POS':<5}{'HANDS':>7}{'VPIP':>8}{'PFR':>8}{'3BET':>8}{'BB/100':>10}"
          f"{'95% CI':>12}")
    for r in stats.by_position(conn, hero, w):
        print(f"  {r['position']:<5}{r['hands']:>7}"
              f"{(f'{r['vpip']:.1f}' if r['vpip'] is not None else '-'):>8}"
              f"{(f'{r['pfr']:.1f}' if r['pfr'] is not None else '-'):>8}"
              f"{(f'{r['threebet']:.1f}' if r['threebet'] is not None else '-'):>8}"
              f"{r['bb100']:>10.1f}{('+/-' + f'{r['ci95']:.0f}'):>12}")

    print("\nCompliance")
    print("-" * 68)
    marks = {"ok": "ok ", "off": "!! ", "watch": "   ", "thin": " ? "}
    old = {c["name"]: c for c in stats.compliance(conn, hero, prior)} if prior else {}
    for c in stats.compliance(conn, hero, w):
        if c["kind"] == "count":
            val = f"{int(c['num'])}"
            rate = "" if c["per100"] is None else f" ({c['per100']:.2f}/100)"
        else:
            val = "-" if c["value"] is None else f"{c['value']:.1f}%"
            rate = f" ({int(c['num'])}/{int(c['den'])})"
        line = (f"  {marks[c['status']]}{c['name']:<28} {val:>7}{rate:<14}"
                f"target {c['target']:<6}")
        o = old.get(c["name"])
        if o is not None:
            was = (f"{int(o['num'])}" if c["kind"] == "count"
                   else ("-" if o["value"] is None else f"{o['value']:.1f}%"))
            line += f"prior {was:>7}  "
        print(line + c["note"])

    print("\nSample size")
    print("-" * 68)
    for r in stats.reliability(m["hands"]):
        mark = "ok" if r["ready"] else "  "
        print(f"  {mark} {r['stat']:<30} {r['have']:>7} / {r['needed']:<7} "
              f"({r['pct']:.0f}%)")

    pots = stats.big_pots(conn, hero, 10, w)
    for label, key in (("Biggest losses", "losses"), ("Biggest wins", "wins")):
        rows = pots[key]
        if not rows:
            continue
        print(f"\n{label}")
        print("-" * 100)
        for r in rows:
            print(f"  #{r['site_hand_no']:<13}{r['played_at'][5:16]}  "
                  f"{r['position']:<4}{r['hole_cards'] or '--':<8}"
                  f"{r['board'] or '':<16}pot {r['pot_bb']:6.1f}  "
                  f"{r['net_bb']:+8.1f}bb  {r['exit_street']:<9}{r['outcome']}")

    sess = stats.sessions(conn, hero, w)
    print(f"\nSessions: {len(sess)}   timeouts: {stats.timeouts(conn, hero, w)}")
    for s in sess[-10:]:
        print(f"  {s['start'][:16]}  {s['duration_min']:5.0f}min  "
              f"{s['hands']:4d} hands  {s['n_tables']} table(s)  "
              f"{s['bb100']:+8.1f} bb/100")
    print()
    return 0


def cmd_ev(args) -> int:
    from . import equity

    conn = db.connect(args.db)
    n = equity.compute_all(conn, force=args.force, progress=True)
    print(f"all-in EV computed for {n} hand/player rows")
    return 0


def cmd_report(args) -> int:
    from . import report

    conn = db.connect(args.db)
    hero = args.hero or db.detect_hero(conn)
    out = Path(args.output)
    html = report.build(conn, hero)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}  ({len(html) // 1024} KB)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pokertracker", description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB), help="sqlite database path")
    p.add_argument("--hero", default="", help="hero screen name (auto-detected)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="parse hand histories into the database")
    s.add_argument("--root", default=str(DEFAULT_ROOT))
    s.add_argument("--force", action="store_true", help="re-read unchanged files")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("refresh",
                       help="import + EV + report in one command (the usual one)")
    s.add_argument("--root", default=str(DEFAULT_ROOT))
    s.add_argument("--force", action="store_true")
    s.add_argument("-o", "--output", default="report.html")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("serve",
                       help="serve the dashboard with a working Refresh button")
    s.add_argument("--root", default=str(DEFAULT_ROOT))
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true",
                   help="do not open a browser window")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("derive", help="rebuild the derived flag table")
    s.set_defaults(func=cmd_derive)

    s = sub.add_parser("stats", help="print stats to the terminal")
    s.add_argument("--problems", action="store_true", help="list parse problems")
    s.add_argument("--period", default="all",
                   help="restrict to a period: all, session, day, week, n500, "
                        "n1000 (whichever the history supports)")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("ev", help="compute all-in EV")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_ev)

    s = sub.add_parser("report", help="write the HTML dashboard")
    s.add_argument("-o", "--output", default="report.html")
    s.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except db.StaleSchema as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
