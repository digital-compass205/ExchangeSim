"""``exsim`` -- control and monitor a running exchange simulator.

Exit codes are part of the contract, because CI scripts branch on them:

===  ==========================================================
 0   success
 1   the command ran and the server rejected it
 2   usage error (bad arguments, unknown subcommand)
 3   could not reach or stay connected to the control plane
===  ==========================================================

Every command prints a formatted table by default and raw JSON with
``--json``, which may appear before or after the subcommand.

Environment defaults: ``EXSIM_HOST``, ``EXSIM_PORT``, ``EXSIM_TOKEN`` and
``EXSIM_MARKET``. Setting ``EXSIM_MARKET`` saves repeating ``--market`` on a
venue that runs several.
"""

import argparse
import json
import os
import sys
import time

from .client import CommandFailed, ControlClient, ControlClientError
from .format import as_json, render_pairs, render_table

EXIT_OK = 0
EXIT_COMMAND_FAILED = 1
EXIT_USAGE = 2
EXIT_TRANSPORT = 3

DEFAULT_HOST = os.environ.get("EXSIM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("EXSIM_PORT", "9101"))
DEFAULT_TOKEN = os.environ.get("EXSIM_TOKEN") or None
DEFAULT_MARKET = os.environ.get("EXSIM_MARKET") or None

CLEAR_SCREEN = "\033[2J\033[H"


def build_parser():
    # Subparsers must not carry a default for --json: argparse writes every
    # subparser default into the namespace *after* parsing the top-level
    # arguments, so a default of False would silently undo "exsim --json ping".
    # SUPPRESS leaves the attribute alone unless the flag is actually given.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS,
                        help="emit raw JSON instead of a formatted table")

    parser = argparse.ArgumentParser(
        prog="exsim",
        description="Control and monitor a running exchange simulator.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        default=False,
                        help="emit raw JSON instead of a formatted table")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="control plane host (default %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="control plane port (default %(default)s)")
    parser.add_argument("--token", default=DEFAULT_TOKEN,
                        help="shared secret, if the server requires one")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="socket timeout in seconds (default %(default)s)")

    sub = parser.add_subparsers(dest="command")

    def add(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[common])

    def with_market(subparser):
        subparser.add_argument("--market", "-m", default=DEFAULT_MARKET,
                               help="market name (default $EXSIM_MARKET)")
        return subparser

    add("ping", "check the simulator is alive")
    add("commands", "list the control commands this simulator supports")
    add("info", "venue summary: markets, instruments, sessions")
    add("markets", "list markets and their trading state")
    add("assumptions", "behaviours the specification does not define")

    # -- trading state --
    state = add("state", "read or change the trading state")
    state.add_argument("action", choices=("get", "set", "clear"))
    state.add_argument("value", nargs="?",
                       help="the new state, for 'set' (e.g. OPEN, CLOSED, HALTED)")
    with_market(state)
    state.add_argument("--symbol", "-s", help="scope the change to one symbol")

    # -- reference data --
    instruments = add("instruments", "list the instrument universe")
    instruments.add_argument("symbol", nargs="?", help="show just this symbol")

    instrument = add("instrument", "add, remove or adjust one instrument")
    instrument.add_argument("action", choices=("add", "remove", "set"))
    instrument.add_argument("symbol")
    instrument.add_argument("--name", help="display name (add)")
    instrument.add_argument("--lot-size", type=int, help="shares per lot (add)")
    instrument.add_argument("--base-price",
                            help="reference price the daily band is built around")
    instrument.add_argument("--tier", help="tick-size tier, e.g. TOPIX100 (add)")
    instrument.add_argument("--shares-outstanding", type=int,
                            help="drives the 5%% maximum order quantity (add)")
    instrument.add_argument("--band-low", help="explicit lower price limit (set)")
    instrument.add_argument("--band-high", help="explicit upper price limit (set)")
    instrument.add_argument("--clear-band", action="store_true",
                            help="drop an explicit band override (set)")
    tradability = instrument.add_mutually_exclusive_group()
    tradability.add_argument("--tradable", dest="tradable", action="store_true",
                             default=None, help="allow orders in this symbol")
    tradability.add_argument("--not-tradable", dest="tradable",
                             action="store_false", default=None,
                             help="refuse orders in this symbol")

    reference = add("reference", "manage reference data")
    reference.add_argument("action", choices=("reload",))

    # -- market data --
    book = with_market(add("book", "aggregated depth for a symbol"))
    book.add_argument("symbol")
    book.add_argument("--depth", "-d", type=int, default=5,
                      help="price levels per side (default %(default)s)")

    ladder = with_market(add("ladder", "tick-aligned ita board for a symbol"))
    ladder.add_argument("symbol")
    ladder.add_argument("--rows", "-r", type=int, default=10,
                        help="rows either side of the centre (default %(default)s)")
    ladder.add_argument("--center", "-C", help="centre on this price instead")

    bbo = with_market(add("bbo", "best bid and offer for a symbol"))
    bbo.add_argument("symbol")

    trades = with_market(add("trades", "recent trades for a symbol"))
    trades.add_argument("symbol")
    trades.add_argument("--limit", "-n", type=int, default=20,
                        help="how many trades (default %(default)s)")

    stats = with_market(add("stats", "session statistics"))
    stats.add_argument("symbol", nargs="?",
                       help="omit for a one-line summary of every instrument")

    monitor = with_market(add("monitor", "live book and trade view"))
    monitor.add_argument("symbol")
    monitor.add_argument("--depth", "-d", type=int, default=5)
    monitor.add_argument("--no-clear", action="store_true",
                         help="append updates instead of redrawing (for logs)")

    # -- orders --
    orders = with_market(add("orders", "list orders"))
    orders.add_argument("--session", "-S", help="filter by TargetCompID")
    orders.add_argument("--symbol", "-s", help="filter by symbol")
    orders.add_argument("--all", action="store_true",
                        help="include terminated orders")

    order = add("order", "inspect or cancel a single order")
    order.add_argument("action", choices=("get", "cancel"))
    order.add_argument("order_id")

    # -- sessions --
    add("sessions", "list FIX sessions and their state")

    session = add("session", "act on one FIX session")
    session.add_argument("action", choices=("reset", "kill"))
    session.add_argument("target_comp_id")

    # -- audit --
    audit = add("audit", "every message and command, newest last")
    audit.add_argument("--seq", type=int,
                       help="show one entry field by field instead of the list")
    audit.add_argument("--symbol", "-s", help="only this instrument's traffic")
    audit.add_argument("--kind", choices=("fix", "control"),
                       help="FIX messages or control commands")
    audit.add_argument("--direction", "-d", choices=("in", "out"))
    audit.add_argument("--type", "-t", dest="msg_type",
                       help="a MsgType or a command name, e.g. D or order.new")
    audit.add_argument("--exclude", "-x", dest="exclude",
                       help="hide these MsgTypes or command names, e.g. 0,1")
    audit.add_argument("--since", help="from this time, e.g. 09:30:00")
    audit.add_argument("--until", help="up to this time")
    audit.add_argument("--limit", "-n", type=int, default=50)
    audit.add_argument("--wire", action="store_true",
                       help="with --seq, print the raw message instead")

    # -- generic --
    call = add("call", "invoke any control command directly")
    call.add_argument("name", help="command name, e.g. state.set")
    call.add_argument("args", nargs="?", default="{}",
                      help="arguments as a JSON object (default '{}')")

    watch = add("watch", "stream raw events matching topic patterns")
    watch.add_argument("topics", nargs="+",
                       help="patterns, e.g. 'trade:*' 'book:DAY:7203'")

    return parser


