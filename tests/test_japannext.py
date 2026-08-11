"""Japannext venue: FIX in, execution reports out.

Assertions are against the tag numbers and values in
JNX_FIX_Trading_Specification_Equities_3.00, so a change that breaks wire
fidelity fails here rather than against the real test environment.
"""

import unittest

from exchangesim.fix import constants as C
from exchangesim.venues.japannext import dictionary as D

from .jnxsupport import CLIENT1, CLIENT2, SYMBOL, VenueHarness, venue_config


class VenueTestCase(unittest.TestCase):
    """Base class providing a started venue and two logged-on clients."""

    config = None

    def setUp(self):
        self.harness = VenueHarness(self.config)
        self.addCleanup(self.harness.close)
        self.buyer = self.harness.client(CLIENT1)
        self.seller = self.harness.client(CLIENT2)


class LogonTest(VenueTestCase):

    def test_logon_is_followed_by_trading_session_status_per_market(self):
        harness = VenueHarness()
        self.addCleanup(harness.close)
        client = harness.client(CLIENT1, logon=False)

        client.logon()
        messages = client.received()

        self.assertEqual(C.LOGON, messages[0].msg_type)
        statuses = [m for m in messages
                    if m.msg_type == C.TRADING_SESSION_STATUS]
        self.assertEqual({"DAY", "NGHT"},
                         {m.get(D.TRADING_SESSION_ID) for m in statuses})

    def test_status_reports_the_configured_state(self):
        harness = VenueHarness()
        self.addCleanup(harness.close)
        client = harness.client(CLIENT1, logon=False)

        client.logon()
        statuses = dict((m.get(D.TRADING_SESSION_ID), m)
                        for m in client.received()
                        if m.msg_type == C.TRADING_SESSION_STATUS)

        self.assertEqual(D.TradSesStatus.OPEN,
                         statuses["DAY"].get(D.TRAD_SES_STATUS))
        self.assertEqual(D.TradSesStatus.CLOSED,
                         statuses["NGHT"].get(D.TRAD_SES_STATUS))
        self.assertEqual(D.TradSesMode.TESTING,
                         statuses["DAY"].get(D.TRAD_SES_MODE))


