"""The reactor's TLS seam (:mod:`exchangesim.core.reactor`'s ``transport=``
argument on ``listen``/``connect``), exercised over real loopback sockets and
pumped by hand -- the same discipline every other reactor test in this suite
uses (see ``tests/support.py``), extended here to cover a listener and a
connection that both happen to speak TLS.

``exchangesim.core.reactor`` is not modified by this module, or anywhere in
this phase -- it was already done and already knows nothing about TLS beyond
the duck-typed ``transport`` seam :class:`exchangesim.tls.transport.TlsTransport`
fills.

Certificate material is generated once, at module scope, exactly as
``tests/test_tls_transport.py`` does for its own (deliberately not shared
across files, per the same reasoning that fixture gives for not sharing
across its own ten classes -- and because a wrong-CA scenario is exercised
here too and needs its own unrelated CA regardless).

Policy ``"1.2"`` is used everywhere except :class:`Tls13Test`, so the bulk of
this module runs identically on both interpreters this project targets --
venv36's OpenSSL 1.0.2q has no TLS 1.3 at all.
"""

import atexit
import datetime
import logging
import os
import shutil
import socket
import ssl
import tempfile
import unittest

from exchangesim.core.clock import FixedClock
from exchangesim.core.reactor import Reactor
from exchangesim.tls import certs, context, rsa, x509
from exchangesim.tls.transport import TlsTransport

from tests.support import connect, pump, pump_until


def _write_pem(path, text):
    with open(path, "wb") as handle:
        handle.write(text.encode("ascii"))


class _Fixture(object):
    """A CA, a ``localhost``/``127.0.0.1`` leaf it signs, and a second,
    unrelated CA for the wrong-CA scenario -- generated once for this whole
    module, the slow part (three RSA-2048 keys) paid for exactly once."""

    @classmethod
    def build(cls):
        cls.tmp = tempfile.mkdtemp(prefix="exsim-reactor-tls-")
        now = datetime.datetime(2026, 1, 1, 0, 0, 0)

        cls.ca_key = rsa.generate(2048)
        cls.leaf_key = rsa.generate(2048)
        cls.wrong_ca_key = rsa.generate(2048)

        cls.ca_cert = x509.self_signed_ca(
            cls.ca_key, "ExchangeSim Reactor Test CA", "ExchangeSim",
            days=3650, serial=1, now=now)
        ca_subject = x509.subject_name("ExchangeSim Reactor Test CA", "ExchangeSim")
        ca_key_id = x509.key_identifier(cls.ca_key)
        cls.leaf_cert = x509.sign_leaf(
            cls.ca_key, ca_subject, ca_key_id, cls.leaf_key, "localhost",
            "ExchangeSim", ["localhost", "127.0.0.1"], days=825, serial=2,
            now=now)
        cls.wrong_ca_cert = x509.self_signed_ca(
            cls.wrong_ca_key, "A Different Reactor CA", "ExchangeSim",
            days=3650, serial=1, now=now)

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
    if not _FIXTURE:
        _Fixture.build()
        for name in ("tmp", "ca_key", "leaf_key", "wrong_ca_key", "ca_cert",
                     "leaf_cert", "wrong_ca_cert", "ca_pem", "leaf_pem",
                     "leaf_key_pem", "wrong_ca_pem"):
            _FIXTURE[name] = getattr(_Fixture, name)
        atexit.register(shutil.rmtree, _Fixture.tmp, True)
    return _FIXTURE


