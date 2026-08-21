"""Process control: start, stop and inspect the simulator's daemons.

A working simulator is several processes -- one per venue, plus the web board
-- and running them by hand means several backgrounded commands whose output
lands wherever the shell happened to point it. This package is the single
front end: ``exchangesim start`` brings up everything named in
``config/services.json``, ``exchangesim stop`` takes it down, and each daemon's
output goes to its own rotated file under ``var/log``.

It is deliberately a *supervisor of last resort*, not a service manager: it
does not restart a crashed process, because on the RHEL 8 target that is
systemd's job (see ``deploy/exsimd@.service``). What it replaces is the
handful of ``nohup ... &`` lines and the ``kill`` that follows them.
"""
