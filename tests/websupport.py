"""Harness for the web layer: a fake socket and a fake venue link.

The HTTP parser and the router are both pure given their inputs, so neither
needs a real socket to test. What a real socket would add is coverage of the
reactor, which ``test_reactor`` already provides, and seconds of runtime.
"""

from exchangesim.web.http import HttpConnection, Request, Responder


class FakeConn(object):
    """Stands in for a reactor Connection, recording everything written."""

    def __init__(self, peer=("127.0.0.1", 51234)):
        self.peer = peer
        self.out = b""
        self.closed = False
        self.on_data = None
        self.on_close = None
        self.data = None

    def send(self, payload):
        if not self.closed:
            self.out += payload

    def close(self):
        self.closed = True
        if self.on_close is not None:
            handler, self.on_close = self.on_close, None
            handler(self)

    def close_when_flushed(self):
        self.close()

    # -- assertions --------------------------------------------------------

    def take(self):
        out, self.out = self.out, b""
        return out

    def text(self):
        return self.out.decode("utf-8", "replace")


class FakeServer(object):
    """Collects what HttpConnection dispatches, without routing it."""

    def __init__(self, handler=None):
        self.requests = []
        self.connections = set()
        self.handler = handler

    def dispatch(self, request, responder):
        self.requests.append((request, responder))
        if self.handler is not None:
            self.handler(request, responder)


def feed(chunks, handler=None):
    """Push raw bytes through an HttpConnection; returns (conn, server)."""
    conn = FakeConn()
    server = FakeServer(handler)
    connection = HttpConnection(conn, server)
    conn.on_data = connection.on_data
    conn.on_close = connection.on_close
    for chunk in ([chunks] if isinstance(chunks, bytes) else chunks):
        connection.on_data(conn, chunk)
    return conn, server


def raw_request(method="GET", target="/", headers=None, body=b"",
                version="HTTP/1.1"):
    lines = ["%s %s %s" % (method, target, version)]
    for name, value in (headers or {}).items():
        lines.append("%s: %s" % (name, value))
    if body:
        lines.append("Content-Length: %d" % len(body))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def parse_response(raw):
    """Split a raw HTTP response into (status, headers, body)."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


def make_request(method="GET", path="/", query=None, headers=None, body=b""):
    """Build a Request directly, for testing the router without parsing."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    return Request(method=method, path=path, query=query or {},
                   headers=dict((k.lower(), v)
                                for k, v in (headers or {}).items()),
                   body=body, peer="127.0.0.1:51234")


def json_request(path, payload_bytes=b"{}", method="POST"):
    return make_request(method, path,
                        headers={"Content-Type": "application/json"},
                        body=payload_bytes)


def responder_for(conn=None):
    """A Responder wired to its connection the way HttpConnection wires one.

    Without mirroring that wiring, a closed socket would never reach
    ``responder.on_close`` and a test would wrongly conclude that streams are
    not cleaned up.
    """
    conn = conn or FakeConn()
    responder = Responder(conn, keep_alive=True)

    def on_close(_conn):
        if responder.on_close is not None:
            responder.on_close()

    conn.on_close = on_close
    return responder


class FakeLink(object):
    """A venue link whose replies the test chooses."""

    def __init__(self, key="japannext", connected=True):
        self.key = key
        self.label = key.title()
        self.host = "127.0.0.1"
        self.port = 9101
        self.connected = connected
        self.last_error = None
        self.topics = set()
        self.calls = []
        #: command -> (ok, payload); anything unset echoes the command back.
        self.replies = {}
        #: When set, calls are recorded but the callback is held for later.
        self.defer = False
        self.deferred = []

    def call(self, command, args, callback):
        self.calls.append((command, args))
        ok, payload = self.replies.get(command, (True, {"command": command}))
        if self.defer:
            self.deferred.append((callback, ok, payload))
            return
        callback(ok, payload)

    def release(self):
        """Deliver every held reply."""
        held, self.deferred = self.deferred, []
        for callback, ok, payload in held:
            callback(ok, payload)

    def subscribe(self, topics):
        self.topics |= set(topics)

    def start(self):
        return self

    def stop(self):
        pass

    def describe(self):
        return {"key": self.key, "label": self.label, "host": self.host,
                "port": self.port, "connected": self.connected,
                "error": self.last_error}
