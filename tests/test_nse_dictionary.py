"""The Capital Market transcription, checked against itself and the appendix.

There is no session and no venue here, deliberately: these are the tests that
can run before either exists, and they are the ones that catch a transcription
slip. A fixed-width protocol punishes those harder than any other kind of
mistake -- a field written at the wrong offset produces messages that frame
perfectly and mean something else -- so the check that matters is mechanical:

    every structure's fields must reach **exactly** the packet length the
    specification's appendix tables for that transaction code.

Everything else here is bookkeeping in the same spirit: every layout has a
message definition and every definition has a layout; every tag a layout writes
is a field the dictionary can name; no two fields of a structure overlap.
"""

import unittest

from exchangesim.core.instrument import (
    load_band_table,
    load_instruments,
    load_tick_table,
    read_rows,
)
from exchangesim.core.prices import PriceCodec
from exchangesim.fix.message import Message
from exchangesim.nnf import layout as L
from exchangesim.nnf.codec import NnfCodec
from exchangesim.venues.nse import dictionary as D
from exchangesim.venues.nse import layouts as LY
from exchangesim.venues.nse import transactions as X

#: The packet lengths the appendix tables, transaction code by transaction code.
#: Written out here rather than read from the layouts, so that this is a second
#: statement of the same fact and not a tautology.
PUBLISHED_SIZES = {
    X.SYSTEM_INFORMATION_IN: 40,
    X.SYSTEM_INFORMATION_OUT: 94,      # 94 per Chapter 3; the appendix says 90
    X.BOARD_LOT_IN: 290,
    X.PRICE_CONFIRMATION: 290,
    X.ORDER_MOD_IN: 290,
    X.ORDER_MOD_REJECT: 290,
    X.ORDER_CANCEL_IN: 290,
    X.ORDER_CANCEL_REJECT: 290,
    X.ORDER_CONFIRMATION: 290,
    X.ORDER_MOD_CONFIRMATION: 290,
    X.ORDER_CANCEL_CONFIRMATION: 290,
    X.ORDER_ERROR: 290,
    X.TRADE_CONFIRMATION: 228,
    X.SIGN_ON_REQUEST_IN: 276,
    X.SIGN_ON_REQUEST_OUT: 276,
    X.SIGN_OFF_REQUEST_IN: 40,
    X.SIGN_OFF_REQUEST_OUT: 40,
    X.ERROR_RESPONSE_OUT: 180,
    X.HEARTBEAT: 40,
    X.GR_REQUEST: 48,
    X.GR_RESPONSE: 136,                # the new encryption's form
    X.SECURE_BOX_REGISTRATION_REQUEST_IN: 42,
    X.SECURE_BOX_REGISTRATION_REQUEST_OUT: 40,
    X.BOX_SIGN_ON_REQUEST_IN: 60,
    X.BOX_SIGN_ON_REQUEST_OUT: 52,
    X.DOWNLOAD_REQUEST: 48,
}

#: Defined in transactions.py so a log line can name them, but produced by
#: nothing: the message download is deliberately unbuilt because a
#: MESSAGE_RECORD is 80 to 512 bytes and every structure here is fixed width.
#: See ``rules.NOT_IMPLEMENTED``.
NOT_STRUCTURED = (X.HEADER_RECORD, X.MESSAGE_RECORD, X.TRAILER_RECORD)

REFERENCE = "exchangesim/venues/nse/reference/%s.csv"


class StructureSizeTest(unittest.TestCase):
    """The cheapest test in this venue, and the one that earns its place."""

    def setUp(self):
        self.layouts = LY.build_cm()

    def test_every_structure_reaches_its_published_length(self):
        for code, size in sorted(PUBLISHED_SIZES.items()):
            layout = self.layouts.layout(code)
            self.assertIsNotNone(layout, "no structure for %d" % code)
            self.assertEqual(
                layout.body_size, size,
                "%s (%d): fields reach %d, the specification says %d"
                % (layout.name, code, layout.body_size, size))

    def test_no_structure_has_overlapping_or_short_fields(self):
        for layout in self.layouts.layouts:
            self.assertIsNone(layout.check(), layout.name)

    def test_every_published_code_has_a_structure(self):
        self.assertEqual(sorted(PUBLISHED_SIZES),
                         sorted(int(l.msg_type) for l in self.layouts.layouts))

    def test_the_download_response_records_have_none(self):
        for code in NOT_STRUCTURED:
            self.assertIsNone(self.layouts.layout(code), X.NAMES[code])

    def test_the_header_is_forty_bytes_with_the_long_long_at_fourteen(self):
        # pragma pack 2, and the single most quotable consequence of it.
        self.assertEqual(LY.HEADER.size, 40)
        offsets = {field.name: field.offset for field in LY.HEADER.fields}
        self.assertEqual(offsets["TransactionCode"], 0)
        self.assertEqual(offsets["UserId"], 8)
        self.assertEqual(offsets["ErrorCode"], 12)
        self.assertEqual(offsets["Timestamp"], 14)
        self.assertEqual(offsets["MessageLength"], 38)

    def test_one_structure_serves_the_whole_order_flow(self):
        # Fourteen transaction codes, one 290-byte table. If they ever diverge
        # this is where it shows.
        order_codes = (X.BOARD_LOT_IN, X.ORDER_MOD_IN, X.ORDER_CANCEL_IN,
                       X.ORDER_CONFIRMATION, X.ORDER_MOD_CONFIRMATION,
                       X.ORDER_CANCEL_CONFIRMATION, X.ORDER_ERROR,
                       X.ORDER_MOD_REJECT, X.ORDER_CANCEL_REJECT,
                       X.PRICE_CONFIRMATION)
        shapes = {tuple(sorted(self.layouts.layout(code).tags))
                  for code in order_codes}
        self.assertEqual(len(shapes), 1)


