"""The NNF wire, behind the same interface as the other two.

Everything above this line -- session, dictionary, handlers, audit, control
plane, board -- works in :class:`exchangesim.fix.message.Message`, so this
codec's whole job is to be the place where an NNF packet becomes one and stops
being anything else. The methods are the ones
:class:`exchangesim.fix.codec.FixCodec` names, so a session holds one of these
without knowing which protocol it is reading.

Three things are particular to this protocol.

**The codec holds the connection's cipher, and it changes mid-stream.** A box
connection opens in clear, carries one unencrypted registration message, and is
encrypted from the second message onwards under a key the Gateway Router issued
on a different connection entirely. So the cipher is assignable rather than
constructor-fixed, and one codec belongs to one box connection rather than to
the venue.

**The packet sequence number travels as ``MsgSeqNum(34)``.** Not because NNF is
FIX -- it has no resend and no gap detection -- but because the field is a
per-message counter that the exchange echoes on order responses, which is the
same *concept* tag 34 names, and putting it there gives the audit its sequence
column for free. What the number does *not* do is police anything; that
difference lives in :mod:`exchangesim.nnf.session`, which never asks for a gap.

**There is nothing to gap-fill**, so :meth:`is_admin` is always False. The
method stays because the seam names it, and answering honestly is shorter than
explaining its absence.
"""

from ..fix import constants as C
from ..fix.message import MalformedMessage, Message
from . import packet as framing
from .crypto import PlainCipher

_HEX_WIDTH = 16


def transaction_code_of(msg_type):
    """The transaction code behind a message type, or None if it is not one.

    A message type *is* the decimal transaction code here, so this is a parse
    rather than a lookup. The binary encoding of OCG-C needs a ``B`` prefix to
    keep its numeric types clear of FIX's letters; nothing in NNF is spelled
    with a letter, so a code with no structure decodes to its own number and no
    dialect defines it, which is refusal enough.
    """
    try:
        return int(str(msg_type))
    except ValueError:
        return None


class NnfCodec(object):
    """NSE's NNF Trimmed Protocol, Chapters 2 and 10."""

    name = "nnf"

    def __init__(self, layouts, cipher=None, client=False):
        #: The structures, an :class:`exchangesim.nnf.layout.NnfDictionary`.
        self.layouts = layouts
        #: How this connection carries a message. Replaced by the session once
        #: the box has registered; until then every message travels in clear.
        self.cipher = cipher or PlainCipher()
        #: True on the client side. The wire is symmetric, so this affects only
        #: which end a fresh cipher is built for.
        self.client = client

    def framer(self):
        return framing.Framer()

    def extract(self, buffer):
        """Split one complete packet off the front of a buffer."""
        return framing.extract(buffer)

    # -- inbound -----------------------------------------------------------

    def decode(self, raw, validate_checksum=True):
        """One complete packet as the message the rest of the simulator uses.

        ``validate_checksum`` false skips the integrity check but still
        decrypts: an audit reading back a recorded packet wants the fields, and
        a message whose MD5 failed is exactly the one worth being able to read.
        """
        prefix = framing.unpack_prefix(raw)
        data = self.cipher.open(framing.data_of(raw),
                                prefix.checksum if validate_checksum else None)

        code = self.layouts.transaction_code(data)
        layout = self.layouts.layout(code)

        if layout is None:
            # No structure, so only the header can be read -- but that is
            # enough for the dictionary to refuse the code by name and for the
            # venue to answer with an error naming what it refused.
            message = Message.create(str(code))
            self.layouts.header.decode(data, message)
        else:
            message = layout.decode(data)

        message.set(C.MSG_SEQ_NUM, prefix.sequence)
        return message

    # -- outbound ----------------------------------------------------------

    def encode(self, message):
        """One message as a packet, encrypted and with its length stamped."""
        layout = self.layouts.layout_for(message)
        if layout is None:
            raise ValueError("no NNF structure for transaction code '%s'"
                             % message.msg_type)

        data, checksum = self.cipher.seal(layout.encode(message))
        return framing.pack(data, sequence=message.seq_num or 0,
                            checksum=checksum)

    def is_admin(self, raw):
        """Nothing here is gap-fillable: NNF has no resend."""
        return False

    # -- rendering ---------------------------------------------------------

    def raw_string(self, raw, dictionary=None, delimiter=None):
        """The packet as a hex dump, with any credential struck out.

        A hex dump rather than a reconstruction, because the reason to open the
        bytes of a binary message is to lay them beside the client's own log.

        **Nothing is decrypted here, and nothing may be.** Under the existing
        methodology the keystream runs continuously across the whole connection,
        so a packet cannot be decrypted out of order without replaying every
        message before it -- and rendering an audit entry must not touch the
        state of a live connection at all. That is why
        :class:`exchangesim.nnf.session.BoxConnection` records the *plaintext*
        packet rather than the ciphertext: the dump then lines up field for
        field with the structure and a credential can be struck out of it, at
        the cost of not being byte-identical to an encrypted wire capture.
        """
        blanked = self._redacted_offsets(framing.data_of(raw))
        return self._dump(raw, blanked, 0)

    def _redacted_offsets(self, data):
        """Offsets *in the packet* covered by a field marked as a credential."""
        try:
            layout = self.layouts.layout(self.layouts.transaction_code(data))
        except MalformedMessage:
            return frozenset()
        if layout is None:
            return frozenset()
        blanked = set()
        for start, end in layout.redacted_spans():
            blanked.update(range(start + framing.PREFIX_BYTES,
                                 end + framing.PREFIX_BYTES))
        return frozenset(blanked)

    @staticmethod
    def _dump(raw, blanked, base):
        lines = []
        for start in range(0, len(raw), _HEX_WIDTH):
            chunk = bytearray(raw[start:start + _HEX_WIDTH])
            hex_cells, text_cells = [], []
            for index, byte in enumerate(chunk):
                if start + index in blanked:
                    hex_cells.append("--")
                    text_cells.append(".")
                    continue
                hex_cells.append("%02x" % byte)
                text_cells.append(chr(byte) if 32 <= byte < 127 else ".")
            padding = "   " * (_HEX_WIDTH - len(chunk))
            lines.append("%04x  %s%s  %s"
                         % (base + start, " ".join(hex_cells), padding,
                            "".join(text_cells)))
        return "\n".join(lines)

    def __repr__(self):
        return "NnfCodec(%s, %d structures)" % (self.cipher.name,
                                                len(self.layouts))
