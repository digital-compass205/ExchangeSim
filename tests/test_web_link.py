"""VenueLink: the non-blocking control client the web process talks through."""

import unittest

from exchangesim.core.clock import FixedClock
from exchangesim.core.reactor import Reactor
from exchangesim.web.link import MAX_RETRY, VenueLink

from .support import ServerThread, StubVenue, pump_until


class _StubConn(object):
    """Enough of a reactor Connection to exercise the link's own bookkeeping."""

    def __init__(self):
        self.out = b""
        self.closed = False

    def send(self, payload):
        self.out += payload

    def close(self):
        self.closed = True


def _emitter(registry):
    """A command that publishes, so events originate on the server's own thread."""
    @registry.add("emit", "Publish a topic, for tests.")
    def _emit(context, args):
        context.publisher.publish(args["topic"], args.get("data") or {})
        return {"published": args["topic"]}


class LinkAgainstRealServerTest(unittest.TestCase):

    def setUp(self):
        self.reactor = Reactor(FixedClock())
        self.addCleanup(self.reactor.close)
        self.events = []

    def link_to(self, harness, token=None):
        link = VenueLink(
            self.reactor, "japannext", "127.0.0.1", harness.port,
            token=token,
            on_event=lambda key, topic, data: self.events.append((key, topic, data)))
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(pump_until(self.reactor, lambda: link.connected),
                        "link never connected")
        return link

    def collect(self, link, command, args=None):
        replies = []
        link.call(command, args or {}, lambda ok, payload: replies.append((ok, payload)))
        pump_until(self.reactor, lambda: replies)
        return replies

    def test_a_command_round_trips(self):
        with ServerThread(venue=StubVenue("JNX-SIM")) as harness:
            link = self.link_to(harness)

            replies = self.collect(link, "ping")

            self.assertEqual(1, len(replies))
            ok, payload = replies[0]
            self.assertTrue(ok)
            self.assertEqual("JNX-SIM", payload["venue"])

    def test_an_unknown_command_reports_the_venue_error_code(self):
        with ServerThread() as harness:
            link = self.link_to(harness)

            ok, payload = self.collect(link, "no.such.command")[0]

            self.assertFalse(ok)
            self.assertEqual("unknown_command", payload["code"])

    def test_several_calls_are_matched_to_their_own_replies(self):
        with ServerThread(venue=StubVenue()) as harness:
            link = self.link_to(harness)
            replies = []

            for name in ("ping", "help", "ping"):
                link.call(name, {},
                          lambda ok, payload, n=name: replies.append((n, ok)))
            pump_until(self.reactor, lambda: len(replies) == 3)

            self.assertEqual([("ping", True), ("help", True), ("ping", True)],
                             replies)

    def test_a_subscribed_event_reaches_the_handler(self):
        with ServerThread(extra=_emitter) as harness:
            link = self.link_to(harness)
            link.subscribe(["trade:*"])

            self.collect(link, "emit", {"topic": "trade:DAY:7203",
                                        "data": {"price": "100.0"}})
            pump_until(self.reactor, lambda: self.events)

            self.assertEqual([("japannext", "trade:DAY:7203", {"price": "100.0"})],
                             self.events)

    def test_an_unsubscribed_topic_is_not_delivered(self):
        with ServerThread(extra=_emitter) as harness:
            link = self.link_to(harness)
            link.subscribe(["trade:*"])

            self.collect(link, "emit", {"topic": "book:DAY:7203"})

            self.assertEqual([], self.events)

    def test_a_token_is_presented_without_the_caller_asking(self):
        with ServerThread(venue=StubVenue(), token="s3cret") as harness:
            link = self.link_to(harness, token="s3cret")

            ok, _payload = self.collect(link, "ping")[0]

            self.assertTrue(ok)

    def test_a_missing_token_leaves_commands_unauthorised(self):
        with ServerThread(venue=StubVenue(), token="s3cret") as harness:
            link = self.link_to(harness)

            ok, payload = self.collect(link, "help")[0]

            self.assertFalse(ok)
            self.assertEqual("unauthorised", payload["code"])

    def test_the_link_describes_itself_as_connected(self):
        with ServerThread() as harness:
            link = self.link_to(harness)

            described = link.describe()

            self.assertTrue(described["connected"])
            self.assertIsNone(described["error"])
            self.assertEqual("japannext", described["key"])


