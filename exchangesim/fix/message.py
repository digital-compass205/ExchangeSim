"""FIX message representation, framing, encoding and decoding.

A FIX message is an ordered sequence of ``tag=value`` pairs delimited by SOH
(0x01). Order matters: ``BeginString(8)``, ``BodyLength(9)`` and ``MsgType(35)``
must be the first three fields and ``CheckSum(10)`` the last, so this module
keeps fields in a list rather than a dict.

Bytes are handled as latin-1 throughout. FIX 4.2 is an ASCII protocol, and
latin-1 round-trips every byte value without raising -- so a malformed message
from a client produces a clean reject rather than a decoding traceback.
"""

SOH = b"\x01"
SOH_CHAR = "\x01"

# Structural tags, handled specially by the encoder.
TAG_BEGIN_STRING = 8
TAG_BODY_LENGTH = 9
TAG_MSG_TYPE = 35
TAG_MSG_SEQ_NUM = 34
TAG_CHECKSUM = 10

#: Tags that must appear in this order at the front of every message, after
#: 8/9/35. Any other field is emitted afterwards in the order it was set.
HEADER_ORDER = (
    TAG_MSG_TYPE,      # 35
    TAG_MSG_SEQ_NUM,   # 34
    49,                # SenderCompID
    50,                # SenderSubID
    52,                # SendingTime
    56,                # TargetCompID
    57,                # TargetSubID
    43,                # PossDupFlag
    97,                # PossResend
    122,               # OrigSendingTime
    1128,              # ApplVerID -- FIXT.1.1 only; never set by a FIX 4.2 venue
)

_HEADER_RANK = dict((tag, index) for index, tag in enumerate(HEADER_ORDER))


class FixError(Exception):
    """Base class for malformed-wire-data errors."""


class IncompleteMessage(FixError):
    """Not enough bytes buffered yet -- wait for more, do not reject."""


class MalformedMessage(FixError):
    """The bytes cannot form a valid FIX message and must be rejected."""


class Message(object):
    """An ordered collection of FIX fields.

    Values are stored as :class:`str`. Numeric conversion happens at the edges
    through :meth:`get_int` so that a client sending ``38=abc`` produces a
    reject rather than an exception deep in the engine.
    """

    __slots__ = ("fields",)

    def __init__(self, fields=None):
        # type: (list) -> None
        self.fields = list(fields) if fields else []

    # -- construction ------------------------------------------------------

    @classmethod
    def create(cls, msg_type, **_ignored):
        message = cls()
        message.set(TAG_MSG_TYPE, msg_type)
        return message

    def copy(self):
        return Message(self.fields)

    # -- access ------------------------------------------------------------

    @property
    def msg_type(self):
        return self.get(TAG_MSG_TYPE)

    @property
    def seq_num(self):
        return self.get_int(TAG_MSG_SEQ_NUM)

    def get(self, tag, default=None):
        for field_tag, value in self.fields:
            if field_tag == tag:
                return value
        return default

    def get_all(self, tag):
        """Every value for ``tag``; repeating groups are not otherwise modelled."""
        return [value for field_tag, value in self.fields if field_tag == tag]

    def get_int(self, tag, default=None):
        """Integer value, or ``default`` if absent or not an integer."""
        value = self.get(tag)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def has(self, tag):
        for field_tag, _value in self.fields:
            if field_tag == tag:
                return True
        return False

    def is_empty(self, tag):
        """True when the tag is present but carries no value (``55=<SOH>``).

        FIX treats this as an error distinct from omission -- it is
        ``SessionRejectReason=4``, tag specified without a value.
        """
        value = self.get(tag)
        return value is not None and value == ""

    def tags(self):
        return [tag for tag, _value in self.fields]

    # -- mutation ----------------------------------------------------------

    def set(self, tag, value):
        """Set a tag, replacing any existing occurrence in place."""
        if value is None:
            return self
        text = value if isinstance(value, str) else str(value)
        for index, (field_tag, _old) in enumerate(self.fields):
            if field_tag == tag:
                self.fields[index] = (tag, text)
                return self
        self.fields.append((tag, text))
        return self

    def set_if(self, tag, value):
        """Set a tag only when ``value`` is not None -- for optional fields."""
        if value is not None:
            self.set(tag, value)
        return self

    def append(self, tag, value):
        """Append without replacing, for genuinely repeated tags."""
        self.fields.append((tag, value if isinstance(value, str) else str(value)))
        return self

    def remove(self, tag):
        self.fields = [(t, v) for t, v in self.fields if t != tag]
        return self

    # -- rendering ---------------------------------------------------------

    def __repr__(self):
        return "Message(%s)" % self.to_string(" | ")

    def to_string(self, delimiter="|"):
        """Human-readable form, as specifications print it."""
        return delimiter.join("%d=%s" % (tag, value) for tag, value in self.fields)


def sort_fields(fields):
    """Order fields for transmission: header tags first, then the body as set."""
    header = []
    body = []
    for tag, value in fields:
        if tag in (TAG_BEGIN_STRING, TAG_BODY_LENGTH, TAG_CHECKSUM):
            continue
        if tag in _HEADER_RANK:
            header.append((tag, value))
        else:
            body.append((tag, value))
    header.sort(key=lambda item: _HEADER_RANK[item[0]])
    return header + body


