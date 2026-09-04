"""AES-256-GCM, against published vectors rather than against itself.

A hand-written cipher that only ever talks to its own decryptor is worth
nothing: it round-trips whatever it does. Everything structural here is pinned
to FIPS-197 and to the GCM specification's test cases, and the two IV lengths
that are *not* ninety-six bits are included deliberately -- NSE's IV is sixteen
bytes, so the ``J0 = GHASH(H, {}, IV)`` path is the one that runs in production
and the fast path is the one that never does.
"""

import binascii
import struct
import unittest

from exchangesim.nnf import crypto
from exchangesim.nnf.crypto import (
    Aes,
    CryptoError,
    ExistingCipher,
    Gcm,
    NewCipher,
    PlainCipher,
)


def unhex(text):
    return binascii.unhexlify(text.replace(" ", ""))


class AesTest(unittest.TestCase):
    """FIPS-197 Appendix C.3, the AES-256 known-answer test."""

    KEY = unhex("000102030405060708090a0b0c0d0e0f"
                "101112131415161718191a1b1c1d1e1f")
    PLAIN = unhex("00112233445566778899aabbccddeeff")
    CIPHER = unhex("8ea2b7ca516745bfeafc49904b496089")

    def test_known_answer(self):
        self.assertEqual(Aes(self.KEY).encrypt_block(self.PLAIN), self.CIPHER)

    def test_only_a_256_bit_key_is_accepted(self):
        self.assertRaises(ValueError, Aes, self.KEY[:16])

    def test_the_sbox_is_the_real_one(self):
        # Derived rather than tabulated, so check the corners against FIPS-197.
        self.assertEqual(crypto.SBOX[0x00], 0x63)
        self.assertEqual(crypto.SBOX[0x53], 0xED)
        self.assertEqual(crypto.SBOX[0xFF], 0x16)
        self.assertEqual(len(set(crypto.SBOX)), 256)


