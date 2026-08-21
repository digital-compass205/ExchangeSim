"""A binary client against the whole HKEX venue.

The point of these is that nothing below the codec knows which encoding it is
serving: one venue, one set of books, one audit, and a broker on each encoding
trading with the other. What is tested here is the seam -- the session layer,
the handlers and the one message whose two encodings genuinely differ.
"""

import unittest

from exchangesim.audit import DIRECTION_IN
from exchangesim.binary import message as framing
from exchangesim.binary import types as T
from exchangesim.fix import constants as C
from exchangesim.fix.message import Message
from exchangesim.venues.hkex import dictionary as D

from .hkexsupport import (
    BROKER1,
    BROKER3,
    GEM_SYMBOL,
    SYMBOL,
    VenueHarness,
    binary_venue_config,
    party,
    tags,
)


class BinarySessionTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness(binary_venue_config())
        self.addCleanup(self.harness.close)

    def test_a_binary_session_logs_on_without_the_fields_it_has_no_room_for(self):
        client = self.harness.client(BROKER3, logon=False)
        client.logon()
        [logon] = client.received()

        self.assertEqual(logon.msg_type, C.LOGON)
        self.assertTrue(client.session.logged_on)
        self.assertEqual(client.session.wire, "binary")
        # NextExpectedMsgSeqNum is negotiated in both encodings; EncryptMethod,
        # HeartBtInt and DefaultApplVerID are not fields of this one.
        self.assertEqual(logon.get(C.NEXT_EXPECTED_MSG_SEQ_NUM), "2")
        self.assertIsNone(logon.get(C.ENCRYPT_METHOD))
        self.assertIsNone(logon.get(C.HEART_BT_INT))
        self.assertIsNone(logon.get(C.DEFAULT_APPL_VER_ID))

    def test_the_configured_heartbeat_interval_is_kept(self):
        client = self.harness.client(BROKER3)
        self.assertEqual(client.session.heartbeat_interval, 20)

    def test_a_logon_without_a_password_is_refused(self):
        # Password is required of the client (section 7.5.1), and a Logon that
        # fails validation ends the session rather than drawing a Reject.
        client = self.harness.client(BROKER3, logon=False)
        client.logon(password=None)
        self.assertEqual([m.msg_type for m in client.received()], [C.LOGOUT])
        self.assertFalse(client.session.logged_on)

    def test_the_session_list_says_which_encoding_each_speaks(self):
        result = self.harness.command("sessions")
        protocols = dict((row["target_comp_id"], row["protocol"])
                         for row in result["sessions"])
        self.assertEqual(protocols[BROKER1], "fix")
        self.assertEqual(protocols[BROKER3], "binary")

    def test_a_message_type_the_venue_does_not_implement_is_rejected(self):
        client = self.harness.client(BROKER3)
        # Quote (16): defined by the protocol, not implemented by this venue.
        header = framing.Header(msg_type=16, seq_num=client.seq,
                                comp_id=BROKER3)
        client.session.on_data(
            framing.pack(header, T.pack_presence([]), b""))

        [reject] = client.received()
        self.assertEqual(reject.msg_type, C.REJECT)
        self.assertEqual(reject.get(C.SESSION_REJECT_REASON),
                         str(C.SessionRejectReason.INVALID_MSGTYPE))
        self.assertTrue(client.session.logged_on)

    def test_a_frame_that_will_not_parse_ends_the_session(self):
        client = self.harness.client(BROKER3)
        client.session.on_data(b"8=FIXT.1.1\x019=10\x01")
        self.assertFalse(client.session.logged_on)


class BinaryOrderTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness(binary_venue_config())
        self.addCleanup(self.harness.close)
        self.client = self.harness.client(BROKER3)

    def test_an_order_is_accepted_and_reported(self):
        self.client.new_order("B1", quantity=1000, price="395.800")
        [report] = self.client.reports()

        self.assertEqual(
            tags(report, D.CL_ORD_ID, D.EXEC_TYPE, D.ORD_STATUS, D.SIDE,
                 D.ORDER_QTY, D.LEAVES_QTY, D.SECURITY_ID),
            {D.CL_ORD_ID: "B1", D.EXEC_TYPE: D.ExecType.NEW,
             D.ORD_STATUS: D.OrdStatus.NEW, D.SIDE: D.SideValue.BUY,
             D.ORDER_QTY: "1000", D.LEAVES_QTY: "1000",
             D.SECURITY_ID: SYMBOL})
        # The price survives as a value even though the Decimal type has no
        # notion of the trailing zeros the FIX encoding would have carried.
        self.assertEqual(report.get(D.PRICE), "395.8")

    def test_the_report_carries_the_broker_but_not_the_bcan(self):
        # The BCAN is submitted (bit 22 of a New Order) and reaches the order,
        # but the binary Execution Report has no field for it -- unlike the FIX
        # encoding, which echoes the whole <Parties> block back.
        self.client.new_order("B2", quantity=1000, bcan="BCAN01")
        [report] = self.client.reports()
        self.assertEqual(party(report, D.PartyRole.EXECUTING_FIRM), "1003")
        self.assertIsNone(party(report, D.PartyRole.CLIENT_ID))

        order = self.harness.venue.engine.registry.resolve(
            self.client.session.key, "B2")
        self.assertEqual(order.account, "BCAN01")

    def test_an_order_on_the_gem_book_reaches_its_own_segment(self):
        self.client.new_order("B3", symbol=GEM_SYMBOL, quantity=2000,
                              price="0.235")
        [report] = self.client.reports()
        self.assertEqual(report.get(D.ORD_STATUS), D.OrdStatus.NEW)
        self.assertEqual(report.get(D.SECURITY_ID), GEM_SYMBOL)

    def test_an_odd_lot_is_still_refused(self):
        self.client.new_order("B4", quantity=50)
        [report] = self.client.reports()
        self.assertEqual(report.get(D.EXEC_TYPE), D.ExecType.REJECTED)
        self.assertEqual(report.get(D.ORD_REJ_REASON),
                         str(D.OrdRejReason.INCORRECT_QUANTITY))

    def test_an_order_is_cancelled(self):
        self.client.new_order("B5", quantity=1000)
        self.client.drain()
        self.client.cancel("B6", "B5", quantity=1000)
        [report] = self.client.reports()
        self.assertEqual(report.get(D.EXEC_TYPE), D.ExecType.CANCELED)
        self.assertEqual(report.get(D.ORIG_CL_ORD_ID), "B5")
        self.assertEqual(report.get(D.LEAVES_QTY), "0")

    def test_an_order_is_amended(self):
        self.client.new_order("B7", quantity=1000, price="395.800")
        self.client.drain()
        self.client.replace("B8", "B7", quantity=2000, price="395.600")
        [amended] = self.client.reports()
        self.assertEqual(amended.get(D.EXEC_TYPE), D.ExecType.REPLACED)
        self.assertEqual(amended.get(D.ORIG_CL_ORD_ID), "B7")
        self.assertEqual(amended.get(D.ORDER_QTY), "2000")
        self.assertEqual(amended.get(D.PRICE), "395.6")

    def test_a_mass_cancel_is_reported(self):
        self.client.new_order("B9", quantity=1000)
        self.client.drain()
        self.client.mass_cancel("B10")
        received = self.client.received()
        report = [m for m in received
                  if m.msg_type == D.ORDER_MASS_CANCEL_REPORT][-1]
        self.assertEqual(report.get(D.MASS_CANCEL_RESPONSE),
                         D.MassCancelResponse.ALL)
        self.assertEqual(report.get(D.MASS_ACTION_REPORT_ID)[:1], "X")


