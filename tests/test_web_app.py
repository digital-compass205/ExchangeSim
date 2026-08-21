"""Routing, the command proxy, and the Server-Sent Events stream."""

import json
import unittest

from exchangesim.core.clock import FixedClock
from exchangesim.core.config import Config, ConfigError
from exchangesim.core.reactor import Reactor
from exchangesim.web.app import WebApp
from exchangesim.web.main import build_board

from .websupport import (
    FakeConn,
    FakeLink,
    json_request,
    make_request,
    parse_response,
    responder_for,
)


class AppTest(unittest.TestCase):

    def setUp(self):
        self.reactor = Reactor(FixedClock())
        self.addCleanup(self.reactor.close)
        self.app = WebApp(self.reactor)
        self.link = FakeLink("japannext")
        self.app.links["japannext"] = self.link
        self.app.order.append("japannext")

    # -- helpers -----------------------------------------------------------

    def call(self, request):
        conn = FakeConn()
        responder = responder_for(conn)
        self.app.handle(request, responder)
        return conn, responder

    def result_of(self, request):
        conn, _responder = self.call(request)
        status, _headers, body = parse_response(conn.out)
        return status, json.loads(body.decode("utf-8"))


class RoutingTest(AppTest):

    def test_the_root_serves_the_page(self):
        conn, _ = self.call(make_request("GET", "/"))

        status, headers, body = parse_response(conn.out)
        self.assertEqual(200, status)
        self.assertEqual("text/html; charset=utf-8", headers["content-type"])
        self.assertIn(b"ExchangeSim", body)

    def test_static_assets_are_served(self):
        conn, _ = self.call(make_request("GET", "/static/app.js"))

        status, headers, _body = parse_response(conn.out)
        self.assertEqual(200, status)
        self.assertEqual("application/javascript; charset=utf-8",
                         headers["content-type"])

    def test_an_unknown_path_is_404(self):
        status, _payload = self.result_of(make_request("GET", "/nope"))
        self.assertEqual(404, status)

    def test_an_unsupported_method_is_405(self):
        status, _payload = self.result_of(make_request("DELETE", "/"))
        self.assertEqual(405, status)

    def test_the_venue_list_reports_connection_state(self):
        status, payload = self.result_of(make_request("GET", "/api/venues"))

        self.assertEqual(200, status)
        self.assertEqual(["japannext"], [v["key"] for v in payload["venues"]])
        self.assertTrue(payload["venues"][0]["connected"])
        self.assertTrue(payload["allow_order_entry"])

    def test_a_disconnected_venue_is_still_listed(self):
        self.link.connected = False
        self.link.last_error = "connection refused"

        _status, payload = self.result_of(make_request("GET", "/api/venues"))

        self.assertFalse(payload["venues"][0]["connected"])
        self.assertEqual("connection refused", payload["venues"][0]["error"])

    def test_one_venue_can_be_described(self):
        status, payload = self.result_of(make_request("GET", "/api/japannext"))

        self.assertEqual(200, status)
        self.assertEqual("japannext", payload["key"])

    def test_an_unknown_venue_is_404(self):
        status, _payload = self.result_of(json_request("/api/nasdaq/bbo"))
        self.assertEqual(404, status)


class BoardLayoutTest(AppTest):
    """How the board is laid out and coloured, and where that is decided.

    Presentation only, and served from the config rather than chosen in the
    browser: two people describing the same screen the other way round is a
    real hazard, and a per-browser setting would produce exactly that.
    """

    def test_the_defaults_are_buy_right_and_the_japanese_colours(self):
        _status, payload = self.result_of(make_request("GET", "/api/venues"))

        self.assertEqual({"buy_side": "right", "buy_colour": "red",
                          "sell_colour": "green"}, payload["board"])

    def test_config_overrides_reach_the_page(self):
        self.app.board = dict(self.app.board)
        self.app.board.update({"buy_side": "left", "buy_colour": "blue"})

        _status, payload = self.result_of(make_request("GET", "/api/venues"))

        self.assertEqual("left", payload["board"]["buy_side"])
        self.assertEqual("blue", payload["board"]["buy_colour"])
        # Untouched keys keep their defaults rather than disappearing.
        self.assertEqual("green", payload["board"]["sell_colour"])

    def test_a_partial_board_config_is_filled_in(self):
        app = WebApp(self.reactor, board={"buy_side": "left"})

        self.assertEqual({"buy_side": "left", "buy_colour": "red",
                          "sell_colour": "green"}, app.board)


