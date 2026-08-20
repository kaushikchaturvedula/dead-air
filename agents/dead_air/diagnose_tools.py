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

    def scalar(q, default=None):
        v = _clean(_promql(q))
        return next(iter(v.values()), default)

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
        "encoder_up": scalar(Q_ENCODER_UP, 0),
        "encoder_fps": scalar(Q_ENCODER_FPS, 0),
        "rungs_active": scalar(Q_RUNGS, 0),
        "dropped_frames_rate": scalar(Q_DROPPED, 0),
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
                evidence, tool_context.state.get("visual_finding"))
        except Exception:                              # noqa: BLE001
            pass

    result = evaluate_all(evidence)
    result["evidence"] = evidence
    result["visual_evidence_available"] = bool(evidence.get("visual"))
    if not evidence.get("visual"):
        result["visual_note"] = (
            "Phase 2 did not run, so black_source and ladder_mismatch cannot be "
            "confirmed or eliminated from telemetry alone. They are reported as "
            "unconfirmable, never as ruled out."
        )
    return result


def attach_visual_evidence(evidence: dict, visual_finding) -> dict:
    """Fold a VisualFinding (dict or JSON string) into collected evidence."""
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
    return evidence
