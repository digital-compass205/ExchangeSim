"""The NNF session layer: a box, the users on it, and the heartbeat.

The two things worth pinning here are the ones no other venue has. A connection
is a *box* and a session is a *user*, several of which share one box; and the
opening sequence has three steps rather than one, with encryption switched on
in the middle of it.
"""

import unittest

from exchangesim.nnf import packet
from exchangesim.nnf.session import BoxState, MISSED_HEARTBEATS_ALLOWED
from exchangesim.venues.nse import dictionary as D
from exchangesim.venues.nse import transactions as X

from tests.nnfsupport import (
    BOX_ID,
    BROKER_ID,
    BoxHarness,
    OTHER_USER_ID,
    PASSWORD,
    USER_ID,
    existing_ciphers,
    new_ciphers,
)


class OpeningSequenceTest(unittest.TestCase):
    """Register the box, sign the box on, then sign users on over it."""

    def setUp(self):
        self.harness = BoxHarness(users=(USER_ID, OTHER_USER_ID))

    def test_a_fresh_connection_awaits_registration(self):
        self.assertEqual(self.harness.box.state, BoxState.AWAITING_REGISTRATION)
        self.assertTrue(self.harness.box.connected)

    def test_registration_is_answered_and_moves_the_box_on(self):
        self.harness.register()
        self.assertEqual(self.harness.last().msg_type,
                         str(X.SECURE_BOX_REGISTRATION_REQUEST_OUT))
        self.assertEqual(self.harness.box.state, BoxState.AWAITING_BOX_SIGN_ON)

    def test_box_sign_on_makes_the_box_active(self):
        self.harness.register().box_sign_on()
        reply = self.harness.last()
        self.assertEqual(reply.msg_type, str(X.BOX_SIGN_ON_REQUEST_OUT))
        self.assertEqual(reply.get(D.BOX_ID), str(BOX_ID))
        self.assertEqual(self.harness.box.state, BoxState.ACTIVE)
        self.assertEqual(self.harness.box.broker_id, BROKER_ID)

    def test_a_user_signs_on_over_the_box(self):
        self.harness.open()
        reply = self.harness.last()
        self.assertEqual(reply.msg_type, str(X.SIGN_ON_REQUEST_OUT))
        self.assertEqual(reply.get(D.ERROR_CODE), "0")
        self.assertEqual(reply.get(D.SIGNON_USER_ID), str(USER_ID))

        session = self.harness.session()
        self.assertTrue(session.logged_on)
        self.assertEqual(session.key, ("nnf", USER_ID))
        self.assertEqual(session.target_comp_id, str(USER_ID))
        self.assertEqual(self.harness.gateway.logons, [str(USER_ID)])

    def test_many_users_share_one_box(self):
        self.harness.register().box_sign_on()
        self.harness.sign_on(USER_ID).sign_on(OTHER_USER_ID)
        self.assertEqual(sorted(self.harness.box.users),
                         [USER_ID, OTHER_USER_ID])
        self.assertTrue(self.harness.session(USER_ID).logged_on)
        self.assertTrue(self.harness.session(OTHER_USER_ID).logged_on)

    def test_an_unknown_user_is_answered_with_an_error_code(self):
        self.harness.register().box_sign_on()
        self.harness.clear()
        self.harness.sign_on(99999)
        reply = self.harness.last()
        self.assertEqual(reply.msg_type, str(X.SIGN_ON_REQUEST_OUT))
        self.assertEqual(reply.get(D.ERROR_CODE), "16042")
        self.assertEqual(self.harness.box.users, {})

    def test_the_session_key_can_never_collide_with_an_injected_order(self):
        # order.new tags an injected order "control:<OWNER>", a string; a user
        # session key is a tuple. The two populations cannot meet.
        self.harness.open()
        key = self.harness.session().key
        self.assertIsInstance(key, tuple)
        self.assertNotEqual(key, "control:WEB")


