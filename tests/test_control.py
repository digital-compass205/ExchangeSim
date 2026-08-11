"""Control plane: dispatch, error reporting, auth, and streaming subscriptions."""

import time
import unittest

from exchangesim.cli.client import CommandFailed, ControlClient
from exchangesim.control.commands import (
    CommandError,
    CommandRegistry,
    E_NOT_FOUND,
    arg_choice,
    arg_int,
    arg_str,
)
from exchangesim.control.subscriptions import Publisher

from .support import ServerThread, StubVenue


class RegistryTest(unittest.TestCase):
    """Dispatch is exercised directly -- no socket needed."""

    def setUp(self):
        self.registry = CommandRegistry()

    def test_dispatch_passes_context_and_args(self):
        seen = {}

        @self.registry.add("echo", "echo back")
        def _echo(context, args):
            seen["context"] = context
            return {"got": args.get("value")}

        result = self.registry.dispatch("echo", "CTX", {"value": 42})

        self.assertEqual({"got": 42}, result)
        self.assertEqual("CTX", seen["context"])

    def test_unknown_command_is_reported_not_raised_as_a_bug(self):
        with self.assertRaises(CommandError) as caught:
            self.registry.dispatch("nope", None, {})
        self.assertEqual("unknown_command", caught.exception.code)

    def test_non_object_args_are_rejected(self):
        self.registry.register("x", lambda c, a: None, "")
        with self.assertRaises(CommandError) as caught:
            self.registry.dispatch("x", None, ["not", "an", "object"])
        self.assertEqual("bad_request", caught.exception.code)

    def test_duplicate_registration_is_a_programming_error(self):
        self.registry.register("x", lambda c, a: None, "")
        with self.assertRaises(ValueError):
            self.registry.register("x", lambda c, a: None, "")

    def test_custom_error_code_survives_dispatch(self):
        @self.registry.add("missing", "")
        def _missing(context, args):
            raise CommandError("no such order", E_NOT_FOUND)

        with self.assertRaises(CommandError) as caught:
            self.registry.dispatch("missing", None, {})
        self.assertEqual(E_NOT_FOUND, caught.exception.code)


class ArgHelperTest(unittest.TestCase):

    def test_required_argument_names_itself_when_absent(self):
        with self.assertRaises(CommandError) as caught:
            arg_str({}, "symbol", required=True)
        self.assertIn("symbol", str(caught.exception))

    def test_string_type_is_enforced(self):
        with self.assertRaises(CommandError):
            arg_str({"symbol": 7203}, "symbol")

    def test_upper_normalises(self):
        self.assertEqual("DAY", arg_str({"market": "day"}, "market", upper=True))

    def test_booleans_are_not_integers(self):
        # bool is a subclass of int in Python; a caller passing true for a
        # quantity is a mistake worth catching.
        with self.assertRaises(CommandError):
            arg_int({"qty": True}, "qty")

    def test_integer_bounds_are_enforced(self):
        self.assertEqual(5, arg_int({"n": 5}, "n", minimum=1, maximum=10))
        with self.assertRaises(CommandError):
            arg_int({"n": 0}, "n", minimum=1)
        with self.assertRaises(CommandError):
            arg_int({"n": 11}, "n", maximum=10)

    def test_choice_lists_the_valid_values(self):
        with self.assertRaises(CommandError) as caught:
            arg_choice({"state": "SNOOZE"}, "state", {"OPEN", "CLOSED"})
        message = str(caught.exception)
        self.assertIn("OPEN", message)
        self.assertIn("CLOSED", message)


class PublisherTest(unittest.TestCase):

    class Sink(object):
        def __init__(self):
            self.events = []

        def push(self, topic, data):
            self.events.append((topic, data))

    def setUp(self):
        self.publisher = Publisher()

    def test_exact_topic_delivery(self):
        sink = self.Sink()
        self.publisher.subscribe(sink, ["trade:DAY:7203"])

        self.publisher.publish("trade:DAY:7203", {"px": 100})
        self.publisher.publish("trade:DAY:9984", {"px": 200})

        self.assertEqual([("trade:DAY:7203", {"px": 100})], sink.events)

    def test_wildcard_patterns(self):
        sink = self.Sink()
        self.publisher.subscribe(sink, ["trade:*"])

        self.publisher.publish("trade:DAY:7203", {})
        self.publisher.publish("book:DAY:7203", {})

        self.assertEqual(1, len(sink.events))

    def test_publish_with_no_subscribers_is_free(self):
        self.assertEqual(0, self.publisher.publish("trade:X", {}))

    def test_unsubscribe_specific_patterns(self):
        sink = self.Sink()
        self.publisher.subscribe(sink, ["trade:*", "book:*"])

        remaining = self.publisher.unsubscribe(sink, ["trade:*"])

        self.assertEqual(["book:*"], remaining)
        self.publisher.publish("trade:DAY:7203", {})
        self.assertEqual([], sink.events)

    def test_unsubscribe_everything(self):
        sink = self.Sink()
        self.publisher.subscribe(sink, ["trade:*"])
        self.publisher.unsubscribe(sink, None)
        self.assertEqual(0, self.publisher.publish("trade:X", {}))

    def test_a_failing_subscriber_cannot_disrupt_delivery(self):
        class Broken(object):
            def push(self, topic, data):
                raise RuntimeError("subscriber exploded")

        healthy = self.Sink()
        self.publisher.subscribe(Broken(), ["trade:*"])
        self.publisher.subscribe(healthy, ["trade:*"])

        with self.assertLogs("exchangesim.control.subscriptions", level="ERROR"):
            delivered = self.publisher.publish("trade:DAY:7203", {"px": 1})

        self.assertEqual(1, delivered)
        self.assertEqual(1, len(healthy.events))


