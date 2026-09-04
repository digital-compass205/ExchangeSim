"""The Gateway Router: a second listener that hands out the keys.

Chapter 10's connection sequence has two legs. A member dials this port first,
sends ``GR_REQUEST (2400)`` naming its Box ID, and receives ``GR_RESPONSE
(2401)`` carrying the address of the gateway, a session key, a 256-bit
cryptographic key, a 128-bit IV and a 96-bit additional key. Only then does it
open the *gateway* connection those secrets encrypt.

Keeping it a separate listener rather than a special case of the gateway is not
tidiness: the two legs are different connections in the specification, they use
different security, and a member may dial the router again after a disconnect to
collect fresh secrets -- "in the event of a box disconnection, the IVs are reset
at exchange end, and a new static and dynamic IV is provided in GR response
message to a fresh GR query".

**This leg is not encrypted, and the specification requires TLS 1.3 on it.**
The reason is worth stating rather than burying: this project's reactor is a
single-threaded ``selectors`` loop with no TLS support, and giving it some means
a non-blocking handshake state machine inside the most load-bearing module in
the tree. That is a change with its own design, not a detail of adding a venue,
so ``gateway_router.tls`` refuses anything but ``"none"`` today rather than
quietly serving plain TCP under a setting that claims otherwise. It is recorded
in ``rules.ASSUMPTIONS`` and in ``docs/specs/README.md``.

What a client loses by that is confidentiality on the key exchange. What it
keeps is the whole message flow -- the same transaction codes, the same
structures, the same secrets, and the same AES-256-GCM on every gateway message
afterwards.
"""

import logging
import os

from ...core.config import ConfigError
from ...fix.message import MalformedMessage, Message
from ...nnf import crypto
from ...nnf.codec import NnfCodec
from . import dictionary as D
from . import transactions as X

log = logging.getLogger(__name__)

#: What ``gateway_router.tls`` accepts. The other two values the specification
#: would want are refused rather than faked -- see the module docstring.
SUPPORTED_TLS = ("none",)


class Secrets(object):
    """What one box was issued, and the cipher it implies.

    Held by the venue rather than by either listener, because the two legs are
    different connections: the router writes this and the gateway reads it.
    """

    __slots__ = ("box_id", "session_key", "key", "iv", "additional_key",
                 "methodology")

    def __init__(self, box_id, session_key, key, iv, additional_key,
                 methodology="new"):
        self.box_id = box_id
        #: The eight-byte token a box quotes back in BOX_SIGN_ON_REQUEST_IN.
        self.session_key = session_key
        self.key = key
        self.iv = iv
        self.additional_key = additional_key
        #: "existing" (GCM as a keystream, MD5 for integrity) or "new"
        #: (authenticated GCM, the tag in the packet prefix).
        self.methodology = methodology

    def exchange_cipher(self):
        """The exchange half of the pair. The member builds the other."""
        if self.methodology == "existing":
            return crypto.ExistingCipher(self.key, self.iv)
        return crypto.NewCipher(self.key, self.iv, self.additional_key)

    def member_cipher(self):
        """The member half, for a test harness or the scenario runner."""
        if self.methodology == "existing":
            return crypto.ExistingCipher(self.key, self.iv)
        return crypto.NewCipher(self.key, self.iv, self.additional_key,
                                client=True)

    def describe(self):
        """Everything but the secrets themselves."""
        return {"box_id": self.box_id, "methodology": self.methodology,
                "issued": True}


def issue(box_id, methodology="new", entropy=None):
    """Fresh secrets for one box.

    ``entropy`` is injectable so a test can pin the bytes; otherwise they come
    from ``os.urandom``. The dynamic half of the IV starts at a random value
    rather than zero, so that two boxes issued in the same second do not walk
    the same counter.
    """
    source = entropy or os.urandom
    return Secrets(
        box_id=int(box_id),
        session_key=_printable(source(8)),
        key=source(crypto.KEY_BYTES),
        iv=source(crypto.IV_BYTES),
        additional_key=source(crypto.AAD_BYTES),
        methodology=methodology)


