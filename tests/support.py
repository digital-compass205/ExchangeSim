"""Shared test scaffolding.

Two ways to drive the stack:

* :func:`pump` -- step the reactor by hand in the test thread. Preferred: no
  threads, no sleeps, and completely deterministic. Works whenever the test can
  write to a socket without blocking, which is every case at simulator message
  sizes.
* :class:`ServerThread` -- run the reactor in a background thread, for tests
  that use the blocking :class:`ControlClient` as a real client would.
"""

import socket
import threading
import time

from exchangesim.control.builtin import register as register_builtin
from exchangesim.control.commands import CommandRegistry
from exchangesim.control.server import ControlServer
from exchangesim.control.subscriptions import Publisher
from exchangesim.core.clock import FixedClock
from exchangesim.core.reactor import Reactor


def pump(reactor, rounds=8):
    """Step the reactor until it goes quiet, or ``rounds`` is exhausted.

    Returns the total number of callbacks fired, so a test can assert that
    something actually happened rather than silently passing on a no-op.
    """
    total = 0
    for _ in range(rounds):
        handled = reactor.step(0.0)
        total += handled
        if handled == 0:
            break
    return total


def pump_until(reactor, predicate, timeout=5.0):
    """Step the reactor until ``predicate`` holds, or ``timeout`` elapses.

    Unlike :func:`pump`, this tolerates a quiet round: an outbound connect can
    take a moment to report writable, and stopping at the first idle step would
    make the test depend on how fast the loopback stack happens to be.
    """
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        reactor.step(0.02)
    return predicate()


def connect(address, timeout=5.0):
    """Blocking client socket connected to ``(host, port)``."""
    sock = socket.create_connection(address, timeout)
    sock.settimeout(timeout)
    return sock


def read_line(sock, buffer_holder):
    """Read one newline-delimited message, carrying leftovers in a list cell."""
    while b"\n" not in buffer_holder[0]:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError("connection closed while awaiting a line")
        buffer_holder[0] += chunk
    line, buffer_holder[0] = buffer_holder[0].split(b"\n", 1)
    return line


class ServerThread(object):
    """A control server on an ephemeral port, served by a background thread."""

    def __init__(self, venue=None, token=None, clock=None, extra=None):
        self.reactor = Reactor(clock or FixedClock())
        self.publisher = Publisher()
        self.registry = CommandRegistry()
        register_builtin(self.registry)
        if extra is not None:
            extra(self.registry)
        self.server = ControlServer(
            self.reactor, self.registry, self.publisher, venue=venue, token=token)
        self.address = None
        self._thread = None

    def __enter__(self):
        self.address = self.server.start("127.0.0.1", 0)
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()
        return self

    def _serve(self):
        try:
            self.reactor.run(poll_interval=0.02)
        except Exception:
            pass

    @property
    def port(self):
        return self.address[1]

    def __exit__(self, exc_type, exc_value, traceback):
        self.reactor.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.server.stop()
        self.reactor.close()
        return False


class StubVenue(object):
    """Minimal stand-in where a test only needs a venue to have a name."""

    def __init__(self, name="TEST-VENUE"):
        self.name = name
        self.key = "stub"

    def describe(self):
        return {"venue": self.key, "name": self.name, "started": True}
