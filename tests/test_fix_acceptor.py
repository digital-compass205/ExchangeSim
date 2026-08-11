"""Acceptor: CompID routing, the one-connection-per-session rule, tick wiring."""

import os
import shutil
import socket
import tempfile
import unittest

from exchangesim.core.clock import FixedClock
from exchangesim.core.reactor import Reactor
from exchangesim.fix import constants as C
from exchangesim.fix.acceptor import Acceptor, SessionManager
from exchangesim.fix.message import Framer, Message, decode, encode
from exchangesim.fix.session import SessionConfig

from .fixsupport import CLIENT, SERVER, RecordingApplication, test_dictionary
from .support import connect, pump

OTHER_CLIENT = "CLIENT2"


class FixClient(object):
    """A blocking client socket that speaks just enough FIX to connect."""

    def __init__(self, address, sender=CLIENT, target=SERVER):
        self.sock = connect(address)
        self.sender = sender
        self.target = target
        self.seq = 1
        self._framer = Framer()
        self.clock = FixedClock()

    def send(self, message):
        message.set(C.MSG_SEQ_NUM, self.seq)
        self.seq += 1
        message.set(C.SENDER_COMP_ID, self.sender)
        message.set(C.TARGET_COMP_ID, self.target)
        message.set(C.SENDING_TIME, self.clock.timestamp())
        self.sock.sendall(encode(message))
        return message

    def logon(self, heartbeat=30):
        message = Message.create(C.LOGON)
        message.set(C.ENCRYPT_METHOD, "0")
        message.set(C.HEART_BT_INT, heartbeat)
        return self.send(message)

    def send_bytes(self, data):
        self.sock.sendall(data)

    def receive(self):
        """Read whatever is available now, returning decoded messages."""
        self.sock.settimeout(0.2)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return []
        if not chunk:
            return []
        return [decode(raw) for raw in self._framer.feed(chunk)]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class AcceptorTest(unittest.TestCase):

    def setUp(self):
        self.clock = FixedClock()
        self.reactor = Reactor(self.clock)
        self.addCleanup(self.reactor.close)

        self.manager = SessionManager(self.clock, test_dictionary())
        self.applications = {}
        for target in (CLIENT, OTHER_CLIENT):
            application = RecordingApplication()
            self.applications[target] = application
            self.manager.add(
                SessionConfig(sender_comp_id=SERVER, target_comp_id=target),
                application)
        self.addCleanup(self.manager.close)

        self.acceptor = Acceptor(self.reactor, self.manager)
        self.address = self.acceptor.start("127.0.0.1", 0)
        self.addCleanup(self.acceptor.stop)

    def _client(self, sender=CLIENT, target=SERVER):
        client = FixClient(self.address, sender, target)
        self.addCleanup(client.close)
        return client

    # -- routing -----------------------------------------------------------

    def test_a_known_client_is_routed_to_its_session_and_logs_on(self):
        client = self._client()
        client.logon()
        pump(self.reactor)

        received = client.receive()

        self.assertEqual(1, len(received))
        self.assertEqual(C.LOGON, received[0].msg_type)
        self.assertEqual(SERVER, received[0].get(C.SENDER_COMP_ID))
        self.assertEqual(CLIENT, received[0].get(C.TARGET_COMP_ID))
        self.assertEqual(1, len(self.applications[CLIENT].logons))

    def test_each_client_reaches_its_own_session(self):
        first = self._client(sender=CLIENT)
        second = self._client(sender=OTHER_CLIENT)
        first.logon()
        second.logon()
        pump(self.reactor)

        self.assertEqual(1, len(self.applications[CLIENT].logons))
        self.assertEqual(1, len(self.applications[OTHER_CLIENT].logons))

    def test_an_unknown_sender_comp_id_is_refused_with_a_logout(self):
        client = self._client(sender="STRANGER")
        client.logon()
        pump(self.reactor)

        received = client.receive()

        self.assertEqual(1, len(received))
        self.assertEqual(C.LOGOUT, received[0].msg_type)
        self.assertIn("unknown", received[0].get(C.TEXT))

    def test_an_unknown_target_comp_id_is_refused(self):
        client = self._client(target="NOT-THIS-VENUE")
        client.logon()
        pump(self.reactor)

        received = client.receive()
        self.assertEqual(C.LOGOUT, received[0].msg_type)

    def test_application_messages_flow_after_routing(self):
        client = self._client()
        client.logon()
        pump(self.reactor)
        client.receive()

        order = Message.create(C.NEW_ORDER_SINGLE)
        order.set(11, "ORD-1")
        order.set(55, "7203")
        order.set(54, "1")
        order.set(40, "2")
        order.set(38, "100")
        order.set(44, "2845.5")
        order.set(60, self.clock.timestamp())
        client.send(order)
        pump(self.reactor)

        messages = self.applications[CLIENT].messages
        self.assertEqual(1, len(messages))
        self.assertEqual("ORD-1", messages[0].get(11))

    # -- one connection per session ----------------------------------------

    def test_a_second_connection_to_a_live_session_is_refused(self):
        first = self._client()
        first.logon()
        pump(self.reactor)
        first.receive()

        second = self._client()
        second.logon()
        pump(self.reactor)

        received = second.receive()
        self.assertEqual(1, len(received))
        self.assertEqual(C.LOGOUT, received[0].msg_type)
        self.assertIn("already has an active connection", received[0].get(C.TEXT))

    def test_the_incumbent_connection_is_not_disturbed_by_the_refusal(self):
        first = self._client()
        first.logon()
        pump(self.reactor)
        first.receive()

        second = self._client()
        second.logon()
        pump(self.reactor)
        second.receive()

        # The original session is still active and still serving.
        session = self.manager.get(SERVER, CLIENT)
        self.assertTrue(session.logged_on)

        request = Message.create(C.TEST_REQUEST)
        request.set(C.TEST_REQ_ID, "STILL-ALIVE")
        first.send(request)
        pump(self.reactor)

        replies = first.receive()
        self.assertEqual(C.HEARTBEAT, replies[0].msg_type)
        self.assertEqual("STILL-ALIVE", replies[0].get(C.TEST_REQ_ID))

    def test_reconnecting_after_a_disconnect_is_allowed(self):
        first = self._client()
        first.logon()
        pump(self.reactor)
        first.receive()
        first.close()
        pump(self.reactor)

        self.assertFalse(self.manager.get(SERVER, CLIENT).connected)

        # A reconnecting client continues its sequence rather than restarting.
        second = self._client()
        second.seq = 2
        second.logon(heartbeat=30)
        pump(self.reactor)

        received = second.receive()
        self.assertEqual(C.LOGON, received[0].msg_type)

    def test_reconnecting_from_sequence_one_without_a_reset_is_refused(self):
        first = self._client()
        first.logon()
        pump(self.reactor)
        first.receive()
        first.close()
        pump(self.reactor)

        second = self._client()
        second.logon()  # seq 1 again, no ResetSeqNumFlag
        pump(self.reactor)

        received = second.receive()
        self.assertEqual(C.LOGOUT, received[0].msg_type)
        self.assertIn("too low", received[0].get(C.TEXT))

    def test_reconnecting_with_reset_seq_num_flag_starts_over(self):
        first = self._client()
        first.logon()
        pump(self.reactor)
        first.receive()
        first.close()
        pump(self.reactor)

        second = self._client()
        logon = Message.create(C.LOGON)
        logon.set(C.ENCRYPT_METHOD, "0")
        logon.set(C.HEART_BT_INT, 30)
        logon.set(C.RESET_SEQ_NUM_FLAG, C.YES)
        second.send(logon)
        pump(self.reactor)

        received = second.receive()
        self.assertEqual(C.LOGON, received[0].msg_type)
        self.assertEqual(1, received[0].seq_num)

    def test_disconnect_notifies_the_application(self):
        client = self._client()
        client.logon()
        pump(self.reactor)
        client.receive()

        client.close()
        pump(self.reactor)

        self.assertEqual(1, len(self.applications[CLIENT].logouts))

    # -- malformed input ---------------------------------------------------

    def test_junk_before_any_valid_message_closes_the_connection(self):
        client = self._client()
        client.send_bytes(b"8=FIX.4.2\x019=notanumber\x0135=A\x0110=000\x01")
        pump(self.reactor)

        self.assertEqual([], client.receive())
        self.assertEqual(0, len(self.applications[CLIENT].logons))

    def test_a_message_split_across_packets_is_routed_once_complete(self):
        client = self._client()
        logon = Message.create(C.LOGON)
        logon.set(C.MSG_SEQ_NUM, 1)
        logon.set(C.ENCRYPT_METHOD, "0")
        logon.set(C.HEART_BT_INT, 30)
        logon.set(C.SENDER_COMP_ID, CLIENT)
        logon.set(C.TARGET_COMP_ID, SERVER)
        logon.set(C.SENDING_TIME, self.clock.timestamp())
        raw = encode(logon)

        client.send_bytes(raw[:12])
        pump(self.reactor)
        self.assertEqual(0, len(self.applications[CLIENT].logons))

        client.send_bytes(raw[12:])
        pump(self.reactor)

        self.assertEqual(1, len(self.applications[CLIENT].logons))

    # -- timers ------------------------------------------------------------

    def test_the_tick_timer_drives_heartbeat_policing(self):
        client = self._client()
        client.logon(heartbeat=10)
        pump(self.reactor)
        client.receive()

        self.clock.advance(11.0)
        pump(self.reactor)

        received = client.receive()
        self.assertIn(C.HEARTBEAT, [m.msg_type for m in received])

    def test_stopping_the_acceptor_cancels_the_tick(self):
        self.acceptor.stop()
        self.clock.advance(3600.0)
        # Must not raise, and must not reschedule itself.
        pump(self.reactor)