def main(argv=None):
    parser = build_parser()
    opts = parser.parse_args(argv)

    if not opts.command:
        parser.print_help()
        return EXIT_USAGE

    handler = _HANDLERS[opts.command]
    client = ControlClient(opts.host, opts.port, opts.timeout, opts.token)
    try:
        client.connect()
        return handler(client, opts)
    except CommandFailed as exc:
        _fail("%s: %s" % (exc.code, exc.message))
        return EXIT_COMMAND_FAILED
    except ControlClientError as exc:
        _fail(str(exc))
        return EXIT_TRANSPORT
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        client.close()


# -- simple commands ---------------------------------------------------------

def cmd_ping(client, opts):
    return _emit(client.call("ping"), opts,
                 lambda r: render_pairs(r, ["venue", "version", "time", "pong"]))


def cmd_commands(client, opts):
    return _emit(client.call("help"), opts,
                 lambda r: render_table(r["commands"], ["command", "summary"]))


def cmd_info(client, opts):
    result = client.call("venue.info")

    def layout(data):
        header = render_pairs(
            {"venue": data.get("name"), "type": data.get("venue"),
             "instruments": data.get("instruments"),
             "sessions": data.get("sessions"),
             "fix_port": data.get("fix_port")},
            ["venue", "type", "instruments", "sessions", "fix_port"])
        return header + "\n\n" + render_table(
            data.get("markets", []),
            ["market", "description", "state", "stp_mode", "resting_orders"])

    return _emit(result, opts, layout)


