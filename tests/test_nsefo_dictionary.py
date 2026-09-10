"""The Futures & Options transcription, checked against itself, the appendix,
and Capital Market's own dictionary.

Modelled on ``tests/test_nse_dictionary.py``: there is no venue and no session
here, deliberately, because these are the tests that can run before either
exists and they are the ones that catch a transcription slip. The check that
matters most is mechanical, the same one Capital Market's own test runs:

    every structure's fields must reach **exactly** the packet length the
    transcription's appendix tables give for that transaction code.

What is particular to this venue is the safety net Capital Market's own test
file has no need of: several transaction codes are numerically identical
between the two dictionaries while decoding to different, larger structures,
and the single most important thing this test file can do is prove the two
dictionaries actually disagree about them rather than assuming it.
"""

import unittest

from exchangesim.core.instrument import read_rows
from exchangesim.fix.message import Message
from exchangesim.nnf.codec import NnfCodec
from exchangesim.venues.nse import dictionary as CMD
from exchangesim.venues.nse import layouts as CMLY
from exchangesim.venues.nse import transactions as CMX
from exchangesim.venues.nsefo import dictionary as D
from exchangesim.venues.nsefo import layouts as LY
from exchangesim.venues.nsefo import transactions as X

#: The packet lengths the transcription's appendix tables, transaction code by
#: transaction code -- transcribed here independently from
#: ``docs/specs/NSE_FO_TRANSCRIPTION.md`` §1, not imported from the layouts,
#: so that this is a second statement of the same fact and not a tautology.
PUBLISHED_SIZES = {
    # The download's answers. Header and trailer are a bare header; the
    # record is the one structure here with no published length, which is
    # what RECORD_CODES below is about.
    X.HEADER_RECORD: 40,
    X.TRAILER_RECORD: 40,
    # The trimmed order flow: a second, compact encoding of the same four
    # messages, and the shape a real gateway sends. None of these carries the
    # forty-byte MESSAGE_HEADER at all -- see layouts.py.
    X.BOARD_LOT_IN_TR: 158,
    X.ORDER_MOD_IN_TR: 186,
    X.ORDER_CANCEL_IN_TR: 186,
    X.ORDER_CONFIRMATION_TR: 240,
    X.ORDER_MOD_CONFIRMATION_TR: 240,
    X.ORDER_CXL_CONFIRMATION_TR: 240,
    X.TRADE_CONFIRMATION_TR: 230,
    X.SYSTEM_INFORMATION_IN: 44,
    X.SYSTEM_INFORMATION_OUT: 106,
    X.BOARD_LOT_IN: 316,
    X.PRICE_CONFIRMATION: 316,
    X.ORDER_MOD_IN: 316,
    X.ORDER_MOD_REJECT: 316,
    X.ORDER_CANCEL_IN: 316,
    X.ORDER_CANCEL_REJECT: 316,
    X.ORDER_CONFIRMATION: 316,
    X.ORDER_MOD_CONFIRMATION: 316,
    X.ORDER_CANCEL_CONFIRMATION: 316,
    X.ORDER_ERROR: 316,
    X.PRICE_MOD_IN: 106,
    X.TRADE_CONFIRMATION: 296,
    X.SIGN_ON_REQUEST_IN: 278,
    X.SIGN_ON_REQUEST_OUT: 278,
    X.SIGN_OFF_REQUEST_IN: 40,
    X.SIGN_OFF_REQUEST_OUT: 190,        # the appendix wins; see layouts.py
    X.ERROR_RESPONSE_OUT: 182,
    X.HEARTBEAT: 40,
    X.GR_REQUEST: 48,
    X.GR_RESPONSE: 136,                 # the new encryption's form
    X.SECURE_BOX_REGISTRATION_REQUEST_IN: 42,
    X.SECURE_BOX_REGISTRATION_REQUEST_OUT: 40,
    X.BOX_SIGN_ON_REQUEST_IN: 60,
    X.BOX_SIGN_ON_REQUEST_OUT: 52,       # the detailed table; the appendix says 54
    X.BOX_SIGN_OFF: 42,
    X.DOWNLOAD_REQUEST: 48,
}

