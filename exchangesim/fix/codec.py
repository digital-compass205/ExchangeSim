"""The tag=value wire format, behind the interface a session talks to.

A session does four things to bytes: frame them, decode one message, encode one
message, and decide whether a stored message may be gap-filled rather than
replayed. Naming those four makes the wire format an argument rather than an
assumption, which is what lets one session implementation serve both encodings
HKEX publishes -- see :mod:`exchangesim.binary.codec` for the other one.
"""

from . import constants as C
from . import render
from .message import Framer, decode, encode, extract


class FixCodec(object):
    """FIX 4.2 / FIXT.1.1 tag=value, as every venue here has always spoken it."""

    #: Recorded on audit entries, so a reader knows which codec to read them with.
    name = "fix"

    def __init__(self, begin_string="FIX.4.2"):
        self.begin_string = begin_string

    def framer(self):
        return Framer()

    def extract(self, buffer):
        """Split one complete message off the front of a buffer."""
        return extract(buffer)

    def decode(self, raw, validate_checksum=True):
        return decode(raw, validate_checksum=validate_checksum)

    def encode(self, message):
        return encode(message, self.begin_string)

    def is_admin(self, raw):
        """True when a stored raw message may be gap-filled instead of replayed."""
        marker = raw.find(b"\x0135=")
        if marker < 0:
            return False
        end = raw.find(b"\x01", marker + 1)
        return raw[marker + 4:end].decode("latin-1") in C.ADMIN_MSG_TYPES

    def raw_string(self, raw, dictionary, delimiter="|"):
        return render.raw_string(raw, dictionary, delimiter)

    # -- the client half of the seam ---------------------------------------
    #
    # A scripted client has to fill in whatever its protocol's header needs
    # before a message goes out, and what that is differs completely: FIX wants
    # a sequence number, a CompID pair and a SendingTime, NNF wants none of
    # them. These two put that knowledge behind the same line as the rest of
    # the wire format, so `scenario/runner.py` branches on nothing.

    def prepare(self, message, sender, target, seq, clock, sub_id=None):
        """Stamp the header fields a scripted client would not write itself."""
        message.set(C.MSG_SEQ_NUM, seq)
        message.set(C.SENDER_COMP_ID, sender)
        message.set(C.TARGET_COMP_ID, target)
        message.set(C.SENDING_TIME, clock.timestamp())
        if sub_id and not message.has(C.TARGET_SUB_ID):
            message.set(C.TARGET_SUB_ID, sub_id)
        # Application messages need a TransactTime; filling it in keeps
        # scenario files free of timestamps that would go stale.
        if message.msg_type in ("D", "F", "G", "q") and not message.has(60):
            message.set(60, clock.timestamp())
        return message

    def logon_defaults(self, heartbeat=30):
        """The fields a Logon carries before a scenario adds its own."""
        return {str(C.MSG_TYPE): C.LOGON,
                str(C.ENCRYPT_METHOD): "0",
                str(C.HEART_BT_INT): str(heartbeat)}

    def __repr__(self):
        return "FixCodec(%s)" % self.begin_string
