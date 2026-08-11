"""Matching: price/time priority, execution price, TIF handling, STP.

The self-trade prevention cases reproduce the three worked examples printed in
JNX_Self-Trade_Prevention_2.00 exactly, so the implementation is checked
against the venue's own arithmetic rather than an assumption about it.
"""

import unittest

from exchangesim.core.enums import (
    CancelReason,
    ExecInst,
    Liquidity,
    OrderStatus,
    RejectReason,
    Side,
    StpMode,
    TimeInForce,
)

from .coresupport import (
    CODEC,
    OrderFactory,
    events_of,
    fills_for,
    ladder,
    make_book,
    make_engine,
    px,
)


class BasicMatchingTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.engine = make_engine()
        self.order = OrderFactory()

    def test_a_non_crossing_order_rests(self):
        events = self.engine.submit(self.book, self.order.buy(100, "2845.5"))

        self.assertEqual(["OrderAccepted"], [e.name for e in events])
        self.assertEqual([("2845.5", 100)], ladder(self.book, "bids"))

    def test_a_crossing_order_executes(self):
        resting = self.order.sell(100, "2846")
        self.engine.submit(self.book, resting)

        incoming = self.order.buy(100, "2846")
        events = self.engine.submit(self.book, incoming)

        trades = events_of(events, "TradeExecuted")
        self.assertEqual(1, len(trades))
        self.assertEqual(100, trades[0].quantity)
        self.assertEqual(OrderStatus.FILLED, incoming.status)
        self.assertEqual(OrderStatus.FILLED, resting.status)
        self.assertEqual(0, len(self.book))

    def test_execution_happens_at_the_resting_price_not_the_incoming_one(self):
        # The rules are explicit: the new order executes at the book's price,
        # so price improvement accrues to the aggressor.
        self.engine.submit(self.book, self.order.sell(100, "2846"))

        events = self.engine.submit(self.book, self.order.buy(100, "2850"))

        self.assertEqual(px("2846"), events_of(events, "TradeExecuted")[0].price)

    def test_partial_fill_leaves_the_remainder_resting(self):
        self.engine.submit(self.book, self.order.sell(40, "2846"))

        incoming = self.order.buy(100, "2846")
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(40, incoming.cum_qty)
        self.assertEqual(60, incoming.leaves_qty)
        self.assertEqual(OrderStatus.PARTIALLY_FILLED, incoming.status)
        self.assertEqual([("2846.0", 60)], ladder(self.book, "bids"))
        # No acceptance report: the partial fill already carries LeavesQty.
        self.assertEqual(["OrderFilled", "OrderFilled", "TradeExecuted"],
                         [e.name for e in events])

    def test_an_order_sweeps_several_price_levels(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))
        self.engine.submit(self.book, self.order.sell(100, "2847"))
        self.engine.submit(self.book, self.order.sell(100, "2848"))

        incoming = self.order.buy(250, "2848")
        events = self.engine.submit(self.book, incoming)

        trades = events_of(events, "TradeExecuted")
        self.assertEqual([(px("2846"), 100), (px("2847"), 100), (px("2848"), 50)],
                         [(t.price, t.quantity) for t in trades])
        self.assertEqual(250, incoming.cum_qty)

    def test_matching_stops_at_the_limit_price(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))
        self.engine.submit(self.book, self.order.sell(100, "2850"))

        incoming = self.order.buy(200, "2846")
        self.engine.submit(self.book, incoming)

        self.assertEqual(100, incoming.cum_qty)
        self.assertEqual([("2846.0", 100)], ladder(self.book, "bids"))
        self.assertEqual([("2850.0", 100)], ladder(self.book, "asks"))

    def test_time_priority_is_honoured_within_a_level(self):
        first = self.order.sell(100, "2846")
        second = self.order.sell(100, "2846")
        self.engine.submit(self.book, first)
        self.engine.submit(self.book, second)

        self.engine.submit(self.book, self.order.buy(100, "2846"))

        self.assertEqual(100, first.cum_qty)
        self.assertEqual(0, second.cum_qty)

    def test_both_sides_receive_a_fill_with_a_shared_trade_id(self):
        resting = self.order.sell(100, "2846")
        self.engine.submit(self.book, resting)
        incoming = self.order.buy(100, "2846")

        events = self.engine.submit(self.book, incoming)

        fills = events_of(events, "OrderFilled")
        self.assertEqual(2, len(fills))
        self.assertEqual(fills[0].trade_id, fills[1].trade_id)

    def test_liquidity_indicator_distinguishes_maker_from_taker(self):
        resting = self.order.sell(100, "2846")
        self.engine.submit(self.book, resting)
        incoming = self.order.buy(100, "2846")

        events = self.engine.submit(self.book, incoming)

        self.assertEqual(Liquidity.ADDED, fills_for(events, resting)[0].liquidity)
        self.assertEqual(Liquidity.REMOVED, fills_for(events, incoming)[0].liquidity)

    def test_the_trade_records_which_side_aggressed(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))

        events = self.engine.submit(self.book, self.order.buy(100, "2846"))

        self.assertEqual(Side.BUY, events_of(events, "TradeExecuted")[0].aggressor_side)

    def test_average_price_reflects_a_multi_level_sweep(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))
        self.engine.submit(self.book, self.order.sell(100, "2848"))

        incoming = self.order.buy(200, "2848")
        self.engine.submit(self.book, incoming)

        self.assertEqual("2847.0000",
                         CODEC.format_average(incoming.notional_units,
                                              incoming.cum_qty))


class TimeInForceTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.engine = make_engine()
        self.order = OrderFactory()

    # -- IOC ---------------------------------------------------------------

    def test_ioc_cancels_its_unfilled_remainder(self):
        self.engine.submit(self.book, self.order.sell(40, "2846"))

        incoming = self.order.buy(100, "2846", tif=TimeInForce.IOC)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(40, incoming.cum_qty)
        self.assertEqual(OrderStatus.CANCELLED, incoming.status)
        self.assertEqual(0, len(self.book.bids))
        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(CancelReason.IOC_REMAINDER, cancels[0].reason)

    def test_an_ioc_that_matches_nothing_is_cancelled_outright(self):
        incoming = self.order.buy(100, "2846", tif=TimeInForce.IOC)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(0, incoming.cum_qty)
        self.assertEqual(["OrderCancelled"], [e.name for e in events])

    def test_a_fully_filled_ioc_is_not_also_cancelled(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))

        incoming = self.order.buy(100, "2846", tif=TimeInForce.IOC)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(OrderStatus.FILLED, incoming.status)
        self.assertEqual([], events_of(events, "OrderCancelled"))

    # -- IOC with MinQty ---------------------------------------------------

    def test_min_qty_is_met_and_the_order_executes(self):
        self.engine.submit(self.book, self.order.sell(80, "2846"))

        incoming = self.order.buy(100, "2846", tif=TimeInForce.IOC, min_qty=50)
        self.engine.submit(self.book, incoming)

        self.assertEqual(80, incoming.cum_qty)

    def test_min_qty_unmet_cancels_without_executing_anything(self):
        resting = self.order.sell(40, "2846")
        self.engine.submit(self.book, resting)

        incoming = self.order.buy(100, "2846", tif=TimeInForce.IOC, min_qty=50)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(0, incoming.cum_qty)
        self.assertEqual(0, resting.cum_qty, "the book must be left untouched")
        self.assertEqual(CancelReason.MIN_QTY_UNMET,
                         events_of(events, "OrderCancelled")[0].reason)
        self.assertEqual([("2846.0", 40)], ladder(self.book, "asks"))

    def test_min_qty_counts_liquidity_across_price_levels(self):
        self.engine.submit(self.book, self.order.sell(30, "2846"))
        self.engine.submit(self.book, self.order.sell(30, "2847"))

        incoming = self.order.buy(100, "2847", tif=TimeInForce.IOC, min_qty=50)
        self.engine.submit(self.book, incoming)

        self.assertEqual(60, incoming.cum_qty)

    # -- FOK ---------------------------------------------------------------

    def test_fok_fills_completely_when_it_can(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))

        incoming = self.order.buy(100, "2846", tif=TimeInForce.FOK)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(OrderStatus.FILLED, incoming.status)
        self.assertEqual([], events_of(events, "OrderCancelled"))

    def test_fok_cancels_whole_when_it_cannot_fill_completely(self):
        resting = self.order.sell(99, "2846")
        self.engine.submit(self.book, resting)

        incoming = self.order.buy(100, "2846", tif=TimeInForce.FOK)
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(0, incoming.cum_qty)
        self.assertEqual(0, resting.cum_qty, "the book must be left untouched")
        self.assertEqual(CancelReason.FOK_UNFILLED,
                         events_of(events, "OrderCancelled")[0].reason)

    def test_fok_may_fill_across_several_levels(self):
        self.engine.submit(self.book, self.order.sell(60, "2846"))
        self.engine.submit(self.book, self.order.sell(40, "2847"))

        incoming = self.order.buy(100, "2847", tif=TimeInForce.FOK)
        self.engine.submit(self.book, incoming)

        self.assertEqual(100, incoming.cum_qty)

    def test_fok_ignores_liquidity_beyond_its_limit_price(self):
        self.engine.submit(self.book, self.order.sell(60, "2846"))
        self.engine.submit(self.book, self.order.sell(40, "2900"))

        incoming = self.order.buy(100, "2846", tif=TimeInForce.FOK)
        self.engine.submit(self.book, incoming)

        self.assertEqual(0, incoming.cum_qty)


class PostOnlyTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.engine = make_engine()
        self.order = OrderFactory()

    def test_a_post_only_order_that_would_cross_is_rejected(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))

        incoming = self.order.buy(100, "2846", exec_inst=(ExecInst.POST_ONLY,))
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(["OrderRejected"], [e.name for e in events])
        self.assertEqual(RejectReason.POST_ONLY_WOULD_CROSS, events[0].reason)
        self.assertEqual(OrderStatus.REJECTED, incoming.status)

    def test_a_post_only_order_that_rests_is_accepted(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"))

        incoming = self.order.buy(100, "2845", exec_inst=(ExecInst.POST_ONLY,))
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(["OrderAccepted"], [e.name for e in events])
        self.assertEqual([("2845.0", 100)], ladder(self.book, "bids"))

    def test_post_only_on_an_empty_book_is_accepted(self):
        incoming = self.order.buy(100, "2845", exec_inst=(ExecInst.POST_ONLY,))
        events = self.engine.submit(self.book, incoming)

        self.assertEqual(["OrderAccepted"], [e.name for e in events])


class SelfTradePreventionTest(unittest.TestCase):
    """The three worked examples from JNX_Self-Trade_Prevention_2.00.

    Resting sell side, in priority order::

        100 @ 100.0  MPID B
        200 @ 100.1  MPID C
        200 @ 100.1  MPID A     <- same MPID as the incoming order
        500 @ 100.1  MPID D

    Incoming: buy 1,200 @ 100.1 with MPID A.
    """

    def _setup(self, mode):
        book = make_book()
        engine = make_engine(stp_mode=mode)
        factory = OrderFactory()

        self.resting = {
            "B": factory.sell(100, "100.0", mpid="B"),
            "C": factory.sell(200, "100.1", mpid="C"),
            "A": factory.sell(200, "100.1", mpid="A"),
            "D": factory.sell(500, "100.1", mpid="D"),
        }
        for order in self.resting.values():
            engine.submit(book, order)

        incoming = factory.buy(1200, "100.1", mpid="A")
        events = engine.submit(book, incoming)
        return book, incoming, events

    def test_cancel_newest_cancels_the_incoming_balance(self):
        book, incoming, events = self._setup(StpMode.CANCEL_NEWEST)

        # Steps 1 and 2 execute 100 then 200.
        self.assertEqual(300, incoming.cum_qty)
        # Step 3: the remaining 900 is cancelled back to the buyer.
        self.assertEqual(OrderStatus.CANCELLED, incoming.status)
        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(1, len(cancels))
        self.assertIs(incoming, cancels[0].order)
        self.assertEqual(CancelReason.SELF_TRADE_PREVENTION, cancels[0].reason)
        # A's 200 and D's 500 both remain on the sell side.
        self.assertEqual([("100.1", 700)], ladder(book, "asks"))
        self.assertEqual(0, len(book.bids))

    def test_cancel_oldest_cancels_the_resting_order_and_resumes(self):
        book, incoming, events = self._setup(StpMode.CANCEL_OLDEST)

        # Steps 1, 2 and 4 execute 100 + 200 + 500.
        self.assertEqual(800, incoming.cum_qty)
        # Step 3 cancelled A's resting 200.
        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(1, len(cancels))
        self.assertIs(self.resting["A"], cancels[0].order)
        self.assertEqual(OrderStatus.CANCELLED, self.resting["A"].status)
        # Step 5: the remaining 400 is posted on the buy side.
        self.assertEqual(400, incoming.leaves_qty)
        self.assertEqual([("100.1", 400)], ladder(book, "bids"))
        self.assertEqual(0, len(book.asks))

    def test_decrement_shrinks_the_larger_order_and_resumes(self):
        book, incoming, events = self._setup(StpMode.DECREMENT)

        # Steps 1, 2 and 4 execute 100 + 200 + 500.
        self.assertEqual(800, incoming.cum_qty)
        # Step 3: A's resting 200 is cancelled and 200 comes off the buy order,
        # taking it from 1,200 to 1,000.
        self.assertEqual(1000, incoming.quantity)
        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(1, len(cancels))
        self.assertIs(self.resting["A"], cancels[0].order)
        decrements = events_of(events, "OrderDecremented")
        self.assertEqual(1, len(decrements))
        self.assertIs(incoming, decrements[0].order)
        self.assertEqual(200, decrements[0].reduced_by)
        # Step 5: 200 remains and is posted on the buy side.
        self.assertEqual(200, incoming.leaves_qty)
        self.assertEqual([("100.1", 200)], ladder(book, "bids"))

    def test_disabled_prevention_lets_the_orders_trade(self):
        book, incoming, _events = self._setup(StpMode.NONE)

        self.assertEqual(1000, incoming.cum_qty)
        self.assertEqual([], events_of(_events, "OrderCancelled"))


class SelfTradePreventionEdgeTest(unittest.TestCase):

    def setUp(self):
        self.order = OrderFactory()

    def _run(self, mode, resting_qty, incoming_qty):
        book = make_book()
        engine = make_engine(stp_mode=mode)
        resting = self.order.sell(resting_qty, "100.0", mpid="A")
        engine.submit(book, resting)
        incoming = self.order.buy(incoming_qty, "100.0", mpid="A")
        events = engine.submit(book, incoming)
        return book, resting, incoming, events

    def test_decrement_with_equal_sizes_cancels_both(self):
        book, resting, incoming, events = self._run(StpMode.DECREMENT, 100, 100)

        self.assertEqual(OrderStatus.CANCELLED, resting.status)
        self.assertEqual(OrderStatus.CANCELLED, incoming.status)
        self.assertEqual(2, len(events_of(events, "OrderCancelled")))
        self.assertEqual(0, len(book))

    def test_decrement_with_a_smaller_incoming_order_shrinks_the_resting_one(self):
        book, resting, incoming, events = self._run(StpMode.DECREMENT, 500, 200)

        self.assertEqual(OrderStatus.CANCELLED, incoming.status)
        self.assertEqual(300, resting.leaves_qty)
        self.assertEqual([("100.0", 300)], ladder(book, "asks"))
        decrements = events_of(events, "OrderDecremented")
        self.assertIs(resting, decrements[0].order)
        self.assertEqual(200, decrements[0].reduced_by)

    def test_orders_without_an_mpid_are_never_prevented(self):
        book = make_book()
        engine = make_engine(stp_mode=StpMode.CANCEL_NEWEST)
        engine.submit(book, self.order.sell(100, "100.0"))

        incoming = self.order.buy(100, "100.0")
        engine.submit(book, incoming)

        self.assertEqual(100, incoming.cum_qty)

    def test_different_mpids_trade_normally(self):
        book = make_book()
        engine = make_engine(stp_mode=StpMode.CANCEL_NEWEST)
        engine.submit(book, self.order.sell(100, "100.0", mpid="B"))

        incoming = self.order.buy(100, "100.0", mpid="A")
        engine.submit(book, incoming)

        self.assertEqual(100, incoming.cum_qty)

    def test_fok_pre_check_excludes_liquidity_blocked_by_prevention(self):
        # 100 of the 200 available belongs to the same MPID, so a FOK for 200
        # cannot fill and must leave the book alone.
        book = make_book()
        engine = make_engine(stp_mode=StpMode.CANCEL_OLDEST)
        theirs = self.order.sell(100, "100.0", mpid="B")
        mine = self.order.sell(100, "100.0", mpid="A")
        engine.submit(book, theirs)
        engine.submit(book, mine)

        incoming = self.order.buy(200, "100.0", tif=TimeInForce.FOK, mpid="A")
        events = engine.submit(book, incoming)

        self.assertEqual(0, incoming.cum_qty)
        self.assertEqual(CancelReason.FOK_UNFILLED,
                         events_of(events, "OrderCancelled")[0].reason)
        self.assertEqual(2, len(book.asks), "the resting orders must survive")


class AmendTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.engine = make_engine()
        self.order = OrderFactory()

    def test_reducing_quantity_keeps_time_priority(self):
        first = self.order.buy(100, "2845.5")
        second = self.order.buy(100, "2845.5")
        self.engine.submit(self.book, first)
        self.engine.submit(self.book, second)

        kept = self.engine.amend(self.book, first, new_quantity=50)

        self.assertTrue(kept)
        self.assertEqual([first, second],
                         list(self.book.bids.orders_in_priority()))
        self.assertEqual([("2845.5", 150)], ladder(self.book, "bids"))

    def test_increasing_quantity_loses_time_priority(self):
        first = self.order.buy(100, "2845.5")
        second = self.order.buy(100, "2845.5")
        self.engine.submit(self.book, first)
        self.engine.submit(self.book, second)

        kept = self.engine.amend(self.book, first, new_quantity=200)

        self.assertFalse(kept)
        self.assertEqual([second, first],
                         list(self.book.bids.orders_in_priority()))

    def test_changing_price_loses_time_priority(self):
        first = self.order.buy(100, "2845.5")
        second = self.order.buy(100, "2845.5")
        self.engine.submit(self.book, first)
        self.engine.submit(self.book, second)

        kept = self.engine.amend(self.book, first, new_price=px("2845.5") + 1)

        self.assertFalse(kept)
        self.assertEqual(px("2845.6"), self.book.best_bid)

    def test_amending_to_a_new_level_moves_the_order(self):
        order = self.order.buy(100, "2845.5")
        self.engine.submit(self.book, order)

        self.engine.amend(self.book, order, new_price=px("2840"))

        self.assertEqual([("2840.0", 100)], ladder(self.book, "bids"))

    def test_reducing_quantity_to_the_executed_amount_removes_the_order(self):
        self.engine.submit(self.book, self.order.sell(40, "2846"))
        order = self.order.buy(100, "2846")
        self.engine.submit(self.book, order)  # 40 filled, 60 resting

        self.engine.amend(self.book, order, new_quantity=40)

        self.assertEqual(0, order.leaves_qty)
        self.assertEqual(0, len(self.book.bids))


class CancelTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.engine = make_engine()
        self.order = OrderFactory()

    def test_cancelling_removes_the_order_from_the_book(self):
        order = self.order.buy(100, "2845.5")
        self.engine.submit(self.book, order)

        events = self.engine.cancel(self.book, order)

        self.assertEqual(OrderStatus.CANCELLED, order.status)
        self.assertEqual(0, len(self.book))
        self.assertEqual(CancelReason.USER_REQUEST, events[0].reason)

    def test_a_partially_filled_order_can_be_cancelled(self):
        self.engine.submit(self.book, self.order.sell(40, "2846"))
        order = self.order.buy(100, "2846")
        self.engine.submit(self.book, order)

        self.engine.cancel(self.book, order)

        self.assertEqual(40, order.cum_qty)
        self.assertEqual(OrderStatus.CANCELLED, order.status)
        self.assertEqual(0, order.leaves_qty)


class AccumulationTest(unittest.TestCase):
    """Auction phases accept orders onto the book without matching them."""

    def setUp(self):
        self.book = make_book()
        self.engine = make_engine()
        self.order = OrderFactory()

    def test_crossing_orders_rest_instead_of_trading(self):
        self.engine.submit(self.book, self.order.sell(100, "2846"),
                           allow_matching=False)
        incoming = self.order.buy(100, "2846")

        events = self.engine.submit(self.book, incoming, allow_matching=False)

        self.assertEqual(["OrderAccepted"], [e.name for e in events])
        self.assertEqual(0, incoming.cum_qty)
        self.assertTrue(self.book.is_crossed)

    def test_an_ioc_does_not_get_cancelled_while_accumulating(self):
        incoming = self.order.buy(100, "2846", tif=TimeInForce.IOC)

        events = self.engine.submit(self.book, incoming, allow_matching=False)

        self.assertEqual(["OrderAccepted"], [e.name for e in events])


if __name__ == "__main__":
    unittest.main()