#: The ten transaction codes the transcription's §4 "safety list" names as
#: colliding, numerically, with Capital Market's own transaction codes on the
#: 290-byte ORDER_ENTRY_REQUEST -- this venue's own is 316 bytes throughout.
COLLIDING_ORDER_CODES = (
    X.BOARD_LOT_IN, X.PRICE_CONFIRMATION, X.ORDER_MOD_IN, X.ORDER_MOD_REJECT,
    X.ORDER_CANCEL_IN, X.ORDER_CANCEL_REJECT, X.ORDER_CONFIRMATION,
    X.ORDER_MOD_CONFIRMATION, X.ORDER_CANCEL_CONFIRMATION, X.ORDER_ERROR,
)

#: The remaining collisions the transcription's §4 table names outside the
#: order family -- same transaction code, different structure at each venue.
OTHER_COLLIDING_CODES = (
    X.SYSTEM_INFORMATION_IN, X.SYSTEM_INFORMATION_OUT, X.TRADE_CONFIRMATION,
    X.SIGN_ON_REQUEST_IN, X.SIGN_ON_REQUEST_OUT, X.ERROR_RESPONSE_OUT,
    X.SIGN_OFF_REQUEST_OUT,
)

REFERENCE = "exchangesim/venues/nsefo/reference/%s.csv"


class StructureSizeTest(unittest.TestCase):
    """The cheapest test in this venue, and the one that earns its place."""

    def setUp(self):
        self.layouts = LY.build_fo()

    def test_every_structure_reaches_its_published_length(self):
        for code, size in sorted(PUBLISHED_SIZES.items()):
            layout = self.layouts.layout(code)
            self.assertIsNotNone(layout, "no structure for %d" % code)
            self.assertEqual(
                layout.body_size, size,
                "%s (%d): fields reach %d, the transcription says %d"
                % (layout.name, code, layout.body_size, size))

    def test_no_structure_has_overlapping_or_short_fields(self):
        for layout in self.layouts.layouts:
            self.assertIsNone(layout.check(), layout.name)

    def test_every_published_code_has_a_structure(self):
        self.assertEqual(sorted(list(PUBLISHED_SIZES) + [X.MESSAGE_RECORD]),
                         sorted(int(l.msg_type) for l in self.layouts.layouts))

    def test_the_download_record_is_the_one_structure_without_a_length(self):
        # Every other structure in this protocol is fixed width and the size
        # table above is checked against it. A MESSAGE_RECORD carries another
        # whole message, so it has no length to check.
        record = self.layouts.layout(X.MESSAGE_RECORD)
        self.assertIsNotNone(record)
        self.assertIsNone(record.check())
        self.assertEqual(40, record.header.size)

    def test_the_header_is_forty_bytes_and_identical_to_capital_markets(self):
        # pragma pack 2, and the single most quotable consequence of it --
        # restated independently for this venue (see layouts.py's docstring).
        self.assertEqual(LY.HEADER.size, 40)
        offsets = {field.name: field.offset for field in LY.HEADER.fields}
        self.assertEqual(offsets["TransactionCode"], 0)
        self.assertEqual(offsets["TraderId"], 8)
        self.assertEqual(offsets["ErrorCode"], 12)
        self.assertEqual(offsets["Timestamp"], 14)
        self.assertEqual(offsets["MessageLength"], 38)
        cm_offsets = {field.name: field.offset for field in CMLY.HEADER.fields}
        self.assertEqual(cm_offsets["UserId"], offsets["TraderId"])
        self.assertEqual(CMLY.HEADER.size, LY.HEADER.size)

    def test_one_structure_serves_the_whole_order_flow(self):
        # Ten transaction codes, one 316-byte table. If they ever diverge
        # this is where it shows.
        shapes = {tuple(sorted(self.layouts.layout(code).tags))
                  for code in COLLIDING_ORDER_CODES}
        self.assertEqual(len(shapes), 1)

    def test_contract_desc_is_twenty_eight_bytes(self):
        # Embedded at offset 58 of the order structure and 136 of the trade
        # structure; both must reach the same six fields.
        order = self.layouts.layout(X.BOARD_LOT_IN)
        trade = self.layouts.layout(X.TRADE_CONFIRMATION)
        contract_tags = (D.INSTRUMENT_NAME, D.SYMBOL, D.EXPIRY_DATE,
                         D.STRIKE_PRICE, D.OPTION_TYPE, D.CA_LEVEL)
        for tag in contract_tags:
            self.assertIn(tag, order.tags)
            self.assertIn(tag, trade.tags)


