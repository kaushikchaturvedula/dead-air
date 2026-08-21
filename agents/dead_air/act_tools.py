"""Phase 4/5 tools: propose, gate, execute, verify, record.

THE GATE IS STRUCTURAL
----------------------
`propose_remediation` cannot execute anything -- it has no code path that
touches the plant. Execution lives in `execute_approved_remediation`, which
refuses to run unless it is handed an explicit approval token that only a human
can produce. The separation is deliberate: an agent that can restart a
production encoder on its own judgement is a liability, and the value here is
that the decision stays with a person while the analysis does not.

RECOVERY IS MEASURED, NOT ASSUMED
---------------------------------
`verify_recovery` re-queries the SLO after the action and reports what it finds.
Performing an action is not evidence that it worked. An agent that closes an
incident because it did something is worse than one that does nothing, because
now nobody is looking.
"""

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

REGIONS = ["us-east1", "europe-west1", "asia-south1"]
ENCODER_URL = os.environ.get("DEADAIR_ENCODER_CONTROL", "http://localhost:9103")
EDGE_PORTS = {"us-east1": 8081, "europe-west1": 8082, "asia-south1": 8083}

# The only actions that exist. A model cannot invent a sixth.
ACTIONS = {
    "restart_encoder_with_healthy_source": {
        "target_kind": "encoder",
        "command": "POST /chaos {\"mode\":\"none\"} on the encoder  "
                   "# clears the injected source fault and restarts ffmpeg",
        "blast_radius": "ALL regions -- every viewer sees a brief "
                        "discontinuity while ffmpeg restarts",
        "reversible": True,
    },
    "restore_missing_rendition": {
        "target_kind": "encoder",
        "command": "POST /chaos {\"mode\":\"none\"} on the encoder  "
                   "# restores the full ladder",
        "blast_radius": "ALL regions -- the ladder is rebuilt and players "
                        "re-read the manifest",
        "reversible": True,
    },
    "clear_packager_segment_gap": {
        "target_kind": "encoder",
        "command": "POST /chaos {\"mode\":\"none\"} on the encoder  "
                   "# stops segments being deleted",
        "blast_radius": "ALL regions, but non-disruptive -- it only stops "
                        "further deletions",
        "reversible": True,
    },
    "reencode_mismatched_rung": {
        "target_kind": "encoder",
        "command": "POST /chaos {\"mode\":\"none\"} on the encoder  "
                   "# re-encodes the top rung at its true resolution",
        "blast_radius": "ALL regions -- brief discontinuity on the top rung",
        "reversible": True,
    },
    "drain_region_from_rotation": {
        "target_kind": "region",
        "command": "POST /chaos {\"mode\":\"none\"} on the affected edge  "
                   "# clears the edge fault; a real CDN would drain the POP",
        "blast_radius": "ONE region only -- other regions are untouched",
        "reversible": True,
    },
    "no_action_required": {
        "target_kind": "none",
        "command": "(none)",
        "blast_radius": "nothing",
        "reversible": True,
    },
}

FAULT_TO_ACTION = {
    "black_source": "restart_encoder_with_healthy_source",
    "ladder_collapse": "restore_missing_rendition",
    "segment_gap": "clear_packager_segment_gap",
    "ladder_mismatch": "reencode_mismatched_rung",
    "edge_latency": "drain_region_from_rotation",
    "no_fault_detected": "no_action_required",
}


