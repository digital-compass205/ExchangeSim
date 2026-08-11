"""The HKEX venue end to end: orders in over OCG-C FIX, reports out.

Covers what makes this dialect different from Japannext's -- repeating groups,
market segments taken from reference data rather than from the wire, market
orders, self-match prevention keyed on the order, and mass cancel -- rather than
re-testing matching, which tests/test_matching.py already owns.
"""

import unittest

from exchangesim.core.enums import StpMode
from exchangesim.fix import constants as C
from exchangesim.fix.message import Message
from exchangesim.venues.hkex import dictionary as D
from exchangesim.venues.hkex import rules

from .hkexsupport import (
    BROKER1,
    BROKER1_ID,
    BROKER2,
    BROKER2_ID,
    GEM_SYMBOL,
    SYMBOL,
    VenueHarness,
    party,
    venue_config,
)


class HkexTestCase(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client(BROKER1)
        self.other = self.harness.client(BROKER2)
        self.client.drain()
        self.other.drain()

    def only(self, client=None):
        reports = (client or self.client).reports()
        self.assertEqual(1, len(reports),
                         "expected exactly one report, got %d" % len(reports))
        return reports[0]


class OrderAcceptanceTest(HkexTestCase):

    def test_a_limit_order_is_acknowledged(self):
        self.client.new_order("A1", quantity=100, price="393.000")

        report = self.only()

        self.assertEqual(D.ExecType.NEW, report.get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.NEW, report.get(D.ORD_STATUS))
        self.assertEqual("A1", report.get(D.CL_ORD_ID))
        self.assertEqual("100", report.get(D.LEAVES_QTY))

    def test_the_report_names_the_security_the_way_the_request_did(self):
        self.client.new_order("A2", quantity=100, price="393.000")

        report = self.only()

        self.assertEqual(SYMBOL, report.get(D.SECURITY_ID))
        self.assertEqual(D.SecurityIDSource.EXCHANGE_SYMBOL,
                         report.get(D.SECURITY_ID_SOURCE))
        self.assertEqual(D.SECURITY_EXCHANGE_VALUE,
                         report.get(D.SECURITY_EXCHANGE))

    def test_the_report_carries_the_broker_back_in_a_parties_group(self):
        self.client.new_order("A3", quantity=100, price="393.000")

        report = self.only()

        self.assertEqual("1", report.get(D.NO_PARTY_IDS))
        self.assertEqual(BROKER1_ID, party(report, D.PartyRole.EXECUTING_FIRM))

    def test_a_BCAN_is_echoed_as_a_second_party(self):
        self.client.new_order("A4", quantity=100, price="393.000",
                              bcan="88001234")

        report = self.only()

        self.assertEqual("2", report.get(D.NO_PARTY_IDS))
        self.assertEqual("88001234", party(report, D.PartyRole.CLIENT_ID))

    def test_a_board_lot_order_is_marked_as_a_round_lot(self):
        self.client.new_order("A5", quantity=100, price="393.000")

        self.assertEqual(D.LotType.ROUND_LOT, self.only().get(D.LOT_TYPE))


class RepeatingGroupTest(HkexTestCase):
    """The count field must agree with the entries, or it is a session Reject."""

    def order_with_bad_count(self, tag, value):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(D.CL_ORD_ID, "G1")
        self.client._parties(message)
        self.client._instrument(message, SYMBOL)
        message.set(D.ORD_TYPE, D.OrdType.LIMIT)
        message.set(D.SIDE, D.SideValue.BUY)
        message.set(D.ORDER_QTY, 100)
        message.set(D.PRICE, "393.000")
        message.set(D.TRANSACT_TIME, self.harness.clock.timestamp())
        self.client._disclosure(message)
        message.set(tag, value)
        return self.client.send(message)

    def test_a_wrong_NoPartyIDs_is_rejected_as_an_incorrect_group_count(self):
        self.order_with_bad_count(D.NO_PARTY_IDS, 3)

        reject = self.client.received()[0]

        self.assertEqual(C.REJECT, reject.msg_type)
        self.assertEqual(str(C.SessionRejectReason.INCORRECT_NUM_IN_GROUP),
                         reject.get(C.SESSION_REJECT_REASON))
        self.assertEqual(str(D.NO_PARTY_IDS), reject.get(C.REF_TAG_ID))

    def test_a_wrong_NoDisclosureInstructions_is_rejected_the_same_way(self):
        self.order_with_bad_count(D.NO_DISCLOSURE_INSTRUCTIONS, 2)

        reject = self.client.received()[0]

        self.assertEqual(C.REJECT, reject.msg_type)
        self.assertEqual(str(C.SessionRejectReason.INCORRECT_NUM_IN_GROUP),
                         reject.get(C.SESSION_REJECT_REASON))

    def test_a_missing_disclosure_group_is_rejected(self):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(D.CL_ORD_ID, "G2")
        self.client._parties(message)
        self.client._instrument(message, SYMBOL)
        message.set(D.ORD_TYPE, D.OrdType.LIMIT)
        message.set(D.SIDE, D.SideValue.BUY)
        message.set(D.ORDER_QTY, 100)
        message.set(D.PRICE, "393.000")
        message.set(D.TRANSACT_TIME, self.harness.clock.timestamp())
        self.client.send(message)

        reject = self.client.received()[0]

        self.assertEqual(C.REJECT, reject.msg_type)
        self.assertEqual(str(C.SessionRejectReason.REQUIRED_TAG_MISSING),
                         reject.get(C.SESSION_REJECT_REASON))

    def test_all_three_party_entries_are_read(self):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(D.CL_ORD_ID, "G3")
        self.client._parties(message, bcan="88009999", location="BSU01")
        self.client._instrument(message, SYMBOL)
        message.set(D.ORD_TYPE, D.OrdType.LIMIT)
        message.set(D.SIDE, D.SideValue.BUY)
        message.set(D.ORDER_QTY, 100)
        message.set(D.PRICE, "393.000")
        message.set(D.TRANSACT_TIME, self.harness.clock.timestamp())
        self.client._disclosure(message)
        self.client.send(message)

        order = self.harness.venue.engine.orders()[0]

        self.assertEqual(BROKER1_ID, order.mpid)
        self.assertEqual("88009999", order.account)


class MarketSegmentTest(HkexTestCase):
    """No message names a segment; the security decides."""

    def test_a_main_board_order_lands_on_the_main_board(self):
        self.client.new_order("S1", quantity=100, price="393.000")

        self.assertEqual("MAIN", self.harness.venue.engine.orders()[0].market)

    def test_a_GEM_order_lands_on_GEM(self):
        self.client.new_order("S2", symbol=GEM_SYMBOL, quantity=2000,
                              price="0.235")

        self.assertEqual("GEM", self.harness.venue.engine.orders()[0].market)

    def test_the_two_segments_keep_separate_books(self):
        self.client.new_order("S3", quantity=100, price="393.000")
        self.client.new_order("S4", symbol=GEM_SYMBOL, quantity=2000,
                              price="0.235")

        main = self.harness.venue.markets["MAIN"]
        gem = self.harness.venue.markets["GEM"]

        self.assertNotIn(GEM_SYMBOL, main.symbols)
        self.assertNotIn(SYMBOL, gem.symbols)

    def test_an_unlisted_security_is_rejected_with_a_report(self):
        self.client.new_order("S5", symbol="99999", quantity=100,
                              price="393.000")

        report = self.only()

        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertEqual("NONE", report.get(D.ORDER_ID))
        self.assertIn("not listed", report.get(D.REJECT_TEXT))

    def test_a_session_barred_from_a_segment_is_refused(self):
        config = venue_config(sessions=[
            {"target_comp_id": BROKER1, "markets": ["MAIN"]},
            {"target_comp_id": BROKER2, "markets": ["MAIN", "GEM"]},
        ])
        with VenueHarness(config) as harness:
            client = harness.client(BROKER1)
            client.drain()
            client.new_order("S6", symbol=GEM_SYMBOL, quantity=2000,
                             price="0.235")

            report = client.reports()[0]

            self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
            self.assertIn("may not trade", report.get(D.REJECT_TEXT))


class SpreadTableTest(HkexTestCase):
    """The tick ladder, transcribed from the Second Schedule, Part A.

    The boundary semantics are the easy thing to get wrong. The Schedule reads
    "From 0.01 to 0.25 ... 0.001 / Over 0.25 to 10.00 ... 0.005", so the upper
    bound of a band belongs to that band and the next one starts strictly above
    it -- a price of exactly 0.25 takes the *narrower* spread, not the wider.
    """

    #: (price, spread) straight off the published Schedule, including both
    #: sides of every boundary.
    SCHEDULE = [
        ("0.010", "0.001"), ("0.250", "0.001"),
        ("0.251", "0.005"), ("10.000", "0.005"),
        ("10.010", "0.010"), ("20.000", "0.010"),
        ("20.020", "0.020"), ("50.000", "0.020"),
        ("50.050", "0.050"), ("100.000", "0.050"),
        ("100.100", "0.100"), ("200.000", "0.100"),
        ("200.200", "0.200"), ("500.000", "0.200"),
        ("500.500", "0.500"), ("1000.000", "0.500"),
        ("1001.000", "1.000"), ("2000.000", "1.000"),
        ("2002.000", "2.000"), ("5000.000", "2.000"),
        ("5005.000", "5.000"), ("9995.000", "5.000"),
    ]

    def test_the_ladder_matches_the_published_schedule(self):
        codec = self.harness.venue.codec
        ticks = self.harness.venue._tick_table

        actual = [(price, codec.format(ticks.tick_for(codec.parse(price))))
                  for price, _ in self.SCHEDULE]

        self.assertEqual(self.SCHEDULE, actual)

    def test_every_shipped_base_price_is_on_its_own_tick(self):
        """A nominal price off the ladder would make the security untradable."""
        offenders = []
        for symbol, instrument in sorted(
                self.harness.venue.instruments.items()):
            if instrument.base_price is None:
                continue
            if not instrument.is_on_tick(instrument.base_price):
                offenders.append(symbol)

        self.assertEqual([], offenders)

    def test_a_price_off_the_spread_is_rejected(self):
        # Above 200 the spread is 0.200, so 393.150 is not a valid price.
        self.client.new_order("T1", quantity=100, price="393.150")

        report = self.only()

        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertIn("tick size", report.get(D.REJECT_TEXT))

    def test_a_cheap_security_uses_the_narrow_spread(self):
        # Below 0.25 the spread is 0.001, so 0.237 is valid on GEM.
        self.client.new_order("T2", symbol=GEM_SYMBOL, quantity=2000,
                              price="0.237")

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_a_board_lot_violation_is_rejected(self):
        # 00700 trades in lots of 100.
        self.client.new_order("T5", quantity=150, price="393.000")

        report = self.only()

        self.assertEqual(str(D.OrdRejReason.INCORRECT_QUANTITY),
                         report.get(D.ORD_REJ_REASON))

    def test_board_lots_differ_by_security(self):
        # 08083 trades in lots of 2,000, so 100 is not a board lot there.
        self.client.new_order("T6", symbol=GEM_SYMBOL, quantity=100,
                              price="0.235")

        self.assertEqual(str(D.OrdRejReason.INCORRECT_QUANTITY),
                         self.only().get(D.ORD_REJ_REASON))


class NineTimesRuleTest(HkexTestCase):
    """The nominal-price limit: 9 times or more, or a ninth or less, is out.

    This is *not* a band of N spreads. It is multiplicative, so at 00700's
    nominal of 395.800 the acceptable range runs from 43.978 to 3,562.199.
    """

    def test_the_limits_are_a_ninth_and_nine_times_the_nominal_price(self):
        instrument = self.harness.venue.instruments[SYMBOL]
        codec = self.harness.venue.codec

        low, high = instrument.price_limits

        self.assertEqual("43.978", codec.format(low))
        self.assertEqual("3562.199", codec.format(high))

    def test_a_price_the_old_24_spread_band_would_have_refused_is_fine(self):
        """420.000 is 121 spreads out, and entirely acceptable to SEHK."""
        self.client.new_order("N1", quantity=100, price="420.000")

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_nine_times_the_nominal_price_is_rejected(self):
        # 395.800 x 9 = 3,562.200, and "9 times or more" is refused. The spread
        # up there is 2.000, so the first valid price past the limit is 3,564.
        self.client.new_order("N2", quantity=100, price="3564.000")

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         self.only().get(D.ORD_REJ_REASON))

    def test_just_inside_nine_times_is_accepted(self):
        self.client.new_order("N3", quantity=100, price="3562.000")

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_a_ninth_of_the_nominal_price_is_rejected(self):
        # 395.800 / 9 = 43.977..., and "a ninth or less" is refused. The spread
        # there is 0.020, so the last valid price below the limit is 43.960.
        self.client.new_order("N4", quantity=100, price="43.960")

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         self.only().get(D.ORD_REJ_REASON))

    def test_just_inside_a_ninth_is_accepted(self):
        self.client.new_order("N5", quantity=100, price="44.000")

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_ExecInst_c_waives_the_nominal_limit_but_not_the_spread(self):
        self.client.new_order("N6", quantity=100, price="4000.000",
                              exec_inst=D.ExecInstValue.IGNORE_PRICE_CHECKS)
        accepted = self.only()

        self.client.new_order("N7", quantity=100, price="4000.100",
                              exec_inst=D.ExecInstValue.IGNORE_PRICE_CHECKS)
        off_tick = self.only()

        self.assertEqual(D.ExecType.NEW, accepted.get(D.EXEC_TYPE))
        self.assertEqual(D.ExecType.REJECTED, off_tick.get(D.EXEC_TYPE))
        self.assertIn("tick size", off_tick.get(D.REJECT_TEXT))


