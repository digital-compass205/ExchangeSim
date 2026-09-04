"""The venue engine: order lifecycle across markets.

Owns the order registry, identifier generation and validation, and routes
requests to the right :class:`~exchangesim.core.market.Market`. Everything it
returns is a core event; nothing here knows about FIX.
"""

import logging

from .behaviour import REJECT, BehaviourStore
from .enums import (
    CancelRejectReason,
    CancelReason,
    OrderStatus,
    RejectReason,
)
from .events import CancelRejected, OrderRejected, OrderReplaced
from .orders import DuplicateClOrdID, Order, OrderRegistry

log = logging.getLogger(__name__)


class Engine(object):
    """Order entry, amendment and cancellation for one venue."""

    def __init__(self, codec, instruments, markets, validator, order_ids,
                 sequences, clock=None, registry=None, behaviour=None):
        self.codec = codec
        #: symbol -> Instrument
        self.instruments = instruments
        #: market name -> Market
        self.markets = markets
        self.validator = validator
        self.order_ids = order_ids
        self.sequences = sequences
        self.clock = clock
        self.registry = registry or OrderRegistry()
        #: Injected test behaviour; see :mod:`exchangesim.core.behaviour`.
        self.behaviour = behaviour or BehaviourStore()

    # -- lookup ------------------------------------------------------------

    def market(self, name):
        return self.markets.get(name)

    def instrument(self, symbol):
        return self.instruments.get(symbol)

    # -- new orders --------------------------------------------------------

    def new_order(self, request):
        """Validate and enter an order, returning the events to report."""
        order = self._build_order(request)

        if self.registry.is_duplicate(request.session_key, request.cl_ord_id):
            order.status = OrderStatus.REJECTED
            return [OrderRejected(order, RejectReason.DUPLICATE_ORDER,
                                  "ClOrdID '%s' has already been used"
                                  % request.cl_ord_id)]

        market = self.market(request.market)
        if market is None:
            order.status = OrderStatus.REJECTED
            return [OrderRejected(order, RejectReason.OTHER,
                                  "unknown market '%s'" % request.market)]

        # An injected rejection takes precedence over validation: the point is
        # to make a client see a reject it could not otherwise provoke.
        forced = self.behaviour.take(REJECT, order)
        if forced is not None:
            order.status = OrderStatus.REJECTED
            return [OrderRejected(order, forced.reason or RejectReason.OTHER,
                                  forced.text or "rejected by injected behaviour")]

        instrument = self.instrument(request.symbol)
        rejection = self.validator.validate_new(
            order, instrument, market.state, market.book(request.symbol))
        if rejection is not None:
            order.status = OrderStatus.REJECTED
            return [OrderRejected(order, rejection.reason, rejection.text)]

        try:
            self.registry.add(order)
        except DuplicateClOrdID:
            order.status = OrderStatus.REJECTED
            return [OrderRejected(order, RejectReason.DUPLICATE_ORDER,
                                  "ClOrdID '%s' has already been used"
                                  % request.cl_ord_id)]

        return market.submit(order)

    def _build_order(self, request):
        return Order(
            order_id=self.order_ids.next(),
            cl_ord_id=request.cl_ord_id,
            session_key=request.session_key,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            market=request.market,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            min_qty=request.min_qty,
            exec_inst=request.exec_inst,
            capacity=request.capacity,
            account=request.account,
            mpid=request.mpid,
            cash_margin=request.cash_margin,
            margin_type=request.margin_type,
            stp_id=request.stp_id,
            stp_instruction=request.stp_instruction,
            sequence=self.sequences.next(),
            created_at=self._now(),
        )

    # -- cancel ------------------------------------------------------------

    def cancel_order(self, request):
        order, rejection = self._resolve(request, is_replace=False)
        if rejection is not None:
            return [rejection]

        market = self.market(order.market)
        if market is None:
            return [CancelRejected(request.cl_ord_id, request.orig_cl_ord_id,
                                   CancelRejectReason.OTHER,
                                   "unknown market '%s'" % order.market,
                                   order)]

        self.registry.rename(order, request.cl_ord_id)
        return market.cancel(order, CancelReason.USER_REQUEST,
                             "cancelled at client request")

    # -- replace -----------------------------------------------------------

    def replace_order(self, request):
        order, rejection = self._resolve(request, is_replace=True)
        if rejection is not None:
            return [rejection]

        market = self.market(order.market)
        instrument = self.instrument(order.symbol)

        quantity = request.quantity if request.quantity is not None else order.quantity
        price = request.price if request.price is not None else order.price

        validation = self.validator.validate_amend(
            order, instrument, market.state if market else None, quantity, price)
        if validation is not None:
            return [CancelRejected(
                request.cl_ord_id, request.orig_cl_ord_id,
                _cancel_reason_for(validation.reason), validation.text,
                order, is_replace=True)]

        previous_cl_ord_id = order.cl_ord_id
        self.registry.rename(order, request.cl_ord_id)

        if request.time_in_force is not None:
            order.time_in_force = request.time_in_force
        if request.min_qty is not None:
            order.min_qty = request.min_qty
        if request.exec_inst:
            order.exec_inst = frozenset(request.exec_inst)

        # The matching engine sets the resulting status itself -- an amend that
        # lifts an order through the offer may fill it completely -- so nothing
        # here may overwrite it afterwards.
        kept_priority, events = market.amend(order, quantity, price)

        return [OrderReplaced(order, previous_cl_ord_id, kept_priority)] + events

    # -- shared resolution -------------------------------------------------

    def _named_order(self, request):
        """The order a request names, or ``(None, why not)``.

        Two ways to name one. A client identifier, which is how FIX does it and
        the only way either of the first two venues here can. Or the venue's own
        ``OrderID``, which is how a protocol with no client handle at all has to
        do it -- NSE's NNF names an order by the ``OrderNumber`` the exchange
        assigned, and gives the client nothing else to hold on to.

        The second path needs an ownership check that the first gets for free:
        the ClOrdID index is keyed by session, so a client can only ever resolve
        its own orders through it, while ``by_order_id`` is a single global map.
        Without the check below, one client could cancel another's order by
        guessing a number.
        """
        if request.orig_cl_ord_id is not None:
            order = self.registry.resolve(request.session_key,
                                          request.orig_cl_ord_id)
            if order is None:
                return None, ("no live order with ClOrdID '%s'"
                              % request.orig_cl_ord_id)
            return order, None

        order_id = getattr(request, "order_id", None)
        if order_id is None:
            return None, "the request names no order"

        order = self.registry.by_order_id(order_id)
        if order is None or order.session_key != request.session_key:
            return None, "no live order with OrderID '%s'" % order_id
        return order, None

    def _resolve(self, request, is_replace):
        """Find the order a cancel or replace names, or produce a rejection."""
        if self.registry.is_duplicate(request.session_key, request.cl_ord_id):
            return None, CancelRejected(
                request.cl_ord_id, request.orig_cl_ord_id,
                CancelRejectReason.DUPLICATE_CLORDID,
                "ClOrdID '%s' has already been used" % request.cl_ord_id,
                is_replace=is_replace)

        order, missing = self._named_order(request)
        if order is None:
            return None, CancelRejected(
                request.cl_ord_id, request.orig_cl_ord_id,
                CancelRejectReason.UNKNOWN_ORDER, missing,
                is_replace=is_replace)

        if not order.is_live:
            return None, CancelRejected(
                request.cl_ord_id, request.orig_cl_ord_id,
                CancelRejectReason.TOO_LATE_TO_CANCEL,
                "order is %s" % order.status, order, is_replace=is_replace)

        # The specification requires Side and Symbol to match the original.
        if request.side is not None and request.side != order.side:
            return None, CancelRejected(
                request.cl_ord_id, request.orig_cl_ord_id,
                CancelRejectReason.MISMATCHED_FIELD,
                "Side does not match the original order", order,
                is_replace=is_replace)

        if request.symbol is not None and request.symbol != order.symbol:
            return None, CancelRejected(
                request.cl_ord_id, request.orig_cl_ord_id,
                CancelRejectReason.MISMATCHED_FIELD,
                "Symbol does not match the original order", order,
                is_replace=is_replace)

        return order, None

    # -- bulk operations ---------------------------------------------------

    def cancel_session_orders(self, session_key, reason, text=""):
        """Cancel every live order belonging to a session.

        This is Cancel on Disconnect. The resulting reports are numbered and
        stored by the session even though nobody is connected, so the client
        retrieves them by resend at its next logon.
        """
        events = []
        for market in self.markets.values():
            events.extend(market.cancel_all(
                lambda order: order.session_key == session_key, reason, text))
        if events:
            log.info("cancelled %d order(s) for session %s (%s)",
                     len(events), session_key, reason)
        return events

    def cancel_symbol_orders(self, market_name, symbol, reason, text=""):
        market = self.market(market_name)
        if market is None:
            return []
        return market.cancel_all(
            lambda order: order.symbol == symbol, reason, text)

    def orders(self, session_key=None, symbol=None, market=None, live_only=True):
        """Query the registry, for the control plane."""
        result = self.registry.live() if live_only else self.registry.all()
        if session_key is not None:
            result = [order for order in result if order.session_key == session_key]
        if symbol is not None:
            result = [order for order in result if order.symbol == symbol]
        if market is not None:
            result = [order for order in result if order.market == market]
        return result

    def _now(self):
        return self.clock.now() if self.clock else None


def _cancel_reason_for(reject_reason):
    """Map an order-validation reason onto a cancel-reject reason."""
    if reject_reason == RejectReason.PRICE_OUTSIDE_BAND:
        return CancelRejectReason.PRICE_OUTSIDE_BAND
    return CancelRejectReason.OTHER
