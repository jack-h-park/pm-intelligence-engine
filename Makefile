# pm-intelligence-engine — Operations Makefile
#
# PRODUCTION (the always-on iMac) runs under launchd, NOT `make start`.
# To deploy/restart production, use the service-* targets:
#   make service-restart  — restart the launchd server (picks up new code after git pull)
#   make service-status   — show the launchd service PID / last exit status
#   make service-logs     — tail live log output
#   make install-service  — one-time: register the launchd service
#   make uninstall-service — remove the launchd service
#
# LOCAL background server (dev convenience on a laptop — do NOT use on the iMac,
# it binds the same port 8000 as the launchd service and will conflict):
#   make start / stop / restart / status / logs
#
# Development:
#   make dev      — start server in foreground with --reload
#   make test     — run unit + decision + insights + integration test suite
#   make eval     — run eval harness
#   make lint     — run ruff linter and the mypy strict type check
#
# See docs/ARCHITECTURE.md Section 11 for the full hosting and Tailscale setup.

.PHONY: start stop restart status logs dev fixture-replay \
        service-start service-stop service-restart service-status service-logs \
        install-service uninstall-service \
        test eval lint require-decision-context

PID_FILE  := .pid
LOG_FILE  := logs/server.log
SERVICE   := com.jackpark.pm-engine
SVC_DOMAIN := gui/$(shell id -u)
PLIST_SRC := deploy/com.jackpark.pm-engine.plist
PLIST_DST := $(HOME)/Library/LaunchAgents/com.jackpark.pm-engine.plist


# ---------------------------------------------------------------------------
# Local background server — dev convenience on a laptop.
# NOT the iMac production server (that runs under launchd; see service-* below).
# Binds port 8000, so it conflicts with the launchd service if both run.
# ---------------------------------------------------------------------------

start: logs/
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Already running (pid $$(cat $(PID_FILE))). Use 'make restart' to restart."; \
		exit 0; \
	fi
	@if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Port 8000 already in use (launchd service? use 'make service-restart')."; \
		exit 1; \
	fi
	python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 >> $(LOG_FILE) 2>&1 & echo $$! > $(PID_FILE)
	@sleep 0.5
	@echo "Started (pid $$(cat $(PID_FILE))). Logs: $(LOG_FILE)"

stop:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		kill $$(cat $(PID_FILE)) && rm -f $(PID_FILE) && echo "Stopped."; \
	else \
		rm -f $(PID_FILE); \
		echo "Not running."; \
	fi

restart: stop start

status:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Running (pid $$(cat $(PID_FILE)))"; \
	else \
		echo "Not running"; \
	fi

logs:
	@tail -f $(LOG_FILE)

logs/:
	@mkdir -p logs


# ---------------------------------------------------------------------------
# Production server control — the launchd service on the iMac.
# Use these (not start/stop/restart) to deploy new code after a git pull.
# ---------------------------------------------------------------------------

service-restart:
	launchctl kickstart -k $(SVC_DOMAIN)/$(SERVICE)
	@echo "Kicked $(SERVICE). Verify: make service-status"

service-start:
	@launchctl start $(SERVICE) && echo "Started $(SERVICE)."

service-stop:
	@echo "Note: KeepAlive=true means launchd restarts the service after stop."
	@launchctl stop $(SERVICE) && echo "Sent stop to $(SERVICE)."

service-status:
	@launchctl list | grep $(SERVICE) || echo "$(SERVICE) not loaded (run 'make install-service')."

service-logs:
	@tail -f $(LOG_FILE)


# ---------------------------------------------------------------------------
# Development — foreground with auto-reload
# ---------------------------------------------------------------------------

dev:
	uvicorn app.api.main:app --reload

fixture-replay:
	python -m app.s2k_fixture_replay


# ---------------------------------------------------------------------------
# iMac auto-start via launchd (one-time setup)
# ---------------------------------------------------------------------------

install-service:
	@mkdir -p $(HOME)/Library/LaunchAgents
	cp $(PLIST_SRC) $(PLIST_DST)
	launchctl load $(PLIST_DST)
	@echo "Service installed. pm-engine will start automatically on login."
	@echo "To check: launchctl list | grep pm-engine"

uninstall-service:
	@launchctl unload $(PLIST_DST) 2>/dev/null || true
	@rm -f $(PLIST_DST)
	@echo "Service removed."


# ---------------------------------------------------------------------------
# Dev tooling
# ---------------------------------------------------------------------------

# The suite reads the operator-configured companion decision-context: the S4
# persona-prompt preflight runs whenever the API app is constructed, so without
# a root 13 tests fail on a missing prompts/s4-personas/ tree. Supplying the
# path is not this target's job — embedding one would commit a host path — but
# failing with the cause beats 13 failures that read as a code regression.
require-decision-context:
	@if [ -z "$$DECISION_CONTEXT_ROOT" ] && [ -z "$$DECISION_SYSTEM_ROOT" ] && [ ! -f .env ]; then \
		echo "make test: DECISION_CONTEXT_ROOT is unset and there is no .env to supply it."; \
		echo "  The tests read the companion decision-context checkout (prompts/s4-personas/)."; \
		echo "  Either 'cp .env.example .env' and set the path there, or run:"; \
		echo "    DECISION_CONTEXT_ROOT=/path/to/decision-context make test"; \
		exit 1; \
	fi

# Every test directory is named explicitly. `tests/decision/` and
# `tests/insights/` were absent, so this target passed without ever executing
# them — a change to either could be merged green on a suite that had not run
# it. Adding a directory under tests/ means adding a line here.
test: require-decision-context
	pytest tests/unit/ -q
	pytest tests/decision/ -q
	pytest tests/insights/ -q
	pytest tests/integration/ -q -m "not slow"

eval:
	python eval/runner.py

# mypy runs here because the repo is configured strict and was driven to zero
# once; without a target that enforces it, that zero only holds for as long as
# someone remembers to check by hand — which it did not.
lint:
	ruff check app/ tests/ eval/
	mypy app/
