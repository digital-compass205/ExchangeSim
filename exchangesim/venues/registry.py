"""Venue lookup by config key.

Imports are deferred so that a broken or half-built venue module cannot stop
the process from starting a different one.
"""

from ..core.config import ConfigError


def _japannext():
    from .japannext.venue import JapannextVenue
    return JapannextVenue


def _hkex():
    from .hkex.venue import HkexVenue
    return HkexVenue


def _nse():
    from .nse.venue import NseVenue
    return NseVenue


def _generic():
    from .base import Venue
    return Venue


#: config ``venue`` value -> zero-argument loader returning the class.
FACTORIES = {
    "japannext": _japannext,
    "hkex": _hkex,
    "nse": _nse,
    "generic": _generic,
}


def available():
    return sorted(FACTORIES)


def create(key, config, reactor, publisher):
    """Instantiate the venue named by ``key``."""
    loader = FACTORIES.get(key)
    if loader is None:
        raise ConfigError("unknown venue '%s'; available: %s"
                          % (key, ", ".join(available())))
    return loader()(config, reactor, publisher)