class SignOffTest(unittest.TestCase):

    def setUp(self):
        self.harness = BoxHarness(users=(USER_ID, OTHER_USER_ID))
        self.harness.register().box_sign_on()
        self.harness.sign_on(USER_ID).sign_on(OTHER_USER_ID)
        self.harness.clear()

    def test_a_user_signing_off_leaves_the_box_and_its_others_up(self):
        self.harness.send(X.SIGN_OFF_REQUEST_IN, user_id=USER_ID)
        self.assertEqual(self.harness.last().msg_type,
                         str(X.SIGN_OFF_REQUEST_OUT))
        self.assertTrue(self.harness.box.connected)
        self.assertIsNone(self.harness.session(USER_ID))
        self.assertTrue(self.harness.session(OTHER_USER_ID).logged_on)

    def test_disconnecting_the_box_signs_off_every_user_on_it(self):
        # "The exchange will also logoff the box id of the member, which means
        # that all the users linked to that box id will be disconnected."
        self.harness.box.disconnect("operator")
        self.assertFalse(self.harness.box.connected)
        self.assertEqual(self.harness.box.users, {})
        self.assertEqual(sorted(name for name, _ in self.harness.gateway.logouts),
                         [str(USER_ID), str(OTHER_USER_ID)])

    def test_a_disconnected_box_forgets_its_cipher(self):
        self.harness.box.disconnect("operator")
        self.assertEqual(self.harness.box.codec.cipher.name, "plain")

    def test_session_reset_is_refused_rather_than_faked(self):
        # There are no sequence numbers to reset. Reporting success for
        # something that did not happen is the failure this avoids.
        session = self.harness.session(OTHER_USER_ID)
        with self.assertRaises(NotImplementedError) as caught:
            session.reset()
        self.assertIn("download", str(caught.exception))

    def test_a_report_for_a_disconnected_user_is_dropped_not_queued(self):
        # NNF has no resend, so there is nothing to queue for. The client asks
        # for a download instead, which is why this is right rather than lossy.
        session = self.harness.session(USER_ID)
        self.harness.box.detach("connection closed")
        from exchangesim.fix.message import Message
        self.assertFalse(session.send(Message.create(str(X.ORDER_CONFIRMATION))))


class HeartbeatTest(unittest.TestCase):

    def setUp(self):
        self.harness = BoxHarness()
        self.harness.open()
        self.harness.clear()

    def interval(self):
        return self.harness.manager.heartbeat_seconds

    def test_a_heartbeat_is_echoed_back(self):
        self.harness.send(X.HEARTBEAT)
        self.assertEqual(self.harness.last().msg_type, str(X.HEARTBEAT))

    def test_a_second_heartbeat_inside_one_interval_is_ignored(self):
        self.harness.send(X.HEARTBEAT)
        self.harness.clear()
        self.harness.send(X.HEARTBEAT)
        self.assertEqual(self.harness.received(), [])
        self.assertEqual(self.harness.box.drop_counter, 1)

    def test_a_heartbeat_after_the_interval_is_echoed_again(self):
        self.harness.send(X.HEARTBEAT)
        self.harness.clear()
        self.harness.clock.advance(self.interval() + 1)
        self.harness.send(X.HEARTBEAT)
        self.assertEqual(self.harness.last().msg_type, str(X.HEARTBEAT))
        self.assertEqual(self.harness.box.drop_counter, 0)

    def test_the_drop_counter_eventually_disconnects_the_box(self):
        self.harness.send(X.HEARTBEAT)
        for _ in range(self.harness.manager.drop_counter_limit):
            self.harness.send(X.HEARTBEAT)
        self.assertFalse(self.harness.box.connected)

    def test_two_missed_heartbeats_disconnect_the_box(self):
        self.harness.clock.advance(
            self.interval() * MISSED_HEARTBEATS_ALLOWED - 1)
        self.harness.box.tick()
        self.assertTrue(self.harness.box.connected)

        self.harness.clock.advance(2)
        self.harness.box.tick()
        self.assertFalse(self.harness.box.connected)

    def test_traffic_keeps_the_box_alive(self):
        self.harness.clock.advance(self.interval() * 3)
        self.harness.send(X.HEARTBEAT)
        self.harness.box.tick()
        self.assertTrue(self.harness.box.connected)


