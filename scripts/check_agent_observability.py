#!/usr/bin/env python3
"""Assert the agent's own telemetry actually ARRIVES in Grafana Cloud.

Same discipline as `make verify`: exporting is not evidence of anything. A
BatchSpanProcessor is fire-and-forget -- if Alloy is down, or the OTLP endpoint
is wrong, or the collector drops the batch, export "succeeds" locally and the
spans simply never exist. The §7 demo beat would then show an empty dashboard
with nothing explaining why.

So this emits a span with a known marker and queries it back out of Tempo.

    python3 scripts/check_agent_observability.py
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agents"))

from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(os.path.join(REPO, "agents", "grafana_probe", ".env"))

OK = "  \033[32mPASS\033[0m"
BAD = "  \033[31mFAIL\033[0m"


def main():
    from dead_air.observability import (
        install_agent_tracing, install_error, usage_totals,
    )

    print("DEAD AIR agent observability\n")

    active = install_agent_tracing()
    if not active:
        print(f"{BAD} tracing did not install: {install_error() or 'disabled'}")
        print("       -> the reflexive layer is OFF; the agent runs but is "
              "invisible in Grafana")
        sys.exit(1)
    print(f"{OK} tracing installed")

    marker = f"deadair-check-{int(time.time())}"
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("deadair.selfcheck")
        with tracer.start_as_current_span("observability_selfcheck") as span:
            span.set_attribute("deadair.selfcheck.marker", marker)
            span.set_attribute("gen_ai.system", "vertex_ai")
            time.sleep(0.05)
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(10_000)
        print(f"{OK} emitted a marker span ({marker}) and flushed")
    except Exception as exc:                            # noqa: BLE001
        print(f"{BAD} could not emit a span: {type(exc).__name__}: {exc}")
        sys.exit(1)

    base = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
    if not base or not token:
        print(f"{BAD} cannot query Tempo: Grafana credentials missing")
        sys.exit(1)

    # Ingestion is not instantaneous; poll rather than assume.
    service = os.environ.get("OTEL_SERVICE_NAME", "deadair-agent")
    found = 0
    for attempt in range(10):
        time.sleep(6)
        end = int(time.time())
        start = end - 900
        url = (f"{base}/api/datasources/proxy/uid/grafanacloud-traces"
               "/api/search?" + urllib.parse.urlencode({
                   "q": f'{{resource.service.name="{service}"}}',
                   "start": start, "end": end, "limit": 20}))
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                traces = (json.loads(r.read().decode()) or {}).get("traces") or []
        except Exception as exc:                        # noqa: BLE001
            print(f"       query attempt {attempt + 1} failed: "
                  f"{type(exc).__name__}: {str(exc)[:90]}")
            continue
        found = len(traces)
        if found:
            print(f"{OK} Tempo returned {found} trace(s) for service "
                  f"'{service}' after {(attempt + 1) * 6}s")
            break
    else:
        print(f"{BAD} no traces for service '{service}' reached Tempo within 60s")
        print("       -> spans were exported locally but never arrived. Check "
              "that Alloy is up (docker compose ps alloy), that its OTLP "
              "receiver is listening on :4318, and that its Tempo exporter "
              "is not erroring (docker logs deadair-alloy | grep otelcol)")
        sys.exit(1)

    u = usage_totals()
    print(f"{OK} usage accounting live "
          f"(llm_calls={u['llm_calls']}, tokens="
          f"{u['input_tokens']}in/{u['output_tokens']}out, est ${u['usd']})")
    print("\nReflexive layer confirmed end to end: emitted, exported, and "
          "queried back out of Grafana Cloud.")


if __name__ == "__main__":
    main()
