"""The message audit: the ring, its filters, and how an entry reads.

Three things here are load-bearing beyond the obvious. The tail query must scan
backwards and stop, or every poll costs the size of the ring. A cursor whose
entries have been overwritten must say so rather than skipping silently. And the
set of audited commands is pinned, because the flag defaults to True: a query
that forgets to opt out floods the tape, and only a test notices.
"""

import unittest
from datetime import datetime

from exchangesim.audit import (
    Audit,
    DEFAULT_CAPACITY,
    DIRECTION_IN,
    DIRECTION_OUT,
    KIND_CONTROL,
    KIND_FIX,
    MAX_CAPACITY,
)
from exchangesim.control.builtin import register as register_builtin
from exchangesim.control.commands import CommandError, CommandRegistry
from exchangesim.core.clock import FixedClock
from exchangesim.core.config import Config, ConfigError
from exchangesim.fix import constants as C
from exchangesim.fix import render
from exchangesim.fix.dictionary import enum_labels
from exchangesim.fix.message import Message, encode
from exchangesim.venues.base import Venue
from exchangesim.venues.hkex import dictionary as H
from exchangesim.venues.japannext import dictionary as J

from .hkexsupport import VenueHarness as HkexHarness
from .jnxsupport import VenueHarness


def message(msg_type, **tags):
    built = Message.create(msg_type)
    for tag, value in sorted(tags.items()):
        built.set(tag, value)
    return built


def audit_with(count, clock=None, capacity=10):
    audit = Audit(capacity, clock or FixedClock())
    for index in range(count):
        audit.record_message(DIRECTION_IN, "CLIENT1",
                             message=message(C.HEARTBEAT),
                             extracted={C.MSG_SEQ_NUM: str(index + 1)})
    return audit


class RingTest(unittest.TestCase):

    def test_sequence_numbers_do_not_restart_when_entries_are_evicted(self):
        audit = audit_with(15, capacity=10)

        seqs = [entry.seq for entry in audit.entries(limit=50).entries]

        self.assertEqual(list(range(6, 16)), seqs)

    def test_the_ring_holds_only_its_capacity(self):
        audit = audit_with(15, capacity=10)

        self.assertEqual(10, audit.describe()["held"])
        self.assertEqual(15, audit.describe()["recorded"])

    def test_an_entry_is_found_by_sequence_number_not_by_position(self):
        audit = audit_with(15, capacity=10)

        self.assertEqual(12, audit.entry(12).seq)

    def test_an_evicted_entry_is_gone_rather_than_wrong(self):
        audit = audit_with(15, capacity=10)

        self.assertIsNone(audit.entry(3))


class TailTest(unittest.TestCase):
    """``after`` is what makes the live tail cheap; it must also be honest."""

    def test_after_returns_only_what_is_new(self):
        audit = audit_with(10, capacity=50)

        page = audit.entries(after=7)

        self.assertEqual([8, 9, 10], [entry.seq for entry in page.entries])

    def test_after_stops_scanning_at_the_cursor(self):
        """The scan must be proportional to what is new, not to the ring."""
        audit = audit_with(500, capacity=1000)
        visited = []

        class Counting(object):
            def __init__(self, entries):
                self._entries = entries

            def __reversed__(self):
                for entry in reversed(self._entries):
                    visited.append(entry.seq)
                    yield entry

            def __len__(self):
                return len(self._entries)

            def __getitem__(self, index):
                return self._entries[index]

        audit._ring = Counting(list(audit._ring))

        audit.entries(after=497)

        self.assertEqual([500, 499, 498, 497], visited)

    def test_last_seq_advances_past_entries_a_filter_excluded(self):
        audit = audit_with(5, capacity=50)

        page = audit.entries(after=1, kind=KIND_CONTROL)

        self.assertEqual([], page.entries)
        self.assertEqual(5, page.last_seq)

    def test_a_cursor_older_than_the_ring_reports_the_gap(self):
        audit = audit_with(30, capacity=10)

        page = audit.entries(after=2)

        self.assertTrue(page.truncated)
        self.assertEqual(21, page.oldest)

    def test_more_new_entries_than_the_limit_reports_the_gap(self):
        audit = audit_with(20, capacity=50)

        page = audit.entries(after=0, limit=5)

        self.assertTrue(page.truncated)
        self.assertEqual(5, len(page.entries))

    def test_a_tail_that_keeps_up_reports_no_gap(self):
        audit = audit_with(20, capacity=50)

        self.assertFalse(audit.entries(after=18).truncated)


