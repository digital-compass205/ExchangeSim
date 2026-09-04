"""NSE's native wire protocol, the NNF Trimmed Protocol.

This package is the third protocol stack here, beside :mod:`exchangesim.fix`
(tag=value) and :mod:`exchangesim.binary` (HKEX's OCG-C binary encoding). It is
not an encoding of FIX and does not pretend to be: it has its own sign-on, no
sequence numbers, no resend, no session-level Reject, and a two-tier session in
which one connection is a *box* carrying many signed-on users.

It knows nothing about any market segment. Capital Market lives in
``venues/nse/``; Futures & Options, when it comes, is a second dictionary and a
second set of layouts over exactly this machinery.

What sits here, and why:

``types.py``
    the data types of Chapter 2 -- big-endian, ``pragma pack 2``, blank-padded
    strings, prices in paise and times in seconds from 1 January 1980.
``packet.py``
    the Chapter 10 packet: a 22-byte prefix of length, sequence number and
    either an MD5 digest or a GCM authentication tag, then the message.
``crypto.py``
    AES-256-GCM, by hand, because the standard library has MD5 and TLS but no
    cipher.
``layout.py``
    fixed-offset structures and the bitfields inside them.
``codec.py``
    the wire seam :class:`exchangesim.fix.codec.FixCodec` names, so a session
    holds one of these without knowing which protocol it reads.
``session.py``
    the box, the users on it, and the heartbeat policing NNF specifies.
"""
