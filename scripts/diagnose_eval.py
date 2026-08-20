#!/usr/bin/env python3
"""Drive every fault past the DIAGNOSE phase and score the result.

The exit criterion for Phase 3: all five of brief §5's faults diagnosed
correctly end to end, plus a healthy control that correctly returns no fault.

This injects each fault, waits for it to manifest in Mimir (remote_write
batches, so the wait is real), runs the full agent pipeline, and compares the
Diagnosis against ground truth.

    python3 scripts/diagnose_eval.py                    # all six
    python3 scripts/diagnose_eval.py black_source healthy
    python3 scripts/diagnose_eval.py --checks-only      # skip the agent; just
                                                        # score the deterministic
                                                        # checklist

--checks-only is the fast loop. It exercises signatures.py + diagnose_tools.py
without any model call, which is where a signature bug will actually show up.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agents"))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(os.path.join(REPO, "agents", "grafana_probe", ".env"))

ENCODER = "http://localhost:9103"
EDGE_PORTS = {"us-east1": 8081, "europe-west1": 8082, "asia-south1": 8083}

# fault -> (settle seconds, region or None, severity, expected verdict)
# Settle times are generous: the deterministic checks read Mimir, and
# remote_write batching means a freshly injected fault takes ~60-90s to become
# visible there. Testing too early scores the PREVIOUS state.
CASES = {
    "healthy":         (150, None, 0, "no_fault_detected"),
    "black_source":    (150, None, 0, "black_source"),
    "ladder_mismatch": (150, None, 0, "ladder_mismatch"),
    "ladder_collapse": (300, None, 0, "ladder_collapse"),
    "segment_gap":     (180, None, 7, "segment_gap"),
    "edge_latency":    (200, "europe-west1", 2, "edge_latency"),
}

# Faults only findable in pixels: their required checks need Phase 2, so the
# checks-only path must supply visual evidence the way Phase 2 would.
NEEDS_VISUAL = {"black_source", "ladder_mismatch", "healthy"}


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def clear_all():
    post(f"{ENCODER}/chaos", {"mode": "none", "severity": 0})
    for port in EDGE_PORTS.values():
        post(f"http://localhost:{port}/chaos", {"mode": "none", "severity": 0})


def inject(fault, region, severity):
    if fault == "healthy":
        return
    if region:
        post(f"http://localhost:{EDGE_PORTS[region]}/chaos",
             {"mode": fault, "severity": severity})
    else:
        post(f"{ENCODER}/chaos", {"mode": fault, "severity": severity})


def gather_visual(region="us-east1"):
    """Run Phase 2's tools directly, without the model, for --checks-only."""
    from dead_air.video_tools import check_rung_resolution, inspect_frame
    frame = inspect_frame(region, "1080p")
    rung = check_rung_resolution(region)
    return {
        "frame_verdict": {"healthy": "healthy", "black_frame": "black_frame",
                          "frozen_frame": "frozen_frame",
                          "corrupted": "corrupted"}.get(
                              frame.get("classification"), "no_frame_available"),
        "timecode_legible": bool(frame.get("timecode_legible")),
        "timecode_value": frame.get("timecode_value"),
        "rung_resolution_verdict": rung.get("verdict", "inconclusive"),
        "rung_resolution_ratio": rung.get("ratio"),
    }


def run_checks_only(fault, expected, region_for_manifest="us-east1"):
    from dead_air.diagnose_tools import attach_visual_evidence, collect_evidence
    from dead_air.signatures import evaluate_all

    evidence = collect_evidence(region_for_manifest)
    if fault in NEEDS_VISUAL:
        evidence = attach_visual_evidence(
            evidence, gather_visual(region_for_manifest))
    result = evaluate_all(evidence)
    verdict = result["deterministic_verdict"]

    ok = verdict == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {fault:<17} verdict={verdict:<20} "
          f"expected={expected}", flush=True)
    if not ok:
        print(f"        confirmed={result['confirmed_faults']} "
              f"ruled_out={result['ruled_out']} "
              f"unconfirmable={result['unconfirmable']}", flush=True)
        ev = evidence
        print(f"        rebuffer={ev.get('rebuffer_by_region')}", flush=True)
        print(f"        bitrate={ev.get('bitrate_by_region')}", flush=True)
        print(f"        4xx={ev.get('fourxx_status')} "
              f"regions={ev.get('fourxx_regions')}", flush=True)
        print(f"        missing_rungs={ev.get('missing_rungs')} "
              f"rungs_active={ev.get('rungs_active')}", flush=True)
        print(f"        visual={ev.get('visual')}", flush=True)
    return ok, verdict, result


