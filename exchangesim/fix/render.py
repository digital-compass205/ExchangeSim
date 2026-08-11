"""Turning FIX messages into something a person can read.

The audit view needs three things the wire does not carry: what each tag is
called, what its value means, and a rendering that never shows a field the
dialect marked as a credential. All three come from the venue's own
:class:`~exchangesim.fix.dictionary.Dictionary`, so a dialect that adds a field
describes it once and every view follows.

Kept here, in the shared FIX layer, rather than in the control commands: it is
generic wire knowledge, not any one venue's dialect.
"""

from . import constants as C
from .constants import FieldType
from .message import SOH

#: What a redacted value is replaced by, in the field dump and in the wire
#: string alike. Not blank, so it is obvious a value was withheld rather than
#: absent.
REDACTED = "<redacted>"

#: Tags whose value is a message type, and so is named by the dictionary's
#: message table rather than by a field enumeration.
_MSG_TYPE_TAGS = (C.MSG_TYPE, C.REF_MSG_TYPE)

#: Lifted into an entry's one-line summary, in this order. All are standard FIX
#: and mean the same at every venue; the instrument tag is not here because it
#: differs by dialect and has its own column.
SUMMARY_ORDER = (
    C.MSG_SEQ_NUM,
    11,     # ClOrdID
    41,     # OrigClOrdID
    37,     # OrderID
    54,     # Side
    38,     # OrderQty
    40,     # OrdType
    44,     # Price
    59,     # TimeInForce
    150,    # ExecType
    39,     # OrdStatus
    32,     # LastShares / LastQty
    31,     # LastPx
    58,     # Text
    C.POSS_DUP_FLAG,
)

#: Everything worth extracting as a message is recorded: the summary fields
#: plus the two instrument tags. One frozenset so the recorder can decide in a
#: single pass over the message.
SYMBOL_TAGS = (55, 48)      # Symbol / SecurityID -- no dialect defines both
WANTED_TAGS = frozenset(SUMMARY_ORDER + SYMBOL_TAGS)


def extract(message):
    """The wanted tags of a message, in **one** pass over its fields.

    This runs on the gateway's message path, so it must stay proportional to
    the message rather than to the number of tags asked for: four calls to
    ``Message.get`` would be four linear scans.
    """
    found = {}
    for tag, value in message.fields:
        if tag in WANTED_TAGS and tag not in found:
            found[tag] = value
    return found


def symbol_of(extracted):
    """The instrument a message names, or None for a session-level message."""
    for tag in SYMBOL_TAGS:
        value = extracted.get(tag)
        if value:
            return value
    return None


def label_for(tag, value, dictionary):
    """What one value means, or None when the tag says it all."""
    if dictionary is None:
        return None
    if tag in _MSG_TYPE_TAGS:
        # There is no MsgType enumeration to reverse -- and its values collide
        # with other namespaces ("0" is both Heartbeat and EncryptMethod.NONE) --
        # so the message table is the only honest source.
        definition = dictionary.message(value)
        return definition.name if definition is not None else None
    field = dictionary.field(tag)
    return field.label(value) if field is not None else None


def describe_fields(message, dictionary):
    """Every field of a message: tag, name, value, meaning, and whether required.

    Fields appear in wire order, repeats included, which is what makes a
    repeating group readable at all -- the codec deliberately does not model
    groups, so position is the only thing that relates one to the next.
    """
    definition = dictionary.message(message.msg_type) if dictionary else None
    rows = []
    for tag, value in message.fields:
        field = dictionary.field(tag) if dictionary else None
        redacted = field is not None and field.redact
        rows.append({
            "tag": tag,
            "name": field.name if field is not None else None,
            "value": REDACTED if redacted else value,
            "label": None if redacted else label_for(tag, value, dictionary),
            "required": definition is not None and tag in definition.required,
            "redacted": redacted,
        })
    return rows


def raw_string(raw, dictionary, delimiter="|"):
    """Raw wire bytes as printable text, with credentials removed.

    Deliberately does not go through ``decode``: this must also render a
    message that *could not* be decoded, which is the one an audit is most
    often opened to look at. Splitting on SOH is enough to find the tags, and a
    tag that will not parse simply keeps its value.
    """
    parts = []
    for chunk in raw.split(SOH):
        if not chunk:
            continue
        text = chunk.decode("latin-1")
        tag_text, separator, value = text.partition("=")
        if separator:
            try:
                tag = int(tag_text)
            except ValueError:
                parts.append(text)
                continue
            field = dictionary.field(tag) if dictionary else None
            if field is not None and field.redact:
                value = REDACTED
            parts.append("%d=%s" % (tag, value))
        else:
            parts.append(text)
    return delimiter.join(parts)


def summarise(extracted, dictionary):
    """One line naming what a message actually says.

    Built from the tags lifted at record time, so a summary costs nothing until
    somebody looks at the list.
    """
    parts = []
    for tag in SUMMARY_ORDER:
        value = extracted.get(tag)
        if value is None or value == "":
            continue
        field = dictionary.field(tag) if dictionary else None
        if field is not None and field.redact:
            continue
        name = field.name if field is not None else str(tag)
        label = label_for(tag, value, dictionary)
        if field is not None and field.type == FieldType.BOOLEAN and value == "N":
            # A flag that is off is not worth a column in a one-line summary.
            continue
        parts.append("%s=%s" % (name, label or value))
    return " ".join(parts)
