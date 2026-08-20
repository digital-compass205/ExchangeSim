"""The binary encoding of OCG-C, from the bytes upwards.

Everything here is about the wire: the data types, the frame, the presence map
and the field mapping. What the venue does with the resulting message is
:mod:`tests.test_hkex_binary`'s business.
"""

import unittest

from exchangesim.binary import types as T
from exchangesim.binary import values as V
from exchangesim.binary import message as framing
from exchangesim.binary.codec import BinaryCodec
from exchangesim.fix import constants as C
from exchangesim.fix.message import (
    IncompleteMessage,
    MalformedMessage,
    Message,
)
from exchangesim.venues.hkex import binary as binary_layouts
from exchangesim.venues.hkex import dictionary as D


def build_codec(comp_id="HKEXSIM"):
    dictionary = D.build_binary()
    return BinaryCodec(binary_layouts.build(dictionary), comp_id), dictionary


class DataTypeTest(unittest.TestCase):

    def test_crc32c_matches_the_published_check_value(self):
        # The standard check value for CRC-32C over "123456789". zlib.crc32 is
        # a different polynomial and gives 0xCBF43926 instead.
        self.assertEqual(T.crc32c(b"123456789"), 0xE3069283)

    def test_unsigned_types_are_little_endian(self):
        self.assertEqual(T.Unsigned(2).pack(513), b"\x01\x02")
        self.assertEqual(T.Unsigned(4).pack(388), b"\x84\x01\x00\x00")
        self.assertEqual(T.Unsigned(4).unpack(b"\x84\x01\x00\x00", 0), (388, 4))

    def test_unsigned_refuses_a_value_the_field_cannot_hold(self):
        self.assertRaises(ValueError, T.Unsigned(1).pack, 256)

    def test_alpha_fixed_is_null_terminated_within_its_width(self):
        alpha = T.AlphaFixed(12)
        self.assertEqual(alpha.pack("HKEXSIM"), b"HKEXSIM\x00\x00\x00\x00\x00")
        self.assertEqual(alpha.unpack(alpha.pack("HKEXSIM"), 0), ("HKEXSIM", 12))

    def test_alpha_fixed_keeps_room_for_the_terminator(self):
        alpha = T.AlphaFixed(4)
        self.assertEqual(alpha.capacity, 3)
        self.assertEqual(alpha.pack("ABCDEF"), b"ABC\x00")

    def test_alpha_fixed_without_a_terminator_takes_the_whole_field(self):
        self.assertEqual(T.AlphaFixed(3).unpack(b"ABC", 0), ("ABC", 3))

    def test_alpha_variable_length_counts_the_terminator(self):
        alpha = T.AlphaVariable(50)
        self.assertEqual(alpha.pack("AB"), b"\x03\x00AB\x00")
        self.assertEqual(alpha.unpack(b"\x03\x00AB\x00", 0), ("AB", 5))
        self.assertEqual(alpha.unpack(alpha.pack(""), 0), ("", 3))

    def test_reading_past_the_end_is_a_framing_error(self):
        self.assertRaises(MalformedMessage, T.Unsigned(4).unpack, b"\x01", 0)


class DecimalTest(unittest.TestCase):
    """Eight implied decimal places, converted on integers only."""

    def test_scaling_is_exact_for_a_tick_a_float_cannot_hold(self):
        self.assertEqual(V.scale("0.001"), 100000)
        self.assertEqual(V.scale("395.800"), 39580000000)
        self.assertEqual(V.scale("1000"), 100000000000)

    def test_unscaling_returns_the_shortest_form_that_means_it(self):
        self.assertEqual(V.unscale(39580000000), "395.8")
        self.assertEqual(V.unscale(100000000000), "1000")
        self.assertEqual(V.unscale(0), "0")

    def test_negative_values_round_trip(self):
        self.assertEqual(V.unscale(V.scale("-12.5")), "-12.5")

    def test_more_precision_than_the_type_holds_is_refused(self):
        self.assertRaises(ValueError, V.scale, "0.000000001")

    def test_a_non_numeric_value_is_refused(self):
        self.assertRaises(ValueError, V.scale, "abc")


