"""The message store and the variable-length record a download is made of.

Both halves of the recovery machinery, below any venue: the ring that decides
what a download can still replay, and the one structure in this protocol that
has no published length. The venue-level flow -- request, header, records,
trailer -- is in ``test_nse.py`` and ``test_nsefo.py``, because which messages
a user was sent is a venue's answer.
"""

import unittest

from exchangesim.fix import constants as C
from exchangesim.fix.message import MalformedMessage, Message
from exchangesim.nnf import layout as L
from exchangesim.nnf import recovery
from exchangesim.nnf import types as T

USER = 40521
OTHER = 40522


def message(msg_type, fields=None):
    built = Message.create(str(msg_type))
    for tag, value in sorted((fields or {}).items()):
        built.set(tag, str(value))
    return built


class SequenceTest(unittest.TestCase):
    """The cursor a client quotes back: the header's own TimeStamp1."""

    def setUp(self):
        self.store = recovery.MessageStore()

    def test_the_cursor_counts_jiffies_from_the_protocols_own_epoch(self):
        # 1 second = 65536 jiffies, from 1980-01-01 -- the same origin the
        # sibling nanosecond Timestamp field uses. One second past the epoch
        # is one jiffy-second, exactly.
        self.assertEqual(65536, T.to_nse_jiffies(T.NSE_EPOCH + 1))
        self.assertEqual(0, T.to_nse_jiffies(T.NSE_EPOCH))

    def test_two_messages_in_one_jiffy_still_get_different_cursors(self):
        # A jiffy is 15 microseconds and this simulator answers an order in
        # less, so the clock alone does not separate two messages. A repeated
        # cursor would make `after` either drop a message or replay one for
        # ever, depending on which side of the comparison it landed.
        first = self.store.sequence(T.NSE_EPOCH + 10.0)
        second = self.store.sequence(T.NSE_EPOCH + 10.0)
        third = self.store.sequence(T.NSE_EPOCH + 10.0)
        self.assertEqual([first + 1, first + 2], [second, third])

    def test_the_cursor_never_goes_backwards_when_the_clock_does(self):
        ahead = self.store.sequence(T.NSE_EPOCH + 100.0)
        behind = self.store.sequence(T.NSE_EPOCH + 1.0)
        self.assertEqual(ahead + 1, behind)


class MessageStoreTest(unittest.TestCase):

    def setUp(self):
        self.store = recovery.MessageStore(capacity=3)

    def fill(self, user, count, first=10):
        for index in range(count):
            self.store.record(user, first + index, message(2073, {37: index}))

    def test_a_download_returns_what_came_after_the_cursor(self):
        self.fill(USER, 3)
        self.assertEqual([11, 12],
                         [seq for seq, _ in self.store.after(USER, 10)])

    def test_a_cursor_of_zero_means_the_whole_trading_day(self):
        self.fill(USER, 3)
        self.assertEqual(3, len(self.store.after(USER, 0)))

    def test_one_users_traffic_cannot_push_anothers_off_the_end(self):
        # Bounded per user, not in total. Sharing one ring would make a quiet
        # user's recovery depend on how busy a stranger was, which is a hole
        # in the very mechanism that exists to close holes.
        self.fill(USER, 3)
        self.fill(OTHER, 9, first=100)
        self.assertEqual(3, self.store.count(USER))
        self.assertEqual(3, self.store.count(OTHER))

    def test_the_oldest_falls_off_when_the_ring_is_full(self):
        self.fill(USER, 5)
        self.assertEqual([12, 13, 14],
                         [seq for seq, _ in self.store.after(USER, 0)])

    def test_a_cursor_older_than_the_ring_is_reported_rather_than_hidden(self):
        # "Nothing since" and "what you asked for is gone" are the same empty
        # download on the wire, so the venue has to be able to tell them apart
        # to say so in a log.
        self.fill(USER, 5)
        self.assertTrue(self.store.truncated(USER, 5))
        self.assertFalse(self.store.truncated(USER, 12))

    def test_what_a_venue_excludes_is_never_stored(self):
        store = recovery.MessageStore(recoverable=lambda code: code != "7021")
        self.assertTrue(store.record(USER, 1, message(2073)))
        self.assertFalse(store.record(USER, 2, message(7021)))
        self.assertEqual(1, store.count(USER))

    def test_a_user_with_no_traffic_downloads_nothing(self):
        self.assertEqual([], self.store.after(USER, 0))
        self.assertFalse(self.store.truncated(USER, 0))


