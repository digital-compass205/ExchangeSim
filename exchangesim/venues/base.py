"""Base class every venue module extends.

A venue owns its markets, gateways and reference data. The runner constructs
exactly one per process and hands it the shared reactor, clock and publisher.
"""

import logging

from ..audit import Audit, DEFAULT_CAPACITY, MAX_CAPACITY
from ..core.config import ConfigError

log = logging.getLogger(__name__)


class Venue(object):
    """Lifecycle and wiring contract for a simulated exchange.

    Subclasses override :meth:`setup` to build markets and gateways,
    :meth:`register_commands` to expose venue-specific control commands, and
    :meth:`teardown` to release listeners.
    """

    #: Short identifier used in config files and the venue registry.
    key = "base"

    #: Wire codecs by protocol name, filled by :meth:`setup`. Anything that has
    #: to read a recorded message back -- the audit view -- looks the codec up
    #: here rather than assuming tag=value FIX, because HKEX serves the same
    #: protocol in two encodings and an entry knows which one recorded it.
    codecs = {}

    def __init__(self, config, reactor, publisher):
        self.config = config
        self.reactor = reactor
        self.clock = reactor.clock
        self.publisher = publisher
        self.name = config.get("name", self.key)
        #: Message and command recorder. Built here, not per venue, so every
        #: venue has one and ``--check`` validates the capacity: the runner
        #: constructs the venue before it decides whether to run.
        self.audit = _build_audit(config, self.clock)
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self._started:
            return
        self.setup()
        self._started = True
        log.info("venue '%s' (%s) started", self.name, self.key)

    def stop(self):
        if not self._started:
            return
        self.teardown()
        self._started = False
        log.info("venue '%s' stopped", self.name)

    @property
    def started(self):
        return self._started

    # -- extension points --------------------------------------------------

    def setup(self):
        """Build markets, instruments and protocol gateways."""

    def teardown(self):
        """Close listeners and flush any persistent state."""

    def register_commands(self, registry):
        """Add venue-specific commands to the control registry."""

    # -- reference data ----------------------------------------------------
    #
    # A venue knows which tables it loaded and how they combine; the control
    # plane only knows that it asked. Venues that can rebuild their universe
    # from disk override these two.

    def reload_reference_data(self):
        """Re-read reference data from disk, returning a description of the diff."""
        raise NotImplementedError(
            "venue '%s' cannot reload reference data" % self.key)

    def create_instrument(self, symbol, name="", lot_size=100, base_price=None,
                          tier=None, shares_outstanding=0, tradable=True):
        """Build one instrument with this venue's band and tick tables attached."""
        raise NotImplementedError(
            "venue '%s' cannot create instruments at runtime" % self.key)

    def resolve_symbol(self, value):
        """This venue's own spelling of a symbol a client sent, or None.

        Most venues have exactly one spelling of a symbol and this is a lookup.
        A venue whose codes are numbers, written with or without padding,
        overrides it -- see :meth:`HkexVenue.resolve_symbol`. Every surface
        goes through here, wire and control plane alike, so what names a
        security cannot depend on which door it arrived at.
        """
        if not value:
            return None
        return value if value in self.instruments else None

    def books_for(self, instrument):
        """The markets that should carry this instrument's book.

        Every market by default, because most venues run the same universe on
        each. A venue where a security belongs to exactly one segment -- HKEX
        Main Board against GEM -- narrows it, so that admitting an instrument at
        runtime places it where the wire protocol would.
        """
        return list(self.markets.values())

    def wire_codec(self, protocol=None):
        """The codec that produced a recorded message, or the venue's only one.

        Falls back rather than raising: an audit entry recorded before a venue
        named its codecs, or by a venue that has none, is still worth showing.
        """
        codecs = self.codecs or {}
        if protocol and protocol in codecs:
            return codecs[protocol]
        if len(codecs) == 1:
            # A venue that speaks one protocol, whatever it is called. Falling
            # straight through to "fix" would answer None at a venue that has
            # no FIX encoding at all, and every audit entry would go unrendered.
            return list(codecs.values())[0]
        return codecs.get("fix")

    def dictionary_for(self, protocol=None):
        """The dialect a recorded message should be read against.

        Only differs from ``self.dictionary`` at a venue whose two encodings
        table the same messages differently.
        """
        return getattr(self, "dictionary", None)

    def describe(self):
        """Summary returned by the ``venue.info`` control command."""
        return {"venue": self.key, "name": self.name, "started": self._started}

    # -- publishing --------------------------------------------------------

    def publish(self, topic, data):
        """Emit a control-plane event, namespaced by nothing -- topics are global
        within a process because one process serves exactly one venue."""
        self.publisher.publish(topic, data)


def _build_audit(config, clock):
    """The venue's recorder, or None when ``audit.capacity`` is zero.

    None rather than a zero-length ring: every recording site is then a single
    ``is not None`` test, and switching the audit off costs nothing at all on
    the path every message takes.
    """
    capacity = config.get("audit.capacity", DEFAULT_CAPACITY)
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        raise ConfigError("'audit.capacity' must be an integer")
    if capacity < 0 or capacity > MAX_CAPACITY:
        raise ConfigError("'audit.capacity' must be between 0 and %d"
                          % MAX_CAPACITY)
    return Audit(capacity, clock) if capacity else None
