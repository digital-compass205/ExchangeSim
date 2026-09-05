"""``exchangesim.tls.transport.TlsTransport`` and ``exchangesim.tls.context``,
exercised with no socket at all.

That is the point of building on ``ssl.MemoryBIO``: two transports can be
handed to each other's ``receive``/``drain`` directly, in a plain Python loop,
with no reactor, no thread and no real network -- which is also exactly how a
unit test is supposed to look under this project's "no sleeps, pump the state
machine synchronously" rule (see ``tests/support.py:pump`` for the reactor's
own version of the same idea).

Certificates come from Phase 1 (:mod:`exchangesim.tls.rsa`,
:mod:`exchangesim.tls.x509`), generated once per test class -- RSA-2048 keygen
is the slow part here, and no test needs a fresh key. 2048 bits, not the 1024
some of ``test_tls_x509.py``'s cheaper cases use, because OpenSSL 3.x's default
security level refuses a 1024-bit leaf during a real handshake.
"""

import atexit
import datetime
import os
import shutil
import ssl
import tempfile
import unittest

from exchangesim.tls import context, rsa, x509
from exchangesim.tls.transport import TlsTransport


def _write_pem(path, text):
    with open(path, "wb") as handle:
        handle.write(text.encode("ascii"))


def pump(a, b, max_rounds=200):
    """Move ``drain()`` output between two transports until neither side has
    anything left to move, feeding each side's whole output to the other's
    ``receive`` in one call.

    Handles a handshake, a reply, and any queued application data with the
    same loop -- there is nothing handshake-specific about "keep moving bytes
    until it stops producing more". Bounded by ``max_rounds`` so a transport
    that never settles fails the test loudly instead of hanging it.

    Returns ``(a_received, b_received)``: the plaintext each side produced,
    concatenated in the order ``receive`` returned it.
    """
    a_received = []
    b_received = []
    for _ in range(max_rounds):
        moved = False
        out_a = a.drain()
        if out_a:
            b_received.append(b.receive(out_a))
            moved = True
        out_b = b.drain()
        if out_b:
            a_received.append(a.receive(out_b))
            moved = True
        if not moved:
            break
    else:
        raise AssertionError("pump did not settle within %d rounds" % max_rounds)
    return b"".join(a_received), b"".join(b_received)


def pump_trickled(a, b, max_bytes=500000):
    """Like :func:`pump`, but feeds every byte of each side's output to the
    other's ``receive`` one at a time, proving the state machine never needs
    a whole TLS record delivered in one piece -- which is what a real
    reactor's ``recv()`` chunking cannot promise.

    Bounded by total bytes moved rather than round count: a byte-at-a-time
    handshake takes far more iterations than :func:`pump`'s default allows.
    """
    a_received = []
    b_received = []
    moved_total = 0
    while True:
        moved = False
        out_a = a.drain()
        for i in range(len(out_a)):
            b_received.append(b.receive(out_a[i:i + 1]))
            moved = True
        out_b = b.drain()
        for i in range(len(out_b)):
            a_received.append(a.receive(out_b[i:i + 1]))
            moved = True
        moved_total += len(out_a) + len(out_b)
        if not moved:
            break
        if moved_total > max_bytes:
            raise AssertionError(
                "pump_trickled exceeded %d bytes without settling" % max_bytes)
    return b"".join(a_received), b"".join(b_received)


class _Fixture(object):
    """The certificate material, generated on first use and then kept.

    A self-signed CA, a leaf for ``localhost`` signed by it, and a second,
    unrelated CA a client can be pointed at to prove verification is actually
    happening (:class:`WrongCaTest`) -- the same shape ``test_tls_x509.py``
    uses for its own handshake tests, built with the same two Phase 1 modules
    rather than a second, parallel certificate-building path.
    """

    @classmethod
    def build(cls):
        cls.tmp = tempfile.mkdtemp(prefix="exsim-tls-transport-")
        now = datetime.datetime(2026, 1, 1, 0, 0, 0)

        cls.ca_key = rsa.generate(2048)
        cls.leaf_key = rsa.generate(2048)
        cls.wrong_ca_key = rsa.generate(2048)

        cls.ca_cert = x509.self_signed_ca(
            cls.ca_key, "ExchangeSim Test CA", "ExchangeSim", days=3650,
            serial=1, now=now)
        ca_subject = x509.subject_name("ExchangeSim Test CA", "ExchangeSim")
        ca_key_id = x509.key_identifier(cls.ca_key)
        cls.leaf_cert = x509.sign_leaf(
            cls.ca_key, ca_subject, ca_key_id, cls.leaf_key, "localhost",
            "ExchangeSim", ["localhost", "127.0.0.1"], days=825, serial=2,
            now=now)
        cls.wrong_ca_cert = x509.self_signed_ca(
            cls.wrong_ca_key, "A Different CA", "ExchangeSim", days=3650,
            serial=1, now=now)

        cls.ca_pem = os.path.join(cls.tmp, "ca.pem")
        cls.leaf_pem = os.path.join(cls.tmp, "leaf.pem")
        cls.leaf_key_pem = os.path.join(cls.tmp, "leaf-key.pem")
        cls.wrong_ca_pem = os.path.join(cls.tmp, "wrong-ca.pem")

        _write_pem(cls.ca_pem, rsa.to_pem(cls.ca_cert, "CERTIFICATE"))
        _write_pem(cls.leaf_pem, rsa.to_pem(cls.leaf_cert, "CERTIFICATE"))
        _write_pem(cls.leaf_key_pem,
                   rsa.to_pem(cls.leaf_key.private_der(), "RSA PRIVATE KEY"))
        _write_pem(cls.wrong_ca_pem, rsa.to_pem(cls.wrong_ca_cert, "CERTIFICATE"))


