"""FIX listener: routes incoming connections to their configured session.

A connection is anonymous until its first message arrives, so the acceptor
buffers bytes, reads the CompIDs out of the first complete message, and hands
both the session and the buffered bytes over. The session layer then sees a
clean stream starting at the Logon.

Enforces the specification's "only one active connection is allowed per FIX
session": a second connection claiming a live session is refused with a Logout
rather than displacing the incumbent.
"""

import logging

from ..audit import DIRECTION_IN, DIRECTION_OUT
from . import constants as C
from .message import (
    IncompleteMessage,
    MalformedMessage,
    Message,
    decode,
    encode,
    extract,
)
from .session import Session
from .store import FileStore, MemoryStore, session_directory

log = logging.getLogger(__name__)

#: How often sessions are polled for heartbeat and TestRequest policing.
TICK_SECONDS = 1.0

#: Cap on bytes buffered from an unidentified connection.
MAX_ROUTING_BYTES = 16384


class SessionManager(object):
    """Owns every configured session for a venue and resolves them by CompID."""

    def __init__(self, clock, dictionary, store_root=None, audit=None):
        self.clock = clock
        self.dictionary = dictionary
        self.store_root = store_root
        self.audit = audit
        self._sessions = {}     # (sender, target) -> Session

    def add(self, config, application=None):
        """Create a session from its config, wiring up a store."""
        if self.store_root:
            store = FileStore(session_directory(
                self.store_root, config.sender_comp_id, config.target_comp_id))
        else:
            store = MemoryStore()
        store.open()

        session = Session(config, store, self.clock, self.dictionary,
                          application, audit=self.audit)
        self._sessions[(config.sender_comp_id, config.target_comp_id)] = session
        log.info("session configured: %s -> %s (next_out=%d, next_in=%d)",
                 config.sender_comp_id, config.target_comp_id,
                 store.next_out, store.next_in)
        return session

    def get(self, sender_comp_id, target_comp_id):
        """Resolve by our own CompIDs (note: reversed relative to the wire)."""
        return self._sessions.get((sender_comp_id, target_comp_id))

    def resolve_inbound(self, message):
        """Find the session a received message belongs to.

        The client's SenderCompID is our TargetCompID and vice versa.
        """
        return self._sessions.get((message.get(C.TARGET_COMP_ID),
                                   message.get(C.SENDER_COMP_ID)))

    @property
    def sessions(self):
        return list(self._sessions.values())

    def describe(self):
        return [session.describe() for session in self._sessions.values()]

    def tick(self):
        for session in self._sessions.values():
            session.tick()

    def close(self):
        for session in self._sessions.values():
            if session.connected:
                session.disconnect("simulator shutting down")
            session.store.close()


class _Router(object):
    """Buffers an unidentified connection until its session can be determined."""

    __slots__ = ("conn", "acceptor", "buffer", "peer")

    def __init__(self, conn, acceptor):
        self.conn = conn
        self.acceptor = acceptor
        self.buffer = b""
        self.peer = "%s:%d" % conn.peer[:2]

    def on_data(self, conn, chunk):
        self.buffer += chunk

        try:
            raw, _rest = extract(self.buffer)
        except IncompleteMessage:
            if len(self.buffer) > MAX_ROUTING_BYTES:
                log.warning("%s sent %d bytes without a complete message; closing",
                            self.peer, len(self.buffer))
                self._record(None, "%d bytes without a complete message"
                                   % len(self.buffer))
                conn.close()
            return
        except MalformedMessage as exc:
            log.warning("%s sent an unframeable message: %s", self.peer, exc)
            self._record(self.buffer, "unframeable message: %s" % exc)
            conn.close()
            return

        try:
            message = decode(raw)
        except MalformedMessage as exc:
            log.warning("%s sent a malformed first message: %s", self.peer, exc)
            self._record(raw, "malformed first message: %s" % exc)
            conn.close()
            return

        self.acceptor.route(conn, message, self.buffer)

    def _record(self, raw, error):
        """Record traffic dropped before any session could be identified.

        Without this, a client whose very first bytes are wrong is closed on
        silently -- which is precisely the failure someone opens the audit to
        understand.
        """
        audit = self.acceptor.manager.audit
        if audit is not None:
            audit.record_message(DIRECTION_IN, self.peer, raw=raw, error=error)

    def on_close(self, _conn):
        log.debug("%s disconnected before identifying a session", self.peer)


