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
        edges edge-port chaos chaos-clear chaos-status \
        agent agent-watch agent-sweep agent-tools diagnose-eval diagnose-checks \
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

# Superseded by `make chaos MODE=black_source`, which switches the encoder in
# place instead of recreating the container. Kept as aliases because the demo
# script and docs refer to them by name.
black-source: ## CHAOS alias: make chaos MODE=black_source
	@$(MAKE) -s chaos MODE=black_source

restore-source: ## Alias: clear L1 faults
	@curl -sS -X POST "$(ENCODER)/chaos" -H 'Content-Type: application/json' \
	  -d '{"mode":"none","severity":0}'

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

# Brief §5's fault menu. edge_latency is region-scoped (L2); the rest are
# plant-wide L1 faults driven at the encoder. See docs/plant.md for the
# expected telemetry signature of each.
L1_MODES := black_source ladder_collapse ladder_mismatch segment_gap
L2_MODES := edge_latency

chaos: ## Inject a fault: make chaos MODE=<mode> [REGION=... SEVERITY=n]
ifndef MODE
	$(error usage: make chaos MODE=<$(L2_MODES) (needs REGION) | $(L1_MODES)> [SEVERITY=n])
endif
	@set -e; \
	case " $(L1_MODES) " in \
	  *" $(MODE) "*) \
	    curl -sS -X POST "$(ENCODER)/chaos" -H 'Content-Type: application/json' \
	      -d '{"mode":"$(MODE)","severity":$(or $(SEVERITY),0)}'; \
	    echo "  L1 fault, plant-wide. Expected signature: docs/plant.md"; \
	    ;; \
	  *) \
	    case " $(L2_MODES) " in \
	      *" $(MODE) "*) \
	        if [ -z "$(REGION)" ]; then \
	          echo "MODE=$(MODE) is region-scoped -- pass REGION=<$(REGIONS)>"; exit 1; fi; \
	        port=$(EDGE_PORT_$(REGION)); \
	        if [ -z "$$port" ]; then \
	          echo "unknown region '$(REGION)' (known: $(REGIONS))"; exit 1; fi; \
	        curl -sS -X POST "http://localhost:$$port/chaos" \
	          -H 'Content-Type: application/json' \
	          -d '{"mode":"$(MODE)","severity":$(or $(SEVERITY),2)}'; \
	        echo "  other regions are untouched -- that differential is the point"; \
	        ;; \
	      *) echo "unknown MODE '$(MODE)'"; \
	         echo "  L1 (plant-wide): $(L1_MODES)"; \
	         echo "  L2 (per-region): $(L2_MODES)"; exit 1;; \
	    esac;; \
	esac

chaos-clear: ## Clear every injected fault, L1 and all regions
	@curl -sS -X POST "$(ENCODER)/chaos" -H 'Content-Type: application/json' \
	  -d '{"mode":"none","severity":0}'
	@for r in $(REGIONS); do \
	  port=$$($(MAKE) -s edge-port REGION=$$r); \
	  curl -sS -X POST "http://localhost:$$port/chaos" \
	    -H 'Content-Type: application/json' -d '{"mode":"none","severity":0}'; \
	done

chaos-status: ## Show every injected fault across L1 and L2
	@printf "L1 encoder      "; curl -sS "$(ENCODER)/chaos" 2>/dev/null || echo "unreachable"
	@for r in $(REGIONS); do \
	  port=$$($(MAKE) -s edge-port REGION=$$r); \
	  printf "L2 %-13s " "$$r"; \
	  curl -sS "http://localhost:$$port/chaos" 2>/dev/null || echo "unreachable"; \
	done

# --- the agent --------------------------------------------------------------

agent: ## Run the DEAD AIR agent once, e.g. make agent REGION=us-east1
ifndef REGION
	$(error usage: make agent REGION=<$(REGIONS)>)
endif
	@$(PY) scripts/run_agent.py --region $(REGION) 2>&1 \
	  | grep -v "Warning\|check_feature\|mTLS\|session = await"

agent-watch: ## Wake the agent on every firing alert from the webhook receiver
	@$(PY) scripts/run_agent.py --watch 2>&1 \
	  | grep -v "Warning\|check_feature\|mTLS\|session = await"

agent-sweep: ## Confidence monitor: proactive sweep on a timer (finds black_source)
	@$(PY) -u scripts/run_agent.py --sweep --interval $(or $(INTERVAL),300) \
	  --region $(or $(REGION),us-east1) 2>&1 \
	  | grep -v "Warning\|check_feature\|mTLS\|session = await"

diagnose-checks: ## Fast: score the deterministic checklist on all 6 cases, no model
	@$(PY) -u scripts/diagnose_eval.py --checks-only 2>&1 \
	  | grep -v "Warning\|check_feature\|mTLS\|session = await"

diagnose-eval: ## Full: drive all 5 faults + healthy control through the agent
	@$(PY) -u scripts/diagnose_eval.py 2>&1 \
	  | grep -v "Warning\|check_feature\|mTLS\|session = await"

agent-tools: ## Show which MCP tools each Phase-1 specialist is pinned to
	@$(PY) scripts/show_agent_tools.py 2>&1 \
	  | grep -v "Warning\|check_feature\|mTLS\|session = await"

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
