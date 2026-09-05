"""DER, RSA and X.509, checked against literal bytes and against a real TLS
handshake -- not against this project's own decoder, because it does not have
one. :mod:`exchangesim.tls.der` only writes, so the only trustworthy check on
its output is either a known encoding transcribed from the standard, or
another implementation's parser. The handshake test in
:class:`HandshakeTest` is that second kind: if OpenSSL, through the standard
library's :mod:`ssl`, completes a verifying TLS handshake against a
certificate this module built and matches both of its Subject Alternative
Names, the DER is right end to end. Matches the structure and tone of
``tests/test_nnf_crypto.py``: literal vectors first, hand-rolled arithmetic
checked against its own definition second, and the thing that actually
matters -- a real peer accepting the output -- last.
"""

import binascii
import datetime
import hashlib
import math
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest

from exchangesim.tls import der
from exchangesim.tls import rsa
from exchangesim.tls import x509


def unhex(text):
    return binascii.unhexlify(text.replace(" ", ""))


# ---------------------------------------------------------------------------
# DER primitives
# ---------------------------------------------------------------------------

class DerIntegerTest(unittest.TestCase):
    """Every case here is checked against a literal expected encoding, never
    against a decoder of our own -- there isn't one."""

    def test_zero(self):
        self.assertEqual(der.integer(0), unhex("020100"))

    def test_127_needs_no_padding(self):
        # 127 is 0x7f: top bit already clear, one byte is enough.
        self.assertEqual(der.integer(127), unhex("02017f"))

    def test_128_needs_a_leading_zero(self):
        # The classic bug: 128 is 0x80, whose top bit is set, so a positive
        # DER INTEGER must carry a leading 0x00 or it reads as -128.
        self.assertEqual(der.integer(128), unhex("02020080"))

    def test_255_also_needs_a_leading_zero(self):
        self.assertEqual(der.integer(255), unhex("020200ff"))

    def test_negative_one(self):
        self.assertEqual(der.integer(-1), unhex("0201ff"))

    def test_a_negative_value_needing_two_bytes(self):
        # -129 does not fit in one signed byte (-128..127).
        self.assertEqual(der.integer(-129), unhex("0202ff7f"))


class DerLengthTest(unittest.TestCase):

    def test_short_form_length(self):
        self.assertEqual(der.tlv(0x04, b"x" * 10)[:2], b"\x04\x0a")

    def test_long_form_length_over_127_bytes(self):
        body = b"y" * 200
        encoded = der.tlv(0x04, body)
        # 200 = 0xc8, one length-of-length byte: 0x80 | 1 = 0x81.
        self.assertEqual(encoded[:3], b"\x04\x81\xc8")
        self.assertEqual(encoded[3:], body)

    def test_a_length_needing_two_length_bytes(self):
        body = b"z" * 300
        encoded = der.tlv(0x04, body)
        # 300 = 0x012c, two length-of-length bytes: 0x80 | 2 = 0x82.
        self.assertEqual(encoded[:4], b"\x04\x82\x01\x2c")


class DerOidTest(unittest.TestCase):

    def test_sha256_with_rsa_encryption(self):
        self.assertEqual(der.oid("1.2.840.113549.1.1.11"),
                         unhex("06092a864886f70d01010b"))

    def test_subject_alt_name(self):
        self.assertEqual(der.oid("2.5.29.17"), unhex("0603551d11"))


class DerTimeTest(unittest.TestCase):
    """RFC 5280's boundary: UTCTime through 2049, GeneralizedTime from 2050."""

    def test_utc_time_encoding(self):
        when = datetime.datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(der.utc_time(when), b"\x17\x0d260102030405Z")

    def test_generalized_time_encoding(self):
        when = datetime.datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(der.generalized_time(when),
                         b"\x18\x0f20260102030405Z")

    def test_time_picks_utc_time_up_to_2049(self):
        when = datetime.datetime(2049, 12, 31, 23, 59, 59)
        encoded = der.time(when)
        self.assertEqual(encoded[0], 0x17)
        self.assertEqual(encoded, der.utc_time(when))

    def test_time_picks_generalized_time_from_2050(self):
        when = datetime.datetime(2050, 1, 1, 0, 0, 0)
        encoded = der.time(when)
        self.assertEqual(encoded[0], 0x18)
        self.assertEqual(encoded, der.generalized_time(when))


