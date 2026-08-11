"""Newline-delimited JSON control server.

Wire format -- one JSON object per line, in both directions.

Request::

    {"id": 7, "cmd": "state.set", "args": {"market": "DAY", "state": "OPEN"}}

Response::

    {"id": 7, "ok": true,  "result": {...}}
    {"id": 7, "ok": false, "error": {"code": "bad_args", "message": "..."}}

Asynchronous push (only after ``subscribe``)::

    {"event": true, "topic": "trade:DAY:7203", "data": {...}}

Line-delimited JSON rather than HTTP because streaming subscriptions are the
awkward case for ``http.server``, and because a request/response plus
server-initiated push over one socket is exactly what the CLI monitor needs.
"""

import json
import logging

from .commands import (
    CommandError,
    E_BAD_REQUEST,
    E_INTERNAL,
    E_UNAUTHORISED,
)

log = logging.getLogger(__name__)

#: Guard against a client that never sends a newline.
MAX_LINE_BYTES = 1 << 20


class CommandContext(object):
    """What a command handler is given: the venue, the caller, the server."""

    __slots__ = ("venue", "session", "server")

    def __init__(self, venue, session, server):
        self.venue = venue
        self.session = session
        self.server = server

    @property
    def publisher(self):
        return self.server.publisher


class ControlSession(object):
    """One connected control client."""

    __slots__ = ("conn", "server", "_buffer", "authenticated", "peer")

    def __init__(self, conn, server):
        self.conn = conn
        self.server = server
        self._buffer = b""
        self.peer = "%s:%d" % conn.peer[:2]
        # With no token configured every connection starts authenticated.
        self.authenticated = not server.requires_auth

    # -- inbound -----------------------------------------------------------

    def on_data(self, _conn, chunk):
        self._buffer += chunk
        if len(self._buffer) > MAX_LINE_BYTES:
            self._send({"ok": False, "error": {
                "code": E_BAD_REQUEST, "message": "request line too long"}})
            self.conn.close_when_flushed()
            return
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line):
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send({"ok": False, "error": {
                "code": E_BAD_REQUEST, "message": "invalid JSON: %s" % exc}})
            return

        if not isinstance(request, dict):
            self._send({"ok": False, "error": {
                "code": E_BAD_REQUEST, "message": "request must be a JSON object"}})
            return

        request_id = request.get("id")
        name = request.get("cmd")
        if not isinstance(name, str) or not name:
            self._reply(request_id, False, error={
                "code": E_BAD_REQUEST, "message": "missing 'cmd'"})
            return

        args = request.get("args", {})
        command = self.server.registry.get(name)

        if command is not None and command.requires_auth and not self.authenticated:
            # Recorded here because it never reaches dispatch, and an attempt to
            # drive the venue without credentials is the most worth seeing of
            # any control event. The arguments are deliberately not kept.
            audit = self.server.registry.audit
            if audit is not None:
                audit.record_command(name, {}, session=self.peer, ok=False,
                                     error={"code": E_UNAUTHORISED,
                                            "message": "not authenticated"})
            self._reply(request_id, False, error={
                "code": E_UNAUTHORISED, "message": "authenticate first"})
            return

        context = CommandContext(self.server.venue, self, self.server)
        try:
            result = self.server.registry.dispatch(name, context, args)
        except CommandError as exc:
            self._reply(request_id, False, error={
                "code": exc.code, "message": exc.message})
            return
        except Exception as exc:
            log.exception("control command '%s' from %s failed", name, self.peer)
            self._reply(request_id, False, error={
                "code": E_INTERNAL, "message": "%s: %s" % (type(exc).__name__, exc)})
            return

        self._reply(request_id, True, result=result)

    # -- outbound ----------------------------------------------------------

    def _reply(self, request_id, ok, result=None, error=None):
        message = {"id": request_id, "ok": ok}
        if ok:
            message["result"] = result
        else:
            message["error"] = error
        self._send(message)

    def push(self, topic, data):
        """Deliver a subscription event. Called by the :class:`Publisher`."""
        self._send({"event": True, "topic": topic, "data": data})

    def _send(self, message):
        if self.conn.closed:
            return
        try:
            payload = json.dumps(message, separators=(",", ":"), default=_fallback)
        except (TypeError, ValueError):
            log.exception("control response is not JSON-serialisable")
            return
        self.conn.send(payload.encode("utf-8") + b"\n")

    def on_close(self, _conn):
        self.server.publisher.remove(self)
        self.server.sessions.discard(self)
        log.debug("control client %s disconnected", self.peer)


def _fallback(value):
    """Last-resort encoder so a stray object cannot kill a response."""
    return str(value)


class ControlServer(object):
    """Listens for control clients and routes their commands."""

    def __init__(self, reactor, registry, publisher, venue=None, token=None):
        self.reactor = reactor
        self.registry = registry
        self.publisher = publisher
        self.venue = venue
        self.token = token
        self.sessions = set()
        self._listener = None

    @property
    def requires_auth(self):
        return bool(self.token)

    @property
    def address(self):
        return self._listener.address if self._listener else None

    def start(self, host, port):
        self._listener = self.reactor.listen(host, port, self._on_accept)
        log.info("control plane on %s:%d%s", self._listener.address[0],
                 self._listener.address[1],
                 " (token required)" if self.requires_auth else "")
        return self._listener.address

    def stop(self):
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        for session in list(self.sessions):
            session.conn.close()

    def _on_accept(self, conn):
        session = ControlSession(conn, self)
        conn.on_data = session.on_data
        conn.on_close = session.on_close
        conn.data = session
        self.sessions.add(session)
        log.debug("control client %s connected", session.peer)
