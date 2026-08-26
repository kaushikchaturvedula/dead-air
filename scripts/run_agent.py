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
import threading
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
from dead_air.content_screen import screen_live_segment          # noqa: E402
from dead_air.video_tools import inspect_frame                   # noqa: E402
from dead_air.observability import reset_usage, usage_totals     # noqa: E402

WEBHOOK = os.environ.get("DEADAIR_WEBHOOK", "http://localhost:9102")
APP_NAME = "dead_air"

# Hard wall-clock ceiling on a single investigation. Backoff and per-call
# timeouts are bounded elsewhere, but this is the backstop that guarantees the
# agent either finishes or FAILS -- it never hangs.
#
# That distinction is a demo risk before it is an eval risk: on camera, an agent
# that errors can be retried in fifteen seconds, while an agent that hangs is
# unrecoverable. Prefer a loud failure.
RUN_TIMEOUT_SECONDS = float(os.environ.get("DEADAIR_RUN_TIMEOUT", "900"))

# Demo ceiling. 900s is the right BACKSTOP -- it exists to catch a genuine
# stall -- but a 15-minute wait on camera is as fatal as an infinite one.
#
# CALIBRATED AGAINST THE PATH IT ACTUALLY GUARDS. The first value here was 300s,
# derived from a ~165s ALERT-path run where Phase 2 is skipped. The demo runs
# the SWEEP path, where Phase 2 always runs and all five phases execute: that
# measures 543s, with scope alone taking 281s. A 300s ceiling would have
# aborted a perfectly healthy demo run -- demo mode killing the demo.
#
# 543s slowest COMPLETED sweep-path run -> 750s, ~38% headroom, while still
# failing fast enough to retry a take.
#
# Deliberately NOT derived from 900s. Nothing has ever completed in 900s: that
# is the backstop above, and the only runs to reach it were the forced-calling
# loop that hung and got aborted (see act.py, model.py). Sizing a demo ceiling
# off an abort would be calibrating against a hang instead of against work.
DEMO_TIMEOUT_SECONDS = float(os.environ.get("DEADAIR_DEMO_TIMEOUT", "750"))


class AgentRunTimeout(RuntimeError):
    """Raised when an investigation exceeds its wall-clock budget."""


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


async def run_once(state, quiet=False, timeout=None):
    """Run one investigation, bounded by a hard wall-clock timeout."""
    timeout = RUN_TIMEOUT_SECONDS if timeout is None else timeout
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            _run_once_inner(state, quiet, started), timeout=timeout)
    except asyncio.TimeoutError:
        raise AgentRunTimeout(
            f"investigation exceeded {timeout:.0f}s and was aborted "
            f"(region={state.get('alert_region')}, "
            f"trigger={state.get('trigger_kind')}). This is a hard ceiling, "
            f"not a retry -- something stalled rather than merely being slow."
        ) from None


async def _run_once_inner(state, quiet, started):
    reset_usage()
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
    elapsed = time.monotonic() - started
    usage = usage_totals()
    print(f"  [done] investigation completed in {elapsed:.1f}s  "
          f"llm_calls={usage['llm_calls']} tools={usage['tool_calls']} "
          f"tokens={usage['input_tokens']}in/{usage['output_tokens']}out "
          f"est_cost=${usage['usd']}", flush=True)
    st = dict(final.state)
    st["_elapsed_seconds"] = round(elapsed, 1)
    st["_usage"] = usage
    return st


def show(state):
    for key, title in (("incident_scope", "PHASE 1 -- INCIDENT SCOPE"),
                       ("visual_finding", "PHASE 2 -- VISUAL FINDING"),
                       ("diagnosis", "PHASE 3 -- DIAGNOSIS"),
                       ("remediation_proposal", "PHASE 4 -- REMEDIATION (proposed)"),
                       ("execution_result", "     APPROVAL GATE"),
                       ("recovery_record", "PHASE 5 -- RECOVERY + POSTMORTEM")):
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


def watch(poll_seconds, timeout=None, approval_mode="deny"):
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
                state["approval_mode"] = approval_mode
                try:
                    final = asyncio.run(run_once(state, timeout=timeout))
                    show(final)
                except Exception as e:
                    # One failed investigation must not kill the watcher.
                    print(f"  investigation failed: {type(e).__name__}: {e}",
                          flush=True)
        except Exception as e:
            print(f"  poll error: {type(e).__name__}: {e}", flush=True)
        time.sleep(poll_seconds)