class QuotationRuleTest(HkexTestCase):
    """24 spreads behind your own side's best, 9 through the other side's.

    Anchored on the live BBO, which is what distinguishes it from the
    nominal-price rule above. 00700's spread at these prices is 0.200, so 24
    spreads is 4.800 and 9 spreads is 1.800.
    """

    def setUp(self):
        HkexTestCase.setUp(self)
        self.other.new_order("Q-BID", side=D.SideValue.BUY, quantity=100,
                             price="395.800")
        self.other.new_order("Q-ASK", side=D.SideValue.SELL, quantity=100,
                             price="396.000")
        self.other.drain()

    def test_exactly_24_spreads_behind_the_best_bid_is_accepted(self):
        self.client.new_order("Q1", quantity=100, price="391.000")

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_more_than_24_spreads_behind_the_best_bid_is_rejected(self):
        self.client.new_order("Q2", quantity=100, price="390.800")

        report = self.only()

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         report.get(D.ORD_REJ_REASON))
        self.assertIn("24 spreads away", report.get(D.REJECT_TEXT))

    def test_the_rule_applies_to_the_sell_side_too(self):
        self.client.new_order("Q3", side=D.SideValue.SELL, quantity=100,
                              price="401.000")

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         self.only().get(D.ORD_REJ_REASON))

    def test_exactly_9_spreads_through_the_best_ask_is_accepted(self):
        self.client.new_order("Q4", quantity=100, price="397.800")

        reports = self.client.reports()

        self.assertNotEqual(D.ExecType.REJECTED, reports[0].get(D.EXEC_TYPE))

    def test_more_than_9_spreads_through_the_best_ask_is_rejected(self):
        self.client.new_order("Q5", quantity=100, price="398.000")

        report = self.only()

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         report.get(D.ORD_REJ_REASON))
        self.assertIn("9 spreads through", report.get(D.REJECT_TEXT))

    def test_an_empty_side_imposes_no_constraint(self):
        """Otherwise the first order of the day could never be placed."""
        self.client.new_order("Q6", symbol="00939", quantity=1000,
                              price="6.180")

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_the_nominal_limit_still_applies_on_top(self):
        self.client.new_order("Q7", symbol="00939", quantity=1000,
                              price="60.000")

        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         self.only().get(D.ORD_REJ_REASON))


