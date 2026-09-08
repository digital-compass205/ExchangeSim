"""The NNF session layer: a box, the users on it, and the heartbeat.

This is the third session implementation here, and it is a separate one rather
than a configured :class:`exchangesim.fix.session.Session` because almost
nothing that class does applies. NNF has **no sequence numbers**, so no gap
detection, no resend request, no gap fill and no store; **no session-level
Reject**, because a refusal is the ``*_ERROR`` or ``*_REJECT`` form of the
transaction that caused it and so belongs to the gateway; and no CompID pair,
because identity is a numeric User ID. Switching all of that off would leave a
shell.

What it has instead, and nothing else here has, is **two tiers**:

    A box is one TCP connection. Many users sign on over it, and disconnecting
    the box signs every one of them off.

So a connection is a :class:`BoxConnection` and a *session*, in the sense the
control plane means, is an :class:`NnfSession` -- one signed-on user. The
acceptor resolves the first; ``sessions``, ``session.kill`` and an order's
owner mean the second.

**Reports produced while a user is disconnected are dropped, not queued.** That
inverts the invariant ``CLAUDE.md`` states for FIX, where such messages are
still numbered and persisted so a resend can deliver them -- and it must,
because NNF has no resend. The client learns what it missed by asking for an
order and trade download at its next sign-on. Do not "fix" this by adding a
store; the download *is* the store.
"""

import logging

from ..audit import DIRECTION_IN, DIRECTION_OUT
from ..fix import constants as C
from ..fix.message import MalformedMessage, Message
from ..fix.render import extract, symbol_of
from .codec import NnfCodec
from .crypto import CryptoError, PlainCipher

log = logging.getLogger(__name__)

#: "Heartbeat is to be sent only if there is inactivity for 30 seconds."
HEARTBEAT_SECONDS = 30

#: "Trading Host will consider the member system as inactive after missing two
#: heartbeats in succession and disconnect the socket connection."
MISSED_HEARTBEATS_ALLOWED = 2

#: Extra heartbeats inside one interval are ignored and counted; at this many
#: the box is disconnected. The document leaves the threshold to the exchange,
#: so this is a default a venue may override -- and it is an ASSUMPTION.
DROP_COUNTER_LIMIT = 10


class BoxState(object):
    DISCONNECTED = "disconnected"
    AWAITING_REGISTRATION = "awaiting_registration"
    AWAITING_BOX_SIGN_ON = "awaiting_box_sign_on"
    ACTIVE = "active"


class Application(object):
    """Callbacks a gateway implements to receive session events.

    The same shape as the FIX one, with a fourth hook. In FIX the session layer
    answers a dialect failure itself with a Reject; NNF has no such message, so
    the answer is the erroring form of the offending transaction and only the
    venue knows which that is.
    """

    def on_logon(self, session):
        """A user reached ACTIVE."""

    def on_logout(self, session, reason):
        """A user left ACTIVE, for any reason including a box disconnect."""

    def on_message(self, session, message):
        """An application message arrived from a signed-on user."""
        return None

    def on_box_message(self, box, message):
        """A message arrived on a box with no user attached to it yet.

        Registration, box sign-on and user sign-on all come through here: the
        session layer knows the sequence but not the structures, so it asks the
        gateway to build every answer. Return True when handled.
        """
        return False

    def on_box_detached(self, box):
        """A box connection is gone, after its users have been signed off.

        Whatever a venue attached to the *box* rather than to a user belongs
        here. At NSE that is the material the Gateway Router issued: the
        document is explicit that "in the event of a box disconnection, the
        IVs are reset at exchange end, and a new static and dynamic IV is
        provided in GR response message to a fresh GR query", so holding them
        past the connection would let a member reconnect on a key the exchange
        has already discarded.
        """

    def on_invalid(self, session, message, failure):
        """The dialect refused a message. Return True when answered."""
        return False


