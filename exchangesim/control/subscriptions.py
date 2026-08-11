"""Topic-based publish/subscribe for streaming control-plane events.

Topics are colon-delimited strings (``trade:7203``, ``book:DAY:7203``,
``state:DAY``). Subscribers register :mod:`fnmatch` patterns, so a CLI can ask
for ``trade:*`` or a single symbol without the publisher knowing anything about
the subscriber.

Publishing is cheap when nobody is listening, which matters because the engine
publishes on every book change.
"""

import fnmatch
import logging

log = logging.getLogger(__name__)


class Publisher(object):
    """Fan-out of events to subscribed control sessions."""

    def __init__(self):
        # session -> set of patterns
        self._subscribers = {}

    def subscribe(self, session, patterns):
        # type: (object, list) -> list
        """Add patterns for a session. Returns the session's full pattern set."""
        current = self._subscribers.setdefault(session, set())
        current.update(patterns)
        return sorted(current)

    def unsubscribe(self, session, patterns=None):
        """Drop patterns, or the whole subscription when ``patterns`` is None."""
        if patterns is None:
            self._subscribers.pop(session, None)
            return []
        current = self._subscribers.get(session)
        if current is None:
            return []
        current.difference_update(patterns)
        if not current:
            self._subscribers.pop(session, None)
            return []
        return sorted(current)

    def remove(self, session):
        """Forget a session entirely; called when its connection closes."""
        self._subscribers.pop(session, None)

    def patterns_for(self, session):
        return sorted(self._subscribers.get(session, ()))

    @property
    def has_subscribers(self):
        return bool(self._subscribers)

    def publish(self, topic, data):
        # type: (str, dict) -> int
        """Send ``data`` to every session whose patterns match ``topic``.

        Returns the number of sessions delivered to. Delivery failures are
        swallowed -- a dead subscriber must never disrupt the matching engine.
        """
        if not self._subscribers:
            return 0
        delivered = 0
        for session, patterns in list(self._subscribers.items()):
            if not _matches(topic, patterns):
                continue
            try:
                session.push(topic, data)
                delivered += 1
            except Exception:
                log.exception("failed pushing %s to subscriber", topic)
        return delivered


def _matches(topic, patterns):
    for pattern in patterns:
        if pattern == topic or fnmatch.fnmatchcase(topic, pattern):
            return True
    return False
