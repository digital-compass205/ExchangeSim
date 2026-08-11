"""Orders and the ClOrdID chain.

The chain deserves explanation. The Japannext specification says
``OrigClOrdID(41)`` carries "ClOrdID (11) of previous order (**not** initial
order)" -- so after an order is amended twice, only the most recent ClOrdID is
a valid handle on it. :class:`OrderRegistry` enforces exactly that, while still
remembering every identifier the session has ever used so a genuine duplicate
can be rejected.
"""

from .enums import OrderStatus, Side, TimeInForce


class Order(object):
    """A single order and its running execution state."""

    __slots__ = (
        "order_id", "cl_ord_id", "orig_cl_ord_id", "session_key", "market",
        "symbol", "side", "order_type", "time_in_force", "price", "quantity",
        "min_qty", "exec_inst", "capacity", "account", "mpid", "cash_margin",
        "margin_type", "stp_id", "stp_instruction", "cum_qty", "notional_units",
        "status", "sequence", "created_at", "updated_at", "chain", "text",
    )

    def __init__(self, order_id, cl_ord_id, session_key, symbol, side, quantity,
                 price=None, market=None, order_type=None,
                 time_in_force=TimeInForce.DAY, min_qty=0, exec_inst=None,
                 capacity=None, account=None, mpid=None, cash_margin=None,
                 margin_type=None, stp_id=None, stp_instruction=None,
                 sequence=0, created_at=None):
        self.order_id = order_id
        self.cl_ord_id = cl_ord_id
        self.orig_cl_ord_id = None
        self.session_key = session_key
        self.market = market
        self.symbol = symbol
        self.side = side
        self.order_type = order_type
        self.time_in_force = time_in_force
        #: Limit price in integer price units (see :mod:`exchangesim.core.prices`).
        self.price = price
        self.quantity = quantity
        self.min_qty = min_qty or 0
        self.exec_inst = frozenset(exec_inst or ())
        self.capacity = capacity
        self.account = account
        #: Market Participant Identifier, the self-trade prevention key where a
        #: venue keys on the firm (Japannext).
        self.mpid = mpid
        self.cash_margin = cash_margin
        self.margin_type = margin_type
        #: Explicit self-match key carried by the order itself, where a venue
        #: lets the client choose it (HKEX SelfMatchPreventionID). When set it
        #: takes precedence over ``mpid``, and matches across firms.
        self.stp_id = stp_id
        #: The :class:`~.enums.StpMode` this order's key asks for, overriding
        #: the market default. HKEX attaches the instruction to the ID.
        self.stp_instruction = stp_instruction

        self.cum_qty = 0
        #: Sum of price_units * quantity over fills, for exact average pricing.
        self.notional_units = 0
        self.status = OrderStatus.NEW
        #: Book arrival order, the tie-break for equal prices.
        self.sequence = sequence
        self.created_at = created_at
        self.updated_at = created_at
        #: Every ClOrdID this order has carried, oldest first.
        self.chain = [cl_ord_id]
        self.text = None

    # -- derived state -----------------------------------------------------

    @property
    def leaves_qty(self):
        """Quantity still open for execution."""
        if self.status in OrderStatus.TERMINAL:
            return 0
        return max(0, self.quantity - self.cum_qty)

    @property
    def is_buy(self):
        return self.side == Side.BUY

    @property
    def is_live(self):
        return self.status in OrderStatus.LIVE and self.leaves_qty > 0

    @property
    def is_filled(self):
        return self.cum_qty >= self.quantity

    @property
    def is_immediate(self):
        """True for time-in-force values that must not rest on the book."""
        return self.time_in_force in TimeInForce.IMMEDIATE

    def average_price_units(self):
        """Average fill price in price units, or 0 when unfilled."""
        if not self.cum_qty:
            return 0
        return self.notional_units // self.cum_qty

    # -- mutation ----------------------------------------------------------

    def apply_fill(self, quantity, price_units, when=None):
        """Record an execution against this order."""
        if quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if quantity > self.leaves_qty:
            raise ValueError("fill of %d exceeds leaves %d for %s"
                             % (quantity, self.leaves_qty, self.order_id))

        self.cum_qty += quantity
        self.notional_units += price_units * quantity
        self.status = (OrderStatus.FILLED if self.is_filled
                       else OrderStatus.PARTIALLY_FILLED)
        self.updated_at = when or self.updated_at
        return self

    def reduce_quantity(self, by, when=None):
        """Shrink the order, as self-trade prevention Decrement mode does.

        The order is left cancelled if nothing remains open.
        """
        self.quantity = max(self.cum_qty, self.quantity - by)
        self.updated_at = when or self.updated_at
        if self.leaves_qty == 0 and self.status not in OrderStatus.TERMINAL:
            self.status = (OrderStatus.FILLED if self.cum_qty
                           else OrderStatus.CANCELLED)
        return self

    def cancel(self, when=None):
        if self.status not in OrderStatus.TERMINAL:
            self.status = OrderStatus.CANCELLED
            self.updated_at = when or self.updated_at
        return self

    def rename(self, cl_ord_id, when=None):
        """Advance the ClOrdID chain, as an accepted replace does."""
        self.orig_cl_ord_id = self.cl_ord_id
        self.cl_ord_id = cl_ord_id
        self.chain.append(cl_ord_id)
        self.updated_at = when or self.updated_at
        return self

    # -- presentation ------------------------------------------------------

    def __repr__(self):
        return ("Order(%s %s %s %d@%s %s/%s cum=%d)"
                % (self.order_id, self.side, self.symbol, self.quantity,
                   self.price, self.status, self.time_in_force, self.cum_qty))

    def describe(self, codec=None):
        """A JSON-friendly summary for the control plane."""
        price = codec.format(self.price) if (codec and self.price is not None) \
            else self.price
        average = codec.format_average(self.notional_units, self.cum_qty) \
            if codec else self.average_price_units()
        return {
            "order_id": self.order_id,
            "cl_ord_id": self.cl_ord_id,
            "orig_cl_ord_id": self.orig_cl_ord_id,
            "symbol": self.symbol,
            "market": self.market,
            "side": self.side,
            "price": price,
            "quantity": self.quantity,
            "cum_qty": self.cum_qty,
            "leaves_qty": self.leaves_qty,
            "avg_px": average,
            "status": self.status,
            "time_in_force": self.time_in_force,
            "min_qty": self.min_qty or None,
            "exec_inst": sorted(self.exec_inst) or None,
            "account": self.account,
            "mpid": self.mpid,
            "session": "%s" % (self.session_key,) if self.session_key else None,
        }


