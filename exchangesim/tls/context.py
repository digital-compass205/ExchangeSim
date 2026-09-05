"""Turning a venue's ``*.tls`` setting into an ``ssl.SSLContext``, or nothing.

This module is standalone in the same sense as the rest of :mod:`exchangesim.tls`
-- it imports nothing from the project, not even the configuration error type a
caller will want to raise instead of :class:`TlsUnavailable`. That translation
belongs to the caller (a venue's config loader), which knows the setting's dotted
name and how its own error type is constructed; this module only knows TLS.

**Version pinning is a feature test, not a version test.** Python 3.7 added
``SSLContext.minimum_version`` / ``maximum_version`` and ``ssl.TLSVersion``;
3.6.8 has neither and pins a range only through the ``OP_NO_TLSv1*`` option
bits. Both attributes exist and behave identically on every 3.7+ interpreter
this project might run under, so the right test is ``hasattr(ssl, "TLSVersion")``,
never ``sys.version_info`` -- a check by name survives whichever interpreter
patch happens to be on the box, a check by version does not.

That split matters here for a second reason: as of Python 3.14 (verified
directly), assigning to ``SSLContext.options`` with an ``OP_NO_TLSv1*`` bit set
raises a ``DeprecationWarning``. Under this project's own test runs that is
harmless noise; under a caller that has turned warnings into errors it would be
a crash for code that has a non-deprecated way to do the same thing available.
So the ``OP_NO_*`` path is reached only on an interpreter that has no
alternative at all.
"""

import ssl

#: The three settings a venue's ``*.tls`` config accepts.
POLICIES = ("1.3", "1.2", "none")


class TlsUnavailable(Exception):
    """The running interpreter cannot honour the requested TLS policy.

    Never :class:`exchangesim.core.config.ConfigError` -- this module imports
    nothing from the project, so the caller that knows about config errors is
    the one that must catch this and re-raise as one.
    """


def _has_version_pinning():
    # type: () -> bool
    return hasattr(ssl, "TLSVersion")


def describe_unavailable(policy, setting_name="tls"):
    # type: (str, str) -> str
    """Why ``policy`` cannot be honoured here, and what to do about it.

    Only ``"1.3"`` is ever actually unavailable -- ``"1.2"`` and ``"none"``
    work on every interpreter this project runs on -- so this always describes
    the TLS 1.3 case. Called both to build :class:`TlsUnavailable`'s message
    and by a caller that wants to explain a policy choice before attempting it.
    """
    return (
        "%s '1.3' needs an interpreter linked against OpenSSL 1.1.1 or later; "
        "this one has %s. Set it to '1.2' for local development, or 'none' "
        "for plain TCP." % (setting_name, _openssl_short_version())
    )


def _openssl_short_version():
    # type: () -> str
    """``"OpenSSL 1.0.2q"``, without the build date ``ssl.OPENSSL_VERSION``
    also carries -- the date is noise in an error message about a protocol
    version."""
    return ssl.OPENSSL_VERSION.split("  ")[0]


def available(policy):
    # type: (str) -> bool
    """Whether this interpreter can honour ``policy`` at all.

    ``"none"`` and ``"1.2"`` are always available; ``"1.3"`` only where
    ``ssl.HAS_TLSv1_3`` is true, which is exactly the OpenSSL-1.1.1-or-later
    condition :func:`describe_unavailable` explains.
    """
    if policy not in POLICIES:
        raise ValueError("unknown tls policy %r; expected one of %s"
                          % (policy, POLICIES))
    if policy == "1.3":
        return ssl.HAS_TLSv1_3
    return True


def _pin_version(context, policy, setting_name):
    # type: (ssl.SSLContext, str, str) -> None
    """Restrict ``context`` to ``policy``, raising :class:`TlsUnavailable`
    first if the interpreter cannot do it at all.

    ``"1.3"`` pins **both** ``minimum_version`` and ``maximum_version`` to TLS
    1.3 -- not just a floor -- because that is what actually matches a real
    client that calls both ``SSL_CTX_set_min_proto_version`` and
    ``set_max_proto_version`` with ``TLS1_3_VERSION``; a floor alone would
    still negotiate 1.3 against such a client but would also silently accept
    a 1.2 handshake this policy is meant to refuse. ``"1.2"`` sets only a
    floor -- 1.2 or anything newer the interpreter happens to support -- so a
    security-conscious minimum does not become a ceiling nobody asked for.
    """
    if policy == "1.3":
        if not ssl.HAS_TLSv1_3:
            raise TlsUnavailable(describe_unavailable(policy, setting_name))
        if _has_version_pinning():
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.maximum_version = ssl.TLSVersion.TLSv1_3
        else:
            context.options |= (
                ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_TLSv1_2)
    elif policy == "1.2":
        if _has_version_pinning():
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        else:
            context.options |= (ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1)
    else:
        raise ValueError("unknown tls policy %r; expected one of %s"
                          % (policy, POLICIES))


def server_context(policy, certfile, keyfile, setting_name="tls"):
    # type: (str, str, str, str) -> ssl.SSLContext
    """A server-side context for ``policy``, or ``None`` for ``"none"``.

    Built on ``PROTOCOL_TLS_SERVER``, never the deprecated ``PROTOCOL_TLS``
    -- the former already implies "server side of a negotiated version",
    which is exactly what a listening venue port is.
    """
    if policy not in POLICIES:
        raise ValueError("unknown tls policy %r; expected one of %s"
                          % (policy, POLICIES))
    if policy == "none":
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    _pin_version(context, policy, setting_name)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


def client_context(policy, cafile, check_hostname=True, setting_name="tls"):
    # type: (str, str, bool, str) -> ssl.SSLContext
    """A client-side context for ``policy``, or ``None`` for ``"none"``.

    ``check_hostname=False`` also disables certificate verification
    (``CERT_NONE``) -- a client that is not checking the name has no reason
    to check the chain either, and ``ssl`` itself refuses the combination of
    ``check_hostname=True`` with ``CERT_NONE``, so the two are set together.
    Used by tests here and by the scenario runner, which is exactly why this
    is a public function rather than something folded into a venue's setup.
    """
    if policy not in POLICIES:
        raise ValueError("unknown tls policy %r; expected one of %s"
                          % (policy, POLICIES))
    if policy == "none":
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    _pin_version(context, policy, setting_name)
    context.load_verify_locations(cafile=cafile)
    if check_hostname:
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context