class LinkFailureTest(unittest.TestCase):
    """Paths that a live server cannot produce on demand."""

    def setUp(self):
        self.reactor = Reactor(FixedClock())
        self.addCleanup(self.reactor.close)
        self.link = VenueLink(self.reactor, "japannext", "127.0.0.1", 1)
        self.replies = []

    def record(self, ok, payload):
        self.replies.append((ok, payload))

    def connect_stub(self):
        self.link._conn = _StubConn()
        self.link.connected = True
        return self.link._conn

    def test_a_call_while_disconnected_fails_at_once(self):
        self.link.call("ping", {}, self.record)

        self.assertEqual(1, len(self.replies))
        ok, payload = self.replies[0]
        self.assertFalse(ok)
        self.assertEqual("unavailable", payload["code"])

    def test_a_call_that_is_never_answered_times_out(self):
        self.connect_stub()
        self.link.call("slow", {}, self.record)
        self.assertEqual([], self.replies)

        self.reactor.clock.advance(self.link.call_timeout + 1)
        with self.assertLogs("exchangesim.web.link", level="WARNING"):
            self.reactor._expire_timers()

        self.assertEqual("timeout", self.replies[0][1]["code"])

    def test_a_disconnect_fails_every_call_still_outstanding(self):
        self.connect_stub()
        self.link.call("one", {}, self.record)
        self.link.call("two", {}, self.record)

        self.link._on_close(None)

        self.assertEqual(2, len(self.replies))
        self.assertEqual({"unavailable"},
                         set(payload["code"] for _ok, payload in self.replies))

    def test_a_reply_arriving_after_a_timeout_is_ignored(self):
        conn = self.connect_stub()
        self.link.call("slow", {}, self.record)
        self.reactor.clock.advance(self.link.call_timeout + 1)
        with self.assertLogs("exchangesim.web.link", level="WARNING"):
            self.reactor._expire_timers()
        conn.out = b""

        self.link._on_data(None, b'{"id":1,"ok":true,"result":{"late":true}}\n')

        self.assertEqual(1, len(self.replies), "the late reply must not fire again")

    def test_malformed_json_from_a_venue_is_logged_not_raised(self):
        self.connect_stub()

        with self.assertLogs("exchangesim.web.link", level="WARNING"):
            self.link._on_data(None, b"{not json}\n")

    def test_an_oversized_line_drops_the_link(self):
        conn = self.connect_stub()

        with self.assertLogs("exchangesim.web.link", level="ERROR"):
            self.link._on_data(None, b"x" * (4 << 20) + b"y")

        self.assertTrue(conn.closed)

    def test_topics_are_remembered_for_the_next_connection(self):
        self.link.subscribe(["trade:*", "book:*"])

        self.assertEqual({"trade:*", "book:*"}, self.link.topics)

    def test_reconnecting_replays_the_subscription(self):
        self.link.subscribe(["trade:*"])
        conn = self.connect_stub()

        self.link._on_connect(conn)

        self.assertIn(b'"subscribe"', conn.out)
        self.assertIn(b'"trade:*"', conn.out)

    def test_the_retry_delay_is_capped(self):
        """A venue restart is routine, so the board must not back off for minutes."""
        for _ in range(20):
            self.link._retry_timer = None
            self.link._on_close(None)

        self.assertLessEqual(self.link._retry, MAX_RETRY)

    def test_the_retry_delay_backs_off_and_resets_on_success(self):
        first = self.link._retry
        self.link._on_close(None)
        backed_off = self.link._retry
        self.assertGreater(backed_off, first)

        self.link._on_connect(self.connect_stub())

        self.assertEqual(first, self.link._retry)


if __name__ == "__main__":
    unittest.main()