class DuplicateClOrdID(Exception):
    """A ClOrdID the session has used before."""


class OrderRegistry(object):
    """Tracks every order, by venue OrderID and by the session's ClOrdID chain."""

    def __init__(self):
        self._by_order_id = {}
        #: (session_key, cl_ord_id) -> Order, for every identifier ever seen.
        self._by_cl_ord_id = {}

    # -- registration ------------------------------------------------------

    def is_duplicate(self, session_key, cl_ord_id):
        """True if this session has already used this ClOrdID for anything."""
        return (session_key, cl_ord_id) in self._by_cl_ord_id

    def add(self, order):
        key = (order.session_key, order.cl_ord_id)
        if key in self._by_cl_ord_id:
            raise DuplicateClOrdID(order.cl_ord_id)
        self._by_order_id[order.order_id] = order
        self._by_cl_ord_id[key] = order
        return order

    def rename(self, order, cl_ord_id):
        """Record a new ClOrdID for an existing order, keeping the old one bound."""
        key = (order.session_key, cl_ord_id)
        if key in self._by_cl_ord_id:
            raise DuplicateClOrdID(cl_ord_id)
        order.rename(cl_ord_id)
        self._by_cl_ord_id[key] = order
        return order

    # -- lookup ------------------------------------------------------------

    def by_order_id(self, order_id):
        return self._by_order_id.get(order_id)

    def by_cl_ord_id(self, session_key, cl_ord_id):
        """Any order that has ever carried this ClOrdID for this session."""
        return self._by_cl_ord_id.get((session_key, cl_ord_id))

    def resolve(self, session_key, orig_cl_ord_id):
        """Find the order a cancel or replace refers to.

        Returns None unless ``orig_cl_ord_id`` is the order's *current*
        identifier -- referencing a superseded one is an unknown order, which
        is what the specification's "not initial order" wording requires.
        """
        order = self._by_cl_ord_id.get((session_key, orig_cl_ord_id))
        if order is None or order.cl_ord_id != orig_cl_ord_id:
            return None
        return order

    # -- enumeration -------------------------------------------------------

    def all(self):
        return list(self._by_order_id.values())

    def live(self):
        return [order for order in self._by_order_id.values() if order.is_live]

    def for_session(self, session_key, live_only=True):
        return [order for order in self._by_order_id.values()
                if order.session_key == session_key
                and (order.is_live or not live_only)]

    def __len__(self):
        return len(self._by_order_id)