class NnfSessionConfig(object):
    """One user who may sign on, from the venue config file."""

    __slots__ = ("user_id", "box_id", "broker_id", "branch_id", "password",
                 "user_type", "markets", "cancel_on_disconnect",
                 "trader_name", "broker_name")

    def __init__(self, user_id, box_id, broker_id, branch_id=1, password=None,
                 user_type=None, markets=None, cancel_on_disconnect=False,
                 trader_name=None, broker_name=None):
        self.user_id = int(user_id)
        self.box_id = int(box_id)
        self.broker_id = broker_id
        self.branch_id = int(branch_id)
        self.password = password
        self.user_type = user_type
        self.markets = tuple(markets or ())
        self.cancel_on_disconnect = cancel_on_disconnect
        #: Echoed back on the sign-on response; the client displays them.
        self.trader_name = trader_name
        self.broker_name = broker_name

    @property
    def key(self):
        """What an order is tagged with.

        A tuple, so it can never collide with the ``control:<OWNER>`` string an
        injected order carries, and never with a FIX session's three-part key.
        """
        return ("nnf", self.user_id)

    @property
    def target_comp_id(self):
        """What the control plane calls a session. Here that is the User ID."""
        return str(self.user_id)


class NnfSession(object):
    """One signed-on user, on a box.

    Deliberately thin: it owns no socket, no cipher and no framing. All of that
    belongs to the box, and several of these share one.
    """

    __slots__ = ("config", "box", "logged_on", "signed_on_at")

    def __init__(self, config, box):
        self.config = config
        self.box = box
        self.logged_on = False
        self.signed_on_at = None

    # -- identity ----------------------------------------------------------

    @property
    def key(self):
        return self.config.key

    @property
    def user_id(self):
        return self.config.user_id

    @property
    def target_comp_id(self):
        return self.config.target_comp_id

    @property
    def broker_id(self):
        return self.config.broker_id

    @property
    def wire(self):
        return "nnf"

    @property
    def connected(self):
        return self.box is not None and self.box.connected

    @property
    def cancel_on_disconnect(self):
        return self.config.cancel_on_disconnect

    # -- traffic -----------------------------------------------------------

    def send(self, message):
        """Send one message to this user, naming them in the header.

        With the box gone the message is recorded as undelivered and dropped --
        see the module docstring. There is nothing to queue it for, and the
        client will ask for a download when it comes back.
        """
        if self.box is None:
            return False
        self.name_recipient(message)
        if not self.box.connected:
            self.box.record(DIRECTION_OUT, message, None,
                            error="not delivered: the box is disconnected",
                            session=self.target_comp_id)
            return False
        return self.box.send(message, session=self)

    def name_recipient(self, message):
        """Put this user's id in the header field the protocol carries it in.

        "This field should contain the user ID" is said of the header once and
        applies to every message with a header, in both directions -- so a
        response that names its user only inside the structure's body leaves
        the header at zero, and a client reading the header to find the trader
        a response belongs to is handed user 0. A real gateway crashed on
        exactly that, looking up trader 0 in a container that had no such
        entry.

        It is stamped here rather than in each venue's handlers because this is
        the one place that knows both the tag and the user: ``user_id_tag`` is
        already what ``BoxConnection`` reads to route an *inbound* message to a
        session, so answering in the same field is the same fact read
        backwards. Doing it per handler is what let a venue ship four message
        types that forgot. A handler that has set the field itself keeps its
        value -- an error naming a user who never signed on is built before
        there is a session to send it through.
        """
        tag = self.box.manager.user_id_tag if self.box is not None else None
        if tag is not None and message.get(tag) is None:
            message.set(tag, str(self.user_id))

    def disconnect(self, reason="disconnected"):
        """Sign this user off. The box, and its other users, stay up."""
        if self.box is not None:
            self.box.sign_off(self, reason)

    def reset(self):
        raise NotImplementedError(
            "session '%s' has no sequence numbers to reset: NNF recovers by "
            "order and trade download (7000), not by resend. Use session.kill "
            "to drop the connection." % self.target_comp_id)

    def describe(self):
        return {
            "protocol": "nnf",
            "target_comp_id": self.target_comp_id,
            "user_id": self.user_id,
            "box_id": self.config.box_id,
            "broker_id": self.broker_id,
            "branch_id": self.config.branch_id,
            "state": "active" if self.logged_on else "signed_off",
            "connected": self.connected,
            "cancel_on_disconnect": self.cancel_on_disconnect,
            "markets": list(self.config.markets),
            "signed_on_at": self.signed_on_at,
        }

    def __repr__(self):
        return "NnfSession(%d, %s)" % (self.user_id,
                                       "on" if self.logged_on else "off")


