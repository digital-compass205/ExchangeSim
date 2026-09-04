"""Structures and the codec, over a layout invented here.

Nothing in this module imports ``venues.nse``, and that is the point: the NNF
package must know nothing about a market segment, so that Futures & Options can
later be a second dictionary and a second set of layouts over exactly this
machinery. If a change here needs the Capital Market definitions to test, the
change has put segment knowledge in the wrong package.

The toy structure below is shaped like a real one -- a forty-byte header, a
``pragma pack 2`` body with an eight-byte field on a two-byte boundary, a
bitfield and a credential -- without being any actual transaction code.
"""

import unittest

from exchangesim.fix import constants as C
from exchangesim.fix.message import MalformedMessage, Message
from exchangesim.nnf import layout as L
from exchangesim.nnf import packet
from exchangesim.nnf import types as T
from exchangesim.nnf.codec import NnfCodec, transaction_code_of
from exchangesim.nnf.crypto import ExistingCipher, NewCipher

# -- tags, in the private space a dialect would allocate ---------------------

TRADER_ID = 9001
ERROR_CODE = 9002
SYMBOL = 55
PRICE = 44
VOLUME = 38
ORDER_NUMBER = 37
PASSWORD = 9101
SIGN_ON_USER = 9110

ATO = 9201
MARKET = 9202
IOC = 9203
DAY = 9204


def build_header():
    """The forty-byte header, with the eight-byte field at offset 14."""
    return L.HeaderLayout(40, (
        L.Field(L.TRANSACTION_CODE, "TransactionCode", T.SHORT, 0),
        L.Field(TRADER_ID, "UserId", T.LONG, 8),
        L.Field(ERROR_CODE, "ErrorCode", T.SHORT, 12),
        L.Field(9003, "Timestamp", T.LONG_LONG, 14),
        L.Field(9004, "MessageLength", T.SHORT, 38),
    ))


def build_layouts():
    layouts = L.NnfDictionary(build_header())
    layouts.define(2000, "ORDER_ENTRY", 80, (
        L.Field(SYMBOL, "Symbol", T.Char(10), 40),
        L.Field(ORDER_NUMBER, "OrderNumber", T.DOUBLE, 50),
        L.Field(VOLUME, "Volume", T.LONG, 58),
        L.Field(PRICE, "Price", T.LONG, 62, L.PAISE),
        L.Flags("ST_ORDER_FLAGS", 66, 2,
                (ATO, MARKET, None, DAY, None, IOC, None, None,
                 None, None, None, None, None, None, None, None)),
        L.Field(9105, "BrokerId", T.Char(5), 68),
        L.Field(9106, "Filler", T.Char(7), 73),
    ))
    layouts.define(2300, "SIGN_ON", 56, (
        L.Field(SIGN_ON_USER, "UserId", T.LONG, 40),
        L.Field(PASSWORD, "Password", T.Char(8), 44, redact=True),
        L.Field(9107, "BrokerId", T.Char(4), 52),
    ))
    return layouts


def order(**overrides):
    message = Message.create("2000")
    fields = {L.TRANSACTION_CODE: "2000", TRADER_ID: "4242", ERROR_CODE: "0",
              SYMBOL: "INFY-EQ", ORDER_NUMBER: "12345678901234",
              VOLUME: "500", PRICE: "1543.25", ATO: "N", MARKET: "N",
              DAY: "Y", IOC: "N", 9105: "10123"}
    fields.update(overrides)
    for tag, value in fields.items():
        message.set(tag, value)
    return message


