"""Exact price arithmetic.

Prices are held as integers in the venue's smallest price increment -- for
Japannext equities, tenths of a yen, since Price(44) and LastPx(31) allow one
decimal place. Nothing in matching ever touches a float: at a tick size of 0.1
a binary float cannot represent the value exactly, and an order book that
compares prices approximately is a book that matches wrongly.

:class:`Decimal` is used only at the boundary, where text becomes an integer
and back.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

#: Decimal places used when rendering an average price (FIX AvgPx allows 4).
AVG_PX_DECIMALS = 4


class PriceError(ValueError):
    """A price string that cannot be represented in the venue's increments."""


class PriceCodec(object):
    """Converts between decimal price text and integer price units.

    ``decimals`` is the number of decimal places the venue's prices carry, so
    one price unit is ``10 ** -decimals`` of the quote currency.
    """

    __slots__ = ("decimals", "scale", "_quantum")

    def __init__(self, decimals=1):
        self.decimals = decimals
        self.scale = 10 ** decimals
        self._quantum = Decimal(1).scaleb(-decimals)

    # -- text <-> units ----------------------------------------------------

    def parse(self, text):
        # type: (str) -> int
        """Convert price text to integer units, exactly.

        Raises :class:`PriceError` if the value carries more precision than the
        venue supports -- silently rounding a client's price would make the
        simulator disagree with the real exchange, which rejects it.
        """
        try:
            value = Decimal(str(text).strip())
        except (InvalidOperation, ValueError, ArithmeticError):
            raise PriceError("'%s' is not a number" % (text,))

        if not value.is_finite():
            raise PriceError("'%s' is not a finite number" % (text,))

        scaled = value * self.scale
        if scaled != scaled.to_integral_value():
            raise PriceError("'%s' has more than %d decimal place%s"
                             % (text, self.decimals,
                                "" if self.decimals == 1 else "s"))
        return int(scaled)

    def format(self, units):
        # type: (int) -> str
        """Render integer units as price text with the venue's precision."""
        value = (Decimal(int(units)) / self.scale).quantize(self._quantum)
        return format(value, "f")

    def to_decimal(self, units):
        return (Decimal(int(units)) / self.scale).quantize(self._quantum)

    # -- derived values ----------------------------------------------------

    def format_average(self, notional_units, quantity, decimals=AVG_PX_DECIMALS):
        """Render an average price from accumulated notional and quantity.

        ``notional_units`` is the sum of ``price_units * quantity`` over fills.
        An unfilled order averages zero, which is what the specification's
        Order Accepted report carries.
        """
        if not quantity:
            return format(Decimal(0).quantize(Decimal(1).scaleb(-decimals)), "f")
        average = (Decimal(int(notional_units)) / (self.scale * int(quantity)))
        return format(average.quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP),
                      "f")

    def notional(self, units, quantity):
        """Order value in whole currency units, rounded up to be conservative.

        Used for value-limit checks, where accepting an order that is
        fractionally over the limit would be wrong.
        """
        return (int(units) * int(quantity) + self.scale - 1) // self.scale

    def exact_notional(self, units, quantity):
        """Order value as an exact :class:`Decimal`."""
        return Decimal(int(units) * int(quantity)) / self.scale

    def is_multiple_of(self, units, tick_units):
        """True when a price sits exactly on a tick boundary."""
        if not tick_units:
            return True
        return int(units) % int(tick_units) == 0
