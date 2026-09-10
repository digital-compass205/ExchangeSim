"""The whole NSE Futures & Options venue over fake sockets.

The counterpart of :mod:`tests.nsesupport`: real dictionary, real structures,
real engine and real books, a fake socket and a frozen clock. What is
different here is what a client names an order by -- not Symbol+Series, but
the five ``CONTRACT_DESC`` fields (instrument family, symbol, expiry, strike,
option type). The two contracts below match
``exchangesim/venues/nsefo/reference/contracts.csv`` exactly, so a test can
rely on their base price, tick and circuit filter without recomputing them.
"""

from exchangesim.control.commands import CommandRegistry
from exchangesim.control.server import CommandContext
from exchangesim.core.clock import FixedClock
from exchangesim.core.config import Config
from exchangesim.core.reactor import Reactor
from exchangesim.control.subscriptions import Publisher
from exchangesim.fix.message import Message
from exchangesim.nnf import packet as framing
from exchangesim.nnf.codec import NnfCodec
from exchangesim.venues.nsefo import dictionary as D
from exchangesim.venues.nsefo import layouts as LY
from exchangesim.venues.nsefo import transactions as X
from exchangesim.venues.nsefo.venue import NsefoVenue, _expiry_seconds

from tests.nnfsupport import BoxTransport

#: NIFTY-FUTIDX-24SEP2026: base 25400.00, tick 0.05, 10% band, lot 25.
FUTURE = {
    "instrument_name": D.InstrumentName.FUTURES_INDEX,
    "symbol": "NIFTY",
    "expiry": "2026-09-24",
    "strike": None,
    "option_type": D.OptionType.FUTURES,
}
FUTURE_CANONICAL = "NIFTY-FUTIDX-24SEP2026"

#: RELIANCE-OPTSTK-24SEP2026-2500-CE: base 45.20, tick 0.05, 10% band, lot 250.
OPTION = {
    "instrument_name": D.InstrumentName.OPTIONS_STOCK,
    "symbol": "RELIANCE",
    "expiry": "2026-09-24",
    "strike": "2500.00",
    "option_type": D.OptionType.CALL,
}
OPTION_CANONICAL = "RELIANCE-OPTSTK-24SEP2026-2500-CE"

BOX_ONE = 101
BOX_TWO = 102
BROKER_ONE = "10123"
BROKER_TWO = "10456"
USER_ONE = 50521
USER_TWO = 50522
USER_THREE = 50777
PASSWORD = "Nse@2026"


def venue_config(markets=None, boxes=None, **extra):
    """A venue config with both listeners on OS-chosen ports."""
    settings = {
        "venue": "nsefo",
        "name": "NSEFO-TEST",
        "audit": {"capacity": 500},
        "nnf": {
            "host": "127.0.0.1",
            "port": 0,
            "heartbeat_interval": 30,
            "boxes": boxes if boxes is not None else _default_boxes(),
        },
        "gateway_router": {"enabled": False},
        "markets": markets or [{"name": "NORMAL", "description": "Normal Market",
                                "state": "OPEN"}],
        "tape_length": 100,
    }
    settings.update(extra)
    return Config(settings)


def _default_boxes():
    return [
        {"box_id": BOX_ONE, "broker_ids": [BROKER_ONE], "users": [
            {"user_id": USER_ONE, "trader_name": "DEALER ONE",
             "broker_name": "SIM SECURITIES", "cancel_on_disconnect": True},
            {"user_id": USER_TWO, "trader_name": "DEALER TWO",
             "broker_name": "SIM SECURITIES"},
        ]},
        {"box_id": BOX_TWO, "broker_ids": [BROKER_TWO], "users": [
            {"user_id": USER_THREE, "trader_name": "DEALER THREE",
             "broker_name": "OTHER SECURITIES"},
        ]},
    ]