class NewOrderTest(VenueTestCase):

    def test_an_accepted_order_reports_every_required_field(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")

        report = self.buyer.reports()[0]

        self.assertEqual(D.ExecType.NEW, report.get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.NEW, report.get(D.ORD_STATUS))
        self.assertEqual(D.ExecTransType.NEW, report.get(D.EXEC_TRANS_TYPE))
        self.assertEqual("B1", report.get(D.CL_ORD_ID))
        self.assertEqual(SYMBOL, report.get(D.SYMBOL))
        self.assertEqual(D.SideValue.BUY, report.get(D.SIDE))
        self.assertEqual("100", report.get(D.ORDER_QTY))
        self.assertEqual("2845.5", report.get(D.PRICE))
        self.assertEqual(D.OrdType.LIMIT, report.get(D.ORD_TYPE))
        # The specification states CumQty and AvgPx are 0 on acceptance and
        # LeavesQty equals OrderQty.
        self.assertEqual("0", report.get(D.CUM_QTY))
        self.assertEqual("0.0000", report.get(D.AVG_PX))
        self.assertEqual("100", report.get(D.LEAVES_QTY))
        self.assertTrue(report.get(D.ORDER_ID))
        self.assertTrue(report.get(D.EXEC_ID))

    def test_identifiers_respect_the_specification_length_limits(self):
        self.buyer.new_order("B1")

        report = self.buyer.reports()[0]

        self.assertLessEqual(len(report.get(D.ORDER_ID)), 20)
        self.assertLessEqual(len(report.get(D.EXEC_ID)), 20)

    def test_the_market_is_reported_in_sender_sub_id(self):
        self.buyer.new_order("B1")

        self.assertEqual("DAY", self.buyer.reports()[0].get(C.SENDER_SUB_ID))

    def test_optional_fields_are_echoed_back(self):
        self.buyer.new_order("B1", account="ACC1", mpid="123456789",
                             capacity=D.Rule80A.AGENCY,
                             tif=D.TimeInForce.DAY)

        report = self.buyer.reports()[0]

        self.assertEqual("ACC1", report.get(D.ACCOUNT))
        self.assertEqual("123456789", report.get(D.CLIENT_ID))
        self.assertEqual(D.Rule80A.AGENCY, report.get(D.RULE_80A))
        self.assertEqual(D.TimeInForce.DAY, report.get(D.TIME_IN_FORCE))

    def test_time_in_force_defaults_to_day(self):
        self.buyer.new_order("B1", tif=None)

        self.assertEqual(D.TimeInForce.DAY,
                         self.buyer.reports()[0].get(D.TIME_IN_FORCE))


class RejectionTest(VenueTestCase):
    """Every OrdRejReason the specification lists should be reachable."""

    def _reason(self, report):
        return report.get(D.ORD_REJ_REASON)

    def test_unknown_symbol_is_reason_one(self):
        self.buyer.new_order("B1", symbol="0001")

        report = self.buyer.reports()[0]
        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.OrdRejReason.UNKNOWN_SYMBOL), self._reason(report))

    def test_a_closed_market_is_reason_two(self):
        self.buyer.new_order("B1", sub_id="NGHT")

        self.assertEqual(str(D.OrdRejReason.VENUE_CLOSED),
                         self._reason(self.buyer.reports()[0]))

    def test_exceeding_the_quantity_cap_is_reason_three(self):
        # 7203 has 16,000,000 shares outstanding, so the 5% cap is 800,000.
        self.buyer.new_order("B1", quantity=800100, price="2845.5")

        self.assertEqual(str(D.OrdRejReason.EXCEEDS_LIMIT),
                         self._reason(self.buyer.reports()[0]))

    def test_a_duplicate_clordid_is_reason_six_with_order_id_none(self):
        self.buyer.new_order("B1")
        self.buyer.drain()

        self.buyer.new_order("B1")
        report = self.buyer.reports()[0]

        self.assertEqual(str(D.OrdRejReason.DUPLICATE_ORDER), self._reason(report))
        self.assertEqual("NONE", report.get(D.ORDER_ID))

    def test_an_odd_lot_is_reason_thirteen(self):
        self.buyer.new_order("B1", quantity=150)

        self.assertEqual(str(D.OrdRejReason.INCORRECT_QUANTITY),
                         self._reason(self.buyer.reports()[0]))

    def test_a_price_outside_the_band_is_reason_sixteen(self):
        # 7203's band is 2345.5 to 3345.5.
        self.buyer.new_order("B1", price="3346")

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         self._reason(self.buyer.reports()[0]))

    def test_min_qty_without_ioc_is_reason_eleven(self):
        self.buyer.new_order("B1", min_qty=50, tif=D.TimeInForce.DAY)

        self.assertEqual(str(D.OrdRejReason.UNSUPPORTED_CHARACTERISTIC),
                         self._reason(self.buyer.reports()[0]))

    def test_a_halted_instrument_is_rejected_while_others_trade(self):
        self.harness.command("state.set", market="DAY", symbol=SYMBOL,
                             state="HALTED")
        self.buyer.drain()

        self.buyer.new_order("B1", symbol=SYMBOL)
        self.buyer.new_order("B2", symbol="6758", price="13500")

        reports = self.buyer.reports()
        self.assertEqual(str(D.OrdRejReason.VENUE_CLOSED),
                         reports[0].get(D.ORD_REJ_REASON))
        self.assertEqual(D.ExecType.NEW, reports[1].get(D.EXEC_TYPE))


