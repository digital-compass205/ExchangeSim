"""Instruments and the reference data that constrains their orders.

Price bands and tick sizes are table-driven because that is how exchanges
publish them -- the Japannext appendices are literally tables of base-price
thresholds against permitted ranges, differing between J-Market, X-Market and
U-Market and, for ticks, between TOPIX tiers. Encoding them as data rather than
code means a table update is a CSV edit.

All prices here are integer price units (see :mod:`exchangesim.core.prices`).
"""

import bisect
import csv
import logging
import os

log = logging.getLogger(__name__)


class ReferenceDataError(Exception):
    """Malformed or missing reference data."""


class BandTable(object):
    """Base price -> permitted daily price range, as a threshold ladder.

    Rows are ``(lower_bound_inclusive, band)``. A base price falls in the row
    with the greatest lower bound not exceeding it, which is exactly how the
    published "equal to or greater than / less than" table reads.
    """

    __slots__ = ("_bounds", "_bands", "name")

    def __init__(self, rows, name="band"):
        ordered = sorted(rows, key=lambda row: row[0])
        self._bounds = [row[0] for row in ordered]
        self._bands = [row[1] for row in ordered]
        self.name = name
        if not ordered:
            raise ReferenceDataError("%s table is empty" % name)
        if self._bounds[0] != 0:
            raise ReferenceDataError(
                "%s table must start at a lower bound of 0" % name)

    def band_for(self, base_price):
        index = bisect.bisect_right(self._bounds, int(base_price)) - 1
        if index < 0:
            index = 0
        return self._bands[index]

    def limits_for(self, base_price):
        """The (low, high) price limits around a base price.

        The lower limit is floored at one price unit: a limit of zero or below
        is not a tradeable price.
        """
        band = self.band_for(base_price)
        return max(1, int(base_price) - band), int(base_price) + band

    def __len__(self):
        return len(self._bounds)


class TickTable(object):
    """Price -> tick size, optionally varying by instrument tier.

    Rows are ``(lower_bound_inclusive, {tier: tick})``. A tier of ``None`` in a
    row acts as the fallback for tiers not named explicitly.
    """

    DEFAULT_TIER = None

    __slots__ = ("_bounds", "_ticks", "name")

    def __init__(self, rows, name="tick"):
        ordered = sorted(rows, key=lambda row: row[0])
        self._bounds = [row[0] for row in ordered]
        self._ticks = [row[1] for row in ordered]
        self.name = name
        if not ordered:
            raise ReferenceDataError("%s table is empty" % name)

    def tick_for(self, price, tier=None):
        index = bisect.bisect_right(self._bounds, int(price)) - 1
        if index < 0:
            index = 0
        row = self._ticks[index]
        if tier in row:
            return row[tier]
        if self.DEFAULT_TIER in row:
            return row[self.DEFAULT_TIER]
        raise ReferenceDataError(
            "%s table has no tick for tier %r at price %s" % (self.name, tier, price))

    def __len__(self):
        return len(self._bounds)


class Instrument(object):
    """One tradeable security and its per-day parameters."""

    __slots__ = ("symbol", "name", "lot_size", "shares_outstanding",
                 "base_price", "tier", "band_table", "tick_table",
                 "band_override", "tradable", "signed_price")

    def __init__(self, symbol, name="", lot_size=100, shares_outstanding=0,
                 base_price=None, tier=None, band_table=None, tick_table=None,
                 tradable=True, signed_price=False):
        self.symbol = symbol
        self.name = name
        self.lot_size = lot_size or 1
        self.shares_outstanding = shares_outstanding or 0
        #: Reference price the daily price band is calculated around.
        self.base_price = base_price
        #: Tier used for tick-size lookup, e.g. TOPIX100.
        self.tier = tier
        self.band_table = band_table
        self.tick_table = tick_table
        #: Explicit (low, high) limits, overriding the table when set.
        self.band_override = None
        self.tradable = tradable
        #: Whether this instrument's price is a *difference* rather than a
        #: level, and so may be zero or negative. A spread combination is
        #: quoted at the difference between its two legs, which is routinely
        #: small and can be either sign -- so the ordinary "a price must be
        #: positive" rule does not apply to it. Not a venue's quirk: every
        #: exchange with a combination book has instruments of this shape.
        self.signed_price = bool(signed_price)

    # -- derived limits ----------------------------------------------------

    @property
    def price_limits(self):
        """The (low, high) permitted limit prices, or None when unconstrained.

        A venue may halt an instrument precisely because no base price exists
        -- an IPO before its first print -- so this legitimately returns None.
        """
        if self.band_override is not None:
            return self.band_override
        if self.base_price is None or self.band_table is None:
            return None
        return self.band_table.limits_for(self.base_price)

    def is_within_band(self, price):
        limits = self.price_limits
        if limits is None:
            return True
        return limits[0] <= price <= limits[1]

    def tick_size(self, price):
        if self.tick_table is None:
            return None
        return self.tick_table.tick_for(price, self.tier)

    def is_on_tick(self, price):
        tick = self.tick_size(price)
        if not tick:
            return True
        return int(price) % int(tick) == 0

    def is_round_lot(self, quantity):
        return int(quantity) % self.lot_size == 0

    @property
    def max_quantity(self):
        """5% of shares outstanding, per the Japannext order restrictions."""
        if not self.shares_outstanding:
            return None
        return self.shares_outstanding // 20

    # -- presentation ------------------------------------------------------

    def describe(self, codec=None):
        limits = self.price_limits
        fmt = codec.format if codec else (lambda value: value)
        return {
            "symbol": self.symbol,
            "name": self.name,
            "lot_size": self.lot_size,
            "tier": self.tier,
            "base_price": fmt(self.base_price) if self.base_price is not None else None,
            "price_low": fmt(limits[0]) if limits else None,
            "price_high": fmt(limits[1]) if limits else None,
            "tick_size": (fmt(self.tick_size(self.base_price))
                          if self.base_price is not None and self.tick_table
                          else None),
            "shares_outstanding": self.shares_outstanding or None,
            "max_quantity": self.max_quantity,
            "tradable": self.tradable,
        }

    def __repr__(self):
        return "Instrument(%s, lot=%d, base=%s)" % (
            self.symbol, self.lot_size, self.base_price)


