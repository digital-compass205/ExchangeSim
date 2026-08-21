"""Harness for driving the HKEX venue end to end, without sockets.

Builds the real venue -- real reference data, real dictionary, real engine --
then attaches fake transports to its configured sessions. Everything from the
FIX bytes inward is the production path; only the socket is replaced.

The client helpers below build the repeating groups the dialect requires, which
is most of what distinguishes an OCG-C message from a Japannext one: a
``<Parties>`` block on everything and a ``<DisclosureInstructionGrp>`` on the
order messages.
"""

from exchangesim.control.commands import CommandRegistry
from exchangesim.control.server import CommandContext
from exchangesim.control.subscriptions import Publisher
from exchangesim.core.clock import FixedClock
from exchangesim.core.config import Config
from exchangesim.core.reactor import Reactor
from exchangesim.fix import constants as C
from exchangesim.fix.codec import FixCodec
from exchangesim.fix.message import Message
from exchangesim.venues.hkex import binary as binary_layouts
from exchangesim.venues.hkex import dictionary as D
from exchangesim.binary.codec import BinaryCodec
from exchangesim.venues.hkex.venue import HkexVenue

from .fixsupport import FakeTransport

SERVER = "HKEXSIM"
BROKER1 = "BROKER1"
BROKER2 = "BROKER2"
#: Registered for the binary encoding of the same protocol; see
#: :func:`binary_venue_config`.
BROKER3 = "BROKER3"

#: Broker Numbers, as PartyID(448) with PartyRole(452)=1.
BROKER1_ID = "1001"
BROKER2_ID = "1002"
BROKER3_ID = "1003"

#: A Main Board security from the shipped universe: base 395.800, lot 100,
#: spread 0.200 above 200, so the band is 24 x 0.2 = 4.8 -> 391.000 to 400.600.
SYMBOL = "00700"

#: A GEM security, on its own book: base 0.235, lot 2000, spread 0.005.
GEM_SYMBOL = "08083"

BEGIN_STRING = "FIXT.1.1"


def venue_config(markets=None, sessions=None, **extra):
    data = {
        "venue": "hkex",
        "name": "TEST-HKEX",
        "fix": {
            "host": "127.0.0.1",
            # Port 0 lets the OS choose, so parallel test runs cannot collide.
            "port": 0,
            "sender_comp_id": SERVER,
            "sessions": sessions or [
                {"target_comp_id": BROKER1, "markets": ["MAIN", "GEM"],
                 "broker_ids": [BROKER1_ID], "cancel_on_disconnect": True},
                {"target_comp_id": BROKER2, "markets": ["MAIN", "GEM"],
                 "broker_ids": [BROKER2_ID], "cancel_on_disconnect": False},
            ],
        },
        "markets": markets or [
            {"name": "MAIN", "state": "OPEN"},
            {"name": "GEM", "state": "OPEN"},
        ],
        "smp": [
            {"id": "SMPAGG", "instruction": "CANCEL_AGGRESSIVE"},
            {"id": "SMPPAS", "instruction": "CANCEL_PASSIVE"},
        ],
    }
    data.update(extra)
    return Config(data)


