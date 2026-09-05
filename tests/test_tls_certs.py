"""``exchangesim.tls.certs.CertificateStore``: generating a CA and a server
leaf on first use, reusing them afterwards, and reissuing exactly when the
module's docstring says it should.

Almost every test here answers "does the store decide correctly when to
reissue", a question that has nothing to do with how expensive it is to make
an RSA key -- so all but one inject a ``key_source`` that hands back one of
two keys generated once, at module scope, via :func:`_fixture` (the same
pattern ``tests/test_tls_transport.py`` uses for its own certificate
material). The two keys stay full 2048-bit RSA throughout: OpenSSL 3.x's
default security level refuses a 1024-bit leaf during a real handshake, and
several tests here do perform one, so there is no cheaper substitute that
would still prove anything about a real client.

Exactly one test (:class:`DefaultKeySourceTest`) leaves ``key_source``
unset, so the default -- production's only path, ``lambda: rsa.generate(
key_bits)`` -- is still exercised end to end, keygen included.
"""

import datetime
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from exchangesim.tls import certs, context, rsa
from exchangesim.tls.transport import TlsTransport
from tests.test_tls_transport import pump

_OPENSSL = shutil.which("openssl")

_FIXTURE = {}


def _fixture():
    """Two 2048-bit RSA keys, generated once for the whole module.

    Kept as plain dict entries rather than a class (as
    ``test_tls_transport.py:_Fixture`` is) because there is only ever one
    caller of each -- :func:`_key_source` -- and nothing else here needs to
    reach into it directly.
    """
    if not _FIXTURE:
        _FIXTURE["ca_key"] = rsa.generate(2048)
        _FIXTURE["leaf_key"] = rsa.generate(2048)
    return _FIXTURE


def _key_source():
    """A fresh ``key_source`` callable for one :class:`CertificateStore`,
    round-robining the two shared fixture keys.

    :meth:`CertificateStore._generate` calls its ``key_source`` exactly
    twice per generation, CA first then leaf -- which is exactly the order
    ``itertools.cycle`` over ``(ca_key, leaf_key)`` replays, forever, no
    matter how many times one store (or several, each with their own fresh
    cycle from this function) regenerates.
    """
    fixture = _fixture()
    keys = itertools.cycle((fixture["ca_key"], fixture["leaf_key"]))
    return lambda: next(keys)


