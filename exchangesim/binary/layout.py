"""Message layouts: which field sits at which bit of the presence map.

A binary message is its header, a presence map, and then the fields whose bits
are set, in ascending bit position (section 6.2.1). A :class:`Layout` is that
bit table for one message type, and it is written in terms of the *FIX* tags the
rest of the simulator uses -- so decoding produces the ordinary
:class:`~exchangesim.fix.message.Message` the venue's handlers, dictionary,
audit and web board already understand, and encoding consumes one.

Where a field has no single tag -- the flat ``Submitting Broker ID`` against
FIX's ``<Parties>`` group -- the layout takes a getter and setter instead. Those
belong to the venue that defines the group, not here.
"""

from ..fix.message import MalformedMessage
from . import message as framing
from . import types as T
from . import values as V


class Field(object):
    """One entry of a presence map: a value at a bit position."""

    __slots__ = ("bit", "name", "type", "tag", "converter", "getter", "setter",
                 "redact", "repeating")

    def __init__(self, bit, name, type_, tag=None, converter=None, get=None,
                 put=None, redact=False):
        self.bit = bit
        self.name = name
        self.type = type_
        #: The FIX tag this field is, when it is exactly one tag.
        self.tag = tag
        self.converter = converter or V.TEXT
        self.getter = get
        self.setter = put
        #: True for a credential, which must not reach a log or the audit view.
        self.redact = redact
        #: Set by :class:`Block` on the fields it owns: their tag repeats once
        #: per entry, so they are appended rather than replaced.
        self.repeating = False
        if tag is None and (get is None or put is None):
            raise ValueError(
                "field '%s' needs either a tag or both a getter and a setter"
                % name)

    def read(self, message, index=0):
        """This field's wire value in ``message``, or None if absent.

        ``index`` selects the occurrence, which is what makes a repeating block
        work: entry *n* of a block is the *n*th occurrence of each member tag,
        exactly as the venue's gateway already reads ``<Parties>``.
        """
        if self.getter is not None:
            return self.getter(message)
        values = message.get_all(self.tag)
        if index >= len(values) or values[index] == "":
            return None
        return self.converter.to_wire(values[index])

    def write(self, message, value, index=0):
        """Put a decoded wire value onto ``message``."""
        if self.setter is not None:
            self.setter(message, value)
        elif self.repeating:
            message.append(self.tag, self.converter.from_wire(value))
        else:
            message.set(self.tag, self.converter.from_wire(value))

    def pack(self, message, index=0):
        """The bytes for this field, or None when it is absent."""
        value = self.read(message, index)
        return None if value is None else self.type.pack(value)

    def unpack(self, message, raw, offset, index=0):
        """Read this field off ``raw`` onto ``message``, returning the offset."""
        value, offset = self.type.unpack(raw, offset)
        self.write(message, value, index)
        return offset

    def __repr__(self):
        return "Field(%d, %s, %s)" % (self.bit, self.name, self.type)


#: Width of a repeating block's own presence map. The message header carries a
#: Bitmap Fixed Length (32); a block carries a Bitmap Variable Length (2).
BLOCK_MAP_BYTES = 2


class Block(object):
    """A repeating block, section 6.2.2.

    On the wire: a UInt16 count, then that many entries, each its own two-byte
    presence map followed by whichever of its fields that map says are there. A
    block may hold another block, which is how an entitlement carries its
    attributes.

    On the FIX side it is the ordinary group this codebase already uses -- a
    NumInGroup tag and the member tags repeated once per entry, read
    positionally. **That representation is flat**, so an outer block with more
    than one entry, each carrying an inner block of its own, could not be read
    back unambiguously; :meth:`pack` refuses to write one rather than emitting
    something it cannot decode. Nothing this venue builds comes close -- an
    entitlement report carries one broker per fragment -- and the alternative is
    a group model through the whole codec for a case no venue here has.
    """

    __slots__ = ("bit", "name", "count_tag", "fields", "by_bit", "redact",
                 "repeating", "count_type")

    def __init__(self, bit, name, count_tag, fields):
        self.bit = bit
        self.name = name
        #: The NumInGroup tag holding the entry count on the FIX side.
        self.count_tag = count_tag
        self.count_type = T.Unsigned(2)
        self.fields = tuple(sorted(fields, key=lambda field: field.bit))
        self.by_bit = dict((field.bit, field) for field in self.fields)
        if len(self.by_bit) != len(self.fields):
            raise ValueError("block '%s' reuses a bit position" % name)
        self.redact = False
        self.repeating = False
        for field in self.fields:
            field.repeating = True

    @property
    def blocks(self):
        return [field for field in self.fields if isinstance(field, Block)]

    def _ambiguous(self, message, count):
        """True when this block's entries could not be told apart on read-back.

        Only an outer block that has several entries *and* actually carries
        inner ones is ambiguous: the inner entries would arrive as one flat run
        of repeated tags with nothing to say which outer entry each belonged
        to. A nested block that is merely *defined* and left empty is fine.
        """
        if count <= 1:
            return False
        return any(message.has(block.count_tag) for block in self.blocks)

    def read_count(self, message, index=0):
        values = message.get_all(self.count_tag)
        if index >= len(values):
            return None
        try:
            return int(values[index])
        except ValueError:
            return None

    def pack(self, message, index=0):
        count = self.read_count(message, index)
        if count is None:
            return None
        if self._ambiguous(message, count):
            raise ValueError(
                "block '%s' has %d entries and holds a block of its own, which "
                "the flat group representation cannot express" % (self.name, count))

        chunks = [self.count_type.pack(count)]
        for entry in range(count):
            positions = []
            body = []
            for field in self.fields:
                packed = field.pack(message, entry)
                if packed is None:
                    continue
                positions.append(field.bit)
                body.append(packed)
            chunks.append(T.pack_presence(positions, BLOCK_MAP_BYTES))
            chunks.extend(body)
        return b"".join(chunks)

    def unpack(self, message, raw, offset, index=0):
        count, offset = self.count_type.unpack(raw, offset)
        if self.repeating:
            message.append(self.count_tag, str(count))
        else:
            message.set(self.count_tag, str(count))
        for entry in range(count):
            bits, offset = T.presence_bits(raw, offset, BLOCK_MAP_BYTES)
            for bit in bits:
                field = self.by_bit.get(bit)
                if field is None:
                    raise MalformedMessage(
                        "%s has no field at bit position %d of its block"
                        % (self.name, bit))
                offset = field.unpack(message, raw, offset, entry)
        return offset

    def __repr__(self):
        return "Block(%d, %s, %d entries)" % (self.bit, self.name,
                                              len(self.fields))


