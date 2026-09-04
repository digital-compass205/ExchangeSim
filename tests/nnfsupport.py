"""A box connection over a fake socket, for testing the NNF session layer.

The narrowest harness that exercises a box: a manager, a stub gateway that
answers the connection sequence, and a client that speaks the same structures
back. There is no venue here, no engine and no book -- what is under test is
registration, box sign-on, user sign-on, the heartbeat and what a disconnect
does to the users on a box.

It uses the real Capital Market structures rather than invented ones, because
the sequence being tested is a real sequence. That does not weaken the claim
that ``exchangesim.nnf`` knows nothing about a segment:
:mod:`tests.test_nnf_codec` pins that with a layout defined in the test file,
and the session takes the three dialect facts it needs -- which tag is the User
ID, which is the Box ID, which transaction code is the heartbeat -- as
arguments.
"""

from exchangesim.core.clock import FixedClock
from exchangesim.fix.message import Message
from exchangesim.nnf import crypto
from exchangesim.nnf.codec import NnfCodec
from exchangesim.nnf.session import (
    Application,
    BoxConnection,
    NnfSessionConfig,
    NnfSessionManager,
)
from exchangesim.venues.nse import dictionary as D
from exchangesim.venues.nse import layouts as LY
from exchangesim.venues.nse import transactions as X

from tests.fixsupport import FakeTransport

class BoxTransport(FakeTransport):
    """A fake socket with the two attributes a reactor connection also has."""

    peer = ("127.0.0.1", 51234)

    def close_when_flushed(self):
        self.close()


BOX_ID = 7
BROKER_ID = "10123"
USER_ID = 40521
OTHER_USER_ID = 40522
PASSWORD = "Nse@2026"

#: What a Gateway Router would have issued for this box.
KEY = b"\x41" * crypto.KEY_BYTES
IV = b"\x42" * 8 + b"\x00" * 7 + b"\x05"
AAD = b"\x43" * crypto.AAD_BYTES


def user_config(user_id=USER_ID, **overrides):
    settings = dict(box_id=BOX_ID, broker_id=BROKER_ID, branch_id=1,
                    password=PASSWORD, markets=("NORMAL",),
                    cancel_on_disconnect=False)
    settings.update(overrides)
    return NnfSessionConfig(user_id, **settings)


class StubGateway(Application):
    """Answers the connection sequence, and records everything else.

    Stands in for the venue's handlers, which do not exist until the venue
    does. It knows only the sequence the session layer drives -- register, sign
    the box on, sign a user on -- and hands every application message straight
    to a list.
    """

    def __init__(self, manager, cipher_for=None):
        self.manager = manager
        self.cipher_for = cipher_for or (lambda box: None)
        self.received = []
        self.logons = []
        self.logouts = []
        self.invalid = []

    # -- the box sequence --------------------------------------------------

    def on_box_message(self, box, message):
        code = int(message.msg_type)

        if code == X.SECURE_BOX_REGISTRATION_REQUEST_IN:
            box.send(self._reply(X.SECURE_BOX_REGISTRATION_REQUEST_OUT))
            box.registered(self.cipher_for(box))
            return True

        if code == X.BOX_SIGN_ON_REQUEST_IN:
            reply = self._reply(X.BOX_SIGN_ON_REQUEST_OUT)
            reply.set(D.BOX_ID, str(box.box_id))
            box.send(reply)
            box.signed_on(message.get(D.BROKER_ID))
            return True

        if code == X.SIGN_ON_REQUEST_IN:
            config = self.manager.user_config(int(message.get(D.SIGNON_USER_ID)))
            if config is None or config.box_id != box.box_id:
                error = self._reply(X.SIGN_ON_REQUEST_OUT)
                error.set(D.ERROR_CODE, "16042")        # ERR_USER_NOT_FOUND
                box.send(error)
                return True
            reply = self._reply(X.SIGN_ON_REQUEST_OUT)
            reply.set(D.SIGNON_USER_ID, str(config.user_id))
            reply.set(D.BROKER_ID, config.broker_id)
            box.send(reply)
            box.sign_on_user(config)
            return True

        if code == X.SIGN_OFF_REQUEST_IN:
            return True

        self.received.append(message)
        return True

    # -- everything else ---------------------------------------------------

    def on_message(self, session, message):
        if int(message.msg_type) == X.SIGN_OFF_REQUEST_IN:
            session.send(self._reply(X.SIGN_OFF_REQUEST_OUT))
            session.disconnect("signed off")
            return None
        self.received.append(message)
        return None

    def on_logon(self, session):
        self.logons.append(session.target_comp_id)

    def on_logout(self, session, reason):
        self.logouts.append((session.target_comp_id, reason))

    def on_invalid(self, session, message, failure):
        self.invalid.append(failure)
        return True

    @staticmethod
    def _reply(code):
        message = Message.create(str(code))
        message.set(D.ERROR_CODE, "0")
        return message