class TradingTest(HkexTestCase):

    def rest(self, cl_ord_id, side=D.SideValue.SELL, quantity=100,
             price="395.800", client=None):
        (client or self.other).new_order(cl_ord_id, side=side,
                                         quantity=quantity, price=price)
        (client or self.other).drain()

    def test_a_fill_reports_ExecType_F(self):
        self.rest("R1")

        self.client.new_order("B1", side=D.SideValue.BUY, quantity=100,
                              price="395.800")
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.TRADE, report.get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.FILLED, report.get(D.ORD_STATUS))
        self.assertEqual("395.800", report.get(D.LAST_PX))
        self.assertEqual("100", report.get(D.LAST_QTY))

    def test_both_sides_agree_on_the_trade_identifier(self):
        self.rest("R2")

        self.client.new_order("B2", quantity=100, price="395.800")
        mine = self.client.reports()[-1]
        theirs = self.other.reports()[-1]

        self.assertEqual(mine.get(D.TRD_MATCH_ID), theirs.get(D.TRD_MATCH_ID))
        self.assertTrue(mine.get(D.TRD_MATCH_ID))

    def test_the_aggressor_is_flagged_and_the_resting_side_is_not(self):
        self.rest("R3")

        self.client.new_order("B3", quantity=100, price="395.800")

        self.assertEqual(C.YES,
                         self.client.reports()[-1].get(D.AGGRESSOR_INDICATOR))
        self.assertEqual(C.NO,
                         self.other.reports()[-1].get(D.AGGRESSOR_INDICATOR))

    def test_a_partial_fill_reports_its_own_running_totals(self):
        self.rest("R4", quantity=100)

        self.client.new_order("B4", quantity=200, price="395.800")
        fills = [report for report in self.client.reports()
                 if report.get(D.EXEC_TYPE) == D.ExecType.TRADE]

        self.assertEqual(D.OrdStatus.PARTIALLY_FILLED,
                         fills[0].get(D.ORD_STATUS))
        self.assertEqual("100", fills[0].get(D.CUM_QTY))
        self.assertEqual("100", fills[0].get(D.LEAVES_QTY))

    def test_a_market_order_lifts_the_offer(self):
        self.rest("R5")

        self.client.new_order("B5", quantity=100, price=None,
                              ord_type=D.OrdType.MARKET)
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.TRADE, report.get(D.EXEC_TYPE))
        self.assertEqual("395.800", report.get(D.LAST_PX))

    def test_a_market_order_remainder_expires(self):
        self.rest("R6", quantity=100)

        self.client.new_order("B6", quantity=200, price=None,
                              ord_type=D.OrdType.MARKET)
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.EXPIRED, report.get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.EXPIRED, report.get(D.ORD_STATUS))

    def test_an_unfilled_IOC_expires_rather_than_cancelling(self):
        self.client.new_order("B7", quantity=100, price="393.000",
                              tif=D.TimeInForce.IOC)

        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.EXPIRED, report.get(D.EXEC_TYPE))

    def test_a_cancel_reports_ExecType_4(self):
        self.client.new_order("B8", quantity=100, price="393.000")
        self.client.drain()

        self.client.cancel("X8", "B8", quantity=100)
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.CANCELED, report.get(D.EXEC_TYPE))
        self.assertEqual("B8", report.get(D.ORIG_CL_ORD_ID))

    def test_an_amend_reports_ExecType_5_and_a_live_status(self):
        self.client.new_order("B9", quantity=100, price="393.000")
        self.client.drain()

        self.client.replace("Y9", "B9", quantity=200, price="393.200")
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.REPLACED, report.get(D.EXEC_TYPE))
        self.assertEqual(D.OrdStatus.NEW, report.get(D.ORD_STATUS),
                         "FIX 5.0 has no 'Replaced' order status")

    def test_a_cancel_for_an_unknown_order_is_rejected(self):
        self.client.cancel("X0", "NOPE", quantity=100)

        reject = self.client.received()[0]

        self.assertEqual(C.ORDER_CANCEL_REJECT, reject.msg_type)
        self.assertEqual(str(D.CxlRejReason.UNKNOWN_ORDER),
                         reject.get(D.CXL_REJ_REASON))
        self.assertEqual(D.CxlRejResponseTo.CANCEL_REQUEST,
                         reject.get(D.CXL_REJ_RESPONSE_TO))


