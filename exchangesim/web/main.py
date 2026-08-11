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
from ..core.logutil import configure as configure_logging
from ..core.reactor import Reactor
from .app import WebApp

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
        allow_audit=config.get("allow_audit", True))


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
        config.resolve_path("log.file"))

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

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    sys.exit(main())
