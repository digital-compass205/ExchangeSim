"""Data-driven FIX dialect description and validation.

A :class:`Dictionary` says which tags exist, what they may contain, and which
are required per message type. Venues build one to describe their dialect --
which is how one session implementation serves exchanges whose FIX differs in
tags, enumerations and field lengths.

Validation failures are reported as :class:`Failure`, carrying the
``SessionRejectReason(373)`` the caller should put on the Reject, so reject
semantics stay in one place rather than being reinvented per venue.
"""

from .constants import FieldType, SessionRejectReason
from .message import TAG_BODY_LENGTH, TAG_BEGIN_STRING, TAG_CHECKSUM

_BOOLEANS = frozenset(("Y", "N"))


class Failure(object):
    """A validation failure and the reject reason it maps to."""

    __slots__ = ("reason", "tag", "text")

    def __init__(self, reason, tag=None, text=""):
        self.reason = reason
        self.tag = tag
        self.text = text

    def __repr__(self):
        return "Failure(reason=%s, tag=%s, %r)" % (self.reason, self.tag, self.text)


def enum_labels(source):
    """Reverse an enumeration class into ``{wire value: NAME}``.

    Venue dialects declare their enumerations as plain classes of constants and
    feed those same constants to a :class:`FieldDef`'s ``values``. The audit
    view needs the other direction -- ``39=1`` should read PARTIALLY_FILLED --
    so taking it from the same declaration keeps the meaning from drifting away
    from the validation.

    Members are selected **by type, not by name**: ``ALL`` is a metadata tuple
    on Japannext's ``SubID`` but a genuine wire value on HKEX's
    ``MassCancelRequestType``, so a name-based exclusion would be wrong while
    ignoring anything that is not a string or an integer is exactly right. That
    also drops ``LABELS`` maps, and the leading-underscore skip drops the
    docstring. Integer members are keyed by their string form, because that is
    what travels on the wire.
    """
    labels = {}
    for name in sorted(vars(source)):
        if name.startswith("_"):
            continue
        value = getattr(source, name)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        # setdefault, over a sorted pass, so a dialect that ever gives one value
        # two names picks the same one every run rather than by dict ordering.
        labels.setdefault(str(value), name)
    return labels


class FieldDef(object):
    """One tag: its name, type, permitted values and length limits."""

    __slots__ = ("tag", "name", "type", "values", "max_length",
                 "max_digits", "max_decimals", "multi", "labels", "redact")

    def __init__(self, tag, name, type_=FieldType.STRING, values=None,
                 max_length=None, max_digits=None, max_decimals=None,
                 labels=None, redact=False):
        self.tag = tag
        self.name = name
        self.type = type_
        #: Permitted values, or None when unconstrained.
        self.values = frozenset(values) if values else None
        self.max_length = max_length
        #: Digits before the decimal point, for PRICE/QTY/AMT.
        self.max_digits = max_digits
        #: Digits after the decimal point.
        self.max_decimals = max_decimals
        self.multi = type_ == FieldType.MULTI_VALUE_STRING
        #: What each permitted value means, for a human reading a message.
        self.labels = dict(labels) if labels else None
        #: True for a field whose value must never be shown or logged.
        self.redact = redact

    def label(self, value):
        """The meaning of one raw value, or None when there is nothing to add."""
        if self.type == FieldType.BOOLEAN:
            return {"Y": "YES", "N": "NO"}.get(value)
        if self.labels is None:
            return None
        if self.multi:
            parts = [self.labels.get(part, part)
                     for part in value.split(" ") if part]
            return " ".join(parts) if parts else None
        return self.labels.get(value)

    def validate(self, value):
        # type: (str) -> Failure
        """Check a raw value, returning a :class:`Failure` or None."""
        if value == "":
            return Failure(SessionRejectReason.TAG_WITHOUT_VALUE, self.tag,
                           "%s (%d) has no value" % (self.name, self.tag))

        checker = _CHECKERS.get(self.type)
        if checker is not None:
            failure = checker(self, value)
            if failure is not None:
                return failure

        if self.max_length is not None and len(value) > self.max_length:
            return Failure(SessionRejectReason.VALUE_INCORRECT, self.tag,
                           "%s (%d) exceeds %d characters"
                           % (self.name, self.tag, self.max_length))

        if self.values is not None:
            if self.multi:
                for part in value.split(" "):
                    if part and part not in self.values:
                        return Failure(SessionRejectReason.VALUE_INCORRECT, self.tag,
                                       "%s (%d) has unsupported value '%s'"
                                       % (self.name, self.tag, part))
            elif value not in self.values:
                return Failure(SessionRejectReason.VALUE_INCORRECT, self.tag,
                               "%s (%d) has unsupported value '%s'"
                               % (self.name, self.tag, value))
        return None


