"""Sequence-number and outbound-message persistence.

A FIX session's sequence numbers must survive a process restart, and every
outbound message must remain retrievable so a client's ResendRequest can be
answered. Without this, a client cannot exercise its own recovery logic against
the simulator -- which is one of the things the simulator exists to test.

Layout on disk, one directory per session::

    <root>/<SenderCompID>-<TargetCompID>/
        state.json   {"next_out": N, "next_in": M, ...}
        out.log      length-prefixed outbound message records

``out.log`` records are ``<seq> <length>\\n<raw bytes>\\n``. The length prefix
means the reader never has to guess where a message ends, so a raw FIX body
containing any byte at all round-trips safely.
"""

import json
import logging
import os

log = logging.getLogger(__name__)


class Store(object):
    """Interface for sequence-number and message persistence."""

    def open(self):
        raise NotImplementedError

    def close(self):
        pass

    @property
    def next_out(self):
        raise NotImplementedError

    @property
    def next_in(self):
        raise NotImplementedError

    def set_next_out(self, value):
        raise NotImplementedError

    def set_next_in(self, value):
        raise NotImplementedError

    def store_outbound(self, seq, raw):
        raise NotImplementedError

    def get_outbound(self, begin, end):
        """Stored messages with ``begin <= seq <= end``, ascending.

        ``end`` of 0 means "through the end", matching FIX's EndSeqNo
        convention for an open-ended resend request.
        """
        raise NotImplementedError

    def reset(self):
        """Discard history and return both sequence numbers to 1."""
        raise NotImplementedError


class MemoryStore(Store):
    """Non-persistent store. Used by unit tests and by throwaway sessions."""

    def __init__(self):
        self._next_out = 1
        self._next_in = 1
        self._messages = {}

    def open(self):
        return self

    @property
    def next_out(self):
        return self._next_out

    @property
    def next_in(self):
        return self._next_in

    def set_next_out(self, value):
        self._next_out = int(value)

    def set_next_in(self, value):
        self._next_in = int(value)

    def store_outbound(self, seq, raw):
        self._messages[int(seq)] = raw

    def get_outbound(self, begin, end):
        upper = max(self._messages) if (end in (0, None) and self._messages) else end
        if upper is None:
            upper = 0
        return [(seq, self._messages[seq])
                for seq in sorted(self._messages)
                if begin <= seq <= upper]

    def reset(self):
        self._next_out = 1
        self._next_in = 1
        self._messages.clear()


class FileStore(Store):
    """Durable store backed by a directory.

    The outbound log is indexed in memory on :meth:`open` -- at simulator
    volumes a full scan costs nothing and removes a whole class of index-drift
    bugs that a separate index file would introduce.
    """

    STATE_FILE = "state.json"
    LOG_FILE = "out.log"

    def __init__(self, directory):
        self.directory = directory
        self._state_path = os.path.join(directory, self.STATE_FILE)
        self._log_path = os.path.join(directory, self.LOG_FILE)
        self._next_out = 1
        self._next_in = 1
        self._offsets = {}       # seq -> (offset, length) within out.log
        self._log = None
        self._opened = False

    # -- lifecycle ---------------------------------------------------------

    def open(self):
        if self._opened:
            return self
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)
        self._load_state()
        self._index_log()
        self._log = open(self._log_path, "ab")
        self._opened = True
        log.debug("store %s opened: next_out=%d next_in=%d messages=%d",
                  self.directory, self._next_out, self._next_in, len(self._offsets))
        return self

    def close(self):
        if self._log is not None:
            try:
                self._log.flush()
                self._log.close()
            except (IOError, OSError):
                pass
            self._log = None
        self._opened = False

    # -- sequence numbers --------------------------------------------------

    @property
    def next_out(self):
        return self._next_out

    @property
    def next_in(self):
        return self._next_in

    def set_next_out(self, value):
        self._next_out = int(value)
        self._save_state()

    def set_next_in(self, value):
        self._next_in = int(value)
        self._save_state()

    # -- messages ----------------------------------------------------------

    def store_outbound(self, seq, raw):
        if self._log is None:
            raise RuntimeError("store is not open")
        header = b"%d %d\n" % (int(seq), len(raw))
        offset = self._log.tell() + len(header)
        self._log.write(header)
        self._log.write(raw)
        self._log.write(b"\n")
        self._log.flush()
        self._offsets[int(seq)] = (offset, len(raw))

    def get_outbound(self, begin, end):
        if not self._offsets:
            return []
        upper = max(self._offsets) if end in (0, None) else end
        wanted = [seq for seq in sorted(self._offsets) if begin <= seq <= upper]
        if not wanted:
            return []

        results = []
        with open(self._log_path, "rb") as handle:
            for seq in wanted:
                offset, length = self._offsets[seq]
                handle.seek(offset)
                results.append((seq, handle.read(length)))
        return results

    def reset(self):
        self._next_out = 1
        self._next_in = 1
        self._offsets = {}
        if self._log is not None:
            self._log.close()
        # Truncate rather than unlink: the file handle layout stays identical
        # whether or not the session has ever run before.
        self._log = open(self._log_path, "wb")
        self._log.flush()
        self._log.close()
        self._log = open(self._log_path, "ab")
        self._save_state()
        log.info("store %s reset", self.directory)

    # -- persistence -------------------------------------------------------

    def _load_state(self):
        if not os.path.isfile(self._state_path):
            return
        try:
            with open(self._state_path, "r") as handle:
                state = json.load(handle)
        except (IOError, OSError, ValueError) as exc:
            log.warning("unreadable store state %s (%s); starting from 1",
                        self._state_path, exc)
            return
        self._next_out = int(state.get("next_out", 1))
        self._next_in = int(state.get("next_in", 1))

    def _save_state(self):
        state = {"next_out": self._next_out, "next_in": self._next_in}
        temp = self._state_path + ".tmp"
        try:
            with open(temp, "w") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic on POSIX, and os.replace overwrites on Windows too, so a
            # crash mid-write can never leave a truncated state file.
            os.replace(temp, self._state_path)
        except (IOError, OSError) as exc:
            log.error("cannot persist store state %s: %s", self._state_path, exc)

    def _index_log(self):
        self._offsets = {}
        if not os.path.isfile(self._log_path):
            return
        try:
            with open(self._log_path, "rb") as handle:
                while True:
                    header = handle.readline()
                    if not header:
                        break
                    try:
                        seq_text, length_text = header.split()
                        seq = int(seq_text)
                        length = int(length_text)
                    except ValueError:
                        log.warning("truncated record in %s; ignoring the tail",
                                    self._log_path)
                        break
                    offset = handle.tell()
                    body = handle.read(length)
                    if len(body) < length:
                        log.warning("truncated message %d in %s; ignoring the tail",
                                    seq, self._log_path)
                        break
                    handle.read(1)  # trailing newline
                    self._offsets[seq] = (offset, length)
        except (IOError, OSError) as exc:
            log.error("cannot index %s: %s", self._log_path, exc)


def session_directory(root, sender_comp_id, target_comp_id):
    """Directory name for a session, safe for use as a filesystem path."""
    return os.path.join(root, "%s-%s" % (_safe(sender_comp_id), _safe(target_comp_id)))


def _safe(text):
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in text)
