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

**This leg is TLS, per the specification (p.156).** The exchange issues its own
self-signed CA, common to every member, and distributes it over the extranet as
``gr_ca_cert1.pem``; a member's client verifies the router's server certificate
against that one file. There are no client certificates -- the document
describes server-side TLS only, which authenticates the router to the member,
not the other way around. ``certs.CertificateStore`` generates that CA and a
leaf under it on first use (see its module docstring); ``venue.py`` is what
builds one, or takes an operator-supplied certificate instead, and hands this
router the resulting paths. ``gateway_router.tls`` then picks the version by
way of ``tls.context.server_context`` -- ``"1.3"`` by default, ``"1.2"`` as a
development floor, or ``"none"`` for plain TCP.

**The one thing that will bite an operator:** a real NSE client sets both
``SSL_CTX_set_min_proto_version`` and ``set_max_proto_version`` to
``TLS1_3_VERSION``, so it offers only TLS 1.3 and will not negotiate down. A
router configured for ``"1.2"`` will therefore never see a connection from a
real client -- that setting exists for a box whose interpreter cannot do 1.3 at
all (this project's own Windows development box, on OpenSSL 1.0.2), not as a
gentler production choice.

What a client loses under ``"none"`` is confidentiality on the key exchange.
What it keeps is the whole message flow -- the same transaction codes, the same
structures, the same secrets, and the same AES-256-GCM on every gateway message
afterwards.
"""

import logging
import os

from ...core.config import ConfigError
from ...fix.message import MalformedMessage, Message
from ...nnf import crypto
from ...nnf.codec import NnfCodec
from ...tls import certs, context
from . import dictionary as D
from . import transactions as X

log = logging.getLogger(__name__)

#: What ``gateway_router.tls`` accepts -- the same three values
#: ``tls.context.POLICIES`` defines, restated here as the venue's own public
#: name for them.
SUPPORTED_TLS = context.POLICIES


class Secrets(object):
    """What one box was issued, and the cipher it implies.

    Held by the venue rather than by either listener, because the two legs are
    different connections: the router writes this and the gateway reads it.
    """

    __slots__ = ("box_id", "session_key", "key", "iv", "additional_key",
                 "methodology", "dynamic_endian")

    def __init__(self, box_id, session_key, key, iv, additional_key,
                 methodology="new", dynamic_endian="auto"):
        self.box_id = box_id
        #: The eight-byte token a box quotes back in BOX_SIGN_ON_REQUEST_IN.
        self.session_key = session_key
        self.key = key
        self.iv = iv
        self.additional_key = additional_key
        #: "existing" (GCM as a keystream, MD5 for integrity) or "new"
        #: (authenticated GCM, the tag in the packet prefix).
        self.methodology = methodology
        #: How the dynamic half of the IV is laid out, or "auto" to settle it
        #: from the first message a member sends -- see NewCipher.
        self.dynamic_endian = dynamic_endian

    def exchange_cipher(self):
        """The exchange half of the pair. The member builds the other."""
        if self.methodology == "existing":
            return crypto.ExistingCipher(self.key, self.iv)
        return crypto.NewCipher(self.key, self.iv, self.additional_key,
                                dynamic_endian=self.dynamic_endian)

    def member_cipher(self):
        """The member half, for a test harness or the scenario runner."""
        if self.methodology == "existing":
            return crypto.ExistingCipher(self.key, self.iv)
        return crypto.NewCipher(self.key, self.iv, self.additional_key,
                                client=True,
                                dynamic_endian=self.member_endian)

    def describe(self):
        """Everything but the secrets themselves."""
        return {"box_id": self.box_id, "methodology": self.methodology,
                "dynamic_iv": self.dynamic_endian, "issued": True}

    @property
    def member_endian(self):
        """The layout a *member* uses.

        A member cannot probe the way the exchange can -- it speaks first, so
        there is nothing to probe against -- and ``"auto"`` therefore means the
        default reading for anything building the member half here.
        """
        if self.dynamic_endian == "auto":
            return crypto.NewCipher.ENDIANNESS[0]
        return self.dynamic_endian


def issue(box_id, methodology="new", entropy=None, dynamic_endian="auto"):
    """Fresh secrets for one box.

    ``entropy`` is injectable so a test can pin the bytes; otherwise they come
    from ``os.urandom``.

    **The dynamic half of the IV is issued as zero**, and only the static half
    is random. Randomising both is tempting -- but GCM's requirement is that an
    IV never repeat *under one key*, and the key here is fresh per box per
    connection, so a random start buys nothing. What it costs is real: the
    specification's pseudocode copies the Gateway Router's ``long long``
    straight into the struct whose memory becomes the IV, while a member that
    byte-swaps it out of the response first -- as every other numeric field in
    this protocol requires -- then holds a different number. At zero those two
    readings coincide, which leaves a member only the question
    :class:`exchangesim.nnf.crypto.NewCipher` documents.
    """
    source = entropy or os.urandom
    return Secrets(
        box_id=int(box_id),
        session_key=_printable(source(8)),
        key=source(crypto.KEY_BYTES),
        iv=source(crypto.STATIC_IV_BYTES) + b"\x00" * 8,
        additional_key=source(crypto.AAD_BYTES),
        methodology=methodology,
        dynamic_endian=dynamic_endian)


def _printable(raw):
    """Eight printable characters, since SessionKey travels as CHAR[8]."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(alphabet[byte % len(alphabet)] for byte in bytearray(raw))


class GatewayRouter(object):
    """The listener that answers ``GR_REQUEST`` and nothing else."""

    def __init__(self, venue, config, certfile=None, keyfile=None,
                 ca_certificate=None):
        self.venue = venue
        self.codec = NnfCodec(venue.layouts)
        self.methodology = config.get("encryption", "new")
        if self.methodology not in ("existing", "new"):
            raise ConfigError(
                "gateway_router.encryption must be 'existing' or 'new', not %r"
                % self.methodology)

        self.dynamic_iv = str(config.get("dynamic_iv", "auto")).lower()
        if self.dynamic_iv not in ("auto",) + crypto.NewCipher.ENDIANNESS:
            raise ConfigError(
                "gateway_router.dynamic_iv must be 'auto', 'little' or 'big', "
                "not %r" % (config.get("dynamic_iv"),))

        #: Where to tell a member the trading gateway is, when the gateway's
        #: own bound address does not say -- see :meth:`_gateway_address`.
        self.advertise_host = config.get("advertise_host") or None
        if self.advertise_host and len(self.advertise_host) > 15:
            raise ConfigError(
                "gateway_router.advertise_host must fit the IPAddress field's "
                "16 bytes with room for a terminator; %r is %d characters"
                % (self.advertise_host, len(self.advertise_host)))

        tls = str(config.get("tls", "1.3")).lower()
        if tls not in SUPPORTED_TLS:
            raise ConfigError(
                "gateway_router.tls must be one of %s, not %r"
                % (", ".join(repr(value) for value in SUPPORTED_TLS), tls))
        self.tls = tls
        #: The CA path a member needs, for the start-up log line only -- the
        #: control plane's own copy comes from ``venue.tls_certificate``.
        self.ca_certificate = ca_certificate
        try:
            self.tls_context = context.server_context(
                tls, certfile, keyfile, setting_name="gateway_router.tls")
        except context.TlsUnavailable as exc:
            # tls/ deliberately does not know about ConfigError; translating
            # its exception into this project's is the caller's job.
            raise ConfigError(str(exc))
        self._listener = None
        self._gateway = None

    @property
    def address(self):
        return self._listener.address if self._listener else None

    def start(self, host, port):
        self._listener = self.venue.reactor.listen(
            host, port, self._on_accept,
            transport=certs.transport_factory(self.tls_context))
        self._gateway = self.venue.acceptor.address
        if self.tls_context is None:
            log.info("gateway router on %s:%d (tls=none, %s encryption)",
                     self._listener.address[0], self._listener.address[1],
                     self.methodology)
        else:
            log.info("gateway router on %s:%d (tls=%s, %s encryption; "
                     "members should trust CA %s)",
                     self._listener.address[0], self._listener.address[1],
                     self.tls, self.methodology, self.ca_certificate)
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
            self._grant(response, box_id, conn)

        out = self.codec.encode(response)
        self._record(response, out, outbound=True)
        conn.send(out)
        conn.close_when_flushed()


    #: Bound addresses that name every interface rather than one a member could
    #: connect to.
    WILDCARDS = ("0.0.0.0", "::", "")

    def _gateway_address(self, conn):
        """Where to tell this member the trading gateway is.

        The gateway's own bound address, whenever it names an interface. It
        does not when the gateway is bound to all of them, and answering
        ``0.0.0.0`` sends a member somewhere it cannot connect -- the same
        failure as a simulator bound to ``127.0.0.1`` and reached by hostname,
        one step further in. So the fallback is the address *this member*
        reached the router on, which is by construction an interface that works
        from where the member is sitting.

        ``gateway_router.advertise_host`` overrides both, for a member coming
        through a NAT or a port forward where neither end's own view of the
        address is the one the member needs.
        """
        host, port = self._gateway or ("127.0.0.1", 0)
        if self.advertise_host:
            return self.advertise_host, port
        if host not in self.WILDCARDS:
            return host, port
        try:
            local = conn.sock.getsockname()[0]
        except (AttributeError, OSError):
            return host, port
        return (local if local not in self.WILDCARDS else host), port

    def _grant(self, response, box_id, conn):
        secrets = issue(box_id, self.methodology,
                        dynamic_endian=self.dynamic_iv)
        self.venue.issue(box_id, secrets)

        host, port = self._gateway_address(conn)
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
