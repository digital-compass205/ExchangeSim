"""FIX session-layer conformance.

Covers the recovery paths a client's own engine exercises against a real
exchange: gap detection, resend, gap fill, sequence reset, heartbeat policing
and sequence persistence across a restart.
"""

import os
import shutil
import tempfile
import unittest

from exchangesim.fix import constants as C
from exchangesim.fix.dictionary import Failure
from exchangesim.fix.message import Message, decode, encode
from exchangesim.fix.session import SessionState
from exchangesim.fix.store import FileStore

from .fixsupport import CLIENT, SERVER, SessionHarness


class LogonTest(unittest.TestCase):

    def test_logon_is_acknowledged_and_activates_the_session(self):
        harness = SessionHarness().connect()
        harness.logon()

        response = harness.expect_one(C.LOGON)

        self.assertEqual(SessionState.ACTIVE, harness.session.state)
        self.assertEqual("0", response.get(C.ENCRYPT_METHOD))
        self.assertEqual("30", response.get(C.HEART_BT_INT))
        self.assertEqual(SERVER, response.get(C.SENDER_COMP_ID))
        self.assertEqual(CLIENT, response.get(C.TARGET_COMP_ID))
        self.assertEqual(1, response.seq_num)
        self.assertEqual(1, len(harness.application.logons))

    def test_heartbeat_interval_is_taken_from_the_logon(self):
        harness = SessionHarness().connect()
        harness.logon(heartbeat=5)

        self.assertEqual(5, harness.session.heartbeat_interval)
        self.assertEqual("5", harness.expect_one(C.LOGON).get(C.HEART_BT_INT))

    def test_sequence_numbers_advance_after_logon(self):
        harness = SessionHarness().connect()
        harness.logon()

        self.assertEqual(2, harness.store.next_in)
        self.assertEqual(2, harness.store.next_out)

    def test_reset_seq_num_flag_restarts_both_directions(self):
        harness = SessionHarness().connect()
        harness.store.set_next_out(50)
        harness.store.set_next_in(50)

        harness.logon(reset=True)

        response = harness.expect_one(C.LOGON)
        self.assertEqual(C.YES, response.get(C.RESET_SEQ_NUM_FLAG))
        self.assertEqual(1, response.seq_num)
        self.assertEqual(2, harness.store.next_in)

    def test_unsupported_encrypt_method_is_refused(self):
        harness = SessionHarness().connect()
        message = Message.create(C.LOGON)
        message.set(C.ENCRYPT_METHOD, "0")
        message.set(C.HEART_BT_INT, 30)
        # Bypass the harness so an unsupported value reaches the session.
        message.set(C.ENCRYPT_METHOD, "1")
        harness.send(message)

        logout = harness.expect_one(C.LOGOUT)

        self.assertIn("EncryptMethod", logout.get(C.TEXT))
        self.assertTrue(harness.transport.closed)

    def test_first_message_must_be_a_logon(self):
        harness = SessionHarness().connect()
        harness.new_order()

        logout = harness.expect_one(C.LOGOUT)

        self.assertIn("Logon", logout.get(C.TEXT))
        self.assertTrue(harness.transport.closed)

    def test_second_logon_on_a_live_session_is_refused(self):
        harness = SessionHarness().connect()
        harness.logon()
        harness.drain()

        harness.logon()

        logout = harness.expect_one(C.LOGOUT)
        self.assertIn("already logged on", logout.get(C.TEXT))

    def test_logon_below_the_expected_sequence_is_refused(self):
        harness = SessionHarness().connect()
        harness.store.set_next_in(10)

        harness.logon(seq=3)

        logout = harness.expect_one(C.LOGOUT)
        self.assertIn("too low", logout.get(C.TEXT))
        self.assertTrue(harness.transport.closed)


