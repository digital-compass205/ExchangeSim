"""FIX's half of accepting a connection: the session table and its store.

:class:`~exchangesim.wire.acceptor.Acceptor` and its router moved to
:mod:`exchangesim.wire.acceptor` when a third protocol needed them, and are
re-exported here so nothing that imports them has to know that. What remains is
the part that genuinely knows FIX: creating a :class:`~.session.Session` over a
sequence store, resolving a session from the CompID pair a message carries, and
building the Logout that refuses a connection we will not serve.
"""

import logging

from ..wire.acceptor import (       # noqa: F401  (re-exported)
    MAX_ROUTING_BYTES,
    TICK_SECONDS,
    Acceptor,
)
from . import constants as C
from .codec import FixCodec
from .message import Message
from .session import Session
from .store import FileStore, MemoryStore, session_directory

log = logging.getLogger(__name__)


class SessionManager(object):
    """Owns every configured session for a venue and resolves them by CompID."""

    #: What an acceptor says when no session matches the identity sent.
    unknown_session_reason = "unknown SenderCompID or TargetCompID"

    def __init__(self, clock, dictionary, store_root=None, audit=None):
        self.clock = clock
        self.dictionary = dictionary
        self.store_root = store_root
        self.audit = audit
        self._sessions = {}     # (sender, target) -> Session

    def add(self, config, application=None, codec=None, dictionary=None):
        """Create a session from its config, wiring up a store.

        ``codec`` and ``dictionary`` default to the manager's, and are given
        per session by a venue that serves one protocol in more than one
        encoding: the Comp ID belongs to exactly one of them, so the session
        table stays single and every control command still sees them all.
        """
        if self.store_root:
            store = FileStore(session_directory(
                self.store_root, config.sender_comp_id, config.target_comp_id))
        else:
            store = MemoryStore()
        store.open()

        session = Session(config, store, self.clock,
                          dictionary or self.dictionary,
                          application, audit=self.audit, codec=codec)
        self._sessions[(config.sender_comp_id, config.target_comp_id)] = session
        log.info("session configured: %s -> %s (next_out=%d, next_in=%d)",
                 config.sender_comp_id, config.target_comp_id,
                 store.next_out, store.next_in)
        return session

    def get(self, sender_comp_id, target_comp_id):
        """Resolve by our own CompIDs (note: reversed relative to the wire)."""
        return self._sessions.get((sender_comp_id, target_comp_id))

    # -- the three hooks a shared acceptor asks of a manager ---------------

    @property
    def codec(self):
        """The encoding an acceptor serves when it is given none."""
        return FixCodec()

    def claimant(self, message):
        """Who a message claims to be, for an audit entry with no session."""
        return message.get(C.SENDER_COMP_ID)

    def refusal(self, message, reason, codec):
        """A best-effort Logout for a connection we will not serve.

        There is no session and therefore no sequence state, so sequence number
        1 is used. The client sees a reason rather than a bare TCP reset.
        """
        logout = Message.create(C.LOGOUT)
        logout.set(C.MSG_SEQ_NUM, 1)
        logout.set(C.SENDER_COMP_ID, message.get(C.TARGET_COMP_ID) or "UNKNOWN")
        logout.set(C.TARGET_COMP_ID, message.get(C.SENDER_COMP_ID) or "UNKNOWN")
        logout.set(C.SENDING_TIME, self.clock.timestamp())
        logout.set(C.TEXT, reason)
        return logout, codec.encode(logout)

    def resolve_inbound(self, message):
        """Find the session a received message belongs to.

        The client's SenderCompID is our TargetCompID and vice versa.
        """
        return self._sessions.get((message.get(C.TARGET_COMP_ID),
                                   message.get(C.SENDER_COMP_ID)))

    # -- enumeration and lifecycle -----------------------------------------

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
