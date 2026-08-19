"""L2 CDN edge -- caching reverse proxy with application-level fault injection.

One instance per simulated region. Proxies HLS requests to the origin, caches
segments, exports Prometheus metrics, and accepts chaos commands.

Brief §5 specifies `tc netem delay 800ms 200ms` for the edge_latency fault.
That is impossible on Cloud Run: netem needs NET_ADMIN on the container's
network namespace, which the runtime does not grant. So latency is injected in
the application instead -- a sleep on the request path. It is portable to
anywhere the container runs, needs no privileges, and is what a real edge
degradation looks like from the client's side anyway (higher TTFB), which is
what the viewer fleet and the agent actually observe.

Endpoints:
    GET  /hls/...    proxy to origin, cached
    GET  /metrics    Prometheus exposition
    POST /chaos      {"mode": "...", "severity": N}  -- inject a fault
    GET  /chaos      current chaos state
    GET  /healthz    liveness

Chaos modes:
    none          clear
    edge_latency  delay every response (severity 1/2/3 -> 200/800/2000ms
                  with proportional jitter, mirroring §5's netem parameters)
    segment_gap   drop every Nth segment with a 404 (severity sets N)

Cloud Run portable: single process, stdlib only, listens on $PORT.
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8080"))
REGION = os.environ.get("EDGE_REGION", "unknown")
ORIGIN = os.environ.get("ORIGIN_URL", "http://origin:8080").rstrip("/")

# Segments are immutable once written; manifests are live and must never be
# cached or players get stuck on a stale segment list.
SEGMENT_TTL = float(os.environ.get("SEGMENT_CACHE_TTL", "60"))
# Byte budget, not an entry count: a 1080p 4s segment is ~2.5 MB, so a
# 400-entry cache is a gigabyte per edge and three edges would exhaust the
# Docker VM. Bound the thing that actually consumes memory.
MAX_CACHE_BYTES = int(os.environ.get("MAX_CACHE_BYTES", str(192 * 1024 * 1024)))

# Chaos severity -> (ttfb delay, jitter, throughput cap in bits/sec).
#
# §5 specifies `tc netem delay 800ms 200ms`. Reproducing only the delay would
# understate netem badly: at a high RTT, TCP throughput collapses to roughly
# window/RTT, so a netem-delayed edge does not just answer late, it transfers
# slowly. Latency alone would never starve a 4s-segment buffer -- 800ms of
# added TTFB against a 4s deadline is comfortably survivable -- and the
# edge_latency fault would produce no rebuffering at all, which is not what
# netem does on a real link. The throughput cap is what makes this fault
# behave like the real thing.
# Throughput caps are calibrated against the ladder (5M/3M/1.5M/800k):
#   sev 1  4 Mbps  -- between the 1080p and 720p rungs: ABR downshifts and
#                     absorbs it. Quality drops, viewers do not stall.
#   sev 2  600 kbps -- BELOW the 800k bottom rung, so no amount of downshifting
#                     rescues it and rebuffering is sustained. This is the
#                     default because a fault a player can adapt around does not
#                     stay above a 2m alert threshold, and an edge degraded
#                     below the bottom rung is exactly the real-world case the
#                     agent must diagnose.
#   sev 3  250 kbps -- severe.
LATENCY_BY_SEVERITY = {
    1: (0.200, 0.050, 4_000_000),
    2: (0.800, 0.200, 600_000),
    3: (2.000, 0.500, 250_000),
}

TTFB_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]

_lock = threading.Lock()
_cache = OrderedDict()          # path -> (expiry, status, body, content_type)
_cache_bytes = 0
_chaos = {"mode": "none", "severity": 0}
_seq = 0                        # request counter, drives segment_gap

_hits = 0
_misses = 0
_status_counts = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
_shield_misses = 0
_ttfb_buckets = [0] * (len(TTFB_BUCKETS) + 1)
_ttfb_sum = 0.0
_ttfb_count = 0


def status_class(code):
    return f"{code // 100}xx"


def observe_ttfb(seconds):
    global _ttfb_sum, _ttfb_count
    with _lock:
        _ttfb_sum += seconds
        _ttfb_count += 1
        for i, edge in enumerate(TTFB_BUCKETS):
            if seconds <= edge:
                _ttfb_buckets[i] += 1
                break
        else:
            _ttfb_buckets[-1] += 1


def record_status(code):
    with _lock:
        cls = status_class(code)
        if cls in _status_counts:
            _status_counts[cls] += 1


def render_metrics():
    with _lock:
        hits, misses = _hits, _misses
        statuses = dict(_status_counts)
        shield = _shield_misses
        buckets = list(_ttfb_buckets)
        tsum, tcount = _ttfb_sum, _ttfb_count
        chaos = dict(_chaos)

    total = hits + misses
    ratio = (hits / total) if total else 0.0
    r = f'region="{REGION}"'

    out = [
        "# HELP edge_cache_hit_ratio Fraction of edge requests served from cache.\n",
        "# TYPE edge_cache_hit_ratio gauge\n",
        f"edge_cache_hit_ratio{{{r}}} {ratio:.6f}\n",

        "# HELP origin_shield_miss_total Requests that missed cache and went to origin.\n",
        "# TYPE origin_shield_miss_total counter\n",
        f"origin_shield_miss_total{{{r}}} {shield}\n",

        "# HELP segment_status Segment responses served by this edge, by status class.\n",
        "# TYPE segment_status counter\n",
    ]
    for cls, n in sorted(statuses.items()):
        out.append(f'segment_status{{{r},status="{cls}"}} {n}\n')

    # Histogram. `le` is REQUIRED -- the Alloy cardinality allowlist permits it
    # explicitly, because dropping `le` destroys the histogram silently and
    # looks like a broken exporter rather than a relabel bug.
    out += [
        "# HELP segment_ttfb_seconds Time to first byte served to the client, "
        "including any injected edge latency.\n",
        "# TYPE segment_ttfb_seconds histogram\n",
    ]
    cumulative = 0
    for i, edge in enumerate(TTFB_BUCKETS):
        cumulative += buckets[i]
        out.append(f'segment_ttfb_seconds_bucket{{{r},le="{edge}"}} {cumulative}\n')
    cumulative += buckets[-1]
    out.append(f'segment_ttfb_seconds_bucket{{{r},le="+Inf"}} {cumulative}\n')
    out.append(f"segment_ttfb_seconds_sum{{{r}}} {tsum:.6f}\n")
    out.append(f"segment_ttfb_seconds_count{{{r}}} {tcount}\n")

    # Chaos state as a metric so the dashboard can show which region is being
    # degraded without asking the edges.
    out += [
        "# HELP edge_chaos_active 1 when a fault is injected at this edge.\n",
        "# TYPE edge_chaos_active gauge\n",
        f"edge_chaos_active{{{r}}} {0 if chaos['mode'] == 'none' else 1}\n",
        "# HELP edge_up Always 1; presence proves the edge is scrapeable.\n",
        "# TYPE edge_up gauge\n",
        f"edge_up{{{r}}} 1\n",
    ]
    return "".join(out)


def apply_chaos_delay():
    """TTFB component of edge_latency -- the stand-in for `tc netem delay`."""
    with _lock:
        mode, severity = _chaos["mode"], _chaos["severity"]
    if mode != "edge_latency":
        return 0.0
    delay, jitter, _bps = LATENCY_BY_SEVERITY.get(severity, LATENCY_BY_SEVERITY[2])
    actual = max(0.0, random.gauss(delay, jitter / 2))
    time.sleep(actual)
    return actual


def apply_chaos_throughput(nbytes):
    """Throughput component of edge_latency -- what makes buffers actually drain.

    Applied after TTFB is recorded, so segment_ttfb_seconds keeps meaning
    'time to first byte' while the viewer's measured download time reflects
    the degraded transfer rate.
    """
    with _lock:
        mode, severity = _chaos["mode"], _chaos["severity"]
    if mode != "edge_latency" or not nbytes:
        return 0.0
    _d, _j, bps = LATENCY_BY_SEVERITY.get(severity, LATENCY_BY_SEVERITY[2])
    if bps <= 0:
        return 0.0
    seconds = (nbytes * 8) / bps
    # Cap so a single request cannot pin a server thread indefinitely.
    seconds = min(seconds, 30.0)
    time.sleep(seconds)
    return seconds


def should_drop(path):
    """segment_gap: drop every Nth segment, producing a 404 storm."""
    global _seq
    with _lock:
        mode, severity = _chaos["mode"], _chaos["severity"]
        if mode != "segment_gap" or not path.endswith(".ts"):
            return False
        _seq += 1
        every = max(2, severity if severity > 1 else 7)
        return _seq % every == 0


def fetch_origin(path):
    """Fetch from origin. Returns (status, body, content_type)."""
    global _shield_misses
    url = f"{ORIGIN}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            with _lock:
                _shield_misses += 1
            return resp.status, body, ctype
    except urllib.error.HTTPError as e:
        with _lock:
            _shield_misses += 1
        return e.code, e.read(), "text/plain"
    except Exception:
        return 502, b"origin unreachable\n", "text/plain"


def cache_get(path):
    global _cache_bytes
    with _lock:
        entry = _cache.get(path)
        if not entry:
            return None
        expiry, status, body, ctype = entry
        if time.time() > expiry:
            _cache.pop(path, None)
            _cache_bytes -= len(body)
            return None
        _cache.move_to_end(path)
        return status, body, ctype


def cache_put(path, status, body, ctype):
    if not path.endswith(".ts") or status != 200:
        return          # only immutable segments are cached
    global _cache_bytes
    with _lock:
        prev = _cache.pop(path, None)
        if prev:
            _cache_bytes -= len(prev[2])
        _cache[path] = (time.time() + SEGMENT_TTL, status, body, ctype)
        _cache_bytes += len(body)
        while _cache_bytes > MAX_CACHE_BYTES and _cache:
            _, evicted = _cache.popitem(last=False)
            _cache_bytes -= len(evicted[2])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"deadair-edge/{REGION}"

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Deadair-Region", REGION)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/metrics":
            self._send(200, render_metrics().encode())
            return
        if path == "/healthz":
            self._send(200, b"ok\n")
            return
        if path == "/chaos":
            with _lock:
                self._send(200, (json.dumps(_chaos) + "\n").encode(),
                           "application/json")
            return
        if not path.startswith("/hls/"):
            self._send(404, b"not found\n")
            return

        started = time.time()
        global _hits, _misses

        injected = apply_chaos_delay()

        if should_drop(path):
            record_status(404)
            observe_ttfb(time.time() - started)
            self._send(404, b"segment gap (chaos)\n",
                       extra={"X-Deadair-Chaos": "segment_gap"})
            return

        cached = cache_get(path)
        if cached:
            with _lock:
                _hits += 1
            status, body, ctype = cached
            cache_state = "HIT"
        else:
            with _lock:
                _misses += 1
            status, body, ctype = fetch_origin(path)
            cache_put(path, status, body, ctype)
            cache_state = "MISS"

        record_status(status)
        # TTFB is recorded before throughput shaping so the metric keeps its
        # meaning; the client's total download time carries the transfer cost.
        observe_ttfb(time.time() - started)
        shaped = apply_chaos_throughput(len(body))
        self._send(status, body, ctype, extra={
            "X-Cache": cache_state,
            "X-Deadair-Injected-Delay": f"{injected:.3f}",
            "X-Deadair-Shaped-Seconds": f"{shaped:.3f}",
        })

    def do_POST(self):
        if urlparse(self.path).path != "/chaos":
            self._send(404, b"not found\n")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"invalid json"}\n', "application/json")
            return

        mode = str(payload.get("mode", "none"))
        severity = int(payload.get("severity", 2) or 0)
        if mode not in ("none", "edge_latency", "segment_gap"):
            self._send(400, json.dumps({
                "error": f"unknown mode {mode!r}",
                "modes": ["none", "edge_latency", "segment_gap"],
            }).encode() + b"\n", "application/json")
            return

        with _lock:
            _chaos["mode"] = mode
            _chaos["severity"] = severity
        print(f"[edge:{REGION}] chaos -> mode={mode} severity={severity}",
              flush=True)
        self._send(200, (json.dumps(
            {"region": REGION, "mode": mode, "severity": severity}) + "\n").encode(),
            "application/json")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"[edge:{REGION}] proxying {ORIGIN} on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
