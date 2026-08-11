"""Harness for driving the Japannext venue end to end, without sockets.

Builds the real venue -- real reference data, real dictionary, real engine --
then attaches fake transports to its configured sessions. Everything from the
FIX bytes inward is the production path; only the socket is replaced.
"""

from exchangesim.control.commands import CommandRegistry
from exchangesim.control.server import CommandContext
from exchangesim.control.subscriptions import Publisher
from exchangesim.core.clock import FixedClock
from exchangesim.core.config import Config
from exchangesim.core.reactor import Reactor
from exchangesim.fix import constants as C
from exchangesim.fix.message import Framer, Message, decode, encode
from exchangesim.venues.japannext import dictionary as D
from exchangesim.venues.japannext.venue import JapannextVenue

from .fixsupport import FakeTransport

SERVER = "JNXSIM"
CLIENT1 = "CLIENT1"
CLIENT2 = "CLIENT2"

#: A symbol from the shipped universe: base 2845.5, band 2345.5-3345.5,
#: lot 100, tick 0.1, 16,000,000 shares outstanding so the 5% cap is 800,000.
SYMBOL = "7203"


def venue_config(markets=None, sessions=None, **extra):
    data = {
        "venue": "japannext",
        "name": "TEST-JNX",
        "fix": {
            "host": "127.0.0.1",
            # Port 0 lets the OS choose, so parallel test runs cannot collide.
            "port": 0,
            "sender_comp_id": SERVER,
            "sessions": sessions or [
                {"target_comp_id": CLIENT1, "default_sub_id": "DAY",
                 "markets": ["DAY", "NGHT"], "cancel_on_disconnect": True},
                {"target_comp_id": CLIENT2, "default_sub_id": "DAY",
                 "markets": ["DAY", "NGHT"], "cancel_on_disconnect": False},
            ],
        },
        "markets": markets or [
            {"name": "DAY", "state": "OPEN", "stp_mode": "CANCEL_NEWEST"},
            {"name": "NGHT", "state": "CLOSED", "stp_mode": "CANCEL_NEWEST"},
        ],
    }
    data.update(extra)
    return Config(data)


