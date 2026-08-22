"""Phase 4 (ACT) and Phase 5 (RECORD).

PHASE 4 -- propose exactly one remediation, and stop.
    The action comes from a fixed fault->action table via a FORCED tool call,
    not from free generation. The model cannot invent a sixth action, cannot
    propose a sequence, and cannot execute anything: the proposal path has no
    code that touches the plant.

PHASE 5 -- verify, then record.
    Recovery is established by re-querying the SLO after the action, never by
    the action having been performed. If the SLO is still breaching, the
    incident stays OPEN and the postmortem says so.
"""

import os

from google.adk.agents import LlmAgent, SequentialAgent
from google.genai import types

from .act_tools import (
    annotate_dashboard,
    estimate_viewer_impact,
    execute_approved_remediation,
    propose_remediation,
    record_incident,
    verify_recovery_for_diagnosis,
)
from .model import build_model
from .schemas import RecoveryRecord, RemediationProposal

# Forced function calling: the model MUST call propose_remediation rather than
# describing a remediation in prose. Prose is where an agent invents a plausible
# action that does not exist, and a fabricated remediation is the most dangerous
# thing this system could produce.
#
# BUT mode=ANY forces a call on EVERY turn, so an agent given it unconditionally
# can never finish -- it calls the tool, sees the result, is forced to call
# again, forever. That is not hypothetical: it looped until the 900s run ceiling
# aborted it. Forcing needs a termination condition.
#
# So force on the FIRST turn and switch to NONE once a result is in hand, which
# yields exactly one tool call followed by an explanation. That is precisely the
# contract this phase wants: one remediation, chosen from the table rather than
# invented, then justified in prose a human can act on.
def _force_exactly_one_call(callback_context, llm_request):
    already_called = any(
        getattr(part, "function_response", None)
        for content in (llm_request.contents or [])
        for part in (content.parts or [])
    )
    mode = (types.FunctionCallingConfigMode.NONE if already_called
            else types.FunctionCallingConfigMode.ANY)
    cfg = types.FunctionCallingConfig(mode=mode)
    if not already_called:
        cfg.allowed_function_names = ["propose_remediation"]
    if llm_request.config is None:
        llm_request.config = types.GenerateContentConfig()
    llm_request.config.tool_config = types.ToolConfig(
        function_calling_config=cfg)
    llm_request.config.temperature = 0.0
    return None

act_proposer = LlmAgent(
    name="act_proposer",
    model=build_model(),
    description="Proposes exactly one remediation for the diagnosed fault.",
    before_model_callback=_force_exactly_one_call,
    instruction="""\
You propose a remediation for a diagnosed fault in a live video plant.

DIAGNOSIS (Phase 3)
{diagnosis}

Call propose_remediation with the diagnosed fault and the affected region. You
get exactly one call -- after it returns you must explain, not call again.

You must NOT:
- invent an action that the tool does not return
- propose more than one action
- describe a sequence of steps
- execute anything

Then explain, for a human who will approve or reject this in a few seconds:
- what the action does, in one sentence
- why it follows from the evidence in the diagnosis
- what should change if it works, naming the metric
- what happens if the diagnosis was WRONG and this runs anyway

Be blunt about blast radius. An encoder restart interrupts every region and is
a materially different decision from draining one edge; say which this is.

If the diagnosis is no_fault_detected, propose no_action_required and say so
plainly. Proposing an action against a healthy plant is worse than proposing
nothing.""",
    tools=[propose_remediation],
    # Reads ONLY its templated inputs above, so the replayed session history is
    # dead freight -- see docs/agent-performance.md. ADK keeps this agent's own
    # tool loop regardless (a tool response is authored by the agent, not by
    # 'user', so it is never a turn boundary: functions.py:1302, contents.py:913).
    include_contents="none",
    output_key="act_investigation",
)