class ControlServerTest(unittest.TestCase):
    """End-to-end over a real socket, using the real CLI client."""

    def test_ping_reports_the_venue(self):
        with ServerThread(venue=StubVenue("JNX-SIM")) as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                result = client.call("ping")

        self.assertTrue(result["pong"])
        self.assertEqual("JNX-SIM", result["venue"])

    def test_help_lists_registered_commands(self):
        with ServerThread() as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                names = [c["command"] for c in client.call("help")["commands"]]

        self.assertIn("ping", names)
        self.assertIn("subscribe", names)

    def test_unknown_command_returns_a_structured_error(self):
        with ServerThread() as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                with self.assertRaises(CommandFailed) as caught:
                    client.call("does.not.exist")

        self.assertEqual("unknown_command", caught.exception.code)

    def test_handler_exception_becomes_an_internal_error_not_a_hang(self):
        def extra(registry):
            @registry.add("explode", "always fails")
            def _explode(context, args):
                raise RuntimeError("kaboom")

        with ServerThread(extra=extra) as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                with self.assertLogs("exchangesim.control.server", level="ERROR"):
                    with self.assertRaises(CommandFailed) as caught:
                        client.call("explode")
                # The connection must remain usable afterwards.
                self.assertTrue(client.call("ping")["pong"])

        self.assertEqual("internal", caught.exception.code)

    def test_several_requests_share_one_connection(self):
        with ServerThread() as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                for _ in range(5):
                    self.assertTrue(client.call("ping")["pong"])

    def test_token_is_required_when_configured(self):
        with ServerThread(token="s3cret") as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                with self.assertRaises(CommandFailed) as caught:
                    client.call("help")
                self.assertEqual("unauthorised", caught.exception.code)
                # ping is exempt so liveness probes work without credentials.
                self.assertTrue(client.call("ping")["pong"])

    def test_correct_token_unlocks_the_session(self):
        with ServerThread(token="s3cret") as harness:
            # The client authenticates during connect() when given a token.
            with ControlClient("127.0.0.1", harness.port, token="s3cret") as client:
                self.assertIn("commands", client.call("help"))

    def test_wrong_token_is_rejected(self):
        with ServerThread(token="s3cret") as harness:
            client = ControlClient("127.0.0.1", harness.port, token="wrong")
            with self.assertRaises(CommandFailed) as caught:
                client.connect()
            client.close()

        self.assertEqual("unauthorised", caught.exception.code)

    def test_subscription_streams_events_to_the_client(self):
        with ServerThread() as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                client.subscribe(["trade:*"])

                harness.publisher.publish("trade:DAY:7203", {"px": 2845.5, "qty": 100})
                harness.publisher.publish("book:DAY:7203", {"ignored": True})

                event = next(client.events())

        self.assertEqual("trade:DAY:7203", event["topic"])
        self.assertEqual(100, event["data"]["qty"])

    def test_commands_still_work_while_subscribed(self):
        with ServerThread() as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                client.subscribe(["trade:*"])
                harness.publisher.publish("trade:DAY:7203", {"px": 1})
                # The pending push must not be mistaken for the ping reply.
                self.assertTrue(client.call("ping")["pong"])

    def test_malformed_json_does_not_kill_the_connection(self):
        with ServerThread() as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                client._sock.sendall(b"{ this is not json }\n")
                response = client._read_message()
                self.assertFalse(response["ok"])
                self.assertEqual("bad_request", response["error"]["code"])
                self.assertTrue(client.call("ping")["pong"])

    def test_request_without_cmd_is_rejected(self):
        with ServerThread() as harness:
            with ControlClient("127.0.0.1", harness.port) as client:
                client._send({"id": 1, "args": {}})
                response = client._read_message()

        self.assertFalse(response["ok"])
        self.assertEqual("bad_request", response["error"]["code"])

    def test_disconnect_removes_the_subscription(self):
        with ServerThread() as harness:
            client = ControlClient("127.0.0.1", harness.port).connect()
            client.subscribe(["trade:*"])
            self.assertTrue(harness.publisher.has_subscribers)
            client.close()

            # The server thread needs a moment to see the EOF. Publishing at a
            # dead socket in the meantime must not raise.
            deadline = time.time() + 5.0
            while time.time() < deadline and harness.publisher.has_subscribers:
                harness.publisher.publish("trade:DAY:7203", {})
                time.sleep(0.01)

            self.assertFalse(harness.publisher.has_subscribers)


if __name__ == "__main__":
    unittest.main()