class ValidationRejectTest(VenueTestCase):
    """Dialect violations produce session-level Rejects, not business ones."""

    def test_an_unsupported_order_type_is_session_rejected(self):
        # OrdType 1 (Market) is not in the dialect; only 2 (Limit) is.
        self.buyer.new_order("B1", ord_type="1")

        rejects = [m for m in self.buyer.received() if m.msg_type == C.REJECT]
        self.assertEqual(1, len(rejects))
        self.assertEqual(str(C.SessionRejectReason.VALUE_INCORRECT),
                         rejects[0].get(C.SESSION_REJECT_REASON))
        self.assertEqual(str(D.ORD_TYPE), rejects[0].get(C.REF_TAG_ID))

    def test_a_price_with_too_much_precision_is_rejected(self):
        # Price(44) allows one decimal place.
        self.buyer.new_order("B1", price="2845.55")

        rejects = [m for m in self.buyer.received() if m.msg_type == C.REJECT]
        self.assertEqual(str(C.SessionRejectReason.VALUE_INCORRECT),
                         rejects[0].get(C.SESSION_REJECT_REASON))

    def test_a_clordid_over_thirty_two_characters_is_rejected(self):
        self.buyer.new_order("X" * 33)

        rejects = [m for m in self.buyer.received() if m.msg_type == C.REJECT]
        self.assertEqual(str(D.CL_ORD_ID), rejects[0].get(C.REF_TAG_ID))

    def test_an_unknown_target_sub_id_is_rejected(self):
        self.buyer.new_order("B1", sub_id="NOPE")

        rejects = [m for m in self.buyer.received() if m.msg_type == C.REJECT]
        self.assertEqual(str(C.TARGET_SUB_ID), rejects[0].get(C.REF_TAG_ID))

    def test_a_market_the_session_may_not_trade_is_rejected(self):
        config = venue_config(
            markets=[{"name": "DAY", "state": "OPEN"},
                     {"name": "DAYX", "state": "OPEN"}],
            sessions=[{"target_comp_id": CLIENT1, "default_sub_id": "DAY",
                       "markets": ["DAY"]}])
        harness = VenueHarness(config)
        self.addCleanup(harness.close)
        client = harness.client(CLIENT1)

        client.new_order("B1", sub_id="DAYX")

        rejects = [m for m in client.received() if m.msg_type == C.REJECT]
        self.assertIn("may not trade", rejects[0].get(C.TEXT))

    def test_an_execution_report_sent_by_a_client_is_refused(self):
        from exchangesim.fix.message import Message
        self.buyer.send(Message.create(C.EXECUTION_REPORT))

        rejects = [m for m in self.buyer.received() if m.msg_type == C.REJECT]
        self.assertEqual(str(C.SessionRejectReason.INVALID_MSGTYPE),
                         rejects[0].get(C.SESSION_REJECT_REASON))


