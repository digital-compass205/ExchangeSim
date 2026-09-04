"""The NNF wire, from the bytes upwards: data types and the Chapter 10 packet.

Byte order and field width are pinned against **literal expected bytes**
throughout, never against this package's own decoder. A round trip proves only
that the encoder and the decoder agree with each other, which they would even if
both were little-endian -- and that is precisely the bug these tests exist to
catch, because the other binary venue here *is* little-endian.
"""

import unittest

from exchangesim.fix.message import IncompleteMessage, MalformedMessage
from exchangesim.nnf import packet
from exchangesim.nnf import types as T


class WireTypeTest(unittest.TestCase):
    """Every type, against bytes written out by hand from the specification."""

    def test_short_is_two_bytes_big_endian(self):
        self.assertEqual(T.SHORT.pack(2000), b"\x07\xd0")
        self.assertEqual(T.SHORT.unpack(b"\x07\xd0", 0), 2000)

    def test_long_is_four_bytes_not_eight(self):
        # The C names, not Python's: LONG is 4, LONG LONG is 8. A Long that
        # quietly meant eight frames plausibly and then desynchronises.
        self.assertEqual(T.LONG.width, 4)
        self.assertEqual(T.LONG_LONG.width, 8)
        self.assertEqual(T.LONG.pack(1), b"\x00\x00\x00\x01")
        self.assertEqual(T.LONG_LONG.pack(1), b"\x00" * 7 + b"\x01")

    def test_signed_types_carry_the_minus_one_sentinel(self):
        for wire in (T.CHAR, T.SHORT, T.LONG, T.LONG_LONG):
            raw = wire.pack(-1)
            self.assertEqual(raw, b"\xff" * wire.width, wire.name)
            self.assertEqual(wire.unpack(raw, 0), -1, wire.name)

    def test_unsigned_types_read_the_high_bit_as_magnitude(self):
        self.assertEqual(T.UINT.unpack(b"\xff\xff", 0), 65535)
        self.assertEqual(T.UNSIGNED_LONG.unpack(b"\xff\xff\xff\xff", 0), 4294967295)

    def test_range_is_enforced_rather_than_truncated(self):
        self.assertRaises(ValueError, T.SHORT.pack, 32768)
        self.assertRaises(ValueError, T.SHORT.pack, -32769)
        self.assertRaises(ValueError, T.UINT.pack, -1)

    def test_missing_value_packs_as_zero(self):
        # "All numeric data must be set to zero (0) before sending to the host,
        # unless a value is assigned to it."
        self.assertEqual(T.LONG.pack(None), b"\x00\x00\x00\x00")
        self.assertEqual(T.SHORT.default, 0)

    def test_double_is_ieee_big_endian(self):
        self.assertEqual(T.DOUBLE.pack(1.0), b"\x3f\xf0\x00\x00\x00\x00\x00\x00")

    def test_double_returns_a_whole_number_as_an_integer(self):
        # The OrderNumber travels as a DOUBLE. Everything above the codec should
        # see 12345678901234, not 1.2345678901234e+13.
        raw = T.DOUBLE.pack(12345678901234)
        value = T.DOUBLE.unpack(raw, 0)
        self.assertEqual(value, 12345678901234)
        self.assertIsInstance(value, int)

    def test_double_keeps_a_fraction_when_there_is_one(self):
        self.assertEqual(T.DOUBLE.unpack(T.DOUBLE.pack(1.5), 0), 1.5)

    def test_a_field_reaching_past_the_end_is_malformed(self):
        self.assertRaises(MalformedMessage, T.LONG.unpack, b"\x00\x00", 0)
        self.assertRaises(MalformedMessage, T.SHORT.unpack, b"\x00\x00", 1)