class PresenceMapTest(unittest.TestCase):
    """Bit 0 is the most significant bit of the first byte, section 6.2.1."""

    def test_bits_are_read_from_the_most_significant_end(self):
        # 0xa4 = 1010 0100: the Logon presence map a real gateway sends, being
        # Password, NextExpectedMessageSequence and TestMessageIndicator.
        bits, offset = T.presence_bits(b"\xa4" + b"\x00" * 31, 0)
        self.assertEqual(bits, [0, 2, 5])
        self.assertEqual(offset, 32)

    def test_packing_is_the_inverse(self):
        packed = T.pack_presence([0, 2, 5])
        self.assertEqual(packed[0], 0xA4)
        self.assertEqual(len(packed), 32)
        self.assertEqual(T.presence_bits(packed, 0)[0], [0, 2, 5])

    def test_a_bit_beyond_the_map_is_refused(self):
        self.assertRaises(ValueError, T.pack_presence, [256])


class FramingTest(unittest.TestCase):

    def setUp(self):
        self.codec, self.dictionary = build_codec()
        request = Message.create(C.TEST_REQUEST)
        request.set(C.MSG_SEQ_NUM, 4)
        request.set(C.TARGET_COMP_ID, "BROKER3")
        request.set(C.TEST_REQ_ID, "7")
        self.raw = self.codec.encode(request)

    def test_a_frame_is_header_presence_map_body_and_checksum(self):
        self.assertEqual(len(self.raw), framing.BODY_OFFSET + 2 + 4)
        self.assertEqual(self.raw[0], framing.START_OF_MESSAGE)
        # Length is a little-endian UInt16 covering the whole frame.
        self.assertEqual(self.raw[1:3], b"\x3c\x00")
        self.assertEqual(self.raw[3], 1)           # MessageType: Test Request
        self.assertEqual(self.raw[4:8], b"\x04\x00\x00\x00")
        self.assertEqual(self.raw[8:10], b"\x00\x00")    # PossDup, PossResend
        self.assertEqual(self.raw[10:22], b"BROKER3\x00\x00\x00\x00\x00")
        self.assertEqual(self.raw[22], 0x80)       # only bit 0 is set
        self.assertEqual(self.raw[54:56], b"\x07\x00")   # TestRequestID

    def test_the_trailer_is_a_crc32c_of_everything_before_it(self):
        self.assertEqual(self.raw[-4:],
                         T.crc32c(self.raw[:-4]).to_bytes(4, "little"))

    def test_extract_splits_concatenated_frames(self):
        first, rest = framing.extract(self.raw + self.raw)
        self.assertEqual(first, self.raw)
        self.assertEqual(rest, self.raw)

    def test_a_partial_frame_waits_for_more_bytes(self):
        self.assertRaises(IncompleteMessage, framing.extract, self.raw[:-1])
        self.assertRaises(IncompleteMessage, framing.extract, b"\x02")

    def test_a_stream_that_does_not_start_with_stx_is_fatal(self):
        # No resynchronisation: there is no delimiter to hunt for, and section
        # 4.8 has the gateway drop such a connection.
        self.assertRaises(MalformedMessage, framing.extract, b"8=FIXT.1.1\x01")

    def test_a_length_shorter_than_a_header_is_refused(self):
        self.assertRaises(MalformedMessage, framing.extract,
                          b"\x02\x05\x00" + b"\x00" * 10)

    def test_a_corrupted_frame_fails_its_checksum(self):
        broken = bytearray(self.raw)
        broken[55] ^= 0xFF
        self.assertRaises(MalformedMessage, self.codec.decode, bytes(broken))

    def test_the_framer_yields_whole_messages_only(self):
        framer = self.codec.framer()
        self.assertEqual(framer.feed(self.raw[:20]), [])
        self.assertEqual(framer.feed(self.raw[20:] + self.raw[:5]),
                         [self.raw])
        self.assertEqual(framer.feed(self.raw[5:]), [self.raw])


