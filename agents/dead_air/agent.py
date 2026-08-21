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
      phase4_act (SequentialAgent)
        act_proposer                   FORCED tool call, exactly one action
        act_synthesizer                -> RemediationProposal (validated)
      >>> HUMAN APPROVAL GATE <<<      outside the agent entirely
      phase5_record (SequentialAgent)
        record_investigator            verify SLO, annotate, file incident
        record_synthesizer             -> RecoveryRecord  (validated)

THE APPROVAL GATE IS NOT AN AGENT
---------------------------------
Phase 4 ends by producing a proposal and stopping. Execution happens in
scripts/run_agent.py, between phase 4 and phase 5, and only after a human
answers. That placement is deliberate: a gate implemented as an instruction is
a suggestion, while a gate implemented as a missing code path is a gate. The
proposal tool has no ability to touch the plant, and the execution tool refuses
without a token the model cannot mint.

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

from .act import phase4_act, phase5_record
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


def _human_approval_gate(callback_context) -> types.Content | None:
    """Run the approval gate between Phase 4 and Phase 5.

    This is CODE, not an agent. The model produced a proposal and stopped; this
    decides whether it executes. Three things make the gate real rather than
    advisory:

      1. execute_approved_remediation is not in any agent's toolset, so the
         model has no way to call it at all.
      2. It refuses without a token held in the environment, which the model
         never sees.
      3. The default is DENY. An unattended run proposes and records; it does
         not act.

    Modes, seeded into state by scripts/run_agent.py:
      prompt  ask a human on stdin        (the demo path)
      auto    approve without asking      (scripted evaluation only)
      deny    never execute               (default)
    """
    state = callback_context.state
    raw = state.get("remediation_proposal")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if not isinstance(raw, dict):
        state["execution_result"] = json.dumps(
            {"executed": False, "reason": "no proposal to act on"})
        return None

    action_id = raw.get("action_id", "no_action_required")
    target = raw.get("target", "")
    mode = state.get("approval_mode", "deny")

    if action_id == "no_action_required":
        state["execution_result"] = json.dumps({
            "executed": False,
            "reason": "proposal was no_action_required; nothing to approve",
            "approval_status": "auto_skipped"})
        return None

    approved = False
    if mode == "auto":
        approved = True
        decision = "approved automatically (scripted run)"
    elif mode == "prompt":
        banner = (
            f"\n{'=' * 78}\n  HUMAN APPROVAL REQUIRED\n{'=' * 78}\n"
            f"  action      : {action_id}\n"
            f"  target      : {target}\n"
            f"  command     : {raw.get('command', '')}\n"
            f"  blast radius: {raw.get('blast_radius', '')}\n"
            f"  if wrong    : {raw.get('risk_if_wrong', '')}\n"
            f"\n  {raw.get('human_summary', '')}\n"
        )
        print(banner, flush=True)
        try:
            answer = input("  Approve and execute? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        approved = answer in ("y", "yes")
        decision = "approved by operator" if approved else "REJECTED by operator"
    else:
        decision = "not executed (approval mode is deny)"

    if not approved:
        state["execution_result"] = json.dumps({
            "executed": False, "approval_status": "rejected",
            "reason": decision,
            "note": "The plant was NOT changed. Phase 5 still records the "
                    "incident and the plant's current condition."})
        print(f"  -> {decision}; nothing executed\n", flush=True)
        return None

    from .act_tools import execute_approved_remediation
    result = execute_approved_remediation(
        action_id=action_id, target=target,
        approval_token=os.environ.get("DEADAIR_APPROVAL_TOKEN", ""))
    result["approval_status"] = "approved"
    result["decision"] = decision
    state["execution_result"] = json.dumps(result)
    print(f"  -> {decision}; executed={result.get('executed')}\n", flush=True)
    return None


phase2_see.before_agent_callback = _skip_visual_inspection
phase5_record.before_agent_callback = _human_approval_gate

root_agent = SequentialAgent(
    name="dead_air",
    description=(
        "Diagnoses live video streaming incidents by correlating Grafana Cloud "
        "metrics, logs and traces with the actual delivered pixels."
    ),
    sub_agents=[phase1_scope, phase2_see, phase3_diagnose, phase4_act,
                phase5_record],
)