class TradeTest(VenueTestCase):

    def test_both_sides_are_reported_with_a_shared_trade_id(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()

        self.buyer.new_order("B1", side=D.SideValue.BUY, quantity=100,
                             price="2846")

        buy_report = self.buyer.reports()[0]
        sell_report = self.seller.reports()[0]

        self.assertEqual(D.ExecType.FILL, buy_report.get(D.EXEC_TYPE))
        self.assertEqual(D.ExecType.FILL, sell_report.get(D.EXEC_TYPE))
        self.assertEqual(buy_report.get(D.TRD_MATCH_ID),
                         sell_report.get(D.TRD_MATCH_ID))
        self.assertTrue(buy_report.get(D.TRD_MATCH_ID))

    def test_liquidity_indicator_distinguishes_maker_from_taker(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, price="2846")
        self.seller.drain()
        self.buyer.new_order("B1", price="2846")

        self.assertEqual(D.LastLiquidityInd.REMOVED,
                         self.buyer.reports()[0].get(D.LAST_LIQUIDITY_IND))
        self.assertEqual(D.LastLiquidityInd.ADDED,
                         self.seller.reports()[0].get(D.LAST_LIQUIDITY_IND))

    def test_the_fill_reports_last_price_and_quantity(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()
        self.buyer.new_order("B1", quantity=100, price="2850")

        report = self.buyer.reports()[0]

        # Execution is at the resting order's price, per the trading rules.
        self.assertEqual("2846.0", report.get(D.LAST_PX))
        self.assertEqual("100", report.get(D.LAST_SHARES))
        self.assertEqual("100", report.get(D.CUM_QTY))
        self.assertEqual("0", report.get(D.LEAVES_QTY))
        self.assertEqual("2846.0000", report.get(D.AVG_PX))

    def test_a_partial_fill_reports_partially_filled(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()
        self.buyer.new_order("B1", quantity=300, price="2846")

        reports = self.buyer.reports()

        # One report only. The specification defines Order Accepted as
        # carrying CumQty 0 and LeavesQty equal to OrderQty, so an order that
        # filled on entry must not also be acknowledged as new -- the resting
        # remainder is already reported in LeavesQty here.
        self.assertEqual(1, len(reports))
        self.assertEqual(D.ExecType.PARTIAL_FILL, reports[0].get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.PARTIALLY_FILLED,
                         reports[0].get(D.ORD_STATUS))
        self.assertEqual("200", reports[0].get(D.LEAVES_QTY))
        self.assertEqual("100", reports[0].get(D.CUM_QTY))

    def test_an_ioc_partial_fill_reports_the_quantity_open_at_that_moment(self):
        # Regression: events are rendered as a batch after the engine returns,
        # by which time the IOC remainder has been cancelled. The fill report
        # must still show the 200 that were open when it happened, not 0.
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=300, price="2846",
                             tif=D.TimeInForce.IOC)

        fill, cancel = self.buyer.reports()
        self.assertEqual(D.ExecType.PARTIAL_FILL, fill.get(D.EXEC_TYPE))
        self.assertEqual("100", fill.get(D.CUM_QTY))
        self.assertEqual("200", fill.get(D.LEAVES_QTY))
        self.assertEqual(D.ExecType.CANCELED, cancel.get(D.EXEC_TYPE))
        self.assertEqual("0", cancel.get(D.LEAVES_QTY))

    def test_each_fill_of_a_sweep_carries_its_own_running_average(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.new_order("S2", side=D.SideValue.SELL, quantity=100,
                              price="2848")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=200, price="2848")

        first, second = self.buyer.reports()
        self.assertEqual("2846.0000", first.get(D.AVG_PX))
        self.assertEqual("100", first.get(D.CUM_QTY))
        self.assertEqual("100", first.get(D.LEAVES_QTY))
        self.assertEqual("2847.0000", second.get(D.AVG_PX))
        self.assertEqual("200", second.get(D.CUM_QTY))
        self.assertEqual("0", second.get(D.LEAVES_QTY))

    def test_the_average_price_spans_several_fills(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.new_order("S2", side=D.SideValue.SELL, quantity=100,
                              price="2848")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=200, price="2848")

        final = self.buyer.reports()[-1]
        self.assertEqual("2847.0000", final.get(D.AVG_PX))

    def test_markets_do_not_share_liquidity(self):
        self.harness.command("state.set", market="NGHT", state="OPEN")
        self.buyer.drain()
        self.seller.drain()

        self.seller.new_order("S1", side=D.SideValue.SELL, price="2846",
                              sub_id="NGHT")
        self.seller.drain()
        self.buyer.new_order("B1", price="2846", sub_id="DAY")

        self.assertEqual(D.ExecType.NEW, self.buyer.reports()[0].get(D.EXEC_TYPE))


class TimeInForceTest(VenueTestCase):

    def test_ioc_remainder_is_cancelled(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=300, price="2846",
                             tif=D.TimeInForce.IOC)

        reports = self.buyer.reports()
        self.assertEqual(D.ExecType.PARTIAL_FILL, reports[0].get(D.EXEC_TYPE))
        self.assertEqual(D.ExecType.CANCELED, reports[1].get(D.EXEC_TYPE))
        self.assertEqual("0", reports[1].get(D.LEAVES_QTY))

    def test_an_unfillable_fok_is_cancelled_without_executing(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=300, price="2846",
                             tif=D.TimeInForce.FOK)

        reports = self.buyer.reports()
        self.assertEqual(1, len(reports))
        self.assertEqual(D.ExecType.CANCELED, reports[0].get(D.EXEC_TYPE))
        self.assertEqual("0", reports[0].get(D.CUM_QTY))
        # The resting order must be untouched.
        self.assertEqual([], self.seller.reports())

    def test_ioc_with_an_unmet_min_qty_cancels_without_executing(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=300, price="2846",
                             tif=D.TimeInForce.IOC, min_qty=200)

        reports = self.buyer.reports()
        self.assertEqual(D.ExecType.CANCELED, reports[0].get(D.EXEC_TYPE))
        self.assertEqual("0", reports[0].get(D.CUM_QTY))
        self.assertEqual("200", reports[0].get(D.MIN_QTY))


class PostOnlyTest(VenueTestCase):

    def test_a_post_only_order_that_would_cross_is_rejected(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, price="2846")
        self.seller.drain()

        self.buyer.new_order("B1", price="2846",
                             exec_inst=D.ExecInstValue.POST_ONLY)

        report = self.buyer.reports()[0]
        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.OrdRejReason.UNSUPPORTED_CHARACTERISTIC),
                         report.get(D.ORD_REJ_REASON))

    def test_a_post_only_order_that_rests_is_accepted(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, price="2846")
        self.seller.drain()

        self.buyer.new_order("B1", price="2845",
                             exec_inst=D.ExecInstValue.POST_ONLY)

        self.assertEqual(D.ExecType.NEW, self.buyer.reports()[0].get(D.EXEC_TYPE))


