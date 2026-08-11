# Common tasks. On RHEL 8 the default interpreter is the platform python;
# override for local development:
#
#   make test PYTHON=venv36/Scripts/python.exe

PYTHON ?= /usr/libexec/platform-python
CONFIG ?= config/japannext.json
WEB_CONFIG ?= config/web.json
SCENARIOS ?= scenarios/*.json

# Every venue the scenario suite expects to be running. Each scenario names the
# ports it needs, so one `make scenarios` covers them all.
VENUES ?= config/japannext.json config/hkex.json

.PHONY: help test run check web scenarios smoke clean

help:
	@echo "make test        run the unit and integration suite"
	@echo "make check       validate every venue config without starting anything"
	@echo "make run         start $(CONFIG) in the foreground"
	@echo "make web         start the web board for the venues in $(WEB_CONFIG)"
	@echo "make scenarios   run the scenario suite against running simulators"
	@echo "make smoke       start every venue, run the scenarios, stop them"
	@echo "make clean       remove bytecode and runtime state"
	@echo ""
	@echo "PYTHON=$(PYTHON)"
	@echo "VENUES=$(VENUES)"

test:
	$(PYTHON) -m unittest discover -s tests -t .

check:
	@for config in $(VENUES); do \
	    $(PYTHON) -m exchangesim.runner.main --config $$config --check || exit 1; \
	done

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
smoke:
	@rm -rf var
	@rm -f .smoke.pid
	@for config in $(VENUES); do \
	    $(PYTHON) -m exchangesim.runner.main --config $$config >> var-smoke.log 2>&1 & \
	    echo $$! >> .smoke.pid; \
	done; \
	    sleep 3; \
	    $(PYTHON) -m exchangesim.scenario.runner "$(SCENARIOS)"; \
	    status=$$?; \
	    while read pid; do kill $$pid 2>/dev/null || true; done < .smoke.pid; \
	    rm -f .smoke.pid; \
	    exit $$status

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.py[co]' -delete
	rm -rf var var-smoke.log .smoke.pid
