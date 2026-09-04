"""Requests the engine accepts.

Protocol gateways build these from wire messages. They carry only what the core
needs, in core vocabulary -- no FIX tags -- which is what keeps a second
protocol from having to touch the engine.
"""

from .enums import OrderType, TimeInForce


class NewOrderRequest(object):
    """A request to enter an order."""

    __slots__ = ("session_key", "market", "cl_ord_id", "symbol", "side",
                 "quantity", "price", "order_type", "time_in_force", "min_qty",
                 "exec_inst", "capacity", "account", "mpid", "cash_margin",
                 "margin_type", "stp_id", "stp_instruction", "received_at")

    def __init__(self, session_key, market, cl_ord_id, symbol, side, quantity,
                 price=None, order_type=OrderType.LIMIT,
                 time_in_force=TimeInForce.DAY, min_qty=0, exec_inst=(),
                 capacity=None, account=None, mpid=None, cash_margin=None,
                 margin_type=None, stp_id=None, stp_instruction=None,
                 received_at=None):
        self.session_key = session_key
        self.market = market
        self.cl_ord_id = cl_ord_id
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        #: Integer price units, already parsed by the gateway.
        self.price = price
        self.order_type = order_type
        self.time_in_force = time_in_force
        self.min_qty = min_qty
        self.exec_inst = tuple(exec_inst)
        self.capacity = capacity
        self.account = account
        self.mpid = mpid
        self.cash_margin = cash_margin
        self.margin_type = margin_type
        #: Client-chosen self-match key and the mode it asks for, where the
        #: venue puts them on the order (HKEX) rather than on the market.
        self.stp_id = stp_id
        self.stp_instruction = stp_instruction
        self.received_at = received_at

    def __repr__(self):
        return "NewOrderRequest(%s %s %s %s@%s)" % (
            self.cl_ord_id, self.side, self.symbol, self.quantity, self.price)


class CancelRequest(object):
    """A request to cancel an existing order."""

    __slots__ = ("session_key", "market", "cl_ord_id", "orig_cl_ord_id",
                 "symbol", "side", "quantity", "received_at", "order_id")

    def __init__(self, session_key, market, cl_ord_id, orig_cl_ord_id,
                 symbol=None, side=None, quantity=None, received_at=None,
                 order_id=None):
        self.session_key = session_key
        self.market = market
        self.cl_ord_id = cl_ord_id
        #: The order's *current* ClOrdID, not the original one.
        self.orig_cl_ord_id = orig_cl_ord_id
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.received_at = received_at
        #: The venue's own OrderID, for a protocol whose cancel and amend name
        #: the order that way rather than by a client identifier. Not every
        #: exchange gives the client a handle of its own: NSE's NNF has none at
        #: all, and cancel-by-OrderID is FIX's own alternative. Used only when
        #: ``orig_cl_ord_id`` is None, so nothing that names an order the usual
        #: way changes behaviour.
        self.order_id = order_id

    def __repr__(self):
        return "CancelRequest(%s -> %s)" % (self.orig_cl_ord_id, self.cl_ord_id)


class ReplaceRequest(object):
    """A request to amend price and/or quantity."""

    __slots__ = ("session_key", "market", "cl_ord_id", "orig_cl_ord_id",
                 "symbol", "side", "quantity", "price", "time_in_force",
                 "min_qty", "exec_inst", "capacity", "received_at",
                 "order_id")

    def __init__(self, session_key, market, cl_ord_id, orig_cl_ord_id,
                 symbol=None, side=None, quantity=None, price=None,
                 time_in_force=None, min_qty=None, exec_inst=(), capacity=None,
                 received_at=None, order_id=None):
        self.session_key = session_key
        self.market = market
        self.cl_ord_id = cl_ord_id
        self.orig_cl_ord_id = orig_cl_ord_id
        self.symbol = symbol
        self.side = side
        #: New total intended quantity, including what has already executed.
        self.quantity = quantity
        self.price = price
        self.time_in_force = time_in_force
        self.min_qty = min_qty
        self.exec_inst = tuple(exec_inst)
        self.capacity = capacity
        self.received_at = received_at
        #: The venue's own OrderID, for a protocol whose amend and cancel name
        #: the order that way. See CancelRequest.order_id.
        self.order_id = order_id

    def __repr__(self):
        return "ReplaceRequest(%s -> %s, %s@%s)" % (
            self.orig_cl_ord_id, self.cl_ord_id, self.quantity, self.price)
