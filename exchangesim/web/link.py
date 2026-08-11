"""A non-blocking control-plane client, one per venue.

The CLI's :class:`~exchangesim.cli.client.ControlClient` is deliberately
blocking, which is right for a command that does one thing and exits. Inside a
reactor it would be fatal: a stalled venue would freeze the web UI for every
other venue too. This speaks the same JSON-lines protocol over a reactor
connection instead, and reconnects on its own when a venue is restarted.
"""

import itertools
import json
import logging

log = logging.getLogger(__name__)

#: A command with no reply by then is failed, so a browser request cannot hang.
DEFAULT_CALL_TIMEOUT = 10.0

#: Delay before redialling a venue that is down, and the ceiling it backs off to.
#
# The ceiling is deliberately low. Restarting a venue is routine here, not an
# incident, and a developer who restarts one should see the board recover in
# seconds rather than wait out a backoff designed for a remote service. Redialling
# loopback every few seconds costs nothing -- and on Windows each failed attempt
# already takes ~2s to be refused, which lengthens the real interval anyway.
INITIAL_RETRY = 1.0
MAX_RETRY = 5.0

#: Guard against a peer that never sends a newline.
MAX_LINE_BYTES = 4 << 20


class VenueLink(object):
    """One venue's control connection, kept up in the background."""

    def __init__(self, reactor, key, host, port, label=None, token=None,
                 on_event=None, call_timeout=DEFAULT_CALL_TIMEOUT):
        self.reactor = reactor
        self.key = key
        self.label = label or key
        self.host = host
        self.port = port
        self.token = token
        #: ``on_event(venue_key, topic, data)`` for every subscribed push.
        self.on_event = on_event
        self.call_timeout = call_timeout

        self.connected = False
        self.last_error = None
        self.topics = set()

        self._conn = None
        self._buffer = b""
        self._ids = itertools.count(1)
        self._pending = {}          # request id -> (callback, timer)
        self._retry = INITIAL_RETRY
        self._retry_timer = None
        self._stopped = False

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self._stopped = False
        self._dial()
        return self

    def stop(self):
        self._stopped = True
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _dial(self):
        if self._stopped or self._conn is not None:
            return
        log.debug("dialling venue '%s' at %s:%d", self.key, self.host, self.port)
        self._conn = self.reactor.connect(
            self.host, self.port, self._on_connect, self._on_connect_failed)
        self._conn.on_data = self._on_data
        self._conn.on_close = self._on_close

    def _schedule_retry(self):
        if self._stopped or self._retry_timer is not None:
            return
        delay = self._retry
        self._retry = min(self._retry * 2, MAX_RETRY)

        def redial():
            self._retry_timer = None
            self._dial()

        self._retry_timer = self.reactor.call_later(delay, redial)

    # -- connection events -------------------------------------------------

    def _on_connect(self, _conn):
        self.connected = True
        self.last_error = None
        self._retry = INITIAL_RETRY
        log.info("connected to venue '%s' at %s:%d", self.key, self.host, self.port)

        # Ordering carries the authentication: the control plane handles one
        # line at a time, so anything queued behind this is already authorised.
        if self.token:
            self._send({"id": next(self._ids), "cmd": "auth",
                        "args": {"token": self.token}})
        if self.topics:
            self._send({"id": next(self._ids), "cmd": "subscribe",
                        "args": {"topics": sorted(self.topics)}})

    def _on_connect_failed(self, _conn, exc):
        self.last_error = "cannot reach %s:%d (%s)" % (self.host, self.port, exc)
        self._conn = None
        self.connected = False
        log.debug("venue '%s' unreachable: %s", self.key, exc)
        self._fail_pending("venue '%s' is unreachable" % self.key)
        self._schedule_retry()

    def _on_close(self, _conn):
        was_connected = self.connected
        self.connected = False
        self._conn = None
        self._buffer = b""
        if was_connected:
            log.info("venue '%s' disconnected", self.key)
            self.last_error = "connection closed"
        self._fail_pending("venue '%s' disconnected" % self.key)
        self._schedule_retry()

    # -- requests ----------------------------------------------------------

    def call(self, command, args, callback):
        """Invoke a control command; ``callback(ok, payload)`` gets the reply.

        ``payload`` is the result when ok, or an ``{"code", "message"}`` error
        object when not -- including for transport failures, so a caller never
        has to distinguish "the venue said no" from "the venue is not there".
        """
        if not self.connected or self._conn is None:
            callback(False, {"code": "unavailable",
                             "message": self.last_error
                                        or ("venue '%s' is not connected" % self.key)})
            return

        request_id = next(self._ids)
        timer = self.reactor.call_later(
            self.call_timeout, lambda: self._expire(request_id, command))
        self._pending[request_id] = (callback, timer)
        self._send({"id": request_id, "cmd": command, "args": args or {}})

    def subscribe(self, topics):
        """Add topic patterns, now and on every future reconnection."""
        fresh = set(topics) - self.topics
        self.topics |= set(topics)
        if fresh and self.connected:
            self._send({"id": next(self._ids), "cmd": "subscribe",
                        "args": {"topics": sorted(fresh)}})

    def _expire(self, request_id, command):
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return
        log.warning("venue '%s' did not answer '%s' within %.0fs",
                    self.key, command, self.call_timeout)
        entry[0](False, {"code": "timeout",
                         "message": "venue '%s' did not answer '%s' in time"
                                    % (self.key, command)})

    def _fail_pending(self, message):
        pending, self._pending = self._pending, {}
        for callback, timer in pending.values():
            timer.cancel()
            callback(False, {"code": "unavailable", "message": message})

    # -- wire --------------------------------------------------------------

    def _send(self, payload):
        if self._conn is None:
            return
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._conn.send(line + b"\n")

    def _on_data(self, _conn, chunk):
        self._buffer += chunk
        if len(self._buffer) > MAX_LINE_BYTES:
            log.error("venue '%s' sent an oversized line; dropping the link",
                      self.key)
            self._buffer = b""
            if self._conn is not None:
                self._conn.close()
            return

        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("venue '%s' sent malformed JSON: %s", self.key, exc)
            return
        if not isinstance(message, dict):
            return

        if message.get("event"):
            if self.on_event is not None:
                self.on_event(self.key, message.get("topic"), message.get("data"))
            return

        entry = self._pending.pop(message.get("id"), None)
        if entry is None:
            # An auth or subscribe reply, or a response to a call that already
            # timed out. Neither has anyone waiting for it.
            return
        callback, timer = entry
        timer.cancel()
        if message.get("ok"):
            callback(True, message.get("result"))
        else:
            callback(False, message.get("error") or
                     {"code": "unknown", "message": "no message"})

    # -- presentation ------------------------------------------------------

    def describe(self):
        return {
            "key": self.key,
            "label": self.label,
            "host": self.host,
            "port": self.port,
            "connected": self.connected,
            "error": None if self.connected else self.last_error,
        }
