"""Reactor behaviour: accept, read, buffered write, timers, close semantics."""

import errno
import socket
import unittest

from exchangesim.core.clock import FixedClock
from exchangesim.core.reactor import Reactor

from .support import connect, pump, pump_until


class ReactorTest(unittest.TestCase):

    def setUp(self):
        self.clock = FixedClock()
        self.reactor = Reactor(self.clock)
        self.addCleanup(self.reactor.close)

    def _echo_server(self):
        received = []

        def on_accept(conn):
            def on_data(c, chunk):
                received.append(chunk)
                c.send(chunk.upper())
            conn.on_data = on_data

        listener = self.reactor.listen("127.0.0.1", 0, on_accept)
        return listener.address, received

    def test_accept_read_and_write_round_trip(self):
        address, received = self._echo_server()
        client = connect(address)
        self.addCleanup(client.close)

        client.sendall(b"hello")
        pump(self.reactor)

        self.assertEqual([b"hello"], received)
        self.assertEqual(b"HELLO", client.recv(1024))

    def test_multiple_clients_are_served_independently(self):
        address, _ = self._echo_server()
        first = connect(address)
        second = connect(address)
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        first.sendall(b"one")
        second.sendall(b"two")
        pump(self.reactor)

        self.assertEqual(b"ONE", first.recv(1024))
        self.assertEqual(b"TWO", second.recv(1024))
        self.assertEqual(2, len(self.reactor.connections))

    def test_peer_disconnect_fires_on_close_and_drops_the_connection(self):
        closed = []

        def on_accept(conn):
            conn.on_close = closed.append

        listener = self.reactor.listen("127.0.0.1", 0, on_accept)
        client = connect(listener.address)
        pump(self.reactor)
        self.assertEqual(1, len(self.reactor.connections))

        client.close()
        pump(self.reactor)

        self.assertEqual(1, len(closed))
        self.assertEqual(0, len(self.reactor.connections))

    def test_close_when_flushed_delivers_buffered_output_first(self):
        def on_accept(conn):
            conn.send(b"goodbye")
            conn.close_when_flushed()

        listener = self.reactor.listen("127.0.0.1", 0, on_accept)
        client = connect(listener.address)
        self.addCleanup(client.close)
        pump(self.reactor)

        self.assertEqual(b"goodbye", client.recv(1024))
        # Then EOF, not a reset.
        self.assertEqual(b"", client.recv(1024))

    def test_large_write_is_buffered_and_flushed_across_steps(self):
        # Comfortably larger than any socket send buffer, so send() must
        # partially fail internally and finish on later writability events.
        payload = b"x" * (4 << 20)

        def on_accept(conn):
            conn.send(payload)
            conn.close_when_flushed()

        listener = self.reactor.listen("127.0.0.1", 0, on_accept)
        client = connect(listener.address)
        self.addCleanup(client.close)

        received = bytearray()
        for _ in range(2000):
            self.reactor.step(0.0)
            try:
                chunk = client.recv(1 << 16)
            except socket.timeout:
                break
            if not chunk:
                break
            received += chunk
            if len(received) >= len(payload):
                break

        self.assertEqual(len(payload), len(received))

    def test_timers_fire_in_deadline_then_scheduling_order(self):
        fired = []
        self.reactor.call_later(3.0, lambda: fired.append("third"))
        self.reactor.call_later(1.0, lambda: fired.append("first"))
        self.reactor.call_later(1.0, lambda: fired.append("second"))

        self.reactor.step(0.0)
        self.assertEqual([], fired)

        self.clock.advance(1.0)
        self.reactor.step(0.0)
        self.assertEqual(["first", "second"], fired)

        self.clock.advance(2.0)
        self.reactor.step(0.0)
        self.assertEqual(["first", "second", "third"], fired)

    def test_cancelled_timer_does_not_fire(self):
        fired = []
        timer = self.reactor.call_later(1.0, lambda: fired.append(True))
        timer.cancel()

        self.clock.advance(5.0)
        self.reactor.step(0.0)

        self.assertEqual([], fired)

    def test_timer_exception_does_not_stop_the_loop(self):
        fired = []

        def explode():
            raise RuntimeError("boom")

        self.reactor.call_later(1.0, explode)
        self.reactor.call_later(1.0, lambda: fired.append("survivor"))

        self.clock.advance(1.0)
        with self.assertLogs("exchangesim.core.reactor", level="ERROR"):
            self.reactor.step(0.0)

        self.assertEqual(["survivor"], fired)

    def test_send_after_close_is_a_no_op(self):
        def on_accept(conn):
            conn.close()
            conn.send(b"ignored")

        listener = self.reactor.listen("127.0.0.1", 0, on_accept)
        client = connect(listener.address)
        self.addCleanup(client.close)
        pump(self.reactor)

        self.assertEqual(b"", client.recv(1024))