class DerStructureTest(unittest.TestCase):

    def test_sequence_wraps_its_items_in_order(self):
        encoded = der.sequence(der.integer(1), der.integer(2))
        self.assertEqual(encoded, unhex("3006020101020102"))

    def test_boolean_true_and_false(self):
        self.assertEqual(der.boolean(True), unhex("0101ff"))
        self.assertEqual(der.boolean(False), unhex("010100"))

    def test_null(self):
        self.assertEqual(der.null(), unhex("0500"))

    def test_bit_string_carries_the_unused_bit_count(self):
        self.assertEqual(der.bit_string(b"\xa0", 5), unhex("0302" "05" "a0"))

    def test_explicit_wraps_a_complete_tlv(self):
        wrapped = der.explicit(0, der.integer(2))
        self.assertEqual(wrapped, unhex("a003020102"))

    def test_implicit_replaces_the_universal_tag(self):
        # An OCTET STRING's raw content under context tag [0], primitive.
        self.assertEqual(der.implicit(0, b"\x01\x02\x03"),
                         unhex("8003010203"))


# ---------------------------------------------------------------------------
# RSA
# ---------------------------------------------------------------------------

class RsaTest(unittest.TestCase):
    """Arithmetic checked against RSA's own definition. 1024-bit keys here --
    fast, and the arithmetic being tested does not care about key size."""

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate(bits=1024)

    def test_p_and_q_are_different_primes(self):
        self.assertNotEqual(self.key.p, self.key.q)

    def test_d_is_the_inverse_of_e_modulo_the_carmichael_totient(self):
        lam = (self.key.p - 1) // math.gcd(self.key.p - 1, self.key.q - 1) \
            * (self.key.q - 1)
        self.assertEqual((self.key.d * self.key.e) % lam, 1)

    def test_encryption_and_decryption_are_inverse(self):
        message = 424242
        ciphertext = pow(message, self.key.e, self.key.n)
        self.assertEqual(pow(ciphertext, self.key.d, self.key.n), message)

    def test_signature_verifies_and_carries_the_known_digest_info_prefix(self):
        data = b"the message that gets signed"
        signature = self.key.sign_sha256(data)

        modulus_bytes = (self.key.bits + 7) // 8
        self.assertEqual(len(signature), modulus_bytes)

        # Recover the padded message by applying the public exponent -- the
        # signature "verifies" exactly when this reproduces what was signed.
        recovered = pow(int.from_bytes(signature, "big"), self.key.e,
                        self.key.n)
        em = recovered.to_bytes(modulus_bytes, "big")
        self.assertEqual(em[:2], b"\x00\x01")

        separator = em.index(b"\x00", 2)
        self.assertTrue(all(b == 0xff for b in bytearray(em[2:separator])))

        digest_info = em[separator + 1:]
        digest = hashlib.sha256(data).digest()
        self.assertEqual(digest_info[-32:], digest)
        # The well-known DigestInfo prefix for SHA-256, built through der.py
        # rather than hard-coded in rsa.py -- pinned here so it cannot drift.
        self.assertEqual(binascii.hexlify(digest_info[:-32]),
                         b"3031300d060960864801650304020105000420")

    def test_pem_round_trips(self):
        original = self.key.private_der()
        pem = rsa.to_pem(original, "RSA PRIVATE KEY")
        self.assertIn("-----BEGIN RSA PRIVATE KEY-----", pem)
        self.assertIn("-----END RSA PRIVATE KEY-----", pem)
        self.assertEqual(rsa.from_pem(pem, "RSA PRIVATE KEY"), original)

    def test_pem_wraps_at_64_columns(self):
        pem = rsa.to_pem(self.key.private_der(), "RSA PRIVATE KEY")
        body_lines = pem.strip().splitlines()[1:-1]
        for line in body_lines[:-1]:
            self.assertEqual(len(line), 64)