class FieldTest(unittest.TestCase):

    def test_a_field_writes_at_its_declared_offset(self):
        buffer = bytearray(16)
        L.Field(VOLUME, "Volume", T.LONG, 4).encode(
            Message.create("1").set(VOLUME, "7"), buffer)
        self.assertEqual(bytes(buffer),
                         b"\x00\x00\x00\x00\x00\x00\x00\x07" + b"\x00" * 8)

    def test_an_absent_numeric_field_is_written_as_zero(self):
        buffer = bytearray(8)
        L.Field(VOLUME, "Volume", T.LONG, 0).encode(Message.create("1"), buffer)
        self.assertEqual(bytes(buffer[:4]), b"\x00\x00\x00\x00")

    def test_an_absent_character_field_is_written_as_blanks(self):
        # Not NULs: Chapter 2 asks for blanks, and only a reserved run is NUL.
        buffer = bytearray(8)
        L.Field(SYMBOL, "Symbol", T.Char(5), 0).encode(Message.create("1"), buffer)
        self.assertEqual(bytes(buffer[:5]), b"     ")

    def test_a_price_travels_as_paise(self):
        buffer = bytearray(8)
        field = L.Field(PRICE, "Price", T.LONG, 0, L.PAISE)
        field.encode(Message.create("1").set(PRICE, "1543.25"), buffer)
        self.assertEqual(T.LONG.unpack(bytes(buffer), 0), 154325)

        message = Message.create("1")
        field.decode(bytes(buffer), message)
        self.assertEqual(message.get(PRICE), "1543.25")

    def test_a_price_keeps_its_decimal_places(self):
        # 154300 paise reads back as 1543.00, not 1543: every other surface
        # here writes a price with the venue's own precision, and two spellings
        # of one price in the audit would be worse than a longer string.
        buffer = bytearray(8)
        field = L.Field(PRICE, "Price", T.LONG, 0, L.PAISE)
        field.encode(Message.create("1").set(PRICE, "1543"), buffer)
        self.assertEqual(T.LONG.unpack(bytes(buffer), 0), 154300)
        message = Message.create("1")
        field.decode(bytes(buffer), message)
        self.assertEqual(message.get(PRICE), "1543.00")


class FlagsTest(unittest.TestCase):
    """One tag per bit, most significant bit of the first byte first."""

    def flags(self):
        return L.Flags("ST_ORDER_FLAGS", 0, 2,
                       (ATO, MARKET, None, DAY, None, IOC, None, None,
                        None, None, None, None, None, None, None, None))

    def test_the_first_bit_is_the_high_bit_of_the_first_byte(self):
        buffer = bytearray(2)
        self.flags().encode(Message.create("1").set(ATO, "Y"), buffer)
        self.assertEqual(bytes(buffer), b"\x80\x00")

    def test_each_bit_lands_where_the_big_endian_table_says(self):
        for tag, expected in ((ATO, b"\x80\x00"), (MARKET, b"\x40\x00"),
                              (DAY, b"\x10\x00"), (IOC, b"\x04\x00")):
            buffer = bytearray(2)
            self.flags().encode(Message.create("1").set(tag, "Y"), buffer)
            self.assertEqual(bytes(buffer), expected, tag)

    def test_bits_combine(self):
        buffer = bytearray(2)
        message = Message.create("1").set(DAY, "Y")
        message.set(IOC, "Y")
        self.flags().encode(message, buffer)
        self.assertEqual(bytes(buffer), b"\x14\x00")

    def test_decoding_gives_every_bit_a_yes_or_no(self):
        message = Message.create("1")
        self.flags().decode(b"\x90\x00", message)
        self.assertEqual(message.get(ATO), "Y")
        self.assertEqual(message.get(MARKET), "N")
        self.assertEqual(message.get(DAY), "Y")
        self.assertEqual(message.get(IOC), "N")

    def test_a_reserved_bit_is_neither_read_nor_written(self):
        message = Message.create("1")
        self.flags().decode(b"\xff\xff", message)
        # Four named bits, and the message type Message.create() carries. The
        # twelve reserved bits are set on the wire and named nowhere.
        self.assertEqual(len(message.fields), 5)

    def test_a_bit_count_that_disagrees_with_the_width_is_refused(self):
        self.assertRaises(ValueError, L.Flags, "wrong", 0, 2, (ATO, MARKET))