class ConnectTest(unittest.TestCase):
    """Outbound connections: the reactor was acceptor-only until the web UI."""

    def setUp(self):
        self.clock = FixedClock()
        self.reactor = Reactor(self.clock)
        self.addCleanup(self.reactor.close)

    def _echo_listener(self):
        """A listener in the same reactor, so one loop drives both ends."""
        def on_accept(conn):
            conn.on_data = lambda c, chunk: c.send(chunk.upper())

        return self.reactor.listen("127.0.0.1", 0, on_accept).address

    def _dead_address(self):
        """A loopback port with nothing behind it."""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = sock.getsockname()
        sock.close()
        return address

    def test_a_connection_is_established_and_carries_data(self):
        address = self._echo_listener()
        connected, received = [], []

        def on_connect(conn):
            connected.append(conn)
            conn.on_data = lambda c, chunk: received.append(chunk)
            conn.send(b"ping")

        self.reactor.connect(address[0], address[1], on_connect)

        self.assertTrue(pump_until(self.reactor, lambda: received))
        self.assertEqual(1, len(connected))
        self.assertEqual([b"PING"], received)

    def test_the_callback_does_not_fire_before_connect_returns(self):
        address = self._echo_listener()
        fired = []

        conn = self.reactor.connect(address[0], address[1],
                                    lambda c: fired.append(c))

        self.assertEqual([], fired, "on_connect must not run re-entrantly")
        self.assertTrue(pump_until(self.reactor, lambda: fired))
        self.assertIs(conn, fired[0])

    def test_bytes_sent_before_the_connect_completes_are_buffered(self):
        address = self._echo_listener()
        received = []

        conn = self.reactor.connect(
            address[0], address[1],
            lambda c: None)
        conn.on_data = lambda c, chunk: received.append(chunk)
        conn.send(b"early")

        self.assertTrue(pump_until(self.reactor, lambda: received))
        self.assertEqual([b"EARLY"], received)

    def test_the_peer_address_is_filled_in_once_connected(self):
        address = self._echo_listener()
        connected = []

        self.reactor.connect(address[0], address[1], connected.append)
        pump_until(self.reactor, lambda: connected)

        self.assertEqual(address, connected[0].peer[:2])

    def test_a_refused_connection_is_reported_and_cleaned_up(self):
        """The one test that makes the OS actually refuse a connection.

        It asserts every consequence at once because it is slow by nature:
        Windows takes ~2s to refuse a loopback connect to a closed port, where
        Linux answers immediately. That cost buys the only real evidence that
        the SO_ERROR check distinguishes a failed connect from a successful
        one -- a refusal reports the socket *writable* on both platforms.
        """
        host, port = self._dead_address()
        failed, connected = [], []

        conn = self.reactor.connect(host, port, connected.append,
                                    lambda c, exc: failed.append(exc))

        self.assertTrue(pump_until(self.reactor, lambda: failed))
        self.assertEqual([], connected, "on_connect must not fire on failure")
        self.assertIsInstance(failed[0], OSError)
        self.assertTrue(conn.closed)
        self.assertNotIn(conn, self.reactor.connections)

    def test_a_failure_without_an_error_handler_is_survivable(self):
        # Driven through the failure path directly rather than through the OS:
        # the property under test is that a missing handler is not an exception,
        # which does not need a real refusal to demonstrate.
        address = self._echo_listener()
        conn = self.reactor.connect(address[0], address[1], lambda c: None)

        conn._fail_connect(errno.ECONNREFUSED)

        self.assertTrue(conn.closed)
        self.assertNotIn(conn, self.reactor.connections)


if __name__ == "__main__":
    unittest.main()