def cmd_markets(client, opts):
    return _emit(client.call("markets"), opts,
                 lambda r: render_table(
                     r["markets"],
                     ["market", "description", "state", "stp_mode",
                      "instruments", "resting_orders"]))


def cmd_assumptions(client, opts):
    return _emit(client.call("venue.assumptions"), opts,
                 lambda r: render_table(r["assumptions"],
                                        ["topic", "behaviour", "source"]))


# -- trading state -----------------------------------------------------------

def cmd_state(client, opts):
    if opts.action == "set":
        if not opts.value:
            _fail("state set needs a value, e.g. 'exsim state set OPEN'")
            return EXIT_USAGE
        args = {"state": opts.value.upper()}
        _put(args, "market", opts.market)
        _put(args, "symbol", opts.symbol)
        result = client.call("state.set", args)
        return _emit(result, opts, lambda r: render_pairs(
            {"state": r["state"], "market": r["market"], "symbol": r["symbol"],
             "orders_cancelled": r["orders_cancelled"]},
            ["state", "market", "symbol", "orders_cancelled"]))

    if opts.action == "clear":
        if not opts.symbol:
            _fail("state clear needs --symbol")
            return EXIT_USAGE
        args = {"symbol": opts.symbol}
        _put(args, "market", opts.market)
        return _emit(client.call("state.clear", args), opts, render_pairs)

    args = {}
    _put(args, "market", opts.market)
    _put(args, "symbol", opts.symbol)
    return _emit(client.call("state.get", args), opts, render_pairs)


def cmd_instruments(client, opts):
    args = {}
    _put(args, "symbol", opts.symbol)
    return _emit(client.call("instruments", args), opts,
                 lambda r: render_table(
                     r["instruments"],
                     ["symbol", "name", "lot_size", "tier", "base_price",
                      "price_low", "price_high", "tick_size", "max_quantity",
                      "tradable"],
                     align={"lot_size": "r", "base_price": "r",
                            "price_low": "r", "price_high": "r",
                            "tick_size": "r", "max_quantity": "r"}))


def cmd_instrument(client, opts):
    if opts.action == "remove":
        return _emit(client.call("instrument.remove", {"symbol": opts.symbol}),
                     opts, render_pairs)

    args = {"symbol": opts.symbol}
    _put(args, "base_price", opts.base_price)
    # Booleans and zero are legitimate values, so they cannot go through _put.
    if opts.tradable is not None:
        args["tradable"] = opts.tradable

    if opts.action == "add":
        _put(args, "name", opts.name)
        _put(args, "tier", opts.tier)
        if opts.lot_size is not None:
            args["lot_size"] = opts.lot_size
        if opts.shares_outstanding is not None:
            args["shares_outstanding"] = opts.shares_outstanding
        return _emit(client.call("instrument.add", args), opts, render_pairs)

    _put(args, "band_low", opts.band_low)
    _put(args, "band_high", opts.band_high)
    if opts.clear_band:
        args["clear_band_override"] = True
    return _emit(client.call("instrument.set", args), opts, render_pairs)


