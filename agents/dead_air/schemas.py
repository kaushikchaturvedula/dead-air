"""Structured outputs for the DEAD AIR agent.

Every phase emits a validated object rather than prose. Prose is where a
diagnosis agent goes wrong quietly: it reads fluently whether or not it is
correct, which the SEE-phase spike demonstrated in detail (docs/vision-spike.md).
A schema makes a wrong answer at least a *checkable* wrong answer.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

# Brief §5's fault menu, plus the two catch-alls an honest agent needs.
FaultLabel = Literal[
    "edge_latency",
    "segment_gap",
    "ladder_collapse",
    "black_source",
    "ladder_mismatch",
    "unknown",
    "no_fault_detected",
]


class IncidentScope(BaseModel):
    """Phase 1 output: WHAT is affected, before any claim about why.

    Scope is deliberately separated from diagnosis. The agent's first job is to
    bound the blast radius -- one region or all three, one rendition or the
    whole ladder -- because that boundary alone eliminates most of the fault
    menu before a single frame is fetched.
    """

    alert_name: str = Field(description="Alert rule that fired.")
    alert_region: str = Field(
        description="Region from the alert payload. The starting scope.")

    affected_regions: list[str] = Field(
        default_factory=list,
        description="Regions with confirmed degraded signals.")
    healthy_regions: list[str] = Field(
        default_factory=list,
        description="Regions checked and found healthy. Populating this is as "
                    "important as affected_regions: it is what distinguishes a "
                    "regional fault from a plant-wide one.")
    blast_radius: Literal["single_region", "multi_region", "all_regions",
                          "plant_wide", "unknown"] = "unknown"

    renditions_advertised: list[str] = Field(
        default_factory=list,
        description="Renditions the master manifest currently advertises.")
    renditions_degraded: list[str] = Field(default_factory=list)

    metrics_evidence: list[str] = Field(
        default_factory=list,
        description="Concrete metric observations with values. Never a "
                    "restatement of the query that was run.")
    logs_evidence: list[str] = Field(default_factory=list)
    traces_evidence: list[str] = Field(default_factory=list)
    dashboard_refs: list[str] = Field(
        default_factory=list,
        description="Dashboard UIDs/titles relevant to this incident.")

    signals_unavailable: list[str] = Field(
        default_factory=list,
        description="Signals that could not be queried at all. Absence of data "
                    "is not evidence of health and must be reported as a gap, "
                    "not silently omitted.")

    candidate_faults: list[FaultLabel] = Field(
        default_factory=list,
        description="Faults still consistent with the evidence, most likely "
                    "first. Faults ruled out by scope should be omitted.")
    ruled_out: list[str] = Field(
        default_factory=list,
        description="Faults eliminated, each with the observation that "
                    "eliminated it.")

    needs_visual_inspection: bool = Field(
        default=True,
        description="True when telemetry cannot discriminate further -- in "
                    "particular when every delivery metric looks healthy, "
                    "which is the signature of a content fault.")
    inspect_region: str = Field(
        default="", description="Region whose edge Phase 2 should fetch from.")
    inspect_rendition: str = Field(
        default="1080p", description="Rendition Phase 2 should inspect.")

    scope_summary: str = Field(
        description="Two or three sentences an on-call engineer could act on.")
    confidence: float = Field(ge=0.0, le=1.0)


class VisualFinding(BaseModel):
    """Phase 2 output: what the pixels actually show.

    NOTE the frame_verdict enum has no 'upscaled' option, deliberately. The
    spike showed that offering it is what invites confabulation: models produce
    confident, specific, exactly-backwards prose about resolution fidelity.
    Resolution is decided by rung_resolution_verdict, which is measured in code.
    """

    region: str
    rendition: str
    frames_inspected: int = 0

    frame_verdict: Literal[
        "healthy", "black_frame", "frozen_frame", "corrupted",
        "no_frame_available",
    ] = "no_frame_available"
    vision_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_evidence: str = Field(
        default="", description="What was actually visible in the frame.")
    timecode_legible: bool = False
    timecode_value: Optional[str] = Field(
        default=None,
        description="Burned-in timecode if readable. A RUNNING clock over a "
                    "black picture means the encoder is alive and the SOURCE "
                    "is dead; a STOPPED clock means the source froze. This is "
                    "the field that separates those two faults.")

    # Decided in code, never by vision.
    rung_resolution_verdict: Literal[
        "carries_expected_detail", "suspect_upscaled", "inconclusive",
        "not_checked",
    ] = "not_checked"
    rung_resolution_ratio: Optional[float] = None
    rung_resolution_detail: str = ""

    contradicts_telemetry: bool = Field(
        default=False,
        description="True when the pixels disagree with the delivery metrics -- "
                    "the every-dashboard-green-and-the-screen-is-black case.")
    visual_summary: str
    suspected_fault: FaultLabel = "unknown"


class Diagnosis(BaseModel):
    """Phase 3 output: which fault, and the evidence that decided it.

    The deterministic checklist in signatures.py computes the verdict; this
    object records it alongside the model's ranking and explanation. When the
    two disagree the disagreement is recorded rather than resolved silently --
    an agent that quietly overrules its own evidence is worse than one that
    admits confusion.
    """

    fault: FaultLabel
    diagnosis: str = Field(
        description="What is actually wrong, in an operator's language.")

    deterministic_verdict: FaultLabel = Field(
        description="Verdict computed by the signature checklist. Authoritative.")
    verdict_confidence: Literal["high", "medium", "low"] = "low"
    model_agrees_with_evidence: bool = Field(
        default=True,
        description="False when the model's ranking disagrees with the "
                    "deterministic verdict. The verdict still stands.")
    disagreement_note: str = ""

    confirming_evidence: list[str] = Field(
        default_factory=list,
        description="Checks that passed, quoted with their measured values.")
    ruled_out: list[str] = Field(
        default_factory=list,
        description="Faults eliminated, each with the check that eliminated it.")
    unconfirmable: list[str] = Field(
        default_factory=list,
        description="Faults that could not be confirmed OR eliminated, and why. "
                    "Never fold these into ruled_out.")

    fourxx_status: Literal["none", "ongoing", "stopped", "unknown"] = "unknown"
    discriminator_note: str = Field(
        default="",
        description="How ladder_collapse was separated from segment_gap, when "
                    "both were in play. Presence of 404s does not separate "
                    "them; persistence does.")

    blast_radius: Literal["single_region", "multi_region", "all_regions",
                          "plant_wide", "none", "unknown"] = "unknown"
    affected_regions: list[str] = Field(default_factory=list)
    telemetry_visible: bool = Field(
        default=True,
        description="False when no delivery signal moved -- the fault was only "
                    "findable by looking at pixels.")

    operator_summary: str = Field(
        description="What an on-call engineer needs to know, in two or three "
                    "sentences.")
    recommended_action: str = Field(
        default="",
        description="The remediation to propose. Phase 4 gates it on a human.")
