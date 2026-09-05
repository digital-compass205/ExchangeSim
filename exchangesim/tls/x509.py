"""X.509 certificate building on top of :mod:`der` and :mod:`rsa`.

This produces exactly two shapes: a self-signed CA certificate and a leaf
signed by one. Both are TBSCertificate v3 (RFC 5280 section 4.1) with the
extensions a *verifying* TLS client actually inspects -- basicConstraints,
keyUsage, subjectAltName, and the two key identifiers that let a client chain
a leaf to its issuer without a name comparison. Getting those exactly right
is the entire point of this module: a certificate that merely parses proves
nothing, because :func:`der.tlv` will happily encode nonsense. A certificate
that a real client's TLS stack accepts and matches against a hostname proves
the DER is right, which is why the test for this module is an actual
``ssl`` handshake rather than a decoder written to check its own homework.

TBSCertificate, field by field, in the order RFC 5280 fixes:
``[0] EXPLICIT version``, ``serialNumber``, the signature
``AlgorithmIdentifier`` (repeated, byte-for-byte, as the outer one wrapping
the signature itself -- a client that compares the two and finds them
different rejects the certificate), ``issuer``, ``validity``, ``subject``,
``subjectPublicKeyInfo``, and finally ``[3] EXPLICIT extensions``.

Two identifiers link a leaf to its issuer without asking a client to compare
distinguished names byte for byte: :func:`key_identifier` is the SHA-1 of the
issuer's raw public key bits (RFC 5280 4.2.1.2 method 1), carried as the
issuer's own ``subjectKeyIdentifier`` and, unchanged, as the leaf's
``authorityKeyIdentifier``. A CA that reissued its key would break every leaf
it had already signed even if the distinguished name stayed the same, which
is exactly what this identifier is for.

A literal IP address in a Subject Alternative Name must travel as an
``iPAddress`` GeneralName (``[7]``, the raw address bytes), never as a
``dNSName`` (``[2]``, ASCII text) -- a client checking ``server_hostname=
"127.0.0.1"`` against a dNSName entry of the same text does not match it,
because hostname verification (RFC 6125) treats the two GeneralName choices
as disjoint alphabets. :func:`_subject_alt_name` classifies with
:mod:`ipaddress` rather than a regular expression for exactly this reason: a
literal that merely looks like an IP address is not the question, whether
:mod:`ipaddress` will parse it as one is.
"""

import datetime
import hashlib
import ipaddress
import os

from . import der
from . import rsa

#: Common Name, Organization, Country -- the three RDN types this module
#: builds a Name out of.
OID_CN = "2.5.4.3"
OID_O = "2.5.4.10"
OID_C = "2.5.4.6"

_OID_BASIC_CONSTRAINTS = "2.5.29.19"
_OID_KEY_USAGE = "2.5.29.15"
_OID_SUBJECT_KEY_IDENTIFIER = "2.5.29.14"
_OID_EXTENDED_KEY_USAGE = "2.5.29.37"
_OID_SUBJECT_ALT_NAME = "2.5.29.17"
_OID_AUTHORITY_KEY_IDENTIFIER = "2.5.29.35"
_OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"

#: keyUsage bit numbers, MSB-first from bit 0 (RFC 5280 4.2.1.3).
_KU_DIGITAL_SIGNATURE = 0
_KU_KEY_ENCIPHERMENT = 2
_KU_KEY_CERT_SIGN = 5
_KU_CRL_SIGN = 6


def subject_name(common_name, organisation):
    # type: (str, str) -> bytes
    """An RDNSequence of Common Name then Organization.

    Exposed on its own, not only built inline by :func:`self_signed_ca`,
    because :func:`sign_leaf` takes the issuer's name and key identifier as
    plain arguments rather than an issuer certificate object -- a leaf is
    signed against what the CA *is*, not against a parsed copy of its own
    certificate, which is also how a real CA signs a CSR without holding its
    own cert object at all.
    """
    return _name([(OID_CN, common_name), (OID_O, organisation)])


def _name(pairs):
    # type: (list) -> bytes
    rdns = [der.set_of(der.sequence(der.oid(oid), der.printable_string(value)))
            for oid, value in pairs]
    return der.sequence(*rdns)


