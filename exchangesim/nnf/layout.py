"""Structures: a table of offsets, and the bitfields inside them.

Every NNF structure is a fixed-width record whose fields sit at offsets the
specification prints. That has two consequences worth stating, because both run
against the habits the other two protocols here encourage.

**Offsets are transcribed, never computed.** The structures are ``pragma pack 2``
and pad odd-sized runs to even, so a walk that summed field widths would drift
the first time a ``CHAR[5]`` was followed by a ``SHORT`` -- and an eight-byte
``LONG LONG`` genuinely does sit at offset 14 of the message header. Writing the
offset next to the field also makes a layout diff-able against the PDF table it
came from, which is the only review that can catch a transcription slip.

**Every field is on the wire on every message.** There is no presence map and
no optionality: a field nobody set travels as blanks or zeros. So a layout says
what the *size* of a structure is, and :meth:`Layout.body_size` is checked
against the size the specification tables -- that equality is the cheapest test
in this venue and it catches almost every transcription error.

A layout turns bytes into an :class:`exchangesim.fix.message.Message` and back.
That is not a claim that NNF is FIX: a ``Message`` is an ordered list of
numerically-keyed fields with a type discriminator, which is exactly what this
protocol has, and reusing it means the dictionary, the audit, the control plane
and the board all work here without knowing this protocol exists.
"""

from ..fix.message import MalformedMessage, Message
from ..wire import values as V
from . import types as T

#: Tag 35 holds the decimal transaction code, as a string. Not a FIX MsgType:
#: nothing maps ``2000`` onto ``D``, because NNF has no FIX twin and the audit
#: would then print a name the venue never used.
TRANSACTION_CODE = 35


class Field(object):
    """One field of a structure, at the offset the specification gives it."""

    __slots__ = ("tag", "name", "wire", "offset", "converter", "redact")

    def __init__(self, tag, name, wire, offset, converter=None, redact=False):
        self.tag = tag
        self.name = name
        self.wire = wire
        self.offset = offset
        self.converter = converter or _default_converter(wire)
        #: Struck out in the audit's hex dump. The sign-on password is the case.
        self.redact = redact

    @property
    def end(self):
        return self.offset + self.wire.width

    def decode(self, raw, message):
        """Set this field on ``message``, unless it is blank.

        A character field full of blanks is how a fixed-width protocol says
        "nothing here" -- there is no other way to say it, since the field
        travels either way. Above the wire, absence is how every other protocol
        here says the same thing, so a blank field is left off the message
        rather than set to the empty string. A *numeric* zero is a real value
        and is always set: ``ErrorCode`` zero says the message reports no error,
        and dropping it would lose that.
        """
        value = self.wire.unpack(raw, self.offset)
        text = self.converter.from_wire(value)
        if text != "":
            message.set(self.tag, text)

    def encode(self, message, buffer):
        text = message.get(self.tag)
        value = self.wire.default if text is None else self.converter.to_wire(text)
        buffer[self.offset:self.end] = self.wire.pack(value)

    def __repr__(self):
        return "Field(%d, %r, %s, %d)" % (self.tag, self.name, self.wire,
                                          self.offset)


def _default_converter(wire):
    """Text for a character array, an integer for everything else.

    A price is the exception and says so at its call site: the wire carries
    paise and the field carries the decimal string every other surface here
    speaks, which is what :class:`Scaled` is for.
    """
    return V.TEXT if isinstance(wire, T.Char) else V.NUMBER


class Scaled(V.Converter):
    """A price: an integer number of paise on the wire, a decimal string above.

    "All price fields must be multiplied by 100 before sending to the host end
    and divided by 100 while receiving" -- done by parsing digits rather than by
    multiplying a float, for the reason :mod:`exchangesim.core.prices` exists.
    """

    def __init__(self, places=2):
        self.places = places

    def to_wire(self, text):
        return V.scale(text, self.places)

    def from_wire(self, value):
        return V.unscale(value, self.places)


#: Prices are in paise throughout the Capital Market protocol.
PAISE = Scaled(2)


