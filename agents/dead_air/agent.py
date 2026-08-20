"""DEAD AIR -- autonomous broadcast operations agent.

Phase lifecycle as a SequentialAgent; Phase 1's fan-out as a ParallelAgent.

    root (SequentialAgent)
      phase1_scope (SequentialAgent)
        scope_fanout (ParallelAgent)   metrics | logs | traces | dashboards
        scope_synthesizer              -> IncidentScope   (validated)
      phase2_see (SequentialAgent)     [conditional -- see below]
        see_investigator               manifest, frames, vision, rung check
        see_synthesizer                -> VisualFinding   (validated)
      phase3_diagnose (SequentialAgent)
        diagnose_investigator          ranks, then runs the checklist
        diagnose_synthesizer           -> Diagnosis       (validated)

TWO TRIGGERS, ONE PIPELINE
--------------------------
Both are first-class; neither is a workaround for the other.

    reactive   a Grafana alert webhook fires  -> investigate this region
    proactive  a scheduled confidence sweep   -> look at the picture anyway

The proactive path exists because of a measured fact, not a hunch: black_source
moves NO metric, so no threshold is crossed and no alert ever fires. Ten minutes
of black leaves every alert instance Normal. An alert-driven agent would sleep
through the one fault this project is built around.

That is also what real broadcast operations run. A confidence monitor watches
the output continuously; it is not woken by delivery thresholds. The proactive
trigger makes the thesis self-consistent: the agent finds dead air because it is
looking, not because telemetry told it to -- which is the whole point, since
telemetry cannot see it.

PHASE 2 IS CONDITIONAL
----------------------
On the reactive path, Phase 2 runs only when Phase 1 sets
needs_visual_inspection -- typically when every delivery metric looks healthy.
A regional edge fault is already discriminated by telemetry, so fetching frames
costs a vision call and adds nothing.

On the sweep path Phase 2 ALWAYS runs, because looking at the picture is the
entire purpose of the sweep.

Constructed synchronously at module import. Do not switch to an async factory:
that works under `adk web` but breaks on Cloud Run and Agent Engine.
"""

import json
import os

from google.adk.agents import SequentialAgent
from google.genai import types

from .diagnose import phase3_diagnose
from .scope import phase1_scope
from .see import phase2_see

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")


def _skip_visual_inspection(callback_context) -> types.Content | None:
    """Skip Phase 2 when telemetry already discriminated the fault.

    Returning Content from a before_agent_callback skips the agent's body. The
    sweep path never skips: on that path vision IS the point.
    """
    state = callback_context.state
    if state.get("trigger_kind") == "sweep":
        return None

    scope = state.get("incident_scope")
    if isinstance(scope, str):
        try:
            scope = json.loads(scope)
        except json.JSONDecodeError:
            scope = None
    if not isinstance(scope, dict):
        return None                      # no scope to judge by -- inspect anyway

    if scope.get("needs_visual_inspection", True):
        return None

    reason = (
        "Phase 2 skipped: Phase 1 set needs_visual_inspection=false, meaning "
        "delivery telemetry already discriminated the fault, and this run was "
        "triggered by an alert rather than a confidence sweep. Fetching frames "
        "would cost a vision call without changing the diagnosis. Note that "
        "black_source and ladder_mismatch therefore remain UNCONFIRMABLE rather "
        "than ruled out."
    )
    state["visual_finding"] = json.dumps({
        "region": scope.get("inspect_region", ""),
        "rendition": scope.get("inspect_rendition", ""),
        "frames_inspected": 0,
        "frame_verdict": "no_frame_available",
        "skipped": True,
        "skip_reason": reason,
        "visual_summary": reason,
        "suspected_fault": "unknown",
    })
    return types.Content(role="model", parts=[types.Part(text=reason)])


phase2_see.before_agent_callback = _skip_visual_inspection

root_agent = SequentialAgent(
    name="dead_air",
    description=(
        "Diagnoses live video streaming incidents by correlating Grafana Cloud "
        "metrics, logs and traces with the actual delivered pixels."
    ),
    sub_agents=[phase1_scope, phase2_see, phase3_diagnose],
)
