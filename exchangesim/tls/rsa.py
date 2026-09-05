"""RSA-2048 keygen and PKCS#1 v1.5 signing, by hand.

The standard library has no public-key cryptography at all -- `ssl` consumes
a certificate and a key but has nothing that mints one, and `hashlib` stops at
digests. That is the same gap :mod:`exchangesim.nnf.crypto` fills for AES, and
for the same reason: this project runs on the standard library alone, on
Python 3.6.8 at that, so nothing from `pip` is available to paper over it.

Two things here are worth being deliberate about because getting them wrong
is silent.

**The modular inverse is computed by hand.** CPython's three-argument
``pow(x, -1, m)`` would do it in one call, but that form only exists from
Python 3.8 -- calling it here would work perfectly against `venv314` and
raise on the 3.6.8 target the moment `d` needed computing. The extended
Euclidean algorithm is a dozen lines and has been available since there was a
Euclidean algorithm.

**Primality testing is Miller-Rabin, after a cheap sieve.** A random
1024-bit odd candidate is prime with probability roughly ``1 / (bits *
ln(2))``, so most candidates are composite and most of those are divisible by
a small prime -- trial division against everything below 2000 throws away the
overwhelming majority of candidates for the cost of a few hundred divisions,
leaving Miller-Rabin (expensive: one modular exponentiation per round) to run
only on numbers that already survived that filter. 64 rounds on a candidate
that passed the sieve makes the false-positive probability astronomically
small; RSA needs exactly two such candidates.

`pow()` itself is a CPython builtin, not a library -- its three-argument form
is modular exponentiation in C, and it is what makes a 2048-bit key
generation and every subsequent sign or verify fast enough to run inside a
test.
"""

import base64
import hashlib
import math
import os

from . import der

#: sha256WithRSAEncryption -- the outer AlgorithmIdentifier on a signed
#: certificate and CRL.
SIGNATURE_ALGORITHM_OID = "1.2.840.113549.1.1.11"

#: id-sha256, inside a PKCS#1 v1.5 DigestInfo.
_SHA256_OID = "2.16.840.1.101.3.4.2.1"

#: rsaEncryption -- the AlgorithmIdentifier inside a SubjectPublicKeyInfo.
_RSA_ENCRYPTION_OID = "1.2.840.113549.1.1.1"

_PUBLIC_EXPONENT = 65537


def _sieve(limit):
    """Primes below ``limit``, generated rather than tabulated -- same reason
    :func:`exchangesim.nnf.crypto._build_sbox` derives the AES S-box instead
    of copying a table: a list this long, typed by hand, is a list that can
    be typed wrong and only fail against someone else's key."""
    composite = bytearray(limit)
    primes = []
    for candidate in range(2, limit):
        if composite[candidate]:
            continue
        primes.append(candidate)
        for multiple in range(candidate * candidate, limit, candidate):
            composite[multiple] = 1
    return primes


_SMALL_PRIMES = _sieve(2000)