class BoxClient(object):
    """One member's connection, and the users it signs on over it."""

    def __init__(self, venue, box_id, broker_id):
        self.venue = venue
        self.box_id = box_id
        self.broker_id = broker_id
        self.box = _box(venue, box_id)
        self.transport = BoxTransport()
        self.codec = NnfCodec(venue.layouts, client=True)
        self.box.attach(self.transport)

    # -- raw traffic -------------------------------------------------------

    def send(self, code, fields=None, user_id=None):
        message = Message.create(str(code))
        message.set(D.ERROR_CODE, "0")
        if user_id is not None:
            # The header's identity tag -- what `_user_of()` resolves a
            # session by for every application message. Distinct from
            # SIGNON_USER_ID, which only means something on MS_SIGNON itself
            # and is set explicitly by sign_on() below.
            message.set(D.USER_ID, str(user_id))
        for tag, value in sorted((fields or {}).items()):
            message.set(tag, str(value))
        self.box.on_data(self.codec.encode(message))
        return message

    def received(self):
        framer = self.codec.framer()
        return [self.codec.decode(raw)
                for raw in framer.feed(self.transport.take())]

    def last(self):
        messages = self.received()
        return messages[-1] if messages else None

    def clear(self):
        self.transport.take()

    def reports(self):
        return [(int(message.msg_type), message) for message in self.received()]

    def raw(self, code, user_id=None):
        """Send a transaction code with no registered layout at all -- the
        Spread/2L/3L family (transcription §1.4 intro), which Phase 1 scoped
        out entirely. Only the forty-byte header can be built, which is
        exactly what ``NnfCodec.decode`` falls back to on the receiving end
        when it finds no layout for a code.
        """
        message = Message.create(str(code))
        message.set(D.ERROR_CODE, "0")
        if user_id is not None:
            message.set(D.USER_ID, str(user_id))
        message.set(D.MESSAGE_LENGTH, str(LY.HEADER.size))
        buffer = bytearray(LY.HEADER.size)
        for field in LY.HEADER.fields:
            field.encode(message, buffer)
        data, checksum = self.codec.cipher.seal(bytes(buffer))
        self.box.on_data(framing.pack(data, sequence=0, checksum=checksum))
        return message

    # -- the opening sequence ---------------------------------------------

    def open(self, *user_ids):
        self.send(X.SECURE_BOX_REGISTRATION_REQUEST_IN, {D.BOX_ID: self.box_id})
        self.send(X.BOX_SIGN_ON_REQUEST_IN,
                  {D.BOX_ID: self.box_id, D.BROKER_ID: self.broker_id})
        for user_id in (user_ids or ()):
            self.sign_on(user_id)
        self.clear()
        return self

    def sign_on(self, user_id):
        self.send(X.SIGN_ON_REQUEST_IN,
                  {D.SIGNON_USER_ID: user_id, D.PASSWORD: PASSWORD,
                   D.BROKER_ID: self.broker_id},
                  user_id=user_id)
        return self

    # -- order entry -------------------------------------------------------

    def _contract_fields(self, contract):
        fields = {
            D.INSTRUMENT_NAME: contract["instrument_name"],
            D.SYMBOL: contract["symbol"],
            D.EXPIRY_DATE: _expiry_seconds(contract["expiry"]),
            D.OPTION_TYPE: contract["option_type"],
        }
        fields[D.STRIKE_PRICE] = contract["strike"] \
            if contract["strike"] is not None else "-1"
        return fields

    def new_order(self, user_id, side=D.BuySell.BUY, quantity=25,
                  price="25400.00", contract=None, extra=None):
        """Enter one order. ``extra`` overrides or adds fields, keyed by tag."""
        contract = contract or FUTURE
        fields = self._contract_fields(contract)
        fields.update({
            D.BOOK_TYPE: D.BookType.REGULAR_LOT,
            D.BUY_SELL: side,
            D.VOLUME: quantity,
            D.PRO_CLIENT: D.ProClient.CLIENT,
            D.ACCOUNT_NUMBER: "CLIENT%d" % user_id,
            D.BROKER_ID: self.broker_id,
            D.FLAG_DAY: "Y",
        })
        if price is not None:
            fields[D.PRICE] = price
        fields.update(extra or {})
        return self.send(X.BOARD_LOT_IN, fields, user_id=user_id)

    def cancel(self, user_id, order_number):
        return self.send(X.ORDER_CANCEL_IN, {
            D.ORDER_NUMBER: order_number,
            D.BOOK_TYPE: D.BookType.REGULAR_LOT,
        }, user_id=user_id)

    def modify(self, user_id, order_number, quantity=None, price=None):
        fields = {
            D.ORDER_NUMBER: order_number,
            D.BOOK_TYPE: D.BookType.REGULAR_LOT,
            D.FLAG_DAY: "Y",
        }
        if quantity is not None:
            fields[D.VOLUME] = quantity
        if price is not None:
            fields[D.PRICE] = price
        return self.send(X.ORDER_MOD_IN, fields, user_id=user_id)

    # -- the trimmed order flow -------------------------------------------

    def trimmed_order(self, user_id, side=D.BuySell.BUY, quantity=25,
                      price="25400.00", contract=None, extra=None):
        """The same order over ``MS_OE_REQUEST_TR``.

        Deliberately built from the same fields as :meth:`new_order`: the two
        encodings carry the same values under the same tags, and everything
        that differs -- the header, the offsets, the widths -- belongs to the
        layout. A helper that spelled them differently would hide exactly the
        bug this exists to catch.
        """
        contract = contract or FUTURE
        fields = self._contract_fields(contract)
        fields.update({
            D.BOOK_TYPE: D.BookType.REGULAR_LOT,
            D.BUY_SELL: side,
            D.VOLUME: quantity,
            D.PRO_CLIENT: D.ProClient.CLIENT,
            D.ACCOUNT_NUMBER: "CLIENT%d" % user_id,
            D.BROKER_ID: self.broker_id,
            D.FLAG_DAY: "Y",
        })
        if price is not None:
            fields[D.PRICE] = price
        fields.update(extra or {})
        return self.send(X.BOARD_LOT_IN_TR, fields, user_id=user_id)

    def trimmed_cancel(self, user_id, order_number):
        return self.send(X.ORDER_CANCEL_IN_TR, {
            D.ORDER_NUMBER: order_number,
            D.BOOK_TYPE: D.BookType.REGULAR_LOT,
        }, user_id=user_id)

    def trimmed_modify(self, user_id, order_number, quantity=None,
                       price=None):
        fields = {
            D.ORDER_NUMBER: order_number,
            D.BOOK_TYPE: D.BookType.REGULAR_LOT,
            D.FLAG_DAY: "Y",
        }
        if quantity is not None:
            fields[D.VOLUME] = quantity
        if price is not None:
            fields[D.PRICE] = price
        return self.send(X.ORDER_MOD_IN_TR, fields, user_id=user_id)