def cmd_reference(client, opts):
    return _emit(client.call("reference.reload"), opts,
                 lambda r: render_pairs(
                     r, ["instruments", "added", "updated", "withdrawn"]))


# -- market data -------------------------------------------------------------

def cmd_book(client, opts):
    args = {"symbol": opts.symbol, "depth": opts.depth}
    _put(args, "market", opts.market)
    return _emit(client.call("book", args), opts, render_book)


def cmd_ladder(client, opts):
    args = {"symbol": opts.symbol, "rows": opts.rows}
    _put(args, "market", opts.market)
    _put(args, "center", opts.center)
    return _emit(client.call("ladder", args), opts, render_ladder)


def cmd_bbo(client, opts):
    args = {"symbol": opts.symbol}
    _put(args, "market", opts.market)
    return _emit(client.call("bbo", args), opts, lambda r: render_pairs(
        r, ["symbol", "market", "bid", "bid_qty", "ask", "ask_qty", "spread"]))


def cmd_trades(client, opts):
    args = {"symbol": opts.symbol, "limit": opts.limit}
    _put(args, "market", opts.market)
    return _emit(client.call("trades", args), opts,
                 lambda r: render_table(
                     r["trades"],
                     ["time", "price", "quantity", "aggressor", "trade_id"],
                     align={"price": "r", "quantity": "r"}))


def cmd_stats(client, opts):
    args = {}
    _put(args, "market", opts.market)
    _put(args, "symbol", opts.symbol)
    result = client.call("stats", args)

    if opts.symbol:
        return _emit(result, opts, lambda r: render_pairs(
            r, ["symbol", "market", "open", "high", "low", "last", "last_qty",
                "vwap", "volume", "turnover", "trades", "last_time"]))

    return _emit(result, opts, lambda r: render_table(
        r["stats"],
        ["symbol", "bid", "bid_qty", "ask", "ask_qty", "last", "volume",
         "trades"],
        align={"bid": "r", "bid_qty": "r", "ask": "r", "ask_qty": "r",
               "last": "r", "volume": "r", "trades": "r"}))


def cmd_monitor(client, opts):
    """Live view: snapshot first, then redraw as the book and tape change."""
    args = {"symbol": opts.symbol, "depth": opts.depth}
    _put(args, "market", opts.market)

    # Resolve the market once so the subscription topics are exact.
    snapshot = client.call("book", args)
    market = snapshot.get("market")

    client.subscribe(["book:%s:%s" % (market, opts.symbol),
                      "trade:%s:%s" % (market, opts.symbol)])

    if opts.as_json:
        for event in client.events():
            sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        return EXIT_OK

    trades = client.call("trades", {"symbol": opts.symbol, "market": market,
                                    "limit": 10})["trades"]

    def redraw():
        book = client.call("book", args)
        statistics = client.call("stats", {"symbol": opts.symbol,
                                           "market": market})
        panes = [
            "%s  %s   %s" % (opts.symbol, market,
                             time.strftime("%H:%M:%S")),
            "",
            render_book(book),
            "",
            render_pairs({k: statistics.get(k) for k in
                          ("last", "volume", "vwap", "high", "low", "trades")},
                         ["last", "volume", "vwap", "high", "low", "trades"]),
            "",
            "recent trades",
            render_table(trades[-10:],
                         ["time", "price", "quantity", "aggressor"],
                         align={"price": "r", "quantity": "r"}),
        ]
        body = "\n".join(panes)
        sys.stdout.write((CLEAR_SCREEN if not opts.no_clear else "\n") + body + "\n")
        sys.stdout.flush()

    redraw()
    try:
        for event in client.events():
            if event.get("topic", "").startswith("trade:"):
                trades.append(event["data"])
            redraw()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def render_book(data):
    """Depth as a two-sided ladder, best prices adjacent in the middle."""
    bids, asks = data.get("bids", []), data.get("asks", [])
    if not bids and not asks:
        return "(empty book)"

    rows = []
    for index in range(max(len(bids), len(asks))):
        bid = bids[index] if index < len(bids) else {}
        ask = asks[index] if index < len(asks) else {}
        rows.append({
            "bid_orders": bid.get("orders", ""),
            "bid_qty": bid.get("quantity", ""),
            "bid": bid.get("price", ""),
            "ask": ask.get("price", ""),
            "ask_qty": ask.get("quantity", ""),
            "ask_orders": ask.get("orders", ""),
        })
    return render_table(
        rows, ["bid_orders", "bid_qty", "bid", "ask", "ask_qty", "ask_orders"],
        align={"bid_orders": "r", "bid_qty": "r", "bid": "r"})


