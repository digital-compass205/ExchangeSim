"""FIX session layer: logon, sequencing, recovery and heartbeats.

Implements the parts of FIX 4.2 that a client's own recovery logic exercises,
because being able to test that logic is a large part of why the simulator
exists:

* sequence numbers persisted across restarts (see :mod:`exchangesim.fix.store`)
* MsgSeqNum too high -> ResendRequest, with later messages queued until the gap
  closes, so nothing is silently dropped
* MsgSeqNum too low -> Logout and disconnect, per the specification
* resend replay with ``PossDupFlag=Y`` and ``OrigSendingTime``, with
  administrative messages collapsed into ``SequenceReset-GapFill``
* ``ResetSeqNumFlag`` on Logon
* heartbeat and TestRequest policing in both directions

Application messages are handed to an :class:`Application`, which is where a
venue gateway plugs in. The session layer knows nothing about orders.
"""

import logging

from ..audit import DIRECTION_IN, DIRECTION_OUT
from . import constants as C
from .dictionary import Failure
from .render import extract, symbol_of
from .codec import FixCodec
from .message import (
    MalformedMessage,
    Message,
)

log = logging.getLogger(__name__)

#: Grace multiplier on HeartBtInt before a TestRequest is sent, per FIX
#: guidance that some transmission slack be allowed.
HEARTBEAT_GRACE = 1.2

#: EndSeqNo values meaning "everything from BeginSeqNo onwards".
_INFINITY = (0, 999999)


class SessionState(object):
    DISCONNECTED = "disconnected"
    AWAITING_LOGON = "awaiting_logon"
    ACTIVE = "active"
    AWAITING_LOGOUT = "awaiting_logout"


class Application(object):
    """Callbacks a gateway implements to receive session events."""

    def on_logon(self, session):
        """The session reached ACTIVE."""

    def on_logout(self, session, reason):
        """The session left ACTIVE, for any reason including disconnect."""

    def on_message(self, session, message):
        """An in-sequence application message arrived.

        Return a :class:`~exchangesim.fix.dictionary.Failure` to have the
        session emit a Reject, or None when the message was handled.
        """
        return None


class SessionConfig(object):
    """Static per-session settings, from the venue config file."""

    __slots__ = ("sender_comp_id", "target_comp_id", "heartbeat_interval",
                 "begin_string", "reset_on_logon", "cancel_on_disconnect",
                 "default_sub_id", "allowed_sub_ids", "validate_unknown_tags",
                 "next_expected_seq_num", "allow_logon_reset", "appl_ver_id",
                 "default_appl_ver_id", "requires_encrypt_method",
                 "requires_heart_bt_int")

    def __init__(self, sender_comp_id, target_comp_id, heartbeat_interval=30,
                 begin_string="FIX.4.2", reset_on_logon=False,
                 cancel_on_disconnect=False, default_sub_id=None,
                 allowed_sub_ids=None, validate_unknown_tags=True,
                 next_expected_seq_num=False, allow_logon_reset=True,
                 appl_ver_id=None, default_appl_ver_id=None,
                 requires_encrypt_method=True, requires_heart_bt_int=True):
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.heartbeat_interval = heartbeat_interval
        self.begin_string = begin_string
        #: Force a sequence reset on every Logon regardless of what the client asks.
        self.reset_on_logon = reset_on_logon
        self.cancel_on_disconnect = cancel_on_disconnect
        #: Market used when a message omits TargetSubID (57).
        self.default_sub_id = default_sub_id
        self.allowed_sub_ids = frozenset(allowed_sub_ids or ())
        self.validate_unknown_tags = validate_unknown_tags

        # -- FIXT.1.1 additions; every one is off by default so that a FIX 4.2
        # dialect behaves exactly as it did before these existed.

        #: Require and honour NextExpectedMsgSeqNum(789) on Logon (FIX 4.4+).
        self.next_expected_seq_num = next_expected_seq_num
        #: HKEX refuses a client-initiated ResetSeqNumFlag; Japannext accepts it.
        self.allow_logon_reset = allow_logon_reset
        #: ApplVerID(1128) stamped on messages this venue generates.
        self.appl_ver_id = appl_ver_id
        #: DefaultApplVerID(1137) echoed on the Logon response.
        self.default_appl_ver_id = default_appl_ver_id

        # -- fields a Logon negotiates, which not every encoding of FIX has.
        # The binary encoding of OCG-C carries neither: its encryption is fixed
        # by the protocol and its heartbeat interval is agreed out of band.

        #: Require EncryptMethod(98) on Logon and refuse anything but None.
        self.requires_encrypt_method = requires_encrypt_method
        #: Take the heartbeat interval from HeartBtInt(108) on Logon.
        self.requires_heart_bt_int = requires_heart_bt_int

    @property
    def key(self):
        return (self.begin_string, self.sender_comp_id, self.target_comp_id)