class _StoreTestCase(unittest.TestCase):
    """Gives every test here a scratch directory, cleaned up afterwards, and
    a store helper that injects the shared fixture keys by default."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="exsim-tls-certs-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _store(self, **kwargs):
        kwargs.setdefault("key_source", _key_source())
        return certs.CertificateStore(self.dir, **kwargs)

    def _paths(self, issued):
        return (issued.ca_certificate, issued.ca_key, issued.certificate,
                issued.key)


# ---------------------------------------------------------------------------
# 1. Fresh generation, and no regeneration on a second call
# ---------------------------------------------------------------------------

class GenerationTest(_StoreTestCase):

    def test_fresh_directory_generates_all_four_pems_and_metadata(self):
        store = self._store(names=("localhost", "127.0.0.1"))
        issued = store.ensure()

        for path in self._paths(issued):
            self.assertTrue(os.path.isfile(path), "missing %s" % path)
            with open(path, "r") as handle:
                text = handle.read()
            self.assertIn("-----BEGIN ", text)

        meta_path = os.path.join(self.dir, certs.META_NAME)
        self.assertTrue(os.path.isfile(meta_path))
        with open(meta_path, "r") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["names"], ["localhost", "127.0.0.1"])
        self.assertEqual(meta["common_name"], "localhost")
        self.assertEqual(meta["organisation"], "ExchangeSim")

        self.assertEqual(
            sorted(os.listdir(self.dir)),
            sorted([certs.CA_CERT_NAME, certs.CA_KEY_NAME,
                    certs.SERVER_CERT_NAME, certs.SERVER_KEY_NAME,
                    certs.META_NAME]))

    def test_a_fixed_ca_cert_file_name_is_exactly_gr_ca_cert1_pem(self):
        # Fixed by the NSE specification: a member's client is configured to
        # look for this exact file name.
        store = self._store()
        issued = store.ensure()
        self.assertEqual(os.path.basename(issued.ca_certificate),
                          "gr_ca_cert1.pem")

    def test_ensure_twice_does_not_regenerate(self):
        store = self._store()
        issued = store.ensure()
        mtimes = {path: os.path.getmtime(path) for path in self._paths(issued)}

        issued_again = store.ensure()

        self.assertEqual(issued.fingerprint, issued_again.fingerprint)
        for path in self._paths(issued_again):
            self.assertEqual(mtimes[path], os.path.getmtime(path))


# ---------------------------------------------------------------------------
# 2. Regeneration triggers
# ---------------------------------------------------------------------------

class RegenerationTest(_StoreTestCase):

    def test_deleting_any_one_pem_forces_regeneration(self):
        names = (certs.CA_CERT_NAME, certs.CA_KEY_NAME,
                  certs.SERVER_CERT_NAME, certs.SERVER_KEY_NAME)
        for name in names:
            with self.subTest(name=name):
                store = self._store()
                issued = store.ensure()
                os.remove(os.path.join(self.dir, name))

                regenerated = store.ensure()

                self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)
                for path in self._paths(regenerated):
                    self.assertTrue(os.path.isfile(path))

    def test_missing_issued_json_forces_regeneration(self):
        store = self._store()
        issued = store.ensure()
        os.remove(os.path.join(self.dir, certs.META_NAME))

        regenerated = store.ensure()

        self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, certs.META_NAME)))

    def test_corrupt_issued_json_forces_regeneration(self):
        store = self._store()
        issued = store.ensure()
        with open(os.path.join(self.dir, certs.META_NAME), "w") as handle:
            handle.write("{not valid json")

        regenerated = store.ensure()

        self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)

    def test_issued_json_missing_a_key_forces_regeneration(self):
        store = self._store()
        issued = store.ensure()
        with open(os.path.join(self.dir, certs.META_NAME), "w") as handle:
            json.dump({"names": ["localhost"]}, handle)

        regenerated = store.ensure()

        self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)


class ExpiryTest(_StoreTestCase):

    def test_not_after_inside_the_renewal_window_forces_regeneration(self):
        clock = [datetime.datetime(2026, 1, 1)]
        store = self._store(leaf_days=365, renew_within_days=30,
                             now=lambda: clock[0])
        issued = store.ensure()

        clock[0] = issued.not_after - datetime.timedelta(days=10)
        regenerated = store.ensure()

        self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)
        self.assertGreater(regenerated.not_after, issued.not_after)

    def test_not_after_comfortably_in_the_future_does_not_regenerate(self):
        clock = [datetime.datetime(2026, 1, 1)]
        store = self._store(leaf_days=365, renew_within_days=30,
                             now=lambda: clock[0])
        issued = store.ensure()

        clock[0] = issued.not_after - datetime.timedelta(days=200)
        unchanged = store.ensure()

        self.assertEqual(issued.fingerprint, unchanged.fingerprint)

    def test_not_after_already_past_forces_regeneration(self):
        clock = [datetime.datetime(2026, 1, 1)]
        store = self._store(leaf_days=10, renew_within_days=1,
                             now=lambda: clock[0])
        issued = store.ensure()

        clock[0] = issued.not_after + datetime.timedelta(days=5)
        regenerated = store.ensure()

        self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)


# ---------------------------------------------------------------------------
# 3. A changed `names` forces regeneration, and the new SAN is real
# ---------------------------------------------------------------------------

class NamesChangeTest(_StoreTestCase):

    def test_changing_names_forces_regeneration(self):
        store = self._store(names=("localhost",))
        issued = store.ensure()

        other = self._store(names=("otherhost",))
        regenerated = other.ensure()

        self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)
        self.assertEqual(regenerated.names, ("otherhost",))

    def test_the_new_certificate_actually_carries_the_new_san(self):
        store = self._store(names=("localhost",))
        store.ensure()

        other = self._store(names=("otherhost",))
        issued = other.ensure()

        server_ctx = context.server_context("1.2", issued.certificate, issued.key)
        client_ctx = context.client_context("1.2", issued.ca_certificate)
        client = TlsTransport(client_ctx, server_side=False,
                               server_hostname="otherhost")
        server = TlsTransport(server_ctx, server_side=True)

        pump(client, server)

        self.assertTrue(client.handshaken)
        self.assertTrue(server.handshaken)

    def test_common_name_or_organisation_change_also_forces_regeneration(self):
        store = self._store(names=("localhost",), organisation="ExchangeSim")
        issued = store.ensure()

        other = self._store(names=("localhost",), organisation="Somebody Else")
        regenerated = other.ensure()

        self.assertNotEqual(issued.fingerprint, regenerated.fingerprint)


# ---------------------------------------------------------------------------
# 4. describe() is safe to hand to the control plane
# ---------------------------------------------------------------------------

class DescribeTest(_StoreTestCase):

    def test_describe_carries_ca_path_and_fingerprint_only(self):
        store = self._store(names=("localhost", "127.0.0.1"))
        issued = store.ensure()

        description = store.describe()

        self.assertEqual(description["ca_certificate"], issued.ca_certificate)
        self.assertEqual(description["certificate"], issued.certificate)
        self.assertEqual(description["fingerprint"], issued.fingerprint)
        self.assertEqual(description["names"], ["localhost", "127.0.0.1"])
        self.assertIsInstance(description["not_after"], str)

    def test_describe_never_carries_a_key_path_or_pem_body(self):
        store = self._store()
        store.ensure()

        description = store.describe()
        blob = json.dumps(description)

        self.assertNotIn("key", blob.lower())
        self.assertNotIn("-----BEGIN", blob)

    def test_describe_generates_on_first_call(self):
        store = self._store()
        self.assertEqual(os.listdir(self.dir), [])

        description = store.describe()

        self.assertTrue(os.path.isfile(description["ca_certificate"]))


# ---------------------------------------------------------------------------
# 5. The fingerprint matches what `openssl` itself reports
# ---------------------------------------------------------------------------

class OpensslFingerprintTest(_StoreTestCase):

    def test_fingerprint_matches_openssl_x509_dash_fingerprint(self):
        if not _OPENSSL:
            self.skipTest("no openssl binary on PATH")

        store = self._store()
        issued = store.ensure()

        output = subprocess.check_output(
            [_OPENSSL, "x509", "-in", issued.certificate,
             "-fingerprint", "-sha256", "-noout"])
        text = output.decode("ascii").strip()
        # "SHA256 Fingerprint=AA:BB:...:CC" (older openssl: "sha256 Fingerprint=").
        self.assertIn("=", text)
        reported = text.split("=", 1)[1].strip().upper()

        self.assertEqual(reported, issued.fingerprint)


# ---------------------------------------------------------------------------
# 6. The real, uninjected default path -- keygen included -- still works
# ---------------------------------------------------------------------------

class DefaultKeySourceTest(_StoreTestCase):

    def test_default_key_source_generates_a_working_pair_end_to_end(self):
        # No key_source override: this is the one test in the module that
        # pays for a real RSA-2048 keygen (twice), covering the path every
        # other test bypasses by injecting the shared fixture keys.
        store = self._store(names=("localhost", "127.0.0.1"), key_source=None)
        issued = store.ensure()

        for path in self._paths(issued):
            self.assertTrue(os.path.isfile(path))

        server_ctx = context.server_context("1.2", issued.certificate, issued.key)
        client_ctx = context.client_context("1.2", issued.ca_certificate)
        client = TlsTransport(client_ctx, server_side=False,
                               server_hostname="localhost")
        server = TlsTransport(server_ctx, server_side=True)

        pump(client, server)

        self.assertTrue(client.handshaken)
        self.assertTrue(server.handshaken)


if __name__ == "__main__":
    unittest.main()