_FIXTURE = {}


def _fixture():
    """The shared certificate material, built on first use.

    ``atexit`` rather than ``tearDownClass`` removes the directory, because ten
    classes share one copy and the first to finish must not delete it out from
    under the rest.
    """
    if not _FIXTURE:
        _Fixture.build()
        for name in ("tmp", "ca_key", "leaf_key", "wrong_ca_key", "ca_cert",
                     "leaf_cert", "wrong_ca_cert", "ca_pem", "leaf_pem",
                     "leaf_key_pem", "wrong_ca_pem"):
            _FIXTURE[name] = getattr(_Fixture, name)
        atexit.register(shutil.rmtree, _Fixture.tmp, True)
    return _FIXTURE


class TlsTransportTestCase(unittest.TestCase):
    """Base class handing every test here the shared certificate fixture."""

    @classmethod
    def setUpClass(cls):
        # Built once for the whole module, not once per subclass: there are ten
        # of those, and three RSA-2048 keys each would put thirty keygens --
        # about half a minute -- into a suite that otherwise runs in seconds.
        # The fixture is read-only, so sharing it across classes is safe.
        for name, value in _fixture().items():
            setattr(cls, name, value)

    # -- helpers -------------------------------------------------------

    def _raw_server_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.leaf_pem, self.leaf_key_pem)
        return ctx

    def _raw_client_context(self, cafile=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile or self.ca_pem)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def _make_pair(self, client_ctx=None, server_ctx=None, hostname="localhost"):
        client_ctx = client_ctx if client_ctx is not None else self._raw_client_context()
        server_ctx = server_ctx if server_ctx is not None else self._raw_server_context()
        client = TlsTransport(client_ctx, server_side=False, server_hostname=hostname)
        server = TlsTransport(server_ctx, server_side=True)
        return client, server


# ---------------------------------------------------------------------------
# 1. Handshake with no socket at all
# ---------------------------------------------------------------------------

class HandshakeTest(TlsTransportTestCase):

    def test_client_and_server_complete_a_handshake_with_no_socket(self):
        client, server = self._make_pair()
        self.assertFalse(client.handshaken)
        self.assertFalse(server.handshaken)

        pump(client, server)

        self.assertTrue(client.handshaken)
        self.assertTrue(server.handshaken)

    def test_description_names_a_protocol_version_and_a_cipher(self):
        client, server = self._make_pair()
        pump(client, server)

        for description in (client.description, server.description):
            self.assertIsInstance(description, str)
            self.assertIn("TLS", description)
            self.assertIn(" / ", description)


# ---------------------------------------------------------------------------
# 2. Application data both ways, including a multi-record payload
# ---------------------------------------------------------------------------

class ApplicationDataTest(TlsTransportTestCase):

    def test_data_flows_both_ways_after_the_handshake(self):
        client, server = self._make_pair()
        pump(client, server)

        client.transmit(b"hello from the client")
        _, to_server = pump(client, server)
        self.assertEqual(to_server, b"hello from the client")

        server.transmit(b"hello from the server")
        to_client, _ = pump(client, server)
        self.assertEqual(to_client, b"hello from the server")

    def test_a_payload_larger_than_one_tls_record_arrives_whole(self):
        # A TLS record's plaintext is capped at 16KB, so 100KB forces
        # SSLObject.write's short-write loop in transmit() and several
        # iterations of the read loop in receive() on the other end.
        client, server = self._make_pair()
        pump(client, server)

        payload = os.urandom(100 * 1024)
        client.transmit(payload)
        _, to_server = pump(client, server)
        self.assertEqual(to_server, payload)


# ---------------------------------------------------------------------------
# 3. transmit() before the handshake completes is queued, not lost
# ---------------------------------------------------------------------------

