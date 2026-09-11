"""NSE Futures & Options: the venue, end to end over the real structures.

The counterpart of :mod:`tests.test_nse`. What is genuinely new here is
contract identity -- an order names a contract through five ``CONTRACT_DESC``
fields, not a symbol string -- so that gets its own test class in addition to
everything ``test_nse.py`` already covers the shape of: box/user sign-on,
order entry/modify/cancel, trades, and every refusal with its published code.
"""

import unittest

from exchangesim.core.config import ConfigError
from exchangesim.core.enums import TradingState
from exchangesim.venues.nsefo import dictionary as D
from exchangesim.venues.nsefo import rules
from exchangesim.venues.nsefo import transactions as X

from tests.nsefosupport import (
    FAR_CANONICAL,
    FAR_FUTURE,
    NEAR_CANONICAL,
    NEAR_FUTURE,
    OTHER_SYMBOL_FUTURE,
    SPREAD_CANONICAL,
    UNCOMBINED_FUTURE,
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
        # 22 listed contracts, plus one instrument per spread combination --
        # a combination is a tradeable thing in its own right here, with a
        # book of its own quoted at the gap between its two legs.
        self.assertEqual(22 + len(self.venue.spreads),
                         len(self.venue.instruments))
        self.assertEqual(5, len(self.venue.spreads))
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


UNKNOWN_USER = 59999


class HeaderUserIdTest(unittest.TestCase):
    """Every message a user receives names that user in the message header.

    "TraderId -- This field should contain the user ID" is said of the
    MESSAGE_HEADER once (F&O 9.50 p.22) and holds for every structure that
    carries one, in both directions. It is not decoration: a real gateway reads
    the header to find the trader a response belongs to and indexes a container
    with the value, so a response that names its user only inside the
    structure's *body* hands that gateway user 0 -- which is what it did, going
    out of bounds on a sign-on response and taking the client down.

    The mistake is per-handler and silent, and four of them made it separately
    at this venue, so this drives every outbound type a signed-on user can
    provoke and checks them together rather than asserting one reply. The stamp
    itself now lives in ``NnfSession.send``, which is the one place that knows
    both the tag and the user.
    """

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)

    def names(self, client, user_id):
        """Assert every pending message names ``user_id``; return their codes."""
        messages = client.received()
        self.assertTrue(messages, "nothing was answered")
        for message in messages:
            self.assertEqual(
                str(user_id), message.get(D.USER_ID),
                "%s left the header's user id at %r"
                % (rules.describe_transaction(int(message.msg_type)),
                    message.get(D.USER_ID)))
        return set(int(message.msg_type) for message in messages)

    def test_every_reply_to_a_signed_on_user_names_them(self):
        client = self.harness.client(users=())
        client.sign_on(USER_ONE)
        seen = self.names(client, USER_ONE)

        client.new_order(USER_ONE, contract=FUTURE, quantity=25,
                         price="25400.00")
        seen |= self.names(client, USER_ONE)

        client.modify(USER_ONE, "1", quantity=50)
        seen |= self.names(client, USER_ONE)

        client.cancel(USER_ONE, "1")
        seen |= self.names(client, USER_ONE)

        # A refusal, which is the erroring form of the same transaction.
        client.new_order(USER_ONE, contract=FUTURE, price="25400.00",
                         extra={D.BOOK_TYPE: D.BookType.SPECIAL_TERMS})
        seen |= self.names(client, USER_ONE)

        client.send(X.SYSTEM_INFORMATION_IN, user_id=USER_ONE)
        seen |= self.names(client, USER_ONE)

        client.send(X.DOWNLOAD_REQUEST, user_id=USER_ONE)
        seen |= self.names(client, USER_ONE)

        client.send(X.SIGN_OFF_REQUEST_IN, user_id=USER_ONE)
        seen |= self.names(client, USER_ONE)

        self.assertEqual(
            {X.SIGN_ON_REQUEST_OUT, X.SYSTEM_INFORMATION_OUT,
             X.ORDER_CONFIRMATION, X.ORDER_MOD_CONFIRMATION,
             X.ORDER_CANCEL_CONFIRMATION, X.ORDER_ERROR,
             X.HEADER_RECORD, X.MESSAGE_RECORD, X.TRAILER_RECORD,
             X.SIGN_OFF_REQUEST_OUT},
            seen)

    def test_a_trade_report_names_the_side_it_is_sent_to(self):
        # The resting side never sent the message that caused this, so its
        # report is routed by the order's owner -- and must be addressed to
        # that owner, not to whoever triggered the match.
        resting = self.harness.client(box_id=BOX_ONE, users=(USER_ONE,))
        aggressor = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))
        resting.new_order(USER_ONE, side=D.BuySell.SELL, contract=FUTURE,
                          quantity=25, price="25400.00")
        resting.clear()

        aggressor.new_order(USER_THREE, side=D.BuySell.BUY, contract=FUTURE,
                            quantity=25, price="25400.00")

        self.assertIn(X.TRADE_CONFIRMATION, self.names(resting, USER_ONE))
        self.assertIn(X.TRADE_CONFIRMATION, self.names(aggressor, USER_THREE))

    def test_a_refused_sign_on_names_the_user_that_was_asked_for(self):
        # There is no session to stamp the header here: the refusal is about a
        # user who never signed on. The client still looks it up by user id.
        client = self.harness.client(users=())
        client.sign_on(UNKNOWN_USER)
        refusal = client.last()
        self.assertEqual(X.SIGN_ON_REQUEST_OUT, int(refusal.msg_type))
        self.assertEqual("16042", refusal.get(D.ERROR_CODE))
        self.assertEqual(str(UNKNOWN_USER), refusal.get(D.USER_ID))

    def test_a_market_state_broadcast_names_each_user_it_reaches(self):
        # Sending fills the header's user id only where it is still empty, so
        # one message shared across every session named the first user signed
        # on to every user after it.
        one = self.harness.client(box_id=BOX_ONE, users=(USER_ONE,))
        other = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))
        self.harness.set_state(TradingState.HALTED)
        self.assertEqual({X.SYSTEM_INFORMATION_OUT}, self.names(one, USER_ONE))
        self.assertEqual({X.SYSTEM_INFORMATION_OUT},
                         self.names(other, USER_THREE))


