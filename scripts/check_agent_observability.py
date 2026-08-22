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

ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                          "http://localhost:4318")

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
    emitted_at = int(time.time())
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("deadair.selfcheck")
        with tracer.start_as_current_span("observability_selfcheck") as span:
            span.set_attribute("deadair.selfcheck.marker", marker)
            span.set_attribute("gen_ai.system", "vertex_ai")
            time.sleep(0.05)
        provider = trace.get_tracer_provider()
        # force_flush returns whether the queue DRAINED, which is worth checking
        # but is emphatically not proof of delivery: measured against an
        # endpoint on a closed port it still returns True, because
        # BatchSpanProcessor hands the batch to an exporter that retries
        # asynchronously. So this catches a wedged queue and nothing else.
        # Delivery is established below, by the marker query, and only there.
        flushed = True
        if hasattr(provider, "force_flush"):
            flushed = bool(provider.force_flush(10_000))
        if not flushed:
            print(f"{BAD} the span could not be flushed to {ENDPOINT}")
            print("       -> the exporter could not deliver. Check that Alloy "
                  "is up and its OTLP receiver is listening on :4318.")
            sys.exit(1)
        print(f"{OK} emitted marker {marker} and flushed it")
    except Exception as exc:                            # noqa: BLE001
        print(f"{BAD} could not emit a span: {type(exc).__name__}: {exc}")
        sys.exit(1)

    base = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
    if not base or not token:
        print(f"{BAD} cannot query Tempo: Grafana credentials missing")
        sys.exit(1)

    # QUERY FOR THE MARKER, NOT FOR THE SERVICE.
    #
    # This is the second and worse way the check passed without evidence. It
    # used to search `{resource.service.name="deadair-agent"}` over a 900-second
    # window -- so ANY agent trace from the previous quarter hour satisfied it,
    # and the marker it went to the trouble of generating was never used.
    #
    # Demonstrated rather than argued: with the exporter pointed at a dead port,
    # so that nothing from the run could possibly arrive, the old check reported
    # "Tempo returned 1 trace(s)" and exited PASS -- it had found the trace from
    # a legitimate run 36 seconds earlier. That is a test passing for the wrong
    # reason, which is precisely the failure mode this script exists to catch in
    # everything else.
    #
    # Searching by the unique marker means only THIS run's span can satisfy it.
    service = os.environ.get("OTEL_SERVICE_NAME", "deadair-agent")
    query = f'{{span.deadair.selfcheck.marker="{marker}"}}'
    for attempt in range(12):
        time.sleep(6)
        end = int(time.time())
        # A window that starts when we emitted, so a stale span cannot match
        # even if one somehow carried the same marker.
        url = (f"{base}/api/datasources/proxy/uid/grafanacloud-traces"
               "/api/search?" + urllib.parse.urlencode({
                   "q": query, "start": emitted_at - 60, "end": end, "limit": 5}))
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                traces = (json.loads(r.read().decode()) or {}).get("traces") or []
        except Exception as exc:                        # noqa: BLE001
            print(f"       query attempt {attempt + 1} failed: "
                  f"{type(exc).__name__}: {str(exc)[:90]}")
            continue
        if traces:
            print(f"{OK} Tempo returned THIS run's marker span after "
                  f"{(attempt + 1) * 6}s (trace {traces[0].get('traceID','')[:16]})")
            break
    else:
        print(f"{BAD} the span emitted by THIS run never arrived in Tempo "
              f"within 72s (searched {query})")
        print("       -> export succeeded locally and the span did not land. "
              "Check that Alloy is up (docker compose ps alloy), that its OTLP "
              "receiver is listening on :4318, that GRAFANA_TEMPO_USER and "
              "GRAFANA_TEMPO_GRPC are set in your .env, and that its Tempo "
              "exporter is not erroring (docker logs deadair-alloy | grep otelcol)")
        sys.exit(1)

    u = usage_totals()
    print(f"{OK} usage accounting live "
          f"(llm_calls={u['llm_calls']}, tokens="
          f"{u['input_tokens']}in/{u['output_tokens']}out, est ${u['usd']})")
    print("\nReflexive layer confirmed end to end: emitted, exported, and "
          "queried back out of Grafana Cloud.")


if __name__ == "__main__":
    main()