class CharTest(unittest.TestCase):
    """Blank-padded on the way out, tolerant on the way in."""

    def test_written_blank_padded_and_left_justified(self):
        self.assertEqual(T.Char(5).pack("ABC"), b"ABC  ")

    def test_right_justification_pads_in_front(self):
        self.assertEqual(T.Char(5, "right").pack("ABC"), b"  ABC")

    def test_a_value_that_exactly_fills_the_width_is_not_truncated(self):
        self.assertEqual(T.Char(3).pack("ABC"), b"ABC")
        self.assertEqual(T.Char(3).unpack(b"ABC", 0), "ABC")

    def test_an_over_long_value_is_cut_to_the_width(self):
        self.assertEqual(T.Char(3).pack("ABCDE"), b"ABC")

    def test_blank_padding_is_stripped_on_the_way_in(self):
        self.assertEqual(T.Char(5).unpack(b"ABC  ", 0), "ABC")
        self.assertEqual(T.Char(5, "right").unpack(b"  ABC", 0), "ABC")

    def test_nul_padding_is_read_even_though_the_document_forbids_writing_it(self):
        # A real client may NUL-terminate within the width regardless of what
        # Chapter 2 asks for. Reading only blanks would leave the NULs in the
        # value, which round-trips through the simulator and fails nowhere until
        # it reaches somebody else's decoder.
        self.assertEqual(T.Char(5).unpack(b"ABC\x00\x00", 0), "ABC")
        self.assertEqual(T.Char(8).unpack(b"AB\x00 junk"[:8], 0), "AB")

    def test_an_empty_value_is_all_blanks(self):
        self.assertEqual(T.Char(4).pack(None), b"    ")
        self.assertEqual(T.Char(4).unpack(b"    ", 0), "")

    def test_reserved_is_written_as_nulls_and_read_as_nothing(self):
        # "All reserved fields should be mapped to CHAR buffer and initialized
        # to NULL" -- the one place in the protocol where NUL is right.
        reserved = T.Reserved(4)
        self.assertEqual(reserved.pack(), b"\x00\x00\x00\x00")
        self.assertIsNone(reserved.unpack(b"junk", 0))


class EpochTest(unittest.TestCase):
    """Times are seconds from 1 January 1980, not from the Unix epoch."""

    def test_the_epoch_is_1980(self):
        self.assertEqual(T.NSE_EPOCH, 315532800)

    def test_conversion_round_trips(self):
        self.assertEqual(T.to_nse_seconds(T.NSE_EPOCH), 0)
        self.assertEqual(T.from_nse_seconds(0), T.NSE_EPOCH)
        self.assertEqual(T.from_nse_seconds(T.to_nse_seconds(1700000000)),
                         1700000000)

    def test_nanoseconds_keep_the_fraction(self):
        self.assertEqual(T.to_nse_nanoseconds(T.NSE_EPOCH + 1.5), 1500000000)


class PacketTest(unittest.TestCase):
    """Length(2) + SequenceNumber(4) + checksum(16) + data."""

    def body(self, size=40):
        return bytes(bytearray(i % 256 for i in range(size)))

    def test_prefix_is_twenty_two_bytes(self):
        self.assertEqual(packet.PREFIX_BYTES, 22)
        raw = packet.pack(self.body())
        self.assertEqual(len(raw), 22 + 40)

    def test_length_counts_the_prefix_and_is_big_endian(self):
        raw = packet.pack(self.body())
        self.assertEqual(raw[:2], b"\x00\x3e")     # 62 = 22 + 40

    def test_sequence_number_is_four_bytes_big_endian(self):
        raw = packet.pack(self.body(), sequence=7)
        self.assertEqual(raw[2:6], b"\x00\x00\x00\x07")

    def test_checksum_defaults_to_the_md5_of_the_data_alone(self):
        data = self.body()
        raw = packet.pack(data)
        self.assertEqual(raw[6:22], packet.md5_digest(data))
        # Not of the whole packet: that is the mistake the document warns off.
        self.assertNotEqual(raw[6:22], packet.md5_digest(raw))

    def test_an_explicit_checksum_displaces_the_digest(self):
        tag = bytes(bytearray(range(100, 116)))
        raw = packet.pack(self.body(), checksum=tag)
        self.assertEqual(raw[6:22], tag)

    def test_a_checksum_of_the_wrong_width_is_refused(self):
        self.assertRaises(ValueError, packet.pack, self.body(), 0, b"short")

    def test_unpack_prefix_reads_what_pack_wrote(self):
        data = self.body()
        prefix = packet.unpack_prefix(packet.pack(data, sequence=9))
        self.assertEqual(prefix.length, 62)
        self.assertEqual(prefix.sequence, 9)
        self.assertEqual(prefix.checksum, packet.md5_digest(data))

    def test_data_of_returns_the_message(self):
        data = self.body()
        self.assertEqual(packet.data_of(packet.pack(data)), data)

    def test_a_packet_over_the_maximum_is_refused_on_the_way_out(self):
        self.assertRaises(ValueError, packet.pack, self.body(1003))