class BoxConnection(object):
    """One TCP connection: the cipher, the framing, and the users on it.

    Duck-types enough of a session for the shared acceptor -- ``wire``,
    ``connected``, ``target_comp_id``, ``attach``, ``detach``, ``on_data`` --
    because to the acceptor a connection is a connection. Everything the
    control plane calls a session is an :class:`NnfSession` hanging off this.
    """

    def __init__(self, box_id, manager, application=None):
        self.box_id = box_id
        self.manager = manager
        self.application = application
        self.codec = NnfCodec(manager.layouts)
        self.dictionary = manager.dictionary
        self.state = BoxState.DISCONNECTED
        self.transport = None
        self.users = {}                 # user_id -> NnfSession
        self.broker_id = None
        self.drop_counter = 0
        self._framer = self.codec.framer()
        self._last_received = None
        self._last_sent = None
        self._last_heartbeat = None
        self._peer = None

    # -- identity ----------------------------------------------------------

    @property
    def wire(self):
        return "nnf"

    @property
    def target_comp_id(self):
        """What an acceptor names in a log line. A box is known by its number."""
        return "box %d" % self.box_id

    @property
    def connected(self):
        return self.transport is not None

    @property
    def encrypted(self):
        return self.codec.cipher.name != "plain"

    # -- the connection ----------------------------------------------------

    def attach(self, transport):
        self.transport = transport
        peer = getattr(transport, "peer", None)
        self._peer = "%s:%d" % peer[:2] if peer else None
        self.state = BoxState.AWAITING_REGISTRATION
        self.drop_counter = 0
        self._framer.reset()
        self._last_received = self._last_sent = self.manager.clock.monotonic()
        log.info("box %d connected from %s", self.box_id, self._peer)

    def detach(self, reason="disconnected"):
        """The connection is gone. Every user on it is signed off."""
        if self.transport is None:
            return
        self.transport = None
        self.state = BoxState.DISCONNECTED
        self.codec.cipher = PlainCipher()
        for session in list(self.users.values()):
            self._sign_off(session, reason)
        self.users = {}
        log.info("box %d disconnected: %s", self.box_id, reason)
        self.application.on_box_detached(self)

    def disconnect(self, reason="disconnected"):
        transport = self.transport
        self.detach(reason)
        if transport is None:
            return
        closer = getattr(transport, "close_when_flushed", transport.close)
        closer()

    # -- inbound -----------------------------------------------------------

    def on_data(self, chunk):
        try:
            packets = self._framer.feed(chunk)
        except MalformedMessage as exc:
            # There is no delimiter to resynchronise on, so a frame that will
            # not parse has cost us our position in the stream.
            self.record(DIRECTION_IN, None, chunk, error=str(exc))
            self.disconnect("unframeable packet: %s" % exc)
            return
        self._last_received = self.manager.clock.monotonic()
        for raw in packets:
            self._on_packet(raw)

    def _on_packet(self, raw):
        try:
            message = self.codec.decode(raw)
        except CryptoError as exc:
            # "If the checksum (MD5 / authentication tag) does not match, a box
            # sign-off message with error code (19031) will be sent to the
            # member before disconnection."
            self.record(DIRECTION_IN, None, raw, error=str(exc))
            self.manager.on_checksum_failure(self, exc)
            self.disconnect("checksum failure: %s" % exc)
            return
        except MalformedMessage as exc:
            self.record(DIRECTION_IN, None, raw, error=str(exc))
            self.disconnect("malformed packet: %s" % exc)
            return

        plain = self._plain_packet(message)
        self.record(DIRECTION_IN, message, plain)

        failure = self.dictionary.validate(message, check_unknown=False)
        session = self.users.get(self._user_of(message))

        if failure is not None:
            if not self._invalid(session, message, failure):
                self.disconnect("refused: %s" % failure.text)
            return

        if message.msg_type == str(self.manager.heartbeat_code):
            self._on_heartbeat()
            return

        if session is not None and session.logged_on:
            self.application.on_message(session, message)
            return

        if not self.application.on_box_message(self, message):
            log.warning("box %d: nothing handled transaction code %s",
                        self.box_id, message.msg_type)

    def _invalid(self, session, message, failure):
        if self.application is None:
            return False
        return bool(self.application.on_invalid(session, message, failure))

    def _user_of(self, message):
        value = message.get(self.manager.user_id_tag)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # -- outbound ----------------------------------------------------------

    def send(self, message, session=None):
        if self.transport is None:
            return False
        try:
            raw = self.codec.encode(message)
        except ValueError as exc:
            log.error("box %d cannot encode %s: %s", self.box_id,
                      message.msg_type, exc)
            return False
        self.transport.send(raw)
        self._last_sent = self.manager.clock.monotonic()
        self.record(DIRECTION_OUT, message, self._plain_packet(message),
                    session=session.target_comp_id if session else None)
        return True

    def _plain_packet(self, message):
        """The packet as it would look unencrypted, for the audit.

        The audit renders bytes long after they travelled, and under the
        existing methodology the keystream cannot be rewound, so what is
        recorded is the plaintext -- see ``NnfCodec.raw_string``.
        """
        try:
            layout = self.manager.layouts.layout_for(message)
            if layout is None:
                return None
            from . import packet as framing
            return framing.pack(layout.encode(message),
                                sequence=message.seq_num or 0)
        except (ValueError, MalformedMessage):
            return None

    # -- the sequence a box goes through -----------------------------------

    def registered(self, cipher=None):
        """Registration accepted: everything after this message is encrypted."""
        self.state = BoxState.AWAITING_BOX_SIGN_ON
        if cipher is not None:
            self.codec.cipher = cipher

    def signed_on(self, broker_id):
        """Box sign-on accepted. Users linked to this box may now sign on."""
        self.broker_id = broker_id
        self.state = BoxState.ACTIVE

    def sign_on_user(self, config):
        """Bring a user up on this box."""
        session = NnfSession(config, self)
        session.logged_on = True
        session.signed_on_at = self.manager.clock.timestamp()
        self.users[config.user_id] = session
        self.manager.attach(session)
        if self.application is not None:
            self.application.on_logon(session)
        log.info("user %d signed on to box %d", config.user_id, self.box_id)
        return session

    def sign_off(self, session, reason="signed off"):
        self._sign_off(session, reason)
        self.users.pop(session.user_id, None)

    def _sign_off(self, session, reason):
        if not session.logged_on:
            return
        session.logged_on = False
        self.manager.detach(session)
        if self.application is not None:
            self.application.on_logout(session, reason)
        log.info("user %d signed off box %d: %s", session.user_id,
                 self.box_id, reason)

    def user(self, user_id):
        return self.users.get(user_id)

    # -- heartbeats --------------------------------------------------------

    def _on_heartbeat(self):
        """Echo it back, unless it came too soon after the last one.

        "If a member sends more than one heartbeat message within the same
        interval, the exchange will disregard the extra messages and increase
        the drop counter by 1 for every additional heartbeat message received."
        """
        now = self.manager.clock.monotonic()
        interval = self.manager.heartbeat_seconds
        if self._last_heartbeat is not None and now - self._last_heartbeat < interval:
            self.drop_counter += 1
            if self.drop_counter >= self.manager.drop_counter_limit:
                self.disconnect("heartbeat drop counter exceeded")
            return
        self._last_heartbeat = now
        self.send(Message.create(str(self.manager.heartbeat_code)))

    def tick(self):
        """Disconnect a box that has gone quiet for two heartbeat intervals."""
        if self.transport is None or self._last_received is None:
            return
        silent = self.manager.clock.monotonic() - self._last_received
        if silent >= self.manager.heartbeat_seconds * MISSED_HEARTBEATS_ALLOWED:
            self.disconnect("no heartbeat for %.0f seconds" % silent)

    # -- audit -------------------------------------------------------------

    def record(self, direction, message, raw, error=None, session=None):
        audit = self.manager.audit
        if audit is None:
            return
        name = session or self.target_comp_id
        if message is None:
            audit.record_message(direction, name, raw=raw, error=error,
                                 protocol="nnf")
            return
        fields = extract(message)
        definition = self.dictionary.message(message.msg_type)
        audit.record_message(
            direction, name, message=message, raw=raw,
            type_name=definition.name if definition is not None else None,
            extracted=fields, symbol=symbol_of(fields),
            error=error, protocol="nnf")

    def describe(self):
        return {
            "box_id": self.box_id,
            "state": self.state,
            "connected": self.connected,
            "peer": self._peer,
            "broker_id": self.broker_id,
            "encryption": self.codec.cipher.name,
            "users": sorted(self.users),
            "drop_counter": self.drop_counter,
        }

    def __repr__(self):
        return "BoxConnection(%d, %s, %d users)" % (self.box_id, self.state,
                                                    len(self.users))