def render_ladder(data):
    """The ita board: asks left, price centred, bids right, highest first."""
    rows = data.get("rows", [])
    if not rows:
        return "(no reference price to centre on)"

    width = max(len(row["price"]) for row in rows)
    lines = ["%10s  %s  %-10s" % ("ASK", "PRICE".center(width), "BID"),
             "%10s  %s  %-10s" % ("-" * 10, "-" * width, "-" * 10)]

    if data.get("over"):
        lines.append("%10s  %s" % (data["over"], "OVER".center(width)))
    for row in rows:
        marks = ""
        if row["price"] == data.get("last"):
            marks = " <- last"
        lines.append("%10s  %s  %-10s%s" % (
            row["ask_qty"] or "", row["price"].rjust(width),
            row["bid_qty"] or "", marks))
    if data.get("under"):
        lines.append("%10s  %s  %-10s" % ("", "UNDER".center(width), data["under"]))

    lines.append("")
    lines.append("anchor %s   tick %s   band %s - %s" % (
        data.get("anchor"), data.get("tick"),
        data.get("limit_down"), data.get("limit_up")))
    return "\n".join(lines)


# -- orders ------------------------------------------------------------------

def cmd_orders(client, opts):
    args = {"live": not opts.all}
    _put(args, "market", opts.market)
    _put(args, "symbol", opts.symbol)
    _put(args, "session", opts.session)
    return _emit(client.call("orders", args), opts,
                 lambda r: render_table(
                     r["orders"],
                     ["order_id", "cl_ord_id", "market", "symbol", "side",
                      "price", "quantity", "cum_qty", "leaves_qty", "avg_px",
                      "status", "time_in_force"],
                     align={"price": "r", "quantity": "r", "cum_qty": "r",
                            "leaves_qty": "r", "avg_px": "r"}))


def cmd_order(client, opts):
    if opts.action == "get":
        return _emit(client.call("order.get", {"order_id": opts.order_id}),
                     opts, render_pairs)
    return _emit(client.call("order.cancel", {"order_id": opts.order_id}),
                 opts, render_pairs)


# -- sessions ----------------------------------------------------------------

def cmd_sessions(client, opts):
    return _emit(client.call("sessions"), opts,
                 lambda r: render_table(
                     r["sessions"],
                     ["target_comp_id", "state", "connected", "next_out",
                      "next_in", "default_sub_id", "cancel_on_disconnect"],
                     align={"next_out": "r", "next_in": "r"}))


def cmd_session(client, opts):
    command = "session.reset" if opts.action == "reset" else "session.kill"
    return _emit(client.call(command, {"target_comp_id": opts.target_comp_id}),
                 opts, render_pairs)


# -- generic -----------------------------------------------------------------