class MessageDownloadTest(unittest.TestCase):
    """DOWNLOAD_REQUEST: the only recovery this protocol has.

    There is no resend here. A report produced while a user was signed off was
    dropped rather than queued, and the client gets it back by asking -- so
    this is the mechanism that stands where a FIX ResendRequest would.
    """

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def download(self, since=0, stream=None, user_id=USER_ONE):
        fields = {D.DOWNLOAD_SEQUENCE: since}
        if stream is not None:
            fields[D.ALPHA_CHAR] = chr(stream)
        self.client.send(X.DOWNLOAD_REQUEST, fields, user_id=user_id)
        return self.client.received()

    def codes_of(self, messages):
        return [int(message.msg_type) for message in messages]

    def inner(self, record):
        """The message a MESSAGE_RECORD carries, decoded."""
        payload = record.get(D.DOWNLOAD_DATA).encode("latin-1")
        code = self.harness.venue.layouts.transaction_code(payload)
        return self.harness.venue.layouts.layout(code).decode(payload)

    def cursor(self, user_id=USER_ONE):
        """The TimeStamp1 of the most recent message this user was sent.

        What a real client remembers and quotes back, and what keeps a test
        clear of the sign-on response already in the store -- a download from
        zero replays that too, because the document lists the logon response
        first among what recovery returns. Asking for system information is
        how a test gets one: the harness clears the transport once the opening
        sequence is done, and the sign-on response goes with it. The system
        information itself is never stored, so it moves the cursor past
        everything before it without being replayed.
        """
        self.client.send(X.SYSTEM_INFORMATION_IN, user_id=user_id)
        stamp = int(self.client.last().get(D.TIMESTAMP1))
        self.client.clear()
        return stamp

    def recovered(self, since=0, stream=None, user_id=USER_ONE):
        """Just the messages a download replayed, decoded and in order."""
        return [self.inner(message)
                for message in self.download(since, stream, user_id)
                if int(message.msg_type) == X.MESSAGE_RECORD]

    def test_a_download_is_a_header_then_records_then_a_trailer(self):
        since = self.cursor()
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        self.client.clear()

        answered = self.download(since=since)
        codes = self.codes_of(answered)
        self.assertEqual(X.HEADER_RECORD, codes[0])
        self.assertEqual(X.TRAILER_RECORD, codes[-1])
        self.assertEqual({X.MESSAGE_RECORD}, set(codes[1:-1]))

    def test_a_record_carries_the_whole_message_it_recovered(self):
        since = self.cursor()
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        confirmation = self.client.last()
        self.client.clear()

        recovered = self.recovered(since=since)[0]
        self.assertEqual(str(X.ORDER_CONFIRMATION), recovered.msg_type)
        self.assertEqual(confirmation.get(D.ORDER_NUMBER),
                         recovered.get(D.ORDER_NUMBER))
        self.assertEqual(str(USER_ONE), recovered.get(D.USER_ID))

    def test_the_inner_header_is_the_ordinary_one(self):
        # The document prescribes INNER_MESSAGE_HEADER for download data, with
        # the trader id at offset 0; a real client reads the direct-connection
        # header, with the transaction code there. The two differ only in
        # their first twelve bytes, so the wrong one does not fail cleanly --
        # half a user id parses as a transaction code.
        since = self.cursor()
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        self.client.clear()

        payload = self.download(since=since)[1].get(
            D.DOWNLOAD_DATA).encode("latin-1")
        self.assertEqual(X.ORDER_CONFIRMATION,
                         self.harness.venue.layouts.transaction_code(payload))

    def test_a_cursor_replays_only_what_came_after_it(self):
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        first = self.client.last()
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        self.client.clear()

        replayed = self.recovered(since=int(first.get(D.TIMESTAMP1)))
        self.assertEqual(1, len(replayed))
        self.assertEqual("2", replayed[0].get(D.ORDER_NUMBER))

    def test_the_cursor_is_the_timestamp_the_client_was_given(self):
        # TimeStamp1 is what a client quotes back, so it has to be there and
        # it has to move. It was eight zero bytes on every message until the
        # download was built, which left a client nothing to resume from.
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        first = int(self.client.last().get(D.TIMESTAMP1))
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        second = int(self.client.last().get(D.TIMESTAMP1))
        self.assertLess(0, first)
        self.assertLess(first, second)

    def test_a_record_repeats_the_cursor_of_the_message_it_carries(self):
        # So that a client reading its cursor off the outer header and one
        # reading it off the inner header end in the same place.
        since = self.cursor()
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        self.client.clear()

        record = self.download(since=since)[1]
        self.assertEqual(self.inner(record).get(D.TIMESTAMP1),
                         record.get(D.TIMESTAMP1))

    def test_what_a_user_missed_while_signed_off_is_recoverable(self):
        # The whole point of the download. A report for a signed-off user is
        # dropped on the wire -- NNF has no resend and there is nothing to
        # queue it for -- but it is still that user's, and this is where they
        # get it. USER_TWO rather than USER_ONE, because USER_ONE cancels on
        # disconnect and so would have no resting order left to trade.
        self.client.sign_on(USER_TWO)
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, contract=FUTURE, quantity=25, price="25400.00")
        order_number = self.client.last().get(D.ORDER_NUMBER)
        self.harness.venue.manager.session_for(("nnf", USER_TWO)).disconnect()

        aggressor = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))
        aggressor.new_order(USER_THREE, side=D.BuySell.BUY, contract=FUTURE, quantity=25, price="25400.00")

        self.client.sign_on(USER_TWO)
        self.client.clear()
        fills = [m for m in self.recovered(user_id=USER_TWO)
                 if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual(1, len(fills))
        # A trade confirmation names the order under ResponseOrderNumber.
        self.assertEqual(order_number, fills[0].get(D.RESPONSE_ORDER_NUMBER))
        self.assertEqual(str(USER_TWO), fills[0].get(D.USER_ID))

    def test_a_signed_off_users_report_never_reaches_their_counterparty(self):
        # It did: the owner's session was looked up with the triggering
        # session as a default, so a trade against an order whose owner had
        # signed off was reported to whoever hit it.
        self.client.sign_on(USER_TWO)
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, contract=FUTURE, quantity=25, price="25400.00")
        self.harness.venue.manager.session_for(("nnf", USER_TWO)).disconnect()

        aggressor = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))
        aggressor.clear()
        aggressor.new_order(USER_THREE, side=D.BuySell.BUY, contract=FUTURE, quantity=25, price="25400.00")

        for message in aggressor.received():
            self.assertEqual(str(USER_THREE), message.get(D.USER_ID),
                             "%s was addressed to somebody else"
                             % message.msg_type)

    def test_the_download_does_not_replay_itself(self):
        # Storing the download's own answers would make a second download
        # return the first one wrapped in a third, without bound.
        since = self.cursor()
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        self.client.clear()
        first = len(self.recovered(since=since))
        self.client.clear()
        second = len(self.recovered(since=since))
        self.assertEqual(first, second)

    def test_a_stream_this_venue_does_not_serve_is_empty_rather_than_an_error(self):
        # A member loops the download over every stream the system information
        # advertised. Refusing one it does not have would stop that loop; an
        # empty download lets it move on.
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        self.client.clear()
        answered = self.download(stream=99)
        self.assertEqual([X.HEADER_RECORD, X.TRAILER_RECORD],
                         self.codes_of(answered))

    def test_the_system_information_says_how_many_streams_to_ask(self):
        # "In the SYSTEM_INFORMATION_OUT message response, this field should
        # contain the number of modules" -- a byte, not a digit.
        self.client.send(X.SYSTEM_INFORMATION_IN, user_id=USER_ONE)
        information = self.client.last()
        self.assertEqual(self.harness.venue.stream,
                         ord(information.get(D.ALPHA_CHAR)[0]))

    def test_every_message_names_the_stream_it_came_from(self):
        # TimeStamp2 carries the machine number, in its eighth byte for an
        # interactive connection -- which is the low byte of a big-endian
        # LONG LONG, so the number itself is what goes in.
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25, price="25400.00")
        self.assertEqual(str(self.harness.venue.stream),
                         self.client.last().get(D.TIMESTAMP2))

    def test_a_download_from_the_latest_cursor_replays_nothing(self):
        # A client that is up to date asks anyway, on every reconnection, and
        # must be told "nothing" rather than handed its whole day again.
        since = self.cursor()
        self.client.clear()
        self.assertEqual([], self.recovered(since=since))

    def test_system_information_is_never_replayed(self):
        # A client asks for it once, at logon, and sets its streams up from
        # it. Replayed out of a download from zero -- the first download of
        # the day -- a second one tripped a real client's assertion that its
        # streams were not yet set up, and took it down.
        self.client.send(X.SYSTEM_INFORMATION_IN, user_id=USER_ONE)
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25,
                              price="25400.00")
        self.client.clear()

        codes = self.codes_of(self.recovered())
        self.assertNotIn(X.SYSTEM_INFORMATION_OUT, codes)
        self.assertEqual([X.SIGN_ON_REQUEST_OUT, X.ORDER_CONFIRMATION], codes)

    def test_a_market_state_broadcast_is_not_replayed_either(self):
        # The same structure goes out unsolicited when the market moves, and
        # a download after it must not hand the client a second copy.
        since = self.cursor()
        self.harness.set_state(TradingState.HALTED)
        self.assertEqual(X.SYSTEM_INFORMATION_OUT,
                         int(self.client.last().msg_type))
        self.assertEqual([], self.recovered(since=since))


