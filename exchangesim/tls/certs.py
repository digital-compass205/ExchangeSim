"""Issuing and caching the certificates a running simulator needs, on top of
Phases 1 and 2 (:mod:`exchangesim.tls.rsa`, :mod:`exchangesim.tls.x509`,
:mod:`exchangesim.tls.transport`).

The NSE Gateway Router is the first, and so far only, consumer: it needs a
server certificate chaining to a CA, and a real member's client is configured
once, ahead of time, with a copy of that CA -- so the CA must keep the same
key and the same file name across restarts, and the server leaf must keep
covering whatever host name or address the router actually listens on.
:class:`CertificateStore` is the thing that makes both of those true without
either being regenerated needlessly: it produces the pair on first use and
reuses it afterwards, subject to the checks in :meth:`CertificateStore.ensure`.

**Why the on-disk record is JSON metadata, not a certificate parser.** Phase 1's
:mod:`der` is a DER *writer* with no matching reader, deliberately: nothing
above it has ever needed to read a certificate back, only hand one to a real
TLS stack. Deciding whether the certificate on disk is still good enough would
otherwise mean writing that reader just for this one question -- expiry, SANs,
names -- so instead ``issued.json`` sits beside the PEMs and states the answer
directly: what the certificate is good until, and what it was asked to cover
when it was made. The one place this module still needs to get bytes back out
of a PEM -- computing the fingerprint of a certificate it did *not* just
generate in this process -- reuses :func:`rsa.from_pem`, which already does
exactly that (strip the header and footer, base64-decode the body) without
being a parser of anything the DER means.

**A changed name forces a reissue, and that is as important as expiry.** The
certificate's SANs are what a verifying client's hostname check actually
consults (see :mod:`x509`'s module docstring); if the router's configured host
or address changes and the certificate is left alone, the client's TLS
handshake fails with an error that says nothing about configuration having
moved -- it looks exactly like a broken deployment. Comparing the requested
``names`` (and, for the same reason, ``common_name`` and ``organisation``)
against what is recorded in ``issued.json`` on every :meth:`ensure` call is
what turns that into a one-time reissue instead of a support call.
"""

import datetime
import hashlib
import json
import os

from . import rsa
from . import x509
from .transport import TlsTransport

#: File names under a store's directory. ``gr_ca_cert1.pem`` is fixed by the
#: NSE specification -- a member's client is configured to look for exactly
#: that name -- so it is not derived from ``common_name`` or anything else
#: that might otherwise seem like a natural source for it.
CA_CERT_NAME = "gr_ca_cert1.pem"
CA_KEY_NAME = "gr_ca_key.pem"
SERVER_CERT_NAME = "gr_server_cert.pem"
SERVER_KEY_NAME = "gr_server_key.pem"
META_NAME = "issued.json"

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

#: Default RSA key size. A real TLS handshake needs 2048 -- OpenSSL 3.x's
#: default security level refuses a 1024-bit leaf outright -- but a test that
#: only inspects the files a store writes (never negotiates with them) can
#: ask for a smaller ``key_bits`` and skip most of the cost of keygen.
_KEY_BITS = 2048


class Issued(object):
    """What one call to :meth:`CertificateStore.ensure` hands back: the four
    file paths, and the metadata a caller needs without reading any of them.

    Never carries key material directly -- only paths to it -- so that
    handing an :class:`Issued` to something that should not see a private key
    (the control plane, by way of :meth:`CertificateStore.describe`) is a
    matter of which attributes get copied, not a trust boundary this object
    has to enforce itself.
    """

    __slots__ = ("ca_certificate", "ca_key", "certificate", "key",
                 "not_after", "names", "fingerprint")

    def __init__(self, ca_certificate, ca_key, certificate, key,
                 not_after, names, fingerprint):
        self.ca_certificate = ca_certificate
        self.ca_key = ca_key
        self.certificate = certificate
        self.key = key
        self.not_after = not_after
        self.names = tuple(names)
        self.fingerprint = fingerprint


