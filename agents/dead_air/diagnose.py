"""Phase 3 (DIAGNOSE): rank hypotheses, confirm with deterministic checks.

    Gemini   ranks which hypotheses are worth testing, and explains the result
    code     runs the confirming checks and computes the verdict
    evidence decides

The model does not get to conclude. It calls match_fault_signatures, which
evaluates fixed predicates against fixed queries and returns a computed verdict.
The model's job is to explain that verdict in operator language and -- if it
disagrees -- to say so explicitly rather than quietly substituting its own
answer. A recorded disagreement is debuggable; a silent override is not.
"""

import os

from google.adk.agents import LlmAgent, SequentialAgent

from .diagnose_tools import collect_evidence, match_fault_signatures
from .schemas import Diagnosis

# Shared factory: one tier decision, one retry policy. See model.py for why
# backoff is mandatory given the ParallelAgent fan-out.
from .model import build_model

diagnose_investigator = LlmAgent(
    name="diagnose_investigator",
    model=build_model(),
    description="Ranks candidate faults and runs the deterministic checklist.",
    instruction="""\
You are diagnosing a fault in a live video streaming plant.

SCOPE FROM PHASE 1
{incident_scope}

VISUAL FINDING FROM PHASE 2 (may be absent if telemetry already discriminated)
{visual_finding?}

Do this in order:

1. State which faults you consider most likely from the scope, and why. This is
   your ranking, and it is genuinely useful -- but it is a hypothesis, not a
   conclusion.

2. Call match_fault_signatures with the region to inspect. It collects evidence
   with fixed queries and evaluates every fault's checklist. It returns a
   deterministic_verdict.

3. Read the returned checklists carefully. For each fault, note which required
   checks passed, failed, or could not be evaluated.

The fault menu and what identifies each:

  edge_latency     rebuffering and elevated TTFB in exactly ONE region, with
                   the others confirmed healthy
  segment_gap      4xx ONGOING across multiple regions; bitrate unaffected
  ladder_collapse  a rung missing from the manifest; bitrate down everywhere;
                   little or no rebuffering; any 4xx burst has STOPPED
  black_source     no delivery signal moved at all; the picture is black
  ladder_mismatch  no delivery signal moved at all; the rung resolution
                   measurement reports the top rung as upscaled

CRITICAL -- the 404 trap. ladder_collapse and segment_gap BOTH produce 404s.
Presence of 404s does not separate them. Persistence does:

    ongoing  -> segments are still being deleted        -> segment_gap
    stopped  -> a burst died out as players re-read the manifest
                                                        -> ladder_collapse

The evidence carries fourxx_status computed from two time windows. Use it. Do
not reason about 404s from raw counts.

Rules:
- The deterministic verdict is authoritative. If your ranking disagrees with it,
  say so plainly and explain which check you think is wrong. Do not overrule it.
- A fault marked 'unconfirmable' has NOT been ruled out. Missing evidence is a
  gap, never an elimination. Report it as a gap.
- Report tool errors verbatim.""",
    tools=[match_fault_signatures, collect_evidence],
    # Reads ONLY its templated inputs above, so the replayed session history is
    # dead freight -- see docs/agent-performance.md. ADK keeps this agent's own
    # tool loop regardless (a tool response is authored by the agent, not by
    # 'user', so it is never a turn boundary: functions.py:1302, contents.py:913).
    include_contents="none",
    output_key="diagnosis_investigation",
)

diagnose_synthesizer = LlmAgent(
    name="diagnose_synthesizer",
    model=build_model(),
    description="Turns the checklist result into a validated Diagnosis.",
    instruction="""\
Produce a single Diagnosis from the investigation below.

SCOPE (Phase 1)
{incident_scope}

VISUAL FINDING (Phase 2, may be absent)
{visual_finding?}

INVESTIGATION (Phase 3)
{diagnosis_investigation}

Rules:

- deterministic_verdict MUST be copied exactly from the match_fault_signatures
  result. Do not adjust it.
- fault should equal deterministic_verdict. Set them differently ONLY if you
  have a specific, stated reason -- and then set model_agrees_with_evidence to
  false and explain in disagreement_note. The verdict still stands as the
  deterministic answer.
- confirming_evidence: quote the checks that passed, WITH their measured values.
  "rebuffering confined to one region (europe-west1 0.44, others 0.00)" is
  useful. "rebuffering was regional" is not.
- ruled_out: only faults whose REQUIRED checks actually failed, each with the
  check that failed.
- unconfirmable: faults whose required checks could not be evaluated. Keep these
  strictly separate from ruled_out -- conflating them is how an agent reports
  false certainty.
- blind_spots and evidence_complete: copy VERBATIM from the tool's blind_spots
  and evidence_complete. These say what the checklist could not see -- an
  exporter publishing nothing, a query that failed, a manifest that would not
  fetch. Never empty the list to make the output look cleaner, and never
  describe a signal you could not read as healthy. If blind_spots is non-empty,
  say so in diagnosis as well, because a reader who is not shown a gap will
  assume there was none.
- fourxx_status: copy from the evidence.
- discriminator_note: whenever 404s were present at all, state explicitly
  whether they were ongoing or stopped and which fault that implicated. If no
  404s were seen, say so.
- telemetry_visible: false when no delivery signal moved and the fault was only
  findable in pixels. That is the DEAD AIR case and worth flagging clearly.
- recommended_action: the concrete remediation to propose, e.g. restart the
  encoder with the correct input, restore the missing rung, fail the degraded
  region out of rotation. Phase 4 will gate it on a human -- do not act.

operator_summary should read like the first message in an incident channel.""",
    output_schema=Diagnosis,
    # Reads ONLY its templated inputs above, so the replayed session history is
    # dead freight -- see docs/agent-performance.md. ADK keeps this agent's own
    # tool loop regardless (a tool response is authored by the agent, not by
    # 'user', so it is never a turn boundary: functions.py:1302, contents.py:913).
    include_contents="none",
    output_key="diagnosis",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

phase3_diagnose = SequentialAgent(
    name="phase3_diagnose",
    description="Phase 3: confirm a fault against deterministic signatures.",
    sub_agents=[diagnose_investigator, diagnose_synthesizer],
)