class CompIdTest(unittest.TestCase):

    def test_wrong_sender_comp_id_is_refused(self):
        harness = SessionHarness().connect()
        message = Message.create(C.LOGON)
        message.set(C.ENCRYPT_METHOD, "0")
        message.set(C.HEART_BT_INT, 30)
        harness.send(message, sender="IMPOSTOR")

        logout = harness.expect_one(C.LOGOUT)
        self.assertIn("CompIDs", logout.get(C.TEXT))

    def test_wrong_target_comp_id_is_refused(self):
        harness = SessionHarness().connect()
        message = Message.create(C.LOGON)
        message.set(C.ENCRYPT_METHOD, "0")
        message.set(C.HEART_BT_INT, 30)
        harness.send(message, target="SOMEONE-ELSE")

        self.assertIn("CompIDs", harness.expect_one(C.LOGOUT).get(C.TEXT))

    def test_comp_id_problem_mid_session_rejects_then_logs_out(self):
        harness = SessionHarness().connect()
        harness.logon()
        harness.drain()

        harness.new_order()  # correct CompIDs
        harness.drain()
        message = Message.create(C.HEARTBEAT)
        harness.send(message, sender="IMPOSTOR")

        types = harness.received_types()
        self.assertEqual([C.REJECT, C.LOGOUT], types)


class SequencingTest(unittest.TestCase):

    def setUp(self):
        self.harness = SessionHarness().connect()
        self.harness.logon()
        self.harness.drain()

    def test_in_sequence_application_message_reaches_the_application(self):
        self.harness.new_order(cl_ord_id="ORD-7")

        self.assertEqual(1, len(self.harness.application.messages))
        self.assertEqual("ORD-7", self.harness.application.messages[0].get(11))
        self.assertEqual(3, self.harness.store.next_in)

    def test_sequence_gap_triggers_a_resend_request(self):
        self.harness.new_order(seq=5)

        request = self.harness.expect_one(C.RESEND_REQUEST)

        self.assertEqual("2", request.get(C.BEGIN_SEQ_NO))
        self.assertEqual("0", request.get(C.END_SEQ_NO))
        # The out-of-sequence message must not reach the application yet.
        self.assertEqual([], self.harness.application.messages)

    def test_only_one_resend_request_is_sent_per_gap(self):
        self.harness.new_order(seq=5)
        self.harness.drain()

        self.harness.new_order(seq=6)

        self.assertEqual([], self.harness.received_types())

    def test_queued_messages_are_processed_in_order_once_the_gap_closes(self):
        self.harness.new_order(cl_ord_id="ORD-4", seq=4)
        self.harness.new_order(cl_ord_id="ORD-3", seq=3)
        self.harness.drain()

        # The missing message 2 arrives and unblocks 3 then 4.
        self.harness.new_order(cl_ord_id="ORD-2", seq=2)

        received = [m.get(11) for m in self.harness.application.messages]
        self.assertEqual(["ORD-2", "ORD-3", "ORD-4"], received)
        self.assertEqual(5, self.harness.store.next_in)

    def test_low_sequence_number_causes_logout_and_disconnect(self):
        self.harness.new_order(seq=2)
        self.harness.drain()

        self.harness.new_order(seq=2)

        logout = self.harness.expect_one(C.LOGOUT)
        self.assertIn("too low", logout.get(C.TEXT))
        self.assertTrue(self.harness.transport.closed)

    def test_poss_dup_below_the_expected_sequence_is_ignored_silently(self):
        self.harness.new_order(cl_ord_id="ORD-2", seq=2)
        self.harness.drain()

        self.harness.new_order(cl_ord_id="ORD-2", seq=2, poss_dup=True)

        self.assertEqual([], self.harness.received_types())
        self.assertEqual(1, len(self.harness.application.messages))
        self.assertFalse(self.harness.transport.closed)

    def test_logon_above_the_expected_sequence_requests_resend_after_logon(self):
        harness = SessionHarness().connect()
        harness.logon(seq=5)

        types = harness.received_types()

        # Logon must be acknowledged before the ResendRequest.
        self.assertEqual([C.LOGON, C.RESEND_REQUEST], types)
        self.assertEqual(SessionState.ACTIVE, harness.session.state)

    def test_queued_logon_is_consumed_without_logging_on_twice(self):
        harness = SessionHarness().connect()
        harness.logon(seq=3)
        harness.drain()

        # Fill the gap: messages 1 and 2 arrive.
        harness.new_order(cl_ord_id="ORD-1", seq=1)
        harness.new_order(cl_ord_id="ORD-2", seq=2)

        self.assertEqual(4, harness.store.next_in)
        self.assertEqual(1, len(harness.application.logons))
        self.assertFalse(harness.transport.closed)