class QueuedBeforeHandshakeTest(TlsTransportTestCase):

    def test_transmit_before_handshake_completes_is_delivered(self):
        client, server = self._make_pair()
        # Written immediately after construction: the client has produced a
        # ClientHello (see TlsTransport.__init__) but nothing is handshaken
        # yet, so this must be held rather than raising or being discarded.
        client.transmit(b"queued before the handshake")

        _, to_server = pump(client, server)

        self.assertTrue(client.handshaken)
        self.assertTrue(server.handshaken)
        self.assertEqual(to_server, b"queued before the handshake")


# ---------------------------------------------------------------------------
# 4. The handshake survives being fed one byte at a time
# ---------------------------------------------------------------------------

class TrickleTest(TlsTransportTestCase):

    def test_handshake_completes_when_fed_one_byte_at_a_time(self):
        client, server = self._make_pair()

        pump_trickled(client, server)

        self.assertTrue(client.handshaken)
        self.assertTrue(server.handshaken)
        self.assertIn("TLS", client.description)

    def test_application_data_also_survives_a_byte_at_a_time_feed(self):
        client, server = self._make_pair()
        pump_trickled(client, server)

        client.transmit(b"trickled application data")
        _, to_server = pump_trickled(client, server)
        self.assertEqual(to_server, b"trickled application data")


# ---------------------------------------------------------------------------
# 5. receive(b"") mid-handshake is a clean failure, not a hang
# ---------------------------------------------------------------------------

class EofTest(TlsTransportTestCase):

    def test_immediate_eof_on_the_server_side_fails_cleanly(self):
        _, server = self._make_pair()
        # Nothing has been exchanged: the server is waiting for a
        # ClientHello it will now never see.
        with self.assertRaises(ssl.SSLError):
            server.receive(b"")

    def test_immediate_eof_on_the_client_side_fails_cleanly(self):
        client, _ = self._make_pair()
        # The client already produced a ClientHello (queued in its own
        # outgoing BIO by __init__) but has received nothing back.
        with self.assertRaises(ssl.SSLError):
            client.receive(b"")

    def test_eof_partway_through_the_handshake_fails_cleanly(self):
        client, server = self._make_pair()
        # Let the server see the ClientHello, then tell the client the
        # connection ended before any reply arrives.
        first_flight = client.drain()
        server.receive(first_flight)
        with self.assertRaises(ssl.SSLError):
            client.receive(b"")


# ---------------------------------------------------------------------------
# 6. close_notify
# ---------------------------------------------------------------------------

class CloseNotifyTest(TlsTransportTestCase):

    def test_close_notify_produces_bytes_the_peer_reads_as_a_clean_close(self):
        client, server = self._make_pair()
        pump(client, server)

        notify = client.close_notify()
        self.assertIsInstance(notify, bytes)
        self.assertTrue(notify)

        # The peer's receive() sees end-of-stream: no exception, no
        # plaintext, and it does not hang waiting for more.
        result = server.receive(notify)
        self.assertEqual(result, b"")

    def test_close_notify_never_raises_even_before_a_reply(self):
        client, server = self._make_pair()
        pump(client, server)

        # SSLObject.unwrap() raises SSLWantReadError here because the peer
        # has not answered yet -- close_notify() must swallow that itself.
        notify = client.close_notify()
        self.assertIsInstance(notify, bytes)


# ---------------------------------------------------------------------------
# 7. A client verifying against the wrong CA fails, loudly
# ---------------------------------------------------------------------------

class WrongCaTest(TlsTransportTestCase):

    def test_a_client_trusting_the_wrong_ca_fails_the_handshake(self):
        client_ctx = self._raw_client_context(cafile=self.wrong_ca_pem)
        client, server = self._make_pair(client_ctx=client_ctx)

        with self.assertRaises(ssl.SSLError):
            pump(client, server)

        # Proof the failure is real rather than a false positive: neither
        # side ever got to call itself handshaken.
        self.assertFalse(client.handshaken)
        self.assertFalse(server.handshaken)


# ---------------------------------------------------------------------------
# 8. context.py: policies, unknown values, and a round trip through pump()
# ---------------------------------------------------------------------------

