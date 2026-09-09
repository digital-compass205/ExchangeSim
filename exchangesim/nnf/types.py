"""Data types of the NNF protocol, Chapter 2 "General Guidelines".

Four properties of the format are load-bearing, and each one fails silently if
it is got wrong -- the message still frames, and only a real client notices.

* **Integers are big-endian.** The document says so outright: "The trading
  system host end uses big-endian order", and tells a little-endian member to
  twiddle. The other binary venue here is little-endian, so this is exactly the
  sort of thing that gets copied across by mistake.
* **The C type names are C's, not Python's.** ``LONG`` is four bytes and
  ``LONG LONG`` is eight. A ``Long`` that quietly meant eight frames plausibly
  and then desynchronises.
* **Structures are ``pragma pack 2``.** An eight-byte ``LONG LONG`` sits at
  offset 14 of the message header, which no natural alignment would allow. That
  is why :mod:`exchangesim.nnf.layout` takes offsets from the specification
  rather than computing them.
* **Strings are blank-padded and never NUL-terminated** -- "No NULL terminated
  strings should be sent to the host end. Instead, fill it with blanks". Real
  clients send both, so :class:`Char` reads either and writes what the document
  asks for.

Prices are a separate matter and deliberately not handled here: the wire carries
paise, and :class:`exchangesim.core.prices.PriceCodec` with two decimal places is
the one place that conversion happens.
"""

import calendar
import struct

from ..fix.message import MalformedMessage

NUL = b"\x00"
BLANK = b" "

#: NNF time fields are seconds since midnight on 1 January 1980, not the Unix
#: epoch. ``Timestamp`` on selected transaction codes is nanoseconds from the
#: same instant.
NSE_EPOCH = calendar.timegm((1980, 1, 1, 0, 0, 0))

#: Difference between the two epochs, in seconds. Positive: NSE's is later.
EPOCH_OFFSET = NSE_EPOCH


def to_nse_seconds(unix_seconds):
    """Seconds since the NSE epoch, from a Unix timestamp."""
    return int(unix_seconds) - EPOCH_OFFSET


def from_nse_seconds(nse_seconds):
    """A Unix timestamp, from seconds since the NSE epoch."""
    return int(nse_seconds) + EPOCH_OFFSET


def to_nse_nanoseconds(unix_seconds):
    """Nanoseconds since the NSE epoch, from a (possibly fractional) Unix time."""
    return int(round((float(unix_seconds) - EPOCH_OFFSET) * 1000000000))


#: "Jiffy is a Unit of Time (1 second = 65536 jiffies)" -- Chapter 2, of the
#: header's ``TimeStamp1``. The protocol's third time unit, after seconds and
#: nanoseconds, and the one a message download's cursor is counted in.
JIFFIES_PER_SECOND = 65536


def epoch_seconds(when):
    """A ``datetime`` as fractional Unix seconds, read as UTC.

    The clock is injected everywhere here and hands out ``datetime``, while
    every time field on this wire is a count from an epoch. One conversion,
    beside the three that use it.
    """
    return calendar.timegm(when.timetuple()) + when.microsecond / 1000000.0


def to_nse_jiffies(unix_seconds):
    """Jiffies since the NSE epoch, from a (possibly fractional) Unix time.

    ASSUMPTION: the epoch. The document gives ``TimeStamp1``'s unit and never
    its origin, so this uses the one the sibling ``Timestamp`` field is defined
    from. Nothing a client does depends on the choice -- it echoes the value
    back in a download request rather than reading it -- but two time fields in
    one header disagreeing about their origin would trap anyone reading a
    capture beside a real one.
    """
    return int((float(unix_seconds) - EPOCH_OFFSET) * JIFFIES_PER_SECOND)


def _need(raw, offset, count):
    if offset < 0 or offset + count > len(raw):
        raise MalformedMessage(
            "message ends mid-field: %d bytes needed at offset %d, %d available"
            % (count, offset, max(0, len(raw) - offset)))


class WireType(object):
    """One data type: how a value packs to bytes and unpacks back.

    ``width`` is the fixed number of bytes the type occupies. Every NNF type is
    fixed width -- there is no self-describing type in this protocol, which is
    what makes a structure a table of offsets rather than a parse.
    """

    width = None
    name = "unknown"

    #: What a field of this type holds when nothing was assigned to it. The
    #: specification is explicit: "All numeric data must be set to zero (0)
    #: before sending to the host, unless a value is assigned to it."
    default = 0

    def pack(self, value):
        # type: (object) -> bytes
        raise NotImplementedError

    def unpack(self, raw, offset):
        # type: (bytes, int) -> object
        """The value at ``offset``. Fixed width, so the caller knows what follows."""
        raise NotImplementedError

    def __repr__(self):
        return self.name