class AdminMessageTest(unittest.TestCase):

    def setUp(self):
        self.harness = SessionHarness().connect()
        self.harness.logon()
        self.harness.drain()

    def test_test_request_is_answered_with_a_matching_heartbeat(self):
        message = Message.create(C.TEST_REQUEST)
        message.set(C.TEST_REQ_ID, "PROBE-1")
        self.harness.send(message)

        heartbeat = self.harness.expect_one(C.HEARTBEAT)
        self.assertEqual("PROBE-1", heartbeat.get(C.TEST_REQ_ID))

    def test_heartbeat_is_accepted_silently(self):
        self.harness.send(Message.create(C.HEARTBEAT))
        self.assertEqual([], self.harness.received_types())

    def test_logout_is_acknowledged_and_disconnects(self):
        message = Message.create(C.LOGOUT)
        message.set(C.TEXT, "done for the day")
        self.harness.send(message)

        self.harness.expect_one(C.LOGOUT)
        self.assertTrue(self.harness.transport.closed)
        self.assertEqual(SessionState.DISCONNECTED, self.harness.session.state)
        self.assertEqual(["done for the day"], self.harness.application.logouts)

    def test_sequence_reset_gap_fill_advances_the_expected_number(self):
        message = Message.create(C.SEQUENCE_RESET)
        message.set(C.GAP_FILL_FLAG, C.YES)
        message.set(C.NEW_SEQ_NO, 10)
        self.harness.send(message, seq=2)

        self.assertEqual(10, self.harness.store.next_in)
        self.assertEqual([], self.harness.received_types())

    def test_sequence_reset_in_reset_mode_is_applied_out_of_sequence(self):
        message = Message.create(C.SEQUENCE_RESET)
        message.set(C.NEW_SEQ_NO, 20)
        # Deliberately wrong MsgSeqNum: Reset mode ignores it.
        self.harness.send(message, seq=999)

        self.assertEqual(20, self.harness.store.next_in)

    def test_sequence_reset_below_the_expected_number_is_rejected(self):
        self.harness.new_order(seq=2)
        self.harness.drain()

        message = Message.create(C.SEQUENCE_RESET)
        message.set(C.GAP_FILL_FLAG, C.YES)
        message.set(C.NEW_SEQ_NO, 2)
        self.harness.send(message, seq=3)

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual(str(C.SessionRejectReason.VALUE_INCORRECT),
                         reject.get(C.SESSION_REJECT_REASON))

    def test_sequence_reset_closes_a_gap_and_releases_queued_messages(self):
        self.harness.new_order(cl_ord_id="ORD-5", seq=5)
        self.harness.drain()

        message = Message.create(C.SEQUENCE_RESET)
        message.set(C.GAP_FILL_FLAG, C.YES)
        message.set(C.NEW_SEQ_NO, 5)
        self.harness.send(message, seq=2)

        self.assertEqual(["ORD-5"],
                         [m.get(11) for m in self.harness.application.messages])
        self.assertEqual(6, self.harness.store.next_in)


