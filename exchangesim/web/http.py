"""A small HTTP/1.1 server on the reactor.

Hand-rolled rather than built on ``http.server``, for the same reason the FIX
framer is hand-rolled: ``BaseHTTPRequestHandler`` is a blocking, one-thread-per-
connection design, and this process is a single-threaded reactor by deliberate
choice. What is needed is a request line, some headers, an optional body, and
the ability to hold a response open indefinitely for Server-Sent Events -- which
is exactly the case ``http.server`` handles worst.

Only what the board needs is implemented: GET and POST, ``Content-Length``
bodies, keep-alive, and streaming responses. No chunked request bodies, no
``Expect: 100-continue``, no compression.
"""

import json
import logging
import os
import posixpath

try:
    from urllib.parse import parse_qs, unquote
except ImportError:                                        # pragma: no cover
    from urlparse import parse_qs                          # noqa: F401
    from urllib import unquote                             # noqa: F401

log = logging.getLogger(__name__)

#: A request whose headers exceed this is refused rather than buffered.
MAX_HEADER_BYTES = 64 * 1024

#: Bodies are small JSON commands; anything larger is a mistake or an attack.
MAX_BODY_BYTES = 1024 * 1024

STATUS_TEXT = {
    200: "OK",
    204: "No Content",
    304: "Not Modified",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


class Request(object):
    """One parsed HTTP request."""

    __slots__ = ("method", "path", "query", "headers", "body", "peer")

    def __init__(self, method, path, query, headers, body, peer):
        self.method = method
        #: Percent-decoded path, without the query string.
        self.path = path
        #: Query parameters, first value only -- none of the routes repeat a key.
        self.query = query
        #: Header names lowercased, because HTTP header names are case-insensitive.
        self.headers = headers
        self.body = body
        self.peer = peer

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def json_body(self):
        """Decode the body as a JSON object, or raise ``ValueError``."""
        if not self.body:
            return {}
        value = json.loads(self.body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("body must be a JSON object")
        return value

    def __repr__(self):
        return "Request(%s %s)" % (self.method, self.path)


class Responder(object):
    """The reply half of one request.

    A handler either calls :meth:`send` once, or calls :meth:`begin_stream` and
    then :meth:`write` for as long as it likes -- the latter is how Server-Sent
    Events stay open. Set :attr:`on_close` to learn when the client goes away.
    """

    __slots__ = ("conn", "keep_alive", "started", "finished", "streaming",
                 "on_close")

    def __init__(self, conn, keep_alive):
        self.conn = conn
        self.keep_alive = keep_alive
        self.started = False
        self.finished = False
        self.streaming = False
        self.on_close = None

    # -- complete responses ------------------------------------------------

    def send(self, status, body=b"", content_type="text/plain; charset=utf-8",
             headers=None):
        if self.started:
            log.warning("second response attempted for one request")
            return
        if isinstance(body, str):
            body = body.encode("utf-8")

        fields = [("Content-Type", content_type),
                  ("Content-Length", str(len(body)))]
        fields.extend(headers or [])
        self._start(status, fields, close=not self.keep_alive)

        if body:
            self.conn.send(body)
        self.finished = True
        if not self.keep_alive:
            self.conn.close_when_flushed()

    def json(self, status, payload, headers=None):
        body = json.dumps(payload, separators=(",", ":"), default=str)
        self.send(status, body, "application/json; charset=utf-8", headers)

    def error(self, status, message=None):
        self.json(status, {"error": {
            "status": status,
            "message": message or STATUS_TEXT.get(status, "error")}})

    # -- streaming responses -----------------------------------------------

    def begin_stream(self, content_type, headers=None):
        """Open a response with no declared length, held until the peer leaves.

        Deliberately ``Connection: close`` rather than chunked: the body is
        framed by the event protocol itself, the socket is dedicated to this
        one response, and it keeps the framing trivially inspectable in a
        packet capture.
        """
        fields = [("Content-Type", content_type),
                  ("Cache-Control", "no-cache, no-store"),
                  # Without this a reverse proxy may sit on the events.
                  ("X-Accel-Buffering", "no")]
        fields.extend(headers or [])
        self.keep_alive = False
        self.streaming = True
        self._start(200, fields, close=True)

    def write(self, payload):
        if self.finished or self.conn.closed:
            return
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.conn.send(payload)

    def end(self):
        if self.finished:
            return
        self.finished = True
        self.conn.close_when_flushed()

    # -- framing -----------------------------------------------------------

    def _start(self, status, fields, close):
        self.started = True
        lines = ["HTTP/1.1 %d %s" % (status, STATUS_TEXT.get(status, "Unknown"))]
        for name, value in fields:
            lines.append("%s: %s" % (name, value))
        lines.append("Connection: %s" % ("close" if close else "keep-alive"))
        lines.append("")
        lines.append("")
        self.conn.send("\r\n".join(lines).encode("latin-1"))


class HttpConnection(object):
    """Request parsing for one client socket."""

    def __init__(self, conn, server):
        self.conn = conn
        self.server = server
        self.peer = "%s:%d" % conn.peer[:2]
        self._buffer = b""
        self._responder = None

    def on_data(self, _conn, chunk):
        self._buffer += chunk
        # A streaming response owns the socket; anything further is ignored
        # rather than parsed, so a stray byte cannot inject a second request.
        while self._responder is None or self._responder.finished:
            if not self._consume_one():
                return

    def on_close(self, _conn):
        responder = self._responder
        if responder is not None and responder.on_close is not None:
            try:
                responder.on_close()
            except Exception:
                log.exception("stream close handler failed for %s", self.peer)
        self.server.connections.discard(self)

    # -- parsing -----------------------------------------------------------

    def _consume_one(self):
        """Parse and dispatch one request. Returns False when more data is needed."""
        split = self._buffer.find(b"\r\n\r\n")
        if split < 0:
            if len(self._buffer) > MAX_HEADER_BYTES:
                self._refuse(431, "header block too large")
            return False

        head = self._buffer[:split]
        rest = self._buffer[split + 4:]

        try:
            method, target, version, headers = _parse_head(head)
        except ValueError as exc:
            self._refuse(400, str(exc))
            return False

        length = _content_length(headers)
        if length is None:
            self._refuse(400, "invalid Content-Length")
            return False
        if length > MAX_BODY_BYTES:
            self._refuse(413, "body too large")
            return False
        if len(rest) < length:
            return False  # body still arriving

        body, self._buffer = rest[:length], rest[length:]

        path, _, query = target.partition("?")
        request = Request(
            method=method,
            path=unquote(path),
            query=dict((key, values[0])
                       for key, values in parse_qs(query).items()),
            headers=headers,
            body=body,
            peer=self.peer)

        keep_alive = _keep_alive(version, headers)
        self._responder = Responder(self.conn, keep_alive)
        self.server.dispatch(request, self._responder)
        return True

    def _refuse(self, status, message):
        Responder(self.conn, keep_alive=False).error(status, message)
        self._buffer = b""
        self.conn.close_when_flushed()


class HttpServer(object):
    """Listens for browsers and hands parsed requests to ``handler``."""

    def __init__(self, reactor, handler):
        #: ``handler(request, responder)``; must reply or begin a stream.
        self.handler = handler
        self.reactor = reactor
        self.connections = set()
        self._listener = None

    @property
    def address(self):
        return self._listener.address if self._listener else None

    def start(self, host, port):
        self._listener = self.reactor.listen(host, port, self._on_accept)
        log.info("web UI on http://%s:%d",
                 self._listener.address[0], self._listener.address[1])
        return self._listener.address

    def stop(self):
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        for connection in list(self.connections):
            connection.conn.close()

    def dispatch(self, request, responder):
        try:
            self.handler(request, responder)
        except Exception:
            log.exception("handler failed for %s", request)
            if not responder.started:
                responder.error(500, "handler raised")

    def _on_accept(self, conn):
        connection = HttpConnection(conn, self)
        conn.on_data = connection.on_data
        conn.on_close = connection.on_close
        conn.data = connection
        self.connections.add(connection)


# -- head parsing ------------------------------------------------------------

def _parse_head(head):
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:
        raise ValueError("undecodable request head")

    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ValueError("malformed request line")
    method, target, version = parts
    if not target.startswith("/"):
        raise ValueError("request target must be an origin-form path")

    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise ValueError("malformed header line")
        headers[name.strip().lower()] = value.strip()

    return method.upper(), target, version.upper(), headers


def _content_length(headers):
    raw = headers.get("content-length")
    if raw is None:
        return 0
    try:
        length = int(raw)
    except ValueError:
        return None
    return length if length >= 0 else None


def _keep_alive(version, headers):
    token = headers.get("connection", "").lower()
    if version == "HTTP/1.0":
        return token == "keep-alive"
    return token != "close"


# -- static files ------------------------------------------------------------

def safe_join(root, path):
    """Resolve ``path`` under ``root``, or return None if it tries to escape.

    Traversal is *rejected* rather than silently absorbed. Anchoring the path
    at ``/`` before normalising would collapse ``../../etc`` to ``etc`` and
    quietly serve something else, which hides the attempt; normalising the
    relative path instead leaves a leading ``..`` visible to refuse.

    Separators are folded first so a Windows-style ``..\\x`` is caught too, and
    the resolved path is still checked against the root afterwards -- that is
    what stops an absolute path such as ``C:/x``, which ``os.path.join`` would
    otherwise let win outright.
    """
    normalised = posixpath.normpath(path.replace("\\", "/").lstrip("/"))
    if normalised == ".." or normalised.startswith("../"):
        return None

    root = os.path.normpath(root)
    resolved = os.path.normpath(os.path.join(root, normalised))
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    return resolved


def content_type_for(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(),
                             "application/octet-stream")


def serve_file(responder, root, path):
    """Send a file from ``root``, or the appropriate error."""
    resolved = safe_join(root, path)
    if resolved is None:
        responder.error(403, "path escapes the document root")
        return
    if not os.path.isfile(resolved):
        responder.error(404, "no such file")
        return
    try:
        with open(resolved, "rb") as handle:
            body = handle.read()
    except (IOError, OSError) as exc:
        responder.error(500, "cannot read %s: %s" % (path, exc))
        return
    responder.send(200, body, content_type_for(resolved))
