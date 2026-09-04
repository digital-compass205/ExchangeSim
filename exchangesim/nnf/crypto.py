"""AES-256-GCM, by hand, because the standard library has no cipher.

The constraint this project works under is Python 3.6 with nothing but the
standard library, and NSE encrypts every interactive message. `hashlib` supplies
the MD5 the packet prefix needs and `ssl` supplies TLS for the Gateway Router
leg, but there is no AES anywhere in the standard library, so it is written out
here. Speed is not the point -- a simulator encrypts a few hundred bytes per
message and has no throughput target.

Chapter 9 publishes **two** methodologies, and the sample calls in the annexure
are the specification of both. Both use ``EVP_aes_256_gcm``; they differ in what
they do with it.

*The existing methodology* calls ``EVP_EncryptInit_ex`` once with the key and IV
and then ``EVP_EncryptUpdate`` per message -- no ``Final``, no tag. That is GCM's
counter half used as a plain stream cipher, and because the context is
initialised "only once" the keystream **runs continuously across messages**
rather than restarting: message two picks up where message one stopped, mid
block if that is where it stopped. Integrity comes from the MD5 digest carried
separately in the packet prefix, computed over the plaintext.

*The new methodology* re-initialises the IV per message
(``EVP_EncryptInit(ctx, NULL, NULL, iv)``), feeds a twelve-byte additional key
as AAD, and produces a sixteen-byte authentication tag that displaces the MD5 in
the packet prefix. Its IV is a static half and a dynamic counter: the counter is
**incremented before every encryption and decremented before every decryption**,
from two independent copies. That asymmetry is the easiest thing here to get
backwards, and getting it backwards is invisible -- a simulator talking to
itself round-trips perfectly and fails against every real client.

Both IVs are sixteen bytes, which is *not* GCM's ninety-six bit fast path, so
``J0`` comes from GHASH rather than from concatenation. An implementation that
only handles the fast path passes every self-test and produces wrong ciphertext
against OpenSSL, so the NIST vectors in the tests include a long-IV case.
"""

import struct

#: GCM works on 128-bit blocks whatever the key length.
BLOCK_BYTES = 16

#: The authentication tag, and the width of the packet prefix's checksum field.
TAG_BYTES = 16

#: What the Gateway Router hands out: a 256-bit key, a 128-bit IV (8 static and
#: 8 dynamic) and, for the new methodology, a 96-bit additional key used as AAD.
KEY_BYTES = 32
IV_BYTES = 16
STATIC_IV_BYTES = 8
AAD_BYTES = 12

_R = 0xE1000000000000000000000000000000


class CryptoError(Exception):
    """A message that did not decrypt or did not authenticate."""


# ---------------------------------------------------------------------------
# AES-256
# ---------------------------------------------------------------------------

def _build_sbox():
    """The AES S-box, generated rather than tabulated.

    A 256-entry table copied by hand is a table that can be copied wrongly and
    will then fail only against somebody else's cipher. Deriving it from the
    definition -- multiplicative inverse in GF(2^8), then the affine transform --
    is a dozen lines and cannot drift.
    """
    powers, logs = [0] * 256, [0] * 256
    value = 1
    for exponent in range(255):
        powers[exponent] = value
        logs[value] = exponent
        value ^= (value << 1) ^ (0x1B if value & 0x80 else 0)
        value &= 0xFF

    sbox = [0] * 256
    for index in range(256):
        inverse = 0 if index == 0 else powers[(255 - logs[index]) % 255]
        result = inverse
        for _ in range(4):
            inverse = ((inverse << 1) | (inverse >> 7)) & 0xFF
            result ^= inverse
        sbox[index] = result ^ 0x63
    return sbox


SBOX = _build_sbox()

#: Round constants for the key schedule, x^(i-1) in GF(2^8).
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
        0x6C, 0xD8, 0xAB, 0x4D]


def _xtime(byte):
    byte <<= 1
    return (byte ^ 0x1B) & 0xFF if byte & 0x100 else byte