class Layout(object):
    """The bit table of one binary message type."""

    def __init__(self, msg_type, name, fix_msg_type, fields):
        self.msg_type = msg_type
        self.name = name
        #: The MsgType(35) of the same message in the FIX encoding.
        self.fix_msg_type = fix_msg_type
        self.fields = tuple(sorted(fields, key=lambda field: field.bit))
        self.by_bit = dict((field.bit, field) for field in self.fields)
        if len(self.by_bit) != len(self.fields):
            raise ValueError("layout '%s' reuses a bit position" % name)

    def decode_body(self, raw, message):
        """Read the presence map and body of ``raw`` onto ``message``."""
        bits, offset = T.presence_bits(raw, framing.HEADER_BYTES)
        for bit in bits:
            field = self.by_bit.get(bit)
            if field is None:
                # The width of an unknown field is unknown, so the rest of the
                # message cannot be parsed at all. Section 4.8's treatment of a
                # frame that cannot be trusted applies: drop the connection.
                raise MalformedMessage(
                    "%s has no field at bit position %d" % (self.name, bit))
            offset = field.unpack(message, raw, offset)
        end = len(raw) - framing.TRAILER_BYTES
        if offset != end:
            raise MalformedMessage(
                "%s body is %d bytes, the presence map accounts for %d"
                % (self.name, end - framing.HEADER_BYTES,
                   offset - framing.HEADER_BYTES))

    def encode_body(self, message):
        """The presence map and body bytes for ``message``."""
        positions = []
        chunks = []
        for field in self.fields:
            packed = field.pack(message)
            if packed is None:
                continue
            positions.append(field.bit)
            chunks.append(packed)
        return T.pack_presence(positions), b"".join(chunks)

    def __repr__(self):
        return "Layout(%d, %s)" % (self.msg_type, self.name)


#: A binary message type with no layout decodes to this MsgType, which no
#: dialect defines -- so the dictionary refuses it as an invalid message type,
#: which is exactly what the specification's Message Reject Code 11 says. The
#: number survives the round trip so a Reject can name what it refused.
UNSUPPORTED_PREFIX = "B"


def unsupported_msg_type(binary_type):
    return "%s%d" % (UNSUPPORTED_PREFIX, binary_type)


class BinaryDictionary(object):
    """Every layout of a dialect, resolvable from either encoding's type."""

    def __init__(self, layouts, select=None):
        self.by_type = {}
        self.by_fix_type = {}
        for layout in layouts:
            self.by_type[layout.msg_type] = layout
            # First layout wins: two FIX messages may share one binary type
            # (a cancel reject is an Execution Report here), and the chooser
            # below is what disambiguates the other direction.
            self.by_fix_type.setdefault(layout.fix_msg_type, layout)
        self._select = select

    def layout(self, binary_type):
        return self.by_type.get(binary_type)

    def layout_for(self, message):
        """The layout to encode ``message`` with, or None when it has none."""
        if self._select is not None:
            layout = self._select(message)
            if layout is not None:
                return layout
        return self.by_fix_type.get(message.msg_type)

    def fix_msg_type(self, binary_type):
        layout = self.by_type.get(binary_type)
        return layout.fix_msg_type if layout else unsupported_msg_type(binary_type)

    def binary_type(self, fix_msg_type):
        """The binary message type number for a FIX MsgType, or None.

        Understands the placeholder an unsupported inbound type decoded to, so
        a Reject can carry the number the client actually sent.
        """
        layout = self.by_fix_type.get(fix_msg_type)
        if layout is not None:
            return layout.msg_type
        if fix_msg_type and fix_msg_type[0] == UNSUPPORTED_PREFIX:
            try:
                return int(fix_msg_type[1:])
            except ValueError:
                return None
        return None