class LayoutTest(unittest.TestCase):

    def setUp(self):
        self.layouts = build_layouts()
        self.layout = self.layouts.layout(2000)

    def test_the_declared_size_is_what_the_fields_reach(self):
        self.assertIsNone(self.layout.check())
        self.assertEqual(self.layout.body_size, 80)

    def test_a_structure_that_overruns_its_size_is_named(self):
        layouts = L.NnfDictionary(build_header())
        wrong = layouts.define(9999, "TOO_SMALL", 44, (
            L.Field(VOLUME, "Volume", T.LONG, 44),))
        self.assertIn("declares 44 bytes but its fields reach 48", wrong.check())

    def test_overlapping_fields_are_named(self):
        layouts = L.NnfDictionary(build_header())
        wrong = layouts.define(9998, "OVERLAP", 48, (
            L.Field(VOLUME, "Volume", T.LONG, 40),
            L.Field(ORDER_NUMBER, "OrderNumber", T.LONG, 42),
            L.Field(9109, "Tail", T.Char(2), 46)))
        self.assertIn("overlaps", wrong.check())

    def test_encoding_produces_exactly_the_declared_size(self):
        self.assertEqual(len(self.layout.encode(order())), 80)

    def test_a_round_trip_keeps_every_field(self):
        decoded = self.layout.decode(self.layout.encode(order()))
        self.assertEqual(decoded.msg_type, "2000")
        self.assertEqual(decoded.get(SYMBOL), "INFY-EQ")
        self.assertEqual(decoded.get(PRICE), "1543.25")
        self.assertEqual(decoded.get(VOLUME), "500")
        self.assertEqual(decoded.get(ORDER_NUMBER), "12345678901234")
        self.assertEqual(decoded.get(DAY), "Y")
        self.assertEqual(decoded.get(IOC), "N")
        self.assertEqual(decoded.get(9105), "10123")

    def test_an_eight_byte_field_sits_on_a_two_byte_boundary(self):
        # pragma pack 2: the header's LONG LONG is at offset 14, which no
        # natural alignment would allow. A layout that computed offsets rather
        # than transcribing them would put it at 16.
        header = build_header()
        timestamp = [f for f in header.fields if f.name == "Timestamp"][0]
        self.assertEqual(timestamp.offset, 14)

    def test_decoding_the_wrong_number_of_bytes_is_malformed(self):
        raw = self.layout.encode(order())
        self.assertRaises(MalformedMessage, self.layout.decode, raw[:-1])

    def test_a_transaction_code_defined_twice_is_refused(self):
        self.assertRaises(ValueError, self.layouts.define, 2000, "AGAIN", 40, ())

    def test_the_transaction_code_is_readable_before_the_layout_is_known(self):
        raw = self.layout.encode(order())
        self.assertEqual(self.layouts.transaction_code(raw), 2000)


class CodecTest(unittest.TestCase):

    def setUp(self):
        self.layouts = build_layouts()
        self.codec = NnfCodec(self.layouts)

    def test_the_seam_is_the_one_the_session_layer_names(self):
        for method in ("framer", "extract", "decode", "encode", "is_admin",
                       "raw_string"):
            self.assertTrue(callable(getattr(self.codec, method)), method)
        self.assertEqual(self.codec.name, "nnf")

    def test_a_message_round_trips_through_a_packet(self):
        raw = self.codec.encode(order())
        self.assertEqual(len(raw), packet.PREFIX_BYTES + 80)
        decoded = self.codec.decode(raw)
        self.assertEqual(decoded.get(SYMBOL), "INFY-EQ")
        self.assertEqual(decoded.get(PRICE), "1543.25")

    def test_the_packet_sequence_number_travels_as_tag_34(self):
        message = order()
        message.set(C.MSG_SEQ_NUM, 12)
        raw = self.codec.encode(message)
        self.assertEqual(packet.unpack_prefix(raw).sequence, 12)
        self.assertEqual(self.codec.decode(raw).get(C.MSG_SEQ_NUM), "12")

    def test_an_unknown_transaction_code_decodes_to_something_no_dialect_defines(self):
        # A clean refusal by the dictionary, rather than a dropped connection.
        message = order()
        message.set(L.TRANSACTION_CODE, "2000")
        raw = bytearray(self.codec.encode(order()))
        raw[packet.PREFIX_BYTES:packet.PREFIX_BYTES + 2] = T.SHORT.pack(4711)
        decoded = self.codec.decode(packet.pack(bytes(raw[packet.PREFIX_BYTES:])))
        self.assertEqual(decoded.msg_type, "4711")
        self.assertEqual(transaction_code_of(decoded.msg_type), 4711)
        self.assertIsNone(self.layouts.layout(decoded.msg_type))

    def test_the_header_of_an_unknown_code_is_still_read(self):
        raw = bytearray(self.codec.encode(order()))
        raw[packet.PREFIX_BYTES:packet.PREFIX_BYTES + 2] = T.SHORT.pack(4711)
        decoded = self.codec.decode(packet.pack(bytes(raw[packet.PREFIX_BYTES:])))
        self.assertEqual(decoded.get(TRADER_ID), "4242")

    def test_a_message_type_is_the_transaction_code(self):
        self.assertEqual(transaction_code_of("2000"), 2000)
        self.assertIsNone(transaction_code_of("D"))

    def test_encoding_a_message_with_no_structure_is_a_programming_error(self):
        self.assertRaises(ValueError, self.codec.encode, Message.create("9999"))

    def test_nothing_is_gap_fillable(self):
        self.assertFalse(self.codec.is_admin(self.codec.encode(order())))

    def test_a_bad_checksum_is_refused(self):
        raw = bytearray(self.codec.encode(order()))
        raw[6] ^= 0xFF
        self.assertRaises(Exception, self.codec.decode, bytes(raw))

    def test_the_checksum_can_be_skipped_to_read_a_recorded_packet(self):
        raw = bytearray(self.codec.encode(order()))
        raw[6] ^= 0xFF
        decoded = self.codec.decode(bytes(raw), validate_checksum=False)
        self.assertEqual(decoded.get(SYMBOL), "INFY-EQ")


