"""Routing for the board: a static page, a command proxy, and an event stream.

Two decisions shape this file.

*Every control command is automatically a web API.* ``POST /api/<venue>/<cmd>``
forwards its JSON body to that venue's control plane and returns the reply
verbatim. There is no per-command web code, so a command added for the CLI is
reachable from the browser the same day -- and the venue stays the only thing
that validates it.

*Fan-out reuses the control plane's own publisher.* An open Server-Sent Events
response is registered with :class:`~exchangesim.control.subscriptions.Publisher`
exactly as a control session is, because that class only requires a
``push(topic, data)`` method. Topics are namespaced ``<venue>/<topic>`` on the
way in, since one process here watches several venues.
"""

import json
import logging
import os

from ..control.subscriptions import Publisher
from .http import HttpServer, serve_file
from .link import VenueLink

log = logging.getLogger(__name__)

STATIC_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: Everything a venue publishes. The volume is trivial at simulator scale, and
#: subscribing once avoids tracking which patterns are still wanted.
ALL_TOPICS = "*"

#: Commands that place orders, refused when order entry is disabled.
ORDER_ENTRY_COMMANDS = frozenset(("order.new",))

#: Commands that move a market between phases. Gated separately from order
#: entry because they are a different kind of power: closing a market expires
#: every resting order on it, including other clients' -- so a board can be
#: made a trading terminal without also being an operator console, or the
#: reverse.
MARKET_CONTROL_COMMANDS = frozenset((
    "state.set", "state.clear", "stp.set",
    "auction.lock", "auction.reference",
))

#: Commands that read the message audit. Gated on their own, and not by
#: ``allow_order_entry``, because they are a third kind of power: the audit
#: shows every client's traffic -- ClOrdIDs, sizes, prices -- so a board that
#: may not place an order should not thereby be able to read everyone else's.
AUDIT_COMMANDS = frozenset(("audit", "audit.entry", "audit.types"))

#: Idle comment sent down each stream so intermediaries do not time it out.
KEEPALIVE_SECONDS = 20.0

#: How the board is laid out and coloured, when the config says nothing. Buy on
#: the right is where the depth board puts bids, and red-for-buying is the
#: Japanese convention the stylesheet is written around; both are defaults
#: rather than law, because a desk that reads the other way round should not
#: have to edit a stylesheet. This is presentation only -- no capability, and
#: no venue behaviour, turns on any of it.
DEFAULT_BOARD = {"buy_side": "right", "buy_colour": "red",
                 "sell_colour": "green"}


class EventStream(object):
    """One browser holding a Server-Sent Events response open.

    Duck-types a control session: :class:`Publisher` needs nothing but ``push``.
    """

    __slots__ = ("responder", "venue_key", "prefix")

    def __init__(self, responder, venue_key):
        self.responder = responder
        self.venue_key = venue_key
        self.prefix = venue_key + "/"

    def push(self, topic, data):
        if topic.startswith(self.prefix):
            topic = topic[len(self.prefix):]
        payload = json.dumps({"topic": topic, "data": data},
                             separators=(",", ":"), default=str)
        self.responder.write("data: %s\n\n" % payload)

    def comment(self, text):
        self.responder.write(": %s\n\n" % text)

    @property
    def closed(self):
        return self.responder.conn.closed


