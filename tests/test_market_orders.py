"""Market orders, and self-match prevention keyed on the order itself.

Both exist for Hong Kong: HKEX accepts OrdType(40)=1 and keys self-match
prevention on a client-supplied SelfMatchPreventionID(2362) with its own
cancel-passive / cancel-aggressive instruction. Japannext has neither, so these
also serve as the guard that adding them changed nothing there.
"""

import unittest

from exchangesim.core.enums import (
    CancelReason,
    OrderStatus,
    OrderType,
    RejectReason,
    StpMode,
    TimeInForce,
    TradingState,
)
from exchangesim.core.validation import StandardValidator, ValidationLimits

from .coresupport import (
    CODEC,
    OrderFactory,
    events_of,
    fills_for,
    ladder,
    make_book,
    make_engine,
)


class MarketOrderTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.engine = make_engine()
        self.orders = OrderFactory()

    def market(self, side, quantity, **kwargs):
        builder = self.orders.buy if side == "BUY" else self.orders.sell
        return builder(quantity, price=None, order_type=OrderType.MARKET,
                       **kwargs)

    def rest(self, order):
        self.engine.submit(self.book, order)
        return order

    def test_a_market_buy_lifts_the_offer(self):
        self.rest(self.orders.sell(100, "100.0"))

        order = self.market("BUY", 100)
        events = self.engine.submit(self.book, order)

        self.assertEqual(OrderStatus.FILLED, order.status)
        self.assertEqual("100.0", CODEC.format(fills_for(events, order)[0].price))

    def test_it_sweeps_several_levels_at_their_own_prices(self):
        self.rest(self.orders.sell(100, "100.0"))
        self.rest(self.orders.sell(100, "100.5"))
        self.rest(self.orders.sell(100, "101.0"))

        order = self.market("BUY", 300)
        events = self.engine.submit(self.book, order)

        self.assertEqual(["100.0", "100.5", "101.0"],
                         [CODEC.format(fill.price)
                          for fill in fills_for(events, order)])
        self.assertEqual(OrderStatus.FILLED, order.status)

    def test_an_unfilled_remainder_expires_rather_than_resting(self):
        self.rest(self.orders.sell(100, "100.0"))

        order = self.market("BUY", 300)
        events = self.engine.submit(self.book, order)

        self.assertEqual(100, order.cum_qty)
        self.assertEqual(0, order.leaves_qty)
        self.assertEqual([], ladder(self.book, "bids"),
                         "a priceless order must never reach the book")
        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(CancelReason.MARKET_REMAINDER, cancels[0].reason)

    def test_a_market_order_against_an_empty_book_expires_whole(self):
        order = self.market("BUY", 100)
        events = self.engine.submit(self.book, order)

        self.assertEqual(0, order.cum_qty)
        self.assertEqual(1, len(events_of(events, "OrderCancelled")))
        self.assertEqual([], ladder(self.book, "bids"))

    def test_a_market_sell_hits_the_bid(self):
        self.rest(self.orders.buy(100, "99.5"))

        order = self.market("SELL", 100)
        events = self.engine.submit(self.book, order)

        self.assertEqual("99.5", CODEC.format(fills_for(events, order)[0].price))

    def test_a_market_fok_that_cannot_fill_leaves_the_book_alone(self):
        self.rest(self.orders.sell(100, "100.0"))

        order = self.market("BUY", 300, tif=TimeInForce.FOK)
        events = self.engine.submit(self.book, order)

        self.assertEqual(0, order.cum_qty)
        self.assertEqual(CancelReason.FOK_UNFILLED,
                         events_of(events, "OrderCancelled")[0].reason)
        self.assertEqual([("100.0", 100)], ladder(self.book, "asks"))

    def test_a_market_order_is_never_reported_as_accepted(self):
        self.rest(self.orders.sell(100, "100.0"))

        events = self.engine.submit(self.book, self.market("BUY", 300))

        self.assertEqual([], events_of(events, "OrderAccepted"))


class MarketOrderValidationTest(unittest.TestCase):
    """A venue opts in; Japannext's validator still refuses them."""

    def setUp(self):
        self.orders = OrderFactory()
        from .coresupport import make_instruments
        self.instrument = make_instruments()["7203"]

    def validator(self, allowed):
        return StandardValidator(
            CODEC, ValidationLimits(allowed_order_types=allowed))

    def test_a_market_order_is_refused_where_only_limits_are_allowed(self):
        order = self.orders.buy(100, price=None, order_type=OrderType.MARKET)

        rejection = self.validator((OrderType.LIMIT,)).validate_new(
            order, self.instrument, _open_state())

        self.assertIsNotNone(rejection)
        self.assertEqual(RejectReason.UNSUPPORTED_CHARACTERISTIC,
                         rejection.reason)

    def test_a_market_order_passes_where_the_venue_allows_them(self):
        order = self.orders.buy(100, price=None, order_type=OrderType.MARKET)

        rejection = self.validator(
            (OrderType.LIMIT, OrderType.MARKET)).validate_new(
                order, self.instrument, _open_state())

        self.assertIsNone(rejection)

    def test_a_limit_order_still_requires_a_price(self):
        order = self.orders.buy(100, price=None)

        rejection = self.validator(
            (OrderType.LIMIT, OrderType.MARKET)).validate_new(
                order, self.instrument, _open_state())

        self.assertEqual(RejectReason.INVALID_PRICE, rejection.reason)


