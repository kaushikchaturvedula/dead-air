"""Minimal traffic generator for the L2 edges.

NOT the L3 viewer fleet. This deliberately models nothing: no playback clock,
no buffer, no rebuffer events, no QoE beacons. It exists only so the edges have
continuous traffic to serve, which is what makes a per-region differential
visible when one region is degraded. L3 replaces it with real modeled clients
that compute rebuffer ratio.

Each worker walks the ladder the way a player would -- master manifest, rung
playlist, then the newest segments -- so cache hit ratios and TTFB histograms
reflect realistic access patterns rather than a flat hammer.
"""

import os
import random
import threading
import time
import urllib.error
import urllib.request

# "region=url,region=url,..."
EDGES = [
    spec.split("=", 1)
    for spec in os.environ.get("EDGES", "").split(",")
    if "=" in spec
]
CLIENTS_PER_EDGE = int(os.environ.get("CLIENTS_PER_EDGE", "3"))
SEGMENT_SECONDS = float(os.environ.get("SEGMENT_SECONDS", "4"))
RUNGS = ["1080p", "720p", "480p", "360p"]


def fetch(url, session_id, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("X-Deadair-Session", session_id)
    req.add_header("User-Agent", "deadair-loadgen/1")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def worker(region, base, index):
    session_id = f"{region}-load-{index:03d}"
    # Weight toward the top rungs, as an ABR player on a healthy link would.
    rung = random.choices(RUNGS, weights=[4, 3, 2, 1])[0]
    seen = set()

    while True:
        try:
            fetch(f"{base}/hls/master.m3u8", session_id)
            status, body = fetch(f"{base}/hls/{rung}/index.m3u8", session_id)
            if status == 200 and body:
                segs = [l.strip() for l in body.decode(errors="replace").splitlines()
                        if l.strip().endswith(".ts")]
                # Newest couple of segments, like a player at the live edge.
                for seg in segs[-2:]:
                    key = f"{rung}/{seg}"
                    fetch(f"{base}/hls/{rung}/{seg}", session_id)
                    seen.add(key)
                if len(seen) > 500:
                    seen.clear()
            # Occasionally switch rung, exercising cache across the ladder.
            if random.random() < 0.05:
                rung = random.choices(RUNGS, weights=[4, 3, 2, 1])[0]
        except Exception:
            pass
        time.sleep(SEGMENT_SECONDS * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    if not EDGES:
        raise SystemExit("set EDGES=region=url,region=url,...")
    print(f"[loadgen] {CLIENTS_PER_EDGE} client(s) per edge across "
          f"{len(EDGES)} edge(s)", flush=True)
    for region, base in EDGES:
        for i in range(CLIENTS_PER_EDGE):
            threading.Thread(
                target=worker, args=(region, base.rstrip("/"), i), daemon=True
            ).start()
            time.sleep(0.2)   # stagger so they do not lockstep
    while True:
        time.sleep(60)
