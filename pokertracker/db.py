"""SQLite schema and loader.

All money columns are INTEGER CENTS. Tournament chip counts are scaled by 100
as well so one integer type covers both. Nothing here computes a statistic:
the point of parsing into normalized tables is that a stat can be redefined and
recomputed without re-reading 50,000 hands of text.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .parser import Hand, parse_file
from .positions import assign_positions

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hands (
    hand_id        INTEGER PRIMARY KEY,
    site_hand_no   TEXT NOT NULL UNIQUE,
    played_at      TEXT NOT NULL,
    played_at_et   TEXT,
    table_name     TEXT,
    game_type      TEXT,
    is_zoom        INTEGER NOT NULL DEFAULT 0,
    is_tournament  INTEGER NOT NULL DEFAULT 0,
    tournament_id  TEXT,
    level          TEXT,
    sb             INTEGER NOT NULL,
    bb             INTEGER NOT NULL,
    ante           INTEGER NOT NULL DEFAULT 0,
    currency       TEXT,
    max_seats      INTEGER,
    n_dealt        INTEGER NOT NULL,
    button_seat    INTEGER,
    hero           TEXT,
    board_flop     TEXT,
    board_turn     TEXT,
    board_river    TEXT,
    went_to_showdown INTEGER NOT NULL DEFAULT 0,
    total_pot      INTEGER NOT NULL DEFAULT 0,
    rake           INTEGER NOT NULL DEFAULT 0,
    cashout_fee    INTEGER NOT NULL DEFAULT 0,
    cashout_residual INTEGER NOT NULL DEFAULT 0,
    source_file    TEXT
);
CREATE INDEX IF NOT EXISTS ix_hands_time ON hands(played_at);
CREATE INDEX IF NOT EXISTS ix_hands_table ON hands(table_name, played_at);

CREATE TABLE IF NOT EXISTS seats (
    hand_id        INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    seat_no        INTEGER NOT NULL,
    player         TEXT NOT NULL,
    starting_stack INTEGER NOT NULL,
    position       TEXT,
    is_hero        INTEGER NOT NULL DEFAULT 0,
    hole_cards     TEXT,
    hole_combo     TEXT,
    is_dealt_in    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (hand_id, seat_no)
);
CREATE INDEX IF NOT EXISTS ix_seats_player ON seats(player);
CREATE INDEX IF NOT EXISTS ix_seats_hand_player ON seats(hand_id, player);

CREATE TABLE IF NOT EXISTS actions (
    hand_id       INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    street        TEXT NOT NULL,
    player        TEXT NOT NULL,
    action        TEXT NOT NULL,
    -- chips added to the pot by this action (negative for an uncalled return)
    amount        INTEGER NOT NULL DEFAULT 0,
    -- the player's TOTAL commitment on this street after acting; this, not
    -- `amount`, is what raise-size and 3-bet rules need
    to_amount     INTEGER NOT NULL DEFAULT 0,
    is_allin      INTEGER NOT NULL DEFAULT 0,
    is_blind_post INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hand_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_actions_hand_street ON actions(hand_id, street, seq);
CREATE INDEX IF NOT EXISTS ix_actions_player ON actions(player, street);

CREATE TABLE IF NOT EXISTS results (
    hand_id          INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    player           TEXT NOT NULL,
    contributed      INTEGER NOT NULL DEFAULT 0,
    collected        INTEGER NOT NULL DEFAULT 0,
    -- insurance settlement from the cash-out provider; NOT pot money
    cashout_received INTEGER NOT NULL DEFAULT 0,
    net              INTEGER NOT NULL DEFAULT 0,
    rake_share       INTEGER NOT NULL DEFAULT 0,
    reached_showdown INTEGER NOT NULL DEFAULT 0,
    cashed_out       INTEGER NOT NULL DEFAULT 0,
    won_pot          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hand_id, player)
);
CREATE INDEX IF NOT EXISTS ix_results_player ON results(player);

CREATE TABLE IF NOT EXISTS timeouts (
    hand_id INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    player  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    hand_id      INTEGER REFERENCES hands(hand_id) ON DELETE CASCADE,
    site_hand_no TEXT,
    problem      TEXT NOT NULL
);

-- All-in EV, populated separately by equity.py because it is expensive.
CREATE TABLE IF NOT EXISTS allin_ev (
    hand_id   INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    player    TEXT NOT NULL,
    equity    REAL NOT NULL,
    ev_net    INTEGER NOT NULL,
    PRIMARY KEY (hand_id, player)
);

-- Import bookkeeping so re-running only re-reads files that changed.
CREATE TABLE IF NOT EXISTS source_files (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    n_hands  INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _rake_shares(hand: Hand) -> dict[str, int]:
    """Attribute the pot's rake to players in proportion to contribution.

    PokerStars reports rake per pot, not per player, so any per-player figure
    is an ESTIMATE. Contribution-weighted is the convention; the alternative
    (charging it all to the winner) makes positional win rates meaningless.
    Rounding remainders go to the largest contributor so the shares sum exactly.
    """
    # Only the actual rake is spread across players. The cash-out fee and the
    # forfeited residual are costs borne by the player who cashed out, and are
    # already reflected in that player's net.
    charge = hand.rake
    total = sum(r.contributed for r in hand.results)
    if charge <= 0 or total <= 0:
        return {}
    shares, assigned = {}, 0
    ordered = sorted(hand.results, key=lambda r: -r.contributed)
    for r in ordered[1:]:
        share = charge * r.contributed // total
        shares[r.player] = share
        assigned += share
    shares[ordered[0].player] = charge - assigned
    return shares


def insert_hand(conn: sqlite3.Connection, hand: Hand, source_file: str = "") -> int | None:
    if any(p.startswith("FATAL") for p in hand.problems):
        conn.execute(
            "INSERT INTO problems(site_hand_no, problem) VALUES (?,?)",
            (hand.site_hand_no, "; ".join(hand.problems)),
        )
        return None

    assign_positions(hand)
    n_dealt = sum(1 for s in hand.seats if s.is_dealt_in)

    cur = conn.execute("SELECT hand_id FROM hands WHERE site_hand_no = ?", (hand.site_hand_no,))
    row = cur.fetchone()
    if row:
        hand_id = row["hand_id"]
        for table in ("seats", "actions", "results", "timeouts", "allin_ev", "problems"):
            conn.execute(f"DELETE FROM {table} WHERE hand_id = ?", (hand_id,))
        conn.execute("DELETE FROM hands WHERE hand_id = ?", (hand_id,))

    cur = conn.execute(
        """INSERT INTO hands (site_hand_no, played_at, played_at_et, table_name, game_type,
               is_zoom, is_tournament, tournament_id, level, sb, bb, ante, currency,
               max_seats, n_dealt, button_seat, hero, board_flop, board_turn, board_river,
               went_to_showdown, total_pot, rake, cashout_fee, cashout_residual, source_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            hand.site_hand_no, hand.played_at, hand.played_at_et, hand.table_name,
            hand.game_type, int(hand.is_zoom), int(hand.is_tournament), hand.tournament_id,
            hand.level, hand.sb, hand.bb, hand.ante, hand.currency, hand.max_seats,
            n_dealt, hand.button_seat, hand.hero, hand.board_flop, hand.board_turn,
            hand.board_river, int(hand.went_to_showdown), hand.total_pot, hand.rake,
            hand.cashout_fee, hand.cashout_residual, source_file,
        ),
    )
    hand_id = cur.lastrowid

    from .cards import hole_notation, parse_cards

    for s in hand.seats:
        combo = ""
        if s.hole_cards:
            try:
                combo = hole_notation(parse_cards(s.hole_cards))
            except ValueError:
                combo = ""
        conn.execute(
            """INSERT INTO seats (hand_id, seat_no, player, starting_stack, position,
                   is_hero, hole_cards, hole_combo, is_dealt_in)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (hand_id, s.seat_no, s.player, s.starting_stack, s.position,
             int(s.is_hero), s.hole_cards, combo, int(s.is_dealt_in)),
        )

    conn.executemany(
        """INSERT INTO actions (hand_id, seq, street, player, action, amount,
               to_amount, is_allin, is_blind_post)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(hand_id, a.seq, a.street, a.player, a.action, a.amount, a.to_amount,
          int(a.is_allin), int(a.is_blind_post)) for a in hand.actions],
    )

    shares = _rake_shares(hand)
    conn.executemany(
        """INSERT INTO results (hand_id, player, contributed, collected,
               cashout_received, net, rake_share, reached_showdown, cashed_out, won_pot)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(hand_id, r.player, r.contributed, r.collected, r.cashout_received, r.net,
          shares.get(r.player, 0), int(r.reached_showdown), int(r.cashed_out),
          int(r.won_pot)) for r in hand.results],
    )

    conn.executemany(
        "INSERT INTO timeouts (hand_id, player) VALUES (?,?)",
        [(hand_id, p) for p in hand.timeouts],
    )
    conn.executemany(
        "INSERT INTO problems (hand_id, site_hand_no, problem) VALUES (?,?,?)",
        [(hand_id, hand.site_hand_no, p) for p in hand.problems],
    )
    return hand_id


def import_paths(conn: sqlite3.Connection, paths, hero_hint: str = "",
                 force: bool = False) -> dict:
    """Import .txt histories. Unchanged files are skipped unless force=True."""
    stats = {"files": 0, "skipped": 0, "hands": 0, "problems": 0}
    for path in paths:
        path = Path(path)
        st = path.stat()
        prev = conn.execute(
            "SELECT size, mtime FROM source_files WHERE path = ?", (str(path),)
        ).fetchone()
        if prev and not force and prev["size"] == st.st_size and prev["mtime"] == st.st_mtime:
            stats["skipped"] += 1
            continue

        hands = parse_file(path, hero_hint)
        n = 0
        for hand in hands:
            if insert_hand(conn, hand, str(path)) is not None:
                n += 1
            stats["problems"] += len(hand.problems)
        conn.execute(
            "INSERT OR REPLACE INTO source_files (path, size, mtime, n_hands) VALUES (?,?,?,?)",
            (str(path), st.st_size, st.st_mtime, n),
        )
        stats["files"] += 1
        stats["hands"] += n
        conn.commit()
    return stats


def discover(root: str | Path) -> list[Path]:
    return sorted(Path(root).rglob("*.txt"))


def detect_hero(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT hero, COUNT(*) n FROM hands WHERE hero <> '' GROUP BY hero ORDER BY n DESC LIMIT 1"
    ).fetchone()
    return row["hero"] if row else ""


def hand_texts(conn: sqlite3.Connection, hand_ids) -> dict[int, str]:
    """Fetch the original text block for a handful of hands.

    Raw text is not stored in the database -- the histories are the source of
    truth and duplicating them would double the DB for no gain. Instead the
    source file is re-read and re-split on demand, which is only worth doing
    for the few dozen hands a drill-down table actually shows. A file that has
    since been moved or deleted yields no text rather than an error.
    """
    hand_ids = list(hand_ids)
    if not hand_ids:
        return {}
    placeholders = ",".join("?" * len(hand_ids))
    rows = conn.execute(
        f"SELECT hand_id, site_hand_no, source_file FROM hands"
        f" WHERE hand_id IN ({placeholders})",
        hand_ids,
    ).fetchall()

    wanted: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["source_file"]:
            wanted.setdefault(r["source_file"], {})[r["site_hand_no"]] = r["hand_id"]

    from .parser import split_hands

    out: dict[int, str] = {}
    for path, by_no in wanted.items():
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for block in split_hands(text):
            head = block.split("\n", 1)[0]
            for no, hand_id in by_no.items():
                if f"#{no}" in head:
                    out[hand_id] = block
                    break
    return out