class _CollectingHandler(logging.Handler):
    """Captures log records instead of printing them, so a test can assert
    that the reactor logged *nothing* -- the evidence that a shutdown was
    clean rather than one the reactor had to complain about."""

    def __init__(self, level=logging.WARNING):
        logging.Handler.__init__(self, level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class ReactorTlsTestCase(unittest.TestCase):
    """Base class handing every test here the shared certificate fixture and
    a fresh reactor."""

    @classmethod
    def setUpClass(cls):
        for name, value in _fixture().items():
            setattr(cls, name, value)

    def setUp(self):
        self.clock = FixedClock()
        self.reactor = Reactor(self.clock)
        self.addCleanup(self.reactor.close)

    # -- helpers ---------------------------------------------------------

    def _server_context(self, policy="1.2"):
        return context.server_context(policy, self.leaf_pem, self.leaf_key_pem)

    def _client_context(self, policy="1.2", cafile=None, check_hostname=True):
        return context.client_context(
            policy, cafile or self.ca_pem, check_hostname=check_hostname)

    def _listen(self, on_accept, server_ctx=None):
        server_ctx = server_ctx if server_ctx is not None else self._server_context()
        return self.reactor.listen(
            "127.0.0.1", 0, on_accept,
            transport=certs.transport_factory(server_ctx))

    def _connect(self, address, on_connect, on_error=None, client_ctx=None,
                 hostname="localhost"):
        client_ctx = client_ctx if client_ctx is not None else self._client_context()
        transport = TlsTransport(client_ctx, server_side=False,
                                  server_hostname=hostname)
        return self.reactor.connect(address[0], address[1], on_connect,
                                    on_error=on_error, transport=transport)

    def _capture_reactor_warnings(self):
        handler = _CollectingHandler()
        logger = logging.getLogger("exchangesim.core.reactor")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        return handler


# ---------------------------------------------------------------------------
# 1. A handshake completes over real sockets, on_handshake fires exactly once
# ---------------------------------------------------------------------------

class HandshakeTest(ReactorTlsTestCase):

    def test_handshake_completes_and_on_handshake_fires_once_each_side(self):
        server_handshakes = []

        def on_accept(conn):
            conn.on_handshake = server_handshakes.append

        listener = self._listen(on_accept)

        client_handshakes = []

        def on_connect(conn):
            conn.on_handshake = client_handshakes.append

        client_conn = self._connect(listener.address, on_connect)

        self.assertTrue(pump_until(
            self.reactor,
            lambda: server_handshakes and client_handshakes))

        # A few more idle rounds must not fire it again.
        pump(self.reactor, rounds=5)

        self.assertEqual(1, len(server_handshakes))
        self.assertEqual(1, len(client_handshakes))
        self.assertIs(client_conn, client_handshakes[0])

        for conn in (client_conn, server_handshakes[0]):
            self.assertIsInstance(conn.transport.description, str)
            self.assertIn("TLS", conn.transport.description)


# ---------------------------------------------------------------------------
# 2. on_data never sees a handshake byte
# ---------------------------------------------------------------------------

class NoHandshakeBytesInOnDataTest(ReactorTlsTestCase):

    def test_on_data_receives_only_the_application_payload(self):
        server_data = []

        def on_accept(conn):
            conn.on_data = lambda c, chunk: server_data.append(chunk)

        listener = self._listen(on_accept)

        def on_connect(conn):
            conn.send(b"only-app-data")

        self._connect(listener.address, on_connect)

        self.assertTrue(pump_until(self.reactor, lambda: server_data))
        pump(self.reactor, rounds=5)

        self.assertEqual([b"only-app-data"], server_data)


# ---------------------------------------------------------------------------
# 3. Application data both ways, including a payload spanning several
#    TLS records and reactor steps
# ---------------------------------------------------------------------------

class ApplicationDataTest(ReactorTlsTestCase):

    def test_data_flows_both_ways(self):
        server_data = []
        replied = []

        def on_accept(conn):
            def on_data(c, chunk):
                server_data.append(chunk)
                if not replied:
                    replied.append(True)
                    c.send(b"hello from server")
            conn.on_data = on_data

        listener = self._listen(on_accept)

        client_data = []
        connected = []

        def on_connect(conn):
            connected.append(conn)
            conn.on_data = lambda c, chunk: client_data.append(chunk)

        self._connect(listener.address, on_connect)
        self.assertTrue(pump_until(self.reactor, lambda: connected))

        connected[0].send(b"hello from client")

        self.assertTrue(pump_until(self.reactor, lambda: client_data))
        self.assertEqual(b"hello from client", b"".join(server_data))
        self.assertEqual(b"hello from server", b"".join(client_data))

    def test_a_payload_spanning_several_tls_records_arrives_whole_and_in_order(self):
        server_data = []

        def on_accept(conn):
            conn.on_data = lambda c, chunk: server_data.append(chunk)

        listener = self._listen(on_accept)

        connected = []
        self._connect(listener.address, connected.append)
        self.assertTrue(pump_until(self.reactor, lambda: connected))

        # A TLS record's plaintext is capped at 16KB, so 100KB forces several
        # records and, over loopback, likely several reactor steps too.
        payload = os.urandom(100 * 1024)
        connected[0].send(payload)

        self.assertTrue(pump_until(
            self.reactor,
            lambda: len(b"".join(server_data)) >= len(payload),
            timeout=10.0))
        # Whole and in order: a plain concatenation of whatever on_data saw,
        # in the order it saw it, must equal the payload exactly.
        self.assertEqual(payload, b"".join(server_data))


# ---------------------------------------------------------------------------
# 4. Data sent before the handshake completes is queued and delivered
# ---------------------------------------------------------------------------

class QueuedBeforeHandshakeTest(ReactorTlsTestCase):

    def test_data_sent_before_handshake_completes_is_delivered(self):
        server_data = []

        def on_accept(conn):
            conn.on_data = lambda c, chunk: server_data.append(chunk)

        listener = self._listen(on_accept)

        conn = self._connect(listener.address, lambda c: None)
        # Nothing has been pumped yet: no TCP handshake, let alone a TLS one,
        # has happened. This reaches TlsTransport.transmit's queueing path.
        conn.send(b"queued-before-handshake")

        self.assertTrue(pump_until(self.reactor, lambda: server_data))
        self.assertEqual([b"queued-before-handshake"], server_data)


# ---------------------------------------------------------------------------
# 5. close_when_flushed delivers buffered data and a clean close_notify
# ---------------------------------------------------------------------------

class CloseWhenFlushedTest(ReactorTlsTestCase):

    def test_buffered_data_and_a_clean_close_notify_are_delivered(self):
        handler = self._capture_reactor_warnings()

        def on_accept(conn):
            # Wait for the handshake: sending -- and unwrap()-ing for the
            # close_notify -- before it completes has nothing to write into
            # yet, which is a different (and already-covered) scenario from
            # the one this test is about.
            def on_handshake(c):
                c.send(b"final message")
                c.close_when_flushed()
            conn.on_handshake = on_handshake

        listener = self._listen(on_accept)

        client_data = []
        client_closed = []

        def on_connect(conn):
            conn.on_data = lambda c, chunk: client_data.append(chunk)
            conn.on_close = client_closed.append

        self._connect(listener.address, on_connect)

        self.assertTrue(pump_until(self.reactor, lambda: client_closed))

        self.assertEqual(b"final message", b"".join(client_data))
        self.assertEqual(0, len(self.reactor.connections))
        # A real truncation (EOF without close_notify) is a logged warning
        # from the reactor's transport-failure path; a clean close_notify is
        # not.
        self.assertEqual([], handler.records,
                          "unexpected warning(s): %r" % handler.records)


# ---------------------------------------------------------------------------
# 6. A client that trusts the wrong CA fails cleanly
# ---------------------------------------------------------------------------

class WrongCaTest(ReactorTlsTestCase):

    def test_wrong_ca_client_is_dropped_with_no_leaked_connections(self):
        server_closed = []

        def on_accept(conn):
            conn.on_close = server_closed.append

        listener = self._listen(on_accept)

        client_ctx = self._client_context(cafile=self.wrong_ca_pem)
        errors = []
        self._connect(listener.address, lambda c: None,
                      on_error=errors.append, client_ctx=client_ctx)

        self.assertTrue(pump_until(self.reactor, lambda: server_closed))
        # Let the client side settle too.
        pump(self.reactor, rounds=5)

        self.assertEqual(0, len(self.reactor.connections))


# ---------------------------------------------------------------------------
# 7. A peer that disconnects mid-handshake does not raise or leak
# ---------------------------------------------------------------------------

class MidHandshakeDisconnectTest(ReactorTlsTestCase):

    def test_disconnect_mid_handshake_is_survived(self):
        server_closed = []

        def on_accept(conn):
            conn.on_close = server_closed.append

        listener = self._listen(on_accept)

        # A real ClientHello, truncated, so the server is left genuinely
        # mid-handshake -- it has seen the start of a negotiation, not
        # nothing at all -- before the peer vanishes.
        client_ctx = self._client_context()
        probe = TlsTransport(client_ctx, server_side=False,
                              server_hostname="localhost")
        client_hello = probe.drain()
        self.assertTrue(client_hello)

        raw = socket.create_connection(listener.address)
        raw.sendall(client_hello[:16])
        pump(self.reactor)
        raw.close()

        self.assertTrue(pump_until(self.reactor, lambda: server_closed))
        self.assertEqual(0, len(self.reactor.connections))


# ---------------------------------------------------------------------------
# 8. Garbage instead of a ClientHello is dropped cleanly
# ---------------------------------------------------------------------------

class GarbageInputTest(ReactorTlsTestCase):

    def test_garbage_instead_of_a_client_hello_is_dropped_cleanly(self):
        server_closed = []

        def on_accept(conn):
            conn.on_close = server_closed.append

        listener = self._listen(on_accept)

        raw = socket.create_connection(listener.address)
        self.addCleanup(raw.close)
        raw.sendall(b"not a tls record, just garbage bytes" * 4)

        # The point of the test: step() must not raise even though the
        # bytes just sent are nonsense to the SSL object.
        self.assertTrue(pump_until(self.reactor, lambda: server_closed))
        self.assertEqual(0, len(self.reactor.connections))


# ---------------------------------------------------------------------------
# 9. Plain and TLS listeners coexist on one reactor
# ---------------------------------------------------------------------------

class MixedListenersTest(ReactorTlsTestCase):

    def test_plain_and_tls_listeners_both_work_on_one_reactor(self):
        plain_received = []

        def on_plain_accept(conn):
            conn.on_data = lambda c, chunk: plain_received.append(chunk)

        plain_listener = self.reactor.listen("127.0.0.1", 0, on_plain_accept)

        tls_received = []

        def on_tls_accept(conn):
            conn.on_data = lambda c, chunk: tls_received.append(chunk)

        tls_listener = self._listen(on_tls_accept)

        plain_client = connect(plain_listener.address)
        self.addCleanup(plain_client.close)
        plain_client.sendall(b"plain-data")

        tls_connected = []
        self._connect(tls_listener.address, tls_connected.append)
        self.assertTrue(pump_until(self.reactor, lambda: tls_connected))
        tls_connected[0].send(b"tls-data")

        self.assertTrue(pump_until(
            self.reactor, lambda: plain_received and tls_received))

        self.assertEqual(b"plain-data", b"".join(plain_received))
        self.assertEqual(b"tls-data", b"".join(tls_received))


# ---------------------------------------------------------------------------
# TLS 1.3, where the interpreter has it
# ---------------------------------------------------------------------------

@unittest.skipUnless(ssl.HAS_TLSv1_3,
                     "interpreter's OpenSSL has no TLS 1.3 support")
class Tls13Test(ReactorTlsTestCase):

    def test_handshake_completes_under_tls_1_3(self):
        server_ctx = self._server_context(policy="1.3")

        def on_accept(conn):
            conn.on_data = lambda c, chunk: None

        listener = self._listen(on_accept, server_ctx=server_ctx)

        client_ctx = self._client_context(policy="1.3")
        connected = []
        self._connect(listener.address, connected.append, client_ctx=client_ctx)

        self.assertTrue(pump_until(
            self.reactor,
            lambda: connected and connected[0].transport.handshaken))

        self.assertIn("TLSv1.3", connected[0].transport.description)


if __name__ == "__main__":
    unittest.main()
