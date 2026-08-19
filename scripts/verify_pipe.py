#!/usr/bin/env python3
"""Verify the DEAD AIR plant end to end.

Checks each hop in order so a failure names the broken hop rather than just
reporting "no data":

  L0  emitter      synthetic gauge served locally
  L1  encoder      ffmpeg running, ladder producing segments
  L1  origin       nginx serving the master manifest and all four rungs
      alloy        remote_write delivering, no failed samples
      mimir        metrics queryable in Grafana Cloud
      cardinality  the canary's session_id was stripped in flight
      loki         origin access logs queryable

Exit code 0 if every required hop passes. The Loki hop reports separately: it
depends on the access policy carrying `logs:write`, which is a credential
problem rather than a plumbing problem, and it should not mask a healthy
metrics pipeline.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO, "agents", "grafana_probe", ".env")

EMITTER = "http://localhost:9101/metrics"
ENCODER = "http://localhost:9103/metrics"
ORIGIN = "http://localhost:8080"
ALLOY_METRICS = "http://localhost:12345/metrics"

METRIC = "deadair_synthetic_gauge"
LADDER = ["1080p", "720p", "480p", "360p"]

OK = "  \033[32mPASS\033[0m"
BAD = "  \033[31mFAIL\033[0m"
WARN = "  \033[33mWARN\033[0m"

BANNED_LABELS = {
    "session_id", "viewer_id", "client_id", "request_id", "trace_id",
    "span_id", "segment_uri", "segment_id", "media_seq", "instance_ip",
    "pod_ip",
}


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


def scrape(url, timeout=5):
    """Fetch a Prometheus exposition endpoint into {name: value}."""
    body = get(url, timeout=timeout)
    out = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            pass
    return out


def promql(env, query_str):
    base = env.get("GRAFANA_URL", "").rstrip("/")
    token = env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not base or not token:
        return None
    url = (f"{base}/api/datasources/proxy/uid/grafanacloud-prom"
           f"/api/v1/query?query={urllib.request.quote(query_str)}")
    try:
        data = json.loads(get(url, token))
    except Exception:
        return None
    return (data.get("data") or {}).get("result") or []


# --- hops -------------------------------------------------------------------

def hop_emitter():
    try:
        m = scrape(EMITTER)
    except Exception as e:
        print(f"{BAD} emitter: unreachable ({e})  -> make plant-up")
        return False
    if METRIC not in m:
        print(f"{BAD} emitter: {METRIC} not exposed")
        return False
    print(f"{OK} emitter: {METRIC} = {m[METRIC]}")
    return True


def hop_encoder():
    try:
        m = scrape(ENCODER)
    except Exception as e:
        print(f"{BAD} encoder: :9103 unreachable ({e})")
        return False
    if not m.get("encoder_up"):
        print(f"{BAD} encoder: encoder_up = 0 (ffmpeg is not running)")
        print("       -> docker compose logs encoder | tail -30")
        return False
    fps = m.get("encoder_fps", 0)
    lags = {k: v for k, v in m.items() if k.startswith("packager_segment_lag")}
    stale = {k: v for k, v in lags.items() if v > 15}
    print(f"{OK} encoder: up, {fps} fps, {int(m.get('dropped_frames', 0))} dropped, "
          f"{len(lags)} renditions packaging")
    if stale:
        print(f"{WARN} encoder: renditions with stale segments (>15s): "
              f"{sorted(stale)}")
    return True


def hop_origin():
    try:
        master = get(f"{ORIGIN}/hls/master.m3u8", timeout=5)
    except Exception as e:
        print(f"{BAD} origin: master manifest unreachable ({e})")
        return False
    missing = [r for r in LADDER if f"{r}/index.m3u8" not in master]
    if missing:
        print(f"{BAD} origin: master manifest missing rungs: {missing}")
        return False
    segs = 0
    for r in LADDER:
        try:
            pl = get(f"{ORIGIN}/hls/{r}/index.m3u8", timeout=5)
            segs += sum(1 for l in pl.splitlines() if l.endswith(".ts"))
        except Exception:
            print(f"{BAD} origin: rung {r} playlist unreachable")
            return False
    print(f"{OK} origin: master + {len(LADDER)} rungs serving, "
          f"{segs} segments listed")
    return True


EDGE_PORTS = {"us-east1": 8081, "europe-west1": 8082, "asia-south1": 8083}


def hop_edges():
    degraded = []
    for region, port in sorted(EDGE_PORTS.items()):
        try:
            m = scrape(f"http://localhost:{port}/metrics")
        except Exception as e:
            print(f"{BAD} edge {region}: :{port} unreachable ({e})")
            return False
        key = f'edge_up{{region="{region}"}}'
        if not m.get(key):
            print(f"{BAD} edge {region}: not reporting edge_up")
            return False
        if m.get(f'edge_chaos_active{{region="{region}"}}'):
            degraded.append(region)
    print(f"{OK} edges: {len(EDGE_PORTS)} regions proxying"
          + (f"  (chaos active: {', '.join(degraded)})" if degraded else ""))
    return True


VIEWERS = "http://localhost:9104/metrics"


def hop_viewers():
    try:
        m = scrape(VIEWERS)
    except Exception as e:
        print(f"{BAD} viewers: :9104 unreachable ({e})")
        return False
    sessions = sum(v for k, v in m.items() if k.startswith("viewer_sessions_active"))
    if sessions <= 0:
        print(f"{BAD} viewers: no active sessions")
        return False
    stalling = sorted(
        k.split("region=\"")[1].split("\"")[0]
        for k, v in m.items()
        if k.startswith("rebuffer_ratio") and v > 0.02
    )
    note = f"  (rebuffering: {', '.join(sorted(set(stalling)))})" if stalling else ""
    print(f"{OK} viewers: {int(sessions)} modeled sessions on a playback clock{note}")
    return True


def hop_beacons(env):
    """Per-session QoE detail must be in Loki, and session_id must NOT be a
    Loki label either -- a Loki label costs the same as a Prometheus one."""
    base = env.get("GRAFANA_URL", "").rstrip("/")
    token = env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not base or not token:
        print(f"{WARN} beacons: cannot query (Grafana credentials missing)")
        return False
    end = int(time.time() * 1e9)
    start = end - int(15 * 60 * 1e9)
    q = urllib.request.quote('{job="deadair-viewers"}')
    url = (f"{base}/api/datasources/proxy/uid/grafanacloud-logs"
           f"/loki/api/v1/query_range?query={q}&limit=5&start={start}&end={end}")
    try:
        data = json.loads(get(url, token))
    except Exception as e:
        print(f"{WARN} beacons: query failed ({e})")
        return False
    result = (data.get("data") or {}).get("result") or []
    if not result:
        print(f"{WARN} beacons: no QoE beacons in the last 15m")
        return False
    stream = result[0].get("stream", {})
    if "session_id" in stream:
        print(f"{BAD} beacons: session_id is a LOKI LABEL -- same cardinality "
              "explosion, different database")
        return False
    try:
        line = json.loads(result[0]["values"][0][1])
    except Exception:
        line = {}
    if "session_id" not in line:
        print(f"{BAD} beacons: session_id missing from the beacon body -- "
              "per-session detail is not being captured anywhere")
        return False
    print(f"{OK} beacons: per-session QoE in Loki, session_id in the line "
          f"not the labels (e.g. {line['session_id']})")
    return True


def hop_alloy():
    try:
        body = get(ALLOY_METRICS, timeout=5)
    except Exception as e:
        print(f"{BAD} alloy: unreachable ({e})")
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
        print(f"{BAD} alloy: no samples sent yet (wait ~30s after plant-up)")
        return False
    if failed > 0:
        print(f"{BAD} alloy: {int(sent)} sent but {int(failed)} FAILED")
        print("       -> check GRAFANA_CLOUD_METRICS_TOKEN / GRAFANA_MIMIR_USER")
        return False
    print(f"{OK} alloy: {int(sent)} samples delivered, 0 failed")
    return True


def hop_mimir(env):
    if not env.get("GRAFANA_URL") or not env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN"):
        print(f"{BAD} mimir: GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN missing")
        return None
    found = {}
    for metric in (METRIC, "encoder_fps", "encoder_up", "packager_segment_lag"):
        res = promql(env, metric)
        if res:
            found[metric] = res
    missing = [m for m in (METRIC, "encoder_fps", "packager_segment_lag")
               if m not in found]
    if missing:
        print(f"{BAD} mimir: not queryable yet: {missing}")
        print("       -> remote_write batches; wait ~60s after first start")
        return None
    rungs = sorted({(s["metric"].get("rendition") or "?")
                    for s in found["packager_segment_lag"]})
    print(f"{OK} mimir: L0+L1 metrics queryable "
          f"(packager_segment_lag rungs: {', '.join(rungs)})")
    return found[METRIC][0]


def hop_cardinality(env, series):
    leaked = sorted(set((series or {}).get("metric", {})) & BANNED_LABELS)
    if leaked:
        print(f"{BAD} cardinality: banned labels reached Mimir: {leaked}")
        return False

    # The check above is vacuous alone -- deadair_synthetic_gauge never carries
    # a session_id. The canaries do, specifically so the guard has something
    # real to strip. Absence of the label HERE is the actual proof.
    #
    # The L3 canary additionally carries region and device_class, so one series
    # proves both directions at once: the guard drops what would blow the free
    # tier and keeps what the diagnosis depends on.
    for canary, must_keep in (
        ("deadair_cardinality_canary", set()),
        ("viewer_cardinality_canary", {"region", "device_class"}),
    ):
        res = promql(env, canary)
        if not res:
            print(f"{BAD} cardinality: {canary} not in Mimir yet")
            return False
        labels = set(res[0].get("metric", {}))
        if "session_id" in labels:
            print(f"{BAD} cardinality: {canary} arrived WITH session_id -- "
                  "the relabel guard is not stripping per-session labels")
            return False
        dropped = sorted(must_keep - labels)
        if dropped:
            print(f"{BAD} cardinality: {canary} lost {dropped} -- the guard is "
                  "over-stripping and the diagnosis loses its dimensions")
            return False
    print(f"{OK} cardinality: both canaries' session_id stripped en route, "
          "region/device_class kept")
    return True


# Labels each metric MUST still carry once it reaches Mimir. The allowlist in
# plant/alloy/config.alloy drops anything not named there, and it does so
# silently -- a metric arrives looking healthy, just without the dimension you
# needed. That is the same failure shape as a cardinality check that passes
# because the label was never emitted, so absence of banned labels is only half
# the assertion; these are the other half.
EXPECTED_LABELS = [
    ("deadair_synthetic_gauge", {"job", "instance", "layer", "component"}),
    ("encoder_fps", {"job", "layer", "component"}),
    # Without `rendition` you cannot tell which ladder rung stalled, which is
    # the entire ladder_collapse diagnosis.
    ("packager_segment_lag", {"rendition"}),
    # Without `region` there is no per-region differential, which is the entire
    # edge_latency diagnosis.
    ("edge_cache_hit_ratio", {"region"}),
    ("origin_shield_miss_total", {"region"}),
    ("segment_status", {"region", "status"}),
    # `le` carries the histogram. Dropping it leaves _bucket series that look
    # fine individually but make histogram_quantile return nothing.
    ("segment_ttfb_seconds_bucket", {"region", "le"}),
    # L3. `region` is what the alert splits on and what the agent keys its
    # investigation off; `device_class` is what shows mobile stalling first.
    ("rebuffer_ratio", {"region", "device_class"}),
    ("viewer_rebuffer_seconds_total", {"region", "device_class"}),
    ("viewer_playing_seconds_total", {"region", "device_class"}),
]

EXPECTED_REGIONS = {"us-east1", "europe-west1", "asia-south1"}


def hop_labels_present(env):
    ok = True
    for metric, required in EXPECTED_LABELS:
        res = promql(env, metric)
        if not res:
            print(f"{BAD} labels: {metric} not in Mimir at all")
            ok = False
            continue
        present = set()
        for s in res:
            present |= set(s.get("metric", {}))
        missing = sorted(required - present)
        if missing:
            print(f"{BAD} labels: {metric} reached Mimir WITHOUT {missing}")
            print("       -> the labelkeep allowlist in plant/alloy/config.alloy "
                  "is dropping it")
            ok = False
    if ok:
        print(f"{OK} labels: all {len(EXPECTED_LABELS)} metrics kept their "
              "required dimensions")

    # Every region must be reporting, or a 'differential' might just be a dead
    # edge that stopped emitting rather than a degraded one.
    res = promql(env, "edge_up")
    regions = {s["metric"].get("region") for s in (res or [])}
    missing_regions = sorted(EXPECTED_REGIONS - regions)
    if missing_regions:
        print(f"{BAD} regions: not reporting: {missing_regions}")
        ok = False
    else:
        print(f"{OK} regions: all {len(EXPECTED_REGIONS)} edges reporting")

    # A histogram that survives labelwise can still be unusable; check the
    # query that the dashboard and the agent will actually run.
    res = promql(
        env,
        "histogram_quantile(0.95, sum by (region, le) "
        "(rate(segment_ttfb_seconds_bucket[5m])))",
    )
    if not res:
        print(f"{BAD} histogram: histogram_quantile returned nothing "
              "(le present but unusable)")
        ok = False
    else:
        vals = {s["metric"].get("region"): float(s["value"][1]) for s in res}
        rendered = ", ".join(f"{r}={v * 1000:.0f}ms" for r, v in sorted(vals.items()))
        print(f"{OK} histogram: p95 TTFB computable per region ({rendered})")
    return ok


def hop_loki(env):
    """Origin access logs queryable in Loki. Reported separately -- a 401 here
    is a credential scope problem, not a broken pipeline."""
    base = env.get("GRAFANA_URL", "").rstrip("/")
    token = env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not base or not token:
        print(f"{WARN} loki: cannot query (Grafana credentials missing)")
        return False
    end = int(time.time() * 1e9)
    start = end - int(15 * 60 * 1e9)
    q = urllib.request.quote('{job="deadair-origin"}')
    url = (f"{base}/api/datasources/proxy/uid/grafanacloud-logs"
           f"/loki/api/v1/query_range?query={q}&limit=5&start={start}&end={end}")
    try:
        data = json.loads(get(url, token))
    except Exception as e:
        print(f"{WARN} loki: query failed ({e})")
        return False
    result = (data.get("data") or {}).get("result") or []
    lines = sum(len(s.get("values") or []) for s in result)
    if not lines:
        print(f"{WARN} loki: no origin logs in the last 15m")
        print("       -> if Alloy shows 401s, the access policy needs the")
        print("          `logs:write` scope alongside `metrics:write`")
        return False
    labels = sorted(result[0].get("stream", {}))
    print(f"{OK} loki: {lines} origin log line(s) queryable (labels: {labels})")
    return True


def hop_traces(env):
    """Traces are the third signal of brief §5's L4. Metrics say a region is
    rebuffering and logs say which segment 404'd; only a trace says how much of
    a slow fetch was the edge versus the origin."""
    base = env.get("GRAFANA_URL", "").rstrip("/")
    token = env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not base or not token:
        print(f"{WARN} traces: cannot query (Grafana credentials missing)")
        return False

    # Exporter health first: a dead pipeline should not look like "no traffic".
    try:
        local = scrape(VIEWERS)
    except Exception:
        local = {}
    failed = local.get("trace_spans_failed_total", 0)
    dropped = local.get("trace_spans_dropped_total", 0)
    exported = local.get("trace_spans_exported_total", 0)
    if exported <= 0:
        print(f"{WARN} traces: viewer fleet has exported no spans yet")
        return False

    end = int(time.time())
    start = end - 900
    q = urllib.request.quote('{name="origin.fetch_segment"}')
    url = (f"{base}/api/datasources/proxy/uid/grafanacloud-traces"
           f"/api/search?q={q}&start={start}&end={end}&limit=5")
    try:
        traces = (json.loads(get(url, token)) or {}).get("traces") or []
    except Exception as e:
        print(f"{WARN} traces: Tempo query failed ({e})")
        return False
    if not traces:
        print(f"{WARN} traces: no client->edge->origin traces in the last 15m")
        print("       -> a cache-miss fetch must be sampled; raise "
              "TRACE_SAMPLE_RATIO if this is persistently empty")
        return False

    note = f", {int(failed)} failed" if failed else ""
    note += f", {int(dropped)} dropped" if dropped else ""
    print(f"{OK} traces: {len(traces)} client->edge->origin trace(s) in Tempo "
          f"({int(exported)} spans exported{note})")
    return True


def main():
    env = load_env(ENV_PATH)
    print("DEAD AIR plant\n")

    required = [
        ("emitter", hop_emitter()),
        ("encoder", hop_encoder()),
        ("origin", hop_origin()),
        ("edges", hop_edges()),
        ("viewers", hop_viewers()),
        ("alloy", hop_alloy()),
    ]
    if not all(ok for _, ok in required):
        print("\nStopped at the first broken hop above.")
        sys.exit(1)

    series = hop_mimir(env)
    if series is None:
        sys.exit(1)
    if not hop_cardinality(env, series):
        sys.exit(1)
    if not hop_labels_present(env):
        sys.exit(1)

    logs_ok = hop_loki(env) and hop_beacons(env)
    traces_ok = hop_traces(env)

    print("\nMetrics pipeline healthy end to end.")
    if not logs_ok:
        print("Logs pipeline NOT yet verified (see WARN above) -- "
              "metrics are unaffected.")
    if not traces_ok:
        print("Traces NOT yet verified (see WARN above) -- "
              "metrics and logs are unaffected.")
    if logs_ok and traces_ok:
        print("All three signals (metrics, logs, traces) confirmed -- "
              "the agent's Phase 1 fan-out has everything it needs.")
    sys.exit(0)


if __name__ == "__main__":
    main()