class SessionMessageTest(unittest.TestCase):

    def setUp(self):
        self.codec, self.dictionary = build_codec()

    def round_trip(self, message):
        message.set(C.TARGET_COMP_ID, "BROKER3")
        raw = self.codec.encode(message)
        return self.codec.decode(raw), raw

    def test_logon_carries_the_fields_a_client_sends(self):
        logon = Message.create(C.LOGON)
        logon.set(C.MSG_SEQ_NUM, 388)
        logon.set(C.ENCRYPTED_PASSWORD, "OG13zxchmYeV")
        logon.set(C.NEXT_EXPECTED_MSG_SEQ_NUM, 1)
        logon.set(C.TEST_MESSAGE_INDICATOR, "N")

        decoded, raw = self.round_trip(logon)
        self.assertEqual(raw[22], 0xA4)      # Password, NextExpected, TestMsg
        self.assertEqual(decoded.msg_type, C.LOGON)
        self.assertEqual(decoded.seq_num, 388)
        self.assertEqual(decoded.get(C.ENCRYPTED_PASSWORD), "OG13zxchmYeV")
        self.assertEqual(decoded.get(C.NEXT_EXPECTED_MSG_SEQ_NUM), "1")
        self.assertEqual(decoded.get(C.TEST_MESSAGE_INDICATOR), "N")
        self.assertIsNone(self.dictionary.validate(decoded))

    def test_the_comp_id_on_the_wire_is_the_clients_in_both_directions(self):
        logon = Message.create(C.LOGON)
        logon.set(C.MSG_SEQ_NUM, 1)
        logon.set(C.ENCRYPTED_PASSWORD, "x")
        logon.set(C.NEXT_EXPECTED_MSG_SEQ_NUM, 1)
        decoded, _raw = self.round_trip(logon)
        # Decoded venue-side: the wire's Comp ID is the sender, and the venue
        # fills in its own as the target.
        self.assertEqual(decoded.get(C.SENDER_COMP_ID), "BROKER3")
        self.assertEqual(decoded.get(C.TARGET_COMP_ID), "HKEXSIM")

    def test_a_client_side_codec_mirrors_the_comp_ids(self):
        client = BinaryCodec(binary_layouts.build(self.dictionary), "HKEXSIM",
                             client=True)
        heartbeat = Message.create(C.HEARTBEAT)
        heartbeat.set(C.MSG_SEQ_NUM, 2)
        heartbeat.set(C.SENDER_COMP_ID, "BROKER3")
        heartbeat.set(C.TARGET_COMP_ID, "HKEXSIM")
        raw = client.encode(heartbeat)
        self.assertEqual(raw[10:22], b"BROKER3\x00\x00\x00\x00\x00")
        # And the venue reads its own identity back into the target.
        self.assertEqual(self.codec.decode(raw).get(C.TARGET_COMP_ID),
                         "HKEXSIM")

    def test_resend_request_bounds(self):
        request = Message.create(C.RESEND_REQUEST)
        request.set(C.MSG_SEQ_NUM, 3)
        request.set(C.BEGIN_SEQ_NO, 7)
        request.set(C.END_SEQ_NO, 0)
        decoded, _raw = self.round_trip(request)
        self.assertEqual(decoded.get(C.BEGIN_SEQ_NO), "7")
        self.assertEqual(decoded.get(C.END_SEQ_NO), "0")

    def test_sequence_reset_gap_fill_flag(self):
        reset = Message.create(C.SEQUENCE_RESET)
        reset.set(C.MSG_SEQ_NUM, 5)
        reset.set(C.GAP_FILL_FLAG, C.YES)
        reset.set(C.NEW_SEQ_NO, 12)
        decoded, _raw = self.round_trip(reset)
        self.assertEqual(decoded.get(C.GAP_FILL_FLAG), "Y")
        self.assertEqual(decoded.get(C.NEW_SEQ_NO), "12")

    def test_poss_dup_and_poss_resend_live_in_the_header(self):
        heartbeat = Message.create(C.HEARTBEAT)
        heartbeat.set(C.MSG_SEQ_NUM, 9)
        heartbeat.set(C.POSS_DUP_FLAG, C.YES)
        decoded, raw = self.round_trip(heartbeat)
        self.assertEqual(raw[8:10], b"\x01\x00")
        self.assertEqual(decoded.get(C.POSS_DUP_FLAG), "Y")
        self.assertIsNone(decoded.get(C.POSS_RESEND))

    def test_a_reject_names_the_refused_message_by_number_and_field_by_name(self):
        reject = Message.create(C.REJECT)
        reject.set(C.MSG_SEQ_NUM, 2)
        reject.set(C.REF_SEQ_NUM, 41)
        reject.set(C.SESSION_REJECT_REASON,
                   C.SessionRejectReason.REQUIRED_TAG_MISSING)
        reject.set(C.REF_MSG_TYPE, C.NEW_ORDER_SINGLE)
        reject.set(C.REF_TAG_ID, D.ORDER_QTY)
        raw = self.codec.encode(reject.set(C.TARGET_COMP_ID, "BROKER3"))

        # New Order is message type 11 here, and the field travels by name.
        self.assertIn(b"\x0b", raw)
        self.assertIn(b"OrderQty\x00", raw)
        decoded = self.codec.decode(raw)
        self.assertEqual(decoded.get(C.REF_MSG_TYPE), C.NEW_ORDER_SINGLE)
        self.assertEqual(decoded.get(C.REF_TAG_ID), str(D.ORDER_QTY))

    def test_a_reject_is_replayed_rather_than_gap_filled(self):
        # Section 5.6 lists what may be skipped on a resend, and unlike FIX 4.2
        # practice the Reject is not on it.
        reject = Message.create(C.REJECT)
        reject.set(C.MSG_SEQ_NUM, 2)
        reject.set(C.REF_SEQ_NUM, 1)
        reject.set(C.SESSION_REJECT_REASON, C.SessionRejectReason.OTHER)
        reject.set(C.TARGET_COMP_ID, "BROKER3")
        self.assertFalse(self.codec.is_admin(self.codec.encode(reject)))

        heartbeat = Message.create(C.HEARTBEAT)
        heartbeat.set(C.MSG_SEQ_NUM, 3)
        heartbeat.set(C.TARGET_COMP_ID, "BROKER3")
        self.assertTrue(self.codec.is_admin(self.codec.encode(heartbeat)))