class ExtractTest(unittest.TestCase):
    """Framing is exact: there is no delimiter to resynchronise on."""

    def packet_of(self, size=40, sequence=0):
        return packet.pack(bytes(bytearray(size)), sequence=sequence)

    def test_one_packet_leaves_no_remainder(self):
        raw = self.packet_of()
        extracted, rest = packet.extract(raw)
        self.assertEqual(extracted, raw)
        self.assertEqual(rest, b"")

    def test_two_packets_in_one_buffer_are_split(self):
        first, second = self.packet_of(40, 1), self.packet_of(48, 2)
        extracted, rest = packet.extract(first + second)
        self.assertEqual(extracted, first)
        self.assertEqual(rest, second)

    def test_fewer_bytes_than_the_length_field_is_incomplete(self):
        self.assertRaises(IncompleteMessage, packet.extract, b"\x00")

    def test_a_truncated_packet_is_incomplete_not_malformed(self):
        raw = self.packet_of()
        self.assertRaises(IncompleteMessage, packet.extract, raw[:-1])

    def test_a_length_below_the_minimum_is_malformed(self):
        self.assertRaises(MalformedMessage, packet.extract, b"\x00\x08" + b"\x00" * 20)

    def test_a_length_over_the_maximum_is_malformed(self):
        # This is what a little-endian length looks like: 62 written the wrong
        # way round reads as 15872, well past the 1024 byte cap.
        self.assertRaises(MalformedMessage, packet.extract,
                          b"\x3e\x00" + b"\x00" * 60)

    def test_unpack_prefix_rejects_a_length_that_disagrees_with_the_frame(self):
        raw = self.packet_of()
        self.assertRaises(MalformedMessage, packet.unpack_prefix, raw + b"\x00")


class FramerTest(unittest.TestCase):

    def packet_of(self, size=40, sequence=0):
        return packet.pack(bytes(bytearray(size)), sequence=sequence)

    def test_two_packets_arrive_from_one_chunk(self):
        framer = packet.Framer()
        first, second = self.packet_of(40, 1), self.packet_of(44, 2)
        self.assertEqual(framer.feed(first + second), [first, second])
        self.assertEqual(framer.pending, 0)

    def test_a_partial_packet_is_held_until_the_rest_arrives(self):
        framer = packet.Framer()
        raw = self.packet_of()
        self.assertEqual(framer.feed(raw[:30]), [])
        self.assertEqual(framer.pending, 30)
        self.assertEqual(framer.feed(raw[30:]), [raw])
        self.assertEqual(framer.pending, 0)

    def test_a_packet_split_inside_its_length_field_is_held(self):
        framer = packet.Framer()
        raw = self.packet_of()
        self.assertEqual(framer.feed(raw[:1]), [])
        self.assertEqual(framer.feed(raw[1:]), [raw])

    def test_a_bad_length_raises_rather_than_resynchronising(self):
        framer = packet.Framer()
        self.assertRaises(MalformedMessage, framer.feed, b"\x3e\x00" + b"\x00" * 60)

    def test_reset_drops_what_was_buffered(self):
        framer = packet.Framer()
        framer.feed(self.packet_of()[:10])
        framer.reset()
        self.assertEqual(framer.pending, 0)


if __name__ == "__main__":
    unittest.main()
