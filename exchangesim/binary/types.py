"""Data types of the binary dictionary, section 6.1.

Every value on the wire is one of these. They convert between bytes and plain
Python values; turning a Python value into the *string* a FIX field carries is
the business of :mod:`exchangesim.binary.values`, because the same wire type
serves fields whose FIX spelling differs -- ``Side`` is a UInt8 holding 1/2/5,
and so is ``OrderCapacity``, which FIX spells ``A``/``P``.

Two properties of the format are load-bearing and easy to get wrong:

* Integers are **little-endian**, including the length and the checksum. A
  big-endian reading of the length frames short messages plausibly and then
  desynchronises, which is the worst kind of bug to find in a stream protocol.
* An Alphanumeric field is **null-terminated within its fixed width**, and the
  terminator counts towards that width -- so ``Alphanumeric Fixed Length (21)``
  carries at most twenty characters.
"""

import struct

from ..fix.message import MalformedMessage

NUL = b"\x00"


def _need(raw, offset, count):
    if offset + count > len(raw):
        raise MalformedMessage(
            "message ends mid-field: %d bytes needed at offset %d, %d available"
            % (count, offset, len(raw) - offset))


class WireType(object):
    """One data type: how a value packs to bytes and unpacks back.

    ``width`` is the fixed number of bytes the type occupies, or None for the
    self-describing variable-length type.
    """

    width = None
    name = "unknown"

    def pack(self, value):
        # type: (object) -> bytes
        raise NotImplementedError

    def unpack(self, raw, offset):
        # type: (bytes, int) -> tuple
        """Return ``(value, next_offset)``."""
        raise NotImplementedError

    def __repr__(self):
        return self.name


class Unsigned(WireType):
    """UInt8, UInt16 and UInt32 -- unsigned, little-endian."""

    _FORMATS = {1: "<B", 2: "<H", 4: "<I"}

    def __init__(self, width):
        self.width = width
        self.name = "UInt%d" % (width * 8)
        self._format = self._FORMATS[width]
        self._limit = (1 << (width * 8)) - 1

    def pack(self, value):
        value = int(value)
        if value < 0 or value > self._limit:
            raise ValueError("%s out of range for %s" % (value, self.name))
        return struct.pack(self._format, value)

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        value = struct.unpack_from(self._format, raw, offset)[0]
        return value, offset + self.width


class Decimal(WireType):
    """Decimal: a signed 8-byte integer with eight implied decimal places.

    Quantities travel in this type too, so a whole-number quantity of 1,000 is
    100000000000 on the wire. Scaling is done on integers throughout -- see
    :mod:`exchangesim.binary.values` -- because the whole point of the venue's
    integer prices is that no binary float ever touches one.
    """

    width = 8
    name = "Decimal"

    def pack(self, value):
        return struct.pack("<q", int(value))

    def unpack(self, raw, offset):
        _need(raw, offset, 8)
        return struct.unpack_from("<q", raw, offset)[0], offset + 8


class Byte(WireType):
    """Byte: one ASCII character, held as its ordinal.

    ``ExecType`` is this type, which is why its values are the FIX characters
    ``0``/``F``/``H`` rather than the small integers the neighbouring
    ``OrderStatus`` uses.
    """

    width = 1
    name = "Byte"

    def pack(self, value):
        if isinstance(value, int):
            return struct.pack("<B", value)
        text = str(value)
        if len(text) != 1:
            raise ValueError("Byte takes exactly one character, got %r" % text)
        return text.encode("latin-1")

    def unpack(self, raw, offset):
        _need(raw, offset, 1)
        return raw[offset], offset + 1


class AlphaFixed(WireType):
    """Alphanumeric Fixed Length (n): ASCII, null-terminated, null-padded.

    The declared width includes the terminator. A value that fills the field
    exactly is still accepted rather than rejected -- the specification says the
    venue overwrites the last character with the terminator -- so this truncates
    to fit, matching what the real gateway does with an over-long value.
    """

    def __init__(self, width):
        self.width = width
        self.name = "Alpha(%d)" % width

    @property
    def capacity(self):
        """Characters that fit, the null terminator having taken one byte."""
        return self.width - 1

    def pack(self, value):
        data = str(value).encode("latin-1")[:self.capacity]
        return data + NUL * (self.width - len(data))

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        chunk = raw[offset:offset + self.width]
        end = chunk.find(NUL)
        if end < 0:
            # No terminator anywhere in the field: the specification says to
            # take the whole width as the value.
            end = self.width
        return chunk[:end].decode("latin-1"), offset + self.width


class AlphaVariable(WireType):
    """Alphanumeric Variable Length: a UInt16 length, then that many bytes.

    The length counts the null terminator, so an empty value is a length of one
    and a single null byte.
    """

    width = None

    def __init__(self, max_length):
        self.max_length = max_length
        self.name = "AlphaVar(%d)" % max_length

    def pack(self, value):
        data = str(value).encode("latin-1")[:self.max_length]
        return struct.pack("<H", len(data) + 1) + data + NUL

    def unpack(self, raw, offset):
        _need(raw, offset, 2)
        length = struct.unpack_from("<H", raw, offset)[0]
        offset += 2
        _need(raw, offset, length)
        chunk = raw[offset:offset + length]
        end = chunk.find(NUL)
        if end < 0:
            end = length
        return chunk[:end].decode("latin-1"), offset + length


#: The body presence map is 32 bytes -- 256 bit positions -- on every message.
PRESENCE_MAP_BYTES = 32


def presence_bits(raw, offset, width=PRESENCE_MAP_BYTES):
    """The set bit positions of a presence map, lowest position first.

    Position 0 is the **most** significant bit of the first byte (section
    6.2.1), so reading the fields in ascending bit position reads them in the
    order they were written.
    """
    _need(raw, offset, width)
    bits = []
    for index in range(width):
        byte = raw[offset + index]
        if not byte:
            continue
        for bit in range(8):
            if byte & (1 << (7 - bit)):
                bits.append(index * 8 + bit)
    return bits, offset + width


def pack_presence(positions, width=PRESENCE_MAP_BYTES):
    """Build a presence map with ``positions`` set."""
    data = bytearray(width)
    for position in positions:
        index, bit = divmod(position, 8)
        if index >= width:
            raise ValueError("bit position %d exceeds the presence map" % position)
        data[index] |= 1 << (7 - bit)
    return bytes(data)


# -- checksum ----------------------------------------------------------------

def _crc32c_table():
    # CRC-32C (Castagnoli), polynomial 0x1EDC6F41, reflected as 0x82F63B78.
    table = []
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
        table.append(value)
    return tuple(table)


_CRC32C = _crc32c_table()


def crc32c(data):
    """The trailer checksum, section 4.8: CRC32C over everything before it.

    Not ``zlib.crc32``, which is the ISO-HDLC polynomial and gives a different
    number for the same bytes. A client whose checksum disagrees is disconnected
    without a Logout, so this cannot be approximately right.
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF
