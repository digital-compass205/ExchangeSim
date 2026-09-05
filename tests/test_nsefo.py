"""NSE Futures & Options: the venue, end to end over the real structures.

The counterpart of :mod:`tests.test_nse`. What is genuinely new here is
contract identity -- an order names a contract through five ``CONTRACT_DESC``
fields, not a symbol string -- so that gets its own test class in addition to
everything ``test_nse.py`` already covers the shape of: box/user sign-on,
order entry/modify/cancel, trades, and every refusal with its published code.
"""

import unittest

from exchangesim.core.config import ConfigError
from exchangesim.venues.nsefo import dictionary as D
from exchangesim.venues.nsefo import rules
from exchangesim.venues.nsefo import transactions as X

from tests.nsefosupport import (
    BOX_ONE,
    BOX_TWO,
    BROKER_ONE,
    FUTURE,
    FUTURE_CANONICAL,
    OPTION,
    OPTION_CANONICAL,
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
        self.assertEqual(len(self.venue.instruments), 21)
        self.assertIn(FUTURE_CANONICAL, self.venue.instruments)
        self.assertIn(OPTION_CANONICAL, self.venue.instruments)

    def test_a_futures_contract_carries_its_own_lot_and_band(self):
        instrument = self.venue.instruments[FUTURE_CANONICAL]
        self.assertEqual(instrument.lot_size, 25)
        low, high = instrument.band_table.limits_for(instrument.base_price)
        self.assertEqual(self.venue.codec.format(low), "22860.00")
        self.assertEqual(self.venue.codec.format(high), "27940.00")

    def test_an_option_contract_carries_its_own_lot_and_band(self):
        instrument = self.venue.instruments[OPTION_CANONICAL]
        self.assertEqual(instrument.lot_size, 250)
        low, high = instrument.band_table.limits_for(instrument.base_price)
        self.assertEqual(self.venue.codec.format(low), "40.68")
        self.assertEqual(self.venue.codec.format(high), "49.72")

    def test_order_numbers_are_plain_increasing_decimals(self):
        self.assertEqual([self.venue.order_ids.next() for _ in range(3)],
                         ["1", "2", "3"])

    def test_self_trade_prevention_is_refused_rather_than_honoured(self):
        config = venue_config(markets=[{"name": "NORMAL", "state": "OPEN",
                                        "stp_mode": "CANCEL_NEWEST"}])
        self.assertRaises(ConfigError, VenueHarness, config)

    def test_more_than_one_market_is_refused(self):
        config = venue_config(markets=[{"name": "NORMAL", "state": "OPEN"},
                                       {"name": "ODDLOT", "state": "OPEN"}])
        self.assertRaises(ConfigError, VenueHarness, config)


class ContractIdentityTest(unittest.TestCase):
    """resolve_symbol: canonical in, canonical out; forgiving; unambiguous."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.venue = self.harness.venue

    def test_the_canonical_spelling_resolves_to_itself(self):
        self.assertEqual(self.venue.resolve_symbol(FUTURE_CANONICAL),
                         FUTURE_CANONICAL)
        self.assertEqual(self.venue.resolve_symbol(OPTION_CANONICAL),
                         OPTION_CANONICAL)

    def test_case_and_separators_are_forgiven(self):
        self.assertEqual(
            self.venue.resolve_symbol("nifty_futidx 24sep2026"),
            FUTURE_CANONICAL)
        self.assertEqual(
            self.venue.resolve_symbol("reliance-optstk-24sep2026-2500-ce"),
            OPTION_CANONICAL)

    def test_an_iso_expiry_is_forgiven(self):
        self.assertEqual(
            self.venue.resolve_symbol("NIFTY-FUTIDX-2026-09-24"),
            FUTURE_CANONICAL)
        self.assertEqual(
            self.venue.resolve_symbol("RELIANCE-OPTSTK-2026-09-24-2500-CE"),
            OPTION_CANONICAL)

    def test_a_strike_with_trailing_zeroes_is_forgiven(self):
        self.assertEqual(
            self.venue.resolve_symbol("RELIANCE-OPTSTK-24SEP2026-2500.00-CE"),
            OPTION_CANONICAL)

    def test_an_unambiguous_partial_resolves(self):
        # TCS-FUTSTK names exactly one expiry in the reference data.
        self.assertEqual(self.venue.resolve_symbol("TCS-FUTSTK"),
                         "TCS-FUTSTK-24SEP2026")

    def test_an_ambiguous_partial_is_refused(self):
        # NIFTY-FUTIDX names three expiries in the reference data.
        self.assertIsNone(self.venue.resolve_symbol("NIFTY-FUTIDX"))
        # RELIANCE-OPTSTK names both a CE and a PE at the same strike.
        self.assertIsNone(
            self.venue.resolve_symbol("RELIANCE-OPTSTK-24SEP2026"))

    def test_an_unknown_contract_resolves_to_nothing(self):
        self.assertIsNone(self.venue.resolve_symbol("NOSUCH-FUTIDX-24SEP2026"))
        self.assertIsNone(self.venue.resolve_symbol(""))

    def test_a_futures_strike_round_trips_as_the_sentinel(self):
        """CONTRACT_DESC.StrikePrice is -1 for a future, not zero."""
        harness = self.harness
        client = harness.client()
        client.new_order(USER_ONE, contract=FUTURE, price="25400.00")
        confirmation = client.received()[0]
        self.assertEqual(confirmation.get(D.STRIKE_PRICE), "-1")
        self.assertEqual(confirmation.get(D.OPTION_TYPE), D.OptionType.FUTURES)

    def test_an_options_strike_round_trips_as_a_real_price(self):
        harness = self.harness
        client = harness.client()
        client.new_order(USER_ONE, contract=OPTION, price="45.20")
        confirmation = client.received()[0]
        self.assertEqual(confirmation.get(D.STRIKE_PRICE), "2500.00")
        self.assertEqual(confirmation.get(D.OPTION_TYPE), D.OptionType.CALL)


class SessionTest(unittest.TestCase):
    """Box registration, box sign-on, user sign-on, heartbeat, sign-off."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)

    def test_a_user_signs_on_over_a_registered_box(self):
        # open() clears the transport once the sequence completes, so the
        # registration and box sign-on steps are driven by hand here to
        # inspect their replies.
        client = self.harness.client(users=())
        client.send(X.SECURE_BOX_REGISTRATION_REQUEST_IN,
                   {D.BOX_ID: client.box_id})
        client.send(X.BOX_SIGN_ON_REQUEST_IN,
                   {D.BOX_ID: client.box_id, D.BROKER_ID: client.broker_id})
        reports = client.reports()
        self.assertEqual(codes(reports),
                         [X.SECURE_BOX_REGISTRATION_REQUEST_OUT,
                          X.BOX_SIGN_ON_REQUEST_OUT])

        client.sign_on(USER_ONE)
        signon = client.last()
        self.assertEqual(int(signon.msg_type), X.SIGN_ON_REQUEST_OUT)
        self.assertEqual(signon.get(D.ERROR_CODE), "0")

    def test_a_second_user_signs_on_over_the_same_box(self):
        client = self.harness.client(users=(USER_ONE,))
        client.sign_on(USER_TWO)
        signon = client.last()
        self.assertEqual(signon.get(D.ERROR_CODE), "0")

    def test_heartbeat_is_echoed(self):
        client = self.harness.client()
        client.send(X.HEARTBEAT)
        echoed = client.last()
        self.assertEqual(int(echoed.msg_type), X.HEARTBEAT)

    def test_sign_off_disconnects_the_user(self):
        client = self.harness.client()
        client.send(X.SIGN_OFF_REQUEST_IN, user_id=USER_ONE)
        reply = client.last()
        self.assertEqual(int(reply.msg_type), X.SIGN_OFF_REQUEST_OUT)
        self.assertFalse(self.harness.venue.manager.boxes[0].user(USER_ONE))

    def test_box_disconnect_signs_off_every_user_on_it(self):
        client = self.harness.client(users=(USER_ONE, USER_TWO))
        box = self.harness.venue.manager.boxes[0]
        self.assertEqual(len(box.users), 2)
        box.disconnect("test")
        self.assertEqual(len(box.users), 0)


class OrderEntryTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def enter(self, **kwargs):
        self.client.new_order(USER_ONE, **kwargs)
        return self.client.received()

    def test_a_futures_order_is_acknowledged_before_it_can_trade(self):
        reports = self.enter(contract=FUTURE, quantity=25, price="25400.00")
        self.assertEqual(codes([(int(m.msg_type), m) for m in reports]),
                         [X.ORDER_CONFIRMATION])
        confirmation = reports[0]
        self.assertEqual(confirmation.get(D.ERROR_CODE), "0")
        self.assertEqual(confirmation.get(D.ORDER_NUMBER), "1")
        self.assertEqual(confirmation.get(D.VOLUME), "25")
        self.assertEqual(confirmation.get(D.TOTAL_VOL_REMAINING), "25")
        self.assertEqual(confirmation.get(D.VOLUME_FILLED_TODAY), "0")
        self.assertEqual(confirmation.get(D.SYMBOL), "NIFTY")
        self.assertEqual(confirmation.get(D.INSTRUMENT_NAME),
                         D.InstrumentName.FUTURES_INDEX)

    def test_an_options_order_is_acknowledged_before_it_can_trade(self):
        reports = self.enter(contract=OPTION, quantity=250, price="45.20")
        confirmation = reports[0]
        self.assertEqual(int(confirmation.msg_type), X.ORDER_CONFIRMATION)
        self.assertEqual(confirmation.get(D.SYMBOL), "RELIANCE")
        self.assertEqual(confirmation.get(D.INSTRUMENT_NAME),
                         D.InstrumentName.OPTIONS_STOCK)
        self.assertEqual(confirmation.get(D.STRIKE_PRICE), "2500.00")
        self.assertEqual(confirmation.get(D.OPTION_TYPE), D.OptionType.CALL)

    def test_an_order_reaches_the_book(self):
        self.enter(contract=FUTURE, quantity=25, price="25400.00")
        bbo = self.harness.command("bbo", symbol=FUTURE_CANONICAL,
                                   market="NORMAL")
        self.assertEqual(bbo["bid"], "25400.00")
        self.assertEqual(bbo["bid_qty"], 25)

    def test_amend_by_order_number(self):
        confirmation = self.enter(contract=FUTURE, quantity=25,
                                  price="25400.00")[0]
        order_number = confirmation.get(D.ORDER_NUMBER)
        self.client.clear()
        self.client.modify(USER_ONE, order_number, quantity=50)
        reply = self.client.last()
        self.assertEqual(int(reply.msg_type), X.ORDER_MOD_CONFIRMATION)
        self.assertEqual(reply.get(D.VOLUME), "50")

    def test_cancel_by_order_number(self):
        confirmation = self.enter(contract=FUTURE, quantity=25,
                                  price="25400.00")[0]
        order_number = confirmation.get(D.ORDER_NUMBER)
        self.client.clear()
        self.client.cancel(USER_ONE, order_number)
        reply = self.client.last()
        self.assertEqual(int(reply.msg_type), X.ORDER_CANCEL_CONFIRMATION)
        self.assertEqual(reply.get(D.TOTAL_VOL_REMAINING), "0")

    def test_an_ioc_partial_fill_has_its_balance_cancelled(self):
        # Quantities are whole lots (25 each) -- NIFTY-FUTIDX's lot size --
        # so the resting side offers one lot and the IOC asks for two.
        other = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))
        other.new_order(USER_THREE, side=D.BuySell.SELL, contract=FUTURE,
                        quantity=25, price="25400.00")
        other.clear()

        self.client.clear()
        reports = self.enter(contract=FUTURE, quantity=50, price="25400.00",
                             extra={D.FLAG_IOC: "Y", D.FLAG_DAY: "N"})
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_CONFIRMATION, X.TRADE_CONFIRMATION,
                          X.ORDER_CANCEL_CONFIRMATION])
        cancellation = reports[-1]
        self.assertEqual(cancellation.get(D.VOLUME_FILLED_TODAY), "25")
        self.assertEqual(cancellation.get(D.TOTAL_VOL_REMAINING), "0")


class TradeTest(unittest.TestCase):
    """A trade between two members, each side's own running totals."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.resting = self.harness.client(box_id=BOX_ONE, users=(USER_ONE,))
        self.aggressor = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))

    def test_a_trade_reports_correctly_to_both_sides(self):
        self.resting.new_order(USER_ONE, side=D.BuySell.SELL, contract=FUTURE,
                               quantity=25, price="25400.00")
        self.resting.clear()

        self.aggressor.new_order(USER_THREE, side=D.BuySell.BUY,
                                 contract=FUTURE, quantity=25,
                                 price="25400.00")
        aggressor_reports = self.aggressor.received()
        self.assertEqual([int(m.msg_type) for m in aggressor_reports],
                         [X.ORDER_CONFIRMATION, X.TRADE_CONFIRMATION])
        fill = aggressor_reports[-1]
        self.assertEqual(fill.get(D.FILL_QTY), "25")
        self.assertEqual(fill.get(D.VOLUME_FILLED_TODAY), "25")
        self.assertEqual(fill.get(D.TOTAL_VOL_REMAINING), "0")

        # The resting side never sent a message that caused this; its report
        # still arrives on its own box.
        resting_reports = self.resting.received()
        self.assertEqual([int(m.msg_type) for m in resting_reports],
                         [X.TRADE_CONFIRMATION])
        resting_fill = resting_reports[0]
        self.assertEqual(resting_fill.get(D.FILL_QTY), "25")
        self.assertEqual(resting_fill.get(D.VOLUME_FILLED_TODAY), "25")
        self.assertEqual(resting_fill.get(D.TOTAL_VOL_REMAINING), "0")


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
        cases = {
            D.BookType.SPECIAL_TERMS: 16406,
            D.BookType.NEGOTIATED: 16561,
            D.BookType.ODD_LOT: 16406,
            D.BookType.SPOT: 16406,
            D.BookType.AUCTION: 16406,
        }
        for book, error_code in cases.items():
            code, error = self.refused(
                contract=FUTURE, price="25400.00",
                extra={D.BOOK_TYPE: book})
            self.assertEqual(code, X.ORDER_ERROR)
            self.assertEqual(error, error_code)

    def test_stop_loss_and_mit_get_their_own_codes(self):
        code, error = self.refused(
            contract=FUTURE, price="25400.00",
            extra={D.BOOK_TYPE: D.BookType.STOP_LOSS_OR_MIT, D.FLAG_SL: "Y"})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16445))

        code, error = self.refused(
            contract=FUTURE, price="25400.00",
            extra={D.BOOK_TYPE: D.BookType.STOP_LOSS_OR_MIT, D.FLAG_MIT: "Y"})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16446))

    def test_all_or_none_is_refused(self):
        code, error = self.refused(contract=FUTURE, price="25400.00",
                                   extra={D.FLAG_AON: "Y"})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16319))

    def test_minimum_fill_is_refused(self):
        code, error = self.refused(contract=FUTURE, price="25400.00",
                                   extra={D.FLAG_MF: "Y"})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16320))

    def test_disclosed_quantity_is_refused(self):
        code, error = self.refused(contract=FUTURE, price="25400.00",
                                   extra={D.DISCLOSED_VOL: 5})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16400))

    def test_good_till_cancelled_is_refused(self):
        code, error = self.refused(contract=FUTURE, price="25400.00",
                                   extra={D.FLAG_GTC: "Y", D.FLAG_DAY: "N"})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16667))

    def test_give_up_is_refused(self):
        code, error = self.refused(
            contract=FUTURE, price="25400.00",
            extra={D.COUNTERPARTY_BROKER_ID: "10999"})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16415))

    def test_self_trade_prevention_flag_is_refused(self):
        code, error = self.refused(
            contract=FUTURE, price="25400.00",
            extra={D.FLAG_STPC_ADDITIONAL: "Y"})
        self.assertEqual((code, error), (X.ORDER_ERROR, 16415))

    def test_an_off_tick_price_is_refused(self):
        code, error = self.refused(contract=FUTURE, price="25400.02")
        self.assertEqual(code, X.ORDER_ERROR)
        self.assertEqual(error, 16283)          # OE_PRICE_NOT_MULT

    def test_a_price_outside_the_circuit_filter_is_refused(self):
        code, error = self.refused(contract=FUTURE, price="30000.00")
        self.assertEqual(code, X.ORDER_ERROR)
        self.assertEqual(error, 16284)          # OE_PRICE_EXCEEDS_DAY_MIN_MAX

    def test_an_unknown_contract_is_refused(self):
        self.client.clear()
        self.client.new_order(USER_ONE, contract=dict(
            FUTURE, symbol="NOSUCH"), price="25400.00")
        reports = self.client.received()
        message = reports[-1]
        self.assertEqual(int(message.msg_type), X.ORDER_ERROR)
        self.assertEqual(int(message.get(D.ERROR_CODE)), 16012)

    def test_spread_orders_are_refused(self):
        self.client.clear()
        self.client.raw(2100, user_id=USER_ONE)          # SP_BOARD_LOT_IN
        reply = self.client.last()
        self.assertEqual(int(reply.msg_type), X.ORDER_ERROR)
        self.assertEqual(int(reply.get(D.ERROR_CODE)), rules.SPREAD_NOT_ALLOWED)

    def test_two_leg_orders_are_refused(self):
        self.client.clear()
        self.client.raw(2102, user_id=USER_ONE)          # TWOL_BOARD_LOT_IN
        reply = self.client.last()
        self.assertEqual(int(reply.msg_type), X.ORDER_ERROR)
        self.assertEqual(int(reply.get(D.ERROR_CODE)),
                         rules.BAD_TRANSACTION_CODE)


if __name__ == "__main__":
    unittest.main()
