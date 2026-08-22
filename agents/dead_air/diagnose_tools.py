"""Phase 3 tools: collect evidence deterministically, then match signatures.

Every number here is fetched by CODE running fixed PromQL against Mimir, not by
a model choosing a query. That matters for two reasons:

1. Reproducibility. The same plant state produces the same evidence every run,
   so a diagnosis can be re-derived and argued with.
2. The 404 persistence discriminator (ladder_collapse vs segment_gap) needs two
   time windows compared precisely. It is exactly the kind of query a model
   gets subtly wrong, and getting it wrong swaps one diagnosis for the other.

Queries go through the Grafana datasource proxy rather than the local exporters:
the proxy is reachable from anywhere the agent runs, so this code is unchanged
when the agent moves to Cloud Run.
"""

import json
import os
import urllib.parse
import urllib.request

from .signatures import evaluate_all

REGIONS = ["us-east1", "europe-west1", "asia-south1"]
FULL_LADDER = ["1080p", "720p", "480p", "360p"]

EDGE_ENDPOINTS = {
    "us-east1": os.environ.get("EDGE_US_EAST1", "http://localhost:8081"),
    "europe-west1": os.environ.get("EDGE_EUROPE_WEST1", "http://localhost:8082"),
    "asia-south1": os.environ.get("EDGE_ASIA_SOUTH1", "http://localhost:8083"),
}

# Rebuffer ratio derived from counters and weighted by viewing time, matching
# the alert rule rather than averaging the per-cohort gauge.
Q_REBUFFER = (
    'sum by (region) (rate(viewer_rebuffer_seconds_total[5m])) / '
    'clamp_min(sum by (region) (rate(viewer_rebuffer_seconds_total[5m])) + '
    'sum by (region) (rate(viewer_playing_seconds_total[5m])), 0.0001)'
)
Q_BITRATE = 'avg by (region) (viewer_bitrate_avg)'
Q_TTFB_P95 = ('histogram_quantile(0.95, sum by (region, le) '
              '(rate(segment_ttfb_seconds_bucket[5m])))')
# Two windows, deliberately non-overlapping: "right now" versus "a while ago".
Q_4XX_RECENT = 'sum by (region) (rate(segment_status{status="4xx"}[3m]))'
Q_4XX_EARLIER = 'sum by (region) (rate(segment_status{status="4xx"}[3m] offset 8m))'
Q_LAG = 'max by (rendition) (packager_segment_lag)'
Q_ENCODER_UP = 'max(encoder_up)'
Q_ENCODER_FPS = 'max(encoder_fps)'
Q_RUNGS = 'max(encoder_rungs_active)'
Q_DROPPED = 'max(rate(dropped_frames[5m]))'

# --- Source liveness -------------------------------------------------------
# THE PROBLEM THESE SOLVE. An empty PromQL result means two completely
# different things depending on the metric, and the checklist was reading both
# as "we looked and everything is fine":
#
#   rate(segment_status{status="4xx"}) empty  -> genuinely no 404s. An
#                                                OBSERVATION of health.
#   viewer_rebuffer_seconds_total     empty  -> the viewer fleet is gone. We
#                                                know NOTHING about rebuffering.
#
# Both produced `{}`, and `len({}) == 0` passed a required "no region is
# rebuffering" check either way, so a dead fleet during a live fault scored
# no_fault_detected at HIGH confidence.
#
# These count the exporter's own series rather than the condition. If the
# counter exists at all, the exporter is reporting and an absent condition is a
# real absence; if it does not, every check derived from it is not_evaluated.
Q_VIEWERS_PRESENT = 'count(viewer_playing_seconds_total)'
Q_EDGES_PRESENT = 'count(segment_status)'
Q_ENCODER_PRESENT = 'count(encoder_up)'


def _grafana(path, params=None, timeout=45):
    base = os.environ["GRAFANA_URL"].rstrip("/")
    token = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _promql(query):
    """Instant query -> {label_value: float} or {'': float} for scalars."""
    try:
        data = _grafana(
            "/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query",
            {"query": query})
    except Exception as exc:                          # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {str(exc)[:160]}"}
    out = {}
    for series in (data.get("data") or {}).get("result") or []:
        metric = series.get("metric") or {}
        key = metric.get("region") or metric.get("rendition") or ""
        try:
            out[key] = float(series["value"][1])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _clean(d):
    return {k: v for k, v in d.items() if k != "__error__"}


def _source_reporting(query):
    """True if the exporter is publishing, False if not, None if we cannot ask.

    The three-way return is the point. False means "queried Mimir successfully
    and this exporter has no series" -- a real observation. None means the query
    itself failed, so we do not even know that much, and any check downstream
    must come back not_evaluated rather than guessing.
    """
    raw = _promql(query)
    if "__error__" in raw:
        return None
    return bool(_clean(raw))


