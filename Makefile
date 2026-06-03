# jackhpark-pm-intelligence-engine — Operations Makefile
#
# Daily ops:
#   make start    — start server in background (logs → logs/server.log)
#   make stop     — stop background server
#   make restart  — stop then start
#   make status   — show whether server is running and its PID
#   make logs     — tail live log output
#
# Development:
#   make dev      — start server in foreground with --reload
#   make test     — run unit + integration test suite
#   make eval     — run eval harness
#   make lint     — run ruff linter
#
# iMac auto-start (one-time setup):
#   make install-service    — register launchd service (starts on login, auto-restarts)
#   make uninstall-service  — remove launchd service
#
# See docs/ARCHITECTURE.md Section 11 for the full hosting and Tailscale setup.

.PHONY: start stop restart status logs dev \
        install-service uninstall-service \
        test eval lint

PID_FILE  := .pid
LOG_FILE  := logs/server.log
PLIST_SRC := deploy/com.jackpark.pm-engine.plist
PLIST_DST := $(HOME)/Library/LaunchAgents/com.jackpark.pm-engine.plist


# ---------------------------------------------------------------------------
# Server — background mode (production / iMac)
# ---------------------------------------------------------------------------

start: logs/
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Already running (pid $$(cat $(PID_FILE))). Use 'make restart' to restart."; \
		exit 0; \
	fi
	uvicorn app.api.main:app --host 0.0.0.0 --port 8000 >> $(LOG_FILE) 2>&1 & echo $$! > $(PID_FILE)
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
# Development — foreground with auto-reload
# ---------------------------------------------------------------------------

dev:
	uvicorn app.api.main:app --reload


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

test:
	pytest tests/unit/ -q
	pytest tests/integration/ -q -m "not slow"

eval:
	python eval/runner.py

lint:
	ruff check app/ tests/