class FilterTest(unittest.TestCase):

    def setUp(self):
        self.clock = FixedClock(datetime(2026, 8, 11, 9, 0, 0))
        self.audit = Audit(50, self.clock)
        self.audit.record_message(DIRECTION_IN, "CLIENT1",
                                  message=message(C.LOGON), type_name="Logon")
        self.clock.advance(60)
        self.audit.record_message(DIRECTION_IN, "CLIENT1",
                                  message=message(C.NEW_ORDER_SINGLE),
                                  type_name="NewOrderSingle", symbol="7203")
        self.clock.advance(60)
        self.audit.record_message(DIRECTION_OUT, "CLIENT1",
                                  message=message(C.EXECUTION_REPORT),
                                  type_name="ExecutionReport", symbol="6758")
        self.audit.record_command("order.new", {"symbol": "7203"},
                                  session="127.0.0.1:5000", symbol="7203")

    def seqs(self, **filters):
        return [entry.seq for entry in self.audit.entries(**filters).entries]

    def test_a_symbol_filter_keeps_the_session_messages_that_explain_it(self):
        self.assertEqual([1, 2, 4], self.seqs(symbol="7203"))

    def test_session_messages_can_be_excluded(self):
        self.assertEqual([2, 4], self.seqs(symbol="7203",
                                           include_session=False))

    def test_direction(self):
        self.assertEqual([3], self.seqs(direction=DIRECTION_OUT))

    def test_kind(self):
        self.assertEqual([4], self.seqs(kind=KIND_CONTROL))

    def test_types_mixes_message_types_and_command_names(self):
        self.assertEqual(
            [1, 4], self.seqs(types=frozenset((C.LOGON, "order.new"))))

    def test_exclude_types_drops_a_type_and_keeps_the_rest(self):
        self.assertEqual(
            [2, 3, 4], self.seqs(exclude_types=frozenset((C.LOGON,))))

    def test_exclude_types_wins_over_a_type_that_was_selected(self):
        """The two filters are set independently, so the hiding one decides."""
        self.assertEqual(
            [4], self.seqs(types=frozenset((C.LOGON, "order.new")),
                           exclude_types=frozenset((C.LOGON,))))

    def test_excluded_entries_still_advance_the_cursor(self):
        """Or a tail hiding heartbeats would rescan them on every poll."""
        page = self.audit.entries(exclude_types=frozenset((C.LOGON,)))

        self.assertEqual(4, page.last_seq)

    def test_since_is_inclusive_of_later_entries_only(self):
        self.assertEqual(
            [2, 3, 4], self.seqs(since=datetime(2026, 8, 11, 9, 0, 30)))

    def test_until_cuts_the_tail_off(self):
        self.assertEqual(
            [1], self.seqs(until=datetime(2026, 8, 11, 9, 0, 30)))

    def test_before_pages_backwards(self):
        self.assertEqual([1, 2], self.seqs(before=3))


class CommandEntryTest(unittest.TestCase):

    def test_a_command_is_recorded_before_it_runs(self):
        """Otherwise it appears below the messages it caused."""
        audit = Audit(50, FixedClock())
        entry = audit.record_command("order.new", {"symbol": "7203"})

        audit.record_message(DIRECTION_OUT, "CLIENT1",
                             message=message(C.EXECUTION_REPORT))

        self.assertLess(entry.seq, audit.entries().entries[-1].seq)

    def test_an_in_flight_command_has_no_verdict_yet(self):
        audit = Audit(50, FixedClock())

        entry = audit.record_command("order.new", {})

        self.assertIsNone(entry.ok)

    def test_completing_keeps_the_code_and_surfaces_the_message(self):
        audit = Audit(50, FixedClock())
        entry = audit.record_command("state.set", {"market": "NOPE"})

        entry.complete(False, {"code": "not_found", "message": "unknown market"})

        self.assertFalse(entry.ok)
        self.assertEqual("unknown market", entry.error)
        self.assertEqual("not_found", entry.detail["error"]["code"])


