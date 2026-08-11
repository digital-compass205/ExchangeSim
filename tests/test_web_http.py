"""The hand-rolled HTTP/1.1 parser, responder and static file guard."""

import os
import shutil
import tempfile
import unittest

from exchangesim.web.http import (
    MAX_BODY_BYTES,
    content_type_for,
    safe_join,
    serve_file,
)

from .websupport import FakeConn, feed, parse_response, raw_request, responder_for


class RequestParsingTest(unittest.TestCase):

    def test_a_simple_get_is_parsed(self):
        _conn, server = feed(raw_request("GET", "/api/venues"))

        request, _responder = server.requests[0]
        self.assertEqual("GET", request.method)
        self.assertEqual("/api/venues", request.path)
        self.assertEqual({}, request.query)

    def test_the_query_string_is_split_off_and_decoded(self):
        _conn, server = feed(raw_request("GET", "/api/x/events?topics=book%3A*"))

        request, _ = server.requests[0]
        self.assertEqual("/api/x/events", request.path)
        self.assertEqual({"topics": "book:*"}, request.query)

    def test_a_percent_encoded_path_is_decoded(self):
        _conn, server = feed(raw_request("GET", "/static/a%20b.js"))

        self.assertEqual("/static/a b.js", server.requests[0][0].path)

    def test_header_names_are_case_insensitive(self):
        _conn, server = feed(raw_request(
            "GET", "/", headers={"CoNtEnT-TyPe": "application/json"}))

        request, _ = server.requests[0]
        self.assertEqual("application/json", request.header("Content-Type"))

    def test_a_body_is_read_using_content_length(self):
        _conn, server = feed(raw_request("POST", "/api/v/cmd", body=b'{"a":1}'))

        request, _ = server.requests[0]
        self.assertEqual(b'{"a":1}', request.body)
        self.assertEqual({"a": 1}, request.json_body())

    def test_a_request_split_across_packets_is_reassembled(self):
        raw = raw_request("POST", "/api/v/cmd", body=b'{"hello":"world"}')
        _conn, server = feed([raw[:12], raw[12:30], raw[30:]])

        self.assertEqual(1, len(server.requests))
        self.assertEqual({"hello": "world"}, server.requests[0][0].json_body())

    def test_an_incomplete_request_dispatches_nothing(self):
        _conn, server = feed(b"GET / HTTP/1.1\r\nHost: x\r\n")

        self.assertEqual([], server.requests)

    def test_a_body_that_has_not_arrived_yet_waits(self):
        raw = raw_request("POST", "/x", body=b"0123456789")
        _conn, server = feed(raw[:-4])

        self.assertEqual([], server.requests)

    def test_pipelined_requests_are_both_dispatched(self):
        def answer(_request, responder):
            responder.send(200, b"ok")

        _conn, server = feed(
            raw_request("GET", "/one") + raw_request("GET", "/two"),
            handler=answer)

        self.assertEqual(["/one", "/two"],
                         [request.path for request, _ in server.requests])

    def test_a_malformed_request_line_is_refused(self):
        conn, server = feed(b"NONSENSE\r\n\r\n")

        status, _headers, _body = parse_response(conn.out)
        self.assertEqual(400, status)
        self.assertEqual([], server.requests)

    def test_an_absolute_uri_target_is_refused(self):
        conn, _server = feed(raw_request("GET", "http://elsewhere/x"))

        self.assertEqual(400, parse_response(conn.out)[0])

    def test_a_malformed_header_line_is_refused(self):
        conn, _server = feed(b"GET / HTTP/1.1\r\nnot-a-header\r\n\r\n")

        self.assertEqual(400, parse_response(conn.out)[0])

    def test_an_oversized_header_block_is_refused(self):
        conn, _server = feed(b"GET / HTTP/1.1\r\nX: " + b"a" * 70000)

        self.assertEqual(431, parse_response(conn.out)[0])

    def test_an_oversized_body_is_refused_without_buffering_it(self):
        conn, server = feed(
            b"POST /x HTTP/1.1\r\nContent-Length: %d\r\n\r\n"
            % (MAX_BODY_BYTES + 1))

        self.assertEqual(413, parse_response(conn.out)[0])
        self.assertEqual([], server.requests)

    def test_a_non_numeric_content_length_is_refused(self):
        conn, _server = feed(b"POST /x HTTP/1.1\r\nContent-Length: many\r\n\r\n")

        self.assertEqual(400, parse_response(conn.out)[0])


class KeepAliveTest(unittest.TestCase):

    def _respond(self, raw):
        conn, _server = feed(raw, handler=lambda r, resp: resp.send(200, b"hi"))
        return parse_response(conn.out)

    def test_http_11_defaults_to_keep_alive(self):
        _status, headers, _body = self._respond(raw_request("GET", "/"))
        self.assertEqual("keep-alive", headers["connection"])

    def test_connection_close_is_honoured(self):
        _status, headers, _body = self._respond(
            raw_request("GET", "/", headers={"Connection": "close"}))
        self.assertEqual("close", headers["connection"])

    def test_http_10_defaults_to_close(self):
        _status, headers, _body = self._respond(
            raw_request("GET", "/", version="HTTP/1.0"))
        self.assertEqual("close", headers["connection"])


