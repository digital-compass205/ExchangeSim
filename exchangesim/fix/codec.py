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

    def __repr__(self):
        return "FixCodec(%s)" % self.begin_string