def sweep(interval, region, timeout=None, approval_mode="deny",
          rendition="1080p"):
    """The confidence monitor, as a three-stage cascade.

        Stage 0  deterministic screen over the newest segment   ~1.3s, no model
        Stage 1  vision, ONLY when Stage 0 says suspect         ~10s
        Stage 2  the full five-phase pipeline, only on a confirmed finding

    Detection is arithmetic, so it runs every tick and costs almost nothing.
    Before this, every tick ran a full investigation, which meant a sweep
    interval could not go below several minutes and "catches it in seconds" was
    not true. Now the interval is bounded by a ~1.3s screen instead of a ~543s
    pipeline.
    """
    print(f"confidence monitor: {region}/{rendition}, cascade tick every "
          f"{interval}s\n  Stage 0 deterministic screen -> Stage 1 vision -> "
          f"Stage 2 full pipeline", flush=True)
    ticks = screens_suspect = escalated = 0
    while True:
        ticks += 1
        t0 = time.monotonic()
        screen = screen_live_segment(region, rendition)
        dt = time.monotonic() - t0
        m = screen.get("measurements", {})

        if not screen.get("suspect"):
            if screen.get("reason") == "error":
                print(f"  tick {ticks}: screen error -- {screen.get('error')}",
                      flush=True)
            else:
                print(f"  tick {ticks}: clear  (yavg={m.get('yavg_mean')} "
                      f"in {dt:.2f}s)", flush=True)
            time.sleep(interval)
            continue

        screens_suspect += 1
        print(f"\n  tick {ticks}: STAGE 0 SUSPECT -- {screen['reason']} "
              f"(yavg={m.get('yavg_mean')}, "
              f"dark_frames={m.get('dark_frame_fraction')}) in {dt:.2f}s",
              flush=True)

        # The monitor starts BEFORE Stage 1, not after. Vision is bounded at
        # 150s, and the last Stage 0 publish was already ~1.3s ago, so leaving
        # Stage 1 uncovered risks the panel going stale at the escalation beat.
        monitor = _ContentMonitor(region, rendition, interval)
        monitor.__enter__()

        # Stage 1: vision confirms WHAT is wrong before waking five phases.
        t1 = time.monotonic()
        seen = inspect_frame(region, rendition)
        v_dt = time.monotonic() - t1
        verdict = seen.get("classification", "no_frame_available")
        print(f"  tick {ticks}: STAGE 1 vision -> {verdict} "
              f"(confidence {seen.get('confidence')}, timecode "
              f"{seen.get('timecode_value')}) in {v_dt:.1f}s", flush=True)

        if verdict == "healthy":
            monitor.__exit__(None, None, None)
            print(f"  tick {ticks}: vision disagrees with the screen; not "
                  f"escalating\n", flush=True)
            time.sleep(interval)
            continue

        escalated += 1
        print(f"  tick {ticks}: STAGE 2 escalating to the full pipeline "
              f"(screens_suspect={screens_suspect}, escalated={escalated})\n",
              flush=True)
        state = {
            "alert_name": "DEAD AIR / confidence sweep",
            "alert_region": region,
            "alert_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "alert_summary": (
                f"Stage 0 screen flagged {screen['reason']} "
                f"(yavg={m.get('yavg_mean')}); Stage 1 vision confirmed "
                f"{verdict}."),
            "alert_status": "sweep",
            "trigger_kind": "sweep",
            "approval_mode": approval_mode,
            "stage0_screen": json.dumps(screen),
        }
        # The investigation blocks this loop; the monitor (already running,
        # started before Stage 1) keeps the content panel fed throughout.
        final = None
        try:
            final = asyncio.run(run_once(state, timeout=timeout))
        except Exception as e:
            print(f"  sweep error: {type(e).__name__}: {e}", flush=True)
        finally:
            monitor.__exit__(None, None, None)
        if final is not None:
            show(final)
        print(f"  {monitor.report()}\n", flush=True)
        time.sleep(interval)


