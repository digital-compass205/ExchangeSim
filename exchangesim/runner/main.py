"""``exsimd`` -- run one simulated venue.

Builds the shared reactor, clock and publisher, constructs the venue named by
the config, starts the control plane, and serves until interrupted.

One venue per process, so that a crash or restart of one exchange leaves the
others on the host untouched and each gets its own ports and store directory.
"""

import argparse
import logging
import os
import signal
import sys

from ..control.builtin import register as register_builtin
from ..control.commands import CommandRegistry
from ..control.server import ControlServer
from ..control.subscriptions import Publisher
from ..core.clock import RealClock
from ..core.config import Config, ConfigError
from ..core.logutil import configure as configure_logging
from ..core.reactor import Reactor
from ..venues import registry as venue_registry

log = logging.getLogger("exchangesim.runner")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_RUNTIME = 3


def build_parser():
    parser = argparse.ArgumentParser(
        prog="exsimd", description="Run an exchange simulator instance.")
    parser.add_argument("--config", "-c", required=True,
                        help="path to the venue config JSON file")
    parser.add_argument("--control-port", type=int, default=None,
                        help="override control.port from the config")
    parser.add_argument("--log-level", default=None,
                        help="override log.level from the config")
    parser.add_argument("--check", action="store_true",
                        help="load and validate the config, then exit")
    return parser


class Runtime(object):
    """Everything a running instance owns. Constructed, started, then served."""

    def __init__(self, config):
        self.config = config
        self.reactor = Reactor(RealClock())
        self.publisher = Publisher()
        self.registry = CommandRegistry()
        self.venue = None
        self.control = None

    def build(self):
        venue_key = self.config.get("venue", "generic")
        self.venue = venue_registry.create(
            venue_key, self.config, self.reactor, self.publisher)

        register_builtin(self.registry)
        self.venue.register_commands(self.registry)
        # Control commands are recorded on the same tape as FIX messages, so an
        # order entered from the web board sits beside the traffic it caused.
        self.registry.audit = self.venue.audit

        self.control = ControlServer(
            self.reactor, self.registry, self.publisher,
            venue=self.venue, token=self.config.get("control.token"))
        return self

    def start(self):
        self.venue.start()
        host = self.config.get("control.host", "127.0.0.1")
        port = self.config.get("control.port", 9101)
        self.control.start(host, port)

    def stop(self):
        if self.control is not None:
            self.control.stop()
        if self.venue is not None:
            self.venue.stop()
        self.reactor.close()


def main(argv=None):
    opts = build_parser().parse_args(argv)

    try:
        config = Config.load(opts.config)
    except ConfigError as exc:
        sys.stderr.write("exsimd: %s\n" % exc)
        return EXIT_CONFIG

    if opts.control_port is not None:
        config.data.setdefault("control", {})["port"] = opts.control_port

    configure_logging(
        opts.log_level or config.get("log.level", "INFO"),
        config.resolve_path("log.file"))

    try:
        runtime = Runtime(config).build()
    except ConfigError as exc:
        sys.stderr.write("exsimd: %s\n" % exc)
        return EXIT_CONFIG

    if opts.check:
        sys.stdout.write("config OK: venue=%s control=%s:%s\n" % (
            config.get("venue", "generic"),
            config.get("control.host", "127.0.0.1"),
            config.get("control.port", 9101)))
        return EXIT_OK

    try:
        runtime.start()
    except OSError as exc:
        log.error("failed to start: %s", exc)
        runtime.stop()
        return EXIT_RUNTIME

    _install_signal_handlers(runtime)

    log.info("exsimd ready (pid %d) -- venue '%s'", os.getpid(), runtime.venue.name)
    try:
        runtime.reactor.run()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        runtime.stop()
        log.info("exsimd stopped")
    return EXIT_OK


def _install_signal_handlers(runtime):
    def handle(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        runtime.reactor.stop()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                # Not the main thread, or unsupported on this platform.
                pass


if __name__ == "__main__":
    sys.exit(main())
