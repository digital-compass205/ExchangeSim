"""Single-threaded event loop built on :mod:`selectors`.

Chosen over ``asyncio`` deliberately:

* I/O readiness is drained in a defined order, so a scenario replayed twice
  produces the same matching outcome -- which is what makes CI assertions on
  execution reports stable.
* :meth:`Reactor.step` can be pumped synchronously from a unit test, so tests
  drive the whole stack without threads, sleeps, or an event loop.
* Python 3.6 lacks ``asyncio.run`` and ``asyncio.create_task``, and its
  ``loop=`` parameters are a moving target across 3.6/3.7/3.8.

``selectors.DefaultSelector`` is ``EpollSelector`` on RHEL 8 and
``SelectSelector`` on Windows. Nothing here may assume epoll semantics; the
1024-descriptor ceiling that ``select()`` imposes on the dev box is far above
anything a simulator needs.
"""

import errno
import heapq
import itertools
import logging
import os
import selectors
import socket
import time

log = logging.getLogger(__name__)

#: Bytes requested per ``recv`` call.
READ_CHUNK = 65536

# Errors meaning "the socket is fine, just nothing to do right now".
_RETRY = frozenset((errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR))

# Errors meaning the peer is gone. Not an error condition worth logging loudly.
_DISCONNECT = frozenset(
    (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ESHUTDOWN, errno.ENOTCONN)
)

# Errors from a non-blocking connect() meaning "under way, ask again later".
# Collected by name because the two platforms number them differently: Linux
# uses the POSIX values, while Windows returns WSA codes from connect_ex and
# CPython maps errno.EINPROGRESS onto WSAEINPROGRESS there.
_IN_PROGRESS = frozenset(
    value for value in (
        getattr(errno, "EINPROGRESS", None),
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EALREADY", None),
        getattr(errno, "WSAEINPROGRESS", None),
        getattr(errno, "WSAEWOULDBLOCK", None),
        getattr(errno, "WSAEALREADY", None),
    ) if value is not None)


def _set_address_reuse(sock):
    """Make rebinding safe without making port hijacking possible.

    ``SO_REUSEADDR`` means different things on the two target platforms. On
    Linux it only permits rebinding a port left in TIME_WAIT, which is what a
    restartable server wants. On Windows it additionally lets a *second* live
    process bind a port another process is already listening on, with which
    socket receives a connection left undefined -- so a simulator that failed
    to stop would silently share its port with its replacement instead of the
    replacement failing loudly.

    Windows spells the strict behaviour ``SO_EXCLUSIVEADDRUSE``, so use that
    there and ``SO_REUSEADDR`` everywhere else.
    """
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def strerror(err):
    """Describe an errno, including the Winsock codes ``os.strerror`` misses.

    ``connect_ex`` returns WSA codes on Windows (10061 for a refused connect),
    which are outside the table ``os.strerror`` consults -- it answers "Unknown
    error". The symbolic name is far more use in a log or an HTTP response than
    a bare number, and ``errno.errorcode`` does know them.
    """
    text = os.strerror(err)
    if not text or text.lower().startswith("unknown error"):
        return errno.errorcode.get(err, "error %d" % err)
    return text


class Timer(object):
    """Handle for a scheduled callback. Cancel via :meth:`cancel`."""

    __slots__ = ("due", "seq", "callback", "cancelled")

    def __init__(self, due, seq, callback):
        self.due = due
        self.seq = seq
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    # Heap ordering. ``seq`` breaks ties so equal-deadline timers fire in the
    # order they were scheduled, which keeps behaviour reproducible.
    def __lt__(self, other):
        if self.due != other.due:
            return self.due < other.due
        return self.seq < other.seq