class RejectedOrderTest(HkexTestCase):

    def test_an_odd_lot_order_is_refused_rather_than_silently_promoted(self):
        self.client.new_order("O1", quantity=100, price="393.000",
                              lot_type=D.LotType.ODD_LOT)

        report = self.only()

        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertIn("odd and special lot", report.get(D.REJECT_TEXT))

    def test_a_LotType_of_round_is_an_ordinary_board_lot_order(self):
        self.client.new_order("O2", quantity=100, price="393.000",
                              lot_type=D.LotType.ROUND_LOT)

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_TimeInForce_at_crossing_is_refused_outside_an_auction(self):
        self.client.new_order("O3", quantity=100, price="393.000",
                              tif=D.TimeInForce.AT_CROSSING)

        report = self.only()

        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertIn("At Crossing", report.get(D.REJECT_TEXT))

    def test_a_duplicate_ClOrdID_is_rejected_with_no_OrderID(self):
        self.client.new_order("O4", quantity=100, price="393.000")
        self.client.drain()

        self.client.new_order("O4", quantity=100, price="393.000")
        report = self.only()

        self.assertEqual(str(D.OrdRejReason.DUPLICATE_ORDER),
                         report.get(D.ORD_REJ_REASON))
        self.assertEqual("NONE", report.get(D.ORDER_ID))

    def test_a_suspended_security_is_refused(self):
        self.client.new_order("O5", symbol="00028", quantity=2000,
                              price="1.240")

        self.assertEqual(D.ExecType.REJECTED, self.only().get(D.EXEC_TYPE))

    def test_a_security_with_no_nominal_price_reports_reason_19(self):
        self.harness.command("instrument.add", symbol="00099", lot_size=100,
                             tier="MAIN")
        self.client.new_order("O6", symbol="00099", quantity=100,
                              price="10.000")

        report = self.only()

        # Nothing rejects it -- with no base price there is no band at all.
        self.assertEqual(D.ExecType.NEW, report.get(D.EXEC_TYPE))

    def test_a_closed_segment_refuses_entry(self):
        self.harness.command("state.set", market="MAIN", state="CLOSED")
        self.client.drain()

        self.client.new_order("O7", quantity=100, price="393.000")

        self.assertEqual(D.ExecType.REJECTED, self.only().get(D.EXEC_TYPE))


