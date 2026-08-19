"""L3 viewer fleet -- modeled clients where rebuffer ratio is born.

Brief §5: rebuffer ratio is a *client-side* metric. Real OTT companies get it
from a player SDK (Conviva, Mux, NPAW), never from the CDN, because the CDN
cannot see a stalled buffer. This fleet is that SDK.

Each session runs the §5 buffer model on a playback clock:

    buffer += segment_duration - download_time
    buffer <= 0  =>  REBUFFER EVENT, stalled for |buffer| seconds

plus simple ABR: a session that cannot fetch a rung faster than realtime steps
down the ladder, and steps back up once it has healthy headroom. Without ABR a
degraded region pins rebuffer_ratio at ~1.0, which is neither realistic nor a
useful signal -- real players trade quality for continuity, and the residual
rebuffering after they have downshifted is what actually reaches viewers.

CARDINALITY (brief §5, the single biggest infra risk):

    Metrics  -> aggregated by region and device_class ONLY. Never per session.
                200 sessions x 8 metrics as labels is tens of thousands of
                series and a blown free-tier quota in an afternoon.
    Loki     -> per-session QoE beacons, where high-cardinality data belongs.

Both halves are emitted here, deliberately, from the same data.
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("VIEWER_METRICS_PORT", "9104"))
BEACON_LOG = os.environ.get("BEACON_LOG", "/var/log/deadair/viewers.log")

EDGES = [s.split("=", 1) for s in os.environ.get("EDGES", "").split(",") if "=" in s]
CLIENTS_PER_REGION = int(os.environ.get("CLIENTS_PER_REGION", "15"))
SEGMENT_SECONDS = float(os.environ.get("SEGMENT_SECONDS", "4"))

# Ladder, richest first. Index 0 is the top rung.
RUNGS = ["1080p", "720p", "480p", "360p"]
RUNG_BITRATE = {"1080p": 5_000_000, "720p": 3_000_000,
                "480p": 1_500_000, "360p": 800_000}

# Bounded label -- three values, per §5's allowlist.
DEVICE_CLASSES = ["tv", "desktop", "mobile"]
# A phone holds less buffer than a TV, so it stalls sooner on the same link.
DEVICE_BUFFER_TARGET = {"tv": 24.0, "desktop": 16.0, "mobile": 10.0}

# Sliding window over which rebuffer_ratio is computed.
RATIO_WINDOW = float(os.environ.get("RATIO_WINDOW_SECONDS", "120"))

_lock = threading.Lock()
# (region, device_class) -> counters
_agg = {}
# (region, device_class) -> deque of (timestamp, playing_sec, rebuffer_sec)
_window = {}
# session_id -> (region, device_class, current bitrate). Internal only: it is
# aggregated before export and never becomes a metric label.
_current_rung = {}
_beacon_lock = threading.Lock()


def agg_key(region, device):
    return (region, device)


def ensure(region, device):
    k = agg_key(region, device)
    with _lock:
        if k not in _agg:
            _agg[k] = {
                "playing_seconds": 0.0,
                "rebuffer_seconds": 0.0,
                "rebuffer_events": 0,
                "sessions": 0,
                "startup_sum": 0.0,
                "startup_count": 0,
                "bitrate_sum": 0.0,
                "bitrate_count": 0,
            }
            _window[k] = deque()
    return k


def record(region, device, playing=0.0, rebuffer=0.0, events=0):
    k = ensure(region, device)
    now = time.time()
    with _lock:
        a = _agg[k]
        a["playing_seconds"] += playing
        a["rebuffer_seconds"] += rebuffer
        a["rebuffer_events"] += events
        w = _window[k]
        w.append((now, playing, rebuffer))
        cutoff = now - RATIO_WINDOW
        while w and w[0][0] < cutoff:
            w.popleft()


def windowed_ratio(k):
    """rebuffer_seconds / (rebuffer_seconds + playing_seconds) over the window."""
    now = time.time()
    cutoff = now - RATIO_WINDOW
    with _lock:
        w = _window.get(k) or deque()
        play = sum(p for t, p, _r in w if t >= cutoff)
        stall = sum(r for t, _p, r in w if t >= cutoff)
    total = play + stall
    return (stall / total) if total > 0 else 0.0


def write_beacon(payload):
    """Per-session QoE beacon -> Loki. Never a metric label."""
    try:
        with _beacon_lock, open(BEACON_LOG, "a") as fh:
            fh.write(json.dumps(payload) + "\n")
    except OSError:
        pass


class Session(threading.Thread):
    daemon = True

    def __init__(self, region, base, index):
        super().__init__()
        self.region = region
        self.base = base.rstrip("/")
        self.device = DEVICE_CLASSES[index % len(DEVICE_CLASSES)]
        self.session_id = f"{region}-{self.device}-{index:03d}"
        self.rung_index = 1                 # start at 720p, like a real player
        self.buffer = 0.0
        self.buffer_target = DEVICE_BUFFER_TARGET[self.device]
        self.startup_time = None
        self.rebuffer_count = 0
        self.rebuffer_seconds = 0.0
        self.played = set()

    # --- transport ---------------------------------------------------------

    def fetch(self, path, timeout=45):
        url = f"{self.base}{path}"
        req = urllib.request.Request(url)
        req.add_header("X-Deadair-Session", self.session_id)
        req.add_header("User-Agent", f"deadair-viewer/{self.device}")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return r.status, body, time.time() - t0
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            return e.code, b"", time.time() - t0
        except Exception:
            return 0, b"", time.time() - t0

    # --- ABR ---------------------------------------------------------------

    def adapt(self, download_time):
        """Step down when we cannot keep up, back up when we have headroom."""
        if download_time > SEGMENT_SECONDS * 0.9:
            if self.rung_index < len(RUNGS) - 1:
                self.rung_index += 1
        elif download_time < SEGMENT_SECONDS * 0.35 and self.buffer > self.buffer_target * 0.6:
            if self.rung_index > 0:
                self.rung_index -= 1

    # --- playback loop -----------------------------------------------------

    def run(self):
        ensure(self.region, self.device)
        with _lock:
            _agg[agg_key(self.region, self.device)]["sessions"] += 1
        started = time.time()
        # Stagger so the fleet does not fetch in lockstep.
        time.sleep(random.uniform(0, SEGMENT_SECONDS))

        last_beacon = time.time()

        while True:
            rung = RUNGS[self.rung_index]
            status, body, _dt = self.fetch(f"/hls/{rung}/index.m3u8")
            if status != 200 or not body:
                # Manifest unavailable: the buffer drains in real time.
                time.sleep(SEGMENT_SECONDS / 2)
                self.drain(SEGMENT_SECONDS / 2)
                continue

            segs = [l.strip() for l in body.decode(errors="replace").splitlines()
                    if l.strip().endswith(".ts")]
            fresh = [s for s in segs if f"{rung}/{s}" not in self.played][-2:]
            if not fresh:
                time.sleep(SEGMENT_SECONDS / 2)
                self.drain(SEGMENT_SECONDS / 2)
                continue

            for seg in fresh:
                status, body, download_time = self.fetch(f"/hls/{rung}/{seg}")
                self.played.add(f"{rung}/{seg}")
                if len(self.played) > 400:
                    self.played = set(list(self.played)[-200:])

                if self.startup_time is None:
                    self.startup_time = time.time() - started
                    with _lock:
                        a = _agg[agg_key(self.region, self.device)]
                        a["startup_sum"] += self.startup_time
                        a["startup_count"] += 1

                if status != 200:
                    # A missing segment stalls playback for its duration.
                    self.stall(SEGMENT_SECONDS)
                    continue

                # §5's buffer model.
                #
                # A segment that arrives late still plays its full duration --
                # the viewer stalls waiting for it, then watches it. Crediting
                # playing time only when the buffer stays positive makes
                # playing_seconds stop accumulating the moment a session starts
                # struggling, which pins rebuffer_ratio at exactly 1.0 and
                # destroys the shape of the curve. Playing time is unconditional
                # on a successful fetch; the stall is additional.
                self.buffer += SEGMENT_SECONDS - download_time
                record(self.region, self.device, playing=SEGMENT_SECONDS)
                if self.buffer <= 0:
                    self.stall(-self.buffer)
                    self.buffer = 0.0

                self.adapt(download_time)
                # Report the rung we are on NOW, not a lifetime average: §5's
                # ladder_collapse diagnosis is "avg bitrate drops, no rebuffer",
                # and a cumulative mean is dominated by history so it can never
                # show a drop clearly.
                with _lock:
                    _current_rung[self.session_id] = (
                        self.region, self.device, RUNG_BITRATE[RUNGS[self.rung_index]]
                    )

                # Pace to the playback clock: hold roughly buffer_target of
                # content, then wait rather than racing ahead of the live edge.
                if self.buffer > self.buffer_target:
                    time.sleep(min(SEGMENT_SECONDS, self.buffer - self.buffer_target))
                    self.buffer -= min(SEGMENT_SECONDS, self.buffer - self.buffer_target)

            if time.time() - last_beacon > 30:
                self.emit_beacon()
                last_beacon = time.time()

    def drain(self, seconds):
        """Wall clock passed without new content: the buffer pays for it."""
        played = min(seconds, max(self.buffer, 0.0))
        self.buffer -= seconds
        if played:
            record(self.region, self.device, playing=played)
        if self.buffer <= 0:
            self.stall(-self.buffer)
            self.buffer = 0.0

    def stall(self, seconds):
        seconds = max(0.0, min(seconds, 30.0))
        self.rebuffer_count += 1
        self.rebuffer_seconds += seconds
        record(self.region, self.device, rebuffer=seconds, events=1)

    def emit_beacon(self):
        """QoE beacon: everything §5 lists, including session_id -- into Loki."""
        write_beacon({
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session_id": self.session_id,
            "region": self.region,
            "device_class": self.device,
            "rendition": RUNGS[self.rung_index],
            "startup_time": round(self.startup_time or 0.0, 3),
            "rebuffer_count": self.rebuffer_count,
            "rebuffer_sec": round(self.rebuffer_seconds, 3),
            "bitrate": RUNG_BITRATE[RUNGS[self.rung_index]],
            "buffer_seconds": round(self.buffer, 2),
        })


# --- metrics ---------------------------------------------------------------

def render_metrics():
    with _lock:
        snapshot = {k: dict(v) for k, v in _agg.items()}

    out = [
        "# HELP rebuffer_ratio Fraction of viewing time spent rebuffering, "
        "aggregated across sessions.\n",
        "# TYPE rebuffer_ratio gauge\n",
    ]
    for (region, device) in sorted(snapshot):
        ratio = windowed_ratio((region, device))
        out.append(
            f'rebuffer_ratio{{region="{region}",device_class="{device}"}} '
            f"{ratio:.6f}\n"
        )

    def counter(name, field, helptext):
        lines = [f"# HELP {name} {helptext}\n", f"# TYPE {name} counter\n"]
        for (region, device), a in sorted(snapshot.items()):
            lines.append(
                f'{name}{{region="{region}",device_class="{device}"}} '
                f"{a[field]:.3f}\n"
            )
        return lines

    out += counter("viewer_rebuffer_seconds_total", "rebuffer_seconds",
                   "Cumulative seconds of rebuffering across sessions.")
    out += counter("viewer_playing_seconds_total", "playing_seconds",
                   "Cumulative seconds of content played across sessions.")
    out += counter("viewer_rebuffer_events_total", "rebuffer_events",
                   "Cumulative rebuffer events across sessions.")

    out += ["# HELP viewer_sessions_active Modeled sessions currently running.\n",
            "# TYPE viewer_sessions_active gauge\n"]
    for (region, device), a in sorted(snapshot.items()):
        out.append(f'viewer_sessions_active{{region="{region}",'
                   f'device_class="{device}"}} {a["sessions"]}\n')

    out += ["# HELP viewer_startup_seconds_avg Mean time to first segment.\n",
            "# TYPE viewer_startup_seconds_avg gauge\n"]
    for (region, device), a in sorted(snapshot.items()):
        avg = (a["startup_sum"] / a["startup_count"]) if a["startup_count"] else 0.0
        out.append(f'viewer_startup_seconds_avg{{region="{region}",'
                   f'device_class="{device}"}} {avg:.3f}\n')

    # Current mean delivered bitrate across active sessions -- the ABR outcome
    # right now. Aggregated from per-session state that never leaves this
    # process as a label.
    with _lock:
        current = list(_current_rung.values())
    by_key = {}
    for region, device, bitrate in current:
        by_key.setdefault((region, device), []).append(bitrate)
    out += ["# HELP viewer_bitrate_avg Mean bitrate currently being delivered "
            "across active sessions (ABR outcome).\n",
            "# TYPE viewer_bitrate_avg gauge\n"]
    for (region, device) in sorted(snapshot):
        vals = by_key.get((region, device)) or []
        avg = (sum(vals) / len(vals)) if vals else 0.0
        out.append(f'viewer_bitrate_avg{{region="{region}",'
                   f'device_class="{device}"}} {avg:.0f}\n')

    # Cardinality canary, viewer edition. Deliberately carries session_id so the
    # Alloy guard has something real to strip at L3, and carries region too so
    # the same series proves the allowlist KEEPS what the diagnosis needs while
    # dropping what would blow the quota. Exactly one series: several differing
    # only by session_id would collapse into duplicate samples once the label is
    # correctly dropped.
    out += [
        "# HELP viewer_cardinality_canary Always 1; labelled with session_id to "
        "prove per-session labels never reach Mimir.\n",
        "# TYPE viewer_cardinality_canary gauge\n",
        'viewer_cardinality_canary{session_id="viewer-canary-0001",'
        'region="us-east1",device_class="tv"} 1\n',
    ]
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.startswith("/metrics"):
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/healthz"):
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not EDGES:
        raise SystemExit("set EDGES=region=url,region=url,...")
    os.makedirs(os.path.dirname(BEACON_LOG), exist_ok=True)

    total = len(EDGES) * CLIENTS_PER_REGION
    print(f"[viewers] {CLIENTS_PER_REGION} sessions x {len(EDGES)} regions "
          f"= {total} modeled clients", flush=True)
    print(f"[viewers] metrics on :{PORT}/metrics, beacons -> {BEACON_LOG}",
          flush=True)

    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever(),
        daemon=True,
    ).start()

    for region, base in EDGES:
        for i in range(CLIENTS_PER_REGION):
            Session(region, base, i).start()
            time.sleep(0.05)

    while True:
        time.sleep(60)