class CancelTest(VenueTestCase):

    def setUp(self):
        VenueTestCase.setUp(self)
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        self.buyer.drain()

    def test_a_cancel_is_confirmed(self):
        self.buyer.cancel("B2", "B1")

        report = self.buyer.reports()[0]

        self.assertEqual(D.ExecType.CANCELED, report.get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.CANCELED, report.get(D.ORD_STATUS))
        self.assertEqual("B2", report.get(D.CL_ORD_ID))
        self.assertEqual("B1", report.get(D.ORIG_CL_ORD_ID))
        self.assertEqual("0", report.get(D.LEAVES_QTY))

    def test_an_unknown_order_gets_a_cancel_reject(self):
        self.buyer.cancel("B2", "NEVER-EXISTED")

        rejects = [m for m in self.buyer.received()
                   if m.msg_type == C.ORDER_CANCEL_REJECT]

        self.assertEqual(1, len(rejects))
        self.assertEqual(str(D.CxlRejReason.UNKNOWN_ORDER),
                         rejects[0].get(D.CXL_REJ_REASON))
        self.assertEqual("NONE", rejects[0].get(D.ORDER_ID))
        self.assertEqual(D.CxlRejResponseTo.CANCEL_REQUEST,
                         rejects[0].get(D.CXL_REJ_RESPONSE_TO))

    def test_cancelling_twice_is_too_late(self):
        self.buyer.cancel("B2", "B1")
        self.buyer.drain()

        self.buyer.cancel("B3", "B2")

        rejects = [m for m in self.buyer.received()
                   if m.msg_type == C.ORDER_CANCEL_REJECT]
        self.assertEqual(str(D.CxlRejReason.TOO_LATE_TO_CANCEL),
                         rejects[0].get(D.CXL_REJ_REASON))

    def test_another_client_cannot_cancel_the_order(self):
        self.seller.cancel("S9", "B1")

        rejects = [m for m in self.seller.received()
                   if m.msg_type == C.ORDER_CANCEL_REJECT]
        self.assertEqual(str(D.CxlRejReason.UNKNOWN_ORDER),
                         rejects[0].get(D.CXL_REJ_REASON))

    def test_a_mismatched_side_is_rejected(self):
        self.buyer.cancel("B2", "B1", side=D.SideValue.SELL)

        rejects = [m for m in self.buyer.received()
                   if m.msg_type == C.ORDER_CANCEL_REJECT]
        self.assertEqual(1, len(rejects))


class ReplaceTest(VenueTestCase):

    def setUp(self):
        VenueTestCase.setUp(self)
        self.buyer.new_order("B1", quantity=200, price="2845.5")
        self.buyer.drain()

    def test_a_replace_is_confirmed(self):
        self.buyer.replace("B2", "B1", quantity=300, price="2846")

        report = self.buyer.reports()[0]

        self.assertEqual(D.ExecType.REPLACED, report.get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.REPLACED, report.get(D.ORD_STATUS))
        self.assertEqual("B2", report.get(D.CL_ORD_ID))
        self.assertEqual("B1", report.get(D.ORIG_CL_ORD_ID))
        self.assertEqual("300", report.get(D.ORDER_QTY))
        self.assertEqual("2846.0", report.get(D.PRICE))

    def test_a_replace_that_crosses_executes(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=200,
                              price="2850")
        self.seller.drain()

        self.buyer.replace("B2", "B1", quantity=200, price="2850")

        reports = self.buyer.reports()
        self.assertEqual(D.ExecType.REPLACED, reports[0].get(D.EXEC_TYPE))
        self.assertEqual(D.ExecType.FILL, reports[1].get(D.EXEC_TYPE))

    def test_an_invalid_price_produces_a_cancel_reject(self):
        self.buyer.replace("B2", "B1", price="3346")

        rejects = [m for m in self.buyer.received()
                   if m.msg_type == C.ORDER_CANCEL_REJECT]

        self.assertEqual(str(D.CxlRejReason.PRICE_EXCEEDS_BAND),
                         rejects[0].get(D.CXL_REJ_REASON))
        self.assertEqual(D.CxlRejResponseTo.CANCEL_REPLACE_REQUEST,
                         rejects[0].get(D.CXL_REJ_RESPONSE_TO))

    def test_a_superseded_clordid_is_no_longer_valid(self):
        self.buyer.replace("B2", "B1", quantity=300)
        self.buyer.drain()

        self.buyer.cancel("B3", "B1")

        rejects = [m for m in self.buyer.received()
                   if m.msg_type == C.ORDER_CANCEL_REJECT]
        self.assertEqual(str(D.CxlRejReason.UNKNOWN_ORDER),
                         rejects[0].get(D.CXL_REJ_REASON))