class BoardConfigTest(unittest.TestCase):
    """What ``board`` in web.json may say, and what it may not."""

    def _board(self, section):
        return build_board(Config({"board": section} if section is not None
                                  else {}))

    def test_no_board_section_means_the_defaults(self):
        self.assertEqual({}, self._board(None))

    def test_buy_side_is_accepted_either_way_round(self):
        self.assertEqual({"buy_side": "left"}, self._board({"buy_side": "LEFT"}))
        self.assertEqual({"buy_side": "right"},
                         self._board({"buy_side": "right"}))

    def test_an_unknown_side_names_the_real_ones(self):
        with self.assertRaises(ConfigError) as caught:
            self._board({"buy_side": "middle"})
        self.assertIn("right", str(caught.exception))

    def test_a_colour_is_a_palette_name(self):
        self.assertEqual({"buy_colour": "blue"},
                         self._board({"buy_colour": "Blue"}))

    def test_the_american_spelling_is_accepted(self):
        # Otherwise the config looks right, is silently ignored, and the board
        # comes up in its default colours.
        self.assertEqual({"sell_colour": "amber"},
                         self._board({"sell_color": "amber"}))

    def test_the_two_spellings_may_not_disagree(self):
        with self.assertRaises(ConfigError):
            self._board({"buy_colour": "blue", "buy_color": "amber"})

    def test_a_colour_off_the_palette_is_refused(self):
        # A free-text colour would let a config write an illegible board; the
        # named hues are the ones checked against the contrast floor.
        with self.assertRaises(ConfigError) as caught:
            self._board({"buy_colour": "#ff00ff"})
        self.assertIn("red", str(caught.exception))

    def test_both_sides_may_not_wear_one_colour(self):
        with self.assertRaises(ConfigError) as caught:
            self._board({"buy_colour": "green"})
        self.assertIn("cannot both be", str(caught.exception))

    def test_swapping_the_two_colours_is_allowed(self):
        board = self._board({"buy_colour": "green", "sell_colour": "red"})
        self.assertEqual({"buy_colour": "green", "sell_colour": "red"}, board)

    def test_board_must_be_an_object(self):
        with self.assertRaises(ConfigError):
            self._board("left")