class DictionaryTest(unittest.TestCase):

    def setUp(self):
        self.dictionary = D.build_cm()
        self.layouts = LY.build_cm()

    def test_every_layout_has_a_message_definition(self):
        for layout in self.layouts.layouts:
            self.assertIsNotNone(self.dictionary.message(layout.msg_type),
                                 "%s has no MessageDef" % layout.name)

    def test_every_message_definition_has_a_layout(self):
        for msg_type in self.dictionary.messages:
            self.assertIsNotNone(self.layouts.layout(msg_type),
                                 "%s has no structure" % msg_type)

    def test_every_tag_a_structure_writes_is_a_named_field(self):
        for layout in self.layouts.layouts:
            for tag in layout.tags:
                self.assertIsNotNone(
                    self.dictionary.field(tag),
                    "%s writes tag %d, which the dialect does not define"
                    % (layout.name, tag))

    def test_every_header_tag_is_allowed_on_every_message(self):
        for field in LY.HEADER.fields:
            self.assertIn(field.tag, self.dictionary.header)

    def test_a_transaction_code_the_venue_only_sends_is_refused_inbound(self):
        for code in (X.ORDER_CONFIRMATION, X.TRADE_CONFIRMATION,
                     X.SIGN_ON_REQUEST_OUT, X.GR_RESPONSE):
            self.assertFalse(self.dictionary.message(str(code)).inbound,
                             X.NAMES[code])

    def test_a_transaction_code_a_client_sends_is_accepted_inbound(self):
        for code in (X.BOARD_LOT_IN, X.ORDER_MOD_IN, X.ORDER_CANCEL_IN,
                     X.SIGN_ON_REQUEST_IN, X.HEARTBEAT):
            self.assertTrue(self.dictionary.message(str(code)).inbound,
                            X.NAMES[code])

    def test_no_client_order_identifier_is_defined(self):
        # NNF has none: modification and cancellation address the exchange's
        # own OrderNumber. Defining ClOrdID would put an identifier no client
        # ever sent into the order record, the audit and the board.
        self.assertIsNone(self.dictionary.field(11))
        self.assertIsNone(self.dictionary.field(41))

    def test_ordtype_and_timeinforce_are_bits_not_fields(self):
        # NSE spells both as bits of ST_ORDER_FLAGS, so the FIX scalars are
        # deliberately absent and each bit is named in its own right.
        self.assertIsNone(self.dictionary.field(40))
        self.assertIsNone(self.dictionary.field(59))
        for tag in (D.FLAG_IOC, D.FLAG_DAY, D.FLAG_GTC, D.FLAG_MARKET,
                    D.FLAG_ATO):
            self.assertIsNotNone(self.dictionary.field(tag))

    def test_an_error_code_is_named_by_its_published_constant(self):
        field = self.dictionary.field(D.ERROR_CODE)
        self.assertEqual(field.label("16012"), "ERR_INVALID_SYMBOL")
        self.assertEqual(field.label("16000"), "ERR_MARKET_NOT_OPEN")
        self.assertEqual(field.label("19031"), "ERR_MD5_CHECKSUM_FAILURE")

    def test_a_book_type_is_named(self):
        field = self.dictionary.field(D.BOOK_TYPE)
        self.assertEqual(field.label("1"), "REGULAR_LOT")
        self.assertEqual(field.label("5"), "ODD_LOT")

    def test_the_password_is_marked_as_a_credential(self):
        self.assertTrue(self.dictionary.field(D.PASSWORD).redact)
        self.assertTrue(self.dictionary.field(D.CRYPTOGRAPHIC_KEY).redact)