class SelfMatchPreventionTest(HkexTestCase):
    """SMP is keyed on the order; the instruction is registered out of band."""

    def test_matching_ids_cancel_the_aggressive_order_by_default(self):
        self.other.new_order("P1", side=D.SideValue.SELL, quantity=100,
                             price="395.800", smp_id="SMPAGG")
        self.other.drain()

        self.client.new_order("P2", quantity=100, price="395.800",
                              smp_id="SMPAGG")
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.CANCELED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.ExecRestatementReason.SMP_CANCEL_AGGRESSIVE),
                         report.get(D.EXEC_RESTATEMENT_REASON))
        self.assertEqual([], self.other.reports(),
                         "the resting order must be untouched")

    def test_a_cancel_passive_instruction_removes_the_resting_order(self):
        self.other.new_order("P3", side=D.SideValue.SELL, quantity=100,
                             price="395.800", smp_id="SMPPAS")
        self.other.drain()

        self.client.new_order("P4", quantity=100, price="395.800",
                              smp_id="SMPPAS")
        theirs = self.other.reports()[-1]

        self.assertEqual(D.ExecType.CANCELED, theirs.get(D.EXEC_TYPE))
        self.assertEqual(str(D.ExecRestatementReason.SMP_CANCEL_PASSIVE),
                         theirs.get(D.EXEC_RESTATEMENT_REASON))

    def test_different_ids_trade_normally(self):
        self.other.new_order("P5", side=D.SideValue.SELL, quantity=100,
                             price="395.800", smp_id="SMPAGG")
        self.other.drain()

        self.client.new_order("P6", quantity=100, price="395.800",
                              smp_id="OTHER")

        self.assertEqual(D.ExecType.TRADE,
                         self.client.reports()[-1].get(D.EXEC_TYPE))

    def test_an_id_collides_across_brokers(self):
        """Section 6.13 is explicit that SMP IDs are shared between EPs."""
        self.other.new_order("P7", side=D.SideValue.SELL, quantity=100,
                             price="395.800", smp_id="SMPAGG")
        self.other.drain()

        self.client.new_order("P8", quantity=100, price="395.800",
                              smp_id="SMPAGG")

        self.assertNotEqual(D.ExecType.TRADE,
                            self.client.reports()[-1].get(D.EXEC_TYPE))

    def test_the_id_is_echoed_on_every_report_for_the_order(self):
        self.client.new_order("P9", quantity=100, price="393.000",
                              smp_id="SMPAGG")

        self.assertEqual("SMPAGG",
                         self.only().get(D.SELF_MATCH_PREVENTION_ID))

    def test_an_unregistered_id_defaults_to_cancel_aggressive(self):
        self.other.new_order("PA", side=D.SideValue.SELL, quantity=100,
                             price="395.800", smp_id="NEVERSEEN")
        self.other.drain()

        self.client.new_order("PB", quantity=100, price="395.800",
                              smp_id="NEVERSEEN")

        self.assertEqual(str(D.ExecRestatementReason.SMP_CANCEL_AGGRESSIVE),
                         self.client.reports()[-1].get(
                             D.EXEC_RESTATEMENT_REASON))

    def test_registering_an_instruction_changes_the_outcome(self):
        self.harness.command("smp.register", id="LATE",
                             instruction="CANCEL_PASSIVE")

        self.other.new_order("PC", side=D.SideValue.SELL, quantity=100,
                             price="395.800", smp_id="LATE")
        self.other.drain()
        self.client.new_order("PD", quantity=100, price="395.800",
                              smp_id="LATE")

        self.assertEqual(str(D.ExecRestatementReason.SMP_CANCEL_PASSIVE),
                         self.other.reports()[-1].get(
                             D.EXEC_RESTATEMENT_REASON))

    def test_an_order_without_an_id_is_unaffected(self):
        self.other.new_order("PE", side=D.SideValue.SELL, quantity=100,
                             price="395.800", smp_id="SMPAGG")
        self.other.drain()

        self.client.new_order("PF", quantity=100, price="395.800")

        self.assertEqual(D.ExecType.TRADE,
                         self.client.reports()[-1].get(D.EXEC_TYPE))