class CancelRejectEncodingTest(unittest.TestCase):
    """The one message the two encodings do not share.

    FIX refuses a cancel with an OrderCancelReject (35=9); the binary encoding
    has no such message and says the same thing with an Execution Report.
    """

    def setUp(self):
        self.harness = VenueHarness(binary_venue_config())
        self.addCleanup(self.harness.close)

    def test_a_fix_client_still_gets_an_order_cancel_reject(self):
        client = self.harness.client(BROKER1)
        client.cancel("F1", "NOSUCHORDER")
        [reject] = client.received()
        self.assertEqual(reject.msg_type, C.ORDER_CANCEL_REJECT)
        self.assertEqual(reject.get(D.CXL_REJ_REASON),
                         str(D.CxlRejReason.UNKNOWN_ORDER))

    def test_a_binary_client_gets_an_execution_report_instead(self):
        client = self.harness.client(BROKER3)
        client.cancel("B1", "NOSUCHORDER")
        [report] = client.received()

        self.assertEqual(report.msg_type, C.EXECUTION_REPORT)
        self.assertEqual(report.get(D.EXEC_TYPE), D.ExecType.CANCEL_REJECT)
        self.assertEqual(report.get(D.CXL_REJ_REASON),
                         str(D.CxlRejReason.UNKNOWN_ORDER))
        # The fields the binary Execution Report requires and 35=9 has no room
        # for. Nothing identifies the order, so the totals are zero.
        self.assertEqual(report.get(D.CUM_QTY), "0")
        self.assertEqual(report.get(D.LEAVES_QTY), "0")
        self.assertTrue(report.get(D.EXEC_ID))

    def test_a_refused_amend_reports_the_amend_reject_exec_type(self):
        client = self.harness.client(BROKER3)
        client.replace("B2", "NOSUCHORDER")
        [report] = client.received()
        self.assertEqual(report.get(D.EXEC_TYPE), D.ExecType.AMEND_REJECT)

    def test_a_known_order_carries_its_identity_and_running_totals(self):
        client = self.harness.client(BROKER3)
        client.new_order("B3", quantity=1000, price="395.800")
        client.drain()
        # A locked auction period is the venue refusing to touch a live order.
        self.harness.command("state.set", state="PRE_OPEN", market="MAIN")
        self.harness.command("auction.lock", market="MAIN")
        client.drain()

        client.cancel("B4", "B3", quantity=1000)
        [report] = client.received()
        self.assertEqual(report.get(D.EXEC_TYPE), D.ExecType.CANCEL_REJECT)
        self.assertEqual(report.get(D.CXL_REJ_REASON),
                         str(D.CxlRejReason.TOO_LATE_TO_CANCEL))
        self.assertEqual(report.get(D.SECURITY_ID), SYMBOL)
        self.assertEqual(report.get(D.SIDE), D.SideValue.BUY)
        self.assertEqual(report.get(D.ORDER_QTY), "1000")
        self.assertEqual(report.get(D.LEAVES_QTY), "1000")
        self.assertEqual(report.get(D.CUM_QTY), "0")