class ClientSession(object):
    """One connected broker: sends FIX in, collects FIX out."""

    def __init__(self, session, clock, broker_id=BROKER1_ID, codec=None):
        self.session = session
        self.clock = clock
        self.broker_id = broker_id
        #: The client half of the session's encoding. Given one, this same
        #: harness drives a binary client: only the bytes on the fake wire
        #: change, and every message helper below is shared.
        self.codec = codec or FixCodec(BEGIN_STRING)
        self.transport = FakeTransport()
        self._framer = self.codec.framer()
        self.seq = 1
        #: What we next expect from the venue, for NextExpectedMsgSeqNum(789).
        self.next_expected = 1
        session.attach(self.transport)

    # -- lifecycle ---------------------------------------------------------

    def logon(self, next_expected=None, password="0Ru2xMk=", **fields):
        message = Message.create(C.LOGON)
        if self.codec.name == "fix":
            # The binary Logon has neither field: see SessionConfig.
            message.set(C.ENCRYPT_METHOD, "0")
            message.set(C.HEART_BT_INT, 20)
        message.set(C.NEXT_EXPECTED_MSG_SEQ_NUM,
                    self.next_expected if next_expected is None else next_expected)
        if self.codec.name == "fix":
            message.set(C.DEFAULT_APPL_VER_ID, C.ApplVerID.FIX50SP2)
        if password is not None:
            message.set(C.ENCRYPTED_PASSWORD_METHOD, 101)
            message.set(C.ENCRYPTED_PASSWORD, password)
        for tag, value in fields.items():
            message.set(int(tag.lstrip("t")), value)
        self.send(message)
        return self

    def disconnect(self, reason="connection lost"):
        self.session.detach(reason)

    # -- sending -----------------------------------------------------------

    def send(self, message):
        message.set(C.MSG_SEQ_NUM, self.seq)
        self.seq += 1
        message.set(C.SENDER_COMP_ID, self.session.target_comp_id)
        message.set(C.TARGET_COMP_ID, self.session.sender_comp_id)
        message.set(C.SENDING_TIME, self.clock.timestamp())
        self.session.on_data(self.codec.encode(message))
        return message

    # -- repeating groups --------------------------------------------------

    def _parties(self, message, bcan=None, location=None):
        entries = [(self.broker_id, D.PartyRole.EXECUTING_FIRM)]
        if bcan is not None:
            entries.append((bcan, D.PartyRole.CLIENT_ID))
        if location is not None:
            entries.append((location, D.PartyRole.LOCATION_ID))
        message.set(D.NO_PARTY_IDS, len(entries))
        for party_id, role in entries:
            message.append(D.PARTY_ID, party_id)
            message.append(D.PARTY_ID_SOURCE, D.PartyIDSource.PROPRIETARY)
            message.append(D.PARTY_ROLE, role)

    def _instrument(self, message, symbol):
        message.set(D.SECURITY_ID, symbol)
        message.set(D.SECURITY_ID_SOURCE, D.SecurityIDSource.EXCHANGE_SYMBOL)
        message.set(D.SECURITY_EXCHANGE, D.SECURITY_EXCHANGE_VALUE)

    def _disclosure(self, message):
        message.set(D.NO_DISCLOSURE_INSTRUCTIONS, 1)
        message.append(D.DISCLOSURE_TYPE, D.DisclosureType.NONE)
        message.append(D.DISCLOSURE_INSTRUCTION, D.DisclosureInstruction.YES)

    # -- business messages -------------------------------------------------

    def new_order(self, cl_ord_id, side=D.SideValue.BUY, quantity=100,
                  price="395.800", symbol=SYMBOL, tif=None, ord_type=None,
                  smp_id=None, exec_inst=None, capacity=None, bcan=None,
                  lot_type=None):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(D.CL_ORD_ID, cl_ord_id)
        self._parties(message, bcan=bcan)
        self._instrument(message, symbol)
        message.set(D.ORD_TYPE, ord_type or D.OrdType.LIMIT)
        message.set(D.SIDE, side)
        message.set(D.ORDER_QTY, quantity)
        if price is not None:
            message.set(D.PRICE, price)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        message.set_if(D.TIME_IN_FORCE, tif)
        message.set_if(D.SELF_MATCH_PREVENTION_ID, smp_id)
        message.set_if(D.EXEC_INST, exec_inst)
        message.set_if(D.ORDER_CAPACITY, capacity)
        message.set_if(D.LOT_TYPE, lot_type)
        self._disclosure(message)
        return self.send(message)

    def cancel(self, cl_ord_id, orig_cl_ord_id, side=D.SideValue.BUY,
               quantity=100, symbol=SYMBOL):
        message = Message.create(C.ORDER_CANCEL_REQUEST)
        message.set(D.CL_ORD_ID, cl_ord_id)
        message.set(D.ORIG_CL_ORD_ID, orig_cl_ord_id)
        self._parties(message)
        self._instrument(message, symbol)
        message.set(D.ORDER_QTY, quantity)
        message.set(D.SIDE, side)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        return self.send(message)

    def replace(self, cl_ord_id, orig_cl_ord_id, quantity=100,
                price="395.800", side=D.SideValue.BUY, symbol=SYMBOL, tif=None):
        message = Message.create(C.ORDER_CANCEL_REPLACE_REQUEST)
        message.set(D.CL_ORD_ID, cl_ord_id)
        message.set(D.ORIG_CL_ORD_ID, orig_cl_ord_id)
        self._parties(message)
        self._instrument(message, symbol)
        message.set(D.ORD_TYPE, D.OrdType.LIMIT)
        message.set(D.SIDE, side)
        message.set(D.ORDER_QTY, quantity)
        if price is not None:
            message.set(D.PRICE, price)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        message.set_if(D.TIME_IN_FORCE, tif)
        self._disclosure(message)
        return self.send(message)

    def mass_cancel(self, cl_ord_id, scope=D.MassCancelRequestType.ALL,
                    symbol=None, segment=None, side=None):
        message = Message.create(D.ORDER_MASS_CANCEL_REQUEST)
        message.set(D.CL_ORD_ID, cl_ord_id)
        message.set(D.MASS_CANCEL_REQUEST_TYPE, scope)
        self._parties(message)
        if symbol is not None:
            self._instrument(message, symbol)
        message.set_if(D.MARKET_SEGMENT_ID, segment)
        message.set_if(D.SIDE, side)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        return self.send(message)

    # -- receiving ---------------------------------------------------------

    def received(self):
        """Decode and clear everything the venue has sent to this client."""
        messages = [self.codec.decode(raw)
                    for raw in self._framer.feed(self.transport.take())]
        for message in messages:
            if message.seq_num is not None:
                self.next_expected = max(self.next_expected, message.seq_num + 1)
        return messages

    def reports(self):
        """Only ExecutionReports, which is what most assertions want."""
        return [message for message in self.received()
                if message.msg_type == C.EXECUTION_REPORT]

    def drain(self):
        self.received()
        return self