class SelfMatchIdTest(unittest.TestCase):
    """SMP keyed on the order, HKEX style."""

    def setUp(self):
        self.book = make_book()
        # NONE: the market default is off, so anything that fires must have
        # come from the key on the order itself.
        self.engine = make_engine(stp_mode=StpMode.NONE)
        self.orders = OrderFactory()

    def rest(self, order):
        self.engine.submit(self.book, order)
        return order

    def test_matching_ids_prevent_a_trade_even_with_the_market_mode_off(self):
        self.rest(self.orders.sell(100, "100.0", stp_id="SMP1",
                                   stp_instruction=StpMode.CANCEL_NEWEST))

        incoming = self.orders.buy(100, "100.0", stp_id="SMP1",
                                   stp_instruction=StpMode.CANCEL_NEWEST)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(0, incoming.cum_qty)
        self.assertEqual([], events_of(events, "TradeExecuted"))

    def test_different_ids_trade_normally(self):
        self.rest(self.orders.sell(100, "100.0", stp_id="SMP1"))

        incoming = self.orders.buy(100, "100.0", stp_id="SMP2")
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(100, incoming.cum_qty)
        self.assertEqual(1, len(events_of(events, "TradeExecuted")))

    def test_cancel_aggressive_removes_the_incoming_order(self):
        resting = self.rest(self.orders.sell(100, "100.0", stp_id="S"))

        incoming = self.orders.buy(100, "100.0", stp_id="S",
                                   stp_instruction=StpMode.CANCEL_NEWEST)
        self.engine.submit(self.book, incoming)

        self.assertEqual(OrderStatus.CANCELLED, incoming.status)
        self.assertEqual([("100.0", 100)], ladder(self.book, "asks"),
                         "the passive order must survive")
        self.assertEqual(100, resting.leaves_qty)

    def test_cancel_passive_removes_the_resting_order(self):
        resting = self.rest(self.orders.sell(100, "100.0", stp_id="S"))

        incoming = self.orders.buy(100, "100.0", stp_id="S",
                                   stp_instruction=StpMode.CANCEL_OLDEST)
        self.engine.submit(self.book, incoming)

        self.assertEqual(OrderStatus.CANCELLED, resting.status)
        self.assertEqual([], ladder(self.book, "asks"))

    def test_cancel_passive_lets_the_incoming_order_carry_on(self):
        """It should trade with the next order down, not stop at the block."""
        self.rest(self.orders.sell(100, "100.0", stp_id="S"))
        self.rest(self.orders.sell(100, "100.5"))

        incoming = self.orders.buy(200, "100.5", stp_id="S",
                                   stp_instruction=StpMode.CANCEL_OLDEST)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(100, incoming.cum_qty)
        self.assertEqual("100.5",
                         CODEC.format(fills_for(events, incoming)[0].price))

    def test_an_id_matches_across_firms(self):
        """HKEX is explicit: the same SMP ID collides whoever submitted it."""
        self.rest(self.orders.sell(100, "100.0", stp_id="S", mpid="FIRM_A"))

        incoming = self.orders.buy(100, "100.0", stp_id="S", mpid="FIRM_B",
                                   stp_instruction=StpMode.CANCEL_NEWEST)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual([], events_of(events, "TradeExecuted"))

    def test_an_order_without_an_id_is_unaffected(self):
        self.rest(self.orders.sell(100, "100.0", stp_id="S"))

        incoming = self.orders.buy(100, "100.0")
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(1, len(events_of(events, "TradeExecuted")))

    def test_the_market_mode_still_governs_orders_without_an_id(self):
        """Japannext's behaviour must be untouched by any of this."""
        engine = make_engine(stp_mode=StpMode.CANCEL_NEWEST)
        resting = self.orders.sell(100, "100.0", mpid="MPID1")
        engine.submit(self.book, resting)

        incoming = self.orders.buy(100, "100.0", mpid="MPID1")
        events = engine.submit(self.book, incoming)

        self.assertEqual([], events_of(events, "TradeExecuted"))
        self.assertEqual(OrderStatus.CANCELLED, incoming.status)

    def test_an_id_without_an_instruction_falls_back_to_the_market_mode(self):
        engine = make_engine(stp_mode=StpMode.CANCEL_OLDEST)
        resting = self.orders.sell(100, "100.0", stp_id="S")
        engine.submit(self.book, resting)

        incoming = self.orders.buy(100, "100.0", stp_id="S")
        engine.submit(self.book, incoming)

        self.assertEqual(OrderStatus.CANCELLED, resting.status)


def _open_state():
    from exchangesim.core.trading_state import TradingStateMachine
    return TradingStateMachine("DAY", TradingState.OPEN)


if __name__ == "__main__":
    unittest.main()
