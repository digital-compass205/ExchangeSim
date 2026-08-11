"""The FIXT.1.1 session layer, as HKEX OCG-C uses it.

Everything here is a difference from the FIX 4.2 sessions Japannext runs:
sequence negotiation through NextExpectedMsgSeqNum(789) instead of a pure
ResendRequest flow, the application version carried per session and per message,
and a client-initiated sequence reset that the venue refuses outright.
"""

import unittest

from exchangesim.fix import constants as C
from exchangesim.fix.message import Message
from exchangesim.fix.session import SessionConfig

from .hkexsupport import BROKER1, BROKER2, SERVER, VenueHarness, venue_config


class LogonTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)

    def logon(self, target=BROKER1, **kwargs):
        client = self.harness.client(target, logon=False)
        client.logon(**kwargs)
        return client

    def responses(self, client):
        return client.received()

    def test_the_venue_answers_with_a_logon(self):
        client = self.logon()

        response = self.responses(client)[0]

        self.assertEqual(C.LOGON, response.msg_type)
        self.assertEqual("0", response.get(C.ENCRYPT_METHOD))

    def test_the_response_declares_the_application_version(self):
        client = self.logon()

        response = self.responses(client)[0]

        self.assertEqual(C.ApplVerID.FIX50SP2,
                         response.get(C.DEFAULT_APPL_VER_ID))

    def test_every_outbound_message_carries_ApplVerID(self):
        client = self.logon()
        client.drain()
        client.new_order("S1")

        report = client.received()[0]

        self.assertEqual(C.ApplVerID.FIX50SP2, report.get(C.APPL_VER_ID))

    def test_the_response_states_what_the_venue_next_expects(self):
        client = self.logon()

        response = self.responses(client)[0]

        # The Logon was sequence 1, so the venue next expects 2.
        self.assertEqual("2", response.get(C.NEXT_EXPECTED_MSG_SEQ_NUM))

    def test_the_response_reports_the_session_as_active(self):
        client = self.logon()

        self.assertEqual(C.SessionStatus.ACTIVE,
                         self.responses(client)[0].get(C.SESSION_STATUS))

    def test_a_logon_without_NextExpectedMsgSeqNum_is_refused(self):
        client = self.harness.client(BROKER1, logon=False)
        message = Message.create(C.LOGON)
        message.set(C.ENCRYPT_METHOD, "0")
        message.set(C.HEART_BT_INT, 20)
        message.set(C.DEFAULT_APPL_VER_ID, C.ApplVerID.FIX50SP2)
        client.send(message)

        # The dictionary makes it a required field, so the session never even
        # reaches the negotiation -- it is refused at validation.
        self.assertFalse(client.session.logged_on)

    def test_a_client_may_not_reset_the_sequence_through_logon(self):
        client = self.harness.client(BROKER1, logon=False)
        client.logon(**{"t141": C.YES})

        received = client.received()

        self.assertFalse(client.session.logged_on)
        self.assertEqual(C.LOGOUT, received[-1].msg_type)
        self.assertIn("ResetSeqNumFlag", received[-1].get(C.TEXT))

    def test_a_NextExpectedMsgSeqNum_above_what_we_have_sent_is_refused(self):
        client = self.harness.client(BROKER1, logon=False)
        client.logon(next_expected=99)

        received = client.received()

        self.assertFalse(client.session.logged_on)
        self.assertEqual(C.LOGOUT, received[-1].msg_type)
        self.assertIn("above the next outbound", received[-1].get(C.TEXT))

    def test_a_second_broker_gets_its_own_sequence_stream(self):
        first = self.logon(BROKER1)
        second = self.logon(BROKER2)

        self.assertEqual("1", self.responses(first)[0].get(C.MSG_SEQ_NUM))
        self.assertEqual("1", self.responses(second)[0].get(C.MSG_SEQ_NUM))


