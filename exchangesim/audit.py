"""A record of everything said to and by the simulator.

The point of a simulator is to stand in for a venue while somebody debugs a
client, and that is impossible without being able to see what was actually on
the wire. This holds a bounded, ordered history of two kinds of entry under one
sequence number:

``fix``
    every message in and out of every session -- administrative traffic,
    messages the dialect rejected, and messages that could not be parsed at all,
    because those are the ones an audit is opened to look at.

``control``
    every *mutating* control command, whoever sent it. An order entered from the
    web board is not a FIX message, so without this it would appear only as its
    effects on somebody else's session.

Two deliberate constraints.

**It records, it does not interpret.** A message arrives here already decoded,
and only its raw bytes and a handful of lifted tags are kept; naming the fields,
labelling their values and building a summary all happen when somebody asks for
one entry, not on the path every message takes. Nothing here imports the FIX
layer -- what it is handed is duck-typed -- so recording adds no edge to the
dependency graph.

**Disabled means absent.** A capacity of zero leaves the venue's ``audit`` as
None rather than a zero-length ring, so every call site is one ``is not None``
test and switching the audit off costs exactly nothing.
"""

import logging
from collections import deque

log = logging.getLogger(__name__)

#: Entries kept per venue. ~350 bytes each, so the default is well under a
#: megabyte; a busy conformance run is a few thousand messages.
DEFAULT_CAPACITY = 2000
MAX_CAPACITY = 100000

KIND_FIX = "fix"
KIND_CONTROL = "control"

DIRECTION_IN = "in"
DIRECTION_OUT = "out"


class AuditEntry(object):
    """One thing that happened, in the order it happened."""

    __slots__ = ("seq", "time", "kind", "direction", "session", "symbol",
                 "type", "type_name", "seq_num", "ok", "raw", "error",
                 "detail", "protocol")

    def __init__(self, seq, time, kind, direction, session=None, symbol=None,
                 type_=None, type_name=None, seq_num=None, ok=True, raw=None,
                 error=None, detail=None, protocol=None):
        self.seq = seq
        self.time = time
        self.kind = kind
        self.direction = direction
        #: Whose traffic this is: a FIX TargetCompID, or a control peer.
        self.session = session
        #: None for a session-level message that names no instrument.
        self.symbol = symbol
        #: MsgType for a FIX entry, command name for a control one.
        self.type = type_
        self.type_name = type_name
        self.seq_num = seq_num
        self.ok = ok
        self.raw = raw
        self.error = error
        #: The lifted tags of a message, or a command's arguments and reply.
        self.detail = detail
        #: Which wire encoding produced ``raw``, so that whoever renders it
        #: later reaches for the same codec that recorded it. None for a
        #: control command, which has no wire at all.
        self.protocol = protocol

    def complete(self, ok, error=None, result=None):
        """Fill in how a control command turned out.

        A command is recorded before it runs, so that it precedes the messages
        it causes; this is the other half.
        """
        self.ok = ok
        self.error = error.get("message") if isinstance(error, dict) else error
        if self.detail is not None:
            self.detail["error"] = error
            self.detail["result"] = result

    def describe(self):
        """The list row: enough to recognise an entry without opening it."""
        return {
            "seq": self.seq,
            "time": self.time.isoformat() if self.time is not None else None,
            "kind": self.kind,
            "direction": self.direction,
            "session": self.session,
            "symbol": self.symbol,
            "type": self.type,
            "type_name": self.type_name,
            "seq_num": self.seq_num,
            "ok": self.ok,
            "error": self.error,
            "protocol": self.protocol,
        }


class Page(object):
    """A window onto the tape, and what the caller needs to ask for the next."""

    __slots__ = ("entries", "last_seq", "oldest", "truncated", "capacity")

    def __init__(self, entries, last_seq, oldest, truncated, capacity):
        self.entries = entries
        #: The newest sequence number in the ring, filtered out or not. A tail
        #: advances its cursor to this, so a filter that excludes recent traffic
        #: does not make every poll rescan it.
        self.last_seq = last_seq
        self.oldest = oldest
        #: True when entries between the cursor and the window were skipped, so
        #: a reader is told about the gap instead of silently missing it.
        self.truncated = truncated
        self.capacity = capacity


