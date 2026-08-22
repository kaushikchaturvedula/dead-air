"""Phase 3 signature library: deterministic evidence checklists per fault.

Brief §5's fault menu, encoded as checks that CODE evaluates. The division of
labour in this phase is deliberate:

    Gemini   ranks which hypotheses are worth testing, and explains the result
    code     runs the confirming checks and computes the verdict
    evidence decides

The checklists must reproduce the table measured by scripts/fault_signatures.py
against the live plant. That harness is the ground truth; if these predicates
and that table ever disagree, the table is right and this file is wrong.

Measured signatures (2026-08-19, docs/plant.md):

    fault            rebuffer          bitrate            4xx
    edge_latency     0.44, ONE region  drops, that region none
    segment_gap      ~0.05, all        unchanged          SUSTAINED, all
    ladder_collapse  ZERO              3.6->2.0 Mbps all  transient, then STOPS
    black_source     zero              unchanged          none
    ladder_mismatch  zero              unchanged          none

The last two are invisible in telemetry and can only be confirmed from Phase 2.

THE 404 DISCRIMINATOR
---------------------
ladder_collapse and segment_gap both produce 404s, which is the trap documented
in docs/plant.md. What separates them is PERSISTENCE, not presence:

    segment_gap      404s continue indefinitely -- segments keep being deleted
    ladder_collapse  404s stop once players re-read the master manifest and
                     settle onto the rungs that still exist

So the check compares a recent window against an earlier one, and an agent that
merely asks "are there 404s" gets this wrong.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

REBUFFER_ALERT_THRESHOLD = 0.02
# Healthy plant sits at the 5 Mbps top rung; ladder_collapse pulls the fleet
# average to ~2.0 Mbps and edge_latency to ~0.8 Mbps in the degraded region.
#
# Deliberately set well below the healthy value rather than just under it. ABR
# recovery is gradual: after a fault clears, players step back UP the ladder over
# a minute or two, so a genuinely healthy plant can legitimately read ~4.1 Mbps
# while it recovers. A threshold at 4.5M scored that as degraded and reported a
# healthy plant as faulty. 3.0M sits in the empty space between recovering-
# healthy (4.1-5.0) and actually-degraded (0.8-2.0).
BITRATE_DEGRADED_BPS = 3_000_000
TTFB_ELEVATED_SECONDS = 0.100
RATE_PRESENT = 0.005          # req/s above which a 4xx rate counts as real
LAG_STALE_SECONDS = 20.0


@dataclass
class Check:
    """One deterministic predicate against collected evidence."""

    description: str
    predicate: Callable[[dict], Optional[bool]]
    required: bool = False

    def evaluate(self, ev: dict) -> dict:
        try:
            result = self.predicate(ev)
        except Exception as exc:                      # noqa: BLE001
            return {"check": self.description, "required": self.required,
                    "result": "error", "detail": f"{type(exc).__name__}: {exc}"}
        if result is None:
            # Not evaluated is NOT the same as failed -- typically the visual
            # evidence this check needs was never gathered.
            return {"check": self.description, "required": self.required,
                    "result": "not_evaluated"}
        return {"check": self.description, "required": self.required,
                "result": "pass" if result else "fail"}


@dataclass
class Signature:
    fault: str
    diagnosis: str
    checks: list[Check] = field(default_factory=list)

    def evaluate(self, ev: dict) -> dict:
        results = [c.evaluate(ev) for c in self.checks]
        required = [r for r in results if r["required"]]
        supporting = [r for r in results if not r["required"]]

        required_failed = [r for r in required if r["result"] == "fail"]
        required_unknown = [r for r in required if r["result"] in
                            ("not_evaluated", "error")]
        required_passed = [r for r in required if r["result"] == "pass"]
        supporting_passed = [r for r in supporting if r["result"] == "pass"]

        if required_failed:
            status = "ruled_out"
        elif required_unknown:
            status = "unconfirmable"
        elif required_passed:
            status = "confirmed"
        else:
            status = "unconfirmable"

        return {
            "fault": self.fault,
            "diagnosis": self.diagnosis,
            "status": status,
            "required_passed": len(required_passed),
            "required_total": len(required),
            "supporting_passed": len(supporting_passed),
            "supporting_total": len(supporting),
            "failed_checks": [r["check"] for r in required_failed],
            "unevaluated_checks": [r["check"] for r in required_unknown],
            "checks": results,
        }


# --- predicate helpers -----------------------------------------------------

def _rebuffering_regions(ev):
    return [r for r, v in (ev.get("rebuffer_by_region") or {}).items()
            if v > REBUFFER_ALERT_THRESHOLD]


def _bitrate_degraded_regions(ev):
    return [r for r, v in (ev.get("bitrate_by_region") or {}).items()
            if v and v < BITRATE_DEGRADED_BPS]


def _ttfb_elevated_regions(ev):
    return [r for r, v in (ev.get("ttfb_p95_by_region") or {}).items()
            if v and v > TTFB_ELEVATED_SECONDS]


# Values that mean "we did not actually look", as opposed to "we looked and saw
# this". Treating them as observations is how a skipped Phase 2 turns into a
# false elimination.
# "vision_unavailable" means the model stalled past its deadline and the call
# was abandoned. That is a fact about Vertex, not about the picture, so it must
# route to not_evaluated -- left out of this set it would read as an observation
# and RULE OUT black_source on the strength of a timeout.
_NO_FRAME_EVIDENCE = {None, "", "no_frame_available", "vision_unavailable"}
_NO_RUNG_EVIDENCE = {None, "", "not_checked", "inconclusive"}


# --- telemetry presence ----------------------------------------------------
# The same discipline as _frame_evidence/_rung_evidence below, applied to
# METRICS. It was only ever applied to visual evidence, and telemetry absence
# went on being scored as an affirmative observation of health.
#
# `sources_reporting` is three-valued: True (exporter publishing), False
# (queried fine, exporter has no series), None (the query itself failed). Only
# True licenses reading an empty condition as "the condition is absent".

def _source(ev, name):
    return (ev.get("sources_reporting") or {}).get(name)


def _if_seen(ev, source, verdict):
    """`verdict()` if that exporter is reporting, otherwise not_evaluated.

    Returning None routes the check to 'not_evaluated', which makes a signature
    'unconfirmable' rather than confirmed or ruled out -- so missing telemetry
    can never be the thing that decides a diagnosis.
    """
    return verdict() if _source(ev, source) is True else None


def _ladder_known(ev):
    """True only when the master manifest was actually fetched."""
    if ev.get("manifest_fetched") is False:
        return None
    return ev.get("manifest_rungs") is not None or ev.get("missing_rungs") is not None


def _visual(ev, key):
    v = ev.get("visual") or {}
    return v.get(key)


def _frame_evidence(ev):
    """The frame verdict, or None when no frame was actually inspected."""
    val = _visual(ev, "frame_verdict")
    return None if val in _NO_FRAME_EVIDENCE else val


def _rung_evidence(ev):
    """The rung measurement, or None when it was never taken."""
    val = _visual(ev, "rung_resolution_verdict")
    return None if val in _NO_RUNG_EVIDENCE else val


# --- the library -----------------------------------------------------------

SIGNATURES = [
    Signature(
        fault="edge_latency",
        diagnosis="CDN edge degradation in a single region",
        checks=[
            Check("rebuffering or elevated TTFB confined to exactly one region",
                  lambda ev: len(set(_rebuffering_regions(ev))
                                 | set(_ttfb_elevated_regions(ev))) == 1,
                  required=True),
            Check("at least one other region confirmed healthy",
                  lambda ev: len(ev.get("regions_seen") or []) -
                             len(set(_rebuffering_regions(ev))
                                 | set(_ttfb_elevated_regions(ev))) >= 1,
                  required=True),
            Check("p95 segment TTFB elevated in the affected region",
                  lambda ev: len(_ttfb_elevated_regions(ev)) >= 1),
            Check("delivered bitrate degraded only in the affected region",
                  lambda ev: 0 < len(_bitrate_degraded_regions(ev)) <
                             len(ev.get("regions_seen") or [1, 2, 3])),
            Check("no sustained 4xx anywhere",
                  lambda ev: _if_seen(ev, "edges",
                      lambda: ev.get("fourxx_status") in ("none", "stopped"))),
            Check("encoder healthy and producing the full ladder",
                  lambda ev: (None if ev.get("encoder_up") is None
                              or ev.get("rungs_active") is None
                              else bool(ev.get("encoder_up"))
                              and ev.get("rungs_active", 0) >= 4)),
        ],
    ),
    Signature(
        fault="segment_gap",
        diagnosis="packager fault -- segments missing at the origin",
        checks=[
            Check("4xx are ONGOING, not a burst that stopped",
                  lambda ev: _if_seen(ev, "edges",
                      lambda: ev.get("fourxx_status") == "ongoing"),
                  required=True),
            Check("4xx present in more than one region (origin-side, not one edge)",
                  lambda ev: len(ev.get("fourxx_regions") or []) > 1,
                  required=True),
            Check("delivered bitrate broadly unchanged",
                  lambda ev: len(_bitrate_degraded_regions(ev)) == 0),
            Check("encoder healthy and producing the full ladder",
                  lambda ev: (None if ev.get("encoder_up") is None
                              or ev.get("rungs_active") is None
                              else bool(ev.get("encoder_up"))
                              and ev.get("rungs_active", 0) >= 4)),
            Check("master manifest still advertises the full ladder",
                  lambda ev: (None if not _ladder_known(ev)
                              else not (ev.get("missing_rungs") or []))),
        ],
    ),
    Signature(
        fault="ladder_collapse",
        diagnosis="encoder rung failure -- a rendition is no longer produced",
        checks=[
            Check("a rung is missing from the master manifest",
                  lambda ev: (None if not _ladder_known(ev)
                              else bool(ev.get("missing_rungs"))),
                  required=True),
            Check("4xx are NOT ongoing (any burst has stopped)",
                  lambda ev: _if_seen(ev, "edges",
                      lambda: ev.get("fourxx_status") in ("none", "stopped")),
                  required=True),
            Check("delivered bitrate dropped across ALL regions",
                  lambda ev: len(_bitrate_degraded_regions(ev)) ==
                             len(ev.get("regions_seen") or [])
                             and bool(ev.get("regions_seen"))),
            Check("little or no rebuffering -- players downshifted cleanly",
                  lambda ev: len(_rebuffering_regions(ev)) == 0),
            Check("packager reports the missing rung as stale",
                  lambda ev: any(v > LAG_STALE_SECONDS for v in
                                 (ev.get("segment_lag_by_rendition") or {}).values())),
            Check("encoder reports fewer active rungs than the full ladder",
                  lambda ev: (None if ev.get("rungs_active") is None
                              else ev.get("rungs_active") < 4)),
        ],
    ),
    Signature(
        fault="black_source",
        diagnosis="source blackout -- the encoder is healthy but the picture is gone",
        checks=[
            Check("frame inspection reports a black picture",
                  lambda ev: (_frame_evidence(ev) == "black_frame"
                              if _frame_evidence(ev) is not None else None),
                  required=True),
            Check("burned-in timecode still legible (encoder alive, source dead)",
                  lambda ev: (bool(_visual(ev, "timecode_legible"))
                              if _frame_evidence(ev) is not None else None)),
            # Three absence claims in one predicate, so it needs BOTH
            # exporters. This is a supporting check on black_source and
            # ladder_mismatch -- the two content faults -- so it passing
            # because the fleet is dead is exactly how a blind agent would
            # corroborate the fault it is most confident about.
            Check("delivery metrics show no degradation at all",
                  lambda ev: (None if _source(ev, "viewers") is not True
                              or _source(ev, "edges") is not True
                              else len(_rebuffering_regions(ev)) == 0
                              and len(_bitrate_degraded_regions(ev)) == 0
                              and ev.get("fourxx_status") == "none")),
            Check("encoder healthy and producing the full ladder",
                  lambda ev: (None if ev.get("encoder_up") is None
                              or ev.get("rungs_active") is None
                              else bool(ev.get("encoder_up"))
                              and ev.get("rungs_active", 0) >= 4)),
        ],
    ),
    Signature(
        fault="ladder_mismatch",
        diagnosis="packager mux-up -- a rung carries lower-resolution content",
        checks=[
            # Deterministic measurement only. Vision cannot see this fault and is
            # never consulted for it (docs/vision-spike.md).
            Check("rung resolution measurement reports the top rung as upscaled",
                  lambda ev: (_rung_evidence(ev) == "suspect_upscaled"
                              if _rung_evidence(ev) is not None else None),
                  required=True),
            Check("frame itself looks healthy -- the fault is invisible to vision",
                  lambda ev: (_frame_evidence(ev) == "healthy"
                              if _frame_evidence(ev) is not None else None)),
            # Three absence claims in one predicate, so it needs BOTH
            # exporters. This is a supporting check on black_source and
            # ladder_mismatch -- the two content faults -- so it passing
            # because the fleet is dead is exactly how a blind agent would
            # corroborate the fault it is most confident about.
            Check("delivery metrics show no degradation at all",
                  lambda ev: (None if _source(ev, "viewers") is not True
                              or _source(ev, "edges") is not True
                              else len(_rebuffering_regions(ev)) == 0
                              and len(_bitrate_degraded_regions(ev)) == 0
                              and ev.get("fourxx_status") == "none")),
            Check("master manifest still advertises the full ladder",
                  lambda ev: (None if not _ladder_known(ev)
                              else not (ev.get("missing_rungs") or []))),
        ],
    ),
]

HEALTHY = Signature(
    fault="no_fault_detected",
    diagnosis="plant healthy -- no fault signature matches",
    checks=[
        # EVERY ONE OF THESE IS AN ABSENCE CLAIM, so every one of them is gated
        # on the exporter that would have shown the presence. Ungated, a dead
        # viewer fleet made the first two pass on `{}`, a dead edge made the
        # third pass via fourxx_status "none", and a failed manifest fetch made
        # the fourth pass via `not (None or [])` -- four required checks
        # confirming health from four different kinds of blindness.
        Check("no region rebuffering above the alert threshold",
              lambda ev: _if_seen(ev, "viewers",
                                  lambda: len(_rebuffering_regions(ev)) == 0),
              required=True),
        Check("no region with degraded delivered bitrate",
              lambda ev: _if_seen(ev, "viewers",
                                  lambda: len(_bitrate_degraded_regions(ev)) == 0),
              required=True),
        Check("no 4xx activity",
              lambda ev: _if_seen(ev, "edges",
                                  lambda: ev.get("fourxx_status") == "none"),
              required=True),
        Check("full ladder advertised",
              lambda ev: (None if not _ladder_known(ev)
                          else not (ev.get("missing_rungs") or [])),
              required=True),
        Check("encoder healthy",
              lambda ev: (None if ev.get("encoder_up") is None
                          or ev.get("rungs_active") is None
                          else bool(ev.get("encoder_up"))
                          and ev.get("rungs_active", 0) >= 4),
              required=True),
        Check("frame inspection found no picture fault",
              lambda ev: (_frame_evidence(ev) == "healthy"
                          if _frame_evidence(ev) is not None else None)),
        Check("rung resolution measurement clean",
              lambda ev: (_rung_evidence(ev) == "carries_expected_detail"
                          if _rung_evidence(ev) is not None else None)),
    ],
)


def evaluate_all(evidence: dict) -> dict:
    """Run every signature against the evidence and pick a verdict.

    The verdict is computed, not argued. A fault is confirmed only when every
    one of its required checks passes; anything whose required checks are
    unavailable is 'unconfirmable' rather than ruled out, so missing evidence
    can never masquerade as an eliminated fault.
    """
    results = [s.evaluate(evidence) for s in SIGNATURES]
    healthy = HEALTHY.evaluate(evidence)

    confirmed = [r for r in results if r["status"] == "confirmed"]
    # Prefer the signature with the most corroboration when several confirm.
    confirmed.sort(key=lambda r: (r["supporting_passed"], r["required_passed"]),
                   reverse=True)

    if len(confirmed) == 1:
        verdict, confidence = confirmed[0]["fault"], "high"
    elif len(confirmed) > 1:
        top, second = confirmed[0], confirmed[1]
        if top["supporting_passed"] > second["supporting_passed"]:
            verdict, confidence = top["fault"], "medium"
        else:
            verdict, confidence = "ambiguous", "low"
    elif healthy["status"] == "confirmed":
        # HEALTHY's required checks are ALL delivery metrics, and every one of
        # them passes during a total blackout -- that is the premise of this
        # project. So "the plant is healthy" is only a high-confidence claim
        # when the picture was actually looked at. If the frame check came back
        # not_evaluated -- Phase 2 skipped, ffmpeg missing, or the vision call
        # abandoned at its deadline -- then black_source and ladder_mismatch
        # were never excludable and the verdict is provisional.
        frame_checked = any(
            c["result"] in ("pass", "fail")
            for c in healthy["checks"]
            if c["check"].startswith("frame inspection"))
        verdict = "no_fault_detected"
        confidence = "high" if frame_checked else "medium"
    else:
        verdict, confidence = "no_signature_matched", "low"

    return {
        "deterministic_verdict": verdict,
        "verdict_confidence": confidence,
        "confirmed_faults": [r["fault"] for r in confirmed],
        "ruled_out": [r["fault"] for r in results if r["status"] == "ruled_out"],
        "unconfirmable": [r["fault"] for r in results
                          if r["status"] == "unconfirmable"],
        "healthy_signature": healthy,
        "signatures": results + [healthy],
        "note": ("This verdict is computed from deterministic checks against "
                 "collected evidence. It is authoritative. A model may explain "
                 "it or flag disagreement, but must not overrule it."),
    }