class ResponderTest(unittest.TestCase):

    def test_a_body_is_sent_with_its_length(self):
        conn = FakeConn()
        responder_for(conn).send(200, b"hello", "text/plain")

        status, headers, body = parse_response(conn.out)
        self.assertEqual(200, status)
        self.assertEqual("5", headers["content-length"])
        self.assertEqual(b"hello", body)

    def test_a_string_body_is_encoded_as_utf8(self):
        conn = FakeConn()
        responder_for(conn).send(200, u"値段")

        _status, headers, body = parse_response(conn.out)
        self.assertEqual(u"値段", body.decode("utf-8"))
        self.assertEqual(str(len(body)), headers["content-length"])

    def test_json_sets_its_content_type(self):
        conn = FakeConn()
        responder_for(conn).json(200, {"a": 1})

        _status, headers, body = parse_response(conn.out)
        self.assertEqual("application/json; charset=utf-8", headers["content-type"])
        self.assertEqual(b'{"a":1}', body)

    def test_an_error_carries_a_structured_payload(self):
        conn = FakeConn()
        responder_for(conn).error(404, "nope")

        status, _headers, body = parse_response(conn.out)
        self.assertEqual(404, status)
        self.assertIn(b"nope", body)

    def test_a_second_response_is_ignored(self):
        conn = FakeConn()
        responder = responder_for(conn)
        responder.send(200, b"first")
        with self.assertLogs("exchangesim.web.http", level="WARNING"):
            responder.send(200, b"second")

        self.assertNotIn(b"second", conn.out)

    def test_a_stream_declares_no_length_and_closes_the_connection(self):
        conn = FakeConn()
        responder = responder_for(conn)
        responder.begin_stream("text/event-stream")

        _status, headers, _body = parse_response(conn.out)
        self.assertEqual("text/event-stream", headers["content-type"])
        self.assertEqual("close", headers["connection"])
        self.assertNotIn("content-length", headers)

    def test_stream_writes_go_straight_out(self):
        conn = FakeConn()
        responder = responder_for(conn)
        responder.begin_stream("text/event-stream")
        conn.take()

        responder.write("data: 1\n\n")

        self.assertEqual(b"data: 1\n\n", conn.out)

    def test_writing_after_close_is_a_no_op(self):
        conn = FakeConn()
        responder = responder_for(conn)
        responder.begin_stream("text/event-stream")
        conn.close()
        conn.take()

        responder.write("data: lost\n\n")

        self.assertEqual(b"", conn.out)


class StaticFileTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exsim-web-")
        self.addCleanup(shutil.rmtree, self.root, True)
        with open(os.path.join(self.root, "index.html"), "w") as handle:
            handle.write("<h1>board</h1>")

    def test_a_file_is_served_with_its_content_type(self):
        conn = FakeConn()
        serve_file(responder_for(conn), self.root, "index.html")

        status, headers, body = parse_response(conn.out)
        self.assertEqual(200, status)
        self.assertEqual("text/html; charset=utf-8", headers["content-type"])
        self.assertEqual(b"<h1>board</h1>", body)

    def test_a_missing_file_is_404(self):
        conn = FakeConn()
        serve_file(responder_for(conn), self.root, "absent.js")

        self.assertEqual(404, parse_response(conn.out)[0])

    def test_traversal_is_refused(self):
        conn = FakeConn()
        serve_file(responder_for(conn), self.root, "../../secrets.txt")

        self.assertEqual(403, parse_response(conn.out)[0])

    def test_safe_join_rejects_escapes_however_spelt(self):
        for attempt in ("../x", "a/../../x", "..\\x", "/../x", "a/./../../x"):
            self.assertIsNone(safe_join(self.root, attempt), attempt)

    def test_safe_join_allows_ordinary_paths(self):
        self.assertIsNotNone(safe_join(self.root, "app.js"))
        self.assertIsNotNone(safe_join(self.root, "sub/app.js"))

    def test_a_sibling_directory_sharing_a_prefix_is_refused(self):
        """`/srv/static-secret` must not pass a check against `/srv/static`."""
        self.assertIsNone(safe_join("/srv/static", "../static-secret/x"))

    def test_content_types_cover_what_the_board_serves(self):
        self.assertEqual("text/html; charset=utf-8", content_type_for("a.html"))
        self.assertEqual("application/javascript; charset=utf-8",
                         content_type_for("a.js"))
        self.assertEqual("text/css; charset=utf-8", content_type_for("a.css"))
        self.assertEqual("application/octet-stream", content_type_for("a.bin"))


if __name__ == "__main__":
    unittest.main()