class CapacityTest(unittest.TestCase):

    def venue(self, **config):
        data = {"venue": "base"}
        data.update(config)
        return Venue(Config(data), _StubReactor(), None)

    def test_the_default_is_used_when_the_config_says_nothing(self):
        self.assertEqual(DEFAULT_CAPACITY, self.venue().audit.capacity)

    def test_zero_switches_recording_off_entirely(self):
        """None, not an empty ring: every hook is then one 'is not None'."""
        self.assertIsNone(self.venue(audit={"capacity": 0}).audit)

    def test_a_capacity_beyond_the_ceiling_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            self.venue(audit={"capacity": MAX_CAPACITY + 1})

    def test_a_non_integer_capacity_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            self.venue(audit={"capacity": "lots"})

    def test_true_is_not_an_integer_here(self):
        with self.assertRaises(ConfigError):
            self.venue(audit={"capacity": True})


class _StubReactor(object):
    def __init__(self):
        self.clock = FixedClock()


class EnumLabelTest(unittest.TestCase):
    """The labels are reversed off the dialect's own constant declarations."""

    def test_a_value_reads_as_its_constant_name(self):
        self.assertEqual({"2": "LIMIT"}, enum_labels(J.OrdType))

    def test_integer_members_are_keyed_by_their_wire_form(self):
        labels = enum_labels(J.OrdRejReason)

        self.assertEqual("PRICE_EXCEEDS_BAND", labels["16"])

    def test_a_docstring_is_not_a_member(self):
        labels = enum_labels(H.ExecRestatementReason)

        self.assertNotIn("__doc__", labels.values())
        self.assertTrue(all(not key.startswith("``") for key in labels))

    def test_an_all_tuple_is_metadata_and_is_skipped(self):
        self.assertNotIn("ALL", enum_labels(J.SubID).values())

    def test_an_all_that_is_a_real_wire_value_is_kept(self):
        """The filter is by type, not by name -- HKEX's ALL means 'cancel all'."""
        self.assertEqual("ALL", enum_labels(H.MassCancelRequestType)["7"])

    def test_a_labels_map_is_metadata_and_is_skipped(self):
        self.assertNotIn("LABELS", enum_labels(H.MarketSegment).values())

    def test_every_dialect_enumeration_reverses_without_junk(self):
        for module in (J, H):
            for name in dir(module):
                candidate = getattr(module, name)
                if not isinstance(candidate, type) or name.startswith("_"):
                    continue
                for value, label in enum_labels(candidate).items():
                    self.assertTrue(label.isupper() or "_" in label,
                                    "%s.%s is not a constant name" % (name, label))
                    self.assertNotIn("=", value)


class FieldLabelTest(unittest.TestCase):

    def setUp(self):
        self.jnx = J.build()
        self.hkex = H.build()

    def test_an_enumerated_value_carries_its_meaning(self):
        self.assertEqual("PARTIALLY_FILLED", self.jnx.field(39).label("1"))

    def test_a_boolean_is_labelled_without_a_map(self):
        self.assertEqual("YES", self.jnx.field(43).label("Y"))

    def test_a_multi_value_field_labels_every_part(self):
        self.assertEqual("POST_ONLY IGNORE_NOTIONAL",
                         self.jnx.field(18).label("6 x"))

    def test_a_sub_id_reads_as_the_market_it_names(self):
        self.assertEqual("J-Market Nighttime Session",
                         self.jnx.field(57).label("NGHT"))

    def test_a_market_segment_reads_as_the_board_it_names(self):
        self.assertEqual("Main Board", self.hkex.field(1300).label("MAIN"))

    def test_an_unconstrained_field_has_nothing_to_add(self):
        self.assertIsNone(self.jnx.field(11).label("ABC-1"))

    def test_a_value_outside_the_enumeration_is_not_invented(self):
        self.assertIsNone(self.jnx.field(39).label("Z"))