class Reserved(object):
    """A run of bytes the specification reserves, carrying nothing.

    Declared rather than left as a gap, for two reasons. It lets a layout be
    read line for line against the table it came from, reserved rows included;
    and it makes ``body_size`` equal the published packet length exactly, so
    :meth:`Layout.check` catches a field that is too *short* as well as one that
    overruns. Without it a trailing reserved run would hide any under-run.

    Nothing is read back -- a reserved field has no value and giving it a tag
    would put noise in the audit and invite somebody to start meaning something
    by it. On the way out it is NULs, which is what Chapter 2 asks for and what
    the zeroed buffer already holds.
    """

    __slots__ = ("name", "offset", "width")

    def __init__(self, offset, width, name="Reserved"):
        self.name = name
        self.offset = offset
        self.width = width

    @property
    def end(self):
        return self.offset + self.width

    @property
    def tags(self):
        return ()

    def decode(self, raw, message):
        """Nothing: a reserved run has no value."""

    def encode(self, message, buffer):
        """Nothing: the buffer is already zero, which is what NUL means here."""

    def __repr__(self):
        return "Reserved(%d, %d)" % (self.offset, self.width)


class Flags(object):
    """A bitfield exploded into one ``Y``/``N`` field per bit.

    NSE spells a price's order type, its time in force and its lifecycle state
    as bits of one two-byte structure rather than as scalar fields, so there is
    nothing to map onto ``OrdType(40)`` or ``TimeInForce(59)`` and no attempt is
    made to. Each bit gets a field of its own, which the dictionary can name and
    the audit can print as ``IOC=YES``.

    ``bits`` is read **most significant bit of the first byte first**, which is
    the order the specification's big-endian table prints. A ``None`` is a
    reserved bit: it is neither read nor written, so nothing starts meaning
    something by it. The document publishes a little-endian table beside the
    big-endian one; the host is big-endian, so that is the one transcribed.
    """

    __slots__ = ("offset", "width", "bits", "name")

    def __init__(self, name, offset, width, bits):
        if len(bits) != width * 8:
            raise ValueError("%s: %d bits given for a %d byte field"
                             % (name, len(bits), width))
        self.name = name
        self.offset = offset
        self.width = width
        self.bits = tuple(bits)

    @property
    def end(self):
        return self.offset + self.width

    @property
    def tags(self):
        return tuple(tag for tag in self.bits if tag is not None)

    def decode(self, raw, message):
        T._need(raw, self.offset, self.width)
        chunk = bytearray(raw[self.offset:self.end])
        for index, tag in enumerate(self.bits):
            if tag is None:
                continue
            byte = chunk[index // 8]
            set_ = byte & (0x80 >> (index % 8))
            message.set(tag, "Y" if set_ else "N")

    def encode(self, message, buffer):
        chunk = bytearray(self.width)
        for index, tag in enumerate(self.bits):
            if tag is None or message.get(tag) != "Y":
                continue
            chunk[index // 8] |= 0x80 >> (index % 8)
        buffer[self.offset:self.end] = bytes(chunk)

    def __repr__(self):
        return "Flags(%r, %d, %d)" % (self.name, self.offset, self.width)


class HeaderLayout(object):
    """The fields every message of a segment carries, and where the code lives.

    Parameterised rather than hard-coded so that a second segment can differ:
    the Capital Market header and the ``INNER_MESSAGE_HEADER`` of the download
    responses hold the same nine fields in different orders, and Futures &
    Options is free to differ again.
    """

    __slots__ = ("size", "fields", "code_field", "length_tag")

    #: The header field holding the length of the whole message. Found by name
    #: rather than by tag, because the tag is the dialect's to choose and the
    #: field is Chapter 2's -- "set to the length of the entire message,
    #: including the length of message header". Filling it in the codec means
    #: no handler can forget it.
    LENGTH_FIELD_NAME = "MessageLength"

    def __init__(self, size, fields):
        self.size = size
        self.fields = tuple(fields)
        self.code_field = self._find_code_field()
        self.length_tag = self._find_length_tag()

    def _find_code_field(self):
        for field in self.fields:
            if field.tag == TRANSACTION_CODE:
                return field
        raise ValueError("a header must carry the transaction code (tag %d)"
                         % TRANSACTION_CODE)

    def _find_length_tag(self):
        for field in self.fields:
            if field.name == self.LENGTH_FIELD_NAME:
                return field.tag
        return None

    def transaction_code(self, raw):
        """The transaction code of a message, before its layout is known."""
        return self.code_field.wire.unpack(raw, self.code_field.offset)

    def decode(self, raw, message):
        for field in self.fields:
            field.decode(raw, message)


class Layout(object):
    """One transaction code's structure.

    ``size`` is the packet length the specification tables. It is given rather
    than derived, so that :meth:`check` can compare the two and refuse a
    structure whose fields do not reach the end of it.
    """

    __slots__ = ("msg_type", "name", "size", "header", "fields")

    def __init__(self, msg_type, name, size, fields, header):
        self.msg_type = msg_type
        self.name = name
        self.size = size
        self.header = header
        self.fields = tuple(fields)

    @property
    def body_size(self):
        """The furthest byte any field reaches -- what the size should be."""
        return max([self.header.size] + [field.end for field in self.fields])

    @property
    def tags(self):
        tags = []
        for field in self.fields:
            if hasattr(field, "tags"):          # Flags, Reserved
                tags.extend(field.tags)
            else:
                tags.append(field.tag)
        return tuple(tags)

    def check(self):
        """Why this layout disagrees with its published size, or None.

        Called by a test rather than at import: a structure that overruns is a
        transcription error, and the place to find it is a failing test naming
        the transaction code, not a traceback during start-up.
        """
        if self.body_size != self.size:
            return ("%s (%s) declares %d bytes but its fields reach %d"
                    % (self.name, self.msg_type, self.size, self.body_size))
        seen = {}
        for field in self.fields:
            for offset in range(field.offset, field.end):
                if offset in seen:
                    return ("%s (%s): %s overlaps %s at offset %d"
                            % (self.name, self.msg_type, field.name,
                               seen[offset], offset))
                seen[offset] = field.name
        return None

    def decode(self, raw, message=None):
        if len(raw) != self.size:
            raise MalformedMessage(
                "%s (%s) is %d bytes, not the %d its structure declares"
                % (self.name, self.msg_type, len(raw), self.size))
        message = message or Message.create(self.msg_type)
        self.header.decode(raw, message)
        for field in self.fields:
            field.decode(raw, message)
        return message

    def encode(self, message):
        buffer = bytearray(self.size)
        if self.header.length_tag is not None:
            # Always the structure's own size, and never a handler's business:
            # a fixed-width message has exactly one possible length.
            message.set(self.header.length_tag, str(self.size))
        for field in self.header.fields:
            field.encode(message, buffer)
        for field in self.fields:
            field.encode(message, buffer)
        return bytes(buffer)

    def redacted_spans(self):
        """``(start, end)`` of every field the dialect marks as a credential."""
        return [(field.offset, field.end)
                for field in list(self.header.fields) + list(self.fields)
                if getattr(field, "redact", False)]

    def __repr__(self):
        return "Layout(%s, %r, %d bytes)" % (self.msg_type, self.name, self.size)


class NnfDictionary(object):
    """Every structure of one segment, by transaction code.

    Named a dictionary because that is what it is to a codec -- the counterpart
    of :class:`exchangesim.fix.dictionary.Dictionary`, which validates the
    message this one produces. The two are separate on purpose: this says what
    the bytes mean, that says whether the values are allowed.
    """

    def __init__(self, header):
        self.header = header
        self._layouts = {}

    def add(self, layout):
        if layout.msg_type in self._layouts:
            raise ValueError("transaction code %s is defined twice"
                             % layout.msg_type)
        self._layouts[layout.msg_type] = layout
        return layout

    def define(self, code, name, size, fields):
        """Build and register one structure. ``code`` is the transaction code."""
        return self.add(Layout(str(code), name, size, fields, self.header))

    def layout(self, msg_type):
        return self._layouts.get(str(msg_type))

    def layout_for(self, message):
        return self.layout(message.msg_type)

    def transaction_code(self, raw):
        return self.header.transaction_code(raw)

    @property
    def layouts(self):
        return sorted(self._layouts.values(), key=lambda l: int(l.msg_type))

    def __contains__(self, msg_type):
        return str(msg_type) in self._layouts

    def __len__(self):
        return len(self._layouts)