class BusinessMessageTest(unittest.TestCase):

    def setUp(self):
        self.codec, self.dictionary = build_codec()

    def order(self, **overrides):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(C.MSG_SEQ_NUM, 2)
        message.set(C.TARGET_COMP_ID, "BROKER3")
        message.set(D.CL_ORD_ID, "1001")
        message.set(D.NO_PARTY_IDS, 3)
        for party_id, role in (("1003", D.PartyRole.EXECUTING_FIRM),
                               ("BCAN01", D.PartyRole.CLIENT_ID),
                               ("LOC1", D.PartyRole.LOCATION_ID)):
            message.append(D.PARTY_ID, party_id)
            message.append(D.PARTY_ID_SOURCE, D.PartyIDSource.PROPRIETARY)
            message.append(D.PARTY_ROLE, role)
        message.set(D.SECURITY_ID, "00700")
        message.set(D.SECURITY_ID_SOURCE, D.SecurityIDSource.EXCHANGE_SYMBOL)
        message.set(D.SECURITY_EXCHANGE, D.SECURITY_EXCHANGE_VALUE)
        message.set(D.TRANSACT_TIME, "20260820-03:41:54.567932")
        message.set(D.SIDE, D.SideValue.BUY)
        message.set(D.ORD_TYPE, D.OrdType.LIMIT)
        message.set(D.PRICE, "395.800")
        message.set(D.ORDER_QTY, 1000)
        message.set(D.NO_DISCLOSURE_INSTRUCTIONS, 1)
        message.append(D.DISCLOSURE_TYPE, D.DisclosureType.NONE)
        message.append(D.DISCLOSURE_INSTRUCTION, D.DisclosureInstruction.YES)
        for tag, value in overrides.items():
            message.set(int(tag.lstrip("t")), value)
        return message

    def round_trip(self, message):
        return self.codec.decode(self.codec.encode(message))

    def test_a_new_order_survives_the_round_trip(self):
        decoded = self.round_trip(self.order())
        self.assertEqual(decoded.msg_type, C.NEW_ORDER_SINGLE)
        self.assertEqual(decoded.get(D.CL_ORD_ID), "1001")
        self.assertEqual(decoded.get(D.SECURITY_ID), "00700")
        self.assertEqual(decoded.get(D.SIDE), D.SideValue.BUY)
        self.assertEqual(decoded.get(D.ORD_TYPE), D.OrdType.LIMIT)
        self.assertEqual(decoded.get(D.ORDER_QTY), "1000")
        self.assertEqual(decoded.get(D.TRANSACT_TIME),
                         "20260820-03:41:54.567932")
        self.assertIsNone(self.dictionary.validate(decoded))

    def test_a_price_keeps_its_value_if_not_its_trailing_zeros(self):
        # The Decimal type has no notion of how a price was written; what must
        # survive is the value, which the venue parses back to integer ticks.
        self.assertEqual(self.round_trip(self.order()).get(D.PRICE), "395.8")

    def test_the_parties_group_is_rebuilt_from_the_flat_fields(self):
        decoded = self.round_trip(self.order())
        self.assertEqual(decoded.get(D.NO_PARTY_IDS), "3")
        self.assertEqual(decoded.get_all(D.PARTY_ID),
                         ["1003", "LOC1", "BCAN01"])
        self.assertEqual(decoded.get_all(D.PARTY_ROLE),
                         [D.PartyRole.EXECUTING_FIRM, D.PartyRole.LOCATION_ID,
                          D.PartyRole.CLIENT_ID])
        self.assertEqual(decoded.get_all(D.PARTY_ID_SOURCE),
                         [D.PartyIDSource.PROPRIETARY] * 3)

    def test_the_disclosure_group_becomes_a_bit_and_comes_back(self):
        raw = self.codec.encode(self.order())
        decoded = self.codec.decode(raw)
        self.assertEqual(decoded.get(D.NO_DISCLOSURE_INSTRUCTIONS), "1")
        self.assertEqual(decoded.get(D.DISCLOSURE_TYPE), D.DisclosureType.NONE)
        self.assertEqual(decoded.get(D.DISCLOSURE_INSTRUCTION),
                         D.DisclosureInstruction.YES)

    def test_exec_inst_is_respelled_between_the_encodings(self):
        instructions = "%s %s" % (D.ExecInstValue.IGNORE_PRICE_CHECKS,
                                  D.ExecInstValue.IGNORE_NOTIONAL)
        raw = self.codec.encode(self.order(t18=instructions))
        self.assertIn(b"0 1\x00", raw)
        self.assertEqual(self.codec.decode(raw).get(D.EXEC_INST), instructions)

    def test_order_capacity_is_a_number_here_and_a_letter_there(self):
        raw = self.codec.encode(self.order(t528=D.OrderCapacity.PRINCIPAL))
        self.assertEqual(self.codec.decode(raw).get(D.ORDER_CAPACITY),
                         D.OrderCapacity.PRINCIPAL)

    def test_an_odd_lot_order_carries_its_lot_type(self):
        decoded = self.round_trip(self.order(t1093=D.LotType.ODD_LOT))
        self.assertEqual(decoded.get(D.LOT_TYPE), D.LotType.ODD_LOT)

    def test_an_execution_report_maps_status_and_exec_type(self):
        report = Message.create(C.EXECUTION_REPORT)
        report.set(C.MSG_SEQ_NUM, 3)
        report.set(C.TARGET_COMP_ID, "BROKER3")
        report.set(D.CL_ORD_ID, "1001")
        report.set(D.ORDER_ID, "O1")
        report.set(D.EXEC_ID, "E1")
        report.set(D.SECURITY_ID, "00700")
        report.set(D.SECURITY_ID_SOURCE, D.SecurityIDSource.EXCHANGE_SYMBOL)
        report.set(D.TRANSACT_TIME, "20260820-03:41:54.567932")
        report.set(D.SIDE, D.SideValue.SELL)
        report.set(D.ORD_STATUS, D.OrdStatus.EXPIRED)
        report.set(D.EXEC_TYPE, D.ExecType.EXPIRED)
        report.set(D.CUM_QTY, 300)
        report.set(D.LEAVES_QTY, 0)

        raw = self.codec.encode(report)
        decoded = self.codec.decode(raw)
        # OrdStatus is a UInt8 here: Expired is 12, not the FIX character C.
        # ExecType is a Byte, so it keeps the character.
        self.assertEqual(decoded.get(D.ORD_STATUS), D.OrdStatus.EXPIRED)
        self.assertEqual(decoded.get(D.EXEC_TYPE), D.ExecType.EXPIRED)
        self.assertEqual(decoded.get(D.CUM_QTY), "300")

    def test_cancel_and_amend_reject_codes_share_one_tag_and_two_bits(self):
        for exec_type, expected_bit in ((D.ExecType.CANCEL_REJECT, 29),
                                        (D.ExecType.AMEND_REJECT, 36)):
            report = Message.create(C.EXECUTION_REPORT)
            report.set(C.MSG_SEQ_NUM, 4)
            report.set(C.TARGET_COMP_ID, "BROKER3")
            report.set(D.EXEC_TYPE, exec_type)
            report.set(D.CXL_REJ_REASON, D.CxlRejReason.UNKNOWN_ORDER)
            raw = self.codec.encode(report)
            bits, _offset = T.presence_bits(raw, framing.HEADER_BYTES)
            self.assertIn(expected_bit, bits)
            self.assertNotIn(65 - expected_bit, bits)


