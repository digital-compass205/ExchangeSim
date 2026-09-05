"""TLS support, by hand, because the standard library has no key or certificate
generation -- only the protocol runtime (`ssl`) that consumes them.

Phase 1 only: enough to produce a certificate chain OpenSSL itself will
verify. What sits here, and why:

``der.py``
    a minimal ASN.1 DER *writer* -- no parser, because nothing here ever needs
    to read a certificate back, only produce bytes a real TLS stack accepts.
``rsa.py``
    RSA-2048 keygen and PKCS#1 v1.5 SHA-256 signing, hand-rolled for the same
    reason :mod:`exchangesim.nnf.crypto` hand-rolls AES-GCM: this project runs
    on the standard library alone, and the standard library has no public-key
    cryptography.
``x509.py``
    certificate building on top of the two above: a self-signed CA and a leaf
    signed by it, with the extensions a verifying client actually checks.

Later phases add ``transport.py`` (wiring a venue's sockets to
``ssl.SSLContext``), ``context.py`` and ``certs.py`` (issuing and caching
certificates for a running simulator). None of that is here yet.
"""