class RenderTest(unittest.TestCase):

    def setUp(self):
        self.dictionary = J.build()

    def test_a_message_type_is_named_from_the_message_table(self):
        """There is no MsgType enumeration, and "0" means two different things."""
        self.assertEqual("Heartbeat",
                         render.label_for(35, "0", self.dictionary))
        self.assertEqual("NONE", self.dictionary.field(98).label("0"))

    def test_fields_come_out_in_wire_order_with_repeats_intact(self):
        built = Message.create(C.NEW_ORDER_SINGLE)
        built.append(J.SIDE, "1")
        built.append(J.SIDE, "2")

        rows = render.describe_fields(built, self.dictionary)

        self.assertEqual(["MsgType", "Side", "Side"],
                         [row["name"] for row in rows])

    def test_a_required_field_is_marked(self):
        built = Message.create(C.NEW_ORDER_SINGLE)
        built.set(J.CL_ORD_ID, "A-1")
        built.set(J.ACCOUNT, "ACC")

        rows = dict((row["name"], row["required"])
                    for row in render.describe_fields(built, self.dictionary))

        self.assertTrue(rows["ClOrdID"])
        self.assertFalse(rows["Account"])

    def test_a_summary_names_what_the_message_says(self):
        summary = render.summarise(
            {11: "A-1", 54: "1", 38: "100", 44: "2845.5", 59: "0"},
            self.dictionary)

        self.assertEqual(
            "ClOrdID=A-1 Side=BUY OrderQty=100 Price=2845.5 TimeInForce=DAY",
            summary)

    def test_a_flag_that_is_off_stays_out_of_the_summary(self):
        summary = render.summarise({11: "A-1", 43: "N"}, self.dictionary)

        self.assertEqual("ClOrdID=A-1", summary)

    def test_a_resend_is_visible_in_the_summary(self):
        summary = render.summarise({11: "A-1", 43: "Y"}, self.dictionary)

        self.assertEqual("ClOrdID=A-1 PossDupFlag=YES", summary)

    def test_the_instrument_is_taken_from_whichever_tag_the_dialect_uses(self):
        self.assertEqual("7203", render.symbol_of({55: "7203"}))
        self.assertEqual("700", render.symbol_of({48: "700"}))
        self.assertIsNone(render.symbol_of({11: "A-1"}))


class RedactionTest(unittest.TestCase):
    """A Logon password must not survive into anything the audit shows."""

    def setUp(self):
        self.dictionary = H.build()
        self.message = Message.create(C.LOGON)
        self.message.set(C.ENCRYPT_METHOD, "0")
        self.message.set(C.HEART_BT_INT, "20")
        self.message.set(C.ENCRYPTED_PASSWORD, "hunter2")

    def test_the_field_dump_withholds_the_value(self):
        rows = dict((row["name"], row["value"])
                    for row in render.describe_fields(self.message,
                                                      self.dictionary))

        self.assertEqual(render.REDACTED, rows["EncryptedPassword"])

    def test_the_field_is_marked_so_a_reader_knows_it_was_withheld(self):
        rows = dict((row["name"], row["redacted"])
                    for row in render.describe_fields(self.message,
                                                      self.dictionary))

        self.assertTrue(rows["EncryptedPassword"])
        self.assertFalse(rows["HeartBtInt"])

    def test_the_wire_string_is_rebuilt_rather_than_echoed(self):
        raw = encode(self.message, "FIXT.1.1")

        wire = render.raw_string(raw, self.dictionary)

        self.assertNotIn("hunter2", wire)
        self.assertIn("1402=%s" % render.REDACTED, wire)

    def test_the_rest_of_the_message_still_reads_normally(self):
        raw = encode(self.message, "FIXT.1.1")

        self.assertIn("98=0", render.raw_string(raw, self.dictionary))