class ContextPolicyTest(TlsTransportTestCase):

    def test_none_returns_no_context_from_either_side(self):
        self.assertIsNone(
            context.server_context("none", self.leaf_pem, self.leaf_key_pem))
        self.assertIsNone(context.client_context("none", self.ca_pem))

    def test_every_available_policy_builds_a_real_context(self):
        for policy in context.POLICIES:
            if policy == "none":
                continue
            with self.subTest(policy=policy):
                if not context.available(policy):
                    with self.assertRaises(context.TlsUnavailable):
                        context.server_context(policy, self.leaf_pem, self.leaf_key_pem)
                    continue
                server_ctx = context.server_context(
                    policy, self.leaf_pem, self.leaf_key_pem)
                client_ctx = context.client_context(policy, self.ca_pem)
                self.assertIsInstance(server_ctx, ssl.SSLContext)
                self.assertIsInstance(client_ctx, ssl.SSLContext)

    def test_an_unknown_policy_raises_value_error_naming_the_choices(self):
        with self.assertRaises(ValueError) as caught:
            context.server_context("1.1", self.leaf_pem, self.leaf_key_pem)
        message = str(caught.exception)
        for policy in context.POLICIES:
            self.assertIn(policy, message)

        with self.assertRaises(ValueError):
            context.client_context("bogus", self.ca_pem)
        with self.assertRaises(ValueError):
            context.available("bogus")

    def test_available_policies_round_trip_through_a_real_handshake(self):
        for policy in context.POLICIES:
            if policy == "none" or not context.available(policy):
                continue
            with self.subTest(policy=policy):
                server_ctx = context.server_context(
                    policy, self.leaf_pem, self.leaf_key_pem)
                client_ctx = context.client_context(policy, self.ca_pem)
                client = TlsTransport(client_ctx, server_side=False,
                                      server_hostname="localhost")
                server = TlsTransport(server_ctx, server_side=True)
                pump(client, server)
                self.assertTrue(client.handshaken)
                self.assertTrue(server.handshaken)


# ---------------------------------------------------------------------------
# 9. Version pinning is actually applied
# ---------------------------------------------------------------------------

class VersionPinningTest(TlsTransportTestCase):

    def test_tls13_negotiates_when_the_interpreter_supports_it(self):
        if not ssl.HAS_TLSv1_3:
            self.skipTest(
                "this interpreter's OpenSSL (%s) has no TLS 1.3 support"
                % ssl.OPENSSL_VERSION)

        server_ctx = context.server_context("1.3", self.leaf_pem, self.leaf_key_pem)
        client_ctx = context.client_context("1.3", self.ca_pem)
        client = TlsTransport(client_ctx, server_side=False, server_hostname="localhost")
        server = TlsTransport(server_ctx, server_side=True)

        pump(client, server)

        self.assertTrue(client.handshaken)
        self.assertTrue(server.handshaken)
        self.assertIn("TLSv1.3", client.description)
        self.assertIn("TLSv1.3", server.description)

    def test_tls13_is_reported_unavailable_with_a_clear_message(self):
        if ssl.HAS_TLSv1_3:
            self.skipTest(
                "this interpreter's OpenSSL (%s) supports TLS 1.3, so there "
                "is nothing to prove unavailable here" % ssl.OPENSSL_VERSION)

        self.assertFalse(context.available("1.3"))

        with self.assertRaises(context.TlsUnavailable) as caught:
            context.server_context(
                "1.3", self.leaf_pem, self.leaf_key_pem,
                setting_name="gateway_router.tls")
        message = str(caught.exception)
        self.assertIn("1.0.2", message)
        self.assertIn("gateway_router.tls", message)
        self.assertIn("1.2", message)
        self.assertIn("none", message)

        # client_context must refuse the same way.
        with self.assertRaises(context.TlsUnavailable):
            context.client_context("1.3", self.ca_pem)


# ---------------------------------------------------------------------------
# 10. TLS 1.2 handshakes on both interpreters
# ---------------------------------------------------------------------------

class Tls12Test(TlsTransportTestCase):

    def test_tls12_handshake_succeeds_on_this_interpreter(self):
        server_ctx = context.server_context("1.2", self.leaf_pem, self.leaf_key_pem)
        client_ctx = context.client_context("1.2", self.ca_pem)
        client = TlsTransport(client_ctx, server_side=False, server_hostname="localhost")
        server = TlsTransport(server_ctx, server_side=True)

        pump(client, server)

        self.assertTrue(client.handshaken)
        self.assertTrue(server.handshaken)
        # "1.2" pins a *floor*, not a ceiling (see context._pin_version's
        # docstring) -- deliberately, so a security-conscious minimum does
        # not silently become a ceiling nobody asked for. Where the
        # interpreter's OpenSSL only goes as high as TLS 1.2 at all (the
        # 3.6.8 / OpenSSL 1.0.2 target), that is exactly what is
        # negotiated. Where it also offers TLS 1.3 (OpenSSL 3.x), an
        # unset ceiling lets the handshake climb there instead -- which is
        # the documented behaviour of the floor-only policy, not a bug in
        # it. Either outcome proves the floor did its job: nothing here
        # ever drops below TLS 1.2.
        self.assertTrue(
            "TLSv1.2" in client.description or "TLSv1.3" in client.description,
            client.description)
        self.assertNotIn("TLSv1.1", client.description)
        self.assertNotIn("TLSv1 ", client.description)
        if not ssl.HAS_TLSv1_3:
            self.assertIn("TLSv1.2", client.description)
            self.assertIn("TLSv1.2", server.description)


if __name__ == "__main__":
    unittest.main()
