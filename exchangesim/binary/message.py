"""Framing a binary message: the header, the trailer and the stream reader.

Section 7.2 fixes the header at twenty-two bytes followed by the thirty-two-byte
body presence map, and section 7.3 puts a four-byte CRC32C at the end. ``Length``
counts the whole thing, so framing is exact rather than a search: unlike FIX,
there is no delimiter to resynchronise on and nothing to hunt for.

That is also why a bad frame is fatal here. FIX can skip forward to the next
``8=``; a binary stream whose length or checksum is wrong has an unknown
position, and section 4.8 says the gateway drops such a connection outright.
"""

import struct

from ..fix.message import IncompleteMessage, MalformedMessage
from . import types as T

#: ASCII STX, the first byte of every message (section 7.2).
START_OF_MESSAGE = 0x02

#: Header fields up to but not including the presence map.
HEADER_BYTES = 22

#: Where the body starts: the header plus the presence map.
BODY_OFFSET = HEADER_BYTES + T.PRESENCE_MAP_BYTES

#: The CRC32C trailer.
TRAILER_BYTES = 4

#: The shortest legal message: header, presence map and trailer, no body.
MIN_MESSAGE_BYTES = BODY_OFFSET + TRAILER_BYTES

#: Comp ID is Alphanumeric Fixed Length (12) -- eleven characters and a null.
COMP_ID = T.AlphaFixed(12)

_HEADER = struct.Struct("<BHBIBB")


class Header(object):
    """The eight header fields of one message."""

    __slots__ = ("msg_type", "seq_num", "poss_dup", "poss_resend", "comp_id")

    def __init__(self, msg_type, seq_num, comp_id, poss_dup=False,
                 poss_resend=False):
        self.msg_type = msg_type
        self.seq_num = seq_num
        self.comp_id = comp_id
        self.poss_dup = poss_dup
        self.poss_resend = poss_resend

    def __repr__(self):
        return ("Header(type=%d, seq=%d, comp_id=%r, poss_dup=%s)"
                % (self.msg_type, self.seq_num, self.comp_id, self.poss_dup))


def extract(buffer):
    # type: (bytes) -> tuple
    """Split one complete raw message off the front of ``buffer``.

    Returns ``(raw_message, remainder)``, raising :class:`IncompleteMessage`
    when more bytes are needed and :class:`MalformedMessage` when the framing
    itself is broken.
    """
    if len(buffer) < 3:
        if buffer and buffer[0] != START_OF_MESSAGE:
            raise MalformedMessage(
                "message does not begin with StartOfMessage (0x02) but 0x%02x"
                % buffer[0])
        raise IncompleteMessage("awaiting the message header")

    if buffer[0] != START_OF_MESSAGE:
        raise MalformedMessage(
            "message does not begin with StartOfMessage (0x02) but 0x%02x"
            % buffer[0])

    length = struct.unpack_from("<H", buffer, 1)[0]
    if length < MIN_MESSAGE_BYTES:
        raise MalformedMessage(
            "Length (%d) is shorter than the %d bytes a message needs"
            % (length, MIN_MESSAGE_BYTES))

    if len(buffer) < length:
        raise IncompleteMessage("awaiting message body")

    return buffer[:length], buffer[length:]


def unpack_header(raw, validate_checksum=True):
    # type: (bytes, bool) -> Header
    """Read the header of a complete frame, checking the trailer checksum."""
    if len(raw) < MIN_MESSAGE_BYTES:
        raise MalformedMessage("message is %d bytes, shorter than a header"
                               % len(raw))

    start, length, msg_type, seq_num, poss_dup, poss_resend = _HEADER.unpack_from(raw)
    if start != START_OF_MESSAGE:
        raise MalformedMessage("StartOfMessage is 0x%02x, not 0x02" % start)
    if length != len(raw):
        raise MalformedMessage("Length says %d bytes, frame is %d"
                               % (length, len(raw)))

    if validate_checksum:
        expected = T.crc32c(raw[:-TRAILER_BYTES])
        received = struct.unpack_from("<I", raw, len(raw) - TRAILER_BYTES)[0]
        if received != expected:
            raise MalformedMessage(
                "CheckSum mismatch: got 0x%08X, computed 0x%08X"
                % (received, expected))

    comp_id, _ = COMP_ID.unpack(raw, 10)
    return Header(msg_type, seq_num, comp_id, bool(poss_dup), bool(poss_resend))


def pack(header, presence, body):
    # type: (Header, bytes, bytes) -> bytes
    """Assemble a frame and stamp its length and checksum."""
    length = BODY_OFFSET + len(body) + TRAILER_BYTES
    if length > 0xFFFF:
        raise ValueError("message of %d bytes exceeds the Length field" % length)

    head = _HEADER.pack(START_OF_MESSAGE, length, header.msg_type,
                        header.seq_num, 1 if header.poss_dup else 0,
                        1 if header.poss_resend else 0)
    frame = head + COMP_ID.pack(header.comp_id) + presence + body
    return frame + struct.pack("<I", T.crc32c(frame))


def body_of(raw):
    """The presence map and body of a complete frame, checksum removed."""
    return raw[HEADER_BYTES:len(raw) - TRAILER_BYTES]


class Framer(object):
    """Accumulates stream bytes and yields complete messages.

    The counterpart of :class:`exchangesim.fix.message.Framer`, and deliberately
    the same shape: the session layer holds one of these without knowing which
    encoding it is reading.
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
