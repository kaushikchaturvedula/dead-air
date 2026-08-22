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

import datetime
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

# Fault CLASS, not fault action. Content faults move no delivery metric at all,
# so viewer impact for them cannot be derived from rebuffer_ratio -- it is 0.0
# throughout while every viewer sees nothing. This table is the single source of
# that distinction, and it is consulted by CODE so a model cannot reason its way
# to the wrong answer.
CONTENT_FAULTS = {"black_source", "ladder_mismatch"}
DELIVERY_FAULTS = {"edge_latency", "segment_gap", "ladder_collapse"}

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
        # Derived from the same CONTENT/DELIVERY table that routes Phase 5, so
        # the proposal's declared SLO and the check actually performed cannot
        # disagree. This field used to be produced by the model and read by
        # NOTHING -- one grep hit, its own declaration in schemas.py -- while
        # sitting under a docstring arguing it must not be a model decision.
        "slo_to_verify": ("visual_frame_check" if fault in CONTENT_FAULTS
                          else "rebuffer_ratio" if fault in DELIVERY_FAULTS
                          else "both"),
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
    # A region we asked about and did not get back is UNMEASURED, not recovered.
    # The `if r in values` filter silently dropped those, `breaching` derived
    # from what survived, and `recovered = not breaching` then reported success
    # for regions never looked at -- so passing a region string that is not an
    # exact label value (`all_regions`, `plant_wide`, or the literal "encoder"
    # that four of the five remediations carry as their target) produced
    # `recovered: true, values_by_region: {}` and a closed incident.
    unmeasured = [r for r in scope if r not in values]
    breaching = {r: v for r, v in checked.items()
                 if v > REBUFFER_ALERT_THRESHOLD}
    if unmeasured:
        return {
            "slo_name": "rebuffer_ratio",
            "slo_threshold": REBUFFER_ALERT_THRESHOLD,
            "waited_seconds": wait_seconds,
            "values_by_region": checked,
            "all_regions": {k: round(v, 4) for k, v in values.items()},
            "breaching_regions": breaching,
            "unmeasured_regions": unmeasured,
            "recovered": False,
            "error": (f"no rebuffer_ratio returned for {unmeasured}; asked for "
                      f"{scope!r}. Known region labels are {REGIONS}."),
            "note": ("recovered=false because these regions were NOT measured. "
                     "Unmeasured is not recovered."),
        }
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
    from .content_screen import screen_live_segment
    from .video_tools import check_rung_resolution, inspect_frame

    wait_seconds = max(0, min(int(wait_seconds), 180))
    if wait_seconds:
        time.sleep(wait_seconds)

    # Stage 0 first: "is it still black" is arithmetic, and answering it with a
    # model is both slower and less certain. Vision then says what the picture
    # IS, which is the question worth a model.
    screen = screen_live_segment(region, rendition)
    frame = inspect_frame(region, rendition)
    rung = check_rung_resolution(region)
    verdict = frame.get("classification", "no_frame_available")
    rung_verdict = rung.get("verdict", "inconclusive")

    picture_ok = verdict == "healthy"
    rung_ok = rung_verdict == "carries_expected_detail"
    screen_ok = not screen.get("suspect")
    return {
        "check": "visual_frame_check",
        "stage0_screen": {
            "suspect": screen.get("suspect"),
            "reason": screen.get("reason"),
            "yavg_mean": (screen.get("measurements") or {}).get("yavg_mean"),
        },
        "region": region,
        "rendition": rendition,
        "waited_seconds": wait_seconds,
        "frame_verdict": verdict,
        "vision_confidence": frame.get("confidence"),
        "timecode_value": frame.get("timecode_value"),
        "visual_evidence": frame.get("visual_evidence", "")[:300],
        "rung_resolution_verdict": rung_verdict,
        "rung_resolution_ratio": rung.get("ratio"),
        # All three must agree. The deterministic screen is included because a
        # model that says "healthy" against a measurably black frame should not
        # be able to close an incident on its own.
        "recovered": bool(picture_ok and rung_ok and screen_ok),
        "note": ("recovered=false means viewers still are not seeing a correct "
                 "picture, regardless of what delivery metrics say. The "
                 "incident stays OPEN."),
    }