class RoundTripTest(unittest.TestCase):
    """One real order through the real structures."""

    def setUp(self):
        self.layouts = LY.build_cm()
        self.dictionary = D.build_cm()
        self.codec = NnfCodec(self.layouts)

    def order(self):
        message = Message.create(str(X.BOARD_LOT_IN))
        for tag, value in (
                (D.USER_ID, "40521"), (D.ERROR_CODE, "0"),
                (D.SYMBOL, "INFY"), (D.SERIES, "EQ"),
                (D.BOOK_TYPE, D.BookType.REGULAR_LOT),
                (D.BUY_SELL, D.BuySell.BUY),
                (D.VOLUME, "500"), (D.PRICE, "1543.25"),
                (D.BROKER_ID, "10123"), (D.BRANCH_ID, "1"),
                (D.TRADER_ID, "40521"), (D.ACCOUNT_NUMBER, "CLIENT001"),
                (D.PRO_CLIENT, D.ProClient.CLIENT),
                (D.FLAG_DAY, "Y"), (D.FLAG_IOC, "N"),
                (D.NNF_FIELD, "778899")):
            message.set(tag, value)
        return message

    def test_an_order_survives_the_wire(self):
        raw = self.codec.encode(self.order())
        self.assertEqual(len(raw), 22 + 290)

        decoded = self.codec.decode(raw)
        self.assertEqual(decoded.msg_type, str(X.BOARD_LOT_IN))
        self.assertEqual(decoded.get(D.SYMBOL), "INFY")
        self.assertEqual(decoded.get(D.SERIES), "EQ")
        self.assertEqual(decoded.get(D.PRICE), "1543.25")
        self.assertEqual(decoded.get(D.VOLUME), "500")
        self.assertEqual(decoded.get(D.BROKER_ID), "10123")
        self.assertEqual(decoded.get(D.ACCOUNT_NUMBER), "CLIENT001")
        self.assertEqual(decoded.get(D.FLAG_DAY), "Y")
        self.assertEqual(decoded.get(D.FLAG_IOC), "N")
        self.assertEqual(decoded.get(D.NNF_FIELD), "778899")

    def test_a_decoded_order_passes_the_dialect(self):
        decoded = self.codec.decode(self.codec.encode(self.order()))
        self.assertIsNone(self.dictionary.validate(decoded, check_unknown=False))

    def test_the_price_is_paise_on_the_wire(self):
        raw = self.codec.encode(self.order())
        body = raw[22:]
        from exchangesim.nnf import types as T
        self.assertEqual(T.LONG.unpack(body, 116), 154325)

    def test_a_bad_book_type_reaches_the_dictionary_as_a_value_error(self):
        # The codec passes an unrecognised value through as text so that the
        # venue answers with its own error rather than the connection dropping.
        message = self.order()
        message.set(D.BOOK_TYPE, "9")
        decoded = self.codec.decode(self.codec.encode(message))
        failure = self.dictionary.validate(decoded, check_unknown=False)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.tag, D.BOOK_TYPE)

    def test_the_sign_on_password_never_reaches_the_dump(self):
        message = Message.create(str(X.SIGN_ON_REQUEST_IN))
        message.set(D.SIGNON_USER_ID, "40521")
        message.set(D.PASSWORD, "Nse@2026")
        message.set(D.BROKER_ID, "10123")
        rendered = self.codec.raw_string(self.codec.encode(message))
        self.assertNotIn("Nse@2026", rendered)
        self.assertIn("10123", rendered)


class ReferenceDataTest(unittest.TestCase):

    def setUp(self):
        self.codec = PriceCodec(D.PRICE_DECIMALS)

    def test_the_tick_is_five_paise_at_every_price(self):
        table = load_tick_table(REFERENCE % "tick_sizes", self.codec)
        for price in ("0.05", "13.70", "1543.25", "3856.40"):
            self.assertEqual(table.tick_for(self.codec.parse(price)),
                             self.codec.parse("0.05"), price)

    def test_every_shipped_base_price_is_on_tick(self):
        table = load_tick_table(REFERENCE % "tick_sizes", self.codec)
        instruments = load_instruments(REFERENCE % "securities", self.codec,
                                       tick_table=table)
        for symbol, instrument in sorted(instruments.items()):
            if instrument.base_price is None:
                continue
            tick = table.tick_for(instrument.base_price)
            self.assertEqual(instrument.base_price % tick, 0,
                             "%s base price is off-tick" % symbol)

    def test_a_symbol_is_the_composite_of_symbol_and_series(self):
        instruments = load_instruments(REFERENCE % "securities", self.codec)
        self.assertIn("INFY-EQ", instruments)
        self.assertIn("YESBANK-BE", instruments)
        for symbol in instruments:
            self.assertIn("-", symbol)

    def test_every_security_names_a_price_band(self):
        for row in read_rows(REFERENCE % "securities", ("symbol", "band")):
            self.assertTrue(row["band"].strip(), row["symbol"])
            self.assertIn(int(row["band"]), (2, 5, 10, 20), row["symbol"])

    def test_the_fallback_band_table_loads(self):
        table = load_band_table(REFERENCE % "price_bands", self.codec)
        self.assertEqual(len(table), 1)


if __name__ == "__main__":
    unittest.main()
