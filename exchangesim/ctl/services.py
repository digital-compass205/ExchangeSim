"""What ``config/services.json`` says, turned into something startable.

A service is a name, a module to run, a config file to hand it, and the three
paths that follow from the name: a pidfile, a log and a capture file for
whatever the process says outside logging. Nothing here starts anything --
that is :mod:`exchangesim.ctl.supervisor`.
"""

import json
import os

from ..core.config import Config, ConfigError
from ..core.logutil import DEFAULT_BACKUPS, DEFAULT_MAX_BYTES

VENUE_MODULE = "exchangesim.runner.main"
WEB_MODULE = "exchangesim.web.main"

DEFAULT_LOG_DIR = "var/log"
DEFAULT_RUN_DIR = "var/run"

# Ports worth printing in `status`, in the order an operator reads them, and
# where to find each one in the service's own config file. A venue answers to
# the first four; the board only to the last.
PORT_KEYS = (
    ("fix", "fix.port"),
    ("binary", "binary.port"),
    ("nnf", "nnf.port"),
    ("router", "gateway_router.port"),
    ("control", "control.port"),
    ("http", "http.port"),
)

# Which port `start` waits for the process to answer on. The control port is
# the honest one for a venue: it comes up last, after reference data has loaded
# and the FIX acceptor has bound.
READY_KEYS = ("control.port", "http.port")


class Service(object):
    """One daemon: how to start it, and where its files live."""

    __slots__ = ("name", "module", "config_path", "args", "root",
                 "log_path", "output_path", "pid_path",
                 "max_bytes", "backups", "log_level")

    def __init__(self, name, module, config_path, root, log_dir, run_dir,
                 args=None, max_bytes=DEFAULT_MAX_BYTES,
                 backups=DEFAULT_BACKUPS, log_level=None):
        self.name = name
        self.module = module
        self.config_path = config_path
        self.root = root
        self.args = list(args or ())
        self.max_bytes = max_bytes
        self.backups = backups
        self.log_level = log_level
        self.log_path = os.path.join(log_dir, "%s.log" % name)
        self.output_path = os.path.join(log_dir, "%s.out" % name)
        self.pid_path = os.path.join(run_dir, "%s.pid" % name)

    def command(self, python):
        # type: (str) -> list
        """The argv that starts this service.

        ``--no-console`` is what keeps the log and the capture file from being
        two copies of the same thing: the daemon's stderr is redirected into
        ``.out``, so leaving the stderr handler on would write every line
        twice and rotate only one of the two files.
        """
        argv = [python, "-m", self.module, "--config", self.config_path,
                "--log-file", self.log_path, "--no-console"]
        if self.log_level:
            argv += ["--log-level", self.log_level]
        return argv + self.args

    def marker(self):
        """A string that identifies this service in a process command line.

        The module alone is not enough -- every venue runs the same one -- so
        the config path goes in too.
        """
        return os.path.basename(self.config_path)

    def ports(self):
        # type: () -> list
        """``[(label, port)]`` read from the service's own config file.

        Read rather than duplicated in ``services.json``: a port lives in
        exactly one place, and moving one cannot leave this listing stale.
        """
        data = self._config_data()
        found = []
        for label, dotted in PORT_KEYS:
            value = _dotted(data, dotted)
            if isinstance(value, int):
                found.append((label, value))
        return found

    def ready_address(self):
        """``(host, port)`` to probe for readiness, or None if unknowable."""
        data = self._config_data()
        for dotted in READY_KEYS:
            port = _dotted(data, dotted)
            if isinstance(port, int) and port > 0:
                host = _dotted(data, dotted.rsplit(".", 1)[0] + ".host")
                if host in (None, "0.0.0.0", "::"):
                    host = "127.0.0.1"
                return (host, port)
        return None

    def _config_data(self):
        try:
            with open(self.config_path, "r") as handle:
                data = json.load(handle)
        except (IOError, OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


class ServiceSet(object):
    """The services declared by one ``services.json``, in declaration order."""

    def __init__(self, services, root, log_dir, run_dir, path=None):
        self.services = list(services)
        self.root = root
        self.log_dir = log_dir
        self.run_dir = run_dir
        self.path = path

    def __iter__(self):
        return iter(self.services)

    def __len__(self):
        return len(self.services)

    @property
    def names(self):
        return [service.name for service in self.services]

    def select(self, names):
        """The named services, in declaration order; all of them if none named."""
        if not names:
            return list(self.services)
        known = dict((service.name, service) for service in self.services)
        chosen = []
        for name in names:
            if name not in known:
                raise ConfigError(
                    "unknown service '%s' -- this file declares %s"
                    % (name, ", ".join(self.names) or "none"))
            if known[name] not in chosen:
                chosen.append(known[name])
        # Declaration order, not the order they were typed: venues must come
        # up before the board that connects to them.
        return [s for s in self.services if s in chosen]

    @classmethod
    def load(cls, path):
        # type: (str) -> ServiceSet
        config = Config.load(path)
        base = os.path.dirname(os.path.abspath(path))

        # Paths in this file are relative to the project root, which is the
        # parent of config/ when the file sits where it ships. `root` overrides.
        default_root = base
        if os.path.basename(base).lower() == "config":
            default_root = os.path.dirname(base)
        root = _resolve(default_root, config.get("root") or default_root)

        log_dir = _resolve(root, config.get("log.dir", DEFAULT_LOG_DIR))
        run_dir = _resolve(root, config.get("run_dir", DEFAULT_RUN_DIR))
        max_bytes = config.get("log.max_bytes", DEFAULT_MAX_BYTES)
        backups = config.get("log.backups", DEFAULT_BACKUPS)
        level = config.get("log.level")

        entries = config.get("services")
        if not isinstance(entries, list) or not entries:
            raise ConfigError("%s: 'services' must be a non-empty list" % path)

        services = []
        seen = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ConfigError(
                    "%s: services[%d] must be an object" % (path, index))
            name = entry.get("name")
            if not name:
                raise ConfigError(
                    "%s: services[%d] needs a 'name'" % (path, index))
            if name in seen:
                raise ConfigError("%s: duplicate service '%s'" % (path, name))
            seen.add(name)

            config_value = entry.get("config")
            if not config_value:
                raise ConfigError(
                    "%s: service '%s' needs a 'config'" % (path, name))
            module = entry.get("module") or (
                WEB_MODULE if entry.get("kind") == "web" else VENUE_MODULE)

            services.append(Service(
                name=name,
                module=module,
                config_path=_resolve(root, config_value),
                root=root,
                log_dir=log_dir,
                run_dir=run_dir,
                args=entry.get("args"),
                max_bytes=entry.get("log_max_bytes", max_bytes),
                backups=entry.get("log_backups", backups),
                log_level=entry.get("log_level", level)))

        return cls(services, root, log_dir, run_dir, path=path)


def default_config_path(start=None):
    """Where to look for ``services.json`` when ``--config`` was not given.

    The working directory first, so a checkout being worked on wins, then the
    tree this module was imported from, so ``exchangesim status`` answers the
    same from anywhere.
    """
    candidates = []
    here = os.path.abspath(start or os.getcwd())
    candidates.append(os.path.join(here, "config", "services.json"))
    candidates.append(os.path.join(here, "services.json"))
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    candidates.append(os.path.join(package_root, "config", "services.json"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _resolve(root, value):
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(root, value))


def _dotted(data, dotted):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