class GcmVectorTest(unittest.TestCase):
    """The GCM specification's AES-256 test cases, 13 through 18."""

    ZERO_KEY = b"\x00" * 32
    KEY = unhex("feffe9928665731c6d6a8f9467308308"
                "feffe9928665731c6d6a8f9467308308")
    PLAIN = unhex("d9313225f88406e5a55909c5aff5269a"
                  "86a7a9531534f7da2e4c303d8a318a72"
                  "1c3c0c95956809532fcf0e2449a6b525"
                  "b16aedf5aa0de657ba637b39")
    AAD = unhex("feedfacedeadbeeffeedfacedeadbeefabaddad2")

    def check(self, key, iv, plain, aad, cipher, tag):
        gcm = Gcm(key)
        produced, produced_tag = gcm.encrypt(iv, plain, aad)
        self.assertEqual(binascii.hexlify(produced), binascii.hexlify(cipher))
        self.assertEqual(binascii.hexlify(produced_tag), binascii.hexlify(tag))
        self.assertEqual(gcm.decrypt(iv, cipher, tag, aad), plain)

    def test_case_13_empty_everything(self):
        self.check(self.ZERO_KEY, b"\x00" * 12, b"", b"", b"",
                   unhex("530f8afbc74536b9a963b4f1c4cb738b"))

    def test_case_14_one_block_of_zeros(self):
        self.check(self.ZERO_KEY, b"\x00" * 12, b"\x00" * 16, b"",
                   unhex("cea7403d4d606b6e074ec5d3baf39d18"),
                   unhex("d0d1c8a799996bf0265b98b5d48ab919"))

    def test_case_16_ninety_six_bit_iv_with_aad(self):
        self.check(self.KEY, unhex("cafebabefacedbaddecaf888"), self.PLAIN,
                   self.AAD,
                   unhex("522dc1f099567d07f47f37a32a84427d"
                         "643a8cdcbfe5c0c97598a2bd2555d1aa"
                         "8cb08e48590dbb3da7b08b1056828838"
                         "c5f61e6393ba7a0abcc9f662"),
                   unhex("76fc6ece0f4e1768cddf8853bb2d551b"))

    def test_case_17_short_iv_takes_the_ghash_path(self):
        self.check(self.KEY, unhex("cafebabefacedbad"), self.PLAIN, self.AAD,
                   unhex("c3762df1ca787d32ae47c13bf19844cb"
                         "af1ae14d0b976afac52ff7d79bba9de0"
                         "feb582d33934a4f0954cc2363bc73f78"
                         "62ac430e64abe499f47c9b1f"),
                   unhex("3a337dbf46a792c45e454913fe2ea8f2"))

    def test_case_18_long_iv_takes_the_ghash_path(self):
        self.check(self.KEY,
                   unhex("9313225df88406e555909c5aff5269aa"
                         "6a7a9538534f7da1e4c303d2a318a728"
                         "c3c0c95156809539fcf0e2429a6b5254"
                         "16aedbf5a0de6a57a637b39b"),
                   self.PLAIN, self.AAD,
                   unhex("5a8def2f0c9e53f1f75d7853659e2a20"
                         "eeb2b22aafde6419a058ab4f6f746bf4"
                         "0fc0c3b780f244452da3ebf1c5d82cde"
                         "a2418997200ef82e44ae7e3f"),
                   unhex("a44a8266ee1c8eb0c8b5d4cf5ae9f19a"))

    def test_a_sixteen_byte_iv_is_not_the_fast_path(self):
        # NSE's IV length. The counter must come from GHASH; a fast-path
        # implementation would answer with the IV itself plus one.
        gcm = Gcm(self.ZERO_KEY)
        iv = b"\x01" * 16
        self.assertNotEqual(gcm.initial_counter(iv),
                            int(binascii.hexlify(iv + b"\x00\x00\x00\x01")[:32], 16))

    def test_a_tampered_tag_does_not_verify(self):
        gcm = Gcm(self.KEY)
        iv = b"\x02" * 16
        cipher, tag = gcm.encrypt(iv, b"payload", self.AAD)
        broken = bytearray(tag)
        broken[0] ^= 0x01
        self.assertRaises(CryptoError, gcm.decrypt, iv, cipher,
                          bytes(broken), self.AAD)

    def test_the_aad_is_authenticated_not_encrypted(self):
        gcm = Gcm(self.KEY)
        iv = b"\x03" * 16
        cipher, tag = gcm.encrypt(iv, b"payload", b"aad-one-here")
        self.assertRaises(CryptoError, gcm.decrypt, iv, cipher, tag,
                          b"aad-two-here")


class KeystreamTest(unittest.TestCase):
    """The offset that makes a continuous stream across messages possible."""

    def test_a_split_message_matches_the_whole_one(self):
        gcm = Gcm(b"\x07" * 32)
        counter = gcm.initial_counter(b"\x09" * 16)
        data = bytes(bytearray(i % 256 for i in range(70)))

        whole = gcm.apply_keystream(counter, data)
        first = gcm.apply_keystream(counter, data[:23])
        second = gcm.apply_keystream(counter, data[23:], skip=23)
        self.assertEqual(first + second, whole)

    def test_the_split_crosses_a_block_boundary(self):
        # 23 is deliberately not a multiple of 16: if the offset were rounded to
        # a block, this is where it would show.
        gcm = Gcm(b"\x07" * 32)
        counter = gcm.initial_counter(b"\x09" * 16)
        self.assertNotEqual(gcm.apply_keystream(counter, b"x" * 8, skip=23),
                            gcm.apply_keystream(counter, b"x" * 8, skip=16))


class PlainCipherTest(unittest.TestCase):

    def test_the_message_travels_as_it_is_with_an_md5(self):
        from exchangesim.nnf.packet import md5_digest
        cipher = PlainCipher()
        sealed, checksum = cipher.seal(b"message data")
        self.assertEqual(sealed, b"message data")
        self.assertEqual(checksum, md5_digest(b"message data"))
        self.assertEqual(cipher.open(sealed, checksum), b"message data")

    def test_a_wrong_checksum_is_refused(self):
        self.assertRaises(CryptoError, PlainCipher().open, b"data", b"\x00" * 16)