class Connection(object):
    """A buffered, non-blocking TCP connection owned by the reactor.

    The owner sets :attr:`on_data` and :attr:`on_close`. Writes are buffered:
    :meth:`send` never blocks and never partially fails from the caller's point
    of view -- anything the kernel would not take is held and flushed when the
    socket reports writable.
    """

    __slots__ = ("sock", "reactor", "peer", "on_data", "on_close", "on_connect",
                 "on_error", "_out", "_closing", "_closed", "_want_write",
                 "_connecting", "data")

    def __init__(self, sock, reactor, peer):
        self.sock = sock
        self.reactor = reactor
        self.peer = peer
        self.on_data = None
        self.on_close = None
        #: Set by :meth:`Reactor.connect` for outbound connections only.
        self.on_connect = None
        self.on_error = None
        self._out = b""
        self._closing = False
        self._closed = False
        self._want_write = False
        self._connecting = False
        #: Free-form slot for the owner to hang state off (e.g. a FIX session).
        self.data = None

    @property
    def closed(self):
        return self._closed

    def send(self, payload):
        # type: (bytes) -> None
        """Queue bytes for transmission."""
        if self._closed or self._closing:
            return
        self._out += payload
        if self._connecting:
            # Nothing can go out yet; _complete_connect flushes what accrued.
            return
        self._flush()

    def close(self):
        """Close immediately, discarding anything still buffered."""
        if self._closed:
            return
        self._closed = True
        self.reactor._unregister(self)
        try:
            self.sock.close()
        except OSError:
            pass
        if self.on_close is not None:
            self.on_close(self)

    def close_when_flushed(self):
        """Close once buffered output has drained.

        Used for a FIX Logout, where the peer must actually receive the message
        before the socket disappears.
        """
        if self._closed:
            return
        self._closing = True
        if not self._out:
            self.close()

    # -- reactor callbacks -------------------------------------------------

    def _on_readable(self):
        try:
            chunk = self.sock.recv(READ_CHUNK)
        except OSError as exc:
            if exc.errno in _RETRY:
                return
            if exc.errno not in _DISCONNECT:
                log.warning("recv from %s failed: %s", self.peer, exc)
            self.close()
            return
        if not chunk:
            self.close()
            return
        if self.on_data is not None:
            self.on_data(self, chunk)

    def _on_writable(self):
        if self._connecting:
            self._complete_connect()
            return
        self._flush()

    # -- outbound connection establishment ---------------------------------

    def _complete_connect(self):
        """Writability on a connecting socket means the attempt has finished.

        It does **not** mean it succeeded. A refused connect also reports the
        socket writable -- on Windows it arrives in the exception fd set, which
        ``selectors.SelectSelector`` folds into the write list -- so ``SO_ERROR``
        is the only thing that distinguishes the two.
        """
        err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            self._fail_connect(err)
            return

        self._connecting = False
        try:
            self.peer = self.sock.getpeername()
        except OSError:
            pass  # cosmetic; the connection is established either way

        self._want_write = False
        self.reactor._modify(self)
        log.debug("connected to %s", self.peer)

        if self.on_connect is not None:
            self.on_connect(self)
        if self._out:
            self._flush()

    def _fail_connect(self, err):
        if self._closed:
            return
        self._connecting = False
        handler = self.on_error
        reason = strerror(err)
        log.debug("connect to %s failed: %s", self.peer, reason)
        self.close()
        if handler is not None:
            handler(self, OSError(err, reason))

    def _flush(self):
        while self._out:
            try:
                sent = self.sock.send(self._out)
            except OSError as exc:
                if exc.errno in _RETRY:
                    break
                if exc.errno not in _DISCONNECT:
                    log.warning("send to %s failed: %s", self.peer, exc)
                self.close()
                return
            if sent == 0:
                break
            self._out = self._out[sent:]

        if self._out:
            self._set_want_write(True)
        else:
            self._set_want_write(False)
            if self._closing:
                self.close()

    def _set_want_write(self, want):
        if want == self._want_write or self._closed:
            return
        self._want_write = want
        self.reactor._modify(self)

    def _events(self):
        if self._connecting:
            # Readiness on a socket that has not connected yet is meaningless
            # except for write, which is how completion is signalled.
            return selectors.EVENT_WRITE
        events = selectors.EVENT_READ
        if self._want_write:
            events |= selectors.EVENT_WRITE
        return events


class Listener(object):
    """A listening socket. ``on_accept(connection)`` is called per client."""

    __slots__ = ("sock", "reactor", "on_accept", "address")

    def __init__(self, sock, reactor, address, on_accept):
        self.sock = sock
        self.reactor = reactor
        self.address = address
        self.on_accept = on_accept

    def close(self):
        self.reactor._unregister_listener(self)
        try:
            self.sock.close()
        except OSError:
            pass

    def _on_readable(self):
        # Drain the backlog; a single readiness event can cover several peers.
        while True:
            try:
                sock, peer = self.sock.accept()
            except OSError as exc:
                if exc.errno in _RETRY:
                    return
                log.warning("accept on %s failed: %s", self.address, exc)
                return
            conn = self.reactor._adopt(sock, peer)
            try:
                self.on_accept(conn)
            except Exception:
                log.exception("accept handler raised for %s", peer)
                conn.close()