class Audit(object):
    """A bounded, ordered history of messages and commands."""

    def __init__(self, capacity=DEFAULT_CAPACITY, clock=None):
        self.capacity = capacity
        self.clock = clock
        self._ring = deque(maxlen=capacity)
        self._next_seq = 1
        #: Entries recorded since the process started, evicted ones included.
        self.recorded = 0

    # -- recording ---------------------------------------------------------
    #
    # Both of these run on a path every message takes, so they do the least
    # possible: one small object and one append. Anything that formats, decodes
    # or serialises belongs in the query side.

    def record_message(self, direction, session, message=None, raw=None,
                       type_name=None, extracted=None, symbol=None,
                       error=None, protocol=None):
        """Record one FIX message, in either direction.

        ``message`` is None when the bytes could not be parsed -- a framing or
        checksum failure -- and ``error`` says why. Those entries keep their raw
        bytes, which is the whole reason to have them.
        """
        entry = AuditEntry(
            seq=self._next_seq,
            time=self.clock.now() if self.clock is not None else None,
            kind=KIND_FIX,
            direction=direction,
            session=session,
            symbol=symbol,
            type_=message.msg_type if message is not None else None,
            type_name=type_name,
            seq_num=_int_or_none(extracted, 34) if extracted else None,
            ok=error is None,
            raw=raw,
            error=error,
            detail=extracted,
            protocol=protocol)
        self._append(entry)
        return entry

    def record_command(self, name, args, session=None, ok=None, error=None,
                       result=None, symbol=None):
        """Record one control command, and return the entry.

        ``ok`` is None until :meth:`AuditEntry.complete` says otherwise: the
        caller records the command first so it precedes its own effects, and
        completes it when the handler answers. An entry left at None is one
        whose handler never returned, which is worth being able to see.

        ``error`` may be the control plane's ``{"code", "message"}`` reply; the
        entry keeps only the message, so a list row reads the same whether the
        failure came from a command or from a malformed FIX message, and the
        code stays available in the detail.
        """
        entry = AuditEntry(
            seq=self._next_seq,
            time=self.clock.now() if self.clock is not None else None,
            kind=KIND_CONTROL,
            direction=DIRECTION_IN,
            session=session,
            symbol=symbol,
            type_=name,
            type_name=name,
            ok=ok,
            error=error.get("message") if isinstance(error, dict) else error,
            detail={"args": args, "result": result, "error": error})
        self._append(entry)
        return entry

    def _append(self, entry):
        self._ring.append(entry)
        self._next_seq += 1
        self.recorded += 1

    # -- querying ----------------------------------------------------------

    def entries(self, after=None, before=None, limit=100, symbol=None,
                include_session=True, direction=None, kind=None, types=None,
                exclude_types=None, since=None, until=None):
        """A page of the tape, oldest first.

        Scans **backwards** and stops as soon as it passes ``after``, so a tail
        polling for what is new costs the number of new entries rather than the
        size of the ring. Written left to right it would be the latter, on every
        poll, for every open board -- so do not "simplify" it.
        """
        collected = []
        truncated = False
        for entry in reversed(self._ring):
            if after is not None and entry.seq <= after:
                break
            if before is not None and entry.seq >= before:
                continue
            if not _matches(entry, symbol, include_session, direction, kind,
                            types, exclude_types, since, until):
                continue
            if len(collected) >= limit:
                # There is more between here and the cursor than was asked for.
                truncated = True
                break
            collected.append(entry)

        collected.reverse()

        oldest = self._ring[0].seq if self._ring else None
        if after is not None and oldest is not None and after < oldest - 1:
            # The cursor named an entry the ring has already evicted.
            truncated = True

        return Page(
            entries=collected,
            last_seq=self._next_seq - 1,
            oldest=oldest,
            truncated=truncated,
            capacity=self.capacity)

    def entry(self, seq):
        """One entry by sequence number, or None once it has been evicted.

        Keyed on ``seq`` and never on a position: the ring shifts under a
        reader as new messages arrive.
        """
        for entry in reversed(self._ring):
            if entry.seq == seq:
                return entry
            if entry.seq < seq:
                break
        return None

    def types(self):
        """Every message type and command name the tape currently holds."""
        seen = {}
        for entry in self._ring:
            if entry.type is not None:
                seen.setdefault((entry.kind, entry.type), entry.type_name)
        return [{"kind": kind, "type": type_, "name": name}
                for (kind, type_), name in sorted(seen.items())]

    def clear(self):
        self._ring.clear()

    def describe(self):
        return {
            "capacity": self.capacity,
            "held": len(self._ring),
            "recorded": self.recorded,
            "oldest": self._ring[0].seq if self._ring else None,
            "last_seq": self._next_seq - 1,
        }


def _matches(entry, symbol, include_session, direction, kind, types,
             exclude_types, since, until):
    if symbol is not None and entry.symbol != symbol:
        # A Logon or a Heartbeat names no instrument, and is usually what
        # explains the instrument's traffic, so it stays unless asked otherwise.
        if not (include_session and entry.symbol is None):
            return False
    if direction is not None and entry.direction != direction:
        return False
    if kind is not None and entry.kind != kind:
        return False
    if types is not None and entry.type not in types:
        return False
    # Hiding a type wins over selecting one: the two are set independently, and
    # a reader who asked for heartbeats to be gone means it.
    if exclude_types is not None and entry.type in exclude_types:
        return False
    if since is not None and (entry.time is None or entry.time < since):
        return False
    if until is not None and (entry.time is None or entry.time > until):
        return False
    return True


def _int_or_none(extracted, tag):
    value = extracted.get(tag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
