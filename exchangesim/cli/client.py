"""Blocking client for the JSON-lines control plane.

Deliberately blocking and thread-free: the CLI does one thing at a time, and a
plain socket keeps it easy to reason about in CI, where a hung client should
fail on a timeout rather than spin.
"""

import itertools
import json
import socket


class ControlClientError(Exception):
    """Transport or protocol failure talking to the control plane."""


class CommandFailed(ControlClientError):
    """The server understood the command and rejected it."""

    def __init__(self, code, message):
        ControlClientError.__init__(self, "%s: %s" % (code, message))
        self.code = code
        self.message = message


class ControlClient(object):
    """Synchronous request/response client with streaming support."""

    def __init__(self, host="127.0.0.1", port=9101, timeout=10.0, token=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.token = token
        self._sock = None
        self._buffer = b""
        self._ids = itertools.count(1)

    # -- lifecycle ---------------------------------------------------------

    def connect(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise ControlClientError(
                "cannot connect to control plane at %s:%d: %s"
                % (self.host, self.port, exc))
        self._sock.settimeout(self.timeout)
        if self.token:
            self.call("auth", {"token": self.token})
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    # -- requests ----------------------------------------------------------

    def call(self, command, args=None):
        """Send a command and return its result, raising on failure.

        Event pushes arriving while waiting for the reply are skipped, so a
        subscribed connection can still issue commands.
        """
        request_id = next(self._ids)
        self._send({"id": request_id, "cmd": command, "args": args or {}})
        while True:
            message = self._read_message()
            if message.get("event"):
                continue
            if message.get("id") != request_id:
                continue
            if message.get("ok"):
                return message.get("result")
            error = message.get("error") or {}
            raise CommandFailed(error.get("code", "unknown"),
                                error.get("message", "no message"))

    def subscribe(self, topics):
        return self.call("subscribe", {"topics": list(topics)})

    def events(self):
        """Yield pushed events forever.

        The socket timeout is cleared first: a monitor is expected to sit idle
        for long stretches between updates.
        """
        self._sock.settimeout(None)
        while True:
            message = self._read_message()
            if message.get("event"):
                yield message

    # -- framing -----------------------------------------------------------

    def _send(self, payload):
        if self._sock is None:
            raise ControlClientError("not connected")
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._sock.sendall(line)
        except OSError as exc:
            raise ControlClientError("send failed: %s" % exc)

    def _read_message(self):
        while b"\n" not in self._buffer:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise ControlClientError(
                    "timed out after %.1fs waiting for the control plane" % self.timeout)
            except OSError as exc:
                raise ControlClientError("receive failed: %s" % exc)
            if not chunk:
                raise ControlClientError("control plane closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            return json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ControlClientError("malformed response: %s" % exc)