def binary_venue_config(**extra):
    """A venue whose third session speaks the binary encoding.

    The first two stay on FIX, so one harness exercises both encodings against
    one set of books -- which is the arrangement the config file ships and the
    one worth having tests over.
    """
    return venue_config(
        sessions=[
            {"target_comp_id": BROKER1, "protocol": "fix",
             "markets": ["MAIN", "GEM"], "broker_ids": [BROKER1_ID],
             "cancel_on_disconnect": True},
            {"target_comp_id": BROKER2, "protocol": "fix",
             "markets": ["MAIN", "GEM"], "broker_ids": [BROKER2_ID],
             "cancel_on_disconnect": False},
            {"target_comp_id": BROKER3, "protocol": "binary",
             "markets": ["MAIN", "GEM"], "broker_ids": [BROKER3_ID],
             "cancel_on_disconnect": True},
        ],
        binary={"host": "127.0.0.1", "port": 0},
        **extra)


class VenueHarness(object):
    """A running HKEX venue with two attached broker sessions."""

    def __init__(self, config=None):
        self.clock = FixedClock()
        self.reactor = Reactor(self.clock)
        self.publisher = Publisher()
        self.venue = HkexVenue(
            config or venue_config(), self.reactor, self.publisher)
        self.venue.start()

        self.registry = CommandRegistry()
        self.venue.register_commands(self.registry)
        # As the runner wires it, so a control command dispatched here lands on
        # the same audit tape as the venue's FIX traffic.
        self.registry.audit = self.venue.audit

        self.clients = {}

    # -- clients -----------------------------------------------------------

    def client(self, target_comp_id=BROKER1, logon=True):
        existing = self.clients.get(target_comp_id)
        if existing is not None:
            return existing
        session = self.venue.manager.get(SERVER, target_comp_id)
        broker_id = _BROKER_IDS.get(target_comp_id, BROKER1_ID)
        codec = None
        if session.wire == "binary":
            # The client half of the venue's own codec: same layouts, mirrored
            # Comp ID handling, because the wire carries only the client's.
            codec = BinaryCodec(binary_layouts.build(session.dictionary),
                                SERVER, client=True)
        client = ClientSession(session, self.clock, broker_id, codec=codec)
        self.clients[target_comp_id] = client
        if logon:
            client.logon().drain()
        return client

    # -- control plane -----------------------------------------------------

    def dispatch(self, name, args):
        context = CommandContext(self.venue, None, _StubServer(self.registry))
        return self.registry.dispatch(name, context, args)

    def command(self, name, **args):
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


#: Which Broker Number each configured session submits under.
_BROKER_IDS = {BROKER1: BROKER1_ID, BROKER2: BROKER2_ID, BROKER3: BROKER3_ID}


def tags(message, *wanted):
    """Extract a dict of the named tags, for compact assertions."""
    return dict((tag, message.get(tag)) for tag in wanted)


def party(message, role):
    """The PartyID(448) carrying a given PartyRole(452), or None."""
    ids = message.get_all(D.PARTY_ID)
    roles = message.get_all(D.PARTY_ROLE)
    for party_id, party_role in zip(ids, roles):
        if party_role == role:
            return party_id
    return None