def cmd_call(client, opts):
    try:
        args = json.loads(opts.args)
    except ValueError as exc:
        _fail("args must be a JSON object: %s" % exc)
        return EXIT_USAGE
    if not isinstance(args, dict):
        _fail("args must be a JSON object")
        return EXIT_USAGE
    return _emit(client.call(opts.name, args), opts, _guess_layout)


def cmd_watch(client, opts):
    client.subscribe(opts.topics)
    if not opts.as_json:
        sys.stderr.write("watching %s -- Ctrl-C to stop\n" % ", ".join(opts.topics))
        sys.stderr.flush()
    try:
        for event in client.events():
            if opts.as_json:
                sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
            else:
                sys.stdout.write("%-28s %s\n" % (
                    event.get("topic", ""),
                    json.dumps(event.get("data"), separators=(",", ":"))))
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


# -- output ------------------------------------------------------------------

def _guess_layout(result):
    """Pick a sensible rendering for an arbitrary command result."""
    if isinstance(result, list):
        if result and all(isinstance(item, dict) for item in result):
            return render_table(result)
        return "\n".join(str(item) for item in result)
    if isinstance(result, dict):
        if len(result) == 1:
            only = list(result.values())[0]
            if isinstance(only, list) and only and all(
                    isinstance(item, dict) for item in only):
                return render_table(only)
        if any(isinstance(value, (dict, list)) for value in result.values()):
            return as_json(result)
        return render_pairs(result)
    return str(result)


def _emit(result, opts, layout):
    if opts.as_json:
        sys.stdout.write(as_json(result) + "\n")
    else:
        sys.stdout.write(layout(result) + "\n")
    return EXIT_OK


def cmd_audit(client, opts):
    if opts.seq is not None:
        result = client.call("audit.entry", {"seq": opts.seq})
        if opts.wire:
            return _emit(result, opts, lambda r: r.get("wire") or "")
        if result["entry"]["kind"] == "control":
            # A command has arguments and a reply, not tags.
            return _emit(result, opts,
                         lambda r: json.dumps(r["detail"], indent=2))
        return _emit(result, opts, lambda r: render_table(
            r["fields"], ["tag", "name", "value", "label"],
            align={"tag": "r"}))

    args = {"limit": opts.limit}
    _put(args, "symbol", opts.symbol)
    _put(args, "kind", opts.kind)
    _put(args, "direction", opts.direction)
    _put(args, "since", opts.since)
    _put(args, "until", opts.until)
    if opts.msg_type:
        args["types"] = [opts.msg_type]
    if opts.exclude:
        args["exclude_types"] = [part for part in opts.exclude.split(",") if part]

    result = client.call("audit", args)
    return _emit(result, opts, lambda r: render_table(
        [{"seq": e["seq"],
          "time": (e["time"] or "")[11:19],
          "": "<--" if e["direction"] == "in" else "-->",
          "message": e["type_name"] or e["type"],
          "symbol": e["symbol"] or "",
          "detail": e["summary"] or ""}
         for e in r["entries"]],
        ["seq", "time", "", "message", "symbol", "detail"],
        align={"seq": "r"}))


def _put(args, key, value):
    """Include an argument only when it has a value."""
    if value:
        args[key] = value


def _fail(message):
    sys.stderr.write("exsim: %s\n" % message)


_HANDLERS = {
    "ping": cmd_ping,
    "commands": cmd_commands,
    "info": cmd_info,
    "markets": cmd_markets,
    "assumptions": cmd_assumptions,
    "state": cmd_state,
    "instruments": cmd_instruments,
    "instrument": cmd_instrument,
    "reference": cmd_reference,
    "ladder": cmd_ladder,
    "book": cmd_book,
    "bbo": cmd_bbo,
    "trades": cmd_trades,
    "stats": cmd_stats,
    "monitor": cmd_monitor,
    "orders": cmd_orders,
    "order": cmd_order,
    "sessions": cmd_sessions,
    "session": cmd_session,
    "audit": cmd_audit,
    "call": cmd_call,
    "watch": cmd_watch,
}


if __name__ == "__main__":
    sys.exit(main())