class ProxyTest(AppTest):

    def test_a_command_is_forwarded_with_its_arguments(self):
        self.link.replies["bbo"] = (True, {"bid": "100.0"})

        status, payload = self.result_of(
            json_request("/api/japannext/bbo", b'{"symbol":"7203"}'))

        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual({"bid": "100.0"}, payload["result"])
        self.assertEqual([("bbo", {"symbol": "7203"})], self.link.calls)

    def test_a_dotted_command_name_survives_routing(self):
        self.result_of(json_request("/api/japannext/state.set", b'{}'))
        self.assertEqual("state.set", self.link.calls[0][0])

    def test_a_get_on_a_command_is_405(self):
        status, _payload = self.result_of(
            make_request("GET", "/api/japannext/bbo"))

        self.assertEqual(405, status)
        self.assertEqual([], self.link.calls)

    def test_a_form_content_type_is_refused(self):
        """The CSRF guard: a cross-site form cannot send application/json."""
        request = make_request(
            "POST", "/api/japannext/order.cancel",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=b"order_id=O1")

        status, _payload = self.result_of(request)

        self.assertEqual(415, status)
        self.assertEqual([], self.link.calls)

    def test_a_missing_content_type_is_refused(self):
        status, _payload = self.result_of(
            make_request("POST", "/api/japannext/bbo", body=b"{}"))
        self.assertEqual(415, status)

    def test_a_content_type_with_parameters_is_accepted(self):
        request = make_request(
            "POST", "/api/japannext/bbo",
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=b"{}")

        status, _payload = self.result_of(request)

        self.assertEqual(200, status)

    def test_an_invalid_json_body_is_400(self):
        status, _payload = self.result_of(
            json_request("/api/japannext/bbo", b"not json"))

        self.assertEqual(400, status)
        self.assertEqual([], self.link.calls)

    def test_a_json_array_body_is_refused(self):
        status, _payload = self.result_of(
            json_request("/api/japannext/bbo", b"[1,2]"))
        self.assertEqual(400, status)

    def test_an_empty_body_becomes_empty_arguments(self):
        self.result_of(json_request("/api/japannext/markets", b""))
        self.assertEqual([("markets", {})], self.link.calls)

    def test_a_venue_error_keeps_its_code_and_maps_to_a_status(self):
        self.link.replies["instrument.remove"] = (
            False, {"code": "conflict", "message": "has live orders"})

        status, payload = self.result_of(
            json_request("/api/japannext/instrument.remove", b'{"symbol":"7203"}'))

        self.assertEqual(409, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("conflict", payload["error"]["code"])

    def test_error_codes_map_onto_sensible_statuses(self):
        expected = {"not_found": 404, "unknown_command": 404, "bad_args": 400,
                    "unauthorised": 403, "unavailable": 503, "timeout": 504,
                    "internal": 500}
        for code, status in expected.items():
            self.link.replies["probe"] = (False, {"code": code, "message": "x"})
            self.assertEqual(
                status, self.result_of(json_request("/api/japannext/probe"))[0],
                "code %s" % code)

    def test_a_disconnected_venue_answers_503_rather_than_hanging(self):
        self.link.replies["bbo"] = (
            False, {"code": "unavailable", "message": "not connected"})

        status, payload = self.result_of(json_request("/api/japannext/bbo"))

        self.assertEqual(503, status)
        self.assertEqual("unavailable", payload["error"]["code"])

    def test_a_late_reply_after_the_client_left_is_dropped(self):
        """The browser may go away before a slow venue answers."""
        self.link.defer = True
        conn, responder = self.call(json_request("/api/japannext/bbo"))
        conn.close()
        responder.finished = True
        conn.take()

        self.link.release()

        self.assertEqual(b"", conn.out)


class OrderEntryGuardTest(AppTest):

    def test_order_entry_is_allowed_by_default(self):
        status, _payload = self.result_of(json_request("/api/japannext/order.new"))
        self.assertEqual(200, status)

    def test_order_entry_can_be_disabled(self):
        self.app.allow_order_entry = False

        status, payload = self.result_of(json_request("/api/japannext/order.new"))

        self.assertEqual(403, status)
        self.assertEqual([], self.link.calls)
        self.assertIn("disabled", payload["error"]["message"])

    def test_disabling_order_entry_leaves_read_commands_alone(self):
        self.app.allow_order_entry = False

        status, _payload = self.result_of(json_request("/api/japannext/bbo"))

        self.assertEqual(200, status)

    def test_the_flag_is_advertised_to_the_page(self):
        self.app.allow_order_entry = False
        _status, payload = self.result_of(make_request("GET", "/api/venues"))
        self.assertFalse(payload["allow_order_entry"])


class MarketControlGuardTest(AppTest):
    """Moving a market is gated separately from trading on it."""

    def test_market_control_is_allowed_by_default(self):
        status, _payload = self.result_of(json_request("/api/japannext/state.set"))
        self.assertEqual(200, status)

    def test_market_control_can_be_disabled(self):
        self.app.allow_market_control = False

        status, payload = self.result_of(json_request("/api/japannext/state.set"))

        self.assertEqual(403, status)
        self.assertEqual([], self.link.calls)
        self.assertIn("disabled", payload["error"]["message"])

    def test_every_phase_changing_command_is_covered(self):
        self.app.allow_market_control = False

        for command in ("state.set", "state.clear", "stp.set",
                        "auction.lock", "auction.reference"):
            status, _payload = self.result_of(
                json_request("/api/japannext/" + command))
            self.assertEqual(403, status, command)

    def test_disabling_it_leaves_order_entry_alone(self):
        """The two switches are independent, which is the point of having two."""
        self.app.allow_market_control = False

        status, _payload = self.result_of(json_request("/api/japannext/order.new"))

        self.assertEqual(200, status)

    def test_disabling_order_entry_leaves_market_control_alone(self):
        self.app.allow_order_entry = False

        status, _payload = self.result_of(json_request("/api/japannext/state.set"))

        self.assertEqual(200, status)

    def test_reading_the_state_is_never_gated(self):
        self.app.allow_market_control = False

        status, _payload = self.result_of(json_request("/api/japannext/state.get"))

        self.assertEqual(200, status)

    def test_the_flag_is_advertised_to_the_page(self):
        self.app.allow_market_control = False
        _status, payload = self.result_of(make_request("GET", "/api/venues"))
        self.assertFalse(payload["allow_market_control"])


class AuditGuardTest(AppTest):
    """Reading everyone's traffic is a third power, with its own switch."""

    def test_the_audit_is_served_by_default(self):
        status, _payload = self.result_of(json_request("/api/japannext/audit"))
        self.assertEqual(200, status)

    def test_the_audit_can_be_disabled(self):
        self.app.allow_audit = False

        status, payload = self.result_of(json_request("/api/japannext/audit"))

        self.assertEqual(403, status)
        self.assertEqual([], self.link.calls)
        self.assertIn("disabled", payload["error"]["message"])

    def test_every_audit_command_is_covered(self):
        self.app.allow_audit = False

        for command in ("audit", "audit.entry", "audit.types"):
            status, _payload = self.result_of(
                json_request("/api/japannext/" + command))
            self.assertEqual(403, status, command)

    def test_a_board_that_cannot_trade_still_does_not_get_the_audit(self):
        """The point of the third switch: the two powers are unrelated."""
        self.app.allow_order_entry = False
        self.app.allow_audit = False

        status, _payload = self.result_of(json_request("/api/japannext/audit"))

        self.assertEqual(403, status)

    def test_disabling_the_audit_leaves_order_entry_alone(self):
        self.app.allow_audit = False

        status, _payload = self.result_of(json_request("/api/japannext/order.new"))

        self.assertEqual(200, status)

    def test_the_flag_is_advertised_to_the_page(self):
        self.app.allow_audit = False
        _status, payload = self.result_of(make_request("GET", "/api/venues"))
        self.assertFalse(payload["allow_audit"])


class EventStreamTest(AppTest):

    def open_stream(self, query=None):
        conn = FakeConn()
        responder = responder_for(conn)
        self.app.handle(
            make_request("GET", "/api/japannext/events", query=query or {}),
            responder)
        return conn, responder

    def events_in(self, raw):
        """Pull the JSON payload out of each `data:` line."""
        return [json.loads(line[len("data: "):])
                for line in raw.decode("utf-8").split("\n")
                if line.startswith("data: ")]

    def test_the_stream_opens_with_event_stream_headers(self):
        conn, _responder = self.open_stream()

        _status, headers, _body = parse_response(conn.out)
        self.assertEqual("text/event-stream", headers["content-type"])
        self.assertEqual("no-cache, no-store", headers["cache-control"])

    def test_opening_a_stream_subscribes_the_venue(self):
        self.open_stream()
        self.assertEqual({"*"}, self.link.topics)

    def test_a_published_event_reaches_the_browser(self):
        conn, _responder = self.open_stream()
        conn.take()

        self.app._on_venue_event("japannext", "trade:DAY:7203", {"price": "100"})

        events = self.events_in(conn.out)
        self.assertEqual([{"topic": "trade:DAY:7203", "data": {"price": "100"}}],
                         events)

    def test_the_venue_prefix_is_stripped_before_the_browser_sees_it(self):
        conn, _responder = self.open_stream()
        conn.take()

        self.app._on_venue_event("japannext", "book:DAY:7203", {})

        self.assertEqual("book:DAY:7203", self.events_in(conn.out)[0]["topic"])

    def test_topics_filter_what_is_delivered(self):
        conn, _responder = self.open_stream({"topics": "trade:*"})
        conn.take()

        self.app._on_venue_event("japannext", "book:DAY:7203", {})
        self.app._on_venue_event("japannext", "trade:DAY:7203", {"price": "1"})

        self.assertEqual(["trade:DAY:7203"],
                         [event["topic"] for event in self.events_in(conn.out)])

    def test_several_topics_can_be_requested(self):
        conn, _responder = self.open_stream({"topics": "trade:*,state:*"})
        conn.take()

        self.app._on_venue_event("japannext", "state:DAY", {"state": "OPEN"})
        self.app._on_venue_event("japannext", "book:DAY:7203", {})

        self.assertEqual(["state:DAY"],
                         [event["topic"] for event in self.events_in(conn.out)])

    def test_another_venues_events_are_not_delivered(self):
        other = FakeLink("hkex")
        self.app.links["hkex"] = other
        self.app.order.append("hkex")
        conn, _responder = self.open_stream()
        conn.take()

        self.app._on_venue_event("hkex", "trade:MAIN:0700", {"price": "1"})

        self.assertEqual([], self.events_in(conn.out))

    def test_two_browsers_both_receive_an_event(self):
        first, _ = self.open_stream()
        second, _ = self.open_stream()
        first.take()
        second.take()

        self.app._on_venue_event("japannext", "trade:DAY:7203", {"price": "1"})

        self.assertEqual(1, len(self.events_in(first.out)))
        self.assertEqual(1, len(self.events_in(second.out)))

    def test_a_closed_stream_is_forgotten(self):
        conn, _responder = self.open_stream()
        self.assertEqual(1, len(self.app.streams))

        conn.close()

        self.assertEqual(0, len(self.app.streams))
        self.assertFalse(self.app.publisher.has_subscribers)

    def test_a_closed_stream_stops_receiving(self):
        conn, _responder = self.open_stream()
        conn.close()
        conn.take()

        self.app._on_venue_event("japannext", "trade:DAY:7203", {"price": "1"})

        self.assertEqual(b"", conn.out)

    def test_a_post_to_the_stream_is_405(self):
        status, _payload = self.result_of(
            json_request("/api/japannext/events"))
        self.assertEqual(405, status)

    def test_the_keepalive_beat_writes_a_comment(self):
        conn, _responder = self.open_stream()
        conn.take()

        self.reactor._expire_timers()          # nothing due yet
        self.assertEqual(b"", conn.out)

        self.reactor.clock.advance(30)
        self.reactor._expire_timers()

        self.assertIn(b": keepalive", conn.out)

    def test_the_keepalive_reaps_a_stream_whose_socket_died(self):
        conn, _responder = self.open_stream()
        conn.closed = True                     # died without notifying us

        self.reactor.clock.advance(30)
        self.reactor._expire_timers()

        self.assertEqual(0, len(self.app.streams))


if __name__ == "__main__":
    unittest.main()