class UnknownInputTest(unittest.TestCase):

    def setUp(self):
        self.codec, self.dictionary = build_codec()

    def test_an_unsupported_message_type_is_rejected_not_dropped(self):
        # Quote is message type 16; this venue does not implement quoting, so
        # the message must reach the dictionary as something it refuses rather
        # than breaking the session.
        header = framing.Header(msg_type=16, seq_num=6, comp_id="BROKER3")
        raw = framing.pack(header, T.pack_presence([]), b"")
        decoded = self.codec.decode(raw)
        self.assertEqual(decoded.msg_type, "B16")
        failure = self.dictionary.validate(decoded)
        self.assertEqual(failure.reason, C.SessionRejectReason.INVALID_MSGTYPE)

    def test_a_reject_can_still_name_the_type_it_refused(self):
        reject = Message.create(C.REJECT)
        reject.set(C.MSG_SEQ_NUM, 7)
        reject.set(C.TARGET_COMP_ID, "BROKER3")
        reject.set(C.REF_SEQ_NUM, 6)
        reject.set(C.SESSION_REJECT_REASON,
                   C.SessionRejectReason.INVALID_MSGTYPE)
        reject.set(C.REF_MSG_TYPE, "B16")
        decoded = self.codec.decode(self.codec.encode(reject))
        self.assertEqual(decoded.get(C.REF_MSG_TYPE), "B16")

    def test_a_bit_the_layout_does_not_define_stops_the_session(self):
        # The width of an unknown field is unknown, so nothing after it can be
        # parsed: this is a framing failure, not a validation one.
        header = framing.Header(msg_type=1, seq_num=1, comp_id="BROKER3")
        raw = framing.pack(header, T.pack_presence([200]), b"\x00\x00")
        self.assertRaises(MalformedMessage, self.codec.decode, raw)

    def test_a_body_longer_than_the_presence_map_accounts_for_is_refused(self):
        header = framing.Header(msg_type=1, seq_num=1, comp_id="BROKER3")
        raw = framing.pack(header, T.pack_presence([0]), b"\x07\x00\x99")
        self.assertRaises(MalformedMessage, self.codec.decode, raw)

    def test_an_unknown_enumeration_value_reaches_the_dictionary_as_text(self):
        # Refusing it in the codec would drop the session; the client should
        # get the reject the specification names for a bad value.
        report = Message.create(C.NEW_ORDER_SINGLE)
        report.set(C.MSG_SEQ_NUM, 2)
        report.set(C.TARGET_COMP_ID, "BROKER3")
        report.set(D.SIDE, "1")
        raw = bytearray(self.codec.encode(report))
        raw[-5] = 3          # Side = 3, which this dialect does not define
        raw[-4:] = T.crc32c(bytes(raw[:-4])).to_bytes(4, "little")
        decoded = self.codec.decode(bytes(raw))
        self.assertEqual(decoded.get(D.SIDE), "3")
        failure = self.dictionary.validate(decoded)
        self.assertEqual(failure.tag, D.SIDE)


class RenderingTest(unittest.TestCase):
    """What the audit shows when somebody opens a binary message."""

    def setUp(self):
        self.codec, self.dictionary = build_codec()

    def test_the_wire_view_is_a_hex_dump_with_the_password_struck_out(self):
        logon = Message.create(C.LOGON)
        logon.set(C.MSG_SEQ_NUM, 1)
        logon.set(C.TARGET_COMP_ID, "BROKER3")
        logon.set(C.ENCRYPTED_PASSWORD, "SECRET")
        logon.set(C.NEXT_EXPECTED_MSG_SEQ_NUM, 1)

        dump = self.codec.raw_string(self.codec.encode(logon), self.dictionary)
        self.assertIn("0000  02 ", dump)
        self.assertIn("BROKER", dump)
        self.assertNotIn("SECRET", dump)
        self.assertIn("--", dump)

    def test_an_unframeable_message_still_renders(self):
        self.assertIn("0000  ff ff", self.codec.raw_string(b"\xff\xff"))


if __name__ == "__main__":
    unittest.main()
