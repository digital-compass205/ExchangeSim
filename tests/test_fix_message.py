"""FIX codec: field ordering, BodyLength/CheckSum, framing, error handling."""

import unittest

from exchangesim.fix.message import (
    Framer,
    IncompleteMessage,
    MalformedMessage,
    Message,
    SOH,
    decode,
    encode,
    extract,
)


def raw(text):
    """Build wire bytes from a pipe-delimited string, as specs print them."""
    return text.replace("|", "\x01").encode("latin-1")


class MessageAccessTest(unittest.TestCase):

    def test_set_replaces_in_place_preserving_position(self):
        message = Message()
        message.set(35, "D").set(11, "ORD-1").set(38, "100")
        message.set(11, "ORD-2")

        self.assertEqual("ORD-2", message.get(11))
        self.assertEqual([35, 11, 38], message.tags())

    def test_get_int_returns_default_for_non_numeric(self):
        message = Message().set(38, "not-a-number")
        self.assertIsNone(message.get_int(38))
        self.assertEqual(0, message.get_int(38, 0))

    def test_get_int_returns_default_when_absent(self):
        self.assertEqual(7, Message().get_int(38, 7))

    def test_set_ignores_none_but_set_if_is_explicit(self):
        message = Message().set(1, None).set_if(2, None).set_if(3, "x")
        self.assertEqual([3], message.tags())

    def test_empty_value_is_distinguishable_from_absence(self):
        message = Message().set(55, "")
        self.assertTrue(message.has(55))
        self.assertTrue(message.is_empty(55))
        self.assertFalse(Message().is_empty(55))

    def test_non_string_values_are_coerced(self):
        message = Message().set(38, 100).set(44, 2845.5)
        self.assertEqual("100", message.get(38))
        self.assertEqual("2845.5", message.get(44))


class EncodeTest(unittest.TestCase):

    def test_header_fields_are_emitted_in_protocol_order(self):
        message = Message()
        # Deliberately set out of order.
        message.set(56, "CLIENT").set(34, "2").set(35, "A").set(49, "JNX")
        wire = encode(message)

        self.assertTrue(wire.startswith(b"8=FIX.4.2\x019="))
        body = wire.split(SOH)
        self.assertEqual(b"35=A", body[2])
        self.assertEqual(b"34=2", body[3])
        self.assertEqual(b"49=JNX", body[4])
        self.assertEqual(b"56=CLIENT", body[5])

    def test_body_length_counts_from_msgtype_to_the_soh_before_checksum(self):
        message = Message().set(35, "0").set(34, "1")
        wire = encode(message)

        body = b"35=0\x0134=1\x01"
        self.assertIn(b"9=%d\x01" % len(body), wire)

    def test_checksum_is_three_digits_modulo_256(self):
        wire = encode(Message().set(35, "0").set(34, "1"))
        head = wire[:wire.rfind(b"10=")]
        self.assertEqual(b"10=%03d\x01" % (sum(head) % 256), wire[wire.rfind(b"10="):])

    def test_structural_tags_supplied_by_the_caller_are_not_duplicated(self):
        message = Message().set(8, "FIX.4.4").set(9, "999").set(35, "0").set(10, "123")
        wire = encode(message)

        self.assertEqual(1, wire.count(b"8="))
        self.assertEqual(1, wire.count(b"\x019="))
        self.assertEqual(1, wire.count(b"\x0110="))
        self.assertTrue(wire.startswith(b"8=FIX.4.2"))

    def test_round_trip_preserves_body_fields(self):
        original = Message().set(35, "D").set(34, "5").set(49, "CLIENT") \
            .set(56, "JNX").set(11, "ORD-1").set(55, "7203").set(38, "100")

        restored = decode(encode(original))

        for tag in (35, 34, 49, 56, 11, 55, 38):
            self.assertEqual(original.get(tag), restored.get(tag), "tag %d" % tag)