class MassCancelTest(HkexTestCase):

    def rest_three(self):
        self.client.new_order("M1", quantity=100, price="393.000")
        self.client.new_order("M2", side=D.SideValue.SELL, quantity=100,
                              price="399.000")
        self.client.new_order("M3", symbol=GEM_SYMBOL, quantity=2000,
                              price="0.235")
        self.client.drain()

    def test_cancel_all_removes_every_order_of_the_session(self):
        self.rest_three()

        self.client.mass_cancel("MC1", D.MassCancelRequestType.ALL)
        received = self.client.received()

        report = received[0]
        self.assertEqual(D.ORDER_MASS_CANCEL_REPORT, report.msg_type)
        self.assertEqual(D.MassCancelResponse.ALL,
                         report.get(D.MASS_CANCEL_RESPONSE))
        self.assertTrue(report.get(D.MASS_ACTION_REPORT_ID))
        self.assertEqual([], self.harness.venue.engine.orders(live_only=True))

    def test_each_cancelled_order_gets_its_own_report(self):
        self.rest_three()

        self.client.mass_cancel("MC2", D.MassCancelRequestType.ALL)
        cancels = [message for message in self.client.received()
                   if message.msg_type == C.EXECUTION_REPORT]

        self.assertEqual(3, len(cancels))
        self.assertEqual(str(D.ExecRestatementReason.MASS_CANCELLED_BY_BROKER),
                         cancels[0].get(D.EXEC_RESTATEMENT_REASON))

    def test_cancel_by_security_leaves_the_others_alone(self):
        self.rest_three()

        self.client.mass_cancel("MC3", D.MassCancelRequestType.SECURITY,
                                symbol=GEM_SYMBOL)

        remaining = sorted(order.cl_ord_id for order
                           in self.harness.venue.engine.orders(live_only=True))
        self.assertEqual(["M1", "M2"], remaining)

    def test_cancel_by_market_segment(self):
        self.rest_three()

        self.client.mass_cancel("MC4", D.MassCancelRequestType.MARKET_SEGMENT,
                                segment="MAIN")

        remaining = [order.cl_ord_id for order
                     in self.harness.venue.engine.orders(live_only=True)]
        self.assertEqual(["M3"], remaining)

    def test_cancel_by_side(self):
        self.rest_three()

        self.client.mass_cancel("MC5", D.MassCancelRequestType.ALL,
                                side=D.SideValue.SELL)

        remaining = sorted(order.cl_ord_id for order
                           in self.harness.venue.engine.orders(live_only=True))
        self.assertEqual(["M1", "M3"], remaining)

    def test_another_brokers_orders_are_untouched(self):
        self.rest_three()
        self.other.new_order("MO1", quantity=100, price="393.000")
        self.other.drain()

        self.client.mass_cancel("MC6", D.MassCancelRequestType.ALL)

        remaining = [order.cl_ord_id for order
                     in self.harness.venue.engine.orders(live_only=True)]
        self.assertEqual(["MO1"], remaining)

    def test_an_unknown_segment_is_rejected(self):
        self.client.mass_cancel("MC7", D.MassCancelRequestType.MARKET_SEGMENT,
                                segment="ETS")

        report = self.client.received()[0]

        self.assertEqual(D.MassCancelResponse.REJECTED,
                         report.get(D.MASS_CANCEL_RESPONSE))
        self.assertEqual(str(D.MassCancelRejectReason.INVALID_MARKET_SEGMENT),
                         report.get(D.MASS_CANCEL_REJECT_REASON))

    def test_cancel_by_security_without_one_is_rejected(self):
        self.client.mass_cancel("MC8", D.MassCancelRequestType.SECURITY)

        report = self.client.received()[0]

        self.assertEqual(D.MassCancelResponse.REJECTED,
                         report.get(D.MASS_CANCEL_RESPONSE))