class SequenceNegotiationTest(unittest.TestCase):
    """Recovery driven by NextExpectedMsgSeqNum rather than ResendRequest."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)

    def test_a_client_behind_us_is_replayed_without_asking(self):
        client = self.harness.client()
        client.new_order("R1", quantity=100, price="395.800")
        client.drain()

        # It reconnects claiming never to have seen the acknowledgement.
        client.disconnect()
        reconnected = self.harness.client(BROKER1, logon=False)
        reconnected.transport.take()
        reconnected.session.attach(reconnected.transport)
        reconnected.logon(next_expected=2)

        received = reconnected.received()
        replayed = [message for message in received
                    if message.msg_type == C.EXECUTION_REPORT]

        self.assertTrue(replayed, "the missed execution report must be resent")
        self.assertEqual(C.YES, replayed[0].get(C.POSS_DUP_FLAG))
        self.assertEqual("R1", replayed[0].get(11))

    def test_the_replay_gap_fills_over_the_logon_itself(self):
        client = self.harness.client()
        client.new_order("R2", quantity=100, price="395.800")
        client.drain()

        client.disconnect()
        reconnected = self.harness.client(BROKER1, logon=False)
        reconnected.transport.take()
        reconnected.session.attach(reconnected.transport)
        reconnected.logon(next_expected=2)

        resets = [message for message in reconnected.received()
                  if message.msg_type == C.SEQUENCE_RESET]

        self.assertTrue(resets, "the Logon's own sequence number must be filled")
        self.assertEqual(C.YES, resets[-1].get(C.GAP_FILL_FLAG))

    def test_a_client_ahead_of_us_is_not_sent_a_ResendRequest(self):
        """The specification forbids one; the response's 789 says it all."""
        client = self.harness.client(BROKER1, logon=False)
        client.seq = 5
        client.logon()

        received = client.received()

        self.assertEqual([], [message for message in received
                              if message.msg_type == C.RESEND_REQUEST])
        self.assertEqual("1", received[0].get(C.NEXT_EXPECTED_MSG_SEQ_NUM),
                         "the response must state the number that closes the gap")


class SessionConfigDefaultsTest(unittest.TestCase):
    """The FIXT options must be inert unless a venue asks for them."""

    def test_a_plain_config_leaves_every_FIXT_option_off(self):
        config = SessionConfig("SIM", "CLIENT")

        self.assertFalse(config.next_expected_seq_num)
        self.assertTrue(config.allow_logon_reset)
        self.assertIsNone(config.appl_ver_id)
        self.assertIsNone(config.default_appl_ver_id)


class CancelOnDisconnectTest(unittest.TestCase):

    def test_a_broker_configured_for_it_loses_its_resting_orders(self):
        with VenueHarness() as harness:
            client = harness.client(BROKER1)
            client.new_order("CD1", quantity=100, price="393.000")
            client.drain()

            client.disconnect()

            self.assertEqual([], harness.venue.engine.orders(live_only=True))

    def test_a_broker_not_configured_for_it_keeps_them(self):
        with VenueHarness() as harness:
            client = harness.client(BROKER2)
            client.new_order("CD2", quantity=100, price="393.000")
            client.drain()

            client.disconnect()

            self.assertEqual(1, len(harness.venue.engine.orders(live_only=True)))


class DialectTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)

    def test_the_begin_string_is_FIXT(self):
        self.assertEqual("FIXT.1.1", self.harness.venue.dictionary.begin_string)

    def test_the_dialect_defines_no_Symbol_tag(self):
        """HKEX names instruments by SecurityID(48); Symbol(55) does not exist."""
        self.assertIsNone(self.harness.venue.dictionary.field(55))

    def test_a_client_cannot_send_an_execution_report(self):
        client = self.harness.client()
        client.drain()
        client.send(Message.create(C.EXECUTION_REPORT))

        received = client.received()

        self.assertEqual(C.REJECT, received[0].msg_type)
        self.assertEqual(str(C.SessionRejectReason.INVALID_MSGTYPE),
                         received[0].get(C.SESSION_REJECT_REASON))

    def test_TargetSubID_is_not_a_legal_header_tag_here(self):
        client = self.harness.client()
        client.drain()
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(C.TARGET_SUB_ID, "MAIN")
        client.send(message)

        received = client.received()

        self.assertEqual(C.REJECT, received[0].msg_type)


if __name__ == "__main__":
    unittest.main()
