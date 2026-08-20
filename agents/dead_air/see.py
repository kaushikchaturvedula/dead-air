"""Phase 2 (SEE): fetch the actual delivered pixels and judge them.

This is the phase that justifies the project. Delivery telemetry measures
whether BYTES arrived; it cannot measure whether those bytes contain a PICTURE.
Phase 1 can prove every metric is green. Only this phase can prove the screen is
black.

The division of labour is settled by measurement, not preference
(docs/vision-spike.md):

    vision  black / frozen / corrupted        detected 100%, no false positives
    code    does the rung carry its detail    vision detects this 0% of the time

So the SEE agent must call check_rung_resolution for any resolution question and
must never answer one from the frame description.
"""

import os

from google.adk.agents import LlmAgent, SequentialAgent

from .schemas import VisualFinding
from .video_tools import check_rung_resolution, get_stream_manifest, inspect_frame

# Shared factory: one tier decision, one retry policy. See model.py for why
# backoff is mandatory given the ParallelAgent fan-out.
from .model import build_model

see_investigator = LlmAgent(
    name="see_investigator",
    model=build_model(),
    description="Fetches real segments from the plant and inspects the picture.",
    instruction="""\
You inspect what viewers are actually seeing in a live video stream.

Phase 1 produced this scope:

{incident_scope}

Work through it in order and do not skip steps:

1. get_stream_manifest for the region in inspect_region. Establish which
   renditions the ladder currently advertises. A MISSING rung is
   ladder_collapse and is settled here -- no frame needed.

2. inspect_frame on inspect_region and inspect_rendition. This returns what the
   picture actually shows: healthy, black_frame, frozen_frame or corrupted.

   The burned-in timecode is the most valuable thing in that result. Over a
   black picture:
     - timecode RUNNING  -> the encoder is alive and the SOURCE went black.
                            That is black_source.
     - timecode STOPPED or absent -> the source froze, or the encoder died.
   If the first frame is black, inspect a second frame from the same rendition
   and compare the timecode values to establish which.

3. check_rung_resolution for the same region -- ALWAYS, even when the frame
   looks healthy. This is a deterministic measurement, not a model judgement,
   and it is the ONLY authority on whether a rung carries the detail it
   advertises. ladder_mismatch is invisible to frame inspection: the picture
   looks completely normal. If you skip this call you will miss the fault
   entirely.

You must NEVER conclude anything about resolution, sharpness or upscaling from
the frame description alone. If check_rung_resolution was inconclusive, say
inconclusive. Do not substitute an impression for a measurement.

Finally, state whether the pixels AGREE or DISAGREE with Phase 1's telemetry.
Disagreement is the headline finding of this whole system: every dashboard
green while the screen is black.

Report tool errors verbatim. If no frame could be fetched, say so -- do not
guess at what it would have shown.""",
    tools=[get_stream_manifest, inspect_frame, check_rung_resolution],
    output_key="see_investigation",
)

see_synthesizer = LlmAgent(
    name="see_synthesizer",
    model=build_model(),
    description="Turns the visual investigation into a VisualFinding.",
    instruction="""\
Produce a single VisualFinding from the investigation below.

SCOPE FROM PHASE 1
{incident_scope}

VISUAL INVESTIGATION
{see_investigation}

Rules:

- frame_verdict comes only from what inspect_frame reported. There is no
  "upscaled" verdict available and you must not invent one -- resolution is
  reported separately in rung_resolution_verdict.
- rung_resolution_verdict and rung_resolution_ratio come ONLY from
  check_rung_resolution. If it was not called or was inconclusive, say
  not_checked or inconclusive. Never infer them from the picture description.
- timecode_value: copy it exactly as read. Over a black picture a running clock
  means the source went black while the encoder kept working.
- contradicts_telemetry is true when the pixels disagree with Phase 1 --
  healthy delivery metrics but a black, frozen or corrupted picture, or a
  suspect_upscaled measurement with no metric anomaly at all.
- suspected_fault: black_frame with a running timecode is black_source;
  suspect_upscaled is ladder_mismatch; a missing rendition is ladder_collapse.
  If the pixels are healthy and the resolution measurement is clean, the fault
  is not a content fault -- say no_fault_detected here and let the delivery
  evidence from Phase 1 stand.

visual_summary should be one an on-call engineer could paste into an incident
channel.""",
    output_schema=VisualFinding,
    output_key="visual_finding",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

phase2_see = SequentialAgent(
    name="phase2_see",
    description="Phase 2: inspect delivered pixels and emit a VisualFinding.",
    sub_agents=[see_investigator, see_synthesizer],
)