def _random_bits(count):
    # type: (int) -> int
    """A uniformly random non-negative integer with exactly ``count`` bits
    of entropy (its own top bit is not forced set -- callers that need a
    specific bit width set the high bits themselves)."""
    raw = os.urandom((count + 7) // 8)
    value = int.from_bytes(raw, "big")
    # Discard any extra bits pulled in by rounding up to a whole byte.
    return value >> (8 * len(raw) - count)


def _random_below(bound):
    # type: (int) -> int
    """A uniformly random integer in ``[0, bound)`` by rejection sampling --
    the only way to stay uniform when ``bound`` is not a power of two."""
    if bound <= 0:
        raise ValueError("bound must be positive")
    width = bound.bit_length()
    while True:
        candidate = _random_bits(width)
        if candidate < bound:
            return candidate


def _egcd(a, b):
    """The extended Euclidean algorithm: ``(g, x)`` such that
    ``a*x + b*y == g == gcd(a, b)`` for some ``y`` this function does not
    bother returning -- every caller here only wants ``x``, the inverse."""
    old_r, r = a, b
    old_s, s = 1, 0
    while r != 0:
        quotient = old_r // r
        old_r, r = r, old_r - quotient * r
        old_s, s = s, old_s - quotient * s
    return old_r, old_s


def _modinv(a, m):
    # type: (int, int) -> int
    """The inverse of ``a`` modulo ``m``.

    Not ``pow(a, -1, m)`` -- that three-argument form is Python 3.8+, and
    this module must also run on the 3.6.8 target.
    """
    gcd, x = _egcd(a % m, m)
    if gcd != 1:
        raise ValueError("%d has no inverse modulo %d" % (a, m))
    return x % m


def _lcm(a, b):
    # type: (int, int) -> int
    """`math.lcm` is Python 3.9+; this is the one-line definition it wraps."""
    return a // math.gcd(a, b) * b


def _is_probable_prime(candidate, rounds=64):
    # type: (int, int) -> bool
    """Miller-Rabin. Run only on a candidate that already survived the small-
    prime sieve, so every round here is spent on a number worth the cost."""
    if candidate < 2:
        return False
    for prime in _SMALL_PRIMES:
        if candidate == prime:
            return True
        if candidate % prime == 0:
            return False

    remainder = candidate - 1
    twos = 0
    while remainder % 2 == 0:
        remainder //= 2
        twos += 1

    for _ in range(rounds):
        witness = 2 + _random_below(candidate - 3)
        x = pow(witness, remainder, candidate)
        if x == 1 or x == candidate - 1:
            continue
        for _ in range(twos - 1):
            x = pow(x, 2, candidate)
            if x == candidate - 1:
                break
        else:
            return False
    return True


def _random_odd_candidate(bits):
    # type: (int) -> int
    """A ``bits``-wide candidate with the top two bits and the low bit set.

    The top bit fixes the width exactly (no candidate a bit shorter than
    asked for); the second-highest bit means the product of two such
    candidates is never more than one bit short of the full width either,
    which is what keeps an RSA-2048 key an RSA-2048 key. The low bit rules
    out testing even numbers, which are composite whenever they are worth
    testing at all.
    """
    value = _random_bits(bits)
    value |= (1 << (bits - 1)) | (1 << (bits - 2)) | 1
    return value


def _generate_prime(bits, public_exponent):
    # type: (int, int) -> int
    while True:
        candidate = _random_odd_candidate(bits)
        divisible = False
        for prime in _SMALL_PRIMES:
            if candidate % prime == 0:
                divisible = candidate != prime
                break
        if divisible:
            continue
        if math.gcd(public_exponent, candidate - 1) != 1:
            continue
        if _is_probable_prime(candidate):
            return candidate


class RsaKey(object):
    """An RSA key pair, plus the CRT parameters PKCS#1 carries alongside the
    private exponent so a real client can use the faster CRT path.

    Public and private material live in one object because every use in this
    project -- signing a certificate, building its SubjectPublicKeyInfo --
    needs both, and a simulator has no reason to hand the private half to
    anyone it would need to keep it from.
    """

    __slots__ = ("n", "e", "d", "p", "q", "dp", "dq", "qinv", "bits")

    def __init__(self, n, e, d, p, q, dp, dq, qinv, bits):
        self.n = n
        self.e = e
        self.d = d
        self.p = p
        self.q = q
        self.dp = dp
        self.dq = dq
        self.qinv = qinv
        self.bits = bits

    def sign_sha256(self, data):
        # type: (bytes) -> bytes
        """EMSA-PKCS1-v1_5 over SHA-256: ``00 || 01 || FF...FF || 00 ||
        DigestInfo``, raised to ``d`` mod ``n``.

        DigestInfo is built through :mod:`der` rather than spliced in as a
        literal, but the well-known encoding of its first fifteen bytes --
        SEQUENCE, SEQUENCE, the SHA-256 OID, NULL, OCTET STRING header -- is
        exactly what falls out of ``der.sequence(der.sequence(der.oid(...),
        der.null()), der.octet_string(digest))``, and a test pins that string
        to catch any drift.
        """
        digest = hashlib.sha256(data).digest()
        digest_info = der.sequence(
            der.sequence(der.oid(_SHA256_OID), der.null()),
            der.octet_string(digest),
        )
        modulus_bytes = (self.bits + 7) // 8
        padding_bytes = modulus_bytes - 3 - len(digest_info)
        if padding_bytes < 8:
            raise ValueError("RSA modulus too small for a SHA-256 signature")
        message = b"\x00\x01" + b"\xff" * padding_bytes + b"\x00" + digest_info
        signature_int = pow(int.from_bytes(message, "big"), self.d, self.n)
        return signature_int.to_bytes(modulus_bytes, "big")

    def private_der(self):
        # type: () -> bytes
        """PKCS#1 ``RSAPrivateKey`` -- version 0, then n, e, d, p, q, dp, dq,
        qinv, in that fixed order."""
        return der.sequence(
            der.integer(0),
            der.integer(self.n),
            der.integer(self.e),
            der.integer(self.d),
            der.integer(self.p),
            der.integer(self.q),
            der.integer(self.dp),
            der.integer(self.dq),
            der.integer(self.qinv),
        )

    def public_bits(self):
        # type: () -> bytes
        """The raw ``RSAPublicKey`` DER -- ``SEQUENCE { n, e }`` -- that goes
        inside a SubjectPublicKeyInfo's BIT STRING. Exposed on its own
        because :mod:`x509`'s subjectKeyIdentifier is a hash of exactly this,
        not of the wrapping SubjectPublicKeyInfo (RFC 5280 4.2.1.2 method
        1)."""
        return der.sequence(der.integer(self.n), der.integer(self.e))

    def public_der(self):
        # type: () -> bytes
        """X.509 ``SubjectPublicKeyInfo``."""
        return der.sequence(
            der.sequence(der.oid(_RSA_ENCRYPTION_OID), der.null()),
            der.bit_string(self.public_bits(), 0),
        )


def generate(bits=2048):
    # type: (int) -> RsaKey
    """A fresh RSA key. 2048 bits for anything a real TLS stack will
    validate; ``bits=1024`` is for tests that need *a* key and not a slow
    one."""
    exponent = _PUBLIC_EXPONENT
    high_bits = bits // 2
    low_bits = bits - high_bits

    while True:
        p = _generate_prime(high_bits, exponent)
        q = _generate_prime(low_bits, exponent)
        if p == q:
            continue
        # Not "tiny": reject two primes close enough that Fermat factorisation
        # would find them quickly. A wide margin costs nothing at these sizes.
        if abs(p - q).bit_length() < high_bits - 100:
            continue

        modulus = p * q
        if modulus.bit_length() != bits:
            continue

        totient = _lcm(p - 1, q - 1)
        if math.gcd(exponent, totient) != 1:
            continue

        private_exponent = _modinv(exponent, totient)
        return RsaKey(
            n=modulus,
            e=exponent,
            d=private_exponent,
            p=p,
            q=q,
            dp=private_exponent % (p - 1),
            dq=private_exponent % (q - 1),
            qinv=_modinv(q, p),
            bits=bits,
        )


def to_pem(data, label):
    # type: (bytes, str) -> str
    """PEM: base64, wrapped at 64 columns, between a labelled header and
    footer."""
    encoded = base64.b64encode(data).decode("ascii")
    lines = [encoded[i:i + 64] for i in range(0, len(encoded), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (
        label, "\n".join(lines), label)


def from_pem(text, label):
    # type: (str, str) -> bytes
    """The reverse of :func:`to_pem`. Whitespace inside the body is ignored,
    which is what lets a PEM edited by hand still parse."""
    begin = "-----BEGIN %s-----" % label
    end = "-----END %s-----" % label
    start = text.index(begin) + len(begin)
    stop = text.index(end, start)
    body = "".join(text[start:stop].split())
    return base64.b64decode(body)