def key_identifier(key):
    # type: (rsa.RsaKey) -> bytes
    """SHA-1 of the raw public-key bits -- RFC 5280 4.2.1.2 method 1.

    Deliberately the hash of :meth:`RsaKey.public_bits`, the bare
    ``RSAPublicKey``, and not of the wrapping ``SubjectPublicKeyInfo`` --
    method 1 is specified over the ``BIT STRING`` *value* (the key bits
    themselves), not the structure that carries it.
    """
    return hashlib.sha1(key.public_bits()).digest()


def _extension(oid, critical, value_der):
    # type: (str, bool, bytes) -> bytes
    """One ``Extension``: an OID, an omitted-when-false criticality flag
    (DER drops a field at its DEFAULT value), and the extension's own DER
    wrapped in an OCTET STRING -- an extension's content is always one more
    layer of encoding than it looks, which is what makes an unrecognised
    extension skippable without being parsed."""
    parts = [der.oid(oid)]
    if critical:
        parts.append(der.boolean(True))
    parts.append(der.octet_string(value_der))
    return der.sequence(*parts)


def _key_usage(*bits):
    # type: (int) -> bytes
    """A keyUsage BIT STRING with exactly the given bit numbers set.

    Bit ``n`` is the ``n``-th bit from the *most* significant end of the
    string, per RFC 5280 -- not the C convention of bit ``n`` being ``1 <<
    n``. The byte count and the unused-bit count are both derived from the
    highest bit requested, so the encoding is minimal by construction rather
    than padded out to a fixed width: a keyUsage with only bit 0 set is one
    byte with seven unused bits, not two bytes of mostly zero.
    """
    highest = max(bits)
    width = highest // 8 + 1
    value = bytearray(width)
    for bit in bits:
        value[bit // 8] |= 0x80 >> (bit % 8)
    unused = width * 8 - 1 - highest
    return der.bit_string(bytes(value), unused)


def _basic_constraints(is_ca, pathlen=None):
    # type: (bool, int) -> bytes
    if not is_ca:
        # cA's DEFAULT is FALSE; DER omits a field left at its default.
        return der.sequence()
    items = [der.boolean(True)]
    if pathlen is not None:
        items.append(der.integer(pathlen))
    return der.sequence(*items)


def _extended_key_usage(*oids):
    # type: (str) -> bytes
    return der.sequence(*[der.oid(oid) for oid in oids])


def _subject_alt_name(names):
    # type: (list) -> bytes
    """``SEQUENCE OF GeneralName``, classifying each entry as an IP address
    literal or a DNS name.

    A literal such as ``"127.0.0.1"`` must become an ``iPAddress`` -- if it
    travelled as a ``dNSName`` instead, a verifying client checking
    ``server_hostname="127.0.0.1"`` would not match it, because RFC 6125
    hostname verification does not cross-check a dNSName entry against an IP
    literal or vice versa.
    """
    entries = []
    for name in names:
        try:
            address = ipaddress.ip_address(name)
        except ValueError:
            entries.append(der.implicit(2, name.encode("ascii")))
        else:
            entries.append(der.implicit(7, address.packed))
    return der.sequence(*entries)


def _authority_key_identifier(key_id):
    # type: (bytes) -> bytes
    return der.sequence(der.implicit(0, key_id))


def _random_serial():
    # type: () -> int
    """A random positive integer of at most 20 bytes.

    Built as an unsigned integer from random bytes -- since the value is a
    Python ``int`` rather than a byte string being reinterpreted, it is
    already non-negative by construction, and :func:`der.integer` adds
    whatever leading ``0x00`` DER's two's-complement rule requires when it is
    encoded. Re-rolled on the one-in-2**160 chance it comes out zero, which a
    certificate serial number must not be.
    """
    value = int.from_bytes(os.urandom(20), "big")
    return value or 1


def _tbs_certificate(serial, issuer, validity, subject, spki, extensions):
    # type: (int, bytes, tuple, bytes, bytes, list) -> bytes
    not_before, not_after = validity
    return der.sequence(
        der.explicit(0, der.integer(2)),
        der.integer(serial),
        der.sequence(der.oid(rsa.SIGNATURE_ALGORITHM_OID), der.null()),
        issuer,
        der.sequence(der.time(not_before), der.time(not_after)),
        subject,
        spki,
        der.explicit(3, der.sequence(*extensions)),
    )


def _sign_certificate(tbs, signing_key):
    # type: (bytes, rsa.RsaKey) -> bytes
    """``SEQUENCE { tbs, AlgorithmIdentifier, signature }``.

    The AlgorithmIdentifier built here must be byte-identical to the one
    already inside ``tbs`` -- both come from the same
    ``SIGNATURE_ALGORITHM_OID`` constant precisely so there is no way for
    them to drift apart.
    """
    algorithm = der.sequence(der.oid(rsa.SIGNATURE_ALGORITHM_OID), der.null())
    signature = signing_key.sign_sha256(tbs)
    return der.sequence(tbs, algorithm, der.bit_string(signature, 0))


def self_signed_ca(key, common_name, organisation, days, serial=None, now=None):
    # type: (rsa.RsaKey, str, str, int, int, datetime.datetime) -> bytes
    """A self-signed CA certificate: issuer and subject are the same Name,
    and the certificate is signed by the key it certifies.

    ``basicConstraints`` (critical, ``CA:TRUE``, path length 0 -- this CA may
    sign leaves but not further intermediates) and ``keyUsage`` (critical,
    ``keyCertSign`` and ``cRLSign``) are what tell a verifying client this
    certificate may act as a trust anchor at all; without them some stacks
    refuse to use it to validate anything.
    """
    now = now or datetime.datetime.utcnow()
    not_after = now + datetime.timedelta(days=days)
    serial = _random_serial() if serial is None else serial
    name = subject_name(common_name, organisation)

    extensions = [
        _extension(_OID_BASIC_CONSTRAINTS, True, _basic_constraints(True, 0)),
        _extension(_OID_KEY_USAGE, True,
                   _key_usage(_KU_KEY_CERT_SIGN, _KU_CRL_SIGN)),
        _extension(_OID_SUBJECT_KEY_IDENTIFIER, False,
                   der.octet_string(key_identifier(key))),
    ]
    tbs = _tbs_certificate(serial, name, (now, not_after), name,
                           key.public_der(), extensions)
    return _sign_certificate(tbs, key)


def sign_leaf(ca_key, ca_subject, ca_key_id, key, common_name, organisation,
             sans, days, serial=None, now=None):
    # type: (...) -> bytes
    """A leaf certificate, signed by ``ca_key`` over an issuer Name and key
    identifier the caller supplies rather than derives from a CA certificate
    object -- see :func:`subject_name` and :func:`key_identifier` for how to
    produce them from the same call that built the CA.

    ``extendedKeyUsage`` names ``serverAuth`` because that is the purpose a
    TLS client checks; ``subjectAltName`` is what hostname verification
    actually matches against (a certificate's Common Name is not consulted by
    a modern client at all); ``authorityKeyIdentifier`` is ``ca_key_id``
    unchanged, so a client can chain this leaf to its issuer by identifier
    rather than by comparing distinguished names.
    """
    now = now or datetime.datetime.utcnow()
    not_after = now + datetime.timedelta(days=days)
    serial = _random_serial() if serial is None else serial
    name = subject_name(common_name, organisation)

    extensions = [
        _extension(_OID_BASIC_CONSTRAINTS, True, _basic_constraints(False)),
        _extension(_OID_KEY_USAGE, True,
                   _key_usage(_KU_DIGITAL_SIGNATURE, _KU_KEY_ENCIPHERMENT)),
        _extension(_OID_EXTENDED_KEY_USAGE, False,
                   _extended_key_usage(_OID_SERVER_AUTH)),
        _extension(_OID_SUBJECT_ALT_NAME, False, _subject_alt_name(sans)),
        _extension(_OID_SUBJECT_KEY_IDENTIFIER, False,
                   der.octet_string(key_identifier(key))),
        _extension(_OID_AUTHORITY_KEY_IDENTIFIER, False,
                   _authority_key_identifier(ca_key_id)),
    ]
    tbs = _tbs_certificate(serial, ca_subject, (now, not_after), name,
                           key.public_der(), extensions)
    return _sign_certificate(tbs, ca_key)