def verify_recovery_for_diagnosis(tool_context=None) -> dict:
    """Verify recovery using the check the DIAGNOSED FAULT CLASS requires.

    THE ROUTING IS DONE HERE, IN CODE, AND TAKES NO ARGUMENTS. It used to be a
    prose table in the Phase 5 prompt ending in "if unsure, call BOTH", with
    both verifiers on the model's tool list and nothing preventing the wrong
    pick. `slo_to_verify` existed in the schema to record the choice and had
    exactly one reference in the repo -- its own declaration -- under a
    docstring arguing that this must not be a model decision.

    What the misroute costs: black_source never moves rebuffer_ratio, which
    reads ~0.0001 against a 0.02 threshold for the entire blackout. So routing
    a content fault to the delivery check returns recovered=True and closes the
    incident while the stream is still black -- the system failing at precisely
    its own thesis, in a run that otherwise looks clean. Most acute in the
    default configuration, where approval_mode is `deny` and the plant really
    is still broken at verification time.

    Args:
        tool_context: injected by ADK; supplies the diagnosis and the region.

    Returns:
        The verification result, plus which check was used and why.
    """
    fault = _diagnosed_fault(tool_context)
    regions = _affected_regions_from_state(tool_context)
    region = regions[0] if regions else "us-east1"

    if fault in CONTENT_FAULTS:
        out = verify_visual_recovery(region=region)
        chosen, why = "visual_frame_check", (
            f"{fault} is a CONTENT fault: it never moved rebuffer_ratio, so a "
            f"delivery SLO cannot show whether it is repaired.")
    elif fault in DELIVERY_FAULTS:
        out = verify_recovery(region="all", wait_seconds=60)
        chosen, why = "rebuffer_ratio", (
            f"{fault} is a DELIVERY fault: it moved rebuffer_ratio, which is "
            f"the SLO the alert fires on.")
    else:
        # Unknown or undiagnosed: run BOTH and require both. Chosen in code, so
        # it cannot be talked out of.
        #
        # no_fault_detected lands here too, and the settle wait is skipped for
        # it: there is no remediation to settle FROM, so waiting 60s only
        # confirms what was already true. Measured cost of not special-casing
        # it: the healthy case ran 284.9s against 142.0s before this dispatcher
        # existed, and the healthy path is the one a demo sits on longest.
        healthy_verdict = fault == "no_fault_detected"
        wait = 0 if healthy_verdict else 60
        vis = verify_visual_recovery(region=region,
                                     wait_seconds=0 if healthy_verdict else 30)
        dele = verify_recovery(region="all", wait_seconds=wait)
        return {
            "slo_to_verify": "both",
            "why_this_check": (
                "no fault was diagnosed, so both checks run to confirm the "
                "plant really is healthy -- with no settle wait, because there "
                "was no action to settle from."
                if healthy_verdict else
                f"fault {fault!r} is not in either class, so neither check "
                f"alone is sufficient."),
            "routed_in_code": True,
            "visual": vis, "delivery": dele,
            "recovered": bool(vis.get("recovered")) and bool(dele.get("recovered")),
        }

    out["slo_to_verify"] = chosen
    out["why_this_check"] = why
    out["diagnosed_fault"] = fault
    out["routed_in_code"] = True
    return out