def symbol_key(symbol):
    """Sort key that orders numeric symbols as numbers, others as text.

    A stock code is a number at both venues, but only HKEX's are of differing
    width now that they are carried unpadded -- and plain string order puts
    ``1299`` before ``179`` and ``28`` after ``2318``. Sorting the digits as an
    integer is the order a person reads a listing table in. Non-numeric codes
    keep text order and sort after the numbers, so a mixed universe is still
    stable rather than raising.
    """
    text = str(symbol)
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text)


# -- CSV loading -------------------------------------------------------------

def load_band_table(path, codec, name="band"):
    """Load ``lower_bound,band`` rows, both as decimal price text."""
    rows = []
    for record in _read_csv(path, ("lower_bound", "band")):
        rows.append((codec.parse(record["lower_bound"]),
                     codec.parse(record["band"])))
    log.debug("loaded %d %s rows from %s", len(rows), name, path)
    return BandTable(rows, name)


def load_tick_table(path, codec, name="tick"):
    """Load ``lower_bound,tier,tick`` rows; an empty tier is the fallback."""
    grouped = {}
    for record in _read_csv(path, ("lower_bound", "tier", "tick")):
        bound = codec.parse(record["lower_bound"])
        tier = (record["tier"] or "").strip() or TickTable.DEFAULT_TIER
        grouped.setdefault(bound, {})[tier] = codec.parse(record["tick"])
    rows = sorted(grouped.items())
    log.debug("loaded %d %s rows from %s", len(rows), name, path)
    return TickTable(rows, name)


def load_instruments(path, codec, band_table=None, tick_table=None):
    """Load the symbol universe from CSV, keyed by symbol."""
    instruments = {}
    for record in _read_csv(path, ("symbol",)):
        symbol = record["symbol"].strip()
        if not symbol:
            continue
        base = record.get("base_price", "").strip()
        instruments[symbol] = Instrument(
            symbol=symbol,
            name=record.get("name", "").strip(),
            lot_size=_int(record.get("lot_size"), 100),
            shares_outstanding=_int(record.get("shares_outstanding"), 0),
            base_price=codec.parse(base) if base else None,
            tier=(record.get("tier", "").strip() or None),
            band_table=band_table,
            tick_table=tick_table,
            tradable=_bool(record.get("tradable"), True),
        )
    log.info("loaded %d instruments from %s", len(instruments), path)
    return instruments


def read_rows(path, required=()):
    """Every row of a reference CSV as a dict, with ``#`` comments stripped.

    Public because a venue's reference data may carry columns the core has no
    opinion about -- HKEX classifies each security into a market segment -- and
    reading those should not mean reimplementing the comment and header handling.
    """
    return _read_csv(path, required)


def _read_csv(path, required):
    if not os.path.isfile(path):
        raise ReferenceDataError("reference data file not found: %s" % path)
    try:
        with open(path, "r") as handle:
            reader = csv.DictReader(_uncommented(handle))
            if reader.fieldnames is None:
                raise ReferenceDataError("%s has no header row" % path)
            missing = [column for column in required
                       if column not in reader.fieldnames]
            if missing:
                raise ReferenceDataError(
                    "%s is missing column(s): %s" % (path, ", ".join(missing)))
            return list(reader)
    except (IOError, OSError) as exc:
        raise ReferenceDataError("cannot read %s: %s" % (path, exc))


def _uncommented(lines):
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield line


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool(value, default):
    text = str(value).strip().lower() if value is not None else ""
    if text in ("y", "yes", "true", "1"):
        return True
    if text in ("n", "no", "false", "0"):
        return False
    return default