def _printable(raw):
    """Eight printable characters, since SessionKey travels as CHAR[8]."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(alphabet[byte % len(alphabet)] for byte in bytearray(raw))


class GatewayRouter(object):
    """The listener that answers ``GR_REQUEST`` and nothing else."""

    def __init__(self, venue, config):
        self.venue = venue
        self.codec = NnfCodec(venue.layouts)
        self.methodology = config.get("encryption", "new")
        if self.methodology not in ("existing", "new"):
            raise ConfigError(
                "gateway_router.encryption must be 'existing' or 'new', not %r"
                % self.methodology)

        tls = str(config.get("tls", "none")).lower()
        if tls not in SUPPORTED_TLS:
            raise ConfigError(
                "gateway_router.tls only supports %s. The specification asks "
                "for TLS 1.3 on this leg; this simulator's reactor has no TLS "
                "support, and serving plain TCP under a setting that says "
                "'1.3' would be worse than refusing. See rules.ASSUMPTIONS."
                % ", ".join(repr(value) for value in SUPPORTED_TLS))
        self.tls = tls
        self._listener = None
        self._gateway = None

    @property
    def address(self):
        return self._listener.address if self._listener else None

    def start(self, host, port):
        self._listener = self.venue.reactor.listen(host, port, self._on_accept)
        self._gateway = self.venue.acceptor.address
        log.info("gateway router on %s:%d (tls=%s, %s encryption)",
                 self._listener.address[0], self._listener.address[1],
                 self.tls, self.methodology)
        return self._listener.address

    def stop(self):
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    # -- one very short conversation ---------------------------------------

    def _on_accept(self, conn):
        state = {"buffer": b""}

        def on_data(_conn, chunk):
            state["buffer"] += chunk
            try:
                raw, state["buffer"] = self.codec.extract(state["buffer"])
            except MalformedMessage as exc:
                log.warning("gateway router: unframeable request: %s", exc)
                conn.close()
                return
            except Exception:                   # IncompleteMessage
                return
            self._answer(conn, raw)

        conn.on_data = on_data
        conn.on_close = lambda _c: None

    def _answer(self, conn, raw):
        try:
            request = self.codec.decode(raw)
        except MalformedMessage as exc:
            log.warning("gateway router: malformed request: %s", exc)
            conn.close()
            return

        self._record(request, raw)

        if request.msg_type != str(X.GR_REQUEST):
            log.warning("gateway router: %s is not a GR_REQUEST",
                        request.msg_type)
            conn.close_when_flushed()
            return

        box_id = _int(request.get(D.BOX_ID))
        broker_id = request.get(D.BROKER_ID)
        response = Message.create(str(X.GR_RESPONSE))
        response.set(D.BOX_ID, str(box_id or 0))
        response.set(D.BROKER_ID, broker_id or "")

        if box_id is None or box_id not in self.venue._boxes:
            response.set(D.ERROR_CODE, "16588")     # USER_IP_REC_NOT_FOUND
            log.warning("gateway router: box %s is not configured", box_id)
        elif broker_id not in self.venue.brokers_of(box_id):
            response.set(D.ERROR_CODE, "16041")     # INVALID_BROKER_OR_BRANCH
            log.warning("gateway router: box %d is not registered to broker %s",
                        box_id, broker_id)
        else:
            self._grant(response, box_id)

        out = self.codec.encode(response)
        self._record(response, out, outbound=True)
        conn.send(out)
        conn.close_when_flushed()

    def _grant(self, response, box_id):
        secrets = issue(box_id, self.methodology)
        self.venue.issue(box_id, secrets)

        host, port = self._gateway or ("127.0.0.1", 0)
        response.set(D.ERROR_CODE, str(X.NO_ERROR))
        response.set(D.IP_ADDRESS, host)
        response.set(D.PORT, str(port))
        response.set(D.SESSION_KEY, secrets.session_key)
        response.set(D.CRYPTOGRAPHIC_KEY, _latin(secrets.key))
        response.set(D.STATIC_IV, _latin(secrets.iv[:8]))
        response.set(D.DYNAMIC_IV, str(_signed64(secrets.iv[8:])))
        response.set(D.ADDITIONAL_KEY, _latin(secrets.additional_key))
        log.info("gateway router: issued %s-encryption secrets to box %d",
                 self.methodology, box_id)

    def _record(self, message, raw, outbound=False):
        from ...audit import DIRECTION_IN, DIRECTION_OUT
        audit = self.venue.audit
        if audit is None:
            return
        definition = self.venue.dictionary.message(message.msg_type)
        audit.record_message(
            DIRECTION_OUT if outbound else DIRECTION_IN,
            "gateway router", message=message, raw=raw,
            type_name=definition.name if definition is not None else None,
            protocol="nnf")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _latin(raw):
    """Raw key bytes as the CHAR field that carries them.

    A key is bytes, and ``CHAR[32]`` is a byte run of the same width, so the
    two map one to one through latin-1 -- the encoding that is the identity on
    bytes. Anything narrower would corrupt a key with a byte above 0x7F.
    """
    return bytes(raw).decode("latin-1")


def _signed64(raw):
    """The dynamic half of the IV as the LONG LONG the structure declares."""
    import struct
    return struct.unpack(">q", bytes(raw))[0]