class _ContentMonitor:
    """Keep Stage 0 publishing while a Stage 2 investigation blocks the loop.

    THE PROBLEM. The sweep is strictly serial: when Stage 0 flags, Stage 1 and
    then the whole five-phase investigation run inline, and the loop cannot
    reach the next screen until they return -- typically 209s, up to the 750s
    demo ceiling. No screens means no deadair_content_* samples, and the panels
    go stale at CONTENT_STALE_AFTER_SECONDS (90s). So roughly ninety seconds
    into every investigation the panel carrying the thesis flipped from a red
    DEAD AIR to an orange NOT WATCHING -- during the exact shot where the
    narration is "content red, delivery green". The inverse of the staleness
    bug the 90s budget was added to fix.

    WHY A BACKGROUND SCREEN RATHER THAN THE ALTERNATIVES.

      A heartbeat republishing the last verdict would hold the panel red, but
      it would be asserting a measurement it did not take. If the plant changed
      during those 209s the panel would state something false, and "measure,
      do not assume" is the entire thesis. It also cannot report a fault that
      STARTS during an investigation.

      Raising the staleness budget is the worst option available. To cover the
      750s demo ceiling it would have to exceed 750s, and to cover the 900s
      hard ceiling, 900s -- which reinstates the original bug at ten times the
      duration: a panel free to show a stale green for a quarter of an hour
      after the sweep has died. The 90s budget exists precisely to stop that.

      Screening on a thread publishes REAL readings throughout, so the budget
      keeps both its value and its meaning. It is also what a confidence
      monitor actually is: broadcast operations do not stop watching the output
      because someone is investigating it.

    LONG RUNS. This is duration-independent. Whether the investigation takes
    209s, hits the 750s demo ceiling or the 900s hard ceiling, samples keep
    flowing at the sweep interval and the panel never goes stale. Nothing here
    needs to know how long the run will be, which is the property the other two
    mechanisms lack.

    The thread is a daemon and every screen is wrapped: a failure to monitor
    must never take down the investigation it is monitoring.
    """

    def __init__(self, region, rendition, interval):
        self.region = region
        self.rendition = rendition
        # Clamped at BOTH ends. The floor stops us hammering the edge; the
        # ceiling matters more -- `make agent-sweep` with no INTERVAL passes
        # 300, and _stop.wait(300) would never return during a 169-245s
        # investigation, so the monitor would publish nothing and then report
        # that it had.
        self.interval = max(5, min(int(interval), 30))
        self.ticks = 0
        self.errors = 0
        self.max_gap = 0.0
        self._last_publish = None
        self._last_reason = None
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._last_publish = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="deadair-content-monitor", daemon=True)
        self._thread.start()
        return self

    def _screen_once(self):
        """One screen, recording whether it actually produced a reading."""
        try:
            v = screen_live_segment(self.region, self.rendition)
        except Exception as exc:                        # noqa: BLE001
            self.errors += 1
            print(f"    [monitor] screen raised: {type(exc).__name__}: "
                  f"{str(exc)[:80]}", flush=True)
            return None
        # screen_live_segment CATCHES its own failures and returns
        # reason="error" rather than raising, so counting only exceptions would
        # report errors=0 on a monitor whose every screen failed.
        if v.get("reason") == "error":
            self.errors += 1
            print(f"    [monitor] screen error: {str(v.get('error'))[:80]}",
                  flush=True)
            return v
        now = time.monotonic()
        if self._last_publish is not None:
            self.max_gap = max(self.max_gap, now - self._last_publish)
        self._last_publish = now
        self.ticks += 1
        return v

    def _run(self):
        # Screen ONCE immediately. Waiting first leaves the interval plus
        # however long Stage 1 took uncovered, and Stage 1 is bounded at 150s,
        # not 5.4s -- a slow vision call would flip the panel at exactly the
        # escalation beat.
        v = self._screen_once()
        if v is not None:
            self._last_reason = v.get("reason")
        # wait() returns True only when stop is set, so this both paces the
        # loop and exits promptly on teardown.
        while not self._stop.wait(self.interval):
            v = self._screen_once()
            if v is None:
                continue
            reason = v.get("reason")
            # Print only on a CHANGE. A line per tick would bury the agent's
            # own output, and the panel is the evidence here, not the terminal.
            if reason != self._last_reason:
                yavg = (v.get("measurements") or {}).get("yavg_mean")
                print(f"    [monitor] content now {reason!r} (yavg={yavg}) "
                      f"-- panel stays live during the investigation",
                      flush=True)
                self._last_reason = reason

    def __exit__(self, *exc_info):
        self._stop.set()
        if self._thread is not None:
            # Short join: a screen in flight can take up to ~60s (ffmpeg's own
            # timeout), and holding the terminal that long on teardown reads as
            # a hang -- especially on Ctrl-C, which reaches here as a
            # BaseException while __exit__ still runs.
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                print("    [monitor] a screen was still in flight at teardown; "
                      "it will publish once more and exit", flush=True)
        return False                     # never swallow the run's exception

    def report(self):
        """What actually happened -- measured, not asserted."""
        if self.ticks == 0:
            return (f"content monitor published NOTHING during the "
                    f"investigation ({self.errors} failed) -- THE PANEL WENT "
                    f"STALE")
        stale = self.max_gap > 90        # CONTENT_STALE_AFTER_SECONDS
        msg = (f"content monitor published {self.ticks} screen(s) during the "
               f"investigation, largest gap {self.max_gap:.0f}s")
        if self.errors:
            msg += f", {self.errors} failed"
        return msg + (" -- EXCEEDED the 90s staleness budget" if stale
                      else " -- panel stayed live")