class ResendTest(unittest.TestCase):

    def setUp(self):
        self.harness = SessionHarness().connect()
        self.harness.logon()
        self.harness.drain()

    def _send_app_messages(self, count):
        """Have the simulator send ``count`` application messages."""
        for index in range(count):
            message = Message.create(C.EXECUTION_REPORT)
            message.set(11, "ORD-%d" % index)
            self.harness.session.send(message)
        self.harness.drain()

    def _resend_request(self, begin, end):
        message = Message.create(C.RESEND_REQUEST)
        message.set(C.BEGIN_SEQ_NO, begin)
        message.set(C.END_SEQ_NO, end)
        self.harness.send(message)
        return self.harness.received()

    def test_application_messages_are_replayed_as_possible_duplicates(self):
        self._send_app_messages(3)  # sequence numbers 2, 3, 4

        replayed = self._resend_request(2, 4)

        self.assertEqual(3, len(replayed))
        for index, message in enumerate(replayed):
            self.assertEqual(C.EXECUTION_REPORT, message.msg_type)
            self.assertEqual(index + 2, message.seq_num)
            self.assertEqual(C.YES, message.get(C.POSS_DUP_FLAG))
            self.assertTrue(message.get(C.ORIG_SENDING_TIME))

    def test_replay_does_not_consume_new_sequence_numbers(self):
        self._send_app_messages(2)
        before = self.harness.store.next_out

        self._resend_request(2, 3)

        self.assertEqual(before, self.harness.store.next_out)

    def test_end_seq_no_of_zero_means_through_the_end(self):
        self._send_app_messages(3)

        replayed = self._resend_request(2, 0)

        self.assertEqual([2, 3, 4], [m.seq_num for m in replayed])

    def test_administrative_messages_are_gap_filled_not_replayed(self):
        # Heartbeats at 2 and 3, then an execution report at 4.
        self.harness.session.send(Message.create(C.HEARTBEAT))
        self.harness.session.send(Message.create(C.HEARTBEAT))
        report = Message.create(C.EXECUTION_REPORT)
        report.set(11, "ORD-X")
        self.harness.session.send(report)
        self.harness.drain()

        replayed = self._resend_request(2, 4)

        self.assertEqual(2, len(replayed))
        gap_fill, resent = replayed
        self.assertEqual(C.SEQUENCE_RESET, gap_fill.msg_type)
        self.assertEqual(C.YES, gap_fill.get(C.GAP_FILL_FLAG))
        self.assertEqual(2, gap_fill.seq_num)
        self.assertEqual("4", gap_fill.get(C.NEW_SEQ_NO))
        self.assertEqual(C.EXECUTION_REPORT, resent.msg_type)
        self.assertEqual(4, resent.seq_num)

    def test_a_trailing_run_of_admin_messages_is_one_gap_fill(self):
        self.harness.session.send(Message.create(C.HEARTBEAT))
        self.harness.session.send(Message.create(C.HEARTBEAT))
        self.harness.drain()

        replayed = self._resend_request(2, 0)

        self.assertEqual(1, len(replayed))
        self.assertEqual(C.SEQUENCE_RESET, replayed[0].msg_type)
        self.assertEqual(2, replayed[0].seq_num)
        self.assertEqual(str(self.harness.store.next_out),
                         replayed[0].get(C.NEW_SEQ_NO))

    def test_resend_beyond_what_was_sent_is_clamped(self):
        self._send_app_messages(1)

        replayed = self._resend_request(2, 999)

        self.assertEqual([2], [m.seq_num for m in replayed])


class DisconnectedSendTest(unittest.TestCase):
    """Messages generated while the client is away, e.g. Cancel on Disconnect."""

    def test_messages_are_numbered_and_stored_while_disconnected(self):
        harness = SessionHarness().connect()
        harness.logon()
        harness.drain()
        harness.session.detach("connection lost")

        report = Message.create(C.EXECUTION_REPORT)
        report.set(11, "ORD-CANCELLED")
        harness.session.send(report)

        self.assertEqual(3, harness.store.next_out)
        stored = harness.store.get_outbound(2, 2)
        self.assertEqual(1, len(stored))
        self.assertIn(b"ORD-CANCELLED", stored[0][1])

    def test_the_client_recovers_them_by_resend_after_reconnecting(self):
        harness = SessionHarness().connect()
        harness.logon()
        harness.drain()
        harness.session.detach("connection lost")

        report = Message.create(C.EXECUTION_REPORT)
        report.set(11, "ORD-CANCELLED")
        harness.session.send(report)

        harness.reconnect()
        harness.logon(seq=2)
        harness.drain()

        message = Message.create(C.RESEND_REQUEST)
        message.set(C.BEGIN_SEQ_NO, 2)
        message.set(C.END_SEQ_NO, 0)
        harness.send(message)

        replayed = harness.received()
        self.assertTrue(any(m.get(11) == "ORD-CANCELLED" for m in replayed))