# ---------------------------------------------------------------------------
# The handshake: the decisive test
# ---------------------------------------------------------------------------

class _Endpoint(object):
    """One side of an in-memory TLS connection: the SSLObject plus the pair
    of MemoryBIOs feeding it, so a pump loop can move bytes between two of
    these without reaching into ssl internals."""

    __slots__ = ("ssl_obj", "incoming", "outgoing", "done")

    def __init__(self, ssl_obj, incoming, outgoing):
        self.ssl_obj = ssl_obj
        self.incoming = incoming
        self.outgoing = outgoing
        self.done = False


def _relay(source, destination):
    # type: (_Endpoint, _Endpoint) -> bool
    chunk = source.outgoing.read()
    if chunk:
        destination.incoming.write(chunk)
        return True
    return False


def _pump_handshake(client, server, rounds=20):
    """Drive both sides' ``do_handshake()`` to completion by hand, moving
    bytes through the MemoryBIOs in between attempts. Bounded: a handshake
    that has not converged in twenty rounds is a bug, not something to wait
    out. An exception other than ``SSLWantReadError`` -- a failed
    verification, most importantly -- is not caught here and propagates to
    the caller, which is what lets :class:`HandshakeTest`'s negative case use
    ``assertRaises`` around this function directly.
    """
    for _ in range(rounds):
        if not client.done:
            try:
                client.ssl_obj.do_handshake()
                client.done = True
            except ssl.SSLWantReadError:
                pass
        if not server.done:
            try:
                server.ssl_obj.do_handshake()
                server.done = True
            except ssl.SSLWantReadError:
                pass
        _relay(client, server)
        _relay(server, client)
        if client.done and server.done:
            return
    raise AssertionError("TLS handshake did not complete within %d rounds"
                         % rounds)


def _send_and_receive(sender, receiver, data, rounds=20):
    """Send ``data`` from one endpoint to the other over the same BIO-pumping
    style as the handshake, and return what the receiver read."""
    sender.ssl_obj.write(data)
    for _ in range(rounds):
        moved = _relay(sender, receiver)
        try:
            received = receiver.ssl_obj.read(len(data) + 64)
        except ssl.SSLWantReadError:
            received = None
        if received:
            return received
        if not moved:
            continue
    raise AssertionError("application data did not arrive within %d rounds"
                         % rounds)


def _write_pem(path, text):
    with open(path, "wb") as handle:
        handle.write(text.encode("ascii"))