class Session(object):
    """One FIX session. At most one transport may be attached at a time."""

    def __init__(self, config, store, clock, dictionary, application=None,
                 audit=None, codec=None):
        self.config = config
        self.store = store
        self.clock = clock
        #: How this session's bytes are framed, decoded and encoded. Every use
        #: of the wire format goes through it, which is what lets HKEX serve
        #: the same protocol in its tag=value and binary encodings at once.
        self.codec = codec or FixCodec(config.begin_string)
        self.dictionary = dictionary
        self.application = application or Application()
        #: Message recorder, or None when the venue has the audit switched off.
        self.audit = audit

        self.state = SessionState.DISCONNECTED
        self.transport = None
        self.heartbeat_interval = config.heartbeat_interval

        self._framer = self.codec.framer()
        self._pending = {}            # seq -> Message, awaiting gap closure
        self._resend_requested = False
        self._last_sent = 0.0
        self._last_received = 0.0
        self._test_request_sent = None
        self._test_request_id = 0
        self._logout_reason = None
        #: Set while replaying, so replayed messages skip normal send handling.
        self._replaying = False

    # -- identity ----------------------------------------------------------

    @property
    def key(self):
        return self.config.key

    @property
    def sender_comp_id(self):
        return self.config.sender_comp_id

    @property
    def target_comp_id(self):
        return self.config.target_comp_id

    @property
    def connected(self):
        return self.transport is not None

    @property
    def logged_on(self):
        return self.state == SessionState.ACTIVE

    @property
    def wire(self):
        """Which encoding this session speaks: ``fix`` or ``binary``."""
        return self.codec.name

    def _next_test_request_id(self):
        """A short numeric TestReqID, which every encoding can carry.

        The binary encoding holds it in a UInt16, so it wraps rather than
        growing: the value only has to come back on the answering Heartbeat,
        and one that is already outstanding stops the session before it could
        be reused.
        """
        self._test_request_id = self._test_request_id % 65535 + 1
        return self._test_request_id

    def __repr__(self):
        return "Session(%s->%s, %s)" % (
            self.sender_comp_id, self.target_comp_id, self.state)

    def reset(self):
        """Discard the sequence numbers and the stored messages.

        A method rather than a reach into ``session.store`` from the control
        plane, because not every protocol here *has* a store: NNF has no
        sequence numbers and no resend, and its session raises instead. A no-op
        store would report success for something that did not happen.
        """
        self.store.reset()

    def describe(self):
        return {
            "sender_comp_id": self.sender_comp_id,
            "target_comp_id": self.target_comp_id,
            "state": self.state,
            "connected": self.connected,
            "next_out": self.store.next_out,
            "next_in": self.store.next_in,
            "heartbeat_interval": self.heartbeat_interval,
            "default_sub_id": self.config.default_sub_id,
            "cancel_on_disconnect": self.config.cancel_on_disconnect,
            "protocol": self.wire,
        }

    # -- audit -------------------------------------------------------------

    def _record(self, direction, message, raw, error=None):
        """Hand one message to the venue's recorder, if it has one.

        On the path every message takes, so it lifts a fixed handful of tags in
        one pass and stops. Naming fields and building a summary happen when
        somebody opens the entry, not here.
        """
        audit = self.audit
        if audit is None:
            return
        extracted = symbol = type_name = None
        if message is not None:
            extracted = extract(message)
            symbol = symbol_of(extracted)
            definition = self.dictionary.message(message.msg_type)
            if definition is not None:
                type_name = definition.name
        audit.record_message(
            direction, self.target_comp_id, message=message, raw=raw,
            type_name=type_name, extracted=extracted, symbol=symbol,
            error=error, protocol=self.codec.name)

    # -- transport ---------------------------------------------------------

    def attach(self, transport):
        """Bind a connection. The caller enforces one-connection-per-session."""
        self.transport = transport
        self.state = SessionState.AWAITING_LOGON
        self._framer.reset()
        self._pending.clear()
        self._resend_requested = False
        self._test_request_sent = None
        self._logout_reason = None
        now = self.clock.monotonic()
        self._last_sent = now
        self._last_received = now

    def detach(self, reason="disconnected"):
        """Drop the transport and notify the application.

        Sequence numbers are untouched: a reconnecting client resumes where it
        left off, which is exactly the path that exercises resend.
        """
        was_active = self.state == SessionState.ACTIVE
        self.transport = None
        self.state = SessionState.DISCONNECTED
        self._framer.reset()
        self._pending.clear()
        if was_active:
            log.info("%s logged out (%s)", self, reason)
        self.application.on_logout(self, reason)

    def disconnect(self, reason="disconnected"):
        transport = self.transport
        self.detach(reason)
        if transport is not None:
            transport.close()

    # -- inbound -----------------------------------------------------------

    def on_data(self, chunk):
        """Feed raw bytes from the transport."""
        try:
            raws = self._framer.feed(chunk)
        except MalformedMessage as exc:
            # BodyLength cannot be trusted, so the stream position is unknown
            # and the session cannot continue safely.
            log.warning("%s framing error: %s", self, exc)
            # Recorded here as well as in _on_raw_message: a framing failure
            # discards the bytes before any message is formed, so without this
            # the audit would show a session that simply stops.
            self._record(DIRECTION_IN, None, None, "framing error: %s" % exc)
            self._logout_and_disconnect("framing error: %s" % exc)
            return

        for raw in raws:
            if self.transport is None:
                return
            self._on_raw_message(raw)

    def _on_raw_message(self, raw):
        try:
            message = self.codec.decode(raw)
        except MalformedMessage as exc:
            log.warning("%s malformed message: %s", self, exc)
            self._record(DIRECTION_IN, None, raw,
                         "malformed message: %s" % exc)
            self._logout_and_disconnect("malformed message: %s" % exc)
            return

        self._last_received = self.clock.monotonic()
        self._test_request_sent = None

        # Before the CompID and dictionary checks below, so what a client sent
        # is recorded whether or not the venue was willing to accept it.
        self._record(DIRECTION_IN, message, raw)

        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s <-- %s", self, message.to_string())

        failure = self._check_comp_ids(message)
        if failure is not None:
            self._handle_comp_id_failure(message, failure)
            return

        failure = self.dictionary.validate(
            message, check_unknown=self.config.validate_unknown_tags)
        if failure is not None:
            self._handle_validation_failure(message, failure)
            return

        self._on_message(message)

    def _on_message(self, message):
        msg_type = message.msg_type

        if self.state == SessionState.AWAITING_LOGON and msg_type != C.LOGON:
            log.warning("%s first message was '%s', not a Logon", self, msg_type)
            self._logout_and_disconnect("first message must be a Logon")
            return

        if msg_type == C.LOGON:
            self._on_logon(message)
            return

        if not self._check_sequence(message):
            return

        self._dispatch(message)
        self._drain_pending()

    def _dispatch(self, message):
        """Route an in-sequence message and consume its sequence number."""
        msg_type = message.msg_type
        handler = _ADMIN_HANDLERS.get(msg_type)

        # SequenceReset sets the expected number itself, so it must not be
        # advanced here.
        if msg_type != C.SEQUENCE_RESET:
            self.store.set_next_in(message.seq_num + 1)

        if handler is not None:
            handler(self, message)
            return

        failure = self.application.on_message(self, message)
        if failure is not None:
            self.send_reject(message, failure)

    # -- sequencing --------------------------------------------------------

    def _check_sequence(self, message):
        """Return True when the message may be processed now."""
        expected = self.store.next_in
        received = message.seq_num

        if received is None:
            self.send_reject(message, Failure(
                C.SessionRejectReason.REQUIRED_TAG_MISSING, C.MSG_SEQ_NUM,
                "MsgSeqNum (34) is missing or not an integer"))
            return False

        if received == expected:
            return True

        if received > expected:
            # A SequenceReset in Reset mode is processed regardless of its own
            # sequence number -- that is its entire purpose.
            if (message.msg_type == C.SEQUENCE_RESET
                    and message.get(C.GAP_FILL_FLAG, C.NO) != C.YES):
                return True
            self._queue_and_request_resend(message, expected, received)
            return False

        # received < expected
        if message.get(C.POSS_DUP_FLAG) == C.YES:
            log.debug("%s ignoring duplicate %s seq %d (expecting %d)",
                      self, message.msg_type, received, expected)
            return False

        # A Logout arriving low is still a logout; answering with another
        # Logout would loop.
        if message.msg_type == C.LOGOUT:
            self.disconnect("peer logged out with a low sequence number")
            return False

        self._logout_and_disconnect(
            "MsgSeqNum too low, expecting %d but received %d" % (expected, received))
        return False

    def _queue_and_request_resend(self, message, expected, received,
                                  request=True):
        self._pending[received] = message
        if self._resend_requested:
            return
        self._resend_requested = True
        if not request:
            log.info("%s gap detected: expecting %d, received %d; awaiting the "
                     "peer's own recovery", self, expected, received)
            return
        log.info("%s gap detected: expecting %d, received %d; requesting resend",
                 self, expected, received)
        resend = Message.create(C.RESEND_REQUEST)
        resend.set(C.BEGIN_SEQ_NO, expected)
        resend.set(C.END_SEQ_NO, 0)
        self.send(resend)

    def _drain_pending(self):
        """Process queued messages that the closed gap has made current."""
        while self._pending:
            expected = self.store.next_in
            message = self._pending.pop(expected, None)
            if message is None:
                break
            self._dispatch(message)
        if not self._pending:
            self._resend_requested = False

    # -- administrative handlers -------------------------------------------

    def _on_logon(self, message):
        if self.state == SessionState.ACTIVE:
            self._logout_and_disconnect("already logged on")
            return

        if self.config.requires_encrypt_method:
            encrypt = message.get(C.ENCRYPT_METHOD)
            if encrypt != C.EncryptMethod.NONE:
                self._logout_and_disconnect(
                    "EncryptMethod (98) %s is not supported" % encrypt)
                return

        if self.config.requires_heart_bt_int:
            interval = message.get_int(C.HEART_BT_INT)
            if interval is None or interval < 0:
                self._logout_and_disconnect("invalid HeartBtInt (108)")
                return
            self.heartbeat_interval = interval

        if (message.get(C.RESET_SEQ_NUM_FLAG) == C.YES
                and not self.config.allow_logon_reset):
            # HKEX OCG-C section 4.6.2.1: a client cannot reset the sequence
            # through Logon; it must ask the exchange operations desk.
            self._logout_and_disconnect(
                "ResetSeqNumFlag (141) is not supported on this session")
            return

        reset = (message.get(C.RESET_SEQ_NUM_FLAG) == C.YES
                 or self.config.reset_on_logon)
        if reset:
            log.info("%s resetting sequence numbers at logon", self)
            self.store.reset()

        # Read after any reset, so the bound it is checked against is the one
        # that will actually apply.
        next_expected = None
        if self.config.next_expected_seq_num:
            next_expected = message.get_int(C.NEXT_EXPECTED_MSG_SEQ_NUM)
            if next_expected is None:
                self._logout_and_disconnect(
                    "NextExpectedMsgSeqNum (789) is required on Logon")
                return
            if next_expected > self.store.next_out:
                # It claims to have seen a message we have not sent yet.
                self._logout_and_disconnect(
                    "NextExpectedMsgSeqNum (789) %d is above the next outbound "
                    "sequence number %d" % (next_expected, self.store.next_out))
                return

        received = message.seq_num
        expected = self.store.next_in

        if received is not None and received < expected:
            self._logout_and_disconnect(
                "MsgSeqNum too low, expecting %d but received %d"
                % (expected, received))
            return

        # Acknowledge first: the client must see our Logon before any
        # ResendRequest or replay that follows it.
        self.state = SessionState.ACTIVE
        response = Message.create(C.LOGON)
        response.set(C.ENCRYPT_METHOD, C.EncryptMethod.NONE)
        response.set(C.HEART_BT_INT, self.heartbeat_interval)
        if reset:
            response.set(C.RESET_SEQ_NUM_FLAG, C.YES)
        response.set_if(C.DEFAULT_APPL_VER_ID, self.config.default_appl_ver_id)
        if self.config.next_expected_seq_num:
            # What we next expect *from the client*: one past this Logon when it
            # arrived in sequence, otherwise still the number that closes the gap.
            response.set(C.NEXT_EXPECTED_MSG_SEQ_NUM,
                         received + 1 if received == expected else expected)
            response.set(C.SESSION_STATUS, C.SessionStatus.ACTIVE)
        self.send(response)
        logon_seq = response.seq_num

        log.info("%s logged on (heartbeat %ds%s)", self, self.heartbeat_interval,
                 ", sequence reset" if reset else "")

        if received is not None and received > expected:
            # The Logon itself sits above the gap. It is queued so that its
            # sequence number is consumed in order once the gap closes --
            # _on_queued_logon then treats it as a no-op rather than logging on
            # a second time.
            #
            # Under NextExpectedMsgSeqNum the response we just sent already told
            # the client what we are missing, and the specification is explicit
            # that neither side may also raise a ResendRequest from the Logon's
            # own MsgSeqNum. So the gap is recorded without asking for it.
            self._queue_and_request_resend(
                message, expected, received,
                request=not self.config.next_expected_seq_num)
        else:
            self.store.set_next_in((received or 0) + 1)

        if next_expected is not None and next_expected < logon_seq:
            # The client is behind us. Replay from where it says it is up to and
            # including the Logon, which -- being administrative -- collapses
            # into the trailing gap fill the specification asks for.
            self.replay(next_expected, logon_seq)

        self.application.on_logon(self)

    def _on_queued_logon(self, message):
        """A Logon reached _dispatch via the pending queue; only consume its seq."""
        log.debug("%s consuming queued Logon at seq %s", self, message.seq_num)

    def _on_heartbeat(self, message):
        pass  # arrival already refreshed the inbound timer

    def _on_test_request(self, message):
        response = Message.create(C.HEARTBEAT)
        response.set_if(C.TEST_REQ_ID, message.get(C.TEST_REQ_ID))
        self.send(response)

    def _on_resend_request(self, message):
        begin = message.get_int(C.BEGIN_SEQ_NO, 1)
        end = message.get_int(C.END_SEQ_NO, 0)
        self.replay(begin, end)

    def _on_sequence_reset(self, message):
        new_seq_no = message.get_int(C.NEW_SEQ_NO)
        if new_seq_no is None:
            self.send_reject(message, Failure(
                C.SessionRejectReason.REQUIRED_TAG_MISSING, C.NEW_SEQ_NO,
                "NewSeqNo (36) is required"))
            return

        gap_fill = message.get(C.GAP_FILL_FLAG, C.NO) == C.YES
        expected = self.store.next_in

        if new_seq_no < expected:
            # Lower than expected is an error in both modes.
            self.send_reject(message, Failure(
                C.SessionRejectReason.VALUE_INCORRECT, C.NEW_SEQ_NO,
                "NewSeqNo (36) %d is below the expected sequence number %d"
                % (new_seq_no, expected)))
            return

        log.info("%s sequence reset%s: next inbound %d -> %d",
                 self, " (gap fill)" if gap_fill else "", expected, new_seq_no)
        self.store.set_next_in(new_seq_no)
        self._drain_pending()

    def _on_reject(self, message):
        log.warning("%s received Reject for seq %s: %s (reason %s, tag %s)",
                    self, message.get(C.REF_SEQ_NUM), message.get(C.TEXT),
                    message.get(C.SESSION_REJECT_REASON), message.get(C.REF_TAG_ID))

    def _on_logout(self, message):
        text = message.get(C.TEXT) or "peer logged out"
        if self.state == SessionState.AWAITING_LOGOUT:
            self.disconnect(text)
            return
        response = Message.create(C.LOGOUT)
        response.set(C.TEXT, "logout acknowledged")
        self.send(response)
        self.disconnect(text)

    # -- resend ------------------------------------------------------------

    def replay(self, begin, end):
        """Answer a ResendRequest for ``begin`` through ``end``.

        Administrative messages are never replayed -- resending an old Logon or
        Heartbeat is meaningless -- so runs of them collapse into a single
        ``SequenceReset-GapFill``, as do sequence numbers with nothing stored.
        """
        upper = self.store.next_out - 1 if end in _INFINITY else end
        if upper >= self.store.next_out:
            upper = self.store.next_out - 1

        log.info("%s replaying %d..%d", self, begin, upper)

        stored = dict(self.store.get_outbound(begin, upper))
        gap_start = None

        for seq in range(begin, upper + 1):
            raw = stored.get(seq)
            if raw is None or self.codec.is_admin(raw):
                if gap_start is None:
                    gap_start = seq
                continue
            if gap_start is not None:
                self._send_gap_fill(gap_start, seq)
                gap_start = None
            self._resend(seq, raw)

        if gap_start is not None:
            self._send_gap_fill(gap_start, self.store.next_out)

    def _resend(self, seq, raw):
        """Retransmit one stored application message as a possible duplicate."""
        try:
            message = self.codec.decode(raw, validate_checksum=False)
        except MalformedMessage:
            log.error("%s cannot decode stored message %d; gap-filling instead",
                      self, seq)
            self._send_gap_fill(seq, seq + 1)
            return

        original_sending_time = message.get(C.SENDING_TIME)
        message.set(C.POSS_DUP_FLAG, C.YES)
        if original_sending_time:
            message.set(C.ORIG_SENDING_TIME, original_sending_time)
        message.set(C.SENDING_TIME, self.clock.timestamp())
        self._transmit(message, store=False)

    def _send_gap_fill(self, seq, new_seq_no):
        message = Message.create(C.SEQUENCE_RESET)
        message.set(C.MSG_SEQ_NUM, seq)
        message.set(C.GAP_FILL_FLAG, C.YES)
        message.set(C.NEW_SEQ_NO, new_seq_no)
        message.set(C.POSS_DUP_FLAG, C.YES)
        message.set(C.ORIG_SENDING_TIME, self.clock.timestamp())
        self._transmit(message, store=False)

    # -- outbound ----------------------------------------------------------

    def send(self, message):
        """Assign the next sequence number, persist, and transmit.

        Messages produced while no transport is attached are still numbered and
        stored. That is what makes Cancel on Disconnect work: the client sees
        the gap at its next Logon and retrieves the reports by resend.
        """
        message.set(C.MSG_SEQ_NUM, self.store.next_out)
        self._transmit(message, store=True)

    def _transmit(self, message, store):
        message.set(C.SENDER_COMP_ID, self.sender_comp_id)
        message.set(C.TARGET_COMP_ID, self.target_comp_id)
        if not message.has(C.SENDING_TIME):
            message.set(C.SENDING_TIME, self.clock.timestamp())
        if self.config.appl_ver_id and not message.has(C.APPL_VER_ID):
            # FIXT.1.1 carries the application version per message, and the
            # dialect requires it on everything the venue generates.
            message.set(C.APPL_VER_ID, self.config.appl_ver_id)

        try:
            raw = self.codec.encode(message)
        except (ValueError, KeyError) as exc:
            # A message this session's encoding cannot express is a fault in
            # the venue, not in the client -- but it must not take the reactor
            # down with it, and the audit is where somebody will look for it.
            log.error("%s cannot encode %s: %s", self, message.msg_type, exc)
            self._record(DIRECTION_OUT, message, None,
                         "not encodable as %s: %s" % (self.codec.name, exc))
            return None

        # A resent message is recorded again, deliberately: it was sent again.
        # PossDupFlag=Y is in the summary, so two entries carrying one MsgSeqNum
        # read as the resend they are.
        self._record(DIRECTION_OUT, message, raw)

        if store:
            seq = message.seq_num
            self.store.store_outbound(seq, raw)
            self.store.set_next_out(seq + 1)

        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s --> %s", self, message.to_string())

        if self.transport is not None:
            self.transport.send(raw)
            self._last_sent = self.clock.monotonic()
        return raw

    def send_reject(self, message, failure):
        """Emit a session-level Reject (35=3) describing a validation failure."""
        reject = Message.create(C.REJECT)
        reject.set(C.REF_SEQ_NUM, message.seq_num if message.seq_num else 0)
        reject.set_if(C.REF_MSG_TYPE, message.msg_type)
        if failure.tag is not None:
            reject.set(C.REF_TAG_ID, failure.tag)
        reject.set(C.SESSION_REJECT_REASON, failure.reason)
        if failure.text:
            reject.set(C.TEXT, failure.text[:255])
        self.send(reject)
        log.info("%s rejected %s seq %s: %s",
                 self, message.msg_type, message.seq_num, failure.text)

    def send_logout(self, text=None):
        message = Message.create(C.LOGOUT)
        message.set_if(C.TEXT, text)
        self.send(message)
        self.state = SessionState.AWAITING_LOGOUT

    def _logout_and_disconnect(self, text):
        self.send_logout(text)
        self.disconnect(text)

    def _handle_comp_id_failure(self, message, failure):
        # A CompID mismatch means the message is not for this session at all,
        # so it is answered and the connection dropped rather than continuing.
        log.warning("%s CompID problem: %s", self, failure.text)
        if self.state == SessionState.ACTIVE:
            self.send_reject(message, failure)
        self._logout_and_disconnect(failure.text)

    def _handle_validation_failure(self, message, failure):
        if self.state == SessionState.AWAITING_LOGON:
            self._logout_and_disconnect(failure.text)
            return
        # The sequence number is consumed even by a rejected message, otherwise
        # the client and simulator would disagree about the next expected value.
        if message.seq_num == self.store.next_in:
            self.store.set_next_in(message.seq_num + 1)
        self.send_reject(message, failure)

    def _check_comp_ids(self, message):
        sender = message.get(C.SENDER_COMP_ID)
        target = message.get(C.TARGET_COMP_ID)
        if sender is None or target is None:
            return Failure(C.SessionRejectReason.REQUIRED_TAG_MISSING,
                           C.SENDER_COMP_ID if sender is None else C.TARGET_COMP_ID,
                           "SenderCompID (49) and TargetCompID (56) are required")
        if sender != self.target_comp_id or target != self.sender_comp_id:
            return Failure(C.SessionRejectReason.COMPID_PROBLEM, None,
                           "unexpected CompIDs: sender '%s', target '%s'"
                           % (sender, target))
        return None

    # -- timers ------------------------------------------------------------

    def tick(self):
        """Heartbeat and TestRequest policing. Called about once a second."""
        if self.state != SessionState.ACTIVE or self.transport is None:
            return
        if not self.heartbeat_interval:
            return

        now = self.clock.monotonic()

        if now - self._last_sent >= self.heartbeat_interval:
            self.send(Message.create(C.HEARTBEAT))

        silence = now - self._last_received
        if self._test_request_sent is not None:
            if now - self._test_request_sent >= self.heartbeat_interval:
                self._logout_and_disconnect("no response to TestRequest (1)")
            return

        if silence >= self.heartbeat_interval * HEARTBEAT_GRACE:
            request = Message.create(C.TEST_REQUEST)
            request.set(C.TEST_REQ_ID, str(self._next_test_request_id()))
            self.send(request)
            self._test_request_sent = now


_ADMIN_HANDLERS = {
    # Logon is normally intercepted before sequence checking; it only reaches
    # _dispatch when replayed out of the pending queue.
    C.LOGON: Session._on_queued_logon,
    C.HEARTBEAT: Session._on_heartbeat,
    C.TEST_REQUEST: Session._on_test_request,
    C.RESEND_REQUEST: Session._on_resend_request,
    C.SEQUENCE_RESET: Session._on_sequence_reset,
    C.REJECT: Session._on_reject,
    C.LOGOUT: Session._on_logout,
}