def run_full_agent(fault, expected):
    from run_agent import run_once

    state = {
        "alert_name": ("DEAD AIR / scheduled confidence sweep" if fault
                       in NEEDS_VISUAL else "DEAD AIR / rebuffer ratio high"),
        "alert_region": "europe-west1" if fault == "edge_latency" else "us-east1",
        "alert_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "alert_summary": f"Evaluation run for {fault}.",
        "alert_status": "firing",
        # Content faults are found by the sweep, because they fire no alert.
        "trigger_kind": "sweep" if fault in NEEDS_VISUAL else "alert",
    }
    final = asyncio.run(run_once(state, quiet=True))
    raw = final.get("diagnosis")
    try:
        diag = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        diag = {}
    verdict = diag.get("deterministic_verdict")
    reported = diag.get("fault")
    ok = verdict == expected and reported == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {fault:<17} "
          f"fault={reported} verdict={verdict} expected={expected}", flush=True)
    if not ok:
        print(f"        summary: {str(diag.get('operator_summary'))[:200]}",
              flush=True)
    return ok, diag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("faults", nargs="*", default=[])
    ap.add_argument("--checks-only", action="store_true",
                    help="score the deterministic checklist without the agent")
    ap.add_argument("--json")
    args = ap.parse_args()

    faults = args.faults or list(CASES)
    unknown = [f for f in faults if f not in CASES]
    if unknown:
        sys.exit(f"unknown: {unknown}; known: {list(CASES)}")

    mode = "deterministic checklist only" if args.checks_only else "full agent"
    print(f"DIAGNOSE evaluation -- {mode}\n")

    results, passed = [], 0
    try:
        for fault in faults:
            settle, region, severity, expected = CASES[fault]
            print(f"\n=== {fault} ===", flush=True)
            clear_all()
            # ABR recovery is gradual -- players step back up the ladder over a
            # minute or two. Testing the next case before that finishes scores a
            # still-recovering plant as a degraded one.
            print("  cleared; letting ABR recover for 120s", flush=True)
            time.sleep(120)
            inject(fault, region, severity)
            print(f"  injected; waiting {settle}s for Mimir to catch up",
                  flush=True)
            time.sleep(settle)

            manifest_region = region or "us-east1"
            if args.checks_only:
                ok, verdict, detail = run_checks_only(
                    fault, expected, manifest_region)
                results.append({"fault": fault, "expected": expected,
                                "verdict": verdict, "pass": ok})
            else:
                try:
                    ok, diag = run_full_agent(fault, expected)
                except Exception as e:
                    # A transient API failure on one case should not discard the
                    # other five results.
                    ok, diag = False, {"error": f"{type(e).__name__}: {str(e)[:300]}"}
                    print(f"  FAIL  {fault:<17} run failed: "
                          f"{type(e).__name__}: {str(e)[:160]}", flush=True)
                results.append({"fault": fault, "expected": expected,
                                "diagnosis": diag, "pass": ok})
            passed += bool(ok)
    finally:
        clear_all()
        print("\nall faults cleared", flush=True)

    print(f"\n{'=' * 62}\n  {passed}/{len(faults)} correct\n{'=' * 62}")
    for r in results:
        print(f"  {'PASS' if r['pass'] else 'FAIL'}  {r['fault']}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=1, default=str)

    sys.exit(0 if passed == len(faults) else 1)


if __name__ == "__main__":
    main()
