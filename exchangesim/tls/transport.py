"""A TLS transport built on ``ssl.SSLObject`` and two ``ssl.MemoryBIO``\\ s,
implementing the duck type ``exchangesim.core.reactor._PlainTransport``
defines.

**Why ``MemoryBIO`` and not ``ssl.wrap_socket`` / ``SSLContext.wrap_socket``.**
An ``SSLSocket`` decrypts eagerly and keeps whatever it decrypted that the
caller has not yet read *inside the SSL object*, where ``select()`` cannot see
it. A reactor asks the OS "is this file descriptor readable" -- and once the
last ciphertext byte for a record has been drained off the socket, the
descriptor genuinely has nothing left to offer, even though the SSL object is
sitting on a complete decrypted message. Read that once and the reactor goes
back to sleep waiting for a socket event that already happened, with a message
delivered late or, under a protocol that pipelines requests, not delivered
until some unrelated later event nudges the object again. The standard fix is
a ``pending()`` drain loop run after every read, which is easy to write and
easy to get subtly wrong -- forget it in one call site and that site starves.

A ``MemoryBIO`` has no such hidden buffer. Ciphertext off the wire is written
into ``incoming`` and read back out of ``outgoing`` explicitly, at times the
caller chooses; nothing is held inside the ``SSLObject`` that the caller did
not just ask for. Everything the transport has that has not yet been fed
somewhere is either bytes the *caller* is holding (this module's own
``_pending`` queue, always visible) or bytes still on the far side of the
network, which no design can see. ``asyncio.sslproto`` is built the same way,
over the same primitives, for the same reason: an event loop's readiness
model and a socket-shaped object that buffers behind the loop's back do not
mix.

Nothing here is version-gated. ``SSLContext.wrap_bio``, ``MemoryBIO.write_eof``
and ``ssl.SSLZeroReturnError`` are present, and behave identically, on both
Python 3.6.8 (OpenSSL 1.0.2q) and Python 3.14 (OpenSSL 3.5.7) -- verified
directly against both interpreters this project targets.
"""

import ssl