class TrimmedOrderFlowTest(unittest.TestCase):
    """The compact encoding of order entry -- what a real gateway sends.

    Appendix p.298-310. These structures do not carry the forty-byte
    MESSAGE_HEADER at all: each opens with a prefix of its own, eight bytes
    for a request. Chapter 11 says "Only Trim-NNF protocol is supported by
    Direct Interface", and a real gateway sends orders in nothing else -- so
    this is not an optional extra, it is the order flow.

    Both encodings are served and a client is answered in the one it asked
    in, which is why almost every test here is a pair.
    """

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def test_a_trimmed_order_is_confirmed_in_the_trimmed_structure(self):
        self.client.trimmed_order(USER_ONE, contract=FUTURE, quantity=25,
                                  price="25400.00")
        confirmation = self.client.last()
        self.assertEqual(X.ORDER_CONFIRMATION_TR,
                         int(confirmation.msg_type))
        self.assertEqual("0", confirmation.get(D.ERROR_CODE))
        self.assertEqual("1", confirmation.get(D.ORDER_NUMBER))
        self.assertEqual("25", confirmation.get(D.VOLUME))
        self.assertEqual("NIFTY", confirmation.get(D.SYMBOL))
        self.assertEqual(str(USER_ONE), confirmation.get(D.USER_ID))

    def test_a_plain_order_is_still_confirmed_in_the_plain_structure(self):
        # The two encodings coexist; serving one must not have moved the other.
        self.client.new_order(USER_ONE, contract=FUTURE, quantity=25,
                              price="25400.00")
        self.assertEqual(X.ORDER_CONFIRMATION,
                         int(self.client.last().msg_type))

    def test_the_two_encodings_carry_the_same_order(self):
        # The strongest statement this file can make: same fields in, same
        # fields out, different bytes on the wire.
        self.client.trimmed_order(USER_ONE, contract=OPTION, quantity=250,
                                  price="45.20")
        trimmed = self.client.last()
        self.client.new_order(USER_ONE, contract=OPTION, quantity=250,
                              price="45.20")
        plain = self.client.last()

        for tag in (D.SYMBOL, D.INSTRUMENT_NAME, D.EXPIRY_DATE,
                    D.STRIKE_PRICE, D.OPTION_TYPE, D.VOLUME, D.PRICE,
                    D.BOOK_TYPE, D.BUY_SELL, D.TOTAL_VOL_REMAINING,
                    D.ACCOUNT_NUMBER, D.USER_ID, D.BROKER_ID):
            self.assertEqual(plain.get(tag), trimmed.get(tag),
                             "tag %d differs between the encodings" % tag)

    def test_a_trimmed_order_wire_frame_is_the_published_size(self):
        # 158 bytes of structure, and no forty-byte header anywhere in it.
        message = self.client.trimmed_order(USER_ONE, contract=FUTURE,
                                            price="25400.00")
        raw = self.harness.venue.layouts.layout(X.BOARD_LOT_IN_TR).encode(
            message)
        self.assertEqual(158, len(raw))
        self.assertEqual(X.BOARD_LOT_IN_TR,
                         self.harness.venue.layouts.transaction_code(raw))

    def test_modify_and_cancel_answer_in_the_trimmed_structure(self):
        self.client.trimmed_order(USER_ONE, contract=FUTURE, quantity=25,
                                  price="25400.00")
        self.client.clear()

        self.client.trimmed_modify(USER_ONE, "1", quantity=50)
        self.assertEqual(X.ORDER_MOD_CONFIRMATION_TR,
                         int(self.client.last().msg_type))

        self.client.trimmed_cancel(USER_ONE, "1")
        self.assertEqual(X.ORDER_CXL_CONFIRMATION_TR,
                         int(self.client.last().msg_type))

    def test_a_refused_trimmed_order_is_the_confirmation_with_an_error(self):
        # ASSUMPTION: the appendix publishes no trimmed ORDER_ERROR, and
        # MS_OE_RESPONSE_TR carries an ErrorCode and a ReasonCode -- so the
        # response structure is built to say "refused" and there is no other
        # code to say it with. See rules.TRIMMED_RESPONSES.
        self.client.trimmed_order(USER_ONE, contract=FUTURE, price="25400.00",
                                  extra={D.BOOK_TYPE: D.BookType.NEGOTIATED})
        refusal = self.client.last()
        self.assertEqual(X.ORDER_CONFIRMATION_TR, int(refusal.msg_type))
        self.assertEqual("16561", refusal.get(D.ERROR_CODE))
        self.assertEqual("16561", refusal.get(D.REASON_CODE))

    def test_a_trade_answers_each_side_in_its_own_encoding(self):
        # Nothing answers a trade confirmation, so the encoding is read off
        # the order rather than off a request -- and two members on opposite
        # sides of one trade can be using different ones.
        resting = self.harness.client(box_id=BOX_ONE, users=(USER_ONE,))
        aggressor = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))
        resting.trimmed_order(USER_ONE, side=D.BuySell.SELL, contract=FUTURE,
                              quantity=25, price="25400.00")
        resting.clear()

        aggressor.new_order(USER_THREE, side=D.BuySell.BUY, contract=FUTURE,
                            quantity=25, price="25400.00")

        self.assertEqual([X.TRADE_CONFIRMATION_TR],
                         [int(m.msg_type) for m in resting.received()])
        self.assertEqual([X.ORDER_CONFIRMATION, X.TRADE_CONFIRMATION],
                         [int(m.msg_type) for m in aggressor.received()])

    def test_a_cancel_on_disconnect_keeps_the_orders_own_encoding(self):
        self.client.trimmed_order(USER_ONE, contract=FUTURE, quantity=25,
                                  price="25400.00")
        self.client.clear()
        self.harness.command("orders.cancel_all", market="NORMAL")
        self.assertEqual([X.ORDER_CXL_CONFIRMATION_TR],
                         [int(m.msg_type) for m in self.client.received()])

    def test_a_download_replays_the_non_trimmed_form(self):
        # "A downloaded message is always a non-trimmed message. If the
        # original message from the exchange was trimmed, the corresponding
        # non-trimmed message is used" -- and it must be, because a trimmed
        # structure has no forty-byte header to wrap in a MESSAGE_RECORD.
        self.client.send(X.SYSTEM_INFORMATION_IN, user_id=USER_ONE)
        since = int(self.client.last().get(D.TIMESTAMP1))
        self.client.trimmed_order(USER_ONE, contract=FUTURE, quantity=25,
                                  price="25400.00")
        self.assertEqual(X.ORDER_CONFIRMATION_TR,
                         int(self.client.last().msg_type))
        self.client.clear()

        self.client.send(X.DOWNLOAD_REQUEST,
                         {D.DOWNLOAD_SEQUENCE: since}, user_id=USER_ONE)
        layouts = self.harness.venue.layouts
        recovered = []
        for message in self.client.received():
            if int(message.msg_type) != X.MESSAGE_RECORD:
                continue
            payload = message.get(D.DOWNLOAD_DATA).encode("latin-1")
            code = layouts.transaction_code(payload)
            recovered.append(layouts.layout(code).decode(payload))

        self.assertEqual([X.ORDER_CONFIRMATION],
                         [int(m.msg_type) for m in recovered])
        self.assertEqual("1", recovered[0].get(D.ORDER_NUMBER))

    def test_a_refused_trimmed_order_recovers_as_an_order_error(self):
        # The plain flow has a separate code for a refusal where the trimmed
        # flow has one code and an ErrorCode, so the untrimming has to read
        # the error rather than map the code blindly.
        self.client.send(X.SYSTEM_INFORMATION_IN, user_id=USER_ONE)
        since = int(self.client.last().get(D.TIMESTAMP1))
        self.client.trimmed_order(USER_ONE, contract=FUTURE, price="25400.00",
                                  extra={D.BOOK_TYPE: D.BookType.NEGOTIATED})
        self.client.clear()

        self.client.send(X.DOWNLOAD_REQUEST,
                         {D.DOWNLOAD_SEQUENCE: since}, user_id=USER_ONE)
        layouts = self.harness.venue.layouts
        codes = []
        for message in self.client.received():
            if int(message.msg_type) != X.MESSAGE_RECORD:
                continue
            payload = message.get(D.DOWNLOAD_DATA).encode("latin-1")
            codes.append(layouts.transaction_code(payload))
        self.assertEqual([X.ORDER_ERROR], codes)

    def test_the_immediate_ack_family_is_refused_by_code(self):
        # Chapter 15's opt-in, which lives on its own Gateway Router port.
        # It shares MS_OE_REQUEST_TR's structure, so it would decode -- and
        # is refused on the transaction code instead of half-answered.
        self.client.raw(X.TRIMMED_BOARD_LOT_ACK_IN, user_id=USER_ONE)
        self.assertIsNone(
            self.harness.venue.layouts.layout(X.TRIMMED_BOARD_LOT_ACK_IN))


