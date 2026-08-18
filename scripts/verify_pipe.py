#!/usr/bin/env python3
"""Verify the DEAD AIR telemetry pipe end to end.

Checks each hop in order and stops at the first break, so a failure tells you
*which* hop is down rather than just "no data":

    1. emitter        /metrics serves the gauge locally
    2. alloy          remote_write is delivering (no failed samples)
    3. grafana cloud  the series is queryable in Mimir
    4. cardinality    session_id-style labels are absent (relabel guard works)

Exit code 0 if every hop passes, 1 otherwise.
"""

import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO, "agents", "grafana_probe", ".env")

EMITTER = "http://localhost:9101/metrics"
ALLOY_METRICS = "http://localhost:12345/metrics"
METRIC = "deadair_synthetic_gauge"

OK, BAD = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def get(url, token=None, timeout=20):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def hop_emitter():
    try:
        body = get(EMITTER, timeout=5)
    except Exception as e:
        print(f"{BAD} emitter: {EMITTER} unreachable ({e})")
        print("       -> is the plant up? `make plant-up`")
        return None
    line = next((l for l in body.splitlines()
                 if l.startswith(METRIC) and not l.startswith("#")), None)
    if not line:
        print(f"{BAD} emitter: {METRIC} not exposed")
        return None
    value = float(line.split()[-1])
    print(f"{OK} emitter: {METRIC} = {value}")
    return value


def hop_alloy():
    try:
        body = get(ALLOY_METRICS, timeout=5)
    except Exception as e:
        print(f"{BAD} alloy: metrics endpoint unreachable ({e})")
        return False

    def total(prefix):
        s = 0.0
        for l in body.splitlines():
            if l.startswith(prefix) and not l.startswith("#"):
                try:
                    s += float(l.split()[-1])
                except ValueError:
                    pass
        return s

    sent = total("prometheus_remote_storage_samples_total")
    failed = total("prometheus_remote_storage_samples_failed_total")
    if sent <= 0:
        print(f"{BAD} alloy: no samples sent yet (give it ~30s after plant-up)")
        return False
    if failed > 0:
        print(f"{BAD} alloy: {int(sent)} sent but {int(failed)} FAILED")
        print("       -> check GRAFANA_CLOUD_METRICS_TOKEN / GRAFANA_MIMIR_USER")
        print("       -> `docker compose logs alloy | tail -30`")
        return False
    print(f"{OK} alloy: {int(sent)} samples delivered, 0 failed")
    return True


def query(env, promql):
    """Instant-query Mimir through the Grafana datasource proxy.

    Uses the same service-account token as everything else, so verification
    needs no extra credential. Returns the first series, or None.
    """
    base = env.get("GRAFANA_URL", "").rstrip("/")
    token = env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not base or not token:
        return None
    url = (f"{base}/api/datasources/proxy/uid/grafanacloud-prom"
           f"/api/v1/query?query={promql}")
    try:
        data = json.loads(get(url, token))
    except Exception:
        return None
    result = (data.get("data") or {}).get("result") or []
    return result[0] if result else None


def hop_cloud(env):
    if not env.get("GRAFANA_URL") or not env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN"):
        print(f"{BAD} grafana: GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN missing")
        return None
    series = query(env, METRIC)
    if series is None:
        print(f"{BAD} grafana: {METRIC} not found in Mimir yet")
        print("       -> remote_write batches; wait ~60s after first start")
        return None
    value = float(series["value"][1])
    print(f"{OK} grafana: {METRIC} = {value} queryable in Mimir")
    return series


BANNED_LABELS = {
    "session_id", "viewer_id", "client_id", "request_id", "trace_id",
    "span_id", "segment_uri", "segment_id", "media_seq", "instance_ip",
    "pod_ip",
}


def hop_cardinality(env, series):
    labels = set((series or {}).get("metric", {}))
    leaked = sorted(labels & BANNED_LABELS)
    if leaked:
        print(f"{BAD} cardinality: high-cardinality labels reached Mimir: {leaked}")
        print("       -> the relabel guard in plant/alloy/config.alloy is not working")
        return False

    # The check above is vacuous on its own -- deadair_synthetic_gauge never
    # carries a session_id to begin with. The canary does: the emitter tags it
    # with session_id specifically so the guard has something real to strip.
    # Absence of the label HERE is what actually proves the guard works.
    canary = query(env, "deadair_cardinality_canary")
    if canary is None:
        print(f"{BAD} cardinality: canary series not in Mimir yet")
        print("       -> rebuild the emitter (`make plant-restart`) and retry")
        return False
    canary_labels = set(canary.get("metric", {}))
    if "session_id" in canary_labels:
        print(f"{BAD} cardinality: canary reached Mimir WITH session_id -- "
              "the relabel guard is NOT stripping per-session labels")
        return False

    print(f"{OK} cardinality: canary's session_id was stripped en route "
          f"(labels: {sorted(canary_labels)})")
    return True


def main():
    env = load_env(ENV_PATH)
    print("DEAD AIR telemetry pipe\n")

    local = hop_emitter()
    if local is None:
        sys.exit(1)
    if not hop_alloy():
        sys.exit(1)
    series = hop_cloud(env)
    if series is None:
        sys.exit(1)
    if not hop_cardinality(env, series):
        sys.exit(1)

    cloud = float(series["value"][1])
    print(f"\nlocal={local}  cloud={cloud}", end="")
    print("  (agreement pending scrape+push lag)" if local != cloud else "  (in sync)")
    print("\nPipe is healthy end to end.")


if __name__ == "__main__":
    main()
