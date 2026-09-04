"""NSE Capital Market: the venue, end to end over the real structures.

What the wire does with bytes is :mod:`tests.test_nnf_codec`'s business and what
a box does with a connection is :mod:`tests.test_nnf_session`'s. This is what
the venue does with an order.
"""

import unittest

from exchangesim.core.config import ConfigError
from exchangesim.core.enums import TradingState
from exchangesim.venues.nse import dictionary as D
from exchangesim.venues.nse import rules
from exchangesim.venues.nse import transactions as X

from tests.nsesupport import (
    BOX_ONE,
    BOX_TWO,
    BROKER_ONE,
    INSTRUMENT,
    USER_ONE,
    USER_THREE,
    USER_TWO,
    VenueHarness,
    codes,
    venue_config,
)


class VenueTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.venue = self.harness.venue

    def test_the_universe_loads(self):
        self.assertEqual(len(self.venue.instruments), 12)
        self.assertIn(INSTRUMENT, self.venue.instruments)

    def test_a_bare_symbol_resolves_when_one_series_is_listed(self):
        self.assertEqual(self.venue.resolve_symbol("INFY"), "INFY-EQ")
        self.assertEqual(self.venue.resolve_symbol("infy"), "INFY-EQ")
        self.assertEqual(self.venue.resolve_symbol("INFY-EQ"), "INFY-EQ")

    def test_an_unknown_security_resolves_to_nothing(self):
        self.assertIsNone(self.venue.resolve_symbol("NOSUCH"))
        self.assertIsNone(self.venue.resolve_symbol("INFY-XX"))
        self.assertIsNone(self.venue.resolve_symbol(""))

    def test_a_security_carries_its_own_circuit_filter(self):
        # 20 per cent of 1543.25, not the fallback table's absolute band.
        instrument = self.venue.instruments[INSTRUMENT]
        low, high = instrument.band_table.limits_for(instrument.base_price)
        self.assertEqual(self.venue.codec.format(low), "1234.60")
        self.assertEqual(self.venue.codec.format(high), "1851.90")

    def test_a_five_per_cent_scrip_gets_a_narrower_one(self):
        instrument = self.venue.instruments["IDEA-EQ"]
        low, high = instrument.band_table.limits_for(instrument.base_price)
        self.assertEqual(self.venue.codec.format(low), "13.02")
        self.assertEqual(self.venue.codec.format(high), "14.38")

    def test_the_tick_is_five_paise(self):
        self.assertEqual(self.venue.codec.format(self.venue.tick_size()), "0.05")

    def test_order_numbers_are_plain_increasing_decimals(self):
        # The client's only handle on an order, and what it cancels by.
        self.assertEqual([self.venue.order_ids.next() for _ in range(3)],
                         ["1", "2", "3"])

    def test_self_trade_prevention_is_refused_rather_than_honoured(self):
        # NNF Capital Market has none, and the core's mode keys on the
        # participant code, which means something else here.
        config = venue_config(markets=[{"name": "NORMAL", "state": "OPEN",
                                        "stp_mode": "CANCEL_NEWEST"}])
        self.assertRaises(ConfigError, VenueHarness, config)

    def test_more_than_one_market_is_refused(self):
        config = venue_config(markets=[{"name": "NORMAL", "state": "OPEN"},
                                       {"name": "ODDLOT", "state": "OPEN"}])
        self.assertRaises(ConfigError, VenueHarness, config)


class OrderEntryTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def enter(self, **kwargs):
        self.client.new_order(USER_ONE, **kwargs)
        return self.client.received()

    def test_an_order_is_acknowledged_before_it_can_trade(self):
        reports = self.enter(quantity=100, price="1540.00")
        self.assertEqual(codes([(int(m.msg_type), m) for m in reports]),
                         [X.ORDER_CONFIRMATION])
        confirmation = reports[0]
        self.assertEqual(confirmation.get(D.ERROR_CODE), "0")
        self.assertEqual(confirmation.get(D.ORDER_NUMBER), "1")
        self.assertEqual(confirmation.get(D.VOLUME), "100")
        self.assertEqual(confirmation.get(D.TOTAL_VOL_REMAINING), "100")
        self.assertEqual(confirmation.get(D.VOLUME_FILLED_TODAY), "0")

    def test_the_confirmation_names_the_security_as_two_fields(self):
        confirmation = self.enter(price="1540.00")[0]
        self.assertEqual(confirmation.get(D.SYMBOL), "INFY")
        self.assertEqual(confirmation.get(D.SERIES), "EQ")
        self.assertEqual(confirmation.get(D.ALPHA_CHAR), "IN")

    def test_the_members_own_reference_is_echoed_back(self):
        confirmation = self.enter(price="1540.00",
                                  extra={D.NNF_FIELD: "778899"})[0]
        self.assertEqual(confirmation.get(D.NNF_FIELD), "778899")

    def test_an_order_reaches_the_book(self):
        self.enter(quantity=100, price="1540.00")
        bbo = self.harness.command("bbo", symbol=INSTRUMENT, market="NORMAL")
        self.assertEqual(bbo["bid"], "1540.00")
        self.assertEqual(bbo["bid_qty"], 100)

    def test_the_price_travels_as_paise(self):
        self.enter(quantity=100, price="1540.05")
        bbo = self.harness.command("bbo", symbol=INSTRUMENT, market="NORMAL")
        self.assertEqual(bbo["bid"], "1540.05")

    def test_an_order_flag_is_read_off_the_bitfield(self):
        # NSE has no TimeInForce field: IOC is a bit of ST_ORDER_FLAGS. With an
        # empty book there is nothing to trade, so it is acknowledged and then
        # immediately expires -- and the acknowledgement matters, because it is
        # the only place the client learns the order number.
        reports = self.enter(quantity=50, price="1540.00",
                             extra={D.FLAG_IOC: "Y", D.FLAG_DAY: "N"})
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_CONFIRMATION, X.ORDER_CANCEL_CONFIRMATION])
        self.assertEqual(reports[0].get(D.FLAG_IOC), "Y")
        self.assertEqual(reports[1].get(D.REASON_CODE), "16388")


