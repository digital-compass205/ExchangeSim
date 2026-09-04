"""OCG-C's own value translations, over the shared converter vocabulary.

Most of what this module used to hold was not about OCG-C at all -- an
enumeration whose two encodings disagree, a ``Y``/``N`` held as 0/1, an integer
held as an integer -- so it moved to :mod:`exchangesim.wire.values` when a third
protocol needed the same things. What stays here is the part that is genuinely
this encoding's: the Decimal wire type of section 6.1, with its eight implied
decimal places, and the quantity and price fields carried in it.

Everything shared is re-exported, so nothing that imports this module has to
know that it moved.
"""

from ..wire.values import (          # noqa: F401  (re-exported)
    CHARACTER,
    Character,
    Converter,
    Enum,
    FLAG,
    Flag,
    MultiEnum,
    NUMBER,
    Number,
    TEXT,
    Text,
)
from ..wire import values as _shared

#: Implied decimal places of the Decimal wire type, section 6.1.
DECIMAL_PLACES = 8


def scale(text, places=DECIMAL_PLACES):
    """A decimal string to a scaled integer, without ever touching a float."""
    return _shared.scale(text, places)


def unscale(value, places=DECIMAL_PLACES):
    """A scaled integer back to the shortest decimal string that means it."""
    return _shared.unscale(value, places)


class Quantity(Converter):
    """A FIX quantity or price field held as the Decimal wire type."""

    def to_wire(self, text):
        return scale(text)

    def from_wire(self, value):
        return unscale(value)


#: Shared instance; converters hold no per-message state.
QUANTITY = Quantity()