def _legacy_sweep(interval, region, timeout=None, approval_mode="deny"):
    """The pre-cascade sweep: a full investigation every tick. Kept for
    comparison only -- it is what made detection latency minutes."""
    while True:
        state = {
            "alert_name": "DEAD AIR / scheduled confidence sweep",
            "alert_region": region,
            "alert_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "alert_summary": "Proactive content check. No alert has fired.",
            "alert_status": "sweep",
            "trigger_kind": "sweep",
            "approval_mode": approval_mode,
        }
        try:
            final = asyncio.run(run_once(state, timeout=timeout))
            show(final)
        except Exception as e:
            # Including AgentRunTimeout: a stalled sweep must not stop the
            # monitor. Log it and look again on the next tick.
            print(f"  sweep error: {type(e).__name__}: {e}", flush=True)
        time.sleep(interval)


def _preflight():
    """Refuse to start if a host prerequisite is missing.

    Checked here, before any run, so a cold clone gets one clear line instead
    of discovering it on the first sweep tick -- where a missing ffmpeg used to
    surface as `suspect: False`, i.e. as a healthy picture.
    """
    import shutil
    if shutil.which("ffmpeg") is None:
        print("error: ffmpeg is not on PATH.\n"
              "       It is a host prerequisite for the Stage 0 content screen, "
              "the frame grabs\n"
              "       and the rung measurement -- without it this agent cannot "
              "see the picture.\n"
              "         macOS:  brew install ffmpeg\n"
              "         Debian: sudo apt install ffmpeg",
              file=sys.stderr)
        sys.exit(2)


def main():
    _preflight()
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", help="run once for this region")
    ap.add_argument("--alert-name", default="DEAD AIR / manual trigger")
    ap.add_argument("--watch", action="store_true",
                    help="poll the webhook receiver and run on each new alert")
    ap.add_argument("--poll", type=int, default=15)
    ap.add_argument("--sweep", action="store_true",
                    help="proactive confidence monitor: run on a timer")
    ap.add_argument("--interval", type=int, default=30,
                    help="seconds between cascade ticks. Stage 0 costs ~1.3s, "
                         "so this can be small -- it no longer gates a full "
                         "investigation")
    ap.add_argument("--json", help="write final session state here")
    ap.add_argument("--timeout", type=float, default=None,
                    help=f"hard ceiling per run in seconds "
                         f"(default {RUN_TIMEOUT_SECONDS:.0f})")
    ap.add_argument("--approve", choices=["deny", "prompt", "auto"],
                    default="deny",
                    help="human approval gate for Phase 4's remediation. "
                         "deny (default) proposes without acting; prompt asks "
                         "on stdin; auto approves without asking and is for "
                         "scripted runs only")
    ap.add_argument("--demo", action="store_true",
                    help=f"demo mode: tighter {DEMO_TIMEOUT_SECONDS:.0f}s "
                         f"ceiling so a stall fails fast enough to retry on "
                         f"camera")
    args = ap.parse_args()

    budget = args.timeout
    if budget is None and args.demo:
        budget = DEMO_TIMEOUT_SECONDS

    # The gate defaults to deny: an unattended run proposes and records, and
    # never touches the plant.
    os.environ.setdefault("DEADAIR_APPROVAL_TOKEN", "operator-approved")
    approval_mode = args.approve

    if args.sweep:
        sweep(args.interval, args.region or "us-east1", timeout=budget,
              approval_mode=approval_mode)
        return

    if args.watch:
        watch(args.poll, timeout=budget, approval_mode=approval_mode)
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
        "approval_mode": approval_mode,
    }
    final = asyncio.run(run_once(state, timeout=budget))
    show(final)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({k: v for k, v in final.items()}, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