class ExistingCipherTest(unittest.TestCase):
    """One keystream per direction, running on across messages."""

    KEY = b"\x11" * 32
    IV = b"\x22" * 16

    def pair(self):
        return ExistingCipher(self.KEY, self.IV), ExistingCipher(self.KEY, self.IV)

    def test_a_message_round_trips_between_two_endpoints(self):
        sender, receiver = self.pair()
        sealed, checksum = sender.seal(b"the first message")
        self.assertEqual(receiver.open(sealed, checksum), b"the first message")

    def test_the_ciphertext_is_not_the_plaintext(self):
        sender, _ = self.pair()
        sealed, _ = sender.seal(b"the first message")
        self.assertNotEqual(sealed, b"the first message")

    def test_the_checksum_is_over_the_plaintext_not_the_ciphertext(self):
        from exchangesim.nnf.packet import md5_digest
        sender, _ = self.pair()
        sealed, checksum = sender.seal(b"the first message")
        self.assertEqual(checksum, md5_digest(b"the first message"))
        self.assertNotEqual(checksum, md5_digest(sealed))

    def test_the_stream_continues_across_messages(self):
        # Three messages of thirteen bytes: none of them lands on a block
        # boundary, so a cipher that restarted the keystream per message would
        # decrypt the first correctly and nothing after it.
        sender, receiver = self.pair()
        for text in (b"message one..", b"message two..", b"message three"):
            sealed, checksum = sender.seal(text)
            self.assertEqual(receiver.open(sealed, checksum), text)

    def test_the_same_plaintext_twice_gives_different_ciphertext(self):
        sender, _ = self.pair()
        first, _ = sender.seal(b"repeated")
        second, _ = sender.seal(b"repeated")
        self.assertNotEqual(first, second)

    def test_the_two_directions_keep_separate_offsets(self):
        # A box sends and receives on one connection, and OpenSSL gives each
        # direction its own context. Sharing one offset would desynchronise as
        # soon as the traffic was not perfectly alternating.
        one, two = self.pair()
        sealed, checksum = one.seal(b"from one to two")
        one.seal(b"and another from one")
        self.assertEqual(two.open(sealed, checksum), b"from one to two")

    def test_a_corrupted_message_fails_its_checksum(self):
        sender, receiver = self.pair()
        sealed, checksum = sender.seal(b"the first message")
        broken = bytearray(sealed)
        broken[0] ^= 0xFF
        self.assertRaises(CryptoError, receiver.open, bytes(broken), checksum)


