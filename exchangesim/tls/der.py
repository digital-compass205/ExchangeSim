"""A minimal ASN.1 DER writer.

Writing only -- there is no parser here, and there does not need to be one.
Nothing in this project ever reads a certificate back; the only consumer of
what this module produces is a real TLS stack (OpenSSL, by way of the
standard library's :mod:`ssl`), and the only test that matters is whether that
stack accepts the bytes. A parser would be code with nothing to exercise it.

DER is BER with the choices nailed down: **definite** lengths only (no
end-of-contents octets), and every value in its **minimal** encoding -- an
INTEGER has no redundant leading `0x00` or `0xFF`, a BIT STRING's unused-bit
count is exact, and so on. Two rules recur across almost every function here:

* **Length**: short form for a body under 128 bytes (`length` byte, top bit
  clear); long form otherwise (`0x80 | count-of-length-bytes`, then those
  bytes, big-endian, themselves minimal).
* **INTEGER's sign bit**: DER integers are two's complement. A positive value
  whose top bit would otherwise read as set gets a leading `0x00` byte so it
  is not mistaken for a negative one -- the classic bug this module is tested
  against directly (``integer(128)`` must be ``02 02 00 80``, not ``02 01
  80``). `int.to_bytes(..., signed=True)` already enforces exactly this rule,
  so :func:`integer` leans on it rather than re-deriving it by hand.

Two functions are context tagging, which DER uses throughout X.509 to say "the
next field is optional / a choice, tag `n` rather than its natural universal
tag": :func:`explicit`, which wraps a complete, already-encoded TLV inside a
new outer tag (the value keeps its own tag one level down), and
:func:`implicit`, which replaces a primitive's universal tag outright (the
value has no tag of its own to keep). Getting the two confused produces bytes
that decode to the wrong type at the point the underlying value is read --
X.509's `authorityKeyIdentifier` keyIdentifier is IMPLICIT for exactly this
reason: it is a plain OCTET STRING with the universal OCTET STRING tag
replaced by context tag `[0]`, not a second, nested layer of tagging.

:func:`set_of` builds a SET OF from already-encoded members without sorting
them by encoding. That is fine for every use in this project because a SET OF
is only ever built here from a *single* element (one RDN inside an X.509
Name's RDNSequence) -- true DER canonical ordering matters only once a SET OF
holds more than one member, and this function does not implement it. Do not
reach for it for a multi-element SET expecting DER's sort-by-encoding rule.
"""

import datetime

#: sha256WithRSAEncryption and friends travel as dotted strings; this module
#: only ever turns one into bytes, never the reverse.


