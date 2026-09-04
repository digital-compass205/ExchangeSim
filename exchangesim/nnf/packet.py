"""The packet a direct-interface member exchanges with NSE, Chapter 10.

Every message travels inside a twenty-two byte prefix::

    Length (2)  |  SequenceNumber (4)  |  MD5 digest or GCM tag (16)  |  data

``Length`` counts the prefix and the data together, so framing is exact rather
than a search -- there is no delimiter and nothing to resynchronise on, which is
also why a bad frame is fatal here rather than skippable. The whole packet is
capped at 1024 bytes, "the predefined value".

Three things this module deliberately does *not* do.

It does not verify the checksum, because what those sixteen bytes hold depends
on how the connection is encrypted: an MD5 digest of the plaintext under the
existing methodology, a GCM authentication tag under the new one, and an MD5
digest again for the one unencrypted registration message that opens even an
encrypted connection. Only the session knows which. :func:`md5_digest` is here
because the digest is over the *message data alone*, never the prefix, and that
is a framing fact worth stating once.

It does not encrypt, for the same reason -- see :mod:`exchangesim.nnf.crypto`.

And it does not read the message header. ``Length`` is the only field framing
needs; the forty bytes that follow are a structure like any other and belong to
:mod:`exchangesim.nnf.layout`.
"""

import hashlib
import struct

from ..fix.message import IncompleteMessage, MalformedMessage

#: Length(2) + SequenceNumber(4) + checksum(16).
PREFIX_BYTES = 22

#: The checksum field: an MD5 digest or a GCM authentication tag, both 16 bytes.
CHECKSUM_BYTES = 16

#: "Max length will be the predefined value of 1024 bytes."
MAX_PACKET_BYTES = 1024

#: A packet carrying nothing but the forty-byte message header.
MIN_PACKET_BYTES = PREFIX_BYTES + 40

#: Length and sequence number; the digest is copied rather than unpacked.
_PREFIX = struct.Struct(">HI")

#: Sent when the connection is not encrypted: "Sequence number will be sent as
#: 0 in all the packets."
UNSEQUENCED = 0


class Prefix(object):
    """The three header fields of one packet."""

    __slots__ = ("length", "sequence", "checksum")

    def __init__(self, length, sequence, checksum):
        self.length = length
        self.sequence = sequence
        #: Sixteen raw bytes, uninterpreted: MD5 digest or GCM tag.
        self.checksum = checksum

    def __repr__(self):
        return ("Prefix(length=%d, sequence=%d, checksum=%s)"
                % (self.length, self.sequence,
                   "".join("%02x" % b for b in self.checksum)))


def md5_digest(data):
    """The MD5 of the message data.

    "Checksum is applied only on the Message data field and not on the entire
    packet" -- and, on an encrypted connection, to the *plaintext*: MD5 first,
    then encrypt.
    """
    return hashlib.md5(data).digest()


def extract(buffer):
    # type: (bytes) -> tuple
    """Split one complete packet off the front of ``buffer``.

    Returns ``(raw_packet, remainder)``, raising :class:`IncompleteMessage` when
    more bytes are needed and :class:`MalformedMessage` when the length itself
    is impossible.
    """
    if len(buffer) < _PREFIX.size:
        raise IncompleteMessage("awaiting the packet length")

    length = struct.unpack_from(">H", buffer, 0)[0]

    if length < MIN_PACKET_BYTES:
        raise MalformedMessage(
            "Length (%d) is shorter than the %d bytes a packet needs"
            % (length, MIN_PACKET_BYTES))
    if length > MAX_PACKET_BYTES:
        raise MalformedMessage(
            "Length (%d) exceeds the %d byte maximum; the length may have been "
            "written little-endian" % (length, MAX_PACKET_BYTES))

    if len(buffer) < length:
        raise IncompleteMessage("awaiting message data")

    return buffer[:length], buffer[length:]


def unpack_prefix(raw):
    # type: (bytes) -> Prefix
    """Read the prefix of a complete packet."""
    if len(raw) < PREFIX_BYTES:
        raise MalformedMessage("packet is %d bytes, shorter than a prefix"
                               % len(raw))
    length, sequence = _PREFIX.unpack_from(raw, 0)
    if length != len(raw):
        raise MalformedMessage("Length says %d bytes, packet is %d"
                               % (length, len(raw)))
    return Prefix(length, sequence, raw[_PREFIX.size:PREFIX_BYTES])


def data_of(raw):
    """The message data of a complete packet: everything past the prefix."""
    return raw[PREFIX_BYTES:]


def pack(data, sequence=UNSEQUENCED, checksum=None):
    # type: (bytes, int, bytes) -> bytes
    """Assemble a packet around ``data``, stamping its length.

    ``checksum`` defaults to the MD5 of ``data``, which is right for an
    unencrypted connection and for the registration message that opens an
    encrypted one. An encrypted connection passes its own digest or tag, over
    the plaintext, alongside already-encrypted ``data``.
    """
    if checksum is None:
        checksum = md5_digest(data)
    if len(checksum) != CHECKSUM_BYTES:
        raise ValueError("checksum is %d bytes, not %d"
                         % (len(checksum), CHECKSUM_BYTES))

    length = PREFIX_BYTES + len(data)
    if length > MAX_PACKET_BYTES:
        raise ValueError("packet of %d bytes exceeds the %d byte maximum"
                         % (length, MAX_PACKET_BYTES))

    return _PREFIX.pack(length, sequence) + checksum + data


class Framer(object):
    """Accumulates stream bytes and yields complete packets.

    The counterpart of :class:`exchangesim.fix.message.Framer` and
    :class:`exchangesim.binary.message.Framer`, and deliberately the same shape:
    a session holds one of these without knowing which protocol it is reading.
    """

    __slots__ = ("_buffer",)

    def __init__(self):
        self._buffer = b""

    @property
    def pending(self):
        return len(self._buffer)

    def feed(self, chunk):
        # type: (bytes) -> list
        """Add bytes, returning every complete raw packet now available."""
        self._buffer += chunk
        packets = []
        while self._buffer:
            try:
                raw, self._buffer = extract(self._buffer)
            except IncompleteMessage:
                break
            packets.append(raw)
        return packets

    def reset(self):
        self._buffer = b""