class NewCipherTest(unittest.TestCase):
    """Authenticated GCM, with the IV walking in opposite directions."""

    KEY = b"\x33" * 32
    AAD = b"\x44" * 12

    def iv(self, dynamic=1000):
        return b"\x55" * 8 + struct.pack(">q", dynamic)

    def pair(self):
        """A member and the exchange, both holding what the Gateway Router issued."""
        return (NewCipher(self.KEY, self.iv(), self.AAD, client=True),
                NewCipher(self.KEY, self.iv(), self.AAD))

    def test_a_message_round_trips_between_two_endpoints(self):
        sender, receiver = self.pair()
        sealed, tag = sender.seal(b"the first message")
        self.assertEqual(receiver.open(sealed, tag), b"the first message")

    def test_the_checksum_field_carries_a_tag_not_a_digest(self):
        from exchangesim.nnf.packet import md5_digest
        sender, _ = self.pair()
        sealed, tag = sender.seal(b"the first message")
        self.assertEqual(len(tag), 16)
        self.assertNotEqual(tag, md5_digest(b"the first message"))
        self.assertNotEqual(tag, md5_digest(sealed))

    def test_several_messages_stay_in_step(self):
        sender, receiver = self.pair()
        for index in range(5):
            text = b"message %d" % index
            sealed, tag = sender.seal(text)
            self.assertEqual(receiver.open(sealed, tag), text)

    def test_the_counter_rises_towards_the_exchange_and_falls_away_from_it(self):
        # The asymmetry the document specifies, from the member's side. Both
        # ends start at 1000: the member seals its first message at 1001 and the
        # exchange opens it at 1001, while the exchange seals its own first
        # message at 999 and the member opens it at 999. Two sequences walking
        # away from one origin. Written from one side only, as the document
        # states it, the two ends would move the same copy the same way and
        # never meet -- which is what this pins down.
        member, exchange = self.pair()

        sealed, tag = member.seal(b"to the exchange")
        self.assertEqual(member._sending, 1001)
        self.assertEqual(exchange.open(sealed, tag), b"to the exchange")
        self.assertEqual(exchange._receiving, 1001)

        sealed, tag = exchange.seal(b"to the member")
        self.assertEqual(exchange._sending, 999)
        self.assertEqual(member.open(sealed, tag), b"to the member")
        self.assertEqual(member._receiving, 999)

    def test_both_directions_travel_on_one_connection(self):
        member, exchange = self.pair()
        for index in range(3):
            request = b"request %d" % index
            sealed, tag = member.seal(request)
            self.assertEqual(exchange.open(sealed, tag), request)

            response = b"response %d" % index
            sealed, tag = exchange.seal(response)
            self.assertEqual(member.open(sealed, tag), response)

    def test_the_two_directions_never_share_an_iv(self):
        # GCM's one hard requirement: an IV must not repeat under a key. A
        # single shared counter would repeat one the moment both ends spoke.
        member, exchange = self.pair()
        seen = set()
        for _ in range(4):
            member.seal(b"up")
            exchange.seal(b"down")
            seen.add(member._iv(member._sending))
            seen.add(exchange._iv(exchange._sending))
        self.assertEqual(len(seen), 8)

    def test_a_message_out_of_step_does_not_authenticate(self):
        sender, receiver = self.pair()
        sender.seal(b"a message the receiver never sees")
        sealed, tag = sender.seal(b"the one it does")
        self.assertRaises(CryptoError, receiver.open, sealed, tag)

    def test_a_tampered_message_does_not_authenticate(self):
        sender, receiver = self.pair()
        sealed, tag = sender.seal(b"the first message")
        broken = bytearray(sealed)
        broken[0] ^= 0xFF
        self.assertRaises(CryptoError, receiver.open, bytes(broken), tag)

    def test_a_missing_tag_is_refused_rather_than_ignored(self):
        _, receiver = self.pair()
        self.assertRaises(CryptoError, receiver.open, b"data", None)

    def test_the_dynamic_half_can_be_read_little_endian(self):
        # ASSUMPTION: the document does not say how the incremented counter is
        # laid out. Big-endian matches the rest of the protocol and is the
        # default; this is the other reading, kept reachable rather than
        # rewritten under a client.
        little = NewCipher(self.KEY, b"\x55" * 8 + struct.pack("<q", 5),
                           self.AAD, dynamic_big_endian=False)
        self.assertEqual(little._iv(6), b"\x55" * 8 + struct.pack("<q", 6))

    def test_the_counter_wraps_rather_than_overflowing(self):
        highest = NewCipher(self.KEY, b"\x55" * 8 + struct.pack(">q", 2 ** 63 - 1),
                            self.AAD)
        sealed, tag = highest.seal(b"payload")
        self.assertEqual(len(sealed), len(b"payload"))
        self.assertEqual(len(tag), 16)

    def test_the_key_iv_and_additional_key_widths_are_checked(self):
        self.assertRaises(ValueError, NewCipher, self.KEY, b"\x00" * 12, self.AAD)
        self.assertRaises(ValueError, NewCipher, self.KEY, self.iv(), b"\x00" * 8)


if __name__ == "__main__":
    unittest.main()
