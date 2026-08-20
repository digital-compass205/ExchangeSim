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
    """One entry of a message's presence map."""

    __slots__ = ("bit", "name", "type", "tag", "converter", "getter", "setter",
                 "redact")

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
        if tag is None and (get is None or put is None):
            raise ValueError(
                "field '%s' needs either a tag or both a getter and a setter"
                % name)

    def read(self, message):
        """The wire value of this field in ``message``, or None if absent."""
        if self.getter is not None:
            return self.getter(message)
        text = message.get(self.tag)
        if text is None or text == "":
            return None
        return self.converter.to_wire(text)

    def write(self, message, value):
        """Put a decoded wire value onto ``message``."""
        if self.setter is not None:
            self.setter(message, value)
        else:
            message.set(self.tag, self.converter.from_wire(value))

    def __repr__(self):
        return "Field(%d, %s, %s)" % (self.bit, self.name, self.type)


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
            value, offset = field.type.unpack(raw, offset)
            field.write(message, value)
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
            value = field.read(message)
            if value is None:
                continue
            positions.append(field.bit)
            chunks.append(field.type.pack(value))
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
