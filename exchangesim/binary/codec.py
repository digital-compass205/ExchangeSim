"""The binary wire format, behind the same interface as the FIX one.

Everything above this line -- session, dictionary, handlers, audit, board --
works in :class:`~exchangesim.fix.message.Message`, so this codec's whole job is
to be the place where a binary frame becomes one and stops being anything else.

Three points where the two encodings genuinely differ, rather than merely
spelling the same thing differently:

* **There is one Comp ID on the wire, the client's** (section 7.2), not a
  Sender/Target pair. Inbound it becomes ``SenderCompID`` and the venue's own
  identity is filled in as ``TargetCompID``; outbound the client's is written
  back. The session layer's CompID checks then work unchanged.
* **There is no SendingTime.** Nothing needs it: business messages carry
  ``TransactTime``, and the audit timestamps arrival itself. It is simply not a
  field of this encoding, so none is invented.
* **A Reject is replayed, not gap-filled.** Section 5.6 lists what may be
  skipped on a resend, and unlike FIX 4.2 practice the Reject is not on it.
"""

from ..fix import constants as C
from ..fix.message import MalformedMessage, Message
from . import message as framing
from . import types as T
from .layout import unsupported_msg_type

#: Binary message types that may be skipped with a gap fill, section 5.6:
#: Logon, Logout, Heartbeat, Test Request, Resend Request and Sequence Reset.
GAP_FILLABLE = frozenset((0, 1, 2, 4, 5, 6))

_HEX_WIDTH = 16


class BinaryCodec(object):
    """HKEX OCG-C binary, section 7."""

    name = "binary"

    def __init__(self, dictionary, comp_id, client=False):
        #: The layouts, a :class:`~exchangesim.binary.layout.BinaryDictionary`.
        self.dictionary = dictionary
        #: The **venue's** Comp ID. The wire carries only the client's, so this
        #: is the half of the pair that has to be supplied from configuration,
        #: whichever end of the session is holding the codec.
        self.comp_id = comp_id
        #: True on the client side of a session, where the identity the wire
        #: carries is one's own rather than the counterparty's.
        self.client = client

    def framer(self):
        return framing.Framer()

    def extract(self, buffer):
        """Split one complete message off the front of a buffer."""
        return framing.extract(buffer)

    # -- inbound -----------------------------------------------------------

    def decode(self, raw, validate_checksum=True):
        """One complete frame as the message the rest of the simulator uses."""
        header = framing.unpack_header(raw, validate_checksum=validate_checksum)
        layout = self.dictionary.layout(header.msg_type)

        message = Message.create(
            layout.fix_msg_type if layout is not None
            else unsupported_msg_type(header.msg_type))
        message.set(C.MSG_SEQ_NUM, header.seq_num)
        if self.client:
            message.set(C.SENDER_COMP_ID, self.comp_id)
            message.set(C.TARGET_COMP_ID, header.comp_id)
        else:
            message.set(C.SENDER_COMP_ID, header.comp_id)
            message.set(C.TARGET_COMP_ID, self.comp_id)
        if header.poss_dup:
            message.set(C.POSS_DUP_FLAG, C.YES)
        if header.poss_resend:
            message.set(C.POSS_RESEND, C.YES)

        if layout is not None:
            layout.decode_body(raw, message)
        return message

    # -- outbound ----------------------------------------------------------

    def encode(self, message):
        """One message as a frame, with its length and checksum stamped."""
        layout = self.dictionary.layout_for(message)
        if layout is None:
            raise ValueError("no binary layout for MsgType '%s'"
                             % message.msg_type)

        # The one Comp ID on the wire is always the client's, in either
        # direction -- so which tag it comes from depends on who is sending.
        comp_id = (message.get(C.SENDER_COMP_ID) if self.client
                   else message.get(C.TARGET_COMP_ID) or self.comp_id)

        header = framing.Header(
            msg_type=layout.msg_type,
            seq_num=message.seq_num or 0,
            comp_id=comp_id or "",
            poss_dup=message.get(C.POSS_DUP_FLAG) == C.YES,
            poss_resend=message.get(C.POSS_RESEND) == C.YES)

        presence, body = layout.encode_body(message)
        return framing.pack(header, presence, body)

    def is_admin(self, raw):
        try:
            header = framing.unpack_header(raw, validate_checksum=False)
        except MalformedMessage:
            return False
        return header.msg_type in GAP_FILLABLE

    # -- rendering ---------------------------------------------------------

    def raw_string(self, raw, dictionary=None, delimiter=None):
        """The frame as a hex dump, with any credential blanked out.

        A hex dump rather than a reconstruction, because the reason to open the
        bytes of a binary message is to compare them with what the client's own
        log shows. Bytes belonging to a redacted field are struck out in place,
        so the offsets still line up with that log.
        """
        blanked = self._redacted_spans(raw)
        lines = []
        for start in range(0, len(raw), _HEX_WIDTH):
            chunk = raw[start:start + _HEX_WIDTH]
            hex_cells = []
            text_cells = []
            for index, byte in enumerate(chunk):
                if start + index in blanked:
                    hex_cells.append("--")
                    text_cells.append(".")
                    continue
                hex_cells.append("%02x" % byte)
                text_cells.append(chr(byte) if 32 <= byte < 127 else ".")
            padding = "   " * (_HEX_WIDTH - len(chunk))
            lines.append("%04x  %s%s  %s"
                         % (start, " ".join(hex_cells), padding,
                            "".join(text_cells)))
        return "\n".join(lines)

    def _redacted_spans(self, raw):
        """Byte offsets covered by a field the dialect marks as a credential."""
        try:
            header = framing.unpack_header(raw, validate_checksum=False)
        except MalformedMessage:
            return frozenset()
        layout = self.dictionary.layout(header.msg_type)
        if layout is None:
            return frozenset()

        blanked = set()
        try:
            bits, offset = T.presence_bits(raw, framing.HEADER_BYTES)
            for bit in bits:
                field = layout.by_bit.get(bit)
                if field is None:
                    break
                _value, end = field.type.unpack(raw, offset)
                if field.redact:
                    blanked.update(range(offset, end))
                offset = end
        except MalformedMessage:
            # A frame that will not walk is exactly the one worth showing raw;
            # what has been found so far is still worth hiding.
            pass
        return frozenset(blanked)

    def __repr__(self):
        return "BinaryCodec(%s)" % self.comp_id
