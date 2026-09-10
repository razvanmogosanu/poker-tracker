"""PokerStars hand-history parser.

Deliberately dumb: it turns text into normalized rows and asserts arithmetic
invariants. It computes no statistics and derives no positions beyond what the
text literally states (position derivation lives in positions.py, stats in
stats.py) so that a stat can be redefined without re-parsing.

Money is stored as integer cents throughout. Tournament chip amounts are also
scaled by 100 so a single integer type covers both.

Action.amount is the number of chips the player ADDED to the pot with that
action. For calls and bets this equals the number PokerStars prints. For raises
it does NOT: PokerStars prints the increment over the previous bet level, e.g.
a big blind holding $0.02 who "raises $0.04 to $0.10" adds $0.08. to_amount is
always the player's total commitment on that street after the action, which is
the figure every raise-size and 3-bet rule actually needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .cards import parse_cards

STREETS = ("preflop", "flop", "turn", "river")


class ParseError(Exception):
    pass


@dataclass
class Action:
    seq: int
    street: str
    player: str
    action: str  # post|fold|check|call|bet|raise|returns
    amount: int = 0  # chips added to pot by this action, in cents
    to_amount: int = 0  # player's total commitment this street, after acting
    is_allin: bool = False
    is_blind_post: bool = False


@dataclass
class Seat:
    seat_no: int
    player: str
    starting_stack: int
    is_hero: bool = False
    hole_cards: str = ""
    is_dealt_in: bool = True
    position: str = ""  # filled in by positions.assign_positions


@dataclass
class Result:
    player: str
    contributed: int = 0
    collected: int = 0
    net: int = 0
    # Money received from the pot. A cash-out settlement is NOT pot money and
    # is tracked separately in cashout_received.
    cashout_received: int = 0
    reached_showdown: bool = False
    cashed_out: bool = False
    won_pot: bool = False


@dataclass
class Hand:
    site_hand_no: str
    played_at: str
    played_at_et: str = ""
    table_name: str = ""
    game_type: str = "Hold'em No Limit"
    is_zoom: bool = False
    is_tournament: bool = False
    tournament_id: str = ""
    level: str = ""
    sb: int = 0
    bb: int = 0
    ante: int = 0
    currency: str = "USD"
    max_seats: int = 0
    button_seat: int = 0
    board_flop: str = ""
    board_turn: str = ""
    board_river: str = ""
    total_pot: int = 0
    rake: int = 0
    cashout_fee: int = 0
    # Pot money that went to the cash-out provider instead of any player.
    cashout_residual: int = 0
    hero: str = ""
    went_to_showdown: bool = False
    seats: list[Seat] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    results: list[Result] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def seat_of(self, player: str) -> Seat | None:
        for s in self.seats:
            if s.player == player:
                return s
        return None

    @property
    def board(self) -> str:
        return " ".join(x for x in (self.board_flop, self.board_turn, self.board_river) if x)


# --------------------------------------------------------------------------
# money

_MONEY_RE = re.compile(r"-?[\d.,]+")


def parse_money(text: str) -> int:
    """'$0.04' / '1,234.56' / '$1' / '25' -> integer cents.

    The RO client writes the *filename* with comma decimals but the file body
    with dots. Both are handled: if a dot is present commas are thousands
    separators, otherwise a lone comma is treated as the decimal point.
    """
    m = _MONEY_RE.search(text.replace("$", "").replace("€", "").replace("£", ""))
    if not m:
        raise ParseError(f"no money in {text!r}")
    s = m.group(0)
    if "." in s:
        s = s.replace(",", "")
    elif s.count(",") == 1 and len(s.split(",")[1]) in (1, 2):
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    return int((Decimal(s) * 100).quantize(Decimal("1")))


# --------------------------------------------------------------------------
# line patterns

RE_HEADER = re.compile(
    r"^PokerStars(?P<zoom>\s+Zoom)?\s+Hand\s+#(?P<hand>\d+):\s+(?P<rest>.*)$"
)
RE_CASH_GAME = re.compile(
    r"^(?P<game>.+?)\s+\((?P<sb>[^/]+)/(?P<bb>[^)\s]+)(?:\s+(?P<cur>[A-Z]{3}))?\)\s+-\s+(?P<ts>.*)$"
)
RE_TOURNEY = re.compile(
    r"^Tournament\s+#(?P<tid>\d+),\s+(?P<buyin>.*?)\s+(?P<game>Hold'em No Limit|.+?)\s+-\s+"
    r"Level\s+(?P<level>\S+)\s+\((?P<sb>[^/]+)/(?P<bb>[^)]+)\)\s+-\s+(?P<ts>.*)$"
)
RE_TIMES = re.compile(
    r"(?P<local>\d{4}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2})(?:\s+(?P<tz>\w+))?"
    r"(?:\s+\[(?P<et>\d{4}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2})\s+ET\])?"
)
RE_TABLE = re.compile(
    r"^Table\s+'(?P<name>.*)'\s+(?:(?P<max>\d+)-max\s+)?(?:\(.*?\)\s+)?Seat\s+#(?P<btn>\d+)\s+is\s+the\s+button"
)
RE_SEAT = re.compile(
    r"^Seat\s+(?P<no>\d+):\s+(?P<player>.+?)\s+\((?P<stack>[^)]+?)\s+in\s+chips\)(?P<tail>.*)$"
)
RE_POST = re.compile(
    r"^(?P<player>.+?):\s+posts\s+(?P<what>small\s+blind|big\s+blind|small\s+&\s+big\s+blinds|the\s+ante|ante)\s+(?P<amt>[\d.,$]+)"
)
RE_DEALT = re.compile(r"^Dealt to\s+(?P<player>.+?)\s+\[(?P<cards>[^\]]+)\]")
RE_ACTION = re.compile(
    r"^(?P<player>.+?):\s+(?P<verb>folds|checks|calls|bets|raises)"
    r"(?:\s+(?P<amt>[\d.,$]+))?(?:\s+to\s+(?P<to>[\d.,$]+))?"
    r"(?P<allin>\s+and\s+is\s+all-in)?(?:\s+\[(?P<cards>[^\]]+)\])?\s*$"
)
RE_UNCALLED = re.compile(
    r"^Uncalled bet\s+\((?P<amt>[^)]+)\)\s+returned to\s+(?P<player>.+?)\s*$"
)
RE_COLLECTED = re.compile(
    r"^(?P<player>.+?)\s+collected\s+(?P<amt>[\d.,$]+)\s+from\s+(?:the\s+)?(?:main\s+pot|side\s+pot(?:-\d+)?|pot)"
)
RE_WON = re.compile(r"^(?P<player>.+?):?\s+wins\s+(?P<amt>[\d.,$]+)")
RE_CASHOUT = re.compile(
    r"^(?P<player>.+?)\s+cashed out the hand for\s+(?P<amt>[\d.,$]+)"
    r"(?:\s*\|\s*Cash Out Fee\s+(?P<fee>[\d.,$]+))?"
)
RE_SHOWS = re.compile(r"^(?P<player>.+?):\s+(?P<verb>shows|mucks hand|doesn't show hand)")
RE_FLOP = re.compile(r"^\*\*\*\s+(?:FIRST\s+|SECOND\s+)?FLOP\s+\*\*\*\s*(?:\[[^\]]*\]\s*)*\[(?P<c>[^\]]+)\]")
RE_TURN = re.compile(r"^\*\*\*\s+(?:FIRST\s+|SECOND\s+)?TURN\s+\*\*\*\s+\[[^\]]*\]\s+\[(?P<c>[^\]]+)\]")
RE_RIVER = re.compile(r"^\*\*\*\s+(?:FIRST\s+|SECOND\s+)?RIVER\s+\*\*\*\s+\[[^\]]*\]\s+\[(?P<c>[^\]]+)\]")
RE_TOTAL_POT = re.compile(r"^Total pot\s+(?P<pot>[\d.,$]+)(?:.*?\|\s*Rake\s+(?P<rake>[\d.,$]+))?")
RE_SUMMARY_SEAT = re.compile(r"^Seat\s+(?P<no>\d+):\s+(?P<rest>.*)$")
RE_ALLOWED_LATER = re.compile(r"^(?P<player>.+?):?\s+will be allowed to play after the button")
RE_TIMEOUT = re.compile(r"^(?P<player>.+?)\s+has timed out")

# Chat, connection and table-membership chatter. These carry no hand state, but
# they are enumerated explicitly rather than matched by a catch-all so that a
# genuinely new line type shows up in Hand.problems instead of being swallowed.
NOISE_FRAGMENTS = (
    " said, ",
    " is sitting out",
    ": sits out",
    " has returned",
    " is disconnected",
    " is connected",
    " leaves the table",
    " joins the table",
    " was removed from the table",
    " re-buys and receives",
    " adds ",
    " has requested TIME",
    " is feeling",
    " will be allowed to play after the button",
    " finished the tournament",
    " wins the tournament",
    " received a reward",
    " has been disconnected",
    " doesn't show hand",
    " mucks hand",
    " is no longer sitting out",
    # Tournament side prizes -- tickets, seats, entries. They are awarded
    # alongside the pot and are not part of it.
    " wins a ",
)
NOISE_PREFIXES = ("Board ", "Hand cancelled", "Total pot ")


def _is_noise(line: str) -> bool:
    return line.startswith(NOISE_PREFIXES) or any(f in line for f in NOISE_FRAGMENTS)


# --------------------------------------------------------------------------


def split_hands(text: str) -> list[str]:
    """Split a history file into per-hand text blocks."""
    text = text.replace("\r\n", "\n").replace("﻿", "")
    blocks, cur = [], []
    for line in text.split("\n"):
        if line.startswith("PokerStars") and " Hand #" in line and cur:
            blocks.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in ("\n".join(x.strip() for x in b.split("\n")).strip() for b in blocks) if b]


def parse_hand(block: str, hero_hint: str = "") -> Hand:  # noqa: C901 - a parser is a big switch
    lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
    if not lines:
        raise ParseError("empty block")

    m = RE_HEADER.match(lines[0])
    if not m:
        raise ParseError(f"bad header: {lines[0]!r}")

    hand = Hand(site_hand_no=m.group("hand"), played_at="", is_zoom=bool(m.group("zoom")))
    rest = m.group("rest")

    mt = RE_TOURNEY.match(rest)
    if mt:
        hand.is_tournament = True
        hand.tournament_id = mt.group("tid")
        hand.level = mt.group("level")
        hand.game_type = mt.group("game").strip()
        hand.sb, hand.bb = parse_money(mt.group("sb")), parse_money(mt.group("bb"))
        ts = mt.group("ts")
    else:
        mc = RE_CASH_GAME.match(rest)
        if not mc:
            raise ParseError(f"unrecognised header body: {rest!r}")
        hand.game_type = mc.group("game").strip()
        hand.sb, hand.bb = parse_money(mc.group("sb")), parse_money(mc.group("bb"))
        hand.currency = mc.group("cur") or "USD"
        ts = mc.group("ts")

    mtime = RE_TIMES.search(ts)
    if not mtime:
        raise ParseError(f"no timestamp in {ts!r}")
    hand.played_at = datetime.strptime(mtime.group("local"), "%Y/%m/%d %H:%M:%S").isoformat(" ")
    if mtime.group("et"):
        hand.played_at_et = datetime.strptime(
            mtime.group("et"), "%Y/%m/%d %H:%M:%S"
        ).isoformat(" ")

    # -- state -------------------------------------------------------------
    street = "preflop"
    seq = 0
    committed: dict[str, int] = {}  # this street
    contributed: dict[str, int] = {}  # whole hand
    collected: dict[str, int] = {}
    folded: set[str] = set()
    cashed_out: set[str] = set()
    cashout_received: dict[str, int] = {}
    allowed_later: set[str] = set()
    posted: set[str] = set()
    sb_posted = False
    in_summary = False
    summary_seats: dict[int, str] = {}
    seen_hole_cards = False

    def add(action: str, player: str, amount: int = 0, to_amount: int = 0,
            allin: bool = False, blind: bool = False) -> None:
        nonlocal seq
        seq += 1
        hand.actions.append(
            Action(seq, street, player, action, amount, to_amount, allin, blind)
        )
        if amount:
            contributed[player] = contributed.get(player, 0) + amount

    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue

        # ---- section markers
        if line.startswith("***"):
            if "HOLE CARDS" in line:
                street, seen_hole_cards = "preflop", True
            elif (mf := RE_FLOP.match(line)):
                street = "flop"
                if not hand.board_flop:
                    hand.board_flop = mf.group("c").strip()
                committed.clear()
            elif (mr := RE_TURN.match(line)):
                street = "turn"
                if not hand.board_turn:
                    hand.board_turn = mr.group("c").strip()
                committed.clear()
            elif (mv := RE_RIVER.match(line)):
                street = "river"
                if not hand.board_river:
                    hand.board_river = mv.group("c").strip()
                committed.clear()
            elif "SHOW DOWN" in line:
                hand.went_to_showdown = True
            elif "SUMMARY" in line:
                in_summary = True
            continue

        if in_summary:
            if (mp := RE_TOTAL_POT.match(line)):
                hand.total_pot = parse_money(mp.group("pot"))
                if mp.group("rake"):
                    hand.rake = parse_money(mp.group("rake"))
                continue
            if (ms := RE_SUMMARY_SEAT.match(line)):
                summary_seats[int(ms.group("no"))] = ms.group("rest").strip()
            continue

        # ---- table / seats
        if line.startswith("Table '"):
            if (mtb := RE_TABLE.match(line)):
                hand.table_name = mtb.group("name")
                hand.max_seats = int(mtb.group("max") or 0)
                hand.button_seat = int(mtb.group("btn"))
            continue

        if line.startswith("Seat ") and " in chips)" in line:
            if (msq := RE_SEAT.match(line)):
                tail = msq.group("tail").lower()
                hand.seats.append(
                    Seat(
                        seat_no=int(msq.group("no")),
                        player=msq.group("player"),
                        starting_stack=parse_money(msq.group("stack")),
                        is_dealt_in=not ("sitting out" in tail or "out of hand" in tail),
                    )
                )
            continue

        if (mal := RE_ALLOWED_LATER.match(line)):
            allowed_later.add(mal.group("player"))
            continue

        # ---- blinds and antes
        if (mpo := RE_POST.match(line)):
            player, what = mpo.group("player"), mpo.group("what")
            amt = parse_money(mpo.group("amt"))
            if "ante" in what:
                # Antes are dead money: they never count toward the amount a
                # player must call, so they do not touch `committed`.
                add("post", player, amt, committed.get(player, 0), blind=True)
                hand.ante = max(hand.ante, amt)
            elif "&" in what:
                # "posts small & big blinds": the small blind half is dead. Only
                # the big blind counts as a live bet the player has already made,
                # so calling a raise costs them the same as any other big blind.
                committed[player] = committed.get(player, 0) + hand.bb
                add("post", player, amt, committed[player], blind=True)
                posted.add(player)
            elif "small blind" in what and sb_posted:
                # The hand's second small blind is a *dead* post: a player who
                # sat out through the blinds pays it to be dealt in again, and
                # it buys them nothing. It is dead money like an ante, so it
                # never touches `committed` -- a returning player who then
                # "raises $0.02 to $0.04" ends the street with $0.05 in front of
                # them, not $0.04, and the pot only balances if that extra cent
                # is counted. Live posts always print before dead ones, so the
                # first small blind of the hand is the real one.
                add("post", player, amt, committed.get(player, 0), blind=True)
                posted.add(player)
            else:
                # An out-of-position big blind post, by contrast, is live money:
                # the poster has genuinely bet it and calls a raise for the
                # difference like any other big blind.
                sb_posted = sb_posted or "small blind" in what
                committed[player] = committed.get(player, 0) + amt
                add("post", player, amt, committed[player], blind=True)
                posted.add(player)
            continue

        if (md := RE_DEALT.match(line)):
            player = md.group("player")
            seat = hand.seat_of(player)
            if seat:
                seat.hole_cards = " ".join(str(x) for x in md.group("cards").split())
                parse_cards(md.group("cards"))  # validate
            if not hand.hero:
                hand.hero = hero_hint or player
            continue

        # ---- uncalled bet
        if (mu := RE_UNCALLED.match(line)):
            player, amt = mu.group("player"), parse_money(mu.group("amt"))
            seq += 1
            hand.actions.append(Action(seq, street, player, "returns", -amt, 0))
            contributed[player] = contributed.get(player, 0) - amt
            committed[player] = committed.get(player, 0) - amt
            continue

        # ---- cash out (no `collected` line is emitted for these players)
        if " cashed out the hand for " in line:
            if (mco := RE_CASHOUT.match(line)):
                player = mco.group("player")
                # The provider pays this, not the pot: a player who LOST the
                # hand can still cash out for a positive amount while the
                # winner collects the whole pot as normal.
                cashout_received[player] = (
                    cashout_received.get(player, 0) + parse_money(mco.group("amt")))
                cashed_out.add(player)
                if mco.group("fee"):
                    hand.cashout_fee += parse_money(mco.group("fee"))
            continue

        if " collected " in line and (mcl := RE_COLLECTED.match(line)):
            collected[mcl.group("player")] = collected.get(mcl.group("player"), 0) + parse_money(
                mcl.group("amt")
            )
            continue

        # ---- betting actions (anchor on the rightmost verb: names can contain ': ')
        if (ma := RE_ACTION.match(line)):
            player, verb = ma.group("player"), ma.group("verb")
            allin = bool(ma.group("allin"))
            if verb == "folds":
                folded.add(player)
                if ma.group("cards") and (seat := hand.seat_of(player))                         and not seat.hole_cards:
                    seat.hole_cards = ma.group("cards")
                add("fold", player)
            elif verb == "checks":
                add("check", player)
            elif verb in ("calls", "bets"):
                amt = parse_money(ma.group("amt"))
                committed[player] = committed.get(player, 0) + amt
                add(verb[:-1], player, amt, committed[player], allin=allin)
            else:  # raises X to Y
                to = parse_money(ma.group("to")) if ma.group("to") else None
                if to is None:
                    raise ParseError(f"raise without 'to': {line!r}")
                delta = to - committed.get(player, 0)
                committed[player] = to
                add("raise", player, delta, to, allin=allin)
            continue

        if (msh := RE_SHOWS.match(line)):
            player = msh.group("player")
            if msh.group("verb") == "shows" and (seat := hand.seat_of(player)) and not seat.hole_cards:
                cards = re.search(r"\[([^\]]+)\]", line)
                if cards:
                    seat.hole_cards = cards.group(1)
            continue

        if RE_TIMEOUT.match(line):
            hand.timeouts.append(RE_TIMEOUT.match(line).group("player"))
            continue

        if _is_noise(line):
            continue

        hand.problems.append(f"unparsed line: {line!r}")

    # -- dealt-in reconciliation ------------------------------------------
    # The summary lists every seat, but only seats that were dealt in get a
    # verb after the name. That is the most reliable signal available.
    for seat in hand.seats:
        summary = summary_seats.get(seat.seat_no)
        acted = seat.player in contributed or seat.player in folded or seat.player in posted
        if summary is not None:
            tail = summary[len(seat.player):].strip() if summary.startswith(seat.player) else ""
            dealt = bool(tail)
        else:
            dealt = seat.is_dealt_in
        if seat.player in allowed_later:
            dealt = False
        if acted:
            dealt = True
        if summary is not None and dealt != seat.is_dealt_in and not acted:
            hand.problems.append(
                f"dealt-in mismatch seat {seat.seat_no} {seat.player}: "
                f"seat-line={seat.is_dealt_in} summary={dealt}"
            )
        seat.is_dealt_in = dealt
        seat.is_hero = seat.player == hand.hero

    # -- results -----------------------------------------------------------
    dealt_players = [s.player for s in hand.seats if s.is_dealt_in]
    for player in dealt_players:
        c_in = contributed.get(player, 0)
        c_out = collected.get(player, 0)
        c_ins = cashout_received.get(player, 0)
        hand.results.append(
            Result(
                player=player,
                contributed=c_in,
                collected=c_out,
                cashout_received=c_ins,
                net=c_out + c_ins - c_in,
                reached_showdown=hand.went_to_showdown and player not in folded,
                cashed_out=player in cashed_out,
                won_pot=c_out > 0,
            )
        )

    _check_invariants(hand, seen_hole_cards)
    return hand


def _check_invariants(hand: Hand, seen_hole_cards: bool) -> None:
    """Arithmetic identities that catch most parser bugs immediately."""
    if not seen_hole_cards:
        hand.problems.append("no HOLE CARDS marker (cancelled hand?)")
        return

    total_in = sum(r.contributed for r in hand.results)
    if total_in != hand.total_pot:
        hand.problems.append(f"contributed {total_in} != total_pot {hand.total_pot}")

    # The pot is always fully accounted for by collections plus rake -- unless a
    # player cashed out, in which case that player forfeits whatever share of
    # the pot they would have won to the cash-out provider, and no `collected`
    # line is emitted for them. That forfeited share is the residual.
    collected = sum(r.collected for r in hand.results)
    hand.cashout_residual = hand.total_pot - hand.rake - collected

    if not any(r.cashed_out for r in hand.results):
        if hand.cashout_residual != 0:
            hand.problems.append(
                f"collected+rake {collected + hand.rake} != total_pot "
                f"{hand.total_pot}")
    elif hand.cashout_residual < 0:
        hand.problems.append(
            f"negative cash-out residual {hand.cashout_residual}")


def parse_file(path, hero_hint: str = "") -> list[Hand]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    hands = []
    for block in split_hands(text):
        try:
            hands.append(parse_hand(block, hero_hint))
        except ParseError as exc:
            hands.append(_broken(block, exc))
    return hands


def _broken(block: str, exc: Exception) -> Hand:
    m = RE_HEADER.match(block.split("\n")[0])
    h = Hand(site_hand_no=m.group("hand") if m else "?", played_at="")
    h.problems.append(f"FATAL: {exc}")
    return h