class Signed(WireType):
    """CHAR, SHORT, LONG and LONG LONG -- signed, big-endian.

    Signedness is not incidental: NSE uses -1 as a sentinel in several fields,
    and an unsigned reading turns it into a very large quantity.
    """

    _FORMATS = {1: ">b", 2: ">h", 4: ">i", 8: ">q"}
    _NAMES = {1: "CHAR", 2: "SHORT", 4: "LONG", 8: "LONG LONG"}

    def __init__(self, width):
        self.width = width
        self.name = self._NAMES[width]
        self._format = self._FORMATS[width]
        self._low = -(1 << (width * 8 - 1))
        self._high = (1 << (width * 8 - 1)) - 1

    def pack(self, value):
        value = int(value or 0)
        if value < self._low or value > self._high:
            raise ValueError("%s out of range for %s" % (value, self.name))
        return struct.pack(self._format, value)

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        return struct.unpack_from(self._format, raw, offset)[0]


class Unsigned(WireType):
    """UINT and UNSIGNED LONG -- unsigned, big-endian."""

    _FORMATS = {2: ">H", 4: ">I", 8: ">Q"}
    _NAMES = {2: "UINT", 4: "UNSIGNED LONG", 8: "UNSIGNED LONG LONG"}

    def __init__(self, width):
        self.width = width
        self.name = self._NAMES[width]
        self._format = self._FORMATS[width]
        self._limit = (1 << (width * 8)) - 1

    def pack(self, value):
        value = int(value or 0)
        if value < 0 or value > self._limit:
            raise ValueError("%s out of range for %s" % (value, self.name))
        return struct.pack(self._format, value)

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        return struct.unpack_from(self._format, raw, offset)[0]


class Double(WireType):
    """DOUBLE -- eight bytes, IEEE 754, big-endian.

    NSE carries the OrderNumber in one of these, which is a whole number that
    happens to travel as a float. :meth:`unpack` therefore returns an ``int``
    when the value has no fractional part, so an order number renders as
    ``12345678901234`` rather than ``1.2345678901234e+13`` everywhere above the
    codec. Doubles hold integers exactly to 2**53, well beyond what NSE assigns.
    """

    width = 8
    name = "DOUBLE"

    def pack(self, value):
        return struct.pack(">d", float(value or 0))

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        value = struct.unpack_from(">d", raw, offset)[0]
        whole = int(value)
        return whole if whole == value else value


class Char(WireType):
    """A fixed-width character array.

    Written blank-padded, as Chapter 2 requires. Read tolerantly: a real client
    may NUL-terminate within the width despite the instruction not to, so the
    value is cut at the first NUL and then stripped of surrounding blanks.
    Getting this half right is invisible in a round trip through the simulator
    and fails only against somebody else's encoder, which is why it is one
    method rather than a habit at each call site.

    ``justify`` is ``"left"`` for the alphabetic fields and ``"right"`` for the
    numeric-in-a-string ones; the specification fixes it per field.
    """

    name = "CHAR[]"
    default = ""

    def __init__(self, width, justify="left"):
        if justify not in ("left", "right"):
            raise ValueError("justify must be 'left' or 'right', not %r" % justify)
        self.width = width
        self.justify = justify
        self.name = "CHAR[%d]" % width

    def pack(self, value):
        text = "" if value is None else str(value)
        raw = text.encode("latin-1", "replace")[:self.width]
        pad = BLANK * (self.width - len(raw))
        return raw + pad if self.justify == "left" else pad + raw

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        chunk = raw[offset:offset + self.width]
        return chunk.split(NUL, 1)[0].decode("latin-1").strip()


class Raw(WireType):
    """A fixed-width run of bytes that is not text.

    The specification declares a cryptographic key as ``CHAR[32]``, but it is a
    key: every one of the 256 byte values is legal in it, NULs and spaces
    included. Reading one with :class:`Char` would truncate it at the first NUL
    and strip the spaces off either end -- silently, and only for some keys, so
    a test with a lucky key would pass. Anything whose bytes are a value rather
    than a word uses this instead.

    Carried above the wire as a latin-1 string, which is the identity mapping
    on bytes, so nothing between here and the venue has to hold ``bytes``.
    """

    name = "CHAR[] (raw)"
    default = ""

    def __init__(self, width):
        self.width = width
        self.name = "CHAR[%d] (raw)" % width

    def pack(self, value):
        if isinstance(value, bytes):
            raw = value
        else:
            raw = ("" if value is None else str(value)).encode("latin-1")
        return (raw + NUL * self.width)[:self.width]

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        return raw[offset:offset + self.width].decode("latin-1")


class Reserved(WireType):
    """A reserved or filler run of bytes.

    "All reserved fields mentioned should be mapped to CHAR buffer and
    initialized to NULL" -- so these are written as NULs, not blanks, and are
    the one place in the protocol where that is right. Nothing is read back:
    a reserved field carries no value, and giving it a tag would put noise in
    the audit and invite somebody to start meaning something by it.
    """

    name = "Reserved"
    default = None

    def __init__(self, width):
        self.width = width
        self.name = "Reserved[%d]" % width

    def pack(self, value=None):
        return NUL * self.width

    def unpack(self, raw, offset):
        _need(raw, offset, self.width)
        return None


# -- the types by their specification names, so a layout reads like the table --

CHAR = Signed(1)
SHORT = Signed(2)
LONG = Signed(4)
LONG_LONG = Signed(8)
UINT = Unsigned(2)
UNSIGNED_LONG = Unsigned(4)
DOUBLE = Double()