class HandshakeTest(unittest.TestCase):
    """Build a CA and a leaf certificate for ``localhost`` with this module
    alone, then hand them to real ``ssl.SSLContext`` objects and run an
    actual handshake over in-memory BIOs.

    Keys are generated once for the whole class, not per test -- RSA-2048
    keygen is the expensive part here, and none of these tests need a fresh
    key. ``now`` and ``serial`` are fixed so a failure is reproducible rather
    than depending on the clock or on which random serial happened to come
    up.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        now = datetime.datetime(2026, 1, 1, 0, 0, 0)

        cls.ca_key = rsa.generate(2048)
        cls.leaf_key = rsa.generate(2048)
        cls.wrong_ca_key = rsa.generate(2048)

        cls.ca_cert = x509.self_signed_ca(
            cls.ca_key, "ExchangeSim Test CA", "ExchangeSim", days=3650,
            serial=1, now=now)
        ca_subject = x509.subject_name("ExchangeSim Test CA", "ExchangeSim")
        ca_key_id = x509.key_identifier(cls.ca_key)
        cls.leaf_cert = x509.sign_leaf(
            cls.ca_key, ca_subject, ca_key_id, cls.leaf_key, "localhost",
            "ExchangeSim", ["localhost", "127.0.0.1"], days=825, serial=2,
            now=now)
        cls.wrong_ca_cert = x509.self_signed_ca(
            cls.wrong_ca_key, "A Different CA", "ExchangeSim", days=3650,
            serial=1, now=now)

        cls.ca_pem = os.path.join(cls.tmp.name, "ca.pem")
        cls.ca_key_pem = os.path.join(cls.tmp.name, "ca-key.pem")
        cls.leaf_pem = os.path.join(cls.tmp.name, "leaf.pem")
        cls.leaf_key_pem = os.path.join(cls.tmp.name, "leaf-key.pem")
        cls.wrong_ca_pem = os.path.join(cls.tmp.name, "wrong-ca.pem")

        _write_pem(cls.ca_pem, rsa.to_pem(cls.ca_cert, "CERTIFICATE"))
        _write_pem(cls.ca_key_pem,
                   rsa.to_pem(cls.ca_key.private_der(), "RSA PRIVATE KEY"))
        _write_pem(cls.leaf_pem, rsa.to_pem(cls.leaf_cert, "CERTIFICATE"))
        _write_pem(cls.leaf_key_pem,
                   rsa.to_pem(cls.leaf_key.private_der(), "RSA PRIVATE KEY"))
        _write_pem(cls.wrong_ca_pem,
                   rsa.to_pem(cls.wrong_ca_cert, "CERTIFICATE"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _build_endpoints(self, trusted_ca_pem):
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(self.leaf_pem, self.leaf_key_pem)

        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.load_verify_locations(trusted_ca_pem)
        client_ctx.check_hostname = True
        client_ctx.verify_mode = ssl.CERT_REQUIRED

        client_incoming, client_outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_incoming, server_outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()

        client_obj = client_ctx.wrap_bio(
            client_incoming, client_outgoing, server_hostname="localhost")
        server_obj = server_ctx.wrap_bio(
            server_incoming, server_outgoing, server_side=True)

        client = _Endpoint(client_obj, client_incoming, client_outgoing)
        server = _Endpoint(server_obj, server_incoming, server_outgoing)
        return client, server

    def test_a_verifying_client_completes_the_handshake_and_exchanges_data(self):
        client, server = self._build_endpoints(self.ca_pem)
        _pump_handshake(client, server)

        from_client = _send_and_receive(client, server, b"hello from the client")
        self.assertEqual(from_client, b"hello from the client")
        from_server = _send_and_receive(server, client, b"hello from the server")
        self.assertEqual(from_server, b"hello from the server")

        peer_cert = client.ssl_obj.getpeercert()
        san_values = [value for kind, value in peer_cert.get("subjectAltName", ())]
        self.assertIn("localhost", san_values)
        self.assertIn("127.0.0.1", san_values)

    def test_a_client_trusting_a_different_ca_refuses_the_handshake(self):
        # Proof the first test is actually verifying something: point the
        # client at an unrelated, independently generated CA and the same
        # handshake must fail rather than pass regardless of the DER.
        client, server = self._build_endpoints(self.wrong_ca_pem)
        with self.assertRaises(ssl.SSLError):
            _pump_handshake(client, server)

    def test_openssl_verifies_the_chain(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl binary not found on PATH")
        result = subprocess.run(
            [openssl, "verify", "-CAfile", self.ca_pem, self.leaf_pem],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)

    def test_openssl_reports_both_sans_and_ca_false(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl binary not found on PATH")
        result = subprocess.run(
            [openssl, "x509", "-in", self.leaf_pem, "-text", "-noout"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DNS:localhost", result.stdout)
        self.assertIn("IP Address:127.0.0.1", result.stdout)
        self.assertIn("CA:FALSE", result.stdout)


if __name__ == "__main__":
    unittest.main()