class WebApp(object):
    """The aggregating web UI over any number of venue control planes."""

    def __init__(self, reactor, venues=(), static_root=None,
                 allow_order_entry=True, allow_market_control=True,
                 allow_audit=True, board=None):
        self.reactor = reactor
        self.static_root = static_root or STATIC_ROOT
        self.allow_order_entry = allow_order_entry
        self.allow_market_control = allow_market_control
        self.allow_audit = allow_audit
        self.board = dict(DEFAULT_BOARD)
        self.board.update(board or {})

        self.publisher = Publisher()
        self.streams = set()
        self.links = {}
        self.order = []
        self.server = HttpServer(reactor, self.handle)
        self._keepalive = None

        for spec in venues:
            self.add_venue(**spec)

    # -- venues ------------------------------------------------------------

    def add_venue(self, key, host="127.0.0.1", port=9101, label=None,
                  token=None):
        link = VenueLink(self.reactor, key, host, port, label=label,
                         token=token, on_event=self._on_venue_event)
        self.links[key] = link
        self.order.append(key)
        return link

    def _on_venue_event(self, venue_key, topic, data):
        if not topic:
            return
        self.publisher.publish("%s/%s" % (venue_key, topic), data)

    # -- lifecycle ---------------------------------------------------------

    def start(self, host, port):
        address = self.server.start(host, port)
        for key in self.order:
            self.links[key].start()
        return address

    def stop(self):
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None
        for link in self.links.values():
            link.stop()
        self.server.stop()

    @property
    def address(self):
        return self.server.address

    def _schedule_keepalive(self):
        """Run a heartbeat only while someone is actually watching.

        It doubles as the reaper for a stream whose socket died without the
        reactor noticing, which is why it must survive an empty write.
        """
        def beat():
            self._keepalive = None
            for stream in list(self.streams):
                if stream.closed:
                    self._drop_stream(stream)
                else:
                    stream.comment("keepalive")
            if self.streams:
                self._schedule_keepalive()

        self._keepalive = self.reactor.call_later(KEEPALIVE_SECONDS, beat)

    # -- routing -----------------------------------------------------------

    def handle(self, request, responder):
        if request.method not in ("GET", "POST"):
            responder.error(405, "only GET and POST are supported")
            return

        path = request.path
        if path == "/" or path == "/index.html":
            serve_file(responder, self.static_root, "index.html")
            return
        if path.startswith("/static/"):
            serve_file(responder, self.static_root, path[len("/static/"):])
            return
        if path == "/api/venues":
            responder.json(200, {"venues": [self.links[key].describe()
                                            for key in self.order],
                                 "allow_order_entry": self.allow_order_entry,
                                 "allow_market_control":
                                     self.allow_market_control,
                                 "allow_audit": self.allow_audit,
                                 "board": self.board})
            return
        if path.startswith("/api/"):
            self._api(request, responder, path[len("/api/"):])
            return

        responder.error(404, "no such resource")

    def _api(self, request, responder, rest):
        venue_key, _, tail = rest.partition("/")
        link = self.links.get(venue_key)
        if link is None:
            responder.error(404, "unknown venue '%s'" % venue_key)
            return
        if not tail:
            responder.json(200, link.describe())
            return
        if tail == "events":
            self._open_stream(request, responder, link)
            return
        self._proxy(request, responder, link, tail)

    # -- command proxy -----------------------------------------------------

    def _proxy(self, request, responder, link, command):
        if request.method != "POST":
            responder.error(405, "commands are invoked with POST")
            return

        # Requiring a JSON content type is what stops a cross-site HTML form
        # from driving the simulator: a form can only send the three simple
        # types, none of which is application/json.
        content_type = (request.header("content-type") or "").split(";")[0].strip()
        if content_type != "application/json":
            responder.error(415, "Content-Type must be application/json")
            return

        if command in ORDER_ENTRY_COMMANDS and not self.allow_order_entry:
            responder.error(403, "order entry is disabled on this server")
            return

        if command in MARKET_CONTROL_COMMANDS and not self.allow_market_control:
            responder.error(403, "market control is disabled on this server")
            return

        if command in AUDIT_COMMANDS and not self.allow_audit:
            responder.error(403, "the audit view is disabled on this server")
            return

        try:
            args = request.json_body()
        except ValueError as exc:
            responder.error(400, "invalid JSON body: %s" % exc)
            return

        def reply(ok, payload):
            if responder.finished:
                return
            if ok:
                responder.json(200, {"ok": True, "result": payload})
            else:
                responder.json(_status_for(payload), {"ok": False, "error": payload})

        link.call(command, args, reply)

    # -- event stream ------------------------------------------------------

    def _open_stream(self, request, responder, link):
        if request.method != "GET":
            responder.error(405, "the event stream is a GET")
            return

        link.subscribe([ALL_TOPICS])
        responder.begin_stream("text/event-stream")

        stream = EventStream(responder, link.key)
        patterns = _patterns(request.query.get("topics"), link.key)
        self.publisher.subscribe(stream, patterns)
        self.streams.add(stream)
        responder.on_close = lambda: self._drop_stream(stream)
        if self._keepalive is None:
            self._schedule_keepalive()

        # Tell the browser how soon to redial, and flush the response head.
        responder.write("retry: 2000\n\n")
        stream.comment("connected to %s" % link.key)
        log.debug("event stream opened for '%s' from %s", link.key, request.peer)

    def _drop_stream(self, stream):
        self.publisher.remove(stream)
        self.streams.discard(stream)
        if not self.streams and self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None


def _patterns(raw, venue_key):
    """Turn ``?topics=book:*,trade:*`` into venue-namespaced patterns."""
    if not raw:
        return ["%s/*" % venue_key]
    return ["%s/%s" % (venue_key, part.strip())
            for part in raw.split(",") if part.strip()]


def _status_for(error):
    """Map a control-plane error code onto an HTTP status.

    The CLI already turns these codes into exit codes, so they are a stable
    part of the interface and safe to key on here.
    """
    return {
        "unknown_command": 404,
        "not_found": 404,
        "bad_args": 400,
        "bad_request": 400,
        "conflict": 409,
        "unauthorised": 403,
        "unavailable": 503,
        "timeout": 504,
    }.get((error or {}).get("code"), 500)
