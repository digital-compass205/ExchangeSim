"""``exsimweb`` -- serve the board for one or more running venues.

Its own process, and a *client* of the venues rather than part of them: the
one-process-per-exchange rule still holds, a venue restart is invisible here
beyond a reconnect, and nothing in this process can stall a matching engine.
"""

import argparse
import logging
import os
import signal
import sys

from ..core.clock import RealClock
from ..core.config import Config, ConfigError
from ..core.logutil import DEFAULT_BACKUPS, DEFAULT_MAX_BYTES
from ..core.logutil import configure as configure_logging
from ..core.reactor import Reactor
from .app import DEFAULT_BOARD, WebApp

log = logging.getLogger("exchangesim.web")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_RUNTIME = 3


def build_parser():
    parser = argparse.ArgumentParser(
        prog="exsimweb", description="Serve the web board for running venues.")
    parser.add_argument("--config", "-c", required=True,
                        help="path to the web config JSON file")
    parser.add_argument("--port", type=int, default=None,
                        help="override http.port from the config")
    parser.add_argument("--log-level", default=None,
                        help="override log.level from the config")
    parser.add_argument("--log-file", default=None,
                        help="override log.file from the config")
    parser.add_argument("--no-console", action="store_true",
                        help="log only to the file, not to stderr")
    parser.add_argument("--check", action="store_true",
                        help="load and validate the config, then exit")
    return parser


def build_app(config, reactor):
    venues = config.get("venues") or []
    if not isinstance(venues, list) or not venues:
        raise ConfigError("'venues' must be a non-empty list")

    specs = []
    seen = set()
    for entry in venues:
        if not isinstance(entry, dict):
            raise ConfigError("each entry of 'venues' must be an object")
        key = entry.get("key")
        if not key:
            raise ConfigError("each entry of 'venues' needs a 'key'")
        if key in seen:
            raise ConfigError("duplicate venue key '%s'" % key)
        seen.add(key)
        specs.append({
            "key": key,
            "label": entry.get("label") or key,
            "host": entry.get("host", "127.0.0.1"),
            "port": entry.get("port", 9101),
            "token": entry.get("token"),
        })

    return WebApp(
        reactor, specs,
        allow_order_entry=config.get("allow_order_entry", True),
        allow_market_control=config.get("allow_market_control", True),
        allow_audit=config.get("allow_audit", True),
        board=build_board(config))


#: Which side of the board the buying side sits on.
BUY_SIDES = ("right", "left")

#: The hues ``style.css`` defines for a side. A name rather than a colour, and
#: validated here rather than in the browser: every one of these is checked
#: against the 4.5:1 contrast floor in both themes by
#: ``tests/test_web_palette.py``, and a free-text colour would let a config
#: write a board nobody can read.
SIDE_COLOURS = ("red", "green", "blue", "amber")


def build_board(config):
    """The board's presentation settings, validated.

    Layout and colour only -- nothing here grants or withholds a capability.
    It is served from the config rather than left to the browser so that
    everyone looking at one board reads it the same way round; two people
    describing the same screen differently is a real hazard on a trading desk.
    """
    section = config.get("board")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("'board' must be an object")

    board = {}
    side = section.get("buy_side")
    if side is not None:
        side = str(side).strip().lower()
        if side not in BUY_SIDES:
            raise ConfigError("board.buy_side must be one of %s, not '%s'"
                              % (", ".join(BUY_SIDES), side))
        board["buy_side"] = side

    for name in ("buy", "sell"):
        colour = _side_colour(section, name)
        if colour is not None:
            board["%s_colour" % name] = colour

    buy = board.get("buy_colour", DEFAULT_BOARD["buy_colour"])
    sell = board.get("sell_colour", DEFAULT_BOARD["sell_colour"])
    if buy == sell:
        raise ConfigError(
            "board: buy and sell cannot both be %s -- the two sides are told "
            "apart by colour on the ladder, the ticket and the tape" % buy)
    return board


def _side_colour(section, name):
    """``buy_colour``/``sell_colour``, spelled either way, or None.

    Both spellings are accepted because the alternative is a config that looks
    right, is silently ignored, and leaves the board in its default colours.
    """
    keys = ("%s_colour" % name, "%s_color" % name)
    present = [key for key in keys if section.get(key) is not None]
    if not present:
        return None
    values = set(str(section[key]).strip().lower() for key in present)
    if len(values) > 1:
        raise ConfigError("board: %s and %s disagree" % keys)
    colour = values.pop()
    if colour not in SIDE_COLOURS:
        raise ConfigError("board.%s must be one of %s, not '%s'"
                          % (keys[0], ", ".join(SIDE_COLOURS), colour))
    return colour


def main(argv=None):
    opts = build_parser().parse_args(argv)

    try:
        config = Config.load(opts.config)
    except ConfigError as exc:
        sys.stderr.write("exsimweb: %s\n" % exc)
        return EXIT_CONFIG

    if opts.port is not None:
        config.data.setdefault("http", {})["port"] = opts.port

    configure_logging(
        opts.log_level or config.get("log.level", "INFO"),
        opts.log_file or config.resolve_path("log.file"),
        max_bytes=config.get("log.max_bytes", DEFAULT_MAX_BYTES),
        backups=config.get("log.backups", DEFAULT_BACKUPS),
        console=not opts.no_console)

    reactor = Reactor(RealClock())
    try:
        app = build_app(config, reactor)
    except ConfigError as exc:
        sys.stderr.write("exsimweb: %s\n" % exc)
        return EXIT_CONFIG

    host = config.get("http.host", "127.0.0.1")
    port = config.get("http.port", 9200)

    if opts.check:
        sys.stdout.write("config OK: http=%s:%s venues=%s\n" % (
            host, port, ", ".join(app.order)))
        return EXIT_OK

    try:
        app.start(host, port)
    except OSError as exc:
        log.error("failed to start: %s", exc)
        app.stop()
        return EXIT_RUNTIME

    _install_signal_handlers(reactor)

    log.info("exsimweb ready (pid %d) -- serving %d venue(s) on http://%s:%d",
             os.getpid(), len(app.order), host, port)
    try:
        reactor.run()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        app.stop()
        reactor.close()
        log.info("exsimweb stopped")
    return EXIT_OK


def _install_signal_handlers(reactor):
    def handle(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        reactor.stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    sys.exit(main())