class SelfTradePreventionTest(VenueTestCase):

    def test_same_mpid_orders_are_prevented_and_flagged(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846", mpid="100000001")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=100, price="2846",
                             mpid="100000001")

        report = self.buyer.reports()[0]

        self.assertEqual(D.ExecType.CANCELED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.ExecRestatementReason.TRADE_PREVENTION),
                         report.get(D.EXEC_RESTATEMENT_REASON))
        self.assertEqual("0", report.get(D.CUM_QTY))

    def test_different_mpids_trade_normally(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846", mpid="100000002")
        self.seller.drain()

        self.buyer.new_order("B1", quantity=100, price="2846",
                             mpid="100000001")

        self.assertEqual(D.ExecType.FILL,
                         self.buyer.reports()[0].get(D.EXEC_TYPE))

    def test_cancel_oldest_notifies_the_resting_order_owner(self):
        # With Cancel Oldest the resting order is cancelled -- and it may
        # belong to a different session, so the report must be routed by owner.
        harness = VenueHarness(venue_config(
            markets=[{"name": "DAY", "state": "OPEN",
                      "stp_mode": "CANCEL_OLDEST"}],
            sessions=[{"target_comp_id": CLIENT1, "default_sub_id": "DAY",
                       "markets": ["DAY"]},
                      {"target_comp_id": CLIENT2, "default_sub_id": "DAY",
                       "markets": ["DAY"]}]))
        self.addCleanup(harness.close)
        first = harness.client(CLIENT1)
        second = harness.client(CLIENT2)

        first.new_order("A1", side=D.SideValue.SELL, quantity=100,
                        price="2846", mpid="100000001")
        first.drain()
        second.new_order("B1", quantity=100, price="2846", mpid="100000001")

        resting_report = first.reports()[0]
        self.assertEqual(D.ExecType.CANCELED, resting_report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.ExecRestatementReason.TRADE_PREVENTION),
                         resting_report.get(D.EXEC_RESTATEMENT_REASON))