def _manifest_rungs(region):
    base = EDGE_ENDPOINTS.get(region, EDGE_ENDPOINTS["us-east1"])
    try:
        with urllib.request.urlopen(f"{base}/hls/master.m3u8", timeout=30) as r:
            raw = r.read().decode(errors="replace")
    except Exception:
        return None
    return [l.strip().split("/")[0] for l in raw.splitlines()
            if l.strip().endswith("index.m3u8")]


def _fourxx_status(recent, earlier):
    """The ladder_collapse vs segment_gap discriminator.

    Presence of 404s is not the signal -- both faults produce them. Persistence
    is: segment_gap keeps deleting segments, while ladder_collapse's burst dies
    out once players re-read the manifest.
    """
    from .signatures import RATE_PRESENT
    now_hot = [r for r, v in recent.items() if v > RATE_PRESENT]
    then_hot = [r for r, v in earlier.items() if v > RATE_PRESENT]
    if now_hot:
        return "ongoing", sorted(set(now_hot) | set(then_hot))
    if then_hot:
        return "stopped", sorted(then_hot)
    return "none", []


def collect_evidence(region: str = "us-east1") -> dict:
    """Gather every deterministic signal a diagnosis could turn on.

    Runs fixed PromQL against Mimir plus a manifest fetch. No model involved.

    Args:
        region: region whose edge to read the master manifest from.

    Returns:
        dict of evidence, ready for match_fault_signatures.
    """
    rebuffer = _promql(Q_REBUFFER)
    bitrate = _promql(Q_BITRATE)
    ttfb = _promql(Q_TTFB_P95)
    recent = _promql(Q_4XX_RECENT)
    earlier = _promql(Q_4XX_EARLIER)
    lag = _promql(Q_LAG)

    errors = [d["__error__"] for d in (rebuffer, bitrate, ttfb, recent, earlier, lag)
              if "__error__" in d]

    rebuffer, bitrate, ttfb = _clean(rebuffer), _clean(bitrate), _clean(ttfb)
    recent, earlier, lag = _clean(recent), _clean(earlier), _clean(lag)

    status, hot_regions = _fourxx_status(recent, earlier)
    rungs = _manifest_rungs(region)

    def scalar(q, name):
        """A single value, or None -- never a default that reads as a reading.

        This used to take `default=0` and return it on any failure, so a
        transport error or a metric that had not landed in Mimir yet became
        `encoder_up=0`, indistinguishable from an encoder that is genuinely
        down. The failure was recorded nowhere: `_clean` strips `__error__`,
        and unlike the six queries above it, a scalar failure never reached
        `query_errors`. It presented as the agent reasoning correctly about a
        broken plant rather than as the agent being blind.
        """
        raw = _promql(q)
        if "__error__" in raw:
            errors.append(f"{name}: {raw['__error__']}")
            return None
        return next(iter(_clean(raw).values()), None)

    evidence = {
        "regions_seen": sorted(set(rebuffer) | set(bitrate) | set(ttfb)) or REGIONS,
        "rebuffer_by_region": {k: round(v, 4) for k, v in rebuffer.items()},
        "bitrate_by_region": {k: round(v) for k, v in bitrate.items()},
        "ttfb_p95_by_region": {k: round(v, 4) for k, v in ttfb.items()},
        "fourxx_recent_rate": {k: round(v, 4) for k, v in recent.items()},
        "fourxx_earlier_rate": {k: round(v, 4) for k, v in earlier.items()},
        "fourxx_status": status,
        "fourxx_regions": hot_regions,
        "segment_lag_by_rendition": {k: round(v, 1) for k, v in lag.items()},
        "manifest_region": region,
        "manifest_rungs": rungs,
        "missing_rungs": ([r for r in FULL_LADDER if r not in rungs]
                          if rungs is not None else None),
        # None here means the manifest FETCH FAILED, not that the ladder is
        # complete. Read asymmetrically it used to both rule ladder_collapse
        # OUT (bool(None) -> the required "a rung is missing" check failed) and
        # vacuously PASS the healthy ladder check (`not (None or [])`), so one
        # slow edge on a cold start eliminated the fault and confirmed health
        # in the same evaluation.
        "manifest_fetched": rungs is not None,
        "encoder_up": scalar(Q_ENCODER_UP, "encoder_up"),
        "encoder_fps": scalar(Q_ENCODER_FPS, "encoder_fps"),
        "rungs_active": scalar(Q_RUNGS, "encoder_rungs_active"),
        "dropped_frames_rate": scalar(Q_DROPPED, "dropped_frames"),
        # Which exporters are actually publishing. Every absence-based check
        # consults this before reading an empty result as good news.
        "sources_reporting": {
            "viewers": _source_reporting(Q_VIEWERS_PRESENT),
            "edges": _source_reporting(Q_EDGES_PRESENT),
            "encoder": _source_reporting(Q_ENCODER_PRESENT),
        },
        "query_errors": errors,
        "fourxx_discriminator_note": (
            "status 'ongoing' means 404s are still happening (segment_gap); "
            "'stopped' means a burst has died out (ladder_collapse). Presence "
            "alone does not separate the two."
        ),
    }
    return evidence


