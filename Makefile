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
ENCODER := http://localhost:9103
ORIGIN := http://localhost:8080
ALLOY_UI := http://localhost:12345

.DEFAULT_GOAL := help
.PHONY: help plant-up plant-down plant-restart plant-status plant-logs \
        set value watch alerts provision tunnel tunnel-url verify \
        player ladder black-source restore-source frame \
        edges edge-port chaos chaos-clear \
        mcp-up mcp-down clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- plant lifecycle --------------------------------------------------------

plant-up: $(ENV_FILE) ## Start the plant (emitter, Alloy, webhook, MCP server)
	$(COMPOSE) up -d --build
	@echo
	@echo "  emitter   $(EMITTER)/metrics"
	@echo "  encoder   $(ENCODER)/metrics"
	@echo "  player    $(ORIGIN)/player/"
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

# --- L1: encoder / origin ---------------------------------------------------

player: ## Open the hls.js player against the local origin
	@echo "opening $(ORIGIN)/player/"
	@open "$(ORIGIN)/player/" 2>/dev/null || echo "browse to $(ORIGIN)/player/"

ladder: ## Show the ABR ladder the origin is currently serving
	@curl -sS "$(ORIGIN)/hls/master.m3u8"

frame: ## Grab the current frame from the top rung as a PNG (frames/ is gitignored)
	@mkdir -p frames
	@seg=$$(curl -sS "$(ORIGIN)/hls/1080p/index.m3u8" | grep -m1 '\.ts$$'); \
	curl -sS -o /tmp/deadair-frame.ts "$(ORIGIN)/hls/1080p/$$seg"; \
	ffmpeg -v error -y -i /tmp/deadair-frame.ts -frames:v 1 frames/latest.png; \
	echo "wrote frames/latest.png (from $$seg)"

# Brief §5 chaos mode `black_source`: the failure the whole demo is built
# around. Delivery telemetry stays perfectly green -- segments keep flowing at
# the right size and cadence -- while the picture is gone. The encoder reads its
# input from ENCODER_SOURCE precisely so this is a restart, not a code change.
black-source: ## CHAOS: swap the encoder input to black (every metric stays green)
	ENCODER_SOURCE="color=black:size=1920x1080:rate=30" \
	  $(COMPOSE) up -d --force-recreate encoder
	@echo
	@echo "  source is now BLACK. Metrics will look healthy; the picture is gone."
	@echo "  compare: make frame     (and watch the dashboard stay green)"
	@echo "  restore: make restore-source"

restore-source: ## Restore the normal test pattern source
	$(COMPOSE) up -d --force-recreate encoder
	@echo "  source restored to testsrc2 + timecode"

# --- L2: edges + chaos ------------------------------------------------------
# Local port per simulated region. On Cloud Run these become service URLs and
# only this mapping changes.
EDGE_PORT_us-east1     := 8081
EDGE_PORT_europe-west1 := 8082
EDGE_PORT_asia-south1  := 8083
REGIONS := us-east1 europe-west1 asia-south1

edges: ## Show each edge's region, chaos state and cache hit ratio
	@printf "%-16s %-22s %s\n" REGION CHAOS "CACHE HIT RATIO"
	@for r in $(REGIONS); do \
	  port=$$($(MAKE) -s edge-port REGION=$$r); \
	  chaos=$$(curl -sS "http://localhost:$$port/chaos" 2>/dev/null || echo '{}'); \
	  ratio=$$(curl -sS "http://localhost:$$port/metrics" 2>/dev/null \
	           | awk -F' ' '/^edge_cache_hit_ratio/ {print $$2}'); \
	  printf "%-16s %-22s %s\n" "$$r" "$$chaos" "$$ratio"; \
	done

edge-port: # internal: resolve a region to its local port
	@echo "$(EDGE_PORT_$(REGION))"

chaos: ## Inject a fault: make chaos REGION=europe-west1 MODE=edge_latency [SEVERITY=2]
ifndef REGION
	$(error usage: make chaos REGION=<$(REGIONS)> MODE=<edge_latency|segment_gap|none> [SEVERITY=1-3])
endif
ifndef MODE
	$(error usage: make chaos REGION=$(REGION) MODE=<edge_latency|segment_gap|none> [SEVERITY=1-3])
endif
	@port=$(EDGE_PORT_$(REGION)); \
	if [ -z "$$port" ]; then echo "unknown region '$(REGION)' (known: $(REGIONS))"; exit 1; fi; \
	curl -sS -X POST "http://localhost:$$port/chaos" \
	  -H 'Content-Type: application/json' \
	  -d '{"mode":"$(MODE)","severity":$(or $(SEVERITY),2)}'
	@echo "  other regions are untouched -- that differential is the point"

chaos-clear: ## Clear injected faults in every region
	@for r in $(REGIONS); do \
	  port=$$($(MAKE) -s edge-port REGION=$$r); \
	  curl -sS -X POST "http://localhost:$$port/chaos" \
	    -H 'Content-Type: application/json' -d '{"mode":"none","severity":0}'; \
	done

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