class DecodeTest(unittest.TestCase):

    def _valid(self, body):
        return encode(Message(body))

    def test_checksum_mismatch_is_rejected(self):
        wire = bytearray(self._valid([(35, "0"), (34, "1")]))
        wire[-4:-1] = b"000"
        with self.assertRaises(MalformedMessage) as caught:
            decode(bytes(wire))
        self.assertIn("CheckSum", str(caught.exception))

    def test_checksum_validation_can_be_skipped(self):
        wire = bytearray(self._valid([(35, "0"), (34, "1")]))
        wire[-4:-1] = b"000"
        message = decode(bytes(wire), validate_checksum=False)
        self.assertEqual("0", message.msg_type)

    def test_missing_begin_string_is_rejected(self):
        with self.assertRaises(MalformedMessage):
            decode(raw("35=0|34=1|10=000|"))

    def test_missing_msg_type_is_rejected(self):
        wire = encode(Message([(34, "1"), (49, "X")]))
        with self.assertRaises(MalformedMessage) as caught:
            decode(wire)
        self.assertIn("MsgType", str(caught.exception))

    def test_non_numeric_tag_is_rejected(self):
        with self.assertRaises(MalformedMessage):
            decode(raw("8=FIX.4.2|9=5|abc=1|10=000|"), validate_checksum=False)

    def test_field_without_equals_is_rejected(self):
        with self.assertRaises(MalformedMessage):
            decode(raw("8=FIX.4.2|9=5|35|10=000|"), validate_checksum=False)

    def test_values_may_contain_equals_signs(self):
        original = Message().set(35, "0").set(58, "a=b=c")
        self.assertEqual("a=b=c", decode(encode(original)).get(58))

    def test_message_must_end_with_soh(self):
        wire = self._valid([(35, "0")])
        with self.assertRaises(MalformedMessage):
            decode(wire[:-1])


class ExtractTest(unittest.TestCase):

    def setUp(self):
        self.one = encode(Message().set(35, "0").set(34, "1"))
        self.two = encode(Message().set(35, "1").set(34, "2").set(112, "TEST"))

    def test_splits_the_first_message_and_returns_the_rest(self):
        first, rest = extract(self.one + self.two)
        self.assertEqual(self.one, first)
        self.assertEqual(self.two, rest)

    def test_partial_message_raises_incomplete(self):
        with self.assertRaises(IncompleteMessage):
            extract(self.one[:-3])

    def test_empty_buffer_raises_incomplete(self):
        with self.assertRaises(IncompleteMessage):
            extract(b"")

    def test_leading_junk_is_skipped_to_resynchronise(self):
        first, _rest = extract(b"garbage" + self.one)
        self.assertEqual(self.one, first)

    def test_sustained_junk_is_rejected_rather_than_buffered_forever(self):
        with self.assertRaises(MalformedMessage):
            extract(b"x" * 200)

    def test_non_numeric_body_length_is_rejected(self):
        with self.assertRaises(MalformedMessage):
            extract(raw("8=FIX.4.2|9=abc|35=0|10=000|"))

    def test_body_length_pointing_away_from_checksum_is_rejected(self):
        # BodyLength too small, so offset arithmetic lands off "10=".
        with self.assertRaises(MalformedMessage) as caught:
            extract(raw("8=FIX.4.2|9=3|35=0|34=1|10=000|"))
        self.assertIn("CheckSum", str(caught.exception))

    def test_absurd_body_length_is_rejected_not_buffered(self):
        with self.assertRaises(MalformedMessage):
            extract(raw("8=FIX.4.2|9=99999999|35=0|10=000|"))


class FramerTest(unittest.TestCase):

    def setUp(self):
        self.framer = Framer()
        self.one = encode(Message().set(35, "0").set(34, "1"))
        self.two = encode(Message().set(35, "1").set(34, "2"))

    def test_two_messages_in_one_chunk(self):
        messages = self.framer.feed(self.one + self.two)
        self.assertEqual([self.one, self.two], messages)

    def test_message_split_across_chunks(self):
        self.assertEqual([], self.framer.feed(self.one[:10]))
        self.assertEqual([self.one], self.framer.feed(self.one[10:]))

    def test_byte_at_a_time_delivery(self):
        collected = []
        for index in range(len(self.one)):
            collected.extend(self.framer.feed(self.one[index:index + 1]))
        self.assertEqual([self.one], collected)

    def test_trailing_partial_is_retained_for_the_next_feed(self):
        messages = self.framer.feed(self.one + self.two[:5])
        self.assertEqual([self.one], messages)
        self.assertEqual(5, self.framer.pending)
        self.assertEqual([self.two], self.framer.feed(self.two[5:]))

    def test_framing_error_propagates(self):
        with self.assertRaises(MalformedMessage):
            self.framer.feed(raw("8=FIX.4.2|9=abc|35=0|10=000|"))


if __name__ == "__main__":
    unittest.main()