class MalformedCaptureTest(unittest.TestCase):
    """The message an audit is opened for is usually the one that failed."""

    def test_bytes_that_will_not_parse_are_still_rendered(self):
        raw = b"8=FIX.4.2\x0135=D\x01nonsense\x0110=000\x01"

        wire = render.raw_string(raw, J.build())

        self.assertIn("35=D", wire)
        self.assertIn("nonsense", wire)

    def test_a_checksum_failure_is_recorded_with_its_reason(self):
        harness = VenueHarness()
        self.addCleanup(harness.close)
        client = harness.client()
        good = encode(_logon_for(client), "FIX.4.2")
        broken = good[:-4] + b"999\x01"

        client.session.on_data(broken)

        # Not the last entry: the venue answers a bad message with a Logout,
        # which is recorded too.
        entry = _failed(harness)
        self.assertFalse(entry.ok)
        self.assertIn("CheckSum", entry.error)
        self.assertEqual(broken, entry.raw)

    def test_a_framing_failure_is_recorded_even_though_no_message_formed(self):
        harness = VenueHarness()
        self.addCleanup(harness.close)
        client = harness.client()

        client.session.on_data(b"8=FIX.4.2\x019=notanumber\x0135=A\x01")

        entry = _failed(harness)
        self.assertFalse(entry.ok)
        self.assertIn("framing error", entry.error)


def _failed(harness):
    """The most recent entry the venue could not accept."""
    failures = [entry for entry in harness.venue.audit.entries(limit=500).entries
                if not entry.ok]
    return failures[-1]


def _logon_for(client):
    built = Message.create(C.LOGON)
    built.set(C.ENCRYPT_METHOD, "0")
    built.set(C.HEART_BT_INT, 30)
    built.set(C.MSG_SEQ_NUM, 99)
    built.set(C.SENDER_COMP_ID, client.session.target_comp_id)
    built.set(C.TARGET_COMP_ID, client.session.sender_comp_id)
    built.set(C.SENDING_TIME, client.clock.timestamp())
    return built


class JapannextAuditTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client()

    def entries(self, **args):
        return self.harness.dispatch("audit", args)["entries"]

    def test_an_order_is_recorded_against_the_instrument_it_names(self):
        self.client.new_order("A-1", price="2845.5")

        rows = [row for row in self.entries() if row["type"] == "D"]

        self.assertEqual(["7203"], [row["symbol"] for row in rows])

    def test_both_sides_of_the_exchange_are_recorded(self):
        self.client.new_order("A-2", price="2845.5")

        directions = set(row["direction"] for row in self.entries())

        self.assertEqual(set(("in", "out")), directions)

    def test_a_control_command_lands_on_the_same_tape(self):
        self.harness.command("order.new", market="DAY", symbol="7203",
                             side="BUY", quantity=100, price="2845.5",
                             owner="WEB")

        rows = [row for row in self.entries() if row["kind"] == "control"]

        self.assertEqual(["order.new"], [row["type"] for row in rows])

    def test_a_command_precedes_the_reports_it_causes(self):
        self.client.new_order("A-3", price="2845.5")
        self.client.drain()
        self.harness.command("order.new", market="DAY", symbol="7203",
                             side="SELL", quantity=100, price="2845.5",
                             owner="WEB")

        rows = self.entries()
        command = [row for row in rows if row["kind"] == "control"][0]
        fills = [row for row in rows
                 if row["type"] == "8" and row["seq"] > command["seq"]]

        self.assertTrue(fills, "the fill should be recorded after the command")

    def test_a_query_is_not_recorded(self):
        """The board asks for these constantly; they would drown the tape."""
        before = self.entries()[-1]["seq"]

        self.harness.command("ladder", market="DAY", symbol="7203")
        self.harness.command("bbo", market="DAY", symbol="7203")

        self.assertEqual(before, self.entries()[-1]["seq"])

    def test_an_entry_opens_with_every_field_named(self):
        self.client.new_order("A-4", price="2845.5")
        order = [row for row in self.entries() if row["type"] == "D"][0]

        detail = self.harness.dispatch("audit.entry", {"seq": order["seq"]})

        named = dict((row["name"], row) for row in detail["fields"])
        self.assertEqual("BUY", named["Side"]["label"])
        self.assertEqual("2845.5", named["Price"]["value"])
        self.assertIn("35=D", detail["wire"])

    def test_a_type_can_be_hidden_rather_than_selected(self):
        """How the board drops the heartbeats of an idle session."""
        self.client.new_order("A-5", price="2845.5")

        types = set(row["type"] for row in self.entries(exclude_types=["D"]))

        self.assertNotIn("D", types)
        self.assertIn("8", types)

    def test_a_hidden_type_may_be_given_as_a_comma_separated_string(self):
        self.client.new_order("A-6", price="2845.5")

        types = set(row["type"] for row in self.entries(exclude_types="D,8"))

        self.assertNotIn("D", types)
        self.assertNotIn("8", types)

    def test_the_filter_vocabulary_comes_from_the_dialect(self):
        types = self.harness.command("audit.types")

        names = dict((row["type"], row["name"]) for row in types["msg_types"])
        self.assertEqual("NewOrderSingle", names["D"])
        self.assertIn("order.new", types["commands"])
        self.assertNotIn("ladder", types["commands"])

    def test_an_evicted_entry_is_reported_as_gone(self):
        with self.assertRaises(CommandError) as caught:
            self.harness.dispatch("audit.entry", {"seq": 999999})

        self.assertEqual("not_found", caught.exception.code)