class CollisionTest(unittest.TestCase):
    """The single most dangerous property of this venue: transaction codes
    that mean two different, different-sized things depending which
    dictionary decodes them."""

    def setUp(self):
        self.fo_layouts = LY.build_fo()
        self.cm_layouts = CMLY.build_cm()

    def test_the_ten_colliding_order_codes_decode_differently(self):
        for code in COLLIDING_ORDER_CODES:
            cm_layout = self.cm_layouts.layout(code)
            fo_layout = self.fo_layouts.layout(code)
            self.assertIsNotNone(cm_layout, CMX.NAMES.get(code, code))
            self.assertIsNotNone(fo_layout, X.NAMES.get(code, code))
            self.assertNotEqual(
                cm_layout.body_size, fo_layout.body_size,
                "%s (%d): both dictionaries agree on %d bytes"
                % (X.NAMES.get(code, code), code, fo_layout.body_size))
            # The exact, known delta -- 26 bytes, throughout the family.
            self.assertEqual(fo_layout.body_size - cm_layout.body_size, 26)

    def test_the_other_named_collisions_decode_differently_too(self):
        for code in OTHER_COLLIDING_CODES:
            cm_layout = self.cm_layouts.layout(code)
            fo_layout = self.fo_layouts.layout(code)
            self.assertIsNotNone(cm_layout, CMX.NAMES.get(code, code))
            self.assertIsNotNone(fo_layout, X.NAMES.get(code, code))
            if cm_layout.body_size == fo_layout.body_size:
                # Sizes coincide is not ruled out by the transcription for
                # every one of these; what must still differ is the field
                # set (e.g. ERROR_RESPONSE_OUT keys on a bare token here
                # against Capital Market's Symbol+Series).
                self.assertNotEqual(sorted(cm_layout.tags),
                                    sorted(fo_layout.tags),
                                    X.NAMES.get(code, code))
            else:
                self.assertNotEqual(cm_layout.body_size, fo_layout.body_size,
                                    X.NAMES.get(code, code))

    def test_batch_order_cancel_is_not_a_collision(self):
        # Capital Market's own transactions.py does not define this code at
        # all, so there is nothing for it to collide with.
        self.assertIsNone(CMLY.build_cm().layout(X.BATCH_ORDER_CANCEL))

    def test_no_tag_means_two_different_things_across_the_two_dictionaries(self):
        cm = CMD.build_cm()
        fo = D.build_fo()
        shared_tags = set(cm.fields) & set(fo.fields)
        self.assertTrue(shared_tags, "expected at least the reused FIX tags")
        for tag in shared_tags:
            self.assertEqual(
                cm.field(tag).name, fo.field(tag).name,
                "tag %d is %r in Capital Market and %r in F&O"
                % (tag, cm.field(tag).name, fo.field(tag).name))

    def test_fo_private_tags_never_collide_with_capital_markets(self):
        cm = CMD.build_cm()
        fo_private_tags = [tag for tag in D.__dict__.values()
                           if isinstance(tag, int) and tag >= 9400]
        for tag in fo_private_tags:
            self.assertIsNone(cm.field(tag),
                              "tag %d is reserved for F&O but Capital "
                              "Market's dictionary defines it" % tag)

    def test_16521_means_something_different_at_each_venue(self):
        # The one outright semantic collision the transcription found in the
        # error-code tables: same numeric value, different published meaning.
        self.assertNotEqual(CMX.ERROR_CODES[16521], X.ERROR_CODES[16521])