def match_fault_signatures(region: str, tool_context=None) -> dict:
    """Collect evidence and run every fault signature against it.

    This is the authoritative diagnosis step. The returned deterministic_verdict
    is computed from fixed predicates, not argued for -- rank hypotheses however
    you like, but do not overrule this result. If you disagree with it, say so
    explicitly and explain which check you believe is wrong.

    Visual evidence (frame verdict, rung resolution measurement) is folded in
    when Phase 2 ran. Faults needing visual evidence that is absent come back as
    'unconfirmable' rather than ruled out -- missing evidence never counts as
    elimination.

    Args:
        region: region whose edge to read the manifest from.

    Returns:
        dict with deterministic_verdict, per-signature checklists and evidence.
    """
    evidence = collect_evidence(region)

    # Phase 2's finding, if it ran. Read from session state rather than asked
    # for as an argument, so the model cannot paraphrase or mistype it on the
    # way in -- the verdict must turn on what Phase 2 actually measured.
    if tool_context is not None:
        try:
            evidence = attach_visual_evidence(
                evidence, tool_context.state.get("visual_finding"),
                tool_context.state.get("rung_measurement"))
        except Exception:                              # noqa: BLE001
            pass

    result = evaluate_all(evidence)
    result["evidence"] = evidence
    result["visual_evidence_available"] = bool(evidence.get("visual"))

    # SURFACE THE BLIND SPOTS. `query_errors` was collected here and read
    # nowhere -- one grep hit, the write. So a run where half the queries failed
    # was indistinguishable in the output from a run where they all succeeded
    # and the plant was fine. Anything the checklist could not see is now
    # reported next to the verdict, and named in the Diagnosis schema, so it
    # reaches the operator rather than dying in a dict.
    srcs = evidence.get("sources_reporting") or {}
    blind = []
    for name, state in srcs.items():
        if state is False:
            blind.append(f"{name}: exporter is publishing no series at all")
        elif state is None:
            blind.append(f"{name}: could not determine whether it is reporting")
    if evidence.get("manifest_fetched") is False:
        blind.append("master manifest: fetch failed, ladder state unknown")
    for e in evidence.get("query_errors") or []:
        blind.append(f"query failed -- {e}")

    result["blind_spots"] = blind
    result["evidence_complete"] = not blind
    if blind:
        result["blind_spot_note"] = (
            "The checklist could NOT see the things listed in blind_spots. "
            "Checks depending on them are 'not_evaluated', which makes their "
            "signatures 'unconfirmable' -- deliberately NOT 'ruled out'. Do not "
            "describe anything above as healthy or eliminated on the strength "
            "of evidence that was never collected; say what was not visible."
        )
    if not evidence.get("visual"):
        result["visual_note"] = (
            "Phase 2 did not run, so black_source and ladder_mismatch cannot be "
            "confirmed or eliminated from telemetry alone. They are reported as "
            "unconfirmable, never as ruled out."
        )
    return result


def attach_visual_evidence(evidence: dict, visual_finding,
                           rung_measurement=None) -> dict:
    """Fold a VisualFinding (dict or JSON string) into collected evidence.

    `rung_measurement` is what check_rung_resolution actually measured, read
    from session state. It WINS over the same field inside visual_finding,
    which is the model's retyping of it. The rung verdict is the sole required
    check for ladder_mismatch and the repo calls it "decided in code, never by
    vision" -- so it must not arrive here having passed through a paraphrase.
    """
    if not visual_finding:
        return evidence
    if isinstance(visual_finding, str):
        try:
            visual_finding = json.loads(visual_finding)
        except json.JSONDecodeError:
            return evidence
    if isinstance(visual_finding, dict):
        evidence["visual"] = {
            "frame_verdict": visual_finding.get("frame_verdict"),
            "timecode_legible": visual_finding.get("timecode_legible"),
            "timecode_value": visual_finding.get("timecode_value"),
            "rung_resolution_verdict": visual_finding.get("rung_resolution_verdict"),
            "rung_resolution_ratio": visual_finding.get("rung_resolution_ratio"),
        }
        if isinstance(rung_measurement, str):
            try:
                rung_measurement = json.loads(rung_measurement)
            except json.JSONDecodeError:
                rung_measurement = None
        if isinstance(rung_measurement, dict) and rung_measurement.get("verdict"):
            reported = evidence["visual"]["rung_resolution_verdict"]
            measured = rung_measurement["verdict"]
            evidence["visual"]["rung_resolution_verdict"] = measured
            evidence["visual"]["rung_resolution_ratio"] = rung_measurement.get(
                "ratio", evidence["visual"]["rung_resolution_ratio"])
            evidence["visual"]["rung_verdict_source"] = "measured in code"
            if reported and reported != measured:
                # Worth shouting about: it means the transcription path that
                # used to be authoritative would have produced a different
                # diagnosis from the measurement.
                evidence["visual"]["rung_transcription_mismatch"] = (
                    f"Phase 2 reported {reported!r} but check_rung_resolution "
                    f"measured {measured!r}; the measurement is authoritative.")
    return evidence
