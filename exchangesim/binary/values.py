"""Translating one field's value between its FIX spelling and its binary one.

The two encodings of OCG-C carry the same fields, but not always as the same
value. ``Side`` is ``1``/``2``/``5`` in both; ``OrderCapacity`` is ``A``/``P``
in FIX and 1/2 in binary; ``OrdStatus`` Expired is ``C`` in FIX and 12 in
binary; a quantity is the text ``1000`` in FIX and the integer 100000000000 in
binary. A converter per field keeps those differences in one table instead of
scattered through the codec.

**An unrecognised inbound value passes through as text rather than being
rejected here.** A client that sends ``Side = 3`` should get the venue's own
Reject naming the field, which is what happens when the value reaches the
dictionary as the string ``3``; failing in the codec instead would drop the
session for what the specification treats as an ordinary validation error.
"""

#: Implied decimal places of the Decimal wire type, section 6.1.
DECIMAL_PLACES = 8


def scale(text, places=DECIMAL_PLACES):
    """A decimal string to a scaled integer, without ever touching a float.

    ``10.05`` at eight implied places is 1005000000. Parsing digits rather than
    multiplying a float is the same reason ``core/prices.py`` exists: 0.1 has no
    exact binary representation, and a venue that rejects an off-tick price would
    disagree with a simulator that rounded one.
    """
    text = str(text).strip()
    negative = text.startswith("-")
    body = text[1:] if text[:1] in ("+", "-") else text
    whole, _, fraction = body.partition(".")
    if not (whole + fraction).isdigit():
        raise ValueError("not a decimal value: %r" % text)
    if len(fraction) > places:
        raise ValueError("%r has more than %d decimal places" % (text, places))
    value = int((whole or "0") + fraction.ljust(places, "0"))
    return -value if negative else value


def unscale(value, places=DECIMAL_PLACES):
    """A scaled integer back to the shortest decimal string that means it."""
    value = int(value)
    negative = value < 0
    whole, fraction = divmod(abs(value), 10 ** places)
    text = str(whole)
    digits = ("%0*d" % (places, fraction)).rstrip("0")
    if digits:
        text += "." + digits
    return "-" + text if negative else text


class Converter(object):
    """Between the string a FIX field carries and the binary wire value."""

    def to_wire(self, text):
        raise NotImplementedError

    def from_wire(self, value):
        # type: (object) -> str
        raise NotImplementedError


class Text(Converter):
    """Identity: an Alphanumeric field whose FIX value is the same string."""

    def to_wire(self, text):
        return text

    def from_wire(self, value):
        return value


class Number(Converter):
    """A FIX integer field held as one of the unsigned wire types."""

    def to_wire(self, text):
        return int(text)

    def from_wire(self, value):
        return str(value)


class Character(Converter):
    """A FIX ``char`` field held as the Byte wire type."""

    def to_wire(self, text):
        if len(text) != 1:
            raise ValueError("expected one character, got %r" % text)
        return ord(text)

    def from_wire(self, value):
        return chr(value)


class Quantity(Converter):
    """A FIX quantity or price field held as the Decimal wire type."""

    def to_wire(self, text):
        return scale(text)

    def from_wire(self, value):
        return unscale(value)


class Flag(Converter):
    """A FIX ``Y``/``N`` boolean held as a UInt8 0/1."""

    def to_wire(self, text):
        return 1 if text == "Y" else 0

    def from_wire(self, value):
        return "Y" if value else "N"


class Enum(Converter):
    """An enumeration whose two encodings disagree, given as ``{fix: binary}``.

    Outbound, an unmapped value is a programming error and raises. Inbound it is
    a client error, so the raw value is handed on as text for the dictionary to
    refuse with the reject reason the specification names.
    """

    def __init__(self, mapping):
        self.forward = dict(mapping)
        self.reverse = {}
        for fix_value, wire_value in mapping.items():
            self.reverse.setdefault(wire_value, fix_value)

    def to_wire(self, text):
        try:
            return self.forward[text]
        except KeyError:
            raise ValueError("no binary encoding for value %r" % text)

    def from_wire(self, value):
        mapped = self.reverse.get(value)
        return str(value) if mapped is None else mapped


class MultiEnum(Converter):
    """A space-separated multiple-value field whose two encodings disagree.

    ``ExecInst`` is the case: FIX spells the two instructions ``c`` and ``x``,
    binary spells them ``0`` and ``1``, and either encoding may carry both at
    once. An unmapped part is passed through for the dictionary to refuse, for
    the reason given on :class:`Enum`.
    """

    def __init__(self, mapping):
        self.forward = dict(mapping)
        self.reverse = {}
        for fix_value, wire_value in mapping.items():
            self.reverse.setdefault(wire_value, fix_value)

    def _translate(self, text, table):
        parts = [table.get(part, part) for part in str(text).split(" ") if part]
        return " ".join(parts)

    def to_wire(self, text):
        return self._translate(text, self.forward)

    def from_wire(self, value):
        return self._translate(value, self.reverse)


#: Shared instances; converters hold no per-message state.
TEXT = Text()
NUMBER = Number()
CHARACTER = Character()
QUANTITY = Quantity()
FLAG = Flag()