class HeartbeatTest(unittest.TestCase):

    def setUp(self):
        self.harness = SessionHarness().connect()
        self.harness.logon(heartbeat=10)
        self.harness.drain()
        self.clock = self.harness.clock

    def test_heartbeat_is_emitted_after_an_idle_interval(self):
        self.clock.advance(10.0)
        self.harness.session.tick()

        self.assertEqual([C.HEARTBEAT], self.harness.received_types())

    def test_no_heartbeat_before_the_interval_elapses(self):
        self.clock.advance(5.0)
        self.harness.session.tick()

        self.assertEqual([], self.harness.received_types())

    def test_inbound_silence_triggers_a_test_request(self):
        self.clock.advance(12.0)
        self.harness.session.tick()

        types = self.harness.received_types()
        self.assertIn(C.TEST_REQUEST, types)

    def test_no_answer_to_the_test_request_disconnects(self):
        self.clock.advance(12.0)
        self.harness.session.tick()
        self.harness.drain()

        self.clock.advance(11.0)
        self.harness.session.tick()

        self.assertEqual(C.LOGOUT, self.harness.received()[-1].msg_type)
        self.assertTrue(self.harness.transport.closed)

    def test_answering_the_test_request_keeps_the_session_alive(self):
        self.clock.advance(12.0)
        self.harness.session.tick()
        self.harness.drain()

        self.harness.send(Message.create(C.HEARTBEAT))
        self.clock.advance(11.0)
        self.harness.session.tick()

        self.assertFalse(self.harness.transport.closed)
        self.assertEqual(SessionState.ACTIVE, self.harness.session.state)

    def test_a_zero_interval_disables_policing(self):
        harness = SessionHarness().connect()
        harness.logon(heartbeat=0)
        harness.drain()

        harness.clock.advance(3600.0)
        harness.session.tick()

        self.assertEqual([], harness.received_types())