class Aes(object):
    """AES-256 in the only direction GCM needs: block encryption.

    GCM never decrypts a block -- both directions XOR against the same counter
    keystream -- so there is no inverse cipher here and no dead code to get
    wrong.
    """

    #: 256-bit key: 8 words in, 14 rounds, 60 words of schedule.
    _NK = 8
    _NR = 14

    __slots__ = ("_schedule",)

    def __init__(self, key):
        if len(key) != KEY_BYTES:
            raise ValueError("AES-256 needs a %d byte key, got %d"
                             % (KEY_BYTES, len(key)))
        self._schedule = self._expand(bytearray(key))

    def _expand(self, key):
        words = [list(key[i * 4:i * 4 + 4]) for i in range(self._NK)]
        total = 4 * (self._NR + 1)
        for index in range(self._NK, total):
            word = list(words[index - 1])
            if index % self._NK == 0:
                word = word[1:] + word[:1]
                word = [SBOX[b] for b in word]
                word[0] ^= RCON[index // self._NK - 1]
            elif index % self._NK == 4:
                word = [SBOX[b] for b in word]
            previous = words[index - self._NK]
            words.append([previous[i] ^ word[i] for i in range(4)])
        return [bytes(bytearray(sum(words[r * 4:r * 4 + 4], [])))
                for r in range(self._NR + 1)]

    def encrypt_block(self, block):
        # type: (bytes) -> bytes
        state = bytearray(block)
        self._add_round_key(state, self._schedule[0])

        for rnd in range(1, self._NR):
            for i in range(16):
                state[i] = SBOX[state[i]]
            self._shift_rows(state)
            self._mix_columns(state)
            self._add_round_key(state, self._schedule[rnd])

        for i in range(16):
            state[i] = SBOX[state[i]]
        self._shift_rows(state)
        self._add_round_key(state, self._schedule[self._NR])
        return bytes(state)

    @staticmethod
    def _add_round_key(state, key):
        for i in range(16):
            state[i] ^= key[i]

    @staticmethod
    def _shift_rows(state):
        # The state is column-major: byte i is row i%4, column i//4.
        for row in range(1, 4):
            column = [state[row], state[row + 4], state[row + 8], state[row + 12]]
            column = column[row:] + column[:row]
            state[row], state[row + 4], state[row + 8], state[row + 12] = column

    @staticmethod
    def _mix_columns(state):
        for column in range(4):
            base = column * 4
            a0, a1, a2, a3 = state[base:base + 4]
            total = a0 ^ a1 ^ a2 ^ a3
            state[base] = a0 ^ total ^ _xtime(a0 ^ a1)
            state[base + 1] = a1 ^ total ^ _xtime(a1 ^ a2)
            state[base + 2] = a2 ^ total ^ _xtime(a2 ^ a3)
            state[base + 3] = a3 ^ total ^ _xtime(a3 ^ a0)


# ---------------------------------------------------------------------------
# GCM
# ---------------------------------------------------------------------------

def _to_int(block):
    high, low = struct.unpack(">QQ", block)
    return (high << 64) | low


def _to_block(value):
    return struct.pack(">QQ", (value >> 64) & 0xFFFFFFFFFFFFFFFF,
                       value & 0xFFFFFFFFFFFFFFFF)


def _gf_multiply(x, y):
    """Multiplication in GF(2^128) under GCM's bit-reversed convention."""
    product = 0
    accumulator = y
    for bit in range(128):
        if x & (1 << (127 - bit)):
            product ^= accumulator
        if accumulator & 1:
            accumulator = (accumulator >> 1) ^ _R
        else:
            accumulator >>= 1
    return product


def _pad(data):
    remainder = len(data) % BLOCK_BYTES
    return data + b"\x00" * (BLOCK_BYTES - remainder) if remainder else data


class Gcm(object):
    """Galois/Counter Mode over :class:`Aes`.

    Kept separate from the two NSE cipher wrappers because it is the standard
    and they are the venue's use of it: this class is what the NIST vectors are
    tested against.
    """

    __slots__ = ("_aes", "_h")

    def __init__(self, key):
        self._aes = Aes(key)
        self._h = _to_int(self._aes.encrypt_block(b"\x00" * BLOCK_BYTES))

    def _ghash(self, data):
        digest = 0
        for offset in range(0, len(data), BLOCK_BYTES):
            digest = _gf_multiply(digest ^ _to_int(data[offset:offset + BLOCK_BYTES]),
                                  self._h)
        return digest

    def initial_counter(self, iv):
        """``J0``, the counter the keystream starts from.

        A twelve-byte IV is the fast path and simply gains a counter of one.
        Anything else -- NSE's sixteen bytes included -- is hashed, and an
        implementation that assumes the fast path is wrong here without ever
        saying so.
        """
        if len(iv) == 12:
            return _to_int(iv + b"\x00\x00\x00\x01")
        padded = _pad(iv) + struct.pack(">QQ", 0, len(iv) * 8)
        return self._ghash(padded)

    def keystream_block(self, counter, index):
        """The ``index``-th block of the keystream that follows ``counter``."""
        block = (counter & ~0xFFFFFFFF) | ((counter + index) & 0xFFFFFFFF)
        return self._aes.encrypt_block(_to_block(block))

    def apply_keystream(self, counter, data, skip=0):
        """XOR ``data`` against the keystream, starting ``skip`` bytes in.

        ``skip`` is what makes the existing methodology's continuous stream
        expressible: a message that ends mid-block leaves the next one to resume
        at the same offset rather than at a block boundary.
        """
        out = bytearray()
        position = skip
        while len(out) < len(data):
            block = self.keystream_block(counter, position // BLOCK_BYTES)
            start = position % BLOCK_BYTES
            take = min(BLOCK_BYTES - start, len(data) - len(out))
            out += block[start:start + take]
            position += take
        return bytes(bytearray(a ^ b for a, b in zip(bytearray(data), out)))

    def encrypt(self, iv, plaintext, aad=b""):
        """Ciphertext and tag, the full authenticated mode."""
        counter = self.initial_counter(iv)
        ciphertext = self.apply_keystream((counter + 1) & ((1 << 128) - 1),
                                          plaintext)
        return ciphertext, self._tag(counter, ciphertext, aad)

    def decrypt(self, iv, ciphertext, tag, aad=b""):
        """Plaintext, or :class:`CryptoError` when the tag does not verify."""
        counter = self.initial_counter(iv)
        expected = self._tag(counter, ciphertext, aad)
        if not _equal(expected, tag):
            raise CryptoError("authentication tag does not verify")
        return self.apply_keystream((counter + 1) & ((1 << 128) - 1), ciphertext)

    def _tag(self, counter, ciphertext, aad):
        lengths = struct.pack(">QQ", len(aad) * 8, len(ciphertext) * 8)
        digest = self._ghash(_pad(aad) + _pad(ciphertext) + lengths)
        return self.apply_keystream(counter, _to_block(digest))


def _equal(left, right):
    """Constant-time comparison, so a tag cannot be found a byte at a time."""
    if len(left) != len(right):
        return False
    difference = 0
    for a, b in zip(bytearray(left), bytearray(right)):
        difference |= a ^ b
    return difference == 0


# ---------------------------------------------------------------------------
# The three ways an NNF connection carries a message
# ---------------------------------------------------------------------------

class Cipher(object):
    """What a box connection does to a message on its way to and from the wire.

    Three implementations, one per connection mode. Each answers two questions
    about a message: what bytes travel, and what goes in the packet prefix's
    sixteen-byte checksum field.
    """

    #: Whether the first message on the connection travels in clear. It always
    #: does -- SECURE_BOX_REGISTRATION_REQUEST announces the encryption -- so
    #: this exists to be read, not overridden.
    name = "plain"

    def seal(self, data):
        # type: (bytes) -> tuple
        """``(bytes to send, checksum for the prefix)``."""
        raise NotImplementedError

    def open(self, data, checksum):
        # type: (bytes, bytes) -> bytes
        """The plaintext, or :class:`CryptoError`."""
        raise NotImplementedError


class PlainCipher(Cipher):
    """No encryption: the packet carries the message and an MD5 of it.

    Still a real mode -- Chapter 10 describes members "connecting on
    non-encrypted mode", where the sequence number is zero in every packet and a
    checksum mismatch drops the packet rather than the connection.
    """

    name = "plain"

    def seal(self, data):
        from .packet import md5_digest
        return data, md5_digest(data)

    def open(self, data, checksum):
        from .packet import md5_digest
        if checksum is not None and not _equal(md5_digest(data), checksum):
            raise CryptoError("MD5 checksum does not match the message data")
        return data


class ExistingCipher(Cipher):
    """The existing methodology: one continuous keystream, MD5 for integrity.

    The context is initialised once and never re-keyed, so this object is
    stateful in a way the other two are not: each direction keeps its own byte
    offset into the stream, and a message that ends mid-block leaves the next
    one to resume there. Reset either offset and every subsequent message is
    garbage -- which is why a reconnection collects a fresh key from the Gateway
    Router rather than reusing this one.
    """

    name = "existing"

    def __init__(self, key, iv):
        if len(iv) != IV_BYTES:
            raise ValueError("IV is %d bytes, not %d" % (len(iv), IV_BYTES))
        self._gcm = Gcm(key)
        self._counter = (self._gcm.initial_counter(iv) + 1) & ((1 << 128) - 1)
        self._sent = 0
        self._received = 0

    def seal(self, data):
        from .packet import md5_digest
        digest = md5_digest(data)
        sealed = self._gcm.apply_keystream(self._counter, data, self._sent)
        self._sent += len(data)
        return sealed, digest

    def open(self, data, checksum):
        from .packet import md5_digest
        plain = self._gcm.apply_keystream(self._counter, data, self._received)
        self._received += len(data)
        if checksum is not None and not _equal(md5_digest(plain), checksum):
            raise CryptoError("MD5 checksum does not match the decrypted message")
        return plain


class NewCipher(Cipher):
    """The new methodology: authenticated GCM with a walking IV.

    Per message: re-initialise with the IV, feed the twelve-byte additional key
    as AAD, and put the tag in the packet prefix where the MD5 used to be.

    The IV is eight static bytes and a sixty-four bit counter kept in two
    independent copies, both starting at the value the Gateway Router issued.
    The document states the member's rule: "for every message the dynamic part
    of the IV is incremented by 1 before encryption and decremented by 1 before
    decryption".

    Read literally that cannot work, because both ends would move the same copy
    the same way and never meet. It works because the rule is written from the
    member's side and the exchange mirrors it: the counter **rises for
    member-to-exchange traffic and falls for exchange-to-member traffic**, two
    sequences walking away from one origin. That also satisfies GCM's hard
    requirement that a key never see an IV twice, which a single shared counter
    would violate the moment both ends spoke at once.

    So ``client`` says which side of the connection this is, exactly as
    :class:`exchangesim.binary.codec.BinaryCodec` does for the one Comp ID that
    travels. The simulator is the exchange, so that is the default; the test
    harness and the scenario runner pass ``client=True``.

    ASSUMPTION: the specification gives the dynamic half as a C ``long long``
    inside a struct and does not say how it is laid out once incremented. It is
    written big-endian here, as every other multi-byte value in this protocol
    is; ``dynamic_big_endian=False`` is the other reading, should a client
    disagree.
    """

    name = "new"

    def __init__(self, key, iv, aad, client=False, dynamic_big_endian=True):
        if len(iv) != IV_BYTES:
            raise ValueError("IV is %d bytes, not %d" % (len(iv), IV_BYTES))
        if len(aad) != AAD_BYTES:
            raise ValueError("additional key is %d bytes, not %d"
                             % (len(aad), AAD_BYTES))
        self._gcm = Gcm(key)
        self._aad = aad
        self._static = iv[:STATIC_IV_BYTES]
        self._format = ">q" if dynamic_big_endian else "<q"
        start = struct.unpack(self._format, iv[STATIC_IV_BYTES:])[0]
        self._sending = start
        self._receiving = start
        #: +1 towards the exchange, -1 away from it.
        self._send_step = 1 if client else -1
        self._receive_step = -self._send_step

    def _iv(self, dynamic):
        return self._static + struct.pack(self._format, _wrap(dynamic))

    def seal(self, data):
        self._sending += self._send_step
        return self._gcm.encrypt(self._iv(self._sending), data, self._aad)

    def open(self, data, checksum):
        self._receiving += self._receive_step
        if checksum is None:
            raise CryptoError("an authenticated message carries no tag")
        return self._gcm.decrypt(self._iv(self._receiving), data, checksum,
                                 self._aad)


def _wrap(value):
    """Keep a 64-bit counter inside its type as it walks past either end."""
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value