class SpreadOrderTest(unittest.TestCase):
    """A calendar spread: one order on two futures, priced at their gap.

    "Spread order is a combination of two normal orders on two contracts with
    same symbol and different expiry dates" (Chapter 5). The combination is an
    instrument here, with a book of its own quoted at ``PriceDiff`` -- which
    is what lets the ordinary engine match it, since price-time priority on a
    difference is still price-time priority. What a spread adds is at the
    edges: which pairs are valid, and that one match becomes two leg trades.
    """

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def refused(self, **kwargs):
        self.client.clear()
        self.client.spread_order(USER_ONE, **kwargs)
        reply = self.client.last()
        self.assertIsNotNone(reply, "nothing was answered")
        return int(reply.msg_type), int(reply.get(D.ERROR_CODE))

    def test_a_combination_is_an_instrument_of_its_own(self):
        self.assertEqual(5, len(self.harness.venue.spreads))
        self.assertIn(SPREAD_CANONICAL, self.harness.venue.instruments)
        combination = self.harness.venue.spread_for(SPREAD_CANONICAL)
        self.assertIsNotNone(combination)
        self.assertEqual(NEAR_CANONICAL, combination.near.canonical)
        self.assertEqual(FAR_CANONICAL, combination.far.canonical)

    def test_its_price_is_a_difference_and_may_be_negative(self):
        # The one core capability a spread needed: a price that is a gap
        # rather than a level, so the "a price must be positive" rule does
        # not apply to it. A calendar spread routinely trades through zero.
        instrument = self.harness.venue.instruments[SPREAD_CANONICAL]
        self.assertTrue(instrument.signed_price)
        self.client.spread_order(USER_ONE, difference="-100.00")
        confirmation = self.client.last()
        self.assertEqual(X.SP_ORDER_CONFIRMATION, int(confirmation.msg_type))
        self.assertEqual("0", confirmation.get(D.ERROR_CODE))
        self.assertEqual("-100.00", confirmation.get(D.PRICE_DIFF))

    def test_a_spread_order_is_confirmed_with_both_its_legs(self):
        self.client.spread_order(USER_ONE, quantity=25, difference="50.00")
        confirmation = self.client.last()
        self.assertEqual(X.SP_ORDER_CONFIRMATION, int(confirmation.msg_type))
        self.assertEqual("1", confirmation.get(D.ORDER_NUMBER))
        self.assertEqual("50.00", confirmation.get(D.PRICE_DIFF))
        self.assertEqual("NIFTY", confirmation.get(D.SYMBOL))
        self.assertEqual("NIFTY", confirmation.get(D.LEG2_SYMBOL))
        self.assertNotEqual(confirmation.get(D.EXPIRY_DATE),
                            confirmation.get(D.LEG2_EXPIRY_DATE))
        # The second leg is always the opposite side of the first.
        self.assertEqual(D.BuySell.BUY, confirmation.get(D.BUY_SELL))
        self.assertEqual(D.BuySell.SELL, confirmation.get(D.LEG2_BUY_SELL))

    def test_the_wire_frame_is_the_published_size(self):
        message = self.client.spread_order(USER_ONE)
        raw = self.harness.venue.layouts.layout(X.SP_BOARD_LOT_IN).encode(
            message)
        self.assertEqual(480, len(raw))

    def test_modify_and_cancel_answer_in_the_spread_codes(self):
        self.client.spread_order(USER_ONE, quantity=25, difference="50.00")
        self.client.clear()

        self.client.spread_modify(USER_ONE, "1", quantity=50,
                                  difference="60.00")
        modified = self.client.last()
        self.assertEqual(X.SP_ORDER_MOD_CON_OUT, int(modified.msg_type))
        self.assertEqual("60.00", modified.get(D.PRICE_DIFF))
        self.assertEqual("50", modified.get(D.VOLUME))

        self.client.spread_cancel(USER_ONE, "1")
        self.assertEqual(X.SP_ORDER_CXL_CONFIRMATION,
                         int(self.client.last().msg_type))

    def test_a_match_is_reported_as_a_trade_in_each_leg(self):
        # The whole point of a spread. Only the *difference* was agreed, so
        # the levels are the exchange's to pick -- but the gap between the
        # two prices a member is told is exactly what they traded at.
        buyer = self.harness.client(box_id=BOX_ONE, users=(USER_ONE,))
        seller = self.harness.client(box_id=BOX_TWO, users=(USER_THREE,))
        buyer.spread_order(USER_ONE, side=D.BuySell.BUY, quantity=25,
                           difference="50.00")
        buyer.clear()
        seller.spread_order(USER_THREE, side=D.BuySell.SELL, quantity=25,
                            difference="50.00")

        fills = [m for m in seller.received()
                 if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual(2, len(fills))
        near, far = fills
        self.assertEqual(NEAR_CANONICAL.split("-")[0], near.get(D.SYMBOL))
        self.assertEqual(near.get(D.SYMBOL), far.get(D.SYMBOL))
        self.assertNotEqual(near.get(D.EXPIRY_DATE), far.get(D.EXPIRY_DATE))
        self.assertEqual(D.BuySell.SELL, near.get(D.BUY_SELL))
        self.assertEqual(D.BuySell.BUY, far.get(D.BUY_SELL))
        difference = (self.harness.venue.codec.parse(far.get(D.FILL_PRICE))
                      - self.harness.venue.codec.parse(near.get(D.FILL_PRICE)))
        self.assertEqual("50.00", self.harness.venue.codec.format(difference))

        # And the other side is told the same trade from its own point of view.
        counterparty = [m for m in buyer.received()
                        if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual(2, len(counterparty))
        self.assertEqual(D.BuySell.BUY, counterparty[0].get(D.BUY_SELL))

    def test_two_spreads_that_do_not_cross_both_rest(self):
        self.client.spread_order(USER_ONE, side=D.BuySell.BUY, quantity=25,
                                 difference="40.00")
        self.client.spread_order(USER_ONE, side=D.BuySell.SELL, quantity=25,
                                 difference="60.00")
        self.assertEqual(2, len(self.harness.venue.markets["NORMAL"]
                                .book(SPREAD_CANONICAL).orders()))

    def test_legs_in_the_wrong_expiry_order_are_refused(self):
        # PriceDiff is "the difference between the prices at which leg2 and
        # leg1 should trade", so the leg order fixes the sign of the whole
        # market. The document has a code for exactly this, which is what
        # says the ordering is its rule rather than this simulator's.
        code, error = self.refused(near=FAR_FUTURE, far=NEAR_FUTURE)
        self.assertEqual(X.SP_ORDER_ERROR, code)
        self.assertEqual(rules.EXPIRY_NOT_ASCENDING, error)

    def test_both_legs_on_one_expiry_are_refused(self):
        code, error = self.refused(near=NEAR_FUTURE, far=NEAR_FUTURE)
        self.assertEqual(X.SP_ORDER_ERROR, code)
        self.assertEqual(rules.EXPIRY_NOT_ASCENDING, error)

    def test_legs_on_different_underlyings_are_refused(self):
        code, error = self.refused(far=OTHER_SYMBOL_FUTURE)
        self.assertEqual(rules.SPREAD_DIFFERENT_UNDERLYING, error)

    def test_a_pair_that_is_not_a_listed_combination_is_refused(self):
        # "Valid spread combinations will be pre-defined in the Spread
        # Combination Contract file" -- a pair of listed futures is not
        # automatically a spread.
        code, error = self.refused(near=NEAR_FUTURE, far=UNCOMBINED_FUTURE)
        self.assertEqual(rules.INVALID_CONTRACT_COMBINATION, error)

    def test_an_option_leg_is_refused(self):
        # "Spread day orders will be allowed only on future contracts."
        code, error = self.refused(near=OPTION, far=FAR_FUTURE)
        self.assertIn(error, (rules.SPREAD_NOT_ALLOWED_HERE,
                              rules.SPREAD_DIFFERENT_UNDERLYING))

    def test_legs_of_different_quantities_are_refused(self):
        code, error = self.refused(quantity=25,
                                   extra={D.LEG2_VOLUME: 50})
        self.assertEqual(rules.QTY_SHOULD_BE_SAME, error)

    def test_an_ioc_spread_is_refused(self):
        # "Currently Spread IOC orders are not allowed."
        code, error = self.refused(extra={D.FLAG_IOC: "Y", D.FLAG_DAY: "N"})
        self.assertEqual(rules.SPREAD_IOC_NOT_ALLOWED, error)

    def test_a_difference_outside_the_operating_range_is_refused(self):
        # "Spread day orders on eligible spread combinations with price
        # difference within the operating range, will be allowed."
        code, error = self.refused(difference="900.00")
        self.assertEqual(X.SP_ORDER_ERROR, code)
        self.assertEqual(16284, error)          # the band refusal

    def test_two_leg_and_three_leg_orders_are_still_refused(self):
        # They share MS_SPD_OE_REQUEST and nothing else: PriceDiff "is not
        # used for 2L/3L", so the one field that gives a spread its price
        # means nothing to them.
        for code in (X.TWOL_BOARD_LOT_IN, X.THRL_BOARD_LOT_IN):
            answered, error = self.refused(code=code)
            self.assertEqual(X.SP_ORDER_ERROR, answered)
            self.assertEqual(rules.BAD_TRANSACTION_CODE, error)