# -- per-type format checks --------------------------------------------------

def _check_int(field, value):
    if not _is_integer(value):
        return _format_failure(field, "an integer")
    return None


def _check_char(field, value):
    if len(value) != 1:
        return _format_failure(field, "a single character")
    return None


def _check_boolean(field, value):
    if value not in _BOOLEANS:
        return _format_failure(field, "Y or N")
    return None


def _check_numeric(field, value):
    """PRICE / QTY / AMT: optionally signed decimal, with digit limits."""
    body = value[1:] if value[:1] in ("-", "+") else value
    if not body:
        return _format_failure(field, "a number")

    if "." in body:
        whole, _, fraction = body.partition(".")
        if "." in fraction:
            return _format_failure(field, "a number")
    else:
        whole, fraction = body, ""

    if not whole.isdigit() and whole != "":
        return _format_failure(field, "a number")
    if fraction and not fraction.isdigit():
        return _format_failure(field, "a number")
    if whole == "" and fraction == "":
        return _format_failure(field, "a number")

    if field.max_digits is not None and len(whole.lstrip("0")) > field.max_digits:
        return Failure(SessionRejectReason.VALUE_INCORRECT, field.tag,
                       "%s (%d) allows at most %d whole digits"
                       % (field.name, field.tag, field.max_digits))
    if field.max_decimals is not None and len(fraction) > field.max_decimals:
        return Failure(SessionRejectReason.VALUE_INCORRECT, field.tag,
                       "%s (%d) allows at most %d decimal places"
                       % (field.name, field.tag, field.max_decimals))
    return None


def _check_timestamp(field, value):
    """UTCTimestamp: ``YYYYMMDD-HH:MM:SS`` with an optional fraction.

    How much fraction is a property of the dialect, not of FIX: Japannext
    writes milliseconds and HKEX OCG-C writes microseconds, and a client that
    sends what its own specification documents must not be rejected. The field
    says how many places it allows through ``max_decimals``; anything from one
    place up to that is accepted, since a client is free to send a shorter
    fraction than the maximum.
    """
    places = field.max_decimals or 3
    expected = "YYYYMMDD-HH:MM:SS[.%s]" % ("s" * places)
    if len(value) < 17 or value[8] != "-":
        return _format_failure(field, expected)
    date, _, clock = value.partition("-")
    if not date.isdigit() or len(date) != 8:
        return _format_failure(field, expected)
    seconds, separator, fraction = clock.partition(".")
    parts = seconds.split(":")
    if len(parts) != 3 or not all(part.isdigit() and len(part) == 2 for part in parts):
        return _format_failure(field, expected)
    if separator and (not fraction.isdigit() or not 1 <= len(fraction) <= places):
        return _format_failure(field, expected)
    if not separator and len(value) != 17:
        return _format_failure(field, expected)
    return None


def _format_failure(field, expected):
    return Failure(SessionRejectReason.INCORRECT_DATA_FORMAT, field.tag,
                   "%s (%d) must be %s" % (field.name, field.tag, expected))


def _is_integer(value):
    body = value[1:] if value[:1] in ("-", "+") else value
    return bool(body) and body.isdigit()