class ClientSession(object):
    """One connected client: sends FIX in, collects FIX out."""

    def __init__(self, session, clock, sub_id="DAY"):
        self.session = session
        self.clock = clock
        self.sub_id = sub_id
        self.transport = FakeTransport()
        self._framer = Framer()
        self.seq = 1
        session.attach(self.transport)

    # -- lifecycle ---------------------------------------------------------

    def logon(self, reset=False):
        message = Message.create(C.LOGON)
        message.set(C.ENCRYPT_METHOD, "0")
        message.set(C.HEART_BT_INT, 30)
        if reset:
            message.set(C.RESET_SEQ_NUM_FLAG, C.YES)
        self.send(message)
        return self

    def disconnect(self, reason="connection lost"):
        self.session.detach(reason)

    # -- sending -----------------------------------------------------------

    def send(self, message, sub_id=None):
        message.set(C.MSG_SEQ_NUM, self.seq)
        self.seq += 1
        message.set(C.SENDER_COMP_ID, self.session.target_comp_id)
        message.set(C.TARGET_COMP_ID, self.session.sender_comp_id)
        message.set(C.SENDING_TIME, self.clock.timestamp())
        target_sub = sub_id if sub_id is not None else self.sub_id
        if target_sub:
            message.set(C.TARGET_SUB_ID, target_sub)
        self.session.on_data(encode(message))
        return message

    def new_order(self, cl_ord_id, side=D.SideValue.BUY, quantity=100,
                  price="2845.5", symbol=SYMBOL, tif=None, min_qty=None,
                  exec_inst=None, mpid=None, account=None, ord_type=None,
                  capacity=None, sub_id=None):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(D.CL_ORD_ID, cl_ord_id)
        message.set(D.SYMBOL, symbol)
        message.set(D.SIDE, side)
        message.set(D.ORDER_QTY, quantity)
        message.set(D.ORD_TYPE, ord_type or D.OrdType.LIMIT)
        if price is not None:
            message.set(D.PRICE, price)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        message.set_if(D.TIME_IN_FORCE, tif)
        message.set_if(D.MIN_QTY, min_qty)
        message.set_if(D.EXEC_INST, exec_inst)
        message.set_if(D.CLIENT_ID, mpid)
        message.set_if(D.ACCOUNT, account)
        message.set_if(D.RULE_80A, capacity)
        return self.send(message, sub_id)

    def cancel(self, cl_ord_id, orig_cl_ord_id, side=D.SideValue.BUY,
               quantity=100, symbol=SYMBOL, sub_id=None):
        message = Message.create(C.ORDER_CANCEL_REQUEST)
        message.set(D.CL_ORD_ID, cl_ord_id)
        message.set(D.ORIG_CL_ORD_ID, orig_cl_ord_id)
        message.set(D.SYMBOL, symbol)
        message.set(D.SIDE, side)
        message.set(D.ORDER_QTY, quantity)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        return self.send(message, sub_id)

    def replace(self, cl_ord_id, orig_cl_ord_id, quantity=100, price="2845.5",
                side=D.SideValue.BUY, symbol=SYMBOL, tif=None, sub_id=None):
        message = Message.create(C.ORDER_CANCEL_REPLACE_REQUEST)
        message.set(D.CL_ORD_ID, cl_ord_id)
        message.set(D.ORIG_CL_ORD_ID, orig_cl_ord_id)
        message.set(D.SYMBOL, symbol)
        message.set(D.SIDE, side)
        message.set(D.ORDER_QTY, quantity)
        message.set(D.ORD_TYPE, D.OrdType.LIMIT)
        message.set(D.PRICE, price)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        message.set_if(D.TIME_IN_FORCE, tif)
        return self.send(message, sub_id)

    # -- receiving ---------------------------------------------------------

    def received(self):
        """Decode and clear everything the venue has sent to this client."""
        return [decode(raw) for raw in self._framer.feed(self.transport.take())]

    def reports(self):
        """Only ExecutionReports, which is what most assertions want."""
        return [message for message in self.received()
                if message.msg_type == C.EXECUTION_REPORT]

    def drain(self):
        self.received()
        return self


class VenueHarness(object):
    """A running Japannext venue with two attached clients."""

    def __init__(self, config=None):
        self.clock = FixedClock()
        self.reactor = Reactor(self.clock)
        self.publisher = Publisher()
        self.venue = JapannextVenue(
            config or venue_config(), self.reactor, self.publisher)
        self.venue.start()

        self.registry = CommandRegistry()
        self.venue.register_commands(self.registry)
        # As the runner wires it, so a control command dispatched here lands on
        # the same audit tape as the venue's FIX traffic.
        self.registry.audit = self.venue.audit

        self.clients = {}

    # -- clients -----------------------------------------------------------

    def client(self, target_comp_id=CLIENT1, sub_id="DAY", logon=True):
        existing = self.clients.get(target_comp_id)
        if existing is not None:
            return existing
        session = self.venue.manager.get(SERVER, target_comp_id)
        client = ClientSession(session, self.clock, sub_id)
        self.clients[target_comp_id] = client
        if logon:
            client.logon().drain()
        return client

    # -- control plane -----------------------------------------------------

    def dispatch(self, name, args):
        """Invoke a control command with an explicit argument dict.

        Needed wherever an argument is itself called ``name``, which the
        keyword form below cannot express.
        """
        context = CommandContext(self.venue, None, _StubServer(self.registry))
        return self.registry.dispatch(name, context, args)

    def command(self, name, **args):
        """Invoke a control command as the CLI would."""
        return self.dispatch(name, args)

    # -- teardown ----------------------------------------------------------

    def close(self):
        self.venue.stop()
        self.reactor.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


class _StubServer(object):
    """Minimal stand-in for ControlServer when dispatching commands directly."""

    def __init__(self, registry):
        self.registry = registry
        self.publisher = Publisher()
        self.token = None

    @property
    def requires_auth(self):
        return False


def tags(message, *wanted):
    """Extract a dict of the named tags, for compact assertions."""
    return dict((tag, message.get(tag)) for tag in wanted)
