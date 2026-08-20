#!/usr/bin/env python3
"""Wake the DEAD AIR agent from a Grafana alert.

THREE ENTRY POINTS, ONE PIPELINE
--------------------------------
    --watch                   REACTIVE. Poll the webhook receiver and run once
                              per new firing alert.
    --sweep                   PROACTIVE. The confidence monitor: run on a timer
                              regardless of alerts, always inspecting pixels.
    --region europe-west1     run once (manual / testing)

The proactive sweep is first-class, not a fallback. black_source moves no
metric, so no alert can ever fire for it -- an alert-driven agent sleeps through
the fault this project exists to catch. Real broadcast operations run a
confidence monitor watching the output continuously for exactly this reason.

The webhook receiver (plant/webhook/receiver.py) is a dependency-free container
and deliberately stays that way -- it records alert deliveries and nothing else.
This script is the bridge: it lives in the venv where google-adk is installed,
watches the receiver, and starts an agent run per firing alert. In production
this becomes the Cloud Run service the contact point posts to directly; the
agent code above it does not change.

The alert's region becomes the agent's STARTING scope, seeded into session
state. It is not the conclusion -- Phase 1 is told to check the other regions
too, because a fault confined to one region rules out the encoder.
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

from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(os.path.join(REPO, "agents", "grafana_probe", ".env"))

from google.adk.runners import Runner                            # noqa: E402
from google.adk.sessions import InMemorySessionService           # noqa: E402
from google.genai import types                                   # noqa: E402

from dead_air.agent import root_agent                            # noqa: E402

WEBHOOK = os.environ.get("DEADAIR_WEBHOOK", "http://localhost:9102")
APP_NAME = "dead_air"


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def alert_state(payload):
    """Pull the fields the agent needs out of a Grafana webhook payload."""
    alerts = payload.get("alerts") or []
    labels = (alerts[0].get("labels") if alerts else {}) or \
             payload.get("commonLabels") or {}
    annotations = (alerts[0].get("annotations") if alerts else {}) or \
                  payload.get("commonAnnotations") or {}
    return {
        "alert_name": labels.get("alertname", "unknown"),
        "alert_region": labels.get("region", "unknown"),
        "alert_time": (alerts[0].get("startsAt") if alerts else "") or "",
        "alert_summary": annotations.get("summary", ""),
        "alert_status": payload.get("status", "unknown"),
        "trigger_kind": "alert",
    }


async def run_once(state, quiet=False):
    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, app_name=APP_NAME,
                    session_service=session_service)
    session = await session_service.create_session(
        app_name=APP_NAME, user_id="alertmanager", state=state)

    if state.get("trigger_kind") == "sweep":
        kickoff = (
            f"Scheduled confidence sweep of region {state['alert_region']}. "
            "No alert has fired. Establish the plant's current state, then "
            "inspect what viewers are actually seeing -- a source blackout or a "
            "ladder mismatch moves no delivery metric and can only be found by "
            "looking. Then diagnose."
        )
    else:
        kickoff = (
            f"Alert {state['alert_name']} is {state['alert_status']} for region "
            f"{state['alert_region']}. {state['alert_summary']} "
            "Scope the incident, inspect what viewers are seeing if telemetry "
            "cannot discriminate, then diagnose."
        )
    message = types.Content(role="user", parts=[types.Part(text=kickoff)])

    kind = "SWEEP" if state.get("trigger_kind") == "sweep" else "ALERT"
    print(f"\n{'=' * 78}\nDEAD AIR woke [{kind}]: {state['alert_name']} / "
          f"{state['alert_region']}\n{'=' * 78}", flush=True)

    async for event in runner.run_async(
            user_id="alertmanager", session_id=session.id, new_message=message):
        author = getattr(event, "author", "?")
        if not quiet and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    args = json.dumps(dict(fc.args or {}))[:110]
                    print(f"  [{author}] -> {fc.name}({args})", flush=True)
                elif getattr(part, "function_response", None):
                    print(f"  [{author}] <- {part.function_response.name} ok",
                          flush=True)
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts).strip()
            if text:
                print(f"  [{author}] final ({len(text)} chars)", flush=True)

    final = await session_service.get_session(
        app_name=APP_NAME, user_id="alertmanager", session_id=session.id)
    return final.state


def show(state):
    for key, title in (("incident_scope", "PHASE 1 -- INCIDENT SCOPE"),
                       ("visual_finding", "PHASE 2 -- VISUAL FINDING"),
                       ("diagnosis", "PHASE 3 -- DIAGNOSIS")):
        raw = state.get(key)
        print(f"\n{'-' * 78}\n{title}\n{'-' * 78}")
        if not raw:
            print("  (not produced)")
            continue
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
            print(json.dumps(obj, indent=2))
        except Exception:
            print(raw)


def watch(poll_seconds):
    print(f"watching {WEBHOOK} for firing alerts (every {poll_seconds}s)",
          flush=True)
    seen = set()
    # Everything already delivered is history, not a trigger.
    try:
        for rec in json.loads(_get(f"{WEBHOOK}/alerts")):
            seen.add(rec.get("received_at"))
        print(f"  ignoring {len(seen)} pre-existing delivery(s)", flush=True)
    except Exception as e:
        print(f"  could not reach receiver: {e}", flush=True)

    while True:
        try:
            for rec in json.loads(_get(f"{WEBHOOK}/alerts")):
                stamp = rec.get("received_at")
                if stamp in seen:
                    continue
                seen.add(stamp)
                payload = rec.get("payload") or {}
                if payload.get("status") != "firing":
                    print(f"  {stamp}: {payload.get('status')} -- not a trigger",
                          flush=True)
                    continue
                state = alert_state(payload)
                final = asyncio.run(run_once(state))
                show(final)
        except Exception as e:
            print(f"  poll error: {type(e).__name__}: {e}", flush=True)
        time.sleep(poll_seconds)


def sweep(interval, region):
    """The confidence monitor: proactive, on a timer, always inspects pixels."""
    print(f"confidence monitor: sweeping {region} every {interval}s "
          f"(vision always runs on this path)", flush=True)
    while True:
        state = {
            "alert_name": "DEAD AIR / scheduled confidence sweep",
            "alert_region": region,
            "alert_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "alert_summary": "Proactive content check. No alert has fired.",
            "alert_status": "sweep",
            "trigger_kind": "sweep",
        }
        try:
            final = asyncio.run(run_once(state))
            show(final)
        except Exception as e:
            print(f"  sweep error: {type(e).__name__}: {e}", flush=True)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", help="run once for this region")
    ap.add_argument("--alert-name", default="DEAD AIR / manual trigger")
    ap.add_argument("--watch", action="store_true",
                    help="poll the webhook receiver and run on each new alert")
    ap.add_argument("--poll", type=int, default=15)
    ap.add_argument("--sweep", action="store_true",
                    help="proactive confidence monitor: run on a timer")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between sweeps")
    ap.add_argument("--json", help="write final session state here")
    args = ap.parse_args()

    if args.sweep:
        sweep(args.interval, args.region or "us-east1")
        return

    if args.watch:
        watch(args.poll)
        return

    if not args.region:
        ap.error("pass --region, or --watch")

    state = {
        "alert_name": args.alert_name,
        "alert_region": args.region,
        "alert_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "alert_summary": f"Manual trigger for {args.region}.",
        "alert_status": "firing",
        "trigger_kind": "manual",
    }
    final = asyncio.run(run_once(state))
    show(final)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({k: v for k, v in final.items()}, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