class EncryptedCodecTest(unittest.TestCase):
    """The cipher is the connection's, and it changes mid-stream."""

    KEY = b"\x61" * 32
    IV = b"\x62" * 8 + b"\x00" * 7 + b"\x0a"
    AAD = b"\x63" * 12

    def codecs(self, cipher_pair):
        member, exchange = cipher_pair
        return (NnfCodec(build_layouts(), member, client=True),
                NnfCodec(build_layouts(), exchange))

    def test_a_message_survives_the_existing_methodology(self):
        member, exchange = self.codecs((ExistingCipher(self.KEY, self.IV),
                                        ExistingCipher(self.KEY, self.IV)))
        raw = member.encode(order())
        self.assertNotIn(b"INFY-EQ", raw)
        self.assertEqual(exchange.decode(raw).get(SYMBOL), "INFY-EQ")

    def test_a_message_survives_the_new_methodology(self):
        member, exchange = self.codecs(
            (NewCipher(self.KEY, self.IV, self.AAD, client=True),
             NewCipher(self.KEY, self.IV, self.AAD)))
        raw = member.encode(order())
        self.assertNotIn(b"INFY-EQ", raw)
        self.assertEqual(exchange.decode(raw).get(SYMBOL), "INFY-EQ")

    def test_the_cipher_can_be_replaced_after_the_connection_opens(self):
        # A box connection opens in clear and is encrypted from the second
        # message onwards, so this is the ordinary case rather than a trick.
        member, exchange = NnfCodec(build_layouts(), client=True), NnfCodec(build_layouts())
        plain = member.encode(order())
        self.assertIn(b"INFY-EQ", plain)
        self.assertEqual(exchange.decode(plain).get(SYMBOL), "INFY-EQ")

        member.cipher = ExistingCipher(self.KEY, self.IV)
        exchange.cipher = ExistingCipher(self.KEY, self.IV)
        sealed = member.encode(order())
        self.assertNotIn(b"INFY-EQ", sealed)
        self.assertEqual(exchange.decode(sealed).get(SYMBOL), "INFY-EQ")


class RawStringTest(unittest.TestCase):

    def setUp(self):
        self.layouts = build_layouts()
        self.codec = NnfCodec(self.layouts)

    def sign_on(self):
        message = Message.create("2300")
        message.set(L.TRANSACTION_CODE, "2300")
        message.set(TRADER_ID, "4242")
        message.set(SIGN_ON_USER, "4242")
        message.set(PASSWORD, "s3cr3t")
        message.set(9107, "1012")
        return message

    def test_the_dump_shows_offsets_and_printable_bytes(self):
        rendered = self.codec.raw_string(self.codec.encode(order()))
        self.assertIn("0000  ", rendered)
        # The BrokerId, which sits inside one dump line rather than across two.
        self.assertIn("10123", rendered)

    def test_a_credential_is_struck_out_of_the_dump(self):
        rendered = self.codec.raw_string(self.codec.encode(self.sign_on()))
        self.assertNotIn("s3cr3t", rendered)
        self.assertIn("--", rendered)

    def test_the_password_bytes_and_only_those_are_struck_out(self):
        rendered = self.codec.raw_string(self.codec.encode(self.sign_on()))
        # The BrokerId sits immediately after the password and must survive.
        self.assertIn("1012", rendered)

    def test_rendering_does_not_disturb_a_live_cipher(self):
        # The existing methodology's keystream runs on across messages, so a
        # render that decrypted would consume stream and desynchronise the
        # connection it was reporting on.
        member = NnfCodec(build_layouts(), ExistingCipher(self.KEY_A, self.IV_A),
                          client=True)
        exchange = NnfCodec(build_layouts(), ExistingCipher(self.KEY_A, self.IV_A))
        first = member.encode(order())
        member.raw_string(first)
        member.raw_string(first)
        second = member.encode(order())
        self.assertEqual(exchange.decode(first).get(SYMBOL), "INFY-EQ")
        self.assertEqual(exchange.decode(second).get(SYMBOL), "INFY-EQ")

    KEY_A = b"\x71" * 32
    IV_A = b"\x72" * 16

    def test_an_unframeable_packet_still_renders(self):
        # The bytes worth showing raw are exactly the ones that will not parse.
        self.assertIn("0000  ", self.codec.raw_string(b"\x00\x01\x02\x03"))


if __name__ == "__main__":
    unittest.main()