class TlsTransport(object):
    """One TLS connection, wired to a reactor ``Connection`` as its transport.

    Construction takes an ``ssl.SSLContext`` built by
    :mod:`exchangesim.tls.context` (or a test's own) and mirrors
    ``SSLContext.wrap_bio``'s own ``server_side`` / ``server_hostname``
    arguments -- a client transport needs ``server_hostname`` for both SNI and
    hostname verification, exactly as a real socket-based client would.

    The whole object is bytes in, bytes out: nothing here touches a socket,
    which is what lets :mod:`tests.test_tls_transport` drive two of these
    against each other with no network at all.
    """

    __slots__ = (
        "_incoming", "_outgoing", "_ssl", "_pending", "handshaken",
        "description", "_closed",
    )

    def __init__(self, context, server_side=True, server_hostname=None):
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._ssl = context.wrap_bio(
            self._incoming, self._outgoing,
            server_side=server_side, server_hostname=server_hostname)
        #: Plaintext queued by :meth:`transmit` before the handshake
        #: completes, or after a short ``SSLObject.write`` that could not
        #: take the whole payload without blocking on a read first.
        self._pending = b""
        self.handshaken = False
        self.description = None
        #: Set once the peer's close_notify has been seen, so a second
        #: read after that point is a no-op rather than a fresh attempt.
        self._closed = False

        # Kick the state machine once, right away. A client has to speak
        # first in TLS -- there is no incoming byte that would otherwise
        # prompt anything -- so the first attempt is made here rather than
        # waiting for a call to receive(). It is harmless on the server
        # side too: with nothing yet in the incoming BIO this simply raises
        # SSLWantReadError immediately and produces no bytes. Any other
        # exception here is a real misconfiguration (missing certificate on
        # a server context, and the like) and is left to propagate out of
        # construction rather than being deferred to a confusing failure on
        # the first receive().
        self._do_handshake()

    def receive(self, ciphertext):
        # type: (bytes) -> bytes
        """Feed ciphertext in, return whatever plaintext falls out.

        An empty ``ciphertext`` is EOF on the underlying connection, not "no
        bytes arrived this time" -- the caller (the reactor, or a test) says
        so explicitly by calling this with ``b""``, and that is signalled to
        the ``SSLObject`` via ``write_eof`` so a handshake or a read blocked
        on more data fails cleanly instead of hanging forever.
        """
        if self._closed:
            return b""
        if ciphertext:
            self._incoming.write(ciphertext)
        else:
            self._incoming.write_eof()

        if not self.handshaken:
            if not self._do_handshake():
                return b""
            # The handshake just completed: anything queued by transmit()
            # while it was in flight can go out now.
            self._flush_pending()

        return self._read_plaintext()

    def _do_handshake(self):
        # type: () -> bool
        """Attempt to advance the handshake. Returns whether it completed.

        ``SSLWantReadError`` / ``SSLWantWriteError`` both mean "more records
        needed before this can proceed" -- under ``MemoryBIO`` neither implies
        an actual socket read or write, only that :meth:`drain` must be called
        to collect what this attempt produced. Any other exception is a real
        protocol failure (bad certificate, version mismatch, corrupt record)
        and is left to propagate, which is what lets the reactor see it, log
        it and drop the connection rather than the failure being silently
        swallowed here.
        """
        try:
            self._ssl.do_handshake()
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            return False
        self.handshaken = True
        self.description = "%s / %s" % (
            self._ssl.version(), self._ssl.cipher()[0])
        return True

    def _read_plaintext(self):
        # type: () -> bytes
        chunks = []
        while True:
            try:
                chunk = self._ssl.read(65536)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                self._closed = True
                break
            if not chunk:
                # An empty read with no exception is the other spelling of
                # "the peer sent close_notify" that some OpenSSL versions use
                # in place of raising SSLZeroReturnError.
                self._closed = True
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def transmit(self, plaintext):
        # type: (bytes) -> None
        """Queue plaintext for the peer.

        Before the handshake completes there is nothing to write it *into*
        yet -- ``SSLObject.write`` would itself raise ``SSLWantReadError`` --
        so it is simply held. After, it is written through the ``SSLObject``
        immediately; :meth:`drain` is what actually moves the resulting
        ciphertext out to the caller.
        """
        self._pending += plaintext
        if self.handshaken:
            self._flush_pending()

    def _flush_pending(self):
        # type: () -> None
        """Push as much of ``_pending`` through the ``SSLObject`` as will go
        without blocking on a read, holding the remainder for next time.

        ``SSLObject.write`` can return a short count -- it is documented to,
        same as a raw socket -- so this loops on what is left rather than
        assuming one call disposes of the whole buffer. If it raises
        ``SSLWantReadError`` (renegotiation-shaped record traffic needed
        first) the untransmitted remainder simply stays queued; nothing is
        lost.
        """
        while self._pending:
            try:
                sent = self._ssl.write(self._pending)
            except ssl.SSLWantReadError:
                break
            self._pending = self._pending[sent:]

    def drain(self):
        # type: () -> bytes
        """Ciphertext ready to put on the wire -- handshake records, queued
        application data, or a close_notify. May be empty."""
        return self._outgoing.read()

    def close_notify(self):
        # type: () -> bytes
        """Start a graceful shutdown and return whatever that produced.

        ``SSLObject.unwrap`` raises ``SSLWantReadError`` when the peer has not
        yet answered with its own close_notify -- normal mid-shutdown
        behaviour, not a failure, so it is caught rather than left to
        propagate. Any bytes ``unwrap`` managed to queue before wanting a read
        (our own close_notify record) are still collected via :meth:`drain`.
        This method never raises.
        """
        try:
            self._ssl.unwrap()
        except ssl.SSLWantReadError:
            pass
        except ssl.SSLError:
            pass
        return self.drain()
