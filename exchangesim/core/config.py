"""Configuration loading.

Plain JSON -- no YAML, since that would mean a third-party parser. Paths inside
a config file are resolved relative to the file's own directory, so a config
plus its reference data can be copied around as a unit.
"""

import json
import os


class ConfigError(Exception):
    """Raised for a missing or malformed configuration value."""


class Config(object):
    """Read-only view over a parsed JSON config.

    Values are addressed by dotted path, which keeps call sites readable and
    lets errors name the exact key that was wrong::

        cfg.require("fix.port")
        cfg.get("fix.heartbeat_interval", 30)
    """

    __slots__ = ("_data", "_path", "_base")

    def __init__(self, data, path=None):
        # type: (dict, str) -> None
        self._data = data
        self._path = path
        self._base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()

    @classmethod
    def load(cls, path):
        # type: (str) -> Config
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
        except (IOError, OSError) as exc:
            raise ConfigError("cannot read config %s: %s" % (path, exc))
        except ValueError as exc:
            raise ConfigError("invalid JSON in %s: %s" % (path, exc))
        if not isinstance(data, dict):
            raise ConfigError("config %s must contain a JSON object" % path)
        return cls(data, path)

    @property
    def path(self):
        return self._path

    @property
    def base_dir(self):
        """Directory the config was loaded from; the root for relative paths."""
        return self._base

    @property
    def data(self):
        return self._data

    def get(self, dotted, default=None):
        """Look up a dotted path, returning ``default`` if any segment is absent."""
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted):
        """Look up a dotted path, raising :class:`ConfigError` if absent."""
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError("missing required config key '%s' in %s"
                              % (dotted, self._path or "<memory>"))
        return value

    def section(self, dotted):
        """Return a sub-object as a :class:`Config` sharing this base directory."""
        value = self.get(dotted, {})
        if not isinstance(value, dict):
            raise ConfigError("config key '%s' must be an object" % dotted)
        child = Config(value, None)
        child._base = self._base
        return child

    def resolve_path(self, dotted, default=None):
        """Look up a path value and resolve it against :attr:`base_dir`."""
        value = self.get(dotted, default)
        if value is None:
            return None
        if os.path.isabs(value):
            return value
        return os.path.normpath(os.path.join(self._base, value))