class ValidationTest(unittest.TestCase):

    def setUp(self):
        self.harness = SessionHarness().connect()
        self.harness.logon()
        self.harness.drain()

    def test_missing_required_field_is_rejected_with_reason_one(self):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(11, "ORD-1")
        message.set(55, "7203")
        message.set(40, "2")
        message.set(60, self.harness.clock.timestamp())
        # Side (54) omitted.
        self.harness.send(message)

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual(str(C.SessionRejectReason.REQUIRED_TAG_MISSING),
                         reject.get(C.SESSION_REJECT_REASON))
        self.assertEqual("54", reject.get(C.REF_TAG_ID))
        self.assertEqual(C.NEW_ORDER_SINGLE, reject.get(C.REF_MSG_TYPE))

    def test_unsupported_enum_value_is_rejected_with_reason_five(self):
        # OrdType 1 (Market) is not in the dialect; only 2 (Limit) is.
        self.harness.new_order()
        self.harness.drain()
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(11, "ORD-2")
        message.set(55, "7203")
        message.set(54, "1")
        message.set(40, "1")
        message.set(60, self.harness.clock.timestamp())
        self.harness.send(message)

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual(str(C.SessionRejectReason.VALUE_INCORRECT),
                         reject.get(C.SESSION_REJECT_REASON))
        self.assertEqual("40", reject.get(C.REF_TAG_ID))

    def test_bad_data_format_is_rejected_with_reason_six(self):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(11, "ORD-1")
        message.set(55, "7203")
        message.set(54, "1")
        message.set(40, "2")
        message.set(38, "not-a-number")
        message.set(60, self.harness.clock.timestamp())
        self.harness.send(message)

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual(str(C.SessionRejectReason.INCORRECT_DATA_FORMAT),
                         reject.get(C.SESSION_REJECT_REASON))

    def test_empty_value_is_rejected_with_reason_four(self):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(11, "ORD-1")
        message.set(55, "")
        message.set(54, "1")
        message.set(40, "2")
        message.set(60, self.harness.clock.timestamp())
        self.harness.send(message)

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual(str(C.SessionRejectReason.TAG_WITHOUT_VALUE),
                         reject.get(C.SESSION_REJECT_REASON))

    def test_unknown_message_type_is_rejected_with_reason_eleven(self):
        self.harness.send(Message.create("ZZ"))

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual(str(C.SessionRejectReason.INVALID_MSGTYPE),
                         reject.get(C.SESSION_REJECT_REASON))

    def test_undefined_tag_is_rejected_with_reason_zero(self):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(11, "ORD-1")
        message.set(55, "7203")
        message.set(54, "1")
        message.set(40, "2")
        message.set(60, self.harness.clock.timestamp())
        message.set(9999, "surprise")
        self.harness.send(message)

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual(str(C.SessionRejectReason.INVALID_TAG_NUMBER),
                         reject.get(C.SESSION_REJECT_REASON))

    def test_a_rejected_message_still_consumes_its_sequence_number(self):
        before = self.harness.store.next_in
        self.harness.send(Message.create("ZZ"))

        self.assertEqual(before + 1, self.harness.store.next_in)

    def test_application_level_failure_produces_a_reject(self):
        self.harness.application.next_failure = Failure(
            C.SessionRejectReason.VALUE_INCORRECT, 55, "unknown symbol")
        self.harness.new_order()

        reject = self.harness.expect_one(C.REJECT)
        self.assertEqual("unknown symbol", reject.get(C.TEXT))

    def test_a_framing_error_terminates_the_session(self):
        self.harness.send_raw(b"8=FIX.4.2\x019=notanumber\x0135=0\x0110=000\x01")

        self.assertEqual(C.LOGOUT, self.harness.received()[-1].msg_type)
        self.assertTrue(self.harness.transport.closed)

    def test_a_bad_checksum_terminates_the_session(self):
        raw = bytearray(encode(Message([(35, "0"), (34, "2"), (49, CLIENT),
                                        (56, SERVER), (52, "20260808-00:00:00.000")])))
        raw[-4:-1] = b"000"
        self.harness.send_raw(bytes(raw))

        self.assertEqual(C.LOGOUT, self.harness.received()[-1].msg_type)
        self.assertTrue(self.harness.transport.closed)


class PersistenceTest(unittest.TestCase):
    """Sequence state must survive a simulator restart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-session-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _store(self):
        return FileStore(os.path.join(self.tmp, "JNXSIM-CLIENT1"))

    def test_sequence_numbers_resume_after_a_restart(self):
        harness = SessionHarness(store=self._store()).connect()
        harness.logon()
        harness.new_order(cl_ord_id="ORD-1")
        harness.new_order(cl_ord_id="ORD-2")
        next_out = harness.store.next_out
        next_in = harness.store.next_in
        harness.store.close()

        restarted = SessionHarness(store=self._store()).connect()
        self.addCleanup(restarted.store.close)

        self.assertEqual(next_out, restarted.store.next_out)
        self.assertEqual(next_in, restarted.store.next_in)

    def test_a_client_can_resend_across_a_simulator_restart(self):
        harness = SessionHarness(store=self._store()).connect()
        harness.logon()
        report = Message.create(C.EXECUTION_REPORT)
        report.set(11, "ORD-BEFORE-RESTART")
        harness.session.send(report)
        harness.store.close()

        restarted = SessionHarness(store=self._store()).connect()
        self.addCleanup(restarted.store.close)
        restarted.logon(seq=restarted.store.next_in)
        restarted.drain()

        request = Message.create(C.RESEND_REQUEST)
        request.set(C.BEGIN_SEQ_NO, 2)
        request.set(C.END_SEQ_NO, 2)
        restarted.send(request)

        replayed = restarted.received()
        self.assertEqual(1, len(replayed))
        self.assertEqual("ORD-BEFORE-RESTART", replayed[0].get(11))
        self.assertEqual(C.YES, replayed[0].get(C.POSS_DUP_FLAG))


if __name__ == "__main__":
    unittest.main()
