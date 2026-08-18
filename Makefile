# DEAD AIR -- plant control.
#
# Everything here runs locally (Docker). Nothing needs GCE or Cloud Run.
#
# Typical Step 1 loop:
#   make plant-up          bring up emitter + Alloy + webhook receiver
#   make tunnel            expose the webhook receiver publicly (ngrok)
#   make provision         push dashboard + alert rule + contact point
#   make set VALUE=95      cross the threshold; Grafana should go red
#   make watch             tail the webhook receiver for the alert delivery
#   make set VALUE=10      back to healthy; expect a RESOLVED delivery

SHELL := /bin/bash
COMPOSE := docker compose
PY := ./.venv/bin/python
ENV_FILE := agents/grafana_probe/.env
EMITTER := http://localhost:9101
WEBHOOK := http://localhost:9102
ALLOY_UI := http://localhost:12345

.DEFAULT_GOAL := help
.PHONY: help plant-up plant-down plant-restart plant-status plant-logs \
        set value watch alerts provision tunnel tunnel-url verify \
        mcp-up mcp-down clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- plant lifecycle --------------------------------------------------------

plant-up: $(ENV_FILE) ## Start the plant (emitter, Alloy, webhook, MCP server)
	$(COMPOSE) up -d --build
	@echo
	@echo "  emitter   $(EMITTER)/metrics"
	@echo "  webhook   $(WEBHOOK)/"
	@echo "  alloy UI  $(ALLOY_UI)/"
	@echo
	@echo "  next: make tunnel && make provision"

plant-down: ## Stop the plant (keeps volumes)
	$(COMPOSE) down

plant-restart: ## Recreate all plant services
	$(COMPOSE) up -d --build --force-recreate

plant-status: ## Show container status
	@$(COMPOSE) ps

plant-logs: ## Follow logs for all plant services
	$(COMPOSE) logs -f

mcp-up: ## Start only the Grafana MCP server
	$(COMPOSE) up -d mcp-grafana

mcp-down: ## Stop only the Grafana MCP server
	$(COMPOSE) stop mcp-grafana

# --- the Step 1 control loop ------------------------------------------------

set: ## Set the synthetic gauge, e.g. make set VALUE=95
ifndef VALUE
	$(error usage: make set VALUE=<number>)
endif
	@curl -sS -X POST "$(EMITTER)/set?value=$(VALUE)" && echo

value: ## Read the current synthetic gauge value
	@curl -sS "$(EMITTER)/value" && echo

watch: ## Follow the webhook receiver, waiting for alert deliveries
	$(COMPOSE) logs -f webhook

alerts: ## Show alert deliveries the webhook receiver has seen
	@curl -sS "$(WEBHOOK)/" || echo "webhook receiver not reachable -- is the plant up?"

# --- Grafana Cloud ----------------------------------------------------------

tunnel: ## Expose the local webhook publicly via ngrok (foreground)
	@echo "Starting ngrok on 9102. Leave this running; in another shell:"
	@echo "  make provision"
	ngrok http 9102

tunnel-url: ## Print the current public ngrok URL
	@curl -sS http://127.0.0.1:4040/api/tunnels \
	  | $(PY) -c "import sys,json; ts=json.load(sys.stdin)['tunnels']; \
	    print(next((t['public_url'] for t in ts if t['public_url'].startswith('https')), 'no https tunnel found'))" \
	  2>/dev/null || echo "ngrok not running -- run 'make tunnel' first"

provision: ## Push dashboard + alert rule + contact point to Grafana Cloud
	@url=$$($(MAKE) -s tunnel-url); \
	if [[ "$$url" == https://* ]]; then \
	  echo "using webhook URL: $$url"; \
	  $(PY) scripts/provision_grafana.py --webhook-url "$$url"; \
	else \
	  echo "no ngrok tunnel detected -- provisioning dashboard + rule only"; \
	  $(PY) scripts/provision_grafana.py; \
	fi

verify: ## Check the telemetry pipe is delivering to Grafana Cloud
	@$(PY) scripts/verify_pipe.py

# --- housekeeping -----------------------------------------------------------

$(ENV_FILE):
	@echo "error: $(ENV_FILE) missing."
	@echo "       cp agents/grafana_probe/.env.example $(ENV_FILE) and fill it in."
	@exit 1

clean: ## Stop everything and remove volumes
	$(COMPOSE) down -v
