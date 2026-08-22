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
        # The vision call exceeded its hard deadline and was abandoned. Distinct
        # from no_frame_available (the rendition does not exist) because one is
        # a fact about the model and the other about the plant. Both are treated
        # as "we did not look" by the checklist; conflating them would let a
        # Vertex stall eliminate a fault.
        "vision_unavailable",
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

    # A diagnosis has to be able to say what it could not see. Without this the
    # output of a run where the viewer fleet was dead and half the queries
    # failed is indistinguishable from a run where everything was measured and
    # the plant was fine -- and the second reading is the one a reader defaults
    # to. Copied from match_fault_signatures' blind_spots; never invented.
    blind_spots: list[str] = Field(
        default_factory=list,
        description="Signals the checklist could NOT observe: exporters "
                    "publishing nothing, failed queries, an unfetchable "
                    "manifest. Copy from the tool's blind_spots verbatim. An "
                    "empty list is a claim that everything was visible, so do "
                    "not empty it to tidy the output.")
    evidence_complete: bool = Field(
        default=True,
        description="False when blind_spots is non-empty. A verdict reached "
                    "with blind spots is provisional regardless of its "
                    "confidence.")

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


class RemediationProposal(BaseModel):
    """Phase 4 output: exactly ONE remediation, proposed and not executed.

    The gate is structural, not advisory. Nothing in this object executes
    anything; it is a request for permission that a human answers. An agent that
    can restart a production encoder on its own judgement is a liability, and
    the whole value of this phase is that the decision to act stays with a
    person while the analysis behind it does not.
    """

    action_id: Literal[
        "restart_encoder_with_healthy_source",
        "restore_missing_rendition",
        "drain_region_from_rotation",
        "clear_packager_segment_gap",
        "reencode_mismatched_rung",
        "no_action_required",
    ] = Field(description="Exactly one action. Never a list.")

    human_summary: str = Field(
        description="What will happen, in one sentence an on-call engineer can "
                    "approve or reject without reading the rest.")
    target: str = Field(
        default="",
        description="What the action operates on -- a region, a rendition, or "
                    "the encoder.")
    command: str = Field(
        default="",
        description="The exact command that would run, so a human can inspect "
                    "it before approving. It is NOT run by this phase.")

    justification: str = Field(
        description="Why this action follows from the diagnosis, citing the "
                    "evidence that decided it.")
    expected_effect: str = Field(
        description="What should change if this works, in terms of a metric "
                    "that Phase 5 can then verify.")
    slo_to_verify: Literal["rebuffer_ratio", "visual_frame_check",
                           "both"] = Field(
        default="both",
        description="How Phase 5 confirms recovery. CONTENT faults "
                    "(black_source, ladder_mismatch) MUST use "
                    "visual_frame_check: rebuffer_ratio never moved for them, "
                    "so it would report recovery on a still-black stream. "
                    "Delivery faults use rebuffer_ratio. The default is BOTH: "
                    "if the model omits this field, over-verifying costs a few "
                    "seconds, while defaulting to rebuffer_ratio would fall "
                    "back to the one metric that is blind to content faults.")

    blast_radius: str = Field(
        description="What else this touches. An encoder restart interrupts "
                    "EVERY region, which is a materially different decision "
                    "from draining one edge.")
    reversible: bool = Field(
        default=True, description="Whether the action can be undone.")
    risk_if_wrong: str = Field(
        description="What happens if the diagnosis was wrong and this runs "
                    "anyway. This is the sentence the human is really "
                    "approving against.")

    requires_human_approval: bool = Field(
        default=True,
        description="Always true. Present so the gate is visible in the "
                    "artifact rather than implied by control flow.")
    approval_status: Literal["pending", "approved", "rejected", "auto_skipped"] = "pending"


class RecoveryRecord(BaseModel):
    """Phase 5 output: did it actually recover, and the postmortem.

    An agent that declares victory without re-querying is worse than one that
    does nothing, because it closes the incident. Recovery is asserted only
    from a fresh measurement taken after the action.
    """

    action_taken: str
    action_executed: bool = Field(
        default=False,
        description="False when the human rejected, or when the run was a "
                    "dry run. Everything below is still recorded.")

    slo_name: str = "rebuffer_ratio"
    slo_threshold: float = 0.02
    slo_before: Optional[float] = None
    slo_after: Optional[float] = None
    recovered: bool = Field(
        default=False,
        description="True only when a re-query after the action shows the SLO "
                    "back within threshold. Never inferred from the action "
                    "having been performed.")
    verification_attempts: int = 0
    still_degraded_reason: str = Field(
        default="",
        description="If not recovered, what the re-query actually showed. The "
                    "incident stays OPEN.")

    annotation_created: bool = False
    annotation_id: str = ""
    incident_id: str = Field(
        default="", description="Incident record id, if one was written.")
    incident_backend: Literal["grafana-irm", "annotation-fallback", "none"] = (
        Field(default="none",
              description="WHERE the incident was recorded. IRM is not enabled "
                          "on every stack; when it is unavailable the record "
                          "falls back to a tagged annotation. Reporting an id "
                          "without saying which backend produced it implies an "
                          "IRM incident exists when it may not."))

    # The postmortem
    timeline: list[str] = Field(
        default_factory=list,
        description="Timestamped sequence from detection to resolution.")
    evidence: list[str] = Field(
        default_factory=list,
        description="The measurements that established the diagnosis.")
    root_cause: str = ""
    mttr_seconds: Optional[float] = Field(
        default=None,
        description="Detection to verified recovery. Not to action -- an action "
                    "that did not work has not repaired anything.")
    viewer_minutes_lost: Optional[float] = Field(
        default=None,
        description="Estimated. rebuffer_ratio x affected sessions x duration. "
                    "State the assumptions; this is an estimate, not a "
                    "measurement.")
    postmortem: str = Field(
        default="", description="The written postmortem, markdown.")
