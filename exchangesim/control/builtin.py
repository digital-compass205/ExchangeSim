"""Control commands that exist for every venue, independent of protocol."""

import time

from .. import __version__
from .commands import (
    CommandError,
    E_UNAUTHORISED,
    arg_list,
    arg_str,
)


def register(registry):
    """Attach the built-in command set to ``registry``.

    None of these is audited. ``ping`` and the subscription pair are session
    housekeeping a control client repeats constantly -- the web link re-sends
    both on every reconnect -- and ``auth`` carries a token, which must never
    reach a recorder.
    """

    @registry.add("ping", "Liveness check; returns server time and version.",
                  requires_auth=False, audit=False)
    def _ping(context, args):
        venue = context.venue
        return {
            "pong": True,
            "version": __version__,
            "venue": getattr(venue, "name", None),
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @registry.add("auth", "Authenticate with the shared token.",
                  requires_auth=False, audit=False)
    def _auth(context, args):
        token = arg_str(args, "token", required=True)
        server = context.server
        if not server.requires_auth:
            context.session.authenticated = True
            return {"authenticated": True, "required": False}
        # Compared in constant time: the control port may be reachable from the
        # CI network even though it defaults to loopback.
        if not _constant_time_equal(token, server.token):
            raise CommandError("invalid token", E_UNAUTHORISED)
        context.session.authenticated = True
        return {"authenticated": True, "required": True}

    # Not auth-exempt: the command list describes the venue's control surface,
    # so it stays behind the token when one is configured. Only liveness
    # (``ping``) and ``auth`` itself are reachable unauthenticated.
    @registry.add("help", "List available commands.", audit=False)
    def _help(context, args):
        return {"commands": context.server.registry.describe()}

    @registry.add("subscribe", "Stream events matching one or more topic patterns.",
                  audit=False)
    def _subscribe(context, args):
        topics = arg_list(args, "topics", required=True)
        patterns = []
        for topic in topics:
            if not isinstance(topic, str) or not topic:
                raise CommandError("each entry of 'topics' must be a non-empty string")
            patterns.append(topic)
        active = context.publisher.subscribe(context.session, patterns)
        return {"subscribed": active}

    @registry.add("unsubscribe", "Stop streaming some or all topics.", audit=False)
    def _unsubscribe(context, args):
        topics = arg_list(args, "topics", default=None)
        if topics is None:
            context.publisher.unsubscribe(context.session, None)
            return {"subscribed": []}
        active = context.publisher.unsubscribe(context.session, topics)
        return {"subscribed": active}


def _constant_time_equal(left, right):
    if left is None or right is None:
        return False
    left_bytes = left.encode("utf-8")
    right_bytes = right.encode("utf-8")
    if len(left_bytes) != len(right_bytes):
        return False
    mismatch = 0
    for a, b in zip(left_bytes, right_bytes):
        mismatch |= a ^ b
    return mismatch == 0