class RecordLayoutTest(unittest.TestCase):
    """MESSAGE_RECORD: a header wrapped around another whole message."""

    PAYLOAD = 9999

    def setUp(self):
        self.header = L.HeaderLayout(40, (
            L.Field(L.TRANSACTION_CODE, "TransactionCode", T.SHORT, 0),
            L.Field(9001, "UserId", T.LONG, 8),
            L.Field(9002, "ErrorCode", T.SHORT, 12),
            L.Reserved(2, 6),
            L.Reserved(14, 24),
            L.Field(9003, "MessageLength", T.SHORT, 38),
        ))
        self.layouts = L.NnfDictionary(self.header)
        self.inner = self.layouts.define(2073, "MS_OE_RESPONSE", 48, (
            L.Field(9010, "OrderNumber", T.DOUBLE, 40),))
        self.record = self.layouts.define_record(7021, "MESSAGE_RECORD",
                                                 self.PAYLOAD)

    def wrapped(self, order_number=7):
        inner = message(2073, {9001: 4242, 9010: order_number})
        payload = self.inner.encode(inner).decode("latin-1")
        return message(7021, {9001: 4242, self.PAYLOAD: payload}), inner

    def test_a_record_is_the_header_plus_whatever_it_carries(self):
        record, _inner = self.wrapped()
        raw = self.record.encode(record)
        self.assertEqual(40 + 48, len(raw))

    def test_the_length_in_the_header_counts_the_payload_too(self):
        # "set to the length of the entire message, including the length of
        # Message Header" -- which for this one structure is not a constant,
        # and is the only way a client can find the end of the record.
        record, _inner = self.wrapped()
        decoded = self.record.decode(self.record.encode(record))
        self.assertEqual("88", decoded.get(9003))

    def test_the_inner_header_is_the_ordinary_one(self):
        # The document says INNER_MESSAGE_HEADER (trader id at offset 0); a
        # real client reads the direct-connection header (transaction code at
        # offset 0). Getting this wrong does not fail cleanly -- half a user
        # id parses as a transaction code -- so it is pinned at the bytes.
        record, _inner = self.wrapped()
        raw = self.record.encode(record)
        self.assertEqual(7021, self.header.transaction_code(raw))
        self.assertEqual(2073, self.header.transaction_code(raw[40:]))

    def test_a_record_round_trips_the_message_it_carries(self):
        record, inner = self.wrapped(order_number=123456789)
        decoded = self.record.decode(self.record.encode(record))
        recovered = self.inner.decode(
            decoded.get(self.PAYLOAD).encode("latin-1"))
        self.assertEqual("2073", recovered.msg_type)
        self.assertEqual("123456789", recovered.get(9010))
        self.assertEqual("4242", recovered.get(9001))

    def test_a_record_has_no_published_length_to_check(self):
        # Every other structure declares a size and `check()` holds it to it.
        # This one cannot, and must say so rather than measuring its header
        # and calling that the body.
        self.assertIsNone(self.record.check())
        self.assertEqual((self.PAYLOAD,), self.record.tags)

    def test_a_record_shorter_than_its_header_is_refused(self):
        self.assertRaises(MalformedMessage, self.record.decode, b"\x00" * 12)

    def test_an_empty_record_is_a_header_and_nothing_else(self):
        raw = self.record.encode(message(7021))
        self.assertEqual(40, len(raw))
        self.assertEqual("", self.record.decode(raw).get(self.PAYLOAD))


if __name__ == "__main__":
    unittest.main()