class DictionaryTest(unittest.TestCase):

    def setUp(self):
        self.dictionary = D.build_fo()
        self.layouts = LY.build_fo()

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
                     X.SIGN_ON_REQUEST_OUT, X.GR_RESPONSE, X.BOX_SIGN_OFF):
            self.assertFalse(self.dictionary.message(str(code)).inbound,
                             X.NAMES[code])

    def test_a_transaction_code_a_client_sends_is_accepted_inbound(self):
        for code in (X.BOARD_LOT_IN, X.ORDER_MOD_IN, X.ORDER_CANCEL_IN,
                     X.SIGN_ON_REQUEST_IN, X.HEARTBEAT, X.PRICE_MOD_IN):
            self.assertTrue(self.dictionary.message(str(code)).inbound,
                            X.NAMES[code])

    def test_no_client_order_identifier_is_defined(self):
        # NNF has none here either: modification and cancellation address
        # the exchange's own OrderNumber.
        self.assertIsNone(self.dictionary.field(11))
        self.assertIsNone(self.dictionary.field(41))

    def test_ordtype_and_timeinforce_are_bits_not_fields(self):
        self.assertIsNone(self.dictionary.field(40))
        self.assertIsNone(self.dictionary.field(59))
        for tag in (D.FLAG_IOC, D.FLAG_DAY, D.FLAG_GTC, D.FLAG_MARKET,
                    D.FLAG_ATO, D.FLAG_SL, D.FLAG_MIT):
            self.assertIsNotNone(self.dictionary.field(tag))

    def test_strikepx_and_putorcall_are_not_reused(self):
        # F&O's strike carries -1 for a futures contract (not a legal FIX
        # StrikePx), and its option type is three-valued, not FIX's boolean.
        self.assertIsNone(self.dictionary.field(202))
        self.assertIsNone(self.dictionary.field(201))

    def test_an_error_code_is_named_by_its_published_constant(self):
        field = self.dictionary.field(D.ERROR_CODE)
        self.assertEqual(field.label("16012"), "ERR_INVALID_SYMBOL")
        self.assertEqual(field.label("16000"), "MARKET_CLOSED")
        self.assertEqual(field.label("19031"), "ERR_MD5_CHECKSUM_FAILURE")

    def test_a_book_type_is_named(self):
        field = self.dictionary.field(D.BOOK_TYPE)
        self.assertEqual(field.label("1"), "REGULAR_LOT")
        self.assertEqual(field.label("5"), "ODD_LOT")

    def test_the_password_is_marked_as_a_credential(self):
        self.assertTrue(self.dictionary.field(D.PASSWORD).redact)
        self.assertTrue(self.dictionary.field(D.CRYPTOGRAPHIC_KEY).redact)

    def test_check_unknown_false_is_the_only_sane_validation_mode(self):
        # Every field of a structure is on the wire on every message, so
        # "required" can never fail here -- the dialect checks values only.
        for message_def in self.dictionary.messages.values():
            self.assertEqual(message_def.required, frozenset())