class HkexAuditTest(unittest.TestCase):

    def test_the_instrument_comes_from_security_id(self):
        harness = HkexHarness()
        self.addCleanup(harness.close)
        client = harness.client()

        client.new_order("H-1", price="395.800")

        rows = [row for row in harness.dispatch("audit", {})["entries"]
                if row["type"] == "D"]
        self.assertEqual(["700"], [row["symbol"] for row in rows])


class DisabledAuditTest(unittest.TestCase):

    def setUp(self):
        from .jnxsupport import venue_config
        self.harness = VenueHarness(venue_config(audit={"capacity": 0}))
        self.addCleanup(self.harness.close)

    def test_the_venue_has_no_recorder_at_all(self):
        self.assertIsNone(self.harness.venue.audit)

    def test_messages_still_flow(self):
        client = self.harness.client()

        client.new_order("D-1", price="2845.5")

        self.assertTrue(client.reports())

    def test_the_command_says_so_rather_than_returning_nothing(self):
        with self.assertRaises(CommandError) as caught:
            self.harness.command("audit")

        self.assertEqual("conflict", caught.exception.code)


class AuditedCommandsTest(unittest.TestCase):
    """The audit flag defaults to True, and this is what makes that safe.

    A new command that changes venue state is recorded without its author
    having to remember. The cost is the other direction: a new *query* that
    forgets ``audit=False`` fills the tape with the fact that somebody is
    watching -- and on a venue nobody has open, nothing would reveal it. So the
    set is pinned here, and either kind of new command fails this test until
    somebody chooses deliberately.
    """

    SHARED = frozenset((
        "state.set", "state.clear", "stp.set",
        "instrument.set", "instrument.add", "instrument.remove",
        "reference.reload", "stats.reset",
        "order.new", "order.cancel", "orders.cancel_all",
        "behaviour.set", "behaviour.clear",
        "session.reset", "session.kill",
    ))

    HKEX_ONLY = frozenset((
        "smp.register", "smp.clear", "auction.lock", "auction.reference",
    ))

    def audited(self, harness):
        return frozenset(command.name
                         for command in harness.registry.commands()
                         if command.audit)

    def test_japannext_audits_exactly_the_commands_that_change_something(self):
        harness = VenueHarness()
        self.addCleanup(harness.close)
        register_builtin(harness.registry)

        self.assertEqual(self.SHARED, self.audited(harness))

    def test_hkex_adds_only_its_own_mutating_commands(self):
        harness = HkexHarness()
        self.addCleanup(harness.close)
        register_builtin(harness.registry)

        self.assertEqual(self.SHARED | self.HKEX_ONLY, self.audited(harness))

    def test_no_built_in_command_is_audited(self):
        """`auth` carries a token; the rest are housekeeping a client repeats."""
        registry = CommandRegistry()
        register_builtin(registry)

        self.assertEqual([], [command.name for command in registry.commands()
                              if command.audit])


if __name__ == "__main__":
    unittest.main()
