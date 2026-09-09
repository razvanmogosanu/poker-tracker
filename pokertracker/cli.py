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

    m = stats.money_summary(conn, hero)
    if not m.get("hands"):
        print("no hands for hero", hero, file=sys.stderr)
        return 1

    print(f"\nHero: {hero}   {m['hands']} hands   {m['first_hand']} .. {m['last_hand']}")
    print("-" * 68)
    print(f"  Win rate        {m['bb100']:+8.2f} bb/100   +/- {m['ci95']:.1f} (95% CI)")
    if m["has_ev"]:
        print(f"  All-in adj. EV  {m['ev_bb100']:+8.2f} bb/100   "
              f"(luck {m['ev_diff_bb100']:+.2f})")
    print(f"  Showdown        {m['showdown_bb100']:+8.2f} bb/100  (blue line)")
    print(f"  Non-showdown    {m['nonshowdown_bb100']:+8.2f} bb/100  (red line)")
    print(f"  Rake paid       {m['rake_bb100']:8.2f} bb/100   "
          f"(gross {m['gross_bb100']:+.2f})")
    print(f"  Std deviation   {m['sd_bb100']:8.1f} bb/100")

    print("\nPreflop")
    print("-" * 68)
    for r in stats.preflop_stats(conn, hero):
        ci = (r["ci_lo"], r["ci_hi"])
        print(f"  {r['name']:<20} {_fmt_pct(r['pct'], ci)}   n={int(r['den'])}")

    print("\nPostflop")
    print("-" * 68)
    for r in stats.postflop_stats(conn, hero):
        ci = (r["ci_lo"], r["ci_hi"])
        print(f"  {r['name']:<20} {_fmt_pct(r['pct'], ci)}   n={int(r['den'])}")
    ag = stats.aggression(conn, hero)
    if ag["afq"] is not None:
        print(f"  {'Aggression Freq':<20} {ag['afq']:5.1f}%")
        print(f"  {'Aggression Factor':<20} "
              f"{ag['af']:5.2f}" if ag["af"] else "  Aggression Factor      -")

    print("\nBy position")
    print("-" * 68)
    print(f"  {'POS':<5}{'HANDS':>7}{'VPIP':>8}{'PFR':>8}{'3BET':>8}{'BB/100':>10}"
          f"{'95% CI':>12}")
    for r in stats.by_position(conn, hero):
        print(f"  {r['position']:<5}{r['hands']:>7}"
              f"{(f'{r['vpip']:.1f}' if r['vpip'] is not None else '-'):>8}"
              f"{(f'{r['pfr']:.1f}' if r['pfr'] is not None else '-'):>8}"
              f"{(f'{r['threebet']:.1f}' if r['threebet'] is not None else '-'):>8}"
              f"{r['bb100']:>10.1f}{('+/-' + f'{r['ci95']:.0f}'):>12}")

    print("\nCompliance")
    print("-" * 68)
    for c in stats.compliance(conn, hero):
        mark = "ok " if c["ok"] else ("!! " if c["ok"] is False else "   ")
        val = (f"{c['value']:.1f}%" if c["unit"] == "pct" and c["value"] is not None
               else str(c["value"]))
        print(f"  {mark}{c['name']:<28} {val:>8}  target {c['target']:<6} {c['note']}")

    print("\nSample size")
    print("-" * 68)
    for r in stats.reliability(m["hands"]):
        mark = "ok" if r["ready"] else "  "
        print(f"  {mark} {r['stat']:<30} {r['have']:>7} / {r['needed']:<7} "
              f"({r['pct']:.0f}%)")

    sess = stats.sessions(conn, hero)
    print(f"\nSessions: {len(sess)}   timeouts: {stats.timeouts(conn, hero)}")
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
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("ev", help="compute all-in EV")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_ev)

    s = sub.add_parser("report", help="write the HTML dashboard")
    s.add_argument("-o", "--output", default="report.html")
    s.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
