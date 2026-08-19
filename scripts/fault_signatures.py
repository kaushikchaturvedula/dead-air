#!/usr/bin/env python3
"""Drive every fault in brief §5's menu and record its telemetry signature.

Each fault must look DIFFERENT in telemetry -- that is the entire premise of
the project, since the agent's job is to tell them apart. This script proves
it: it injects each mode in turn, waits for the fault to manifest, and reports
what actually moved.

It is both the regression test for the fault menu and the ground truth for
agent week. Run it after any change to the encoder, the edges, or the ladder.

    python3 scripts/fault_signatures.py            # all modes
    python3 scripts/fault_signatures.py black_source ladder_collapse
    python3 scripts/fault_signatures.py --markdown # emit the docs table

Observations are read from the LOCAL exporters rather than Mimir: the signature
is a property of the plant, and going through remote_write would add a minute
of batching lag to every measurement without changing what is observed.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENCODER = "http://localhost:9103"
VIEWERS = "http://localhost:9104"
ORIGIN = "http://localhost:8080"
EDGE_PORTS = {"us-east1": 8081, "europe-west1": 8082, "asia-south1": 8083}

# mode -> (settle seconds, region or None, severity)
MODES = {
    "black_source":    (75, None, 0),
    "ladder_collapse": (90, None, 0),
    "ladder_mismatch": (75, None, 0),
    "segment_gap":     (75, None, 7),
    "edge_latency":    (90, "europe-west1", 2),
}


def get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def scrape(url):
    out = {}
    try:
        body = get(url)
    except Exception:
        return out
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            pass
    return out


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def label_of(key, label):
    marker = f'{label}="'
    if marker not in key:
        return None
    return key.split(marker, 1)[1].split('"', 1)[0]


def observe():
    """One snapshot of every signal a diagnosis could key off."""
    enc = scrape(f"{ENCODER}/metrics")
    vw = scrape(f"{VIEWERS}/metrics")

    lags = {label_of(k, "rendition"): v
            for k, v in enc.items() if k.startswith("packager_segment_lag")}
    rebuf = {}
    for k, v in vw.items():
        if k.startswith("rebuffer_ratio"):
            rebuf.setdefault(label_of(k, "region"), []).append(v)
    bitrate = {}
    for k, v in vw.items():
        if k.startswith("viewer_bitrate_avg"):
            bitrate.setdefault(label_of(k, "region"), []).append(v)

    status = {}
    for region, port in EDGE_PORTS.items():
        m = scrape(f"http://localhost:{port}/metrics")
        for k, v in m.items():
            if k.startswith("segment_status"):
                cls = label_of(k, "status")
                if cls:
                    status.setdefault(region, {})[cls] = v

    try:
        master = get(f"{ORIGIN}/hls/master.m3u8")
        rungs = sum(1 for l in master.splitlines() if l.strip().endswith("index.m3u8"))
    except Exception:
        rungs = 0

    return {
        "encoder_fps": enc.get("encoder_fps", 0.0),
        "dropped_frames": enc.get("dropped_frames", 0.0),
        "encoder_up": enc.get("encoder_up", 0.0),
        "rungs_in_manifest": rungs,
        "segment_lag": lags,
        "max_lag": max(lags.values()) if lags else 0.0,
        "rebuffer_by_region": {r: max(v) for r, v in rebuf.items() if r},
        "bitrate_by_region": {r: (sum(v) / len(v)) for r, v in bitrate.items() if r},
        "status_by_region": status,
        "segments_deleted": enc.get("packager_segments_deleted_total", 0.0),
    }


def status_rate(before, after, cls):
    """Per-region delta for a status class over the observation window."""
    out = {}
    for region in EDGE_PORTS:
        b = (before["status_by_region"].get(region) or {}).get(cls, 0.0)
        a = (after["status_by_region"].get(region) or {}).get(cls, 0.0)
        out[region] = a - b
    return out


def clear_and_settle(seconds=75):
    post(f"{ENCODER}/chaos", {"mode": "none", "severity": 0})
    for port in EDGE_PORTS.values():
        post(f"http://localhost:{port}/chaos", {"mode": "none", "severity": 0})
    time.sleep(seconds)


def capture_frame(mode):
    """Grab a frame from the top rung -- the only evidence for the two faults
    that are invisible in telemetry."""
    try:
        pl = get(f"{ORIGIN}/hls/1080p/index.m3u8")
        seg = [l.strip() for l in pl.splitlines() if l.strip().endswith(".ts")]
        if not seg:
            return None
        url = f"{ORIGIN}/hls/1080p/{seg[-1]}"
        subprocess.run(["curl", "-sS", "-o", "/tmp/fs.ts", url], check=True,
                       capture_output=True, timeout=60)
        out = f"frames/signature-{mode}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "/tmp/fs.ts",
                        "-frames:v", "1", out], check=True, capture_output=True,
                       timeout=60)
        return out
    except Exception:
        return None


def describe(mode, before, after, deltas_4xx, deltas_5xx):
    """Reduce a before/after pair to the handful of facts that identify it."""
    lines = []
    b, a = before, after

    if a["rungs_in_manifest"] != b["rungs_in_manifest"]:
        lines.append(f"manifest rungs {b['rungs_in_manifest']} -> "
                     f"{a['rungs_in_manifest']}")
    stale = {r: round(v, 1) for r, v in a["segment_lag"].items() if v > 20}
    if stale:
        lines.append(f"packager_segment_lag climbing: {stale}")

    rb = {r: round(v, 3) for r, v in a["rebuffer_by_region"].items() if v > 0.02}
    lines.append(f"rebuffer_ratio > 0.02 in {rb}" if rb
                 else "rebuffer_ratio: 0 everywhere")

    br_b = b["bitrate_by_region"]
    br_a = a["bitrate_by_region"]
    drops = {r: f"{br_b.get(r, 0)/1e6:.1f}->{br_a.get(r, 0)/1e6:.1f}Mbps"
             for r in br_a if br_b.get(r, 0) - br_a.get(r, 0) > 200_000}
    lines.append(f"delivered bitrate dropped: {drops}" if drops
                 else "delivered bitrate: unchanged")

    hot4 = {r: int(v) for r, v in deltas_4xx.items() if v > 0}
    lines.append(f"4xx in window: {hot4}" if hot4 else "4xx: none")
    hot5 = {r: int(v) for r, v in deltas_5xx.items() if v > 0}
    if hot5:
        lines.append(f"5xx in window: {hot5}")

    lines.append(f"encoder_fps {a['encoder_fps']:.1f}, "
                 f"dropped_frames {int(a['dropped_frames'])}, "
                 f"encoder_up {int(a['encoder_up'])}")
    deleted = a["segments_deleted"] - b["segments_deleted"]
    if deleted > 0:
        lines.append(f"segments deleted by packager: {int(deleted)}")
    return lines


def run_mode(mode):
    settle, region, severity = MODES[mode]
    print(f"\n{'=' * 72}\n{mode}\n{'=' * 72}", flush=True)

    print("  clearing and settling to a healthy baseline...", flush=True)
    clear_and_settle(75)
    before = observe()
    print(f"  baseline: fps={before['encoder_fps']:.1f} "
          f"rungs={before['rungs_in_manifest']} "
          f"maxlag={before['max_lag']:.1f}s "
          f"rebuffer={ {r: round(v,3) for r,v in before['rebuffer_by_region'].items()} }",
          flush=True)

    if region:
        post(f"http://localhost:{EDGE_PORTS[region]}/chaos",
             {"mode": mode, "severity": severity})
        print(f"  injected {mode} at {region} (severity {severity})", flush=True)
    else:
        post(f"{ENCODER}/chaos", {"mode": mode, "severity": severity})
        print(f"  injected {mode} plant-wide", flush=True)

    print(f"  waiting {settle}s for the fault to manifest...", flush=True)
    time.sleep(settle)

    after = observe()
    d4 = status_rate(before, after, "4xx")
    d5 = status_rate(before, after, "5xx")
    frame = capture_frame(mode)

    print("  SIGNATURE:", flush=True)
    for line in describe(mode, before, after, d4, d5):
        print(f"    - {line}", flush=True)
    if frame:
        print(f"    - frame captured: {frame}", flush=True)

    return {"mode": mode, "before": before, "after": after,
            "d4xx": d4, "d5xx": d5, "frame": frame}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("modes", nargs="*", default=[])
    ap.add_argument("--json", help="write raw observations here")
    args = ap.parse_args()

    modes = args.modes or list(MODES)
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        sys.exit(f"unknown mode(s): {unknown}; known: {list(MODES)}")

    results = []
    try:
        for mode in modes:
            results.append(run_mode(mode))
    finally:
        print("\nclearing all faults", flush=True)
        post(f"{ENCODER}/chaos", {"mode": "none", "severity": 0})
        for port in EDGE_PORTS.values():
            post(f"http://localhost:{port}/chaos", {"mode": "none", "severity": 0})

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=1, default=str)
        print(f"raw observations -> {args.json}")


if __name__ == "__main__":
    main()