class NnfSessionManager(object):
    """Every box a venue will accept, and every user signed on across them.

    Two populations, deliberately. ``resolve_inbound`` answers with a *box*,
    because that is what a connection is; ``sessions`` answers with *users*,
    because that is what the control plane, an order's owner and a report's
    recipient mean.
    """

    unknown_session_reason = "unknown Box ID"

    def __init__(self, clock, dictionary, layouts, audit=None,
                 heartbeat_seconds=HEARTBEAT_SECONDS,
                 drop_counter_limit=DROP_COUNTER_LIMIT,
                 user_id_tag=None, heartbeat_code=None, box_id_tag=None):
        self.clock = clock
        self.dictionary = dictionary
        self.layouts = layouts
        self.audit = audit
        self.heartbeat_seconds = heartbeat_seconds
        self.drop_counter_limit = drop_counter_limit
        #: Which tag carries the User ID, the Box ID and which transaction code
        #: is the heartbeat. All three are the dialect's to name, so they are
        #: given rather than assumed -- that is what keeps this module free of
        #: any one market segment.
        self.user_id_tag = user_id_tag
        self.box_id_tag = box_id_tag
        self.heartbeat_code = heartbeat_code
        self._boxes = {}                # box_id -> BoxConnection
        self._users = {}                # user_id -> NnfSessionConfig
        self._live = {}                 # user_id -> NnfSession

    # -- configuration -----------------------------------------------------

    def add_box(self, box_id, application=None):
        box = BoxConnection(int(box_id), self, application)
        self._boxes[int(box_id)] = box
        log.info("box configured: %d", box_id)
        return box

    def add_user(self, config):
        if config.box_id not in self._boxes:
            raise ValueError("user %d names box %d, which is not configured"
                             % (config.user_id, config.box_id))
        self._users[config.user_id] = config
        log.info("user configured: %d on box %d (broker %s)",
                 config.user_id, config.box_id, config.broker_id)
        return config

    def user_config(self, user_id):
        return self._users.get(user_id)

    def users_of(self, box_id):
        return [config for config in self._users.values()
                if config.box_id == box_id]

    # -- the hooks a shared acceptor asks of a manager ---------------------

    @property
    def codec(self):
        return NnfCodec(self.layouts)

    def claimant(self, message):
        box_id = message.get(self.box_id_tag) if self.box_id_tag else None
        return "box %s" % box_id if box_id else None

    def refusal(self, message, reason, codec):
        """NNF has no Logout, and no message that fits every refusal.

        The connection is simply closed. Everything worth saying has already
        been said in the audit entry the acceptor writes, and inventing a
        response for a box we do not recognise would mean guessing which
        structure it could read.
        """
        log.warning("refusing an NNF connection: %s", reason)
        return None, b""

    def resolve_inbound(self, message):
        """The box a first message belongs to, by its Box ID."""
        if self.box_id_tag is None:
            return None
        try:
            box_id = int(message.get(self.box_id_tag))
        except (TypeError, ValueError):
            return None
        return self._boxes.get(box_id)

    def on_checksum_failure(self, box, error):
        """A hook the venue overrides to send the box sign-off of error 19031."""

    # -- the live session table -------------------------------------------

    def attach(self, session):
        self._live[session.user_id] = session

    def detach(self, session):
        self._live.pop(session.user_id, None)

    def session_for(self, key):
        """The signed-on user an order's session key names, or None."""
        if not key or len(key) != 2:
            return None
        return self._live.get(key[1])

    @property
    def boxes(self):
        return [self._boxes[box_id] for box_id in sorted(self._boxes)]

    @property
    def sessions(self):
        """Every configured user, signed on or not.

        Configured rather than live, so ``sessions`` lists a user who has not
        connected yet and ``session.kill`` can name one -- the same as the FIX
        manager, whose sessions exist before anybody dials in.
        """
        return [self._live.get(user_id) or NnfSession(config, self._boxes.get(config.box_id))
                for user_id, config in sorted(self._users.items())]

    def describe(self):
        return [session.describe() for session in self.sessions]

    def describe_boxes(self):
        """The connections, as against the users on them."""
        return [box.describe() for box in self.boxes]

    def tick(self):
        for box in self._boxes.values():
            box.tick()

    def close(self):
        for box in self._boxes.values():
            if box.connected:
                box.disconnect("simulator shutting down")