class BoxHarness(object):
    """One manager, one box, a fake socket and a client codec on the far end."""

    def __init__(self, users=(USER_ID,), cipher_for=None, clock=None):
        self.clock = clock or FixedClock()
        self.manager = NnfSessionManager(
            self.clock, D.build_cm(), LY.build_cm(),
            user_id_tag=D.USER_ID, box_id_tag=D.BOX_ID,
            heartbeat_code=X.HEARTBEAT)
        self.gateway = StubGateway(self.manager, cipher_for)
        self.box = self.manager.add_box(BOX_ID, self.gateway)
        for user_id in users:
            self.manager.add_user(user_config(user_id))

        self.transport = BoxTransport()
        self.client = NnfCodec(LY.build_cm(), client=True)
        self.box.attach(self.transport)

    # -- driving the client ------------------------------------------------

    def send(self, code, fields=None, user_id=None):
        """Encode one message as a client would and feed it to the box.

        ``fields`` is a dict keyed by tag number, not keyword arguments: the
        tags are integers, which Python will not accept as keywords.
        """
        message = Message.create(str(code))
        message.set(D.ERROR_CODE, "0")
        if user_id is not None:
            message.set(D.USER_ID, str(user_id))
        for tag, value in sorted((fields or {}).items()):
            message.set(tag, str(value))
        self.box.on_data(self.client.encode(message))
        return message

    def received(self):
        """Everything the box has sent since the last clear(), decoded."""
        messages = []
        framer = self.client.framer()
        for raw in framer.feed(self.transport.take()):
            messages.append(self.client.decode(raw))
        return messages

    def last(self):
        messages = self.received()
        return messages[-1] if messages else None

    def clear(self):
        self.transport.take()

    # -- the whole opening sequence ---------------------------------------

    def register(self):
        self.send(X.SECURE_BOX_REGISTRATION_REQUEST_IN, {D.BOX_ID: BOX_ID})
        return self

    def box_sign_on(self):
        self.send(X.BOX_SIGN_ON_REQUEST_IN,
                  {D.BOX_ID: BOX_ID, D.BROKER_ID: BROKER_ID})
        return self

    def sign_on(self, user_id=USER_ID):
        self.send(X.SIGN_ON_REQUEST_IN,
                  {D.SIGNON_USER_ID: user_id, D.PASSWORD: PASSWORD,
                   D.BROKER_ID: BROKER_ID},
                  user_id=user_id)
        return self

    def open(self, user_id=USER_ID):
        """Register, sign the box on, and sign one user on."""
        return self.register().box_sign_on().sign_on(user_id)

    def session(self, user_id=USER_ID):
        return self.box.user(user_id)


def existing_ciphers():
    """A member and an exchange under the existing methodology."""
    return (crypto.ExistingCipher(KEY, IV), crypto.ExistingCipher(KEY, IV))


def new_ciphers():
    """A member and an exchange under the new, authenticated methodology."""
    return (crypto.NewCipher(KEY, IV, AAD, client=True),
            crypto.NewCipher(KEY, IV, AAD))