class SessionManagerTest(unittest.TestCase):

    def setUp(self):
        self.clock = FixedClock()
        self.manager = SessionManager(self.clock, test_dictionary())
        self.addCleanup(self.manager.close)

    def test_sessions_resolve_by_reversed_comp_ids(self):
        self.manager.add(SessionConfig(sender_comp_id=SERVER,
                                       target_comp_id=CLIENT))
        message = Message().set(C.SENDER_COMP_ID, CLIENT) \
                           .set(C.TARGET_COMP_ID, SERVER)

        session = self.manager.resolve_inbound(message)

        self.assertIsNotNone(session)
        self.assertEqual(CLIENT, session.target_comp_id)

    def test_unknown_comp_ids_resolve_to_nothing(self):
        message = Message().set(C.SENDER_COMP_ID, "X").set(C.TARGET_COMP_ID, "Y")
        self.assertIsNone(self.manager.resolve_inbound(message))

    def test_describe_reports_every_session(self):
        self.manager.add(SessionConfig(sender_comp_id=SERVER,
                                       target_comp_id=CLIENT))
        self.manager.add(SessionConfig(sender_comp_id=SERVER,
                                       target_comp_id=OTHER_CLIENT))

        described = self.manager.describe()

        self.assertEqual(2, len(described))
        self.assertEqual({CLIENT, OTHER_CLIENT},
                         {row["target_comp_id"] for row in described})


class PersistentManagerTest(unittest.TestCase):
    """A store root makes sessions durable across manager lifetimes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-mgr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = FixedClock()

    def _manager(self):
        manager = SessionManager(self.clock, test_dictionary(), store_root=self.tmp)
        manager.add(SessionConfig(sender_comp_id=SERVER, target_comp_id=CLIENT))
        return manager

    def test_a_store_directory_is_created_per_session(self):
        manager = self._manager()
        self.addCleanup(manager.close)

        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "JNXSIM-CLIENT1")))

    def test_sequence_numbers_survive_a_new_manager(self):
        manager = self._manager()
        session = manager.get(SERVER, CLIENT)
        session.store.set_next_out(17)
        manager.close()

        restarted = self._manager()
        self.addCleanup(restarted.close)

        self.assertEqual(17, restarted.get(SERVER, CLIENT).store.next_out)


if __name__ == "__main__":
    unittest.main()