def annotate_dashboard(text: str, region: str, fault: str,
                       tool_context=None) -> dict:
    """Annotate the Grafana dashboard at the incident's timestamp.

    The annotation is placed at when the incident STARTED, not when the agent
    finished, so it lands where an operator scrubbing the dashboard would look
    for it.

    Args:
        text: annotation body, markdown allowed.
        region: affected region, becomes a tag.
        fault: diagnosed fault, becomes a tag.
        tool_context: injected by ADK; supplies the real incident start.

    Returns:
        dict with the created annotation id.
    """
    # Derived, not passed. The model was previously asked for started_at_epoch
    # and cannot know it; a wrong-era value drew a multi-year band across the
    # dashboard, and the prompt asked for the guess two lines after saying "an
    # annotation in the wrong place is worse than none".
    started_at_epoch = _incident_started_epoch(tool_context)
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
                    status: str = "resolved", tool_context=None) -> dict:
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
        tool_context: injected by ADK; supplies the real incident start so the
            fallback annotation lands at the incident rather than at a guessed
            timestamp.

    Returns:
        dict describing where the record was written.
    """
    started_at_epoch = _incident_started_epoch(tool_context)
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
        tool_context=tool_context)
    return {
        "backend": "annotation-fallback",
        "created": bool(fallback.get("created")),
        "annotation_id": fallback.get("annotation_id", ""),
        "irm_unavailable": irm_error,
        "note": "IRM was unavailable; the record was written as a tagged "
                "dashboard annotation instead. This is stated rather than "
                "hidden so the incident trail is not silently lost.",
    }


def _diagnosed_fault(tool_context):
    """The fault the CHECKLIST decided, read from state. Never an argument."""
    if tool_context is None:
        return ""
    raw = tool_context.state.get("diagnosis")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    if isinstance(raw, dict):
        # deterministic_verdict is the authoritative one; `fault` may have been
        # deliberately set differently with a stated disagreement.
        return (raw.get("deterministic_verdict") or raw.get("fault") or "")
    return ""


def _incident_started_epoch(tool_context):
    """When the incident began, in epoch seconds, derived from the alert.

    THE MODEL USED TO SUPPLY THIS. It cannot know it: record_investigator
    templates only the diagnosis, the proposal and the execution result, and
    none of them carry a timestamp -- so any value it produced was invented.
    A wrong-era epoch (1.7e9 is a very common completion) drew a MULTI-YEAR
    annotation band across the dashboard, and a wrong duration scaled
    viewer_minutes_lost linearly with the guess: a ten-minute blackout guessed
    as sixty seconds reported 201 viewer-minutes instead of 2010, under a
    docstring advertising the arithmetic as code-derived.

    `alert_time` is put into state by run_agent.py on every trigger path, so
    the real value was one state read away the whole time.
    """
    if tool_context is None:
        return 0
    raw = tool_context.state.get("alert_time") or ""
    if not isinstance(raw, str) or not raw:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _incident_duration_seconds(tool_context):
    """How long the incident has been open, measured rather than guessed."""
    started = _incident_started_epoch(tool_context)
    if not started:
        return 0.0
    return max(0.0, time.time() - started)


def _affected_regions_from_state(tool_context):
    """Read the affected region list Phase 1/3 already established."""
    if tool_context is None:
        return []
    for key in ("diagnosis", "incident_scope"):
        raw = tool_context.state.get(key)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if isinstance(raw, dict):
            regions = raw.get("affected_regions")
            if regions:
                return [r for r in regions if isinstance(r, str)]
    return []


def _peak_rebuffer(regions):
    """Measure the peak rebuffer_ratio over the incident window, in code."""
    from .diagnose_tools import _clean, _promql
    scope = "|".join(regions) if regions else ".+"
    q = f'max(max_over_time(rebuffer_ratio{{region=~"{scope}"}}[20m]))'
    vals = _clean(_promql(q))
    return max(vals.values()) if vals else 0.0


def estimate_viewer_impact(fault_id: str, tool_context=None) -> dict:
    """Estimate viewer-minutes lost. Arithmetic is done in CODE.

    The model supplies only the fault. Everything else -- which regions were
    affected, and what fraction of viewing was lost -- is derived here, because
    the impact ratio is exactly the judgement a model gets backwards:

      content faults (black_source, ladder_mismatch)
          ratio 1.0. rebuffer_ratio reads 0.0 throughout, because nothing
          stalled: the bytes arrived perfectly and carried the wrong picture. A
          model reasoning "rebuffer was 0.0, so impact is 0.0" reports zero
          viewer-minutes lost for a total blackout. That is not a prompt
          problem, it is a correct-looking inference from a blind metric, so
          the answer is taken out of the model's hands.

      delivery faults
          ratio is the MEASURED peak rebuffer_ratio over the incident window,
          queried here rather than recalled.

    Args:
        fault_id: the diagnosed fault. The only input the model provides --
            and now genuinely the only one. It previously also took
            duration_seconds, which the model could not know and therefore
            invented, and viewer_minutes_lost scaled linearly with that guess:
            a ten-minute blackout guessed as sixty seconds reported 201
            viewer-minutes instead of 2010, directly beneath a docstring
            promising the arithmetic was done in code. Nothing rejected
            duration_seconds=0 either, which produced viewer_minutes_lost=0.0
            next to "every viewer lost the picture for the whole incident".
        tool_context: injected by ADK; supplies the incident clock.

    Returns:
        dict with the estimate and the assumptions behind it.
    """
    duration_seconds = _incident_duration_seconds(tool_context)
    regions = _affected_regions_from_state(tool_context)
    if fault_id in CONTENT_FAULTS:
        impact_ratio = 1.0
        basis = ("content fault: every viewer lost the picture for the whole "
                 "incident, and no delivery metric moved")
        # A content fault affects every region -- one encoder feeds them all.
        if not regions:
            regions = list(REGIONS)
    elif fault_id in DELIVERY_FAULTS:
        impact_ratio = _peak_rebuffer(regions)
        basis = (f"delivery fault: measured peak rebuffer_ratio "
                 f"{impact_ratio:.4f} over the incident window")
    else:
        impact_ratio = 0.0
        basis = f"no impact model for fault {fault_id!r}"

    sessions_per_region = int(os.environ.get("CLIENTS_PER_REGION", "67"))
    sessions = sessions_per_region * len(regions)
    minutes = max(0.0, duration_seconds) / 60.0
    lost = sessions * minutes * max(0.0, min(impact_ratio, 1.0))
    return {
        "viewer_minutes_lost": round(lost, 1),
        "sessions_affected": sessions,
        "affected_regions": regions,
        "incident_minutes": round(minutes, 2),
        "impact_ratio_used": round(impact_ratio, 4),
        "impact_basis": basis,
        "assumptions": [
            f"{sessions_per_region} sessions per region (the modeled fleet, "
            "not real viewers)",
            "region list and impact ratio derived in code from the diagnosis, "
            "not supplied by the model",
            "the impact ratio is applied across the whole incident, which "
            "OVERSTATES loss if the fault ramped in gradually",
            "a rebuffering viewer is treated as fully lost for that fraction "
            "of time",
        ],
        "caveat": "An estimate from a modeled fleet. Useful for relative "
                  "comparison between incidents, not as an absolute business "
                  "figure.",
    }