class Reactor(object):
    """The event loop.

    Call :meth:`run` to serve, or pump :meth:`step` by hand from a test.
    """

    def __init__(self, clock):
        self._clock = clock
        self._selector = selectors.DefaultSelector()
        self._timers = []          # heap of Timer
        self._timer_seq = itertools.count()
        self._running = False
        self._connections = set()
        self._listeners = set()

    @property
    def clock(self):
        return self._clock

    @property
    def connections(self):
        return frozenset(self._connections)

    # -- setup -------------------------------------------------------------

    def listen(self, host, port, on_accept, backlog=64):
        # type: (str, int, callable, int) -> Listener
        """Open a listening socket and register it."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _set_address_reuse(sock)
        sock.setblocking(False)
        sock.bind((host, port))
        sock.listen(backlog)
        listener = Listener(sock, self, sock.getsockname(), on_accept)
        self._selector.register(sock, selectors.EVENT_READ, listener)
        self._listeners.add(listener)
        log.info("listening on %s:%d", listener.address[0], listener.address[1])
        return listener

    def connect(self, host, port, on_connect, on_error=None):
        # type: (str, int, callable, callable) -> Connection
        """Start an outbound connection, returning immediately.

        ``on_connect(conn)`` fires once the socket is established and
        ``on_error(conn, exc)`` if it never will be; neither is called before
        this returns, so the caller can store the connection first. Bytes handed
        to :meth:`Connection.send` in the meantime are buffered and flushed on
        connection.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        conn = Connection(sock, self, (host, port))
        conn.on_connect = on_connect
        conn.on_error = on_error
        conn._connecting = True
        self._connections.add(conn)

        err = sock.connect_ex((host, port))
        if err == 0 or err in _IN_PROGRESS:
            # Even an immediately completed connect is left for the loop to
            # notice: a connected socket is writable at once, so this costs one
            # step and keeps success and failure on a single code path.
            self._selector.register(sock, selectors.EVENT_WRITE, conn)
        else:
            # Refused outright. Report on the next turn rather than re-entrantly.
            self.call_later(0, lambda: conn._fail_connect(err))

        return conn

    def _adopt(self, sock, peer):
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass  # not fatal; only affects latency, which we do not test for
        conn = Connection(sock, self, peer)
        self._selector.register(sock, conn._events(), conn)
        self._connections.add(conn)
        return conn

    def _modify(self, conn):
        if conn._closed:
            return
        try:
            self._selector.modify(conn.sock, conn._events(), conn)
        except (KeyError, ValueError, OSError):
            pass

    def _unregister(self, conn):
        self._connections.discard(conn)
        try:
            self._selector.unregister(conn.sock)
        except (KeyError, ValueError, OSError):
            pass

    def _unregister_listener(self, listener):
        self._listeners.discard(listener)
        try:
            self._selector.unregister(listener.sock)
        except (KeyError, ValueError, OSError):
            pass

    # -- timers ------------------------------------------------------------

    def call_later(self, delay, callback):
        # type: (float, callable) -> Timer
        """Schedule ``callback`` to run ``delay`` seconds from now."""
        return self.call_at(self._clock.monotonic() + delay, callback)

    def call_at(self, when, callback):
        # type: (float, callable) -> Timer
        """Schedule ``callback`` for a monotonic deadline."""
        timer = Timer(when, next(self._timer_seq), callback)
        heapq.heappush(self._timers, timer)
        return timer

    def _expire_timers(self):
        now = self._clock.monotonic()
        fired = 0
        while self._timers and self._timers[0].due <= now:
            timer = heapq.heappop(self._timers)
            if timer.cancelled:
                continue
            try:
                timer.callback()
            except Exception:
                log.exception("timer callback failed")
            fired += 1
        return fired

    def _next_timeout(self, default):
        while self._timers and self._timers[0].cancelled:
            heapq.heappop(self._timers)
        if not self._timers:
            return default
        delay = self._timers[0].due - self._clock.monotonic()
        if delay < 0:
            return 0.0
        if default is not None and default < delay:
            return default
        return delay

    # -- running -----------------------------------------------------------

    def step(self, timeout=0.0):
        # type: (float) -> int
        """Run one iteration: expire due timers, then service ready sockets.

        Returns the number of callbacks invoked. A test can loop on this until
        it returns 0 to reach quiescence, with no wall-clock waiting at all.
        """
        handled = self._expire_timers()

        delay = self._next_timeout(timeout)

        if not self._selector.get_map():
            # Windows' select() raises WSAEINVAL when every fd set is empty,
            # unlike epoll which happily waits. Honour the delay by sleeping so
            # a timer-only reactor neither crashes nor busy-loops.
            if delay:
                time.sleep(delay)
            return handled + self._expire_timers()

        try:
            events = self._selector.select(delay)
        except OSError as exc:
            if exc.errno in _RETRY:
                return handled
            raise

        for key, mask in events:
            target = key.data
            if isinstance(target, Listener):
                target._on_readable()
                handled += 1
                continue
            # A handler earlier in this batch may have closed this connection.
            if target.closed:
                continue
            if mask & selectors.EVENT_WRITE:
                target._on_writable()
                handled += 1
            if mask & selectors.EVENT_READ and not target.closed:
                target._on_readable()
                handled += 1

        handled += self._expire_timers()
        return handled

    def run(self, poll_interval=0.5):
        """Serve until :meth:`stop` is called."""
        self._running = True
        while self._running:
            self.step(poll_interval)

    def stop(self):
        self._running = False

    def close(self):
        """Shut down every socket and release the selector."""
        for conn in list(self._connections):
            conn.close()
        for listener in list(self._listeners):
            listener.close()
        self._selector.close()