class EncryptionTest(unittest.TestCase):
    """The box opens in clear and is encrypted from the second message on."""

    def open_encrypted(self, ciphers):
        member, exchange = ciphers
        harness = BoxHarness(cipher_for=lambda box: exchange)
        harness.register()
        # Registration was the one message in clear; the client switches too.
        harness.client.cipher = member
        harness.box_sign_on().sign_on()
        return harness

    def test_the_existing_methodology_carries_the_whole_session(self):
        harness = self.open_encrypted(existing_ciphers())
        self.assertTrue(harness.session().logged_on)
        self.assertEqual(harness.box.codec.cipher.name, "existing")

    def test_the_new_methodology_carries_the_whole_session(self):
        harness = self.open_encrypted(new_ciphers())
        self.assertTrue(harness.session().logged_on)
        self.assertEqual(harness.box.codec.cipher.name, "new")

    def test_the_password_does_not_travel_in_clear(self):
        harness = self.open_encrypted(new_ciphers())
        self.assertNotIn(PASSWORD.encode("latin-1"), harness.transport.buffer)

    def test_a_tampered_packet_disconnects_the_box(self):
        member, exchange = new_ciphers()
        harness = BoxHarness(cipher_for=lambda box: exchange)
        harness.register()
        harness.client.cipher = member

        raw = bytearray(harness.client.encode(
            _message(X.BOX_SIGN_ON_REQUEST_IN, {D.BOX_ID: BOX_ID})))
        raw[packet.PREFIX_BYTES] ^= 0xFF
        harness.box.on_data(bytes(raw))
        self.assertFalse(harness.box.connected)

    def test_a_box_that_never_registers_stays_in_clear(self):
        harness = BoxHarness()
        self.assertEqual(harness.box.codec.cipher.name, "plain")
        self.assertFalse(harness.box.encrypted)


class ManagerTest(unittest.TestCase):

    def setUp(self):
        self.harness = BoxHarness(users=(USER_ID, OTHER_USER_ID))

    def test_sessions_lists_configured_users_before_anybody_connects(self):
        # The same as the FIX manager, whose sessions exist before a client
        # dials in -- so `sessions` and `session.kill` can name one.
        described = self.harness.manager.describe()
        self.assertEqual([entry["target_comp_id"] for entry in described],
                         [str(USER_ID), str(OTHER_USER_ID)])
        self.assertEqual([entry["state"] for entry in described],
                         ["signed_off", "signed_off"])

    def test_a_signed_on_user_reports_active(self):
        self.harness.open()
        entry = self.harness.manager.describe()[0]
        self.assertEqual(entry["state"], "active")
        self.assertTrue(entry["connected"])
        self.assertEqual(entry["box_id"], BOX_ID)

    def test_a_connection_resolves_to_its_box_by_box_id(self):
        message = _message(X.SECURE_BOX_REGISTRATION_REQUEST_IN,
                           {D.BOX_ID: BOX_ID})
        self.assertIs(self.harness.manager.resolve_inbound(message),
                      self.harness.box)

    def test_an_unknown_box_resolves_to_nothing(self):
        message = _message(X.SECURE_BOX_REGISTRATION_REQUEST_IN,
                           {D.BOX_ID: 99})
        self.assertIsNone(self.harness.manager.resolve_inbound(message))

    def test_a_refusal_closes_without_answering(self):
        # NNF has no Logout and no message that fits every refusal, so the
        # connection is simply closed and the audit carries the reason.
        message = _message(X.SECURE_BOX_REGISTRATION_REQUEST_IN, {D.BOX_ID: 99})
        refusal, raw = self.harness.manager.refusal(
            message, "unknown box", self.harness.client)
        self.assertIsNone(refusal)
        self.assertEqual(raw, b"")

    def test_an_order_resolves_back_to_the_user_that_sent_it(self):
        self.harness.open()
        session = self.harness.session()
        self.assertIs(self.harness.manager.session_for(session.key), session)

    def test_a_user_must_name_a_configured_box(self):
        from exchangesim.nnf.session import NnfSessionConfig
        self.assertRaises(ValueError, self.harness.manager.add_user,
                          NnfSessionConfig(1, box_id=99, broker_id="X"))


def _message(code, fields):
    from exchangesim.fix.message import Message
    message = Message.create(str(code))
    message.set(D.ERROR_CODE, "0")
    for tag, value in sorted(fields.items()):
        message.set(tag, str(value))
    return message


if __name__ == "__main__":
    unittest.main()