class VenueHarness(object):
    """A started F&O venue, its control registry, and clients for its boxes."""

    def __init__(self, config=None):
        self.clock = FixedClock()
        self.reactor = Reactor(clock=self.clock)
        self.publisher = Publisher()
        self.venue = NsefoVenue(config or venue_config(), self.reactor,
                                self.publisher)
        self.venue.start()
        self.registry = CommandRegistry()
        self.venue.register_commands(self.registry)
        self.registry.audit = self.venue.audit
        self._clients = {}

    # -- clients -----------------------------------------------------------

    def client(self, box_id=BOX_ONE, broker_id=None, users=(USER_ONE,)):
        if box_id not in self._clients:
            self._clients[box_id] = BoxClient(
                self.venue, box_id,
                broker_id or (BROKER_ONE if box_id == BOX_ONE else BROKER_TWO))
            if users:
                self._clients[box_id].open(*users)
        return self._clients[box_id]

    # -- control plane -----------------------------------------------------

    def dispatch(self, name, args=None):
        context = CommandContext(self.venue, None, _StubServer(self))
        return self.registry.dispatch(name, context, args or {})

    def command(self, name, **args):
        return self.dispatch(name, args)

    def set_state(self, state, market="NORMAL"):
        return self.command("state.set", market=market, state=state)

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self.venue.stop()
        self.reactor.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _StubServer(object):
    """Minimal stand-in for ControlServer when dispatching commands directly."""

    def __init__(self, harness):
        self.registry = harness.registry
        self.publisher = harness.publisher
        self.token = None

    @property
    def requires_auth(self):
        return False


def _box(venue, box_id):
    for box in venue.manager.boxes:
        if box.box_id == box_id:
            return box
    raise KeyError(box_id)


def field(message, tag):
    return message.get(tag) if message is not None else None


def codes(reports):
    """Just the transaction codes of a ``reports()`` list, for a readable assert."""
    return [code for code, _message in reports]