class OrderFlagsTest(unittest.TestCase):
    """ST_ORDER_FLAGS explodes to F&O's own bit names, not Capital
    Market's -- the single easiest thing to get silently wrong here."""

    def setUp(self):
        self.dictionary = D.build_fo()

    def test_stpc_is_absent_from_st_order_flags(self):
        # STPC moved to ADDITIONAL_ORDER_FLAGS in this venue.
        flags_tags = {D.FLAG_ATO, D.FLAG_MARKET, D.FLAG_SL, D.FLAG_MIT,
                      D.FLAG_DAY, D.FLAG_GTC, D.FLAG_IOC, D.FLAG_AON,
                      D.FLAG_MF, D.FLAG_MATCHED_IND, D.FLAG_TRADED,
                      D.FLAG_MODIFIED, D.FLAG_FROZEN, D.FLAG_PREOPEN}
        self.assertNotIn(D.FLAG_STPC_ADDITIONAL, flags_tags)

    def test_stpc_is_present_in_additional_order_flags(self):
        additional_tags = {D.FLAG_BOC, D.FLAG_COL, D.FLAG_STPC_ADDITIONAL}
        self.assertIn(D.FLAG_STPC_ADDITIONAL, additional_tags)
        self.assertIsNotNone(self.dictionary.field(D.FLAG_STPC_ADDITIONAL))

    def test_sl_and_mit_are_distinct_bits(self):
        self.assertNotEqual(D.FLAG_SL, D.FLAG_MIT)
        self.assertIsNotNone(self.dictionary.field(D.FLAG_SL))
        self.assertIsNotNone(self.dictionary.field(D.FLAG_MIT))

    def test_the_bits_are_where_the_big_endian_table_says(self):
        layouts = LY.build_fo()
        order = layouts.layout(X.BOARD_LOT_IN)
        flags_field = [f for f in order.fields
                       if getattr(f, "name", None) == "ST_ORDER_FLAGS"][0]
        self.assertEqual(flags_field.bits[0], D.FLAG_ATO)   # byte0 bit7 (MSB)
        self.assertEqual(flags_field.bits[2], D.FLAG_SL)
        self.assertEqual(flags_field.bits[3], D.FLAG_MIT)
        self.assertEqual(flags_field.bits[8], D.FLAG_MF)    # byte1 bit7 (MSB)
        self.assertIsNone(flags_field.bits[14])
        self.assertIsNone(flags_field.bits[15])


class RoundTripTest(unittest.TestCase):
    """Real orders through the real structures -- a futures contract with a
    -1 strike, and an options contract with a real one."""

    def setUp(self):
        self.layouts = LY.build_fo()
        self.dictionary = D.build_fo()
        self.codec = NnfCodec(self.layouts)

    def _order(self, instrument_name, symbol, expiry, strike, option_type,
               price):
        message = Message.create(str(X.BOARD_LOT_IN))
        for tag, value in (
                (D.USER_ID, "50521"), (D.ERROR_CODE, "0"),
                (D.INSTRUMENT_NAME, instrument_name), (D.SYMBOL, symbol),
                (D.EXPIRY_DATE, expiry), (D.STRIKE_PRICE, strike),
                (D.OPTION_TYPE, option_type), (D.CA_LEVEL, "0"),
                (D.BOOK_TYPE, D.BookType.REGULAR_LOT),
                (D.BUY_SELL, D.BuySell.BUY),
                (D.VOLUME, "50"), (D.PRICE, price),
                (D.BROKER_ID, "10123"), (D.BRANCH_ID, "1"),
                (D.TRADER_ID, "50521"), (D.ACCOUNT_NUMBER, "CLIENT001"),
                (D.PRO_CLIENT, D.ProClient.CLIENT),
                (D.FLAG_DAY, "Y"), (D.FLAG_IOC, "N"),
                (D.NNF_FIELD, "778899")):
            message.set(tag, value)
        return message

    def test_a_futures_order_round_trips_with_the_strike_sentinel(self):
        order = self._order(D.InstrumentName.FUTURES_INDEX, "NIFTY",
                            "1472083200", "-1", "XX",
                            "25400.00")
        raw = self.codec.encode(order)
        self.assertEqual(len(raw), 22 + 316)

        decoded = self.codec.decode(raw)
        self.assertEqual(decoded.get(D.INSTRUMENT_NAME), "FUTIDX")
        self.assertEqual(decoded.get(D.SYMBOL), "NIFTY")
        self.assertEqual(decoded.get(D.STRIKE_PRICE), "-1")
        self.assertEqual(decoded.get(D.OPTION_TYPE), "XX")
        self.assertEqual(decoded.get(D.PRICE), "25400.00")
        self.assertIsNone(self.dictionary.validate(decoded, check_unknown=False))

    def test_an_options_order_round_trips_with_a_real_strike(self):
        order = self._order(D.InstrumentName.OPTIONS_INDEX, "NIFTY",
                            "1472083200", "25000.00", D.OptionType.CALL,
                            "420.50")
        raw = self.codec.encode(order)
        decoded = self.codec.decode(raw)
        self.assertEqual(decoded.get(D.INSTRUMENT_NAME), "OPTIDX")
        self.assertEqual(decoded.get(D.STRIKE_PRICE), "25000.00")
        self.assertEqual(decoded.get(D.OPTION_TYPE), "CE")
        self.assertIsNone(self.dictionary.validate(decoded, check_unknown=False))

    def test_the_price_is_paise_on_the_wire(self):
        from exchangesim.nnf import types as T
        order = self._order(D.InstrumentName.FUTURES_STOCK, "RELIANCE",
                            "1472083200", "-1", "XX",
                            "2480.00")
        raw = self.codec.encode(order)
        body = raw[22:]
        self.assertEqual(T.LONG.unpack(body, 140), 248000)

    def test_a_bad_book_type_reaches_the_dictionary_as_a_value_error(self):
        message = self._order(D.InstrumentName.FUTURES_INDEX, "NIFTY",
                              "1472083200", "-1", "XX",
                              "25400.00")
        message.set(D.BOOK_TYPE, "9")
        decoded = self.codec.decode(self.codec.encode(message))
        failure = self.dictionary.validate(decoded, check_unknown=False)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.tag, D.BOOK_TYPE)

    def test_the_sign_on_password_never_reaches_the_dump(self):
        message = Message.create(str(X.SIGN_ON_REQUEST_IN))
        message.set(D.SIGNON_USER_ID, "50521")
        message.set(D.PASSWORD, "Neat@FO1")
        message.set(D.BROKER_ID, "10123")
        rendered = self.codec.raw_string(self.codec.encode(message))
        self.assertNotIn("Neat@FO1", rendered)
        self.assertIn("10123", rendered)

    def test_a_sign_on_message_is_two_hundred_seventy_eight_bytes(self):
        message = Message.create(str(X.SIGN_ON_REQUEST_IN))
        message.set(D.SIGNON_USER_ID, "50521")
        message.set(D.PASSWORD, "Neat@FO1")
        raw = self.codec.encode(message)
        self.assertEqual(len(raw), 22 + 278)