def _length(count):
    # type: (int) -> bytes
    if count < 0x80:
        return bytes([count])
    encoded = count.to_bytes((count.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def tlv(tag, body):
    # type: (int, bytes) -> bytes
    """One DER value: a tag byte, a definite length, and the body verbatim."""
    return bytes([tag]) + _length(len(body)) + bytes(body)


def integer(value):
    # type: (int) -> bytes
    """INTEGER, two's complement, minimal -- the leading zero byte included
    automatically wherever the sign bit would otherwise be ambiguous.

    Grown one byte at a time rather than computed from ``bit_length`` up
    front: ``int.to_bytes(n, "big", signed=True)`` already raises
    ``OverflowError`` the moment ``n`` is too small, so the first size that
    succeeds *is* the minimal encoding, for a positive value, a negative one,
    and zero alike.
    """
    if value == 0:
        body = b"\x00"
    else:
        width = 1
        while True:
            try:
                body = value.to_bytes(width, "big", signed=True)
                break
            except OverflowError:
                width += 1
    return tlv(0x02, body)


def bit_string(data, unused_bits=0):
    # type: (bytes, int) -> bytes
    """BIT STRING: the unused-bit count, then the bits themselves.

    The count is of trailing bits in the *last* byte that are not part of the
    value -- callers such as :mod:`x509`'s ``keyUsage`` builder compute it,
    this function only carries it.
    """
    return tlv(0x03, bytes([unused_bits]) + bytes(data))


def octet_string(data):
    # type: (bytes) -> bytes
    return tlv(0x04, bytes(data))


def _base128(arc):
    # type: (int) -> bytes
    """One OID arc, base-128 big-endian, continuation bit set on every byte
    but the last."""
    if arc == 0:
        return b"\x00"
    digits = []
    while arc:
        digits.append(arc & 0x7F)
        arc >>= 7
    digits.reverse()
    out = bytearray(digits)
    for i in range(len(out) - 1):
        out[i] |= 0x80
    return bytes(out)


def oid(dotted):
    # type: (str) -> bytes
    """OBJECT IDENTIFIER from a dotted string.

    The first two arcs collapse into one byte, ``40*a + b`` -- the
    specification's way of making room for three top-level arcs (0, 1, 2)
    while the first subsequent arc under arcs 0 and 1 is capped at 39. Every
    arc after that is its own base-128 run.
    """
    arcs = [int(part) for part in dotted.strip().split(".")]
    if len(arcs) < 2:
        raise ValueError("an OID needs at least two arcs: %r" % dotted)
    body = bytearray([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        body += _base128(arc)
    return tlv(0x06, bytes(body))


def null():
    # type: () -> bytes
    return tlv(0x05, b"")


def boolean(value):
    # type: (bool) -> bytes
    return tlv(0x01, b"\xff" if value else b"\x00")


def printable_string(text):
    # type: (str) -> bytes
    return tlv(0x13, text.encode("ascii"))


def utf8_string(text):
    # type: (str) -> bytes
    return tlv(0x0C, text.encode("utf-8"))


def ia5_string(text):
    # type: (str) -> bytes
    return tlv(0x16, text.encode("ascii"))


def utc_time(when):
    # type: (datetime.datetime) -> bytes
    """UTCTime, ``YYMMDDHHMMSSZ`` -- a two-digit year, valid for 1950-2049."""
    return tlv(0x17, when.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def generalized_time(when):
    # type: (datetime.datetime) -> bytes
    """GeneralizedTime, ``YYYYMMDDHHMMSSZ`` -- the four-digit-year form RFC
    5280 requires outside 1950-2049."""
    return tlv(0x18, when.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


def time(when):
    # type: (datetime.datetime) -> bytes
    """UTCTime for 1950-2049, GeneralizedTime otherwise -- RFC 5280's rule
    for encoding a certificate's ``Time`` CHOICE, not a free choice made
    here."""
    if 1950 <= when.year <= 2049:
        return utc_time(when)
    return generalized_time(when)


def sequence(*items):
    # type: (bytes) -> bytes
    return tlv(0x30, b"".join(items))


def set_of(*items):
    # type: (bytes) -> bytes
    """SET OF, from already-encoded members, in the order given.

    Not DER-canonical for more than one member -- true DER requires a SET
    OF's members sorted by their encoded bytes, and this function does not
    sort. Every use in this project builds a SET OF one RDN at a time, where
    there is only ever one member and no ordering question arises. Do not
    reuse this for a genuinely multi-element SET without adding the sort.
    """
    return tlv(0x31, b"".join(items))


def explicit(number, body):
    # type: (int, bytes) -> bytes
    """``[n]`` EXPLICIT: a new outer tag wrapping a complete, already-tagged
    TLV. The wrapped value keeps its own tag one level down."""
    return tlv(0xA0 | number, body)


def implicit(number, body, constructed=False):
    # type: (int, bytes, bool) -> bytes
    """``[n]`` IMPLICIT: a context tag standing in for a primitive's own
    universal tag. ``body`` is the value's raw *content*, not a TLV -- there
    is no universal tag left to strip, because IMPLICIT replaced it rather
    than wrapping it.
    """
    tag = (0xA0 if constructed else 0x80) | number
    return tlv(tag, body)