def _utcnow():
    # type: () -> datetime.datetime
    """A naive UTC ``datetime``, without calling ``datetime.datetime.utcnow()``
    -- deprecated since Python 3.12, in favour of an aware ``now(UTC)``. Every
    other timestamp in this module (and in :mod:`x509`, which this module
    always calls with an explicit ``now=`` rather than letting it reach for
    its own default) is naive UTC, so this strips the timezone
    ``now(timezone.utc)`` attaches rather than making the one clock call here
    aware while everything downstream of it stays naive. ``timezone.utc`` --
    unlike ``datetime.UTC``, added in 3.11 -- exists since Python 3.2, so this
    runs unchanged on 3.6.8 through 3.14.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _format_iso(when):
    # type: (datetime.datetime) -> str
    """Milliseconds and a trailing ``Z`` -- the same spelling
    ``core/clock.py:format_iso`` uses on the wire, restated here because
    :mod:`exchangesim.tls` imports nothing from the project."""
    return when.strftime(_ISO_FORMAT)[:-4] + "Z"


def _parse_iso(text):
    # type: (str) -> datetime.datetime
    return datetime.datetime.strptime(text, _ISO_FORMAT)


def _fingerprint(der):
    # type: (bytes) -> str
    """SHA-256 of a certificate's DER, colon-separated uppercase hex pairs --
    the same spelling ``openssl x509 -fingerprint -sha256`` prints."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _write_atomic(path, data, mode=None):
    # type: (str, bytes, int) -> None
    """Write ``data`` to ``path`` via a temporary file and :func:`os.replace`,
    so a crash mid-write leaves the old file (or nothing) rather than a
    half-written one that would read back as valid.

    ``mode``, when given, is applied to the temporary file before the replace
    -- on POSIX this is the ``0600`` a private key wants; on Windows
    :func:`os.chmod` barely affects anything (there is no POSIX permission
    bit to set), so the real protection there is simply that ``var/`` is
    git-ignored and this directory lives under it.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(data)
    if mode is not None:
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
    os.replace(tmp_path, path)


def _read_bytes(path):
    # type: (str) -> bytes
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except (IOError, OSError):
        return None


class CertificateStore(object):
    """A self-signed CA and a server leaf it signs, generated on first use and
    reused afterwards.

    Regeneration is decided by :meth:`ensure` on every call, against
    ``issued.json`` recorded beside the PEMs (see the module docstring for why
    that file exists rather than a certificate parser). Any of the following
    forces a fresh CA and leaf:

    - any of the four PEM files is missing or unreadable
    - ``issued.json`` is missing, is not valid JSON, or is missing a key it
      needs
    - the recorded ``not_after`` is already in the past, or within
      ``renew_within_days`` of it
    - the recorded ``names``, ``common_name`` or ``organisation`` differ from
      what this call is asking for

    That last condition matters as much as expiry. A certificate's Subject
    Alternative Names are what a verifying client's hostname check actually
    consults; if the router's configured host or address changes and the
    certificate is left as-is, a member's client fails its handshake with an
    error that names no configuration problem at all. Reissuing when the
    requested names change is what turns that into an automatic fix instead
    of a puzzle.
    """

    def __init__(self, directory, common_name="localhost",
                 organisation="ExchangeSim", names=(), ca_days=1095,
                 leaf_days=365, renew_within_days=30, now=None,
                 key_bits=_KEY_BITS, key_source=None):
        # type: (str, str, str, tuple, int, int, int, callable, int, callable) -> None
        """``now``, when given, is a zero-argument callable returning the
        current UTC time -- injectable so a test can drive expiry and
        renewal without touching the real clock. Defaults to a naive-UTC
        helper, not ``datetime.datetime.utcnow`` directly (see
        :func:`_utcnow`).

        ``key_source``, when given, is a zero-argument callable returning an
        :class:`rsa.RsaKey` -- called once for the CA key and once for the
        leaf on every generation, in that order. This is a real seam, not a
        shortcut around one: "does the store decide correctly when to
        reissue" and "can RSA produce a key" are different questions, and a
        test of the former has no need to pay RSA-2048 keygen's cost on every
        one of the many generations it triggers. Defaults to
        ``lambda: rsa.generate(key_bits)``, which is the only path production
        code takes.
        """
        self.directory = directory
        self.common_name = common_name
        self.organisation = organisation
        self.names = tuple(names) if names else (common_name,)
        self.ca_days = ca_days
        self.leaf_days = leaf_days
        self.renew_within_days = renew_within_days
        self.key_bits = key_bits
        self._now = now if now is not None else _utcnow
        self._key_source = (
            key_source if key_source is not None
            else (lambda: rsa.generate(self.key_bits)))

        self._ca_cert_path = os.path.join(directory, CA_CERT_NAME)
        self._ca_key_path = os.path.join(directory, CA_KEY_NAME)
        self._server_cert_path = os.path.join(directory, SERVER_CERT_NAME)
        self._server_key_path = os.path.join(directory, SERVER_KEY_NAME)
        self._meta_path = os.path.join(directory, META_NAME)

    # -- public API ----------------------------------------------------

    def ensure(self):
        # type: () -> Issued
        """The current certificate pair, generating a fresh one first if
        :meth:`_needs_regeneration` says so."""
        os.makedirs(self.directory, exist_ok=True)
        now = self._now()
        meta = self._read_meta()
        if meta is not None and not self._needs_regeneration(meta, now):
            issued = self._issued_from_meta(meta)
            if issued is not None:
                return issued
        return self._generate(now)

    def describe(self):
        # type: () -> dict
        """A control-plane-safe summary: paths to public material only, never
        a key path or any PEM body."""
        issued = self.ensure()
        return {
            "ca_certificate": issued.ca_certificate,
            "certificate": issued.certificate,
            "fingerprint": issued.fingerprint,
            "not_after": _format_iso(issued.not_after),
            "names": list(issued.names),
        }

    # -- deciding whether to regenerate ---------------------------------

    def _read_meta(self):
        # type: () -> dict
        try:
            with open(self._meta_path, "r") as handle:
                data = json.load(handle)
        except (IOError, OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _needs_regeneration(self, meta, now):
        # type: (dict, datetime.datetime) -> bool
        for path in (self._ca_cert_path, self._ca_key_path,
                     self._server_cert_path, self._server_key_path):
            if not _read_bytes(path):
                return True
        try:
            not_after = _parse_iso(meta["not_after"])
            names = tuple(meta["names"])
            common_name = meta["common_name"]
            organisation = meta["organisation"]
        except (KeyError, TypeError, ValueError):
            return True
        if not_after <= now + datetime.timedelta(days=self.renew_within_days):
            return True
        if (names != self.names or common_name != self.common_name
                or organisation != self.organisation):
            return True
        return False

    def _issued_from_meta(self, meta):
        # type: (dict) -> Issued
        """Rebuild an :class:`Issued` from ``issued.json`` plus the PEMs
        already on disk, when :meth:`_needs_regeneration` said they are still
        good. The fingerprint is not stored in the metadata -- it is cheap to
        recompute and storing a derived value invites the two drifting apart
        -- so it comes from the server certificate PEM via :func:`rsa.from_pem`,
        the one place this module reads DER back out rather than only ever
        writing it.
        """
        try:
            not_after = _parse_iso(meta["not_after"])
            names = tuple(meta["names"])
        except (KeyError, TypeError, ValueError):
            return None
        fingerprint = self._server_fingerprint()
        if fingerprint is None:
            return None
        return Issued(self._ca_cert_path, self._ca_key_path,
                      self._server_cert_path, self._server_key_path,
                      not_after, names, fingerprint)

    def _server_fingerprint(self):
        # type: () -> str
        raw = _read_bytes(self._server_cert_path)
        if not raw:
            return None
        try:
            der = rsa.from_pem(raw.decode("ascii"), "CERTIFICATE")
        except (ValueError, UnicodeDecodeError):
            return None
        return _fingerprint(der)

    # -- generation ------------------------------------------------------

    def _generate(self, now):
        # type: (datetime.datetime) -> Issued
        ca_key = self._key_source()
        server_key = self._key_source()

        ca_cert_der = x509.self_signed_ca(
            ca_key, self.common_name, self.organisation,
            days=self.ca_days, now=now)

        ca_subject = x509.subject_name(self.common_name, self.organisation)
        ca_key_id = x509.key_identifier(ca_key)
        not_after = now + datetime.timedelta(days=self.leaf_days)
        server_cert_der = x509.sign_leaf(
            ca_key, ca_subject, ca_key_id, server_key,
            self.common_name, self.organisation, list(self.names),
            days=self.leaf_days, now=now)

        _write_atomic(self._ca_cert_path,
                      rsa.to_pem(ca_cert_der, "CERTIFICATE").encode("ascii"))
        _write_atomic(self._ca_key_path,
                      rsa.to_pem(ca_key.private_der(),
                                 "RSA PRIVATE KEY").encode("ascii"),
                      mode=0o600)
        _write_atomic(self._server_cert_path,
                      rsa.to_pem(server_cert_der, "CERTIFICATE").encode("ascii"))
        _write_atomic(self._server_key_path,
                      rsa.to_pem(server_key.private_der(),
                                 "RSA PRIVATE KEY").encode("ascii"),
                      mode=0o600)

        meta = {
            "not_after": _format_iso(not_after),
            "names": list(self.names),
            "common_name": self.common_name,
            "organisation": self.organisation,
        }
        _write_atomic(self._meta_path,
                      json.dumps(meta, indent=2).encode("ascii"))

        fingerprint = _fingerprint(server_cert_der)
        return Issued(self._ca_cert_path, self._ca_key_path,
                      self._server_cert_path, self._server_key_path,
                      not_after, self.names, fingerprint)


def transport_factory(context):
    # type: (object) -> callable
    """``factory(sock) -> TlsTransport`` for :meth:`Reactor.listen`'s
    ``transport=`` argument, or ``None`` when ``context`` is ``None`` --
    which is what a venue configured with ``tls: "none"`` passes, and what
    keeps that venue's listener plain rather than needing its own
    ``if context is not None`` at every call site.
    """
    if context is None:
        return None

    def factory(sock):
        return TlsTransport(context, server_side=True)

    return factory