class CrossEncodingTest(unittest.TestCase):
    """One book, two encodings: the brokers trade with each other."""

    def setUp(self):
        self.harness = VenueHarness(binary_venue_config())
        self.addCleanup(self.harness.close)

    def test_a_binary_order_matches_a_fix_one(self):
        fix_client = self.harness.client(BROKER1)
        binary_client = self.harness.client(BROKER3)

        fix_client.new_order("F1", side=D.SideValue.SELL, quantity=1000,
                             price="395.800")
        fix_client.drain()

        binary_client.new_order("B1", side=D.SideValue.BUY, quantity=1000,
                                price="395.800")

        binary_fills = [r for r in binary_client.reports()
                        if r.get(D.EXEC_TYPE) == D.ExecType.TRADE]
        fix_fills = [r for r in fix_client.reports()
                     if r.get(D.EXEC_TYPE) == D.ExecType.TRADE]

        self.assertEqual(len(binary_fills), 1)
        self.assertEqual(len(fix_fills), 1)
        self.assertEqual(binary_fills[0].get(D.LAST_QTY), "1000")
        self.assertEqual(fix_fills[0].get(D.LAST_QTY), "1000")
        # Each side is told the same price, in its own encoding's spelling.
        self.assertEqual(binary_fills[0].get(D.LAST_PX), "395.8")
        self.assertEqual(fix_fills[0].get(D.LAST_PX), "395.800")
        self.assertEqual(binary_fills[0].get(D.ORD_STATUS),
                         D.OrdStatus.FILLED)

    def test_a_fill_names_its_own_broker(self):
        fix_client = self.harness.client(BROKER1)
        binary_client = self.harness.client(BROKER3)
        fix_client.new_order("F2", side=D.SideValue.SELL, quantity=1000,
                             price="395.800")
        fix_client.drain()
        binary_client.new_order("B2", quantity=1000, price="395.800")

        fill = [r for r in binary_client.reports()
                if r.get(D.EXEC_TYPE) == D.ExecType.TRADE][0]
        self.assertEqual(party(fill, D.PartyRole.EXECUTING_FIRM), "1003")
        # Counterparty Broker ID is bit 31 of the binary Execution Report and
        # PartyRole 17 of the FIX one; this venue populates neither, so the two
        # encodings stay level with each other.
        self.assertIsNone(party(fill, D.PartyRole.CONTRA_FIRM))


class EntitlementTest(unittest.TestCase):
    """Section 7.10, the handshake a gateway does before it trades."""

    def setUp(self):
        self.harness = VenueHarness(binary_venue_config())
        self.addCleanup(self.harness.close)

    def request(self, client, request_id="800102"):
        message = Message.create(D.PARTY_ENTITLEMENT_REQUEST)
        message.set(D.ENTITLEMENTS_REQUEST_ID, request_id)
        client.send(message)
        return client.received()

    def test_a_binary_client_is_told_what_its_brokers_may_do(self):
        client = self.harness.client(BROKER3)
        [report] = self.request(client)

        self.assertEqual(report.msg_type, D.PARTY_ENTITLEMENT_REPORT)
        self.assertEqual(report.get(D.ENTITLEMENTS_REQUEST_ID), "800102")
        self.assertEqual(report.get(D.REQUEST_RESULT), D.RequestResult.VALID)
        self.assertEqual(report.get(D.TOT_NO_PARTY_LIST), "1")
        self.assertEqual(report.get(D.LAST_FRAGMENT), "Y")
        self.assertEqual(report.get(D.PARTY_DETAIL_ID), "1003")
        # Trade, yes; make markets is not offered at all, quoting being unbuilt.
        self.assertEqual(report.get(D.NO_ENTITLEMENTS), "1")
        self.assertEqual(report.get(D.ENTITLEMENT_TYPE),
                         D.EntitlementType.TRADE)
        self.assertEqual(report.get(D.ENTITLEMENT_INDICATOR), "Y")
        self.assertTrue(report.get(D.ENTITLEMENT_ID))

    def test_a_fix_client_gets_the_same_answer_in_its_own_encoding(self):
        client = self.harness.client(BROKER1)
        [report] = self.request(client, request_id="800103")
        self.assertEqual(report.msg_type, D.PARTY_ENTITLEMENT_REPORT)
        self.assertEqual(report.get(D.PARTY_DETAIL_ID), "1001")
        # The wrappers the FIX encoding nests the broker in, which the binary
        # encoding has no bit for and does not carry.
        self.assertEqual(report.get(D.NO_PARTY_ENTITLEMENTS), "1")
        self.assertEqual(report.get(D.NO_PARTY_DETAILS), "1")
        self.assertEqual(report.get(D.PARTY_DETAIL_ROLE),
                         D.PartyDetailRole.EXECUTING_FIRM)

    def test_one_fragment_per_broker_with_the_last_one_marked(self):
        config = binary_venue_config()
        for entry in config.get("fix.sessions"):
            if entry["target_comp_id"] == BROKER3:
                entry["broker_ids"] = ["1003", "1004", "1005"]
        harness = VenueHarness(config)
        self.addCleanup(harness.close)

        client = harness.client(BROKER3)
        reports = self.request(client)
        self.assertEqual([r.get(D.PARTY_DETAIL_ID) for r in reports],
                         ["1003", "1004", "1005"])
        self.assertEqual([r.get(D.LAST_FRAGMENT) for r in reports],
                         ["N", "N", "Y"])
        self.assertEqual(set(r.get(D.TOT_NO_PARTY_LIST) for r in reports),
                         set(["3"]))

    def test_a_session_with_no_brokers_configured_says_so(self):
        config = binary_venue_config()
        for entry in config.get("fix.sessions"):
            if entry["target_comp_id"] == BROKER3:
                entry["broker_ids"] = []
        harness = VenueHarness(config)
        self.addCleanup(harness.close)

        client = harness.client(BROKER3)
        [report] = self.request(client)
        self.assertEqual(report.get(D.REQUEST_RESULT),
                         D.RequestResult.NO_DATA_FOUND)
        self.assertEqual(report.get(D.TOT_NO_PARTY_LIST), "0")
        self.assertEqual(report.get(D.LAST_FRAGMENT), "Y")
        self.assertIsNone(report.get(D.PARTY_DETAIL_ID))

    def test_a_request_without_its_identifier_is_rejected(self):
        client = self.harness.client(BROKER3)
        client.send(Message.create(D.PARTY_ENTITLEMENT_REQUEST))
        [reject] = client.received()
        self.assertEqual(reject.msg_type, C.REJECT)
        self.assertEqual(reject.get(C.SESSION_REJECT_REASON),
                         str(C.SessionRejectReason.REQUIRED_TAG_MISSING))


class BinaryAuditTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness(binary_venue_config())
        self.addCleanup(self.harness.close)

    def test_an_entry_knows_which_encoding_recorded_it(self):
        self.harness.client(BROKER3)
        self.harness.client(BROKER1)
        rows = self.harness.command("audit")["entries"]
        protocols = set(row["protocol"] for row in rows
                        if row["kind"] == "fix")
        self.assertEqual(protocols, set(("binary", "fix")))

    def test_a_binary_entry_opens_as_a_hex_dump_with_no_password_in_it(self):
        client = self.harness.client(BROKER3, logon=False)
        client.logon(password="SUPERSECRET")

        rows = self.harness.command("audit")["entries"]
        logon = [row for row in rows
                 if row["type"] == C.LOGON and row["direction"] == DIRECTION_IN][0]
        entry = self.harness.command("audit.entry", seq=logon["seq"])

        self.assertEqual(entry["entry"]["protocol"], "binary")
        self.assertIn("0000  02 ", entry["wire"])
        self.assertNotIn("SUPERSECRET", entry["wire"])
        # And the field dump names the fields, redacting the same one.
        names = dict((row["name"], row) for row in entry["fields"])
        self.assertEqual(names["SenderCompID"]["value"], BROKER3)
        self.assertTrue(names["EncryptedPassword"]["redacted"])

    def test_a_business_message_is_named_and_summarised(self):
        client = self.harness.client(BROKER3)
        client.new_order("B1", quantity=1000, price="395.800")

        rows = self.harness.command("audit")["entries"]
        order = [row for row in rows
                 if row["type"] == C.NEW_ORDER_SINGLE][0]
        self.assertEqual(order["type_name"], "NewOrderSingle")
        self.assertEqual(order["symbol"], SYMBOL)
        self.assertIn("ClOrdID=B1", order["summary"])


if __name__ == "__main__":
    unittest.main()
