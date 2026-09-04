"""Accepting a connection and binding it to a session, whatever the protocol.

A connection is anonymous until its first message arrives, so the acceptor
buffers bytes, decodes the first complete message through the codec, and asks
the session manager which session it belongs to. The session layer then sees a
clean stream starting at that message.

None of that is FIX. It moved here when a third protocol needed it, and what
stayed in :mod:`exchangesim.fix.acceptor` is the part that genuinely knows a
dialect: how a session is stored, how a message names its session, and what a
refusal looks like. Those are the three hooks a manager supplies --
``resolve_inbound``, ``refusal`` and ``claimant``.

Enforces "only one active connection per session": a second connection claiming
a live session is refused rather than displacing the incumbent. Every venue
here wants that, and the two whose specifications mention it say so.
"""

import logging

from ..audit import DIRECTION_IN, DIRECTION_OUT
from ..fix.message import IncompleteMessage, MalformedMessage

log = logging.getLogger(__name__)

#: How often sessions are polled for heartbeat and liveness.
TICK_SECONDS = 1.0

#: Cap on bytes buffered from an unidentified connection.
MAX_ROUTING_BYTES = 16384

#: How each protocol's opening bytes look. Approximate on purpose: this feeds a
#: diagnostic, never a decision about how to read a stream.
_PROTOCOL_NAMES = {
    "fix": "tag=value FIX",
    "binary": "an OCG-C binary frame",
    "nnf": "an NNF packet",
}


def guess_protocol(buffer):
    """Which protocol these opening bytes resemble, or None."""
    if not buffer:
        return None
    if buffer[:2] == b"8=":
        return "fix"
    if buffer[0] == 0x02:
        return "binary"
    # An NNF packet opens with a big-endian length capped at 1024, so the first
    # byte is zero for every message shorter than 256 bytes -- which is most.
    if buffer[0] == 0x00:
        return "nnf"
    return None


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
        codec = self.acceptor.codec

        try:
            raw, _rest = codec.extract(self.buffer)
        except IncompleteMessage:
            if len(self.buffer) > MAX_ROUTING_BYTES:
                log.warning("%s sent %d bytes without a complete message; closing",
                            self.peer, len(self.buffer))
                self._record(None, "%d bytes without a complete message"
                                   % len(self.buffer))
                conn.close()
            return
        except MalformedMessage as exc:
            reason = "unframeable message: %s%s" % (exc, self._wrong_protocol())
            log.warning("%s sent an unframeable message: %s", self.peer, reason)
            self._record(self.buffer, reason)
            conn.close()
            return

        try:
            message = codec.decode(raw)
        except MalformedMessage as exc:
            log.warning("%s sent a malformed first message: %s", self.peer, exc)
            self._record(raw, "malformed first message: %s" % exc)
            conn.close()
            return

        self.acceptor.route(conn, message, self.buffer)

    def _wrong_protocol(self):
        """A hint when the bytes look like a protocol this port does not speak.

        Nothing about a stream says which protocol it is, so a client aimed at
        the wrong port produces a framing error and no explanation at all. This
        is that explanation, and it is worth the two lines: it is exactly the
        question somebody opens the audit with.
        """
        looks_like = guess_protocol(self.buffer)
        speaking = self.acceptor.codec.name
        if looks_like is None or looks_like == speaking:
            return ""
        return (" (these bytes look like %s, and this port speaks %s)"
                % (_PROTOCOL_NAMES.get(looks_like, looks_like),
                   _PROTOCOL_NAMES.get(speaking, speaking)))

    def _record(self, raw, error):
        """Record traffic dropped before any session could be identified.

        Without this, a client whose very first bytes are wrong is closed on
        silently -- which is precisely the failure someone opens the audit to
        understand.
        """
        audit = self.acceptor.manager.audit
        if audit is not None:
            audit.record_message(DIRECTION_IN, self.peer, raw=raw, error=error,
                                 protocol=self.acceptor.codec.name)

    def on_close(self, _conn):
        log.debug("%s disconnected before identifying a session", self.peer)


class Acceptor(object):
    """Listening socket that binds connections to configured sessions."""

    def __init__(self, reactor, manager, codec=None, dictionary=None,
                 tick=True):
        self.reactor = reactor
        self.manager = manager
        #: The encoding this port serves. A venue that publishes its protocol
        #: in two encodings runs one acceptor per encoding, over one session
        #: table: a Comp ID is registered for one of them, never both.
        self.codec = codec or manager.codec
        self.dictionary = dictionary or manager.dictionary
        #: Whether this acceptor polls the session table. False for the
        #: second acceptor of a venue whose encodings share one.
        self.tick = tick
        self._listener = None
        self._tick_timer = None

    @property
    def address(self):
        return self._listener.address if self._listener else None

    def start(self, host, port):
        self._listener = self.reactor.listen(host, port, self._on_accept)
        if self.tick:
            self._schedule_tick()
        log.info("%s acceptor on %s:%d", self.codec.name,
                 *self._listener.address[:2])
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
        log.debug("%s connection from %s", self.codec.name, router.peer)

    # -- routing -----------------------------------------------------------

    def route(self, conn, message, buffered):
        """Attach ``conn`` to the session identified by ``message``."""
        peer = "%s:%d" % conn.peer[:2]
        session = self.manager.resolve_inbound(message)

        if session is None:
            log.warning("%s: no session for %s", peer,
                        self.manager.claimant(message) or "the identity sent")
            self._refuse(conn, message, self.manager.unknown_session_reason)
            return

        if session.wire != self.codec.name:
            # The identity exists but was registered for the venue's other
            # encoding. Answering in this one would be answering a question the
            # client did not ask, so it is refused like an unknown session.
            log.warning("%s: session %s speaks %s, not %s", peer,
                        session.target_comp_id, session.wire, self.codec.name)
            self._refuse(conn, message,
                         "session %s is configured for the %s protocol"
                         % (session.target_comp_id, session.wire))
            return

        if session.connected:
            log.warning("%s: session %s already has an active connection",
                        peer, session.target_comp_id)
            self._refuse(conn, message,
                         "session already has an active connection")
            return

        log.info("%s bound to session %s", peer, session.target_comp_id)

        session.attach(conn)
        conn.data = session
        conn.on_data = lambda _c, chunk: session.on_data(chunk)
        conn.on_close = lambda _c: session.detach("connection closed")

        # Replay everything buffered during routing, including this message.
        session.on_data(buffered)

    def _refuse(self, conn, message, reason):
        """Tell a connection we will not serve it, then close.

        What the refusal *is* belongs to the protocol -- a Logout in FIX, a
        response carrying an error code in NNF -- so the manager builds it. The
        recording and the closing are the same either way.
        """
        refusal, raw = self.manager.refusal(message, reason, self.codec)

        # Recorded here rather than in the session, which this path does not
        # reach: there is no session to transmit through, and a refusal is the
        # first thing worth seeing when a client cannot get connected.
        audit = self.manager.audit
        if audit is not None:
            claimed = self.manager.claimant(message) or "%s:%d" % conn.peer[:2]
            audit.record_message(DIRECTION_IN, claimed, message=message,
                                 type_name=self._type_name(message),
                                 error="refused: %s" % reason,
                                 protocol=self.codec.name)
            if refusal is not None:
                audit.record_message(DIRECTION_OUT, claimed, message=refusal,
                                     raw=raw, type_name=self._type_name(refusal),
                                     protocol=self.codec.name)

        if raw:
            conn.send(raw)
        conn.close_when_flushed()

    def _type_name(self, message):
        definition = self.dictionary.message(message.msg_type)
        return definition.name if definition is not None else None