_CHECKERS = {
    FieldType.INT: _check_int,
    FieldType.CHAR: _check_char,
    FieldType.BOOLEAN: _check_boolean,
    FieldType.QTY: _check_numeric,
    FieldType.PRICE: _check_numeric,
    FieldType.AMT: _check_numeric,
    FieldType.UTC_TIMESTAMP: _check_timestamp,
}


class MessageDef(object):
    """Which tags a message type accepts, and which it requires."""

    __slots__ = ("msg_type", "name", "required", "optional", "inbound")

    def __init__(self, msg_type, name, required=(), optional=(), inbound=True):
        self.msg_type = msg_type
        self.name = name
        self.required = frozenset(required)
        self.optional = frozenset(optional)
        #: False for messages the simulator only ever sends.
        self.inbound = inbound

    @property
    def known(self):
        return self.required | self.optional


class Dictionary(object):
    """A complete FIX dialect: fields, messages, and header/trailer tags."""

    def __init__(self, begin_string="FIX.4.2", fields=(), messages=(),
                 header=(), trailer=()):
        self.begin_string = begin_string
        self.fields = {}
        self.fields_by_name = {}
        for field in fields:
            self.add_field(field)

        self.messages = {}
        for message in messages:
            self.add_message(message)

        #: Tags legal in any message, so message definitions need not repeat them.
        self.header = frozenset(header)
        self.trailer = frozenset(trailer) | {TAG_CHECKSUM}
        self.structural = frozenset((TAG_BEGIN_STRING, TAG_BODY_LENGTH, TAG_CHECKSUM))

    # -- construction ------------------------------------------------------

    def add_field(self, field):
        self.fields[field.tag] = field
        self.fields_by_name[field.name] = field
        return field

    def add_message(self, message):
        self.messages[message.msg_type] = message
        return message

    def field(self, tag):
        return self.fields.get(tag)

    def message(self, msg_type):
        return self.messages.get(msg_type)

    def supports(self, msg_type):
        return msg_type in self.messages

    # -- validation --------------------------------------------------------

    def validate(self, message, check_unknown=True):
        # type: (object, bool) -> Failure
        """Validate a decoded message against the dialect.

        Returns the first :class:`Failure`, or None when the message is
        acceptable. Checks run in the order a Reject would name them: message
        type, then per-field format and values, then required fields.
        """
        msg_type = message.msg_type
        definition = self.messages.get(msg_type)
        if definition is None:
            return Failure(SessionRejectReason.INVALID_MSGTYPE, None,
                           "MsgType (35) '%s' is not supported" % msg_type)

        if not definition.inbound:
            # Defined so the venue can build it, but never legal to receive --
            # a client sending us an ExecutionReport is an error, not traffic.
            return Failure(SessionRejectReason.INVALID_MSGTYPE, None,
                           "MsgType (35) '%s' cannot be sent to the venue"
                           % msg_type)

        allowed = definition.known | self.header | self.trailer | self.structural

        seen = set()
        for tag, value in message.fields:
            if tag in self.structural:
                continue
            seen.add(tag)

            field = self.fields.get(tag)
            if field is None:
                if check_unknown:
                    return Failure(SessionRejectReason.INVALID_TAG_NUMBER, tag,
                                   "tag %d is not defined" % tag)
                continue

            if check_unknown and tag not in allowed:
                return Failure(SessionRejectReason.TAG_NOT_DEFINED_FOR_MESSAGE, tag,
                               "%s (%d) is not defined for MsgType '%s'"
                               % (field.name, tag, msg_type))

            failure = field.validate(value)
            if failure is not None:
                return failure

        for tag in sorted(definition.required):
            if tag not in seen:
                field = self.fields.get(tag)
                name = field.name if field else "tag"
                return Failure(SessionRejectReason.REQUIRED_TAG_MISSING, tag,
                               "%s (%d) is required for MsgType '%s'"
                               % (name, tag, msg_type))
        return None
