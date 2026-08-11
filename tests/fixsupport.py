"""Harness for driving a FIX session synchronously.

No sockets and no threads: a fake transport collects outbound bytes and the
test feeds inbound bytes directly, so every conformance test is deterministic
and runs in microseconds.
"""

from exchangesim.core.clock import FixedClock
from exchangesim.fix import constants as C
from exchangesim.fix.dictionary import FieldDef, MessageDef
from exchangesim.fix.message import Framer, Message, decode, encode
from exchangesim.fix.session import Application, Session, SessionConfig
from exchangesim.fix.standard import build_session_dictionary
from exchangesim.fix.store import MemoryStore

SERVER = "JNXSIM"
CLIENT = "CLIENT1"


def test_dictionary():
    """Session layer plus one minimal application message, for routing tests."""
    T = C.FieldType
    fields = [
        FieldDef(11, "ClOrdID", T.STRING, max_length=32),
        FieldDef(38, "OrderQty", T.QTY, max_digits=9),
        FieldDef(40, "OrdType", T.CHAR, values=("2",)),
        FieldDef(44, "Price", T.PRICE, max_digits=8, max_decimals=1),
        FieldDef(54, "Side", T.CHAR, values=("1", "2")),
        FieldDef(55, "Symbol", T.STRING, max_length=9),
        FieldDef(60, "TransactTime", T.UTC_TIMESTAMP),
    ]
    messages = [
        MessageDef(C.NEW_ORDER_SINGLE, "NewOrderSingle",
                   required=(11, 54, 55, 40, 60),
                   optional=(38, 44)),
    ]
    return build_session_dictionary(fields=fields, messages=messages)


class FakeTransport(object):
    """Stands in for a reactor Connection, collecting what the session sends."""

    def __init__(self):
        self.buffer = b""
        self.closed = False

    def send(self, data):
        if not self.closed:
            self.buffer += data

    def close(self):
        self.closed = True

    def take(self):
        data, self.buffer = self.buffer, b""
        return data


class RecordingApplication(Application):
    """Captures session callbacks and application messages."""

    def __init__(self):
        self.logons = []
        self.logouts = []
        self.messages = []
        #: Set to a Failure to make the next on_message reject.
        self.next_failure = None

    def on_logon(self, session):
        self.logons.append(session)

    def on_logout(self, session, reason):
        self.logouts.append(reason)

    def on_message(self, session, message):
        self.messages.append(message)
        failure, self.next_failure = self.next_failure, None
        return failure


class SessionHarness(object):
    """A server-side session plus a scripted client on the other end."""

    def __init__(self, store=None, clock=None, dictionary=None, **config_kwargs):
        self.clock = clock or FixedClock()
        self.dictionary = dictionary or test_dictionary()
        self.store = store or MemoryStore()
        self.store.open()

        settings = dict(sender_comp_id=SERVER, target_comp_id=CLIENT,
                        heartbeat_interval=30)
        settings.update(config_kwargs)
        self.config = SessionConfig(**settings)

        self.application = RecordingApplication()
        self.session = Session(self.config, self.store, self.clock,
                               self.dictionary, self.application)
        self.transport = FakeTransport()
        self.client_seq = 1
        self._framer = Framer()

    # -- lifecycle ---------------------------------------------------------

    def connect(self):
        self.session.attach(self.transport)
        return self

    def reconnect(self):
        """Fresh transport onto the same session, as a client restart would."""
        self.transport = FakeTransport()
        self._framer.reset()
        self.session.attach(self.transport)
        return self

    def logon(self, reset=False, seq=None, heartbeat=30):
        message = Message.create(C.LOGON)
        message.set(C.ENCRYPT_METHOD, "0")
        message.set(C.HEART_BT_INT, heartbeat)
        if reset:
            message.set(C.RESET_SEQ_NUM_FLAG, C.YES)
            self.client_seq = 1
        return self.send(message, seq=seq)

    # -- client -> server --------------------------------------------------

    def send(self, message, seq=None, poss_dup=False, sender=None, target=None):
        """Send a message as the client would, filling in the session header."""
        if seq is None:
            seq = self.client_seq
            self.client_seq += 1
        else:
            self.client_seq = seq + 1

        message.set(C.MSG_SEQ_NUM, seq)
        message.set(C.SENDER_COMP_ID, sender or CLIENT)
        message.set(C.TARGET_COMP_ID, target or SERVER)
        if not message.has(C.SENDING_TIME):
            message.set(C.SENDING_TIME, self.clock.timestamp())
        if poss_dup:
            message.set(C.POSS_DUP_FLAG, C.YES)
            message.set(C.ORIG_SENDING_TIME, self.clock.timestamp())

        self.session.on_data(encode(message, self.config.begin_string))
        return message

    def send_raw(self, data):
        self.session.on_data(data)

    def new_order(self, cl_ord_id="ORD-1", symbol="7203", side="1",
                  qty="100", price="2845.5", seq=None, poss_dup=False):
        message = Message.create(C.NEW_ORDER_SINGLE)
        message.set(11, cl_ord_id)
        message.set(55, symbol)
        message.set(54, side)
        message.set(40, "2")
        message.set(38, qty)
        message.set(44, price)
        message.set(60, self.clock.timestamp())
        return self.send(message, seq=seq, poss_dup=poss_dup)

    # -- server -> client --------------------------------------------------

    def received(self):
        """Decode and clear everything the session has sent."""
        return [decode(raw) for raw in self._framer.feed(self.transport.take())]

    def received_types(self):
        return [message.msg_type for message in self.received()]

    def expect_one(self, msg_type=None):
        """Assert exactly one message was sent, and return it."""
        messages = self.received()
        assert len(messages) == 1, (
            "expected exactly 1 message, got %d: %s"
            % (len(messages), [m.to_string() for m in messages]))
        if msg_type is not None:
            assert messages[0].msg_type == msg_type, (
                "expected MsgType '%s', got '%s': %s"
                % (msg_type, messages[0].msg_type, messages[0].to_string()))
        return messages[0]

    def drain(self):
        """Discard anything buffered, e.g. the Logon response."""
        self.received()
        return self