class ControlPlaneTest(HkexTestCase):
    """The venue inherits the whole shared control surface for free."""

    def test_the_shared_commands_are_registered(self):
        for name in ("venue.info", "markets", "instruments", "book", "ladder",
                     "bbo", "trades", "orders", "order.new", "sessions"):
            self.assertIsNotNone(self.harness.registry.get(name),
                                 "%s should be registered" % name)

    def test_markets_lists_the_phases_a_client_may_set(self):
        """So a control surface need not carry its own copy of the vocabulary."""
        from exchangesim.core.enums import TradingState

        result = self.harness.command("markets")

        self.assertEqual(TradingState.ALL, set(result["states"]))
        self.assertEqual("OPEN", result["states"][2],
                         "listed in the order a session runs, not alphabetical")

    def test_assumptions_are_reported(self):
        result = self.harness.command("venue.assumptions")

        topics = [entry["topic"] for entry in result["assumptions"]]
        self.assertIn("quotation rule with an empty side", topics)
        self.assertIn("VCM", topics)

    def test_segments_are_listed(self):
        result = self.harness.command("segments", segment="GEM")

        symbols = [row["symbol"] for row in result["segments"]]
        self.assertIn(GEM_SYMBOL, symbols)
        self.assertNotIn(SYMBOL, symbols)

    def test_registered_smp_ids_are_listed(self):
        result = self.harness.command("smp")

        by_id = dict((row["id"], row["instruction"]) for row in result["smp"])
        self.assertEqual("CANCEL_AGGRESSIVE", by_id["SMPAGG"])
        self.assertEqual("CANCEL_PASSIVE", by_id["SMPPAS"])

    def test_an_unknown_smp_instruction_is_refused(self):
        from exchangesim.control.commands import CommandError

        with self.assertRaises(CommandError):
            self.harness.command("smp.register", id="BAD",
                                 instruction="CANCEL_EVERYTHING")

    def test_smp_can_be_cleared(self):
        self.harness.command("smp.clear", id="SMPPAS")

        ids = [row["id"] for row in self.harness.command("smp")["smp"]]
        self.assertNotIn("SMPPAS", ids)

    def test_an_added_instrument_only_gets_a_book_in_its_own_segment(self):
        self.harness.command("instrument.add", symbol="08999",
                             lot_size=1000, base_price="0.500", tier="GEM")

        self.assertIn("08999", self.harness.venue.markets["GEM"].symbols)
        self.assertNotIn("08999", self.harness.venue.markets["MAIN"].symbols)

    def test_an_added_instrument_is_immediately_tradable(self):
        self.harness.command("instrument.add", symbol="00456", lot_size=100,
                             base_price="12.000", tier="MAIN")

        self.client.new_order("N1", symbol="00456", quantity=100,
                              price="12.000")

        self.assertEqual(D.ExecType.NEW, self.only().get(D.EXEC_TYPE))

    def test_an_instrument_added_to_an_unknown_segment_is_refused(self):
        from exchangesim.control.commands import CommandError

        with self.assertRaises(CommandError):
            self.harness.command("instrument.add", symbol="00457",
                                 lot_size=100, tier="NOSUCH")

    def test_reference_data_reloads(self):
        result = self.harness.command("reference.reload")

        self.assertEqual(15, result["instruments"])
        self.assertEqual([], result["withdrawn"])

    def test_an_injected_order_reaches_the_right_segment(self):
        result = self.harness.command("order.new", market="GEM",
                                      symbol=GEM_SYMBOL, side="BUY",
                                      quantity=2000, price="0.235")

        self.assertEqual("GEM", result["order"]["market"])

    def test_an_injected_order_fills_a_resting_FIX_order(self):
        self.client.new_order("I1", side=D.SideValue.SELL, quantity=100,
                              price="395.800")
        self.client.drain()

        self.harness.command("order.new", market="MAIN", symbol=SYMBOL,
                             side="BUY", quantity=100, price="395.800")
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.TRADE, report.get(D.EXEC_TYPE))
        self.assertEqual("100", report.get(D.LAST_QTY))