class ReferenceDataTest(unittest.TestCase):

    def test_the_reference_csv_loads(self):
        rows = read_rows(REFERENCE % "contracts",
                         ("instrument_name", "symbol", "expiry",
                          "option_type", "lot_size", "base_price", "tick",
                          "band_pct"))
        self.assertTrue(rows)

    def test_every_contract_is_well_formed(self):
        valid_instruments = {"FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"}
        valid_option_types = {"CE", "PE", "XX"}
        for row in read_rows(REFERENCE % "contracts",
                             ("instrument_name", "symbol", "option_type",
                              "strike", "lot_size", "base_price")):
            self.assertIn(row["instrument_name"], valid_instruments, row)
            self.assertTrue(row["symbol"].strip(), row)
            self.assertIn(row["option_type"], valid_option_types, row)
            self.assertGreater(int(row["lot_size"]), 0, row)
            self.assertGreater(float(row["base_price"]), 0, row)
            is_future = row["instrument_name"] in ("FUTIDX", "FUTSTK")
            if is_future:
                self.assertEqual(row["option_type"], "XX", row)
                self.assertEqual(row["strike"].strip(), "", row)
            else:
                self.assertIn(row["option_type"], ("CE", "PE"), row)
                self.assertGreater(float(row["strike"]), 0, row)

    def test_every_contract_names_a_real_underlying(self):
        underlyings = {"NIFTY", "BANKNIFTY", "RELIANCE", "INFY", "TCS"}
        for row in read_rows(REFERENCE % "contracts", ("symbol",)):
            self.assertIn(row["symbol"], underlyings, row)


if __name__ == "__main__":
    unittest.main()