class TradingStateTest(VenueTestCase):

    def test_closing_a_market_pushes_status_to_clients(self):
        self.harness.command("state.set", market="DAY", state="CLOSED")

        statuses = [m for m in self.buyer.received()
                    if m.msg_type == C.TRADING_SESSION_STATUS]

        self.assertEqual(1, len(statuses))
        self.assertEqual("DAY", statuses[0].get(D.TRADING_SESSION_ID))
        self.assertEqual(D.TradSesStatus.CLOSED, statuses[0].get(D.TRAD_SES_STATUS))

    def test_closing_a_market_expires_resting_orders_with_a_report(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        self.buyer.drain()

        self.harness.command("state.set", market="DAY", state="CLOSED")

        cancels = [m for m in self.buyer.received()
                   if m.msg_type == C.EXECUTION_REPORT
                   and m.get(D.EXEC_TYPE) == D.ExecType.CANCELED]
        self.assertEqual(1, len(cancels))
        self.assertEqual("B1", cancels[0].get(D.CL_ORD_ID))

    def test_halting_publishes_status_only_for_market_scope(self):
        self.harness.command("state.set", market="DAY", symbol=SYMBOL,
                             state="HALTED")

        statuses = [m for m in self.buyer.received()
                    if m.msg_type == C.TRADING_SESSION_STATUS]
        # A per-instrument halt is not a session-wide status change.
        self.assertEqual([], statuses)

    def test_a_venue_wide_change_touches_every_market(self):
        result = self.harness.command("state.set", state="OPEN")

        self.assertEqual("(all)", result["market"])
        states = dict((m["market"], m["state"])
                      for m in self.harness.command("markets")["markets"])
        self.assertEqual({"DAY": "OPEN", "NGHT": "OPEN"}, states)


class CancelOnDisconnectTest(VenueTestCase):

    def test_orders_are_cancelled_when_the_session_drops(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        self.buyer.drain()

        self.buyer.disconnect()

        orders = self.harness.command("orders")["orders"]
        self.assertEqual([], orders)

    def test_the_cancel_report_is_stored_for_replay_at_next_logon(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        self.buyer.drain()
        session = self.buyer.session
        before = session.store.next_out

        self.buyer.disconnect()

        # The report was numbered and persisted even though nobody was
        # connected, so a resend will deliver it.
        self.assertEqual(before + 1, session.store.next_out)
        stored = session.store.get_outbound(before, before)
        self.assertIn(b"378=12", stored[0][1])

    def test_a_session_without_the_feature_keeps_its_orders(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.seller.drain()

        self.seller.disconnect()

        orders = self.harness.command("orders")["orders"]
        self.assertEqual(1, len(orders))


class ControlCommandTest(VenueTestCase):

    def test_book_reflects_resting_orders(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=200,
                              price="2846")

        book = self.harness.command("book", market="DAY", symbol=SYMBOL)

        self.assertEqual([{"price": "2845.5", "quantity": 100, "orders": 1}],
                         book["bids"])
        self.assertEqual([{"price": "2846.0", "quantity": 200, "orders": 1}],
                         book["asks"])

    def test_bbo_reports_the_spread(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")

        bbo = self.harness.command("bbo", market="DAY", symbol=SYMBOL)

        self.assertEqual("2845.5", bbo["bid"])
        self.assertEqual("2846.0", bbo["ask"])
        self.assertEqual("0.5", bbo["spread"])

    def test_trades_lists_executions(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.buyer.new_order("B1", quantity=100, price="2846")

        trades = self.harness.command("trades", market="DAY", symbol=SYMBOL)

        self.assertEqual(1, len(trades["trades"]))
        self.assertEqual("2846.0", trades["trades"][0]["price"])

    def test_stats_summarises_the_session(self):
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")
        self.buyer.new_order("B1", quantity=100, price="2846")

        stats = self.harness.command("stats", market="DAY", symbol=SYMBOL)

        self.assertEqual("2846.0", stats["last"])
        self.assertEqual(100, stats["volume"])
        self.assertEqual(1, stats["trades"])

    def test_orders_can_be_filtered_by_session(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        self.seller.new_order("S1", side=D.SideValue.SELL, quantity=100,
                              price="2846")

        orders = self.harness.command("orders", session=CLIENT1)["orders"]

        self.assertEqual(1, len(orders))
        self.assertEqual("B1", orders[0]["cl_ord_id"])

    def test_an_administrative_cancel_notifies_the_owner(self):
        self.buyer.new_order("B1", quantity=100, price="2845.5")
        order_id = self.buyer.reports()[0].get(D.ORDER_ID)

        self.harness.command("order.cancel", order_id=order_id)

        report = self.buyer.reports()[0]
        self.assertEqual(D.ExecType.CANCELED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.ExecRestatementReason.OTHER),
                         report.get(D.EXEC_RESTATEMENT_REASON))

    def test_instrument_overrides_take_effect_immediately(self):
        self.harness.command("instrument.set", symbol=SYMBOL,
                             band_low="2800", band_high="2900")

        self.buyer.new_order("B1", price="2845.5")
        self.buyer.drain()
        self.buyer.new_order("B2", price="2950")

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         self.buyer.reports()[0].get(D.ORD_REJ_REASON))

    def test_an_unknown_symbol_is_reported_as_not_found(self):
        from exchangesim.control.commands import CommandError

        with self.assertRaises(CommandError) as caught:
            self.harness.command("book", market="DAY", symbol="0001")

        self.assertEqual("not_found", caught.exception.code)

    def test_stp_mode_can_be_changed_at_runtime(self):
        self.harness.command("stp.set", market="DAY", mode="CANCEL_OLDEST")

        markets = dict((m["market"], m) for m in
                       self.harness.command("markets")["markets"])
        self.assertEqual("CANCEL_OLDEST", markets["DAY"]["stp_mode"])


if __name__ == "__main__":
    unittest.main()