def _grafana(method, path, body=None, params=None, timeout=45):
    base = os.environ["GRAFANA_URL"].rstrip("/")
    token = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _post(url, payload, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


# --- Phase 4 ---------------------------------------------------------------

def propose_remediation(fault: str, region: str) -> dict:
    """Look up the single remediation for a diagnosed fault. PROPOSES ONLY.

    This tool cannot change the plant. It returns the one action that
    corresponds to the fault, with its blast radius and risk, for a human to
    approve or reject. There is deliberately exactly one action per fault and no
    way to express a sequence -- an agent proposing three steps is an agent
    asking a human to approve something they cannot reason about at a glance.

    Args:
        fault: the diagnosed fault from Phase 3.
        region: the affected region, for region-scoped actions.

    Returns:
        dict describing the proposed action, its command, and its risks.
    """
    action_id = FAULT_TO_ACTION.get(fault)
    if action_id is None:
        return {
            "action_id": "no_action_required",
            "error": f"no remediation is defined for fault {fault!r}",
            "requires_human_approval": True,
            "approval_status": "pending",
        }
    spec = ACTIONS[action_id]
    target = region if spec["target_kind"] == "region" else spec["target_kind"]
    return {
        "action_id": action_id,
        "target": target,
        "command": spec["command"],
        "blast_radius": spec["blast_radius"],
        "reversible": spec["reversible"],
        "requires_human_approval": True,
        "approval_status": "pending",
        "note": ("This is a proposal. Nothing has been executed. Execution "
                 "requires a separate call carrying a human approval token."),
    }


def execute_approved_remediation(action_id: str, target: str,
                                 approval_token: str) -> dict:
    """Execute a remediation. REFUSES without a valid human approval token.

    The token is issued by the human-facing gate, never by the model. If the
    model fabricates one, this returns a refusal rather than acting -- which is
    the point of putting the check here rather than in a prompt.

    Args:
        action_id: the action a human approved.
        target: region for region-scoped actions, otherwise the encoder.
        approval_token: token issued by the approval gate.

    Returns:
        dict with the execution result, or a refusal.
    """
    expected = os.environ.get("DEADAIR_APPROVAL_TOKEN", "")
    if not expected or approval_token != expected:
        return {
            "executed": False,
            "refused": True,
            "reason": ("no valid human approval token. This action was NOT "
                       "performed. A human must approve it through the gate; "
                       "the model cannot self-authorise."),
        }
    if action_id not in ACTIONS or action_id == "no_action_required":
        return {"executed": False, "refused": True,
                "reason": f"unknown or no-op action {action_id!r}"}

    spec = ACTIONS[action_id]
    try:
        if spec["target_kind"] == "region":
            port = EDGE_PORTS.get(target)
            if not port:
                return {"executed": False, "refused": True,
                        "reason": f"unknown region {target!r}"}
            _post(f"http://localhost:{port}/chaos",
                  {"mode": "none", "severity": 0})
            where = f"edge {target}"
        else:
            _post(f"{ENCODER_URL}/chaos", {"mode": "none", "severity": 0})
            where = "encoder"
    except Exception as exc:                            # noqa: BLE001
        return {"executed": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "executed": True,
        "action_id": action_id,
        "applied_to": where,
        "executed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Recovery is NOT established by this result. Phase 5 must "
                "re-query the SLO to confirm.",
    }


# --- Phase 5 ---------------------------------------------------------------

def verify_recovery(region: str, wait_seconds: int = 60) -> dict:
    """Re-query the SLO after a remediation to see whether it actually worked.

    Waits, then measures. Performing an action is not evidence that it worked,
    and this is the only thing entitled to say an incident has recovered.

    Args:
        region: region to check, or "all".
        wait_seconds: how long to wait before measuring, so the fleet's sliding
            rebuffer window reflects the post-action state rather than the
            incident.

    Returns:
        dict with per-region SLO values and whether the threshold is met.
    """
    from .diagnose_tools import Q_REBUFFER, _clean, _promql
    from .signatures import REBUFFER_ALERT_THRESHOLD

    wait_seconds = max(0, min(int(wait_seconds), 300))
    if wait_seconds:
        time.sleep(wait_seconds)

    values = _clean(_promql(Q_REBUFFER))
    if not values:
        return {"recovered": False, "error": "SLO query returned nothing",
                "slo_name": "rebuffer_ratio"}

    scope = REGIONS if region in ("all", "", None) else [region]
    checked = {r: round(values.get(r, 0.0), 4) for r in scope if r in values}
    breaching = {r: v for r, v in checked.items()
                 if v > REBUFFER_ALERT_THRESHOLD}
    return {
        "slo_name": "rebuffer_ratio",
        "slo_threshold": REBUFFER_ALERT_THRESHOLD,
        "waited_seconds": wait_seconds,
        "values_by_region": checked,
        "all_regions": {k: round(v, 4) for k, v in values.items()},
        "breaching_regions": breaching,
        "recovered": not breaching,
        "note": ("recovered=false means the incident STAYS OPEN. Do not close "
                 "it and do not report success."),
    }


def verify_visual_recovery(region: str, rendition: str = "1080p",
                           wait_seconds: int = 30) -> dict:
    """Re-inspect the PICTURE to confirm a content fault is actually repaired.

    Required for black_source and ladder_mismatch. Those faults never moved
    rebuffer_ratio in the first place, so checking a delivery SLO after
    repairing one would report "recovered" against a stream that is still
    black -- the precise blindness this whole system exists to expose. Verifying
    a content fault with a delivery metric would be the system failing at its
    own thesis.

    Args:
        region: region whose edge to fetch from.
        rendition: ladder rung to inspect.
        wait_seconds: settle time before looking, so the encoder has produced
            fresh segments after a restart.

    Returns:
        dict with the frame verdict, the rung measurement, and whether the
        picture is genuinely healthy again.
    """
    from .video_tools import check_rung_resolution, inspect_frame

    wait_seconds = max(0, min(int(wait_seconds), 180))
    if wait_seconds:
        time.sleep(wait_seconds)

    frame = inspect_frame(region, rendition)
    rung = check_rung_resolution(region)
    verdict = frame.get("classification", "no_frame_available")
    rung_verdict = rung.get("verdict", "inconclusive")

    picture_ok = verdict == "healthy"
    rung_ok = rung_verdict == "carries_expected_detail"
    return {
        "check": "visual_frame_check",
        "region": region,
        "rendition": rendition,
        "waited_seconds": wait_seconds,
        "frame_verdict": verdict,
        "vision_confidence": frame.get("confidence"),
        "timecode_value": frame.get("timecode_value"),
        "visual_evidence": frame.get("visual_evidence", "")[:300],
        "rung_resolution_verdict": rung_verdict,
        "rung_resolution_ratio": rung.get("ratio"),
        "recovered": bool(picture_ok and rung_ok),
        "note": ("recovered=false means viewers still are not seeing a correct "
                 "picture, regardless of what delivery metrics say. The "
                 "incident stays OPEN."),
    }


def annotate_dashboard(text: str, region: str, fault: str,
                       started_at_epoch: int = 0) -> dict:
    """Annotate the Grafana dashboard at the incident's timestamp.

    The annotation is placed at when the incident STARTED, not when the agent
    finished, so it lands where an operator scrubbing the dashboard would look
    for it.

    Args:
        text: annotation body, markdown allowed.
        region: affected region, becomes a tag.
        fault: diagnosed fault, becomes a tag.
        started_at_epoch: incident start, epoch SECONDS. 0 means one hour ago.

    Returns:
        dict with the created annotation id.
    """
    now_ms = int(time.time() * 1000)
    start_ms = int(started_at_epoch * 1000) if started_at_epoch else now_ms - 3600_000
    if start_ms > now_ms:
        start_ms = now_ms
    body = {
        "dashboardUID": "deadair-plant",
        "time": start_ms,
        "timeEnd": now_ms,
        "tags": ["dead-air", "agent", f"fault:{fault}", f"region:{region}"],
        "text": text[:4000],
    }
    try:
        res = _grafana("POST", "/api/annotations", body)
        return {"created": True, "annotation_id": str(res.get("id", "")),
                "range_start_ms": start_ms, "range_end_ms": now_ms}
    except Exception as exc:                            # noqa: BLE001
        return {"created": False,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def record_incident(title: str, summary: str, severity: str = "minor",
                    status: str = "resolved",
                    started_at_epoch: int = 0) -> dict:
    """File or update an incident record for this event.

    Tries Grafana IRM first. IRM is not enabled on every stack, and a missing
    incident backend is not a reason to lose the record -- so on failure this
    falls back to a dashboard annotation tagged as an incident and says which
    path it took, rather than silently reporting success.

    Args:
        title: incident title.
        summary: what happened and what was done.
        severity: minor | major | critical.
        status: active | resolved.
        started_at_epoch: incident start in epoch SECONDS, so the fallback
            annotation lands at the incident rather than an hour ago.

    Returns:
        dict describing where the record was written.
    """
    try:
        res = _grafana("POST",
                       "/api/plugins/grafana-irm-app/resources/api/v1/incidents",
                       {"title": title[:200], "severity": severity,
                        "status": status, "summary": summary[:4000]})
        iid = str(res.get("incidentID") or res.get("id") or "")
        if iid:
            return {"backend": "grafana-irm", "incident_id": iid,
                    "created": True}
    except Exception as exc:                            # noqa: BLE001
        irm_error = f"{type(exc).__name__}: {str(exc)[:160]}"
    else:
        irm_error = "IRM returned no incident id"

    fallback = annotate_dashboard(
        f"**INCIDENT** {title}\n\n{summary}", region="all", fault="incident",
        started_at_epoch=started_at_epoch)
    return {
        "backend": "annotation-fallback",
        "created": bool(fallback.get("created")),
        "annotation_id": fallback.get("annotation_id", ""),
        "irm_unavailable": irm_error,
        "note": "IRM was unavailable; the record was written as a tagged "
                "dashboard annotation instead. This is stated rather than "
                "hidden so the incident trail is not silently lost.",
    }


def estimate_viewer_impact(impact_ratio: float, affected_regions: int,
                           duration_seconds: float) -> dict:
    """Estimate viewer-minutes lost. An ESTIMATE, with its assumptions stated.

    CHOOSING impact_ratio IS THE WHOLE JUDGEMENT, and getting it wrong produces
    the most misleading number this system can emit:

      delivery faults (edge_latency, segment_gap, ladder_collapse)
          use the peak rebuffer_ratio -- viewers lost that FRACTION of their
          viewing time.

      content faults (black_source, ladder_mismatch)
          use 1.0. rebuffer_ratio was 0.0 throughout, because nothing stalled --
          the bytes arrived perfectly and carried the wrong picture. Passing
          that 0.0 here reports "0.0 viewer-minutes lost" for a total blackout,
          which is precisely backwards: EVERY viewer lost the picture for the
          ENTIRE incident.

    Args:
        impact_ratio: fraction of viewing time lost, 0.0-1.0. See above.
        affected_regions: how many regions were degraded.
        duration_seconds: detection to recovery.

    Returns:
        dict with the estimate and the assumptions behind it.
    """
    rebuffer_ratio = impact_ratio
    sessions_per_region = int(os.environ.get("CLIENTS_PER_REGION", "67"))
    sessions = sessions_per_region * max(0, affected_regions)
    minutes = max(0.0, duration_seconds) / 60.0
    lost = sessions * minutes * max(0.0, min(rebuffer_ratio, 1.0))
    return {
        "viewer_minutes_lost": round(lost, 1),
        "sessions_affected": sessions,
        "incident_minutes": round(minutes, 2),
        "impact_ratio_used": rebuffer_ratio,
        "assumptions": [
            f"{sessions_per_region} sessions per region (the modeled fleet, "
            "not real viewers)",
            "the impact ratio is applied across the whole incident, which "
            "OVERSTATES loss if the fault ramped in gradually",
            "a rebuffering viewer is treated as fully lost for that fraction "
            "of time",
        ],
        "caveat": "An estimate from a modeled fleet. Useful for relative "
                  "comparison between incidents, not as an absolute business "
                  "figure.",
    }