def encode(message, begin_string="FIX.4.2"):
    # type: (Message, str) -> bytes
    """Serialise a message, computing BodyLength and CheckSum.

    ``BodyLength(9)`` counts every byte from the start of ``MsgType(35)`` up to
    and including the SOH before ``CheckSum(10)``. ``CheckSum`` is the sum of
    all preceding bytes modulo 256, rendered as exactly three digits.
    """
    body = b"".join(
        b"%d=%s%s" % (tag, value.encode("latin-1"), SOH)
        for tag, value in sort_fields(message.fields))

    prefix = b"8=%s%s9=%d%s" % (begin_string.encode("latin-1"), SOH, len(body), SOH)
    head = prefix + body
    checksum = sum(head) % 256
    return head + b"10=%03d%s" % (checksum, SOH)


def extract(buffer):
    # type: (bytes) -> tuple
    """Split one complete raw message off the front of ``buffer``.

    Returns ``(raw_message, remainder)``. Raises :class:`IncompleteMessage` when
    more bytes are needed, and :class:`MalformedMessage` when the framing itself
    is broken -- which a session must answer with a Logout, since sequence
    integrity cannot be recovered from a bad length.
    """
    if not buffer.startswith(b"8="):
        # Resynchronise if possible; anything before "8=" is junk.
        start = buffer.find(b"8=")
        if start < 0:
            if len(buffer) > 64:
                raise MalformedMessage("no BeginString found in buffered data")
            raise IncompleteMessage("awaiting BeginString")
        buffer = buffer[start:]

    length_start = buffer.find(SOH + b"9=")
    if length_start < 0:
        if len(buffer) > 64:
            raise MalformedMessage("BodyLength (9) not found after BeginString")
        raise IncompleteMessage("awaiting BodyLength")

    value_start = length_start + 3
    length_end = buffer.find(SOH, value_start)
    if length_end < 0:
        raise IncompleteMessage("awaiting end of BodyLength")

    raw_length = buffer[value_start:length_end]
    try:
        body_length = int(raw_length)
    except ValueError:
        raise MalformedMessage("BodyLength (9) is not an integer: %r" % raw_length)
    if body_length < 0 or body_length > (1 << 24):
        raise MalformedMessage("BodyLength (9) out of range: %d" % body_length)

    body_start = length_end + 1
    checksum_start = body_start + body_length
    # CheckSum is always exactly "10=nnn" plus SOH.
    message_end = checksum_start + 7

    if len(buffer) < message_end:
        raise IncompleteMessage("awaiting message body")

    if buffer[checksum_start:checksum_start + 3] != b"10=":
        raise MalformedMessage("CheckSum (10) not at the offset BodyLength implies")

    return buffer[:message_end], buffer[message_end:]


def decode(raw, validate_checksum=True):
    # type: (bytes, bool) -> Message
    """Parse one complete raw message into a :class:`Message`."""
    if not raw.endswith(SOH):
        raise MalformedMessage("message does not end with SOH")

    checksum_start = raw.rfind(SOH + b"10=")
    if checksum_start < 0:
        raise MalformedMessage("no CheckSum (10) field")

    if validate_checksum:
        expected = sum(raw[:checksum_start + 1]) % 256
        received = raw[checksum_start + 4:checksum_start + 7]
        try:
            received_value = int(received)
        except ValueError:
            raise MalformedMessage("CheckSum (10) is not numeric: %r" % received)
        if received_value != expected:
            raise MalformedMessage(
                "CheckSum (10) mismatch: got %03d, computed %03d"
                % (received_value, expected))

    fields = []
    for chunk in raw.split(SOH):
        if not chunk:
            continue
        separator = chunk.find(b"=")
        if separator <= 0:
            raise MalformedMessage("field without '=': %r" % chunk)
        raw_tag = chunk[:separator]
        try:
            tag = int(raw_tag)
        except ValueError:
            raise MalformedMessage("non-numeric tag: %r" % raw_tag)
        if tag <= 0:
            raise MalformedMessage("invalid tag number: %d" % tag)
        fields.append((tag, chunk[separator + 1:].decode("latin-1")))

    if not fields or fields[0][0] != TAG_BEGIN_STRING:
        raise MalformedMessage("BeginString (8) must be the first field")

    message = Message(fields)
    if not message.has(TAG_MSG_TYPE):
        raise MalformedMessage("MsgType (35) is missing")
    return message


class Framer(object):
    """Accumulates stream bytes and yields complete messages.

    Framing errors are surfaced by raising from :meth:`feed`, because a session
    that cannot trust BodyLength has lost sequence integrity and must log out
    rather than attempt to resynchronise.
    """

    __slots__ = ("_buffer",)

    def __init__(self):
        self._buffer = b""

    @property
    def pending(self):
        return len(self._buffer)

    def feed(self, chunk):
        # type: (bytes) -> list
        """Add bytes, returning every complete raw message now available."""
        self._buffer += chunk
        messages = []
        while self._buffer:
            try:
                raw, self._buffer = extract(self._buffer)
            except IncompleteMessage:
                break
            messages.append(raw)
        return messages

    def reset(self):
        self._buffer = b""