class RefusalTest(unittest.TestCase):
    """Everything this venue will not trade, and the code it answers with."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def refused(self, **kwargs):
        self.client.clear()
        self.client.new_order(USER_ONE, **kwargs)
        reports = self.client.received()
        self.assertTrue(reports, "nothing was answered")
        message = reports[-1]
        return int(message.msg_type), int(message.get(D.ERROR_CODE))

    def test_every_book_but_regular_lot_is_refused(self):
        for book in (D.BookType.SPECIAL_TERMS, D.BookType.STOP_LOSS,
                     D.BookType.ODD_LOT, D.BookType.SPOT, D.BookType.AUCTION):
            code, error = self.refused(extra={D.BOOK_TYPE: book})
            self.assertEqual(code, X.ORDER_ERROR, book)
            self.assertEqual(error, 16422, book)

    def test_all_or_none_is_refused(self):
        self.assertEqual(self.refused(extra={D.FLAG_AON: "Y"}),
                         (X.ORDER_ERROR, 16319))

    def test_minimum_fill_is_refused(self):
        self.assertEqual(self.refused(extra={D.FLAG_MF: "Y"}),
                         (X.ORDER_ERROR, 16320))

    def test_a_stop_loss_trigger_is_refused(self):
        self.assertEqual(self.refused(extra={D.FLAG_ON_STOP: "Y"}),
                         (X.ORDER_ERROR, 16422))
        self.assertEqual(self.refused(extra={D.TRIGGER_PRICE: "1500.00"}),
                         (X.ORDER_ERROR, 16423))

    def test_a_disclosed_quantity_is_refused(self):
        # The core has no replenishment concept, and accepting the field while
        # ignoring it would be worse than refusing it.
        self.assertEqual(self.refused(extra={D.DISCLOSED_VOL: "10"}),
                         (X.ORDER_ERROR, 16400))

    def test_good_till_cancelled_is_refused(self):
        self.assertEqual(self.refused(extra={D.FLAG_GTC: "Y"}),
                         (X.ORDER_ERROR, 16326))

    def test_an_unknown_security_is_refused(self):
        self.assertEqual(self.refused(symbol="NOSUCH"),
                         (X.ORDER_ERROR, 16012))

    def test_a_price_outside_the_circuit_filter_is_refused(self):
        self.assertEqual(self.refused(price="9999.00"),
                         (X.ORDER_ERROR, 16284))

    def test_an_off_tick_price_is_refused(self):
        self.assertEqual(self.refused(price="1543.27"),
                         (X.ORDER_ERROR, 16283))

    def test_an_order_on_a_closed_market_is_refused(self):
        self.harness.set_state(TradingState.CLOSED)
        self.assertEqual(self.refused(price="1540.00"),
                         (X.ORDER_ERROR, 16000))

    def test_a_transaction_code_the_venue_only_sends_drops_the_box(self):
        # A client sending us a TRADE_CONFIRMATION is not a validation error to
        # be answered -- there is no form of that transaction that means "no".
        # NNF has no Reject, so the connection goes, which is what the
        # specification does with traffic it cannot read.
        self.assertTrue(self.client.box.connected)
        self.client.send(X.TRADE_CONFIRMATION, user_id=USER_ONE)
        self.assertFalse(self.client.box.connected)


class TradingTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.one = self.harness.client(BOX_ONE, users=(USER_ONE, USER_TWO))
        self.two = self.harness.client(BOX_TWO, users=(USER_THREE,))

    def test_a_trade_reports_to_both_sides(self):
        self.one.new_order(USER_ONE, quantity=100, price="1540.00")
        self.one.clear()
        self.two.clear()
        self.two.new_order(USER_THREE, side=D.BuySell.SELL, quantity=40,
                           price="1540.00")

        maker = self.one.received()
        self.assertEqual([int(m.msg_type) for m in maker],
                         [X.TRADE_CONFIRMATION])
        self.assertEqual(maker[0].get(D.FILL_QTY), "40")
        self.assertEqual(maker[0].get(D.FILL_PRICE), "1540.00")
        self.assertEqual(maker[0].get(D.REMAINING_VOL), "60")

        taker = self.two.received()
        self.assertEqual([int(m.msg_type) for m in taker],
                         [X.ORDER_CONFIRMATION, X.TRADE_CONFIRMATION])
        self.assertEqual(taker[1].get(D.FILL_QTY), "40")
        self.assertEqual(taker[1].get(D.REMAINING_VOL), "0")

    def test_a_report_routes_to_the_owner_not_the_sender(self):
        # The resting order belongs to a user on a different box entirely.
        self.one.new_order(USER_ONE, quantity=100, price="1540.00")
        self.one.clear()
        self.two.new_order(USER_THREE, side=D.BuySell.SELL, quantity=10,
                           price="1540.00")
        self.assertEqual([int(m.msg_type) for m in self.one.received()],
                         [X.TRADE_CONFIRMATION])

    def test_execution_is_at_the_resting_price(self):
        self.one.new_order(USER_ONE, quantity=100, price="1540.00")
        self.one.clear()
        self.two.clear()
        self.two.new_order(USER_THREE, side=D.BuySell.SELL, quantity=10,
                           price="1500.00")
        fills = [m for m in self.two.received()
                 if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual(fills[0].get(D.FILL_PRICE), "1540.00")

    def test_a_sweep_reports_each_fill_with_its_own_running_totals(self):
        # The snapshot invariant: without it every fill would report the final
        # state rather than the state at its own moment.
        self.one.new_order(USER_ONE, quantity=30, price="1540.00")
        self.one.new_order(USER_TWO, quantity=30, price="1539.00")
        self.one.clear()
        self.two.clear()
        self.two.new_order(USER_THREE, side=D.BuySell.SELL, quantity=60,
                           price="1539.00")
        fills = [m for m in self.two.received()
                 if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual([m.get(D.FILL_PRICE) for m in fills],
                         ["1540.00", "1539.00"])
        self.assertEqual([m.get(D.VOLUME_FILLED_TODAY) for m in fills],
                         ["30", "60"])
        self.assertEqual([m.get(D.REMAINING_VOL) for m in fills], ["30", "0"])

    def test_a_market_order_takes_whatever_is_there(self):
        self.one.new_order(USER_ONE, quantity=100, price="1540.00")
        self.one.clear()
        self.two.clear()
        self.two.new_order(USER_THREE, side=D.BuySell.SELL, quantity=100,
                           price=None, extra={D.FLAG_MARKET: "Y"})
        reports = [int(m.msg_type) for m in self.two.received()]
        self.assertIn(X.TRADE_CONFIRMATION, reports)


class AmendAndCancelTest(unittest.TestCase):
    """Every request names the exchange's own order number."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.one = self.harness.client(BOX_ONE, users=(USER_ONE, USER_TWO))
        self.two = self.harness.client(BOX_TWO, users=(USER_THREE,))
        self.one.new_order(USER_ONE, quantity=100, price="1540.00")
        self.order_number = self.one.received()[-1].get(D.ORDER_NUMBER)
        self.one.clear()

    def test_an_order_is_amended_by_its_order_number(self):
        self.one.modify(USER_ONE, self.order_number, quantity=50,
                        price="1541.00")
        reports = self.one.received()
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_MOD_CONFIRMATION])
        self.assertEqual(reports[0].get(D.VOLUME), "50")
        self.assertEqual(reports[0].get(D.PRICE), "1541.00")
        self.assertEqual(reports[0].get(D.FLAG_MODIFIED), "Y")

    def test_an_order_is_cancelled_by_its_order_number(self):
        self.one.cancel(USER_ONE, self.order_number)
        reports = self.one.received()
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_CANCEL_CONFIRMATION])
        self.assertEqual(reports[0].get(D.MOD_CXL_BY), D.ModCxlBy.TRADER)
        self.assertEqual(
            self.harness.command("orders", market="NORMAL")["orders"], [])

    def test_another_user_on_the_same_box_cannot_cancel_it(self):
        # The ClOrdID index is keyed by session and carries ownership for free;
        # by_order_id is one global map, so the check has to be explicit.
        self.one.cancel(USER_TWO, self.order_number)
        reports = self.one.received()
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_CANCEL_REJECT])
        self.assertEqual(reports[0].get(D.ERROR_CODE), "16013")

    def test_a_user_on_another_box_cannot_cancel_it(self):
        self.two.clear()
        self.two.cancel(USER_THREE, self.order_number)
        reports = self.two.received()
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_CANCEL_REJECT])
        self.assertEqual(reports[0].get(D.ERROR_CODE), "16013")

    def test_an_unknown_order_number_is_refused(self):
        self.one.cancel(USER_ONE, "999999")
        self.assertEqual([int(m.msg_type) for m in self.one.received()],
                         [X.ORDER_CANCEL_REJECT])

    def test_cancelling_twice_is_refused_the_second_time(self):
        self.one.cancel(USER_ONE, self.order_number)
        self.one.clear()
        self.one.cancel(USER_ONE, self.order_number)
        self.assertEqual([int(m.msg_type) for m in self.one.received()],
                         [X.ORDER_CANCEL_REJECT])

    def test_amending_outside_the_circuit_filter_is_refused(self):
        self.one.modify(USER_ONE, self.order_number, price="9999.00")
        reports = self.one.received()
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_MOD_REJECT])
        self.assertEqual(reports[0].get(D.ERROR_CODE), "16284")


class DisconnectTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client(BOX_ONE, users=(USER_ONE, USER_TWO))

    def test_cancel_on_disconnect_removes_only_that_users_orders(self):
        self.client.new_order(USER_ONE, quantity=10, price="1540.00")
        self.client.new_order(USER_TWO, quantity=20, price="1539.00")
        self.assertEqual(
            len(self.harness.command("orders", market="NORMAL")["orders"]), 2)

        # USER_ONE is configured for cancel on disconnect; USER_TWO is not.
        self.client.box.disconnect("test")
        remaining = self.harness.command("orders", market="NORMAL")["orders"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["quantity"], 20)

    def test_a_market_close_expires_what_is_left(self):
        self.client.new_order(USER_ONE, quantity=10, price="1540.00")
        self.harness.set_state(TradingState.CLOSED)
        self.assertEqual(
            self.harness.command("orders", market="NORMAL")["orders"], [])


class ControlPlaneTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)

    def test_sessions_lists_users_and_boxes_lists_connections(self):
        sessions = self.harness.command("sessions")["sessions"]
        self.assertEqual(sorted(entry["target_comp_id"] for entry in sessions),
                         ["40521", "40522", "40777"])
        boxes = self.harness.command("boxes")["boxes"]
        self.assertEqual([entry["box_id"] for entry in boxes],
                         [BOX_ONE, BOX_TWO])

    def test_killing_a_box_signs_off_every_user_on_it(self):
        self.harness.client(BOX_ONE, users=(USER_ONE, USER_TWO))
        result = self.harness.command("box.kill", box_id=BOX_ONE)
        self.assertTrue(result["disconnected"])
        self.assertEqual(result["users"], 2)

    def test_session_reset_is_refused_with_an_explanation(self):
        # There is nothing to reset, and saying so beats reporting success for
        # something that did not happen.
        from exchangesim.control.commands import CommandError
        self.harness.client(BOX_ONE, users=(USER_ONE,))
        self.harness.client(BOX_ONE).box.disconnect("test")
        with self.assertRaises(CommandError) as caught:
            self.harness.dispatch("session.reset",
                                  {"target_comp_id": str(USER_ONE)})
        self.assertIn("download", str(caught.exception))

    def test_the_assumptions_are_reported(self):
        result = self.harness.command("venue.assumptions")
        self.assertTrue(result["assumptions"])
        self.assertTrue(result["not_implemented"])

    def test_the_transaction_table_is_reported(self):
        served = self.harness.command("transactions")["transactions"]
        by_code = {entry["code"]: entry for entry in served}
        self.assertEqual(by_code[X.BOARD_LOT_IN]["structure_bytes"], 290)
        self.assertTrue(by_code[X.BOARD_LOT_IN]["inbound"])
        self.assertFalse(by_code[X.TRADE_CONFIRMATION]["inbound"])

    def test_an_injected_order_never_collides_with_a_user_session(self):
        self.harness.command("order.new", market="NORMAL", symbol=INSTRUMENT,
                             side="BUY", quantity=10, price="1540.00")
        orders = self.harness.command("orders", market="NORMAL")["orders"]
        self.assertEqual(len(orders), 1)
        self.assertTrue(orders[0]["session"].startswith("control:"))

    def test_reference_data_reloads(self):
        result = self.harness.command("reference.reload")
        self.assertEqual(result["instruments"], 12)
        self.assertEqual(result["withdrawn"], [])


class AuditTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def test_traffic_is_recorded_with_its_protocol(self):
        self.client.new_order(USER_ONE, quantity=10, price="1540.00")
        entries = self.harness.command("audit")["entries"]
        self.assertTrue(entries)
        self.assertTrue(all(entry["protocol"] == "nnf" for entry in entries))

    def test_an_entry_names_its_transaction_and_its_security(self):
        self.client.new_order(USER_ONE, quantity=10, price="1540.00")
        entries = self.harness.command("audit")["entries"]
        inbound = [entry for entry in entries
                   if entry["type"] == str(X.BOARD_LOT_IN)]
        self.assertTrue(inbound)
        self.assertEqual(inbound[-1]["type_name"], "BOARD_LOT_IN")
        self.assertEqual(inbound[-1]["symbol"], "INFY")

    def test_the_sign_on_password_is_struck_out_of_the_dump(self):
        entries = self.harness.command("audit")["entries"]
        signon = [entry for entry in entries
                  if entry["type"] == str(X.SIGN_ON_REQUEST_IN)]
        self.assertTrue(signon)
        detail = self.harness.command("audit.entry", seq=signon[0]["seq"])
        self.assertNotIn("Nse@2026", str(detail))


if __name__ == "__main__":
    unittest.main()
