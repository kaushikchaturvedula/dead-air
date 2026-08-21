#!/usr/bin/env python3
"""Calibrate and validate the Stage 0 content screen.

Two passes, because they test different things:

  fixtures  102 stills across 6 states x 4 rungs, regenerable with
            `python3 scripts/fault_signatures.py --fixtures`. These validate the
            BLACK path against every fault state at every rung.
  live      real segments pulled from the running plant. Stills cannot exercise
            freezedetect at all -- a single frame has no temporal dimension --
            so freeze behaviour is only meaningful here.

The bar, per the brief: 100% detection of black_source, and 0% false positives
on healthy, segment_gap, edge_latency and ladder_collapse. A screen that
false-positives wakes the whole pipeline for nothing; one that misses defeats
the thesis. Numbers are reported as measured -- if the bar is missed, that is
the finding.

    python3 scripts/calibrate_content_screen.py            # fixtures
    python3 scripts/calibrate_content_screen.py --live     # live segments too
    python3 scripts/calibrate_content_screen.py --sweep-thresholds
"""

import argparse
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agents"))

from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(os.path.join(REPO, "agents", "grafana_probe", ".env"))

from dead_air.content_screen import screen_media                  # noqa: E402

FIXTURE_ROOT = os.path.join(REPO, "fixtures", "frames")
RUNGS = ["1080p", "720p", "480p", "360p"]

# What Stage 0 SHOULD say for each injected state. Only black_source is
# suspect: every other fault leaves the picture itself intact, and
# ladder_mismatch is invisible to luma entirely -- it is a resolution fault that
# the deterministic rung check owns, not this screen.
EXPECTED_SUSPECT = {
    "healthy": False,
    "black_source": True,
    "ladder_mismatch": False,
    "ladder_collapse": False,
    "segment_gap": False,
    "edge_latency": False,
}


def fixture_paths():
    out = []
    if not os.path.isdir(FIXTURE_ROOT):
        return out
    for state in sorted(os.listdir(FIXTURE_ROOT)):
        d = os.path.join(FIXTURE_ROOT, state)
        if not os.path.isdir(d) or state not in EXPECTED_SUSPECT:
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".png"):
                out.append((state, f.split("_")[0], os.path.join(d, f)))
    return out


def run_fixtures():
    items = fixture_paths()
    if not items:
        sys.exit("no fixtures -- run: python3 scripts/fault_signatures.py --fixtures")
    print(f"screening {len(items)} fixture stills\n")

    results = []
    by_state = {}
    for state, rung, path in items:
        v = screen_media(path)
        ok = v["suspect"] == EXPECTED_SUSPECT[state]
        results.append((state, rung, v, ok))
        m = v.get("measurements", {})
        by_state.setdefault(state, []).append(m.get("yavg_mean"))

    print("  measured YAVG by state (the separator):")
    for state, vals in sorted(by_state.items()):
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        print(f"    {state:<17} min={min(vals):7.2f}  mean={statistics.fmean(vals):7.2f}"
              f"  max={max(vals):7.2f}   n={len(vals)}")

    print("\n  CONFUSION MATRIX (rows = injected state)")
    print(f"    {'state':<17}{'expect':>8}{'suspect':>9}{'clear':>7}{'errors':>8}  verdict")
    print("    " + "-" * 62)
    total_ok = 0
    for state in EXPECTED_SUSPECT:
        rows = [r for r in results if r[0] == state]
        if not rows:
            continue
        sus = sum(1 for r in rows if r[2]["suspect"])
        err = sum(1 for r in rows if r[2]["reason"] == "error")
        clear = len(rows) - sus - err
        ok = sum(1 for r in rows if r[3])
        total_ok += ok
        want = "suspect" if EXPECTED_SUSPECT[state] else "clear"
        mark = "PASS" if ok == len(rows) else f"FAIL {ok}/{len(rows)}"
        print(f"    {state:<17}{want:>8}{sus:>9}{clear:>7}{err:>8}  {mark}")

    n = len(results)
    print(f"\n    overall: {total_ok}/{n} correct ({100*total_ok/n:.1f}%)")

    detect = [r for r in results if r[0] == "black_source"]
    fp_states = [s for s, want in EXPECTED_SUSPECT.items() if not want]
    fps = [r for r in results if r[0] in fp_states and r[2]["suspect"]]
    d_rate = 100 * sum(1 for r in detect if r[2]["suspect"]) / len(detect) if detect else 0
    n_neg = sum(1 for r in results if r[0] in fp_states)
    fp_rate = 100 * len(fps) / n_neg if n_neg else 0
    print(f"    black_source detection : {d_rate:.1f}%   (bar: 100%)")
    print(f"    false positive rate    : {fp_rate:.1f}%   (bar: 0%)")
    if fps:
        print("    false positives:")
        for s, rung, v, _ in fps[:6]:
            print(f"      {s}/{rung}: reason={v['reason']} "
                  f"yavg={v['measurements'].get('yavg_mean')}")
    return d_rate == 100.0 and fp_rate == 0.0


def run_live(states):
    """Screen live segments -- the only way to exercise freezedetect."""
    import json
    import time
    import urllib.request

    from dead_air.content_screen import screen_live_segment

    ENCODER = "http://localhost:9103"

    def post(url, payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode()

    print("\n  LIVE segments (stills cannot exercise freezedetect)")
    print(f"    {'state':<17}{'suspect':>9}{'reason':>10}{'yavg':>9}"
          f"{'freeze':>8}{'secs':>7}")
    print("    " + "-" * 60)
    ok = True
    for state in states:
        post(f"{ENCODER}/chaos",
             {"mode": "none" if state == "healthy" else state, "severity": 0})
        time.sleep(75)
        t0 = time.time()
        v = screen_live_segment("us-east1", "1080p")
        dt = time.time() - t0
        m = v.get("measurements", {})
        want = EXPECTED_SUSPECT.get(state)
        good = v["suspect"] == want
        ok = ok and good
        print(f"    {state:<17}{str(v['suspect']):>9}{v['reason']:>10}"
              f"{str(m.get('yavg_mean')):>9}{str(m.get('freeze_detected')):>8}"
              f"{dt:>7.2f}" + ("" if good else "   <-- WRONG"))
    post(f"{ENCODER}/chaos", {"mode": "none", "severity": 0})
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--states", default="healthy,black_source")
    args = ap.parse_args()

    fixtures_ok = run_fixtures()
    live_ok = True
    if args.live:
        live_ok = run_live([s.strip() for s in args.states.split(",") if s.strip()])

    print()
    print(f"  fixtures: {'PASS' if fixtures_ok else 'FAIL'}"
          + (f"   live: {'PASS' if live_ok else 'FAIL'}" if args.live else ""))
    sys.exit(0 if (fixtures_ok and live_ok) else 1)


if __name__ == "__main__":
    main()