class Acceptor(object):
    """Listening socket that binds connections to configured sessions."""

    def __init__(self, reactor, manager, begin_string="FIX.4.2"):
        self.reactor = reactor
        self.manager = manager
        self.begin_string = begin_string
        self._listener = None
        self._tick_timer = None

    @property
    def address(self):
        return self._listener.address if self._listener else None

    def start(self, host, port):
        self._listener = self.reactor.listen(host, port, self._on_accept)
        self._schedule_tick()
        log.info("FIX acceptor on %s:%d", *self._listener.address[:2])
        return self._listener.address

    def stop(self):
        if self._tick_timer is not None:
            self._tick_timer.cancel()
            self._tick_timer = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def _schedule_tick(self):
        def fire():
            self.manager.tick()
            self._schedule_tick()
        self._tick_timer = self.reactor.call_later(TICK_SECONDS, fire)

    def _on_accept(self, conn):
        router = _Router(conn, self)
        conn.on_data = router.on_data
        conn.on_close = router.on_close
        log.debug("FIX connection from %s", router.peer)

    # -- routing -----------------------------------------------------------

    def route(self, conn, message, buffered):
        """Attach ``conn`` to the session identified by ``message``."""
        peer = "%s:%d" % conn.peer[:2]
        session = self.manager.resolve_inbound(message)

        if session is None:
            log.warning("%s: no session for SenderCompID '%s' / TargetCompID '%s'",
                        peer, message.get(C.SENDER_COMP_ID),
                        message.get(C.TARGET_COMP_ID))
            self._refuse(conn, message, "unknown SenderCompID or TargetCompID")
            return

        if session.connected:
            log.warning("%s: session %s->%s already has an active connection",
                        peer, session.sender_comp_id, session.target_comp_id)
            self._refuse(conn, message,
                         "session already has an active connection")
            return

        log.info("%s bound to session %s->%s", peer,
                 session.sender_comp_id, session.target_comp_id)

        session.attach(conn)
        conn.data = session
        conn.on_data = lambda _c, chunk: session.on_data(chunk)
        conn.on_close = lambda _c: session.detach("connection closed")

        # Replay everything buffered during routing, including this message.
        session.on_data(buffered)

    def _refuse(self, conn, message, reason):
        """Send a best-effort Logout to a connection we will not serve.

        There is no session and therefore no sequence state, so sequence number
        1 is used. The client sees a reason rather than a bare TCP reset.
        """
        logout = Message.create(C.LOGOUT)
        logout.set(C.MSG_SEQ_NUM, 1)
        logout.set(C.SENDER_COMP_ID, message.get(C.TARGET_COMP_ID) or "UNKNOWN")
        logout.set(C.TARGET_COMP_ID, message.get(C.SENDER_COMP_ID) or "UNKNOWN")
        logout.set(C.SENDING_TIME, self.manager.clock.timestamp())
        logout.set(C.TEXT, reason)
        raw = encode(logout, self.begin_string)

        # Recorded here rather than in Session._transmit, which this path does
        # not reach: there is no session to transmit through, and a refusal is
        # the first thing worth seeing when a client cannot get logged on.
        audit = self.manager.audit
        if audit is not None:
            claimed = message.get(C.SENDER_COMP_ID) or "%s:%d" % conn.peer[:2]
            audit.record_message(DIRECTION_IN, claimed, message=message,
                                 type_name=self._type_name(message),
                                 error="refused: %s" % reason)
            audit.record_message(DIRECTION_OUT, claimed, message=logout,
                                 raw=raw, type_name=self._type_name(logout))

        conn.send(raw)
        conn.close_when_flushed()

    def _type_name(self, message):
        definition = self.manager.dictionary.message(message.msg_type)
        return definition.name if definition is not None else None