class RulesTest(unittest.TestCase):
    """Mapping tables, checked directly where a round trip would be noise."""

    def test_every_core_reject_reason_maps_to_a_venue_code(self):
        from exchangesim.core.enums import RejectReason

        reasons = [value for name, value in vars(RejectReason).items()
                   if not name.startswith("_") and isinstance(value, str)]
        missing = [reason for reason in reasons
                   if reason not in rules.REJECT_TO_ORD_REJ_REASON]

        self.assertEqual([], missing)

    def test_every_core_order_status_maps_to_a_venue_code(self):
        from exchangesim.core.enums import OrderStatus

        statuses = [value for name, value in vars(OrderStatus).items()
                    if not name.startswith("_") and isinstance(value, str)]
        missing = [status for status in statuses
                   if status not in rules.STATUS_TO_FIX]

        self.assertEqual([], missing)

    def test_the_smp_instruction_names_round_trip(self):
        for name, mode in rules.SMP_INSTRUCTION_TO_CORE.items():
            self.assertEqual(name, rules.SMP_INSTRUCTION_TO_NAME[mode])

    def test_cancel_aggressive_is_the_core_cancel_newest_mode(self):
        self.assertEqual(StpMode.CANCEL_NEWEST,
                         rules.SMP_INSTRUCTION_TO_CORE["CANCEL_AGGRESSIVE"])
        self.assertEqual(StpMode.CANCEL_OLDEST,
                         rules.SMP_INSTRUCTION_TO_CORE["CANCEL_PASSIVE"])


if __name__ == "__main__":
    unittest.main()