act_synthesizer = LlmAgent(
    name="act_synthesizer",
    model=build_model(),
    description="Emits the validated RemediationProposal for human approval.",
    instruction="""\
Produce a single RemediationProposal from the proposal below.

DIAGNOSIS
{diagnosis}

PROPOSAL
{act_investigation}

Rules:
- action_id, target, command, blast_radius and reversible are copied EXACTLY
  from the propose_remediation tool result. Do not reword the command; a human
  is going to read it and decide.
- requires_human_approval is always true and approval_status is always
  "pending". This phase never approves its own work.
- risk_if_wrong must describe what happens if the DIAGNOSIS was wrong, not what
  happens if the command fails. Those are different, and the first is what the
  human is really being asked about.
- human_summary is the one sentence someone approves or rejects on. Write it
  for someone who has not read the diagnosis.""",
    output_schema=RemediationProposal,
    # Reads ONLY its templated inputs above, so the replayed session history is
    # dead freight -- see docs/agent-performance.md. ADK keeps this agent's own
    # tool loop regardless (a tool response is authored by the agent, not by
    # 'user', so it is never a turn boundary: functions.py:1302, contents.py:913).
    include_contents="none",
    output_key="remediation_proposal",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

phase4_act = SequentialAgent(
    name="phase4_act",
    description="Phase 4: propose one remediation, gated on human approval.",
    sub_agents=[act_proposer, act_synthesizer],
)


record_investigator = LlmAgent(
    name="record_investigator",
    model=build_model(),
    description="Verifies recovery, annotates Grafana, files the incident.",
    instruction="""\
You close out an incident in a live video plant -- or refuse to.

DIAGNOSIS (Phase 3)
{diagnosis}

PROPOSAL (Phase 4)
{remediation_proposal}

APPROVAL / EXECUTION OUTCOME
{execution_result?}

Work in this order:

1. Call verify_recovery_for_diagnosis. It takes NO arguments.

   You do not choose the recovery check and you cannot: the routing is done in
   code from the diagnosed fault class, and the tool reports which check it
   used in slo_to_verify and why in why_this_check. Copy both.

   This used to be your decision, described here as a routing table. It is not
   any more, because the cost of getting it wrong is the whole thesis:
   black_source never moves rebuffer_ratio, so verifying a content fault with
   the delivery SLO returns "recovered" over a stream that is still black.
   That is not a judgement call worth leaving to prose.

   It runs whether or not the remediation was executed, so the record states
   the plant's actual condition rather than an assumed one.

2. Read the result honestly. recovered=false means the incident STAYS OPEN.
   Do not close it, do not describe the action as successful, and do not
   attribute recovery to an action that did not produce it. If it is still
   breaching, call verify_recovery_for_diagnosis ONCE more before concluding --
   recovery can lag -- and then report what you found.

3. Call estimate_viewer_impact with the diagnosed fault_id. That is the ONLY
   argument. The affected regions, the impact ratio and the incident duration
   are all derived in code, deliberately: the impact ratio is the number a
   model most reliably gets backwards, and the duration is one you cannot know
   -- nothing in your context carries a timestamp. Report the result as
   returned; do not adjust the arithmetic.

4. Call annotate_dashboard with a concise incident note, a region and the
   fault. Do NOT pass a timestamp; the incident start is read from the alert in
   code, so the annotation lands where an operator scrubbing the dashboard
   would look for it.

5. Call record_incident with a title and summary. The start time is again
   derived in code. If IRM is unavailable the tool falls back to a tagged
   annotation and tells you so in its "backend" field; carry that into
   incident_backend rather than reporting a bare id, which would imply an IRM
   incident exists when it does not.

Report every tool error verbatim. Never claim an annotation or incident was
created if the tool said otherwise.""",
    # verify_recovery and verify_visual_recovery are deliberately NOT here.
    # Exposing both made the choice between them a prompt decision; routing now
    # happens in code inside verify_recovery_for_diagnosis, which makes
    # verifying a content fault with a delivery metric unreachable rather than
    # merely discouraged.
    tools=[verify_recovery_for_diagnosis, annotate_dashboard,
           record_incident, estimate_viewer_impact],
    # Reads ONLY its templated inputs above, so the replayed session history is
    # dead freight -- see docs/agent-performance.md. ADK keeps this agent's own
    # tool loop regardless (a tool response is authored by the agent, not by
    # 'user', so it is never a turn boundary: functions.py:1302, contents.py:913).
    include_contents="none",
    output_key="record_investigation",
)

record_synthesizer = LlmAgent(
    name="record_synthesizer",
    model=build_model(),
    description="Emits the validated RecoveryRecord and postmortem.",
    instruction="""\
Produce a single RecoveryRecord, including the postmortem.

DIAGNOSIS
{diagnosis}

PROPOSAL
{remediation_proposal}

EXECUTION
{execution_result?}

VERIFICATION AND RECORDING
{record_investigation}

Rules:

- recovered comes ONLY from the recovery check's result -- verify_recovery for
  delivery faults, verify_visual_recovery for content faults. If it said false,
  recovered is false, still_degraded_reason quotes what the re-query showed,
  and the postmortem says the incident remains open. Never infer recovery from
  the action having been executed.
- slo_name, slo_before and slo_after must describe the check you ACTUALLY used.
  For a delivery fault that is rebuffer_ratio, before and after.
  For a CONTENT fault, slo_name is "visual_frame_check", slo_before is 1.0 and
  slo_after is 0.0, expressed as the fraction of viewers not seeing a correct
  picture. Do NOT report rebuffer_ratio for a content fault: it read 0.0 for
  the entire incident, so quoting it implies the SLO never breached and leaves
  a reader wondering why an incident existed at all.
- mttr_seconds is detection to VERIFIED RECOVERY. If recovery was never
  verified, leave it null -- an action that did not work has not repaired
  anything, and a fast MTTR on an unresolved incident is a lie.
- annotation_created, incident_id and incident_backend come from the tool
  results. incident_backend must be copied exactly from the tool's "backend"
  field. If it was a fallback, say so in the postmortem too.
- viewer_minutes_lost comes from estimate_viewer_impact. It is an estimate from
  a modeled fleet; carry that caveat into the postmortem rather than presenting
  it as measured.

The postmortem is markdown with these sections:

  ## What happened          one paragraph, plain language
  ## Timeline               timestamped, detection -> diagnosis -> action ->
                            verification
  ## Evidence               the measurements that decided it, with values
  ## Root cause             the fault, and how it was distinguished from the
                            faults it resembles
  ## Impact                 viewer-minutes lost, with assumptions stated
  ## Resolution             what was done, whether it worked, MTTR
  ## What telemetry alone   what conventional monitoring would have shown, and
     would have shown       whether it would have caught this at all

That last section is the point of the whole system. For a content fault, state
plainly that every delivery metric stayed green and the fault was only visible
in the pixels.""",
    output_schema=RecoveryRecord,
    # Reads ONLY its templated inputs above, so the replayed session history is
    # dead freight -- see docs/agent-performance.md. ADK keeps this agent's own
    # tool loop regardless (a tool response is authored by the agent, not by
    # 'user', so it is never a turn boundary: functions.py:1302, contents.py:913).
    include_contents="none",
    output_key="recovery_record",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

phase5_record = SequentialAgent(
    name="phase5_record",
    description="Phase 5: verify recovery, annotate, file, and write it up.",
    sub_agents=[record_investigator, record_synthesizer],
)
