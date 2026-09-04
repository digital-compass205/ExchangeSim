# Common tasks. On RHEL 8 the default interpreter is the platform python;
# override for local development:
#
#   make test PYTHON=venv36/Scripts/python.exe

PYTHON ?= /usr/libexec/platform-python
CONFIG ?= config/japannext.json
WEB_CONFIG ?= config/web.json
SERVICES ?= config/services.json
SCENARIOS ?= scenarios/*.json

# What `make smoke` needs up: the venues the scenarios name, but not the web
# board, which is a client of them and takes no part.
SMOKE_SERVICES ?= japannext hkex nse

CTL = $(PYTHON) -m exchangesim.ctl.main --config $(SERVICES)

.PHONY: help test run check web start stop restart status logs scenarios smoke clean

help:
	@echo "make test        run the unit and integration suite"
	@echo "make check       validate every service's config without starting it"
	@echo "make start       start every service in $(SERVICES)"
	@echo "make status      show what is running, and where"
	@echo "make logs        tail every service's log (make logs SERVICE=hkex)"
	@echo "make stop        stop every service"
	@echo "make restart     stop then start"
	@echo "make run         start $(CONFIG) in the foreground"
	@echo "make web         start the web board for the venues in $(WEB_CONFIG)"
	@echo "make scenarios   run the scenario suite against running simulators"
	@echo "make smoke       start every venue, run the scenarios, stop them"
	@echo "make clean       remove bytecode and runtime state"
	@echo ""
	@echo "PYTHON=$(PYTHON)"
	@echo "SERVICES=$(SERVICES)"

test:
	$(PYTHON) -m unittest discover -s tests -t .

check:
	$(CTL) check

# -- the whole simulator, in the background, logging to var/log ---------------

start:
	$(CTL) start $(SERVICE)

stop:
	$(CTL) stop $(SERVICE)

restart:
	$(CTL) restart $(SERVICE)

status:
	@$(CTL) status $(SERVICE) || true

logs:
	@$(CTL) logs $(SERVICE)

# -- one process in the foreground, for a terminal you are watching ----------

run:
	$(PYTHON) -m exchangesim.runner.main --config $(CONFIG)

# Its own process: a client of every venue's control plane, not part of any.
web:
	$(PYTHON) -m exchangesim.web.main --config $(WEB_CONFIG)

scenarios:
	$(PYTHON) -m exchangesim.scenario.runner "$(SCENARIOS)"

# Start every venue, run every scenario against them, then stop them. This is
# the single command a CI job needs. Sequence stores are cleared first so a run
# starts from a known state rather than inheriting the last one's numbering.
#
# `start` returns once each venue answers on its control port, so there is no
# sleep here to be too short on a loaded build agent.
smoke:
	@$(CTL) stop >/dev/null 2>&1 || true
	@rm -rf var
	@$(CTL) start $(SMOKE_SERVICES)
	@$(PYTHON) -m exchangesim.scenario.runner "$(SCENARIOS)"; \
	    status=$$?; \
	    $(CTL) stop $(SMOKE_SERVICES); \
	    exit $$status

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.py[co]' -delete
	rm -rf var var-smoke.log .smoke.pid
