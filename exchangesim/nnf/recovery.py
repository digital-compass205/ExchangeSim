"""What a signed-on user missed, and the clock that orders it.

NNF has no resend. A message produced while a user is disconnected is dropped
rather than queued (see :mod:`exchangesim.nnf.session`), and the client gets it
back by *asking*: ``DOWNLOAD_REQUEST (7000)`` names a stream and a cursor, and
the exchange replays what came after it. This module is the store that makes
that answerable, and the two conventions the cursor rests on.

**The cursor is the header's own ``TimeStamp1``.** "This field should contain
the time when last message was received by the workstation. This can be
obtained from the Time Stamp1 of the MESSAGE_HEADER" -- so a client does not
count messages, it remembers the last stamp it saw and asks for everything
after. That makes ``TimeStamp1`` load-bearing rather than decorative: a
simulator that leaves it at zero (as this one did) gives a client no cursor to
advance and no way to ask for only what it missed.

**Its unit is the jiffy**, 1/65536 of a second, and it is eight bytes at the
host end. The document says a front end "typecast[s] the first four and the
next four bytes into double and store[s] each of these in separate variables",
which is a thirty-two-bit front end's way of holding a sixty-four bit count,
not two separate quantities: the download request carries the whole thing back
in one ``DOUBLE``, and a jiffy count from 1980 needs about forty-seven bits --
comfortably exact in a double, which holds integers to 2**53.

The epoch it counts from is an assumption; see ``types.to_nse_jiffies``.

**A stored message is the one that was sent, not the bytes that went out.**
The two differ, and must: a downloaded message is always the *non-trimmed*
form, so a venue that answers a live order over the compact ``_TR`` structures
still replays the full ones. Keeping the :class:`Message` rather than the
encoded frame is what lets the record be re-rendered under a different layout;
it is also what keeps ciphertext out of the store, since the frame that
travelled was encrypted.
"""

import logging

from .types import to_nse_jiffies

log = logging.getLogger(__name__)

#: How many messages one user can recover, unless a venue says otherwise. A
#: ring rather than a day's worth: a simulator that never forgets is a
#: simulator that runs out of memory in a soak test, and a client that has
#: fallen this far behind has lost its place anyway.
DEFAULT_CAPACITY = 500


class MessageStore(object):
    """Per user, the messages a download can replay, in the order they went.

    Bounded per user rather than in total, so that one busy user cannot push
    another's recovery off the end -- which would be a silent, timing-dependent
    hole in exactly the mechanism that exists to close holes.
    """

    def __init__(self, capacity=DEFAULT_CAPACITY, recoverable=None,
                 normalise=None):
        self.capacity = int(capacity)
        #: ``recoverable(msg_type) -> bool``. The venue's, because which
        #: transaction codes are a user's own traffic is the venue's fact --
        #: and because the download's own three codes must not be stored, or a
        #: second download would replay the first one.
        self._recoverable = recoverable or (lambda msg_type: True)
        #: ``normalise(message) -> message``, applied on the way in. This is
        #: where "a downloaded message is always the non-trimmed message" is
        #: honoured: a venue that answered live in the compact ``_TR``
        #: structures files the full form instead, because a ``_TR`` structure
        #: has no forty-byte header and so could never be wrapped in a record.
        #: Done on the way in rather than on the way out so that the cost is
        #: paid once per message rather than once per download.
        self._normalise = normalise or (lambda message: message)
        self._records = {}              # user_id -> [(sequence, Message)]
        self._last = 0                  # the highest sequence handed out

    # -- writing -----------------------------------------------------------

    def sequence(self, unix_seconds):
        """The next cursor value, never repeating and never going backwards.

        A jiffy is 15 microseconds and a simulator can answer an order in less,
        so two messages can share one reading of the clock. A cursor that
        repeats would make ``after`` either drop a message or replay one
        forever, depending on which side of the comparison it fell, so a
        collision is broken by stepping one jiffy on instead.
        """
        value = to_nse_jiffies(unix_seconds)
        if value <= self._last:
            value = self._last + 1
        self._last = value
        return value

    def record(self, user_id, sequence, message):
        """Keep one message against a user, if its type is recoverable."""
        if not self._recoverable(message.msg_type):
            return False
        records = self._records.setdefault(int(user_id), [])
        records.append((sequence, self._normalise(message)))
        if len(records) > self.capacity:
            del records[:len(records) - self.capacity]
        return True

    # -- reading -----------------------------------------------------------

    def after(self, user_id, sequence):
        """Every message this user was sent after ``sequence``, in order.

        ``sequence`` of zero means the whole trading day, which is what the
        document says a client sends when it has nothing to resume from.
        """
        records = self._records.get(int(user_id), ())
        return [(stamp, message) for stamp, message in records
                if stamp > sequence]

    def count(self, user_id):
        return len(self._records.get(int(user_id), ()))

    def truncated(self, user_id, sequence):
        """Whether the ring has already dropped messages this cursor wanted.

        Answering "nothing since" and "everything you asked for fell off the
        end" with the same empty download would be a lie a client cannot
        detect, so a venue can log the difference.
        """
        records = self._records.get(int(user_id), ())
        return bool(records) and len(records) == self.capacity \
            and sequence < records[0][0]

    def forget(self, user_id):
        """Drop one user's history. Used by the control plane, not the wire."""
        return len(self._records.pop(int(user_id), ()))

    def clear(self):
        self._records = {}

    def describe(self):
        return {"capacity": self.capacity,
                "users": {str(user): len(records)
                          for user, records in sorted(self._records.items())}}

    def __repr__(self):
        return "MessageStore(%d users, capacity %d)" % (len(self._records),
                                                        self.capacity)
