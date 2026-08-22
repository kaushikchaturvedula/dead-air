"""Phase 2 (SEE) tools: fetch real pixels off the plant and judge them.

Two kinds of tool, split along the line the spike drew:

    inspect_frame          Gemini vision -- black / frozen / corrupted
    check_rung_resolution  deterministic code -- does the rung carry its detail

Vision is never asked to judge resolution fidelity. The spike (docs/vision-spike.md)
showed no tier can, and that merely OFFERING an "upscaled" answer produces
confident, specific, exactly-backwards prose. The vision prompt below therefore
does not mention resolution at all.

Vision runs INSIDE a tool rather than by handing images to the agent's own
context. That keeps the prompt pinned: it cannot drift as the surrounding
conversation grows, which is the whole reason this fault class is detectable at
all.
"""

import json
import os
import subprocess
import tempfile
import time

from google import genai
from google.genai import types

# Local edge ports. On Cloud Run these become service URLs and only this map
# changes -- the agent never learns a hostname.
EDGE_ENDPOINTS = {
    "us-east1": os.environ.get("EDGE_US_EAST1", "http://localhost:8081"),
    "europe-west1": os.environ.get("EDGE_EUROPE_WEST1", "http://localhost:8082"),
    "asia-south1": os.environ.get("EDGE_ASIA_SOUTH1", "http://localhost:8083"),
}
FRAME_DIR = os.environ.get("DEADAIR_FRAME_DIR", "frames/agent")
VISION_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

# Deliberately omits any resolution/sharpness/upscaling option. See module docstring.
VISION_PROMPT = """\
You are inspecting one decoded frame from a live video stream, to determine
whether viewers are seeing a picture at all.

The stream should be carrying a broadcast test pattern: vertical colour bars, a
diagonal sweeping line, checkerboard blocks, and a burned-in timecode reading
HH:MM:SS.mmm near the bottom centre.

Classify what you see:

- healthy: a normal test pattern with visible picture content.
- black_frame: the picture area is black or near-black. A burned-in timecode may
  still be visible over the black -- that still counts as black_frame.
- frozen_frame: picture content is present but appears to be a stuck still.
- corrupted: visible tearing, blocking, garbled colour, or partial rendering.

Also report the burned-in timecode EXACTLY as displayed if you can read it, and
whether it was legible.

Do NOT comment on sharpness, resolution, or image quality. You are judging
whether there is a picture, not how good it looks.

Report confidence 0.0-1.0 and state the specific visual evidence you used.
"""

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["healthy", "black_frame", "frozen_frame", "corrupted"],
        },
        "confidence": {"type": "number"},
        "visual_evidence": {"type": "string"},
        "timecode_legible": {"type": "boolean"},
        "timecode_value": {"type": "string"},
    },
    "required": ["classification", "confidence", "visual_evidence",
                 "timecode_legible", "timecode_value"],
}

_client = None


def _genai_client():
    """The vision client, with the SAME timeout and retry as the agent's model.

    THIS IS THE SECOND CLIENT. model.py fixed an infinite hang for the
    ADK-managed one after a single generate_content call stalled for 889
    SECONDS and took a whole investigation down with it -- and that fix never
    reached here, because this client is constructed directly rather than
    through ADK.

    google-genai does NOT default to a sane timeout. It explicitly replaces
    httpx's 5s default with infinity (`_api_client.py`: `if 'timeout' not in
    args: args['timeout'] = None`), and with retry_options unset it stops after
    a single attempt. So a half-open connection -- venue Wi-Fi handing over, a
    load balancer reaping an idle socket -- makes inspect_frame never return.

    In the sweep that is the worst possible presentation: the last line on
    screen is `STAGE 0 SUSPECT -- black (yavg=17.1)` followed by silence, so
    the system looks like it died at the exact instant it correctly caught the
    fault it exists to catch.

    RETRY and REQUEST_TIMEOUT_MS are imported rather than re-declared so the
    two clients cannot drift apart again. Retry matters here for a second
    reason: without it a single 429 becomes `classification:
    "no_frame_available"`, which is the same value returned when the rendition
    genuinely does not exist -- a rate limit presenting as an observation about
    the plant.
    """
    global _client
    if _client is None:
        from .model import RETRY, REQUEST_TIMEOUT_MS
        _client = genai.Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
            http_options=types.HttpOptions(
                retry_options=RETRY,
                timeout=REQUEST_TIMEOUT_MS,
            ),
        )
    return _client


def _edge(region: str) -> str:
    return EDGE_ENDPOINTS.get(region, EDGE_ENDPOINTS["us-east1"])


def _http_get(url: str, timeout: int = 45) -> bytes:
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _pull_frame(region: str, rendition: str, out_path: str,
                nth_from_end: int = 1):
    """Fetch the Nth-newest segment for a rendition and decode one frame."""
    base = _edge(region)
    playlist = _http_get(f"{base}/hls/{rendition}/index.m3u8").decode(
        errors="replace")
    segs = [l.strip() for l in playlist.splitlines() if l.strip().endswith(".ts")]
    if len(segs) < nth_from_end:
        return None, None
    seg = segs[-nth_from_end]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as fh:
        fh.write(_http_get(f"{base}/hls/{rendition}/{seg}"))
        tmp = fh.name
    try:
        # -ss AFTER -i: HLS segments carry a non-zero start PTS, so input-seeking
        # lands past the end and silently writes nothing.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp,
                        "-ss", "1", "-frames:v", "1", out_path],
                       check=True, capture_output=True, timeout=90)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return (out_path, seg) if os.path.exists(out_path) else (None, seg)


# --- tools exposed to the agent -------------------------------------------

def get_stream_manifest(region: str) -> dict:
    """Fetch the HLS master manifest from a region's edge.

    Reports which renditions the ladder currently advertises. A rung that has
    disappeared from the manifest is the ladder_collapse signature and is worth
    establishing before any frame is fetched.

    Args:
        region: one of us-east1, europe-west1, asia-south1.

    Returns:
        dict with advertised renditions and the raw manifest.
    """
    try:
        raw = _http_get(f"{_edge(region)}/hls/master.m3u8").decode(
            errors="replace")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}",
                "region": region, "renditions": []}
    rungs = []
    for line in raw.splitlines():
        line = line.strip()
        if line.endswith("index.m3u8"):
            rungs.append(line.split("/")[0])
    return {
        "region": region,
        "renditions": rungs,
        "rendition_count": len(rungs),
        "expected_full_ladder": ["1080p", "720p", "480p", "360p"],
        "missing_renditions": [r for r in ["1080p", "720p", "480p", "360p"]
                               if r not in rungs],
        "manifest": raw[:1200],
    }


def inspect_frame(region: str, rendition: str) -> dict:
    """Fetch the newest segment for a rendition and inspect the picture.

    This is the SEE capability that delivery telemetry cannot provide: whether
    viewers are actually seeing a picture. Use it whenever metrics look healthy
    but a fault is suspected, and whenever an alert cannot be explained by
    delivery signals alone.

    Judges only: healthy / black_frame / frozen_frame / corrupted. It does NOT
    judge resolution or sharpness -- call check_rung_resolution for that.

    Args:
        region: region whose edge to fetch from.
        rendition: ladder rung, e.g. "1080p".

    Returns:
        dict with the verdict, confidence, visual evidence and burned-in timecode.
    """
    stamp = time.strftime("%H%M%S")
    out = os.path.join(FRAME_DIR, f"{region}-{rendition}-{stamp}.png")
    try:
        path, seg = _pull_frame(region, rendition, out)
    except Exception as e:
        return {"error": f"could not fetch frame: {type(e).__name__}: {str(e)[:200]}",
                "region": region, "rendition": rendition,
                "classification": "no_frame_available"}
    if not path:
        return {"region": region, "rendition": rendition,
                "classification": "no_frame_available",
                "note": f"no segments available for {rendition} at {region} -- "
                        "the rendition may not be being produced at all",
                "segment": seg}

    try:
        with open(path, "rb") as fh:
            data = fh.read()
        resp = _genai_client().models.generate_content(
            model=VISION_MODEL,
            contents=[types.Part.from_text(text=VISION_PROMPT),
                      types.Part.from_bytes(data=data, mime_type="image/png")],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VISION_SCHEMA,
                temperature=0.0,
            ),
        )
        verdict = json.loads(resp.text)
    except Exception as e:
        return {"error": f"vision call failed: {type(e).__name__}: {str(e)[:200]}",
                "region": region, "rendition": rendition, "frame_path": path,
                "classification": "no_frame_available"}

    verdict.update({"region": region, "rendition": rendition,
                    "frame_path": path, "segment": seg, "model": VISION_MODEL})
    return verdict


def check_rung_resolution(region: str, tool_context=None) -> dict:
    """Measure whether the 1080p rung carries the detail it advertises.

    DETERMINISTIC. This is a measurement, not a judgement, and it is the only
    authority on ladder_mismatch.

    THE MEASUREMENT IS WRITTEN TO STATE, not just returned. schemas.py:100 says
    resolution is "measured in code", but the value reached the deterministic
    checklist only after the model had read this dict, re-typed the verdict into
    VisualFinding, and had that transcription read back out -- so the one thing
    the repo calls deterministic was travelling as LLM-transcribed prose. One
    mistyped enum (`inconclusive` where the tool measured `suspect_upscaled`)
    silently turned ladder_mismatch unconfirmable, and because a content fault
    moves no delivery metric, HEALTHY would then confirm on a plant serving an
    upscaled top rung.

    The tool now records its own result under `rung_measurement`, and
    attach_visual_evidence prefers it over anything the model wrote. Vision cannot detect this fault -- no model
    tier separates an upscaled rung from a healthy one, and asking produces
    confident wrong answers (docs/vision-spike.md).

    Downscales the 1080p frame to 720p and back, and compares how much detail it
    loses against the same round trip on the real 720p rung. A rung that
    survives downscaling BETTER than the rung beneath it is not carrying the
    detail it claims.

    Args:
        region: region whose edge to fetch both rungs from.

    Returns:
        dict with ratio, threshold, verdict and a plain-language interpretation.
    """
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(repo, "scripts"))
    try:
        from rung_resolution_check import compare_rungs
    except Exception as e:
        return {"verdict": "inconclusive",
                "error": f"detector unavailable: {type(e).__name__}: {e}"}

    stamp = time.strftime("%H%M%S")
    try:
        hi, _ = _pull_frame(region, "1080p",
                            os.path.join(FRAME_DIR, f"{region}-1080p-rr-{stamp}.png"))
        lo, _ = _pull_frame(region, "720p",
                            os.path.join(FRAME_DIR, f"{region}-720p-rr-{stamp}.png"))
    except Exception as e:
        return {"verdict": "inconclusive",
                "error": f"could not fetch both rungs: {type(e).__name__}: {str(e)[:200]}"}
    if not hi or not lo:
        return {"verdict": "inconclusive",
                "reason": "could not obtain a frame from both 1080p and 720p; "
                          "if 1080p is missing entirely that is ladder_collapse, "
                          "not ladder_mismatch"}

    out = compare_rungs(hi, lo, "1080p", "720p")
    out["region"] = region
    out["method"] = "deterministic round-trip PSNR ratio (no model involved)"
    _record_rung_measurement(out, tool_context)
    return out


def _record_rung_measurement(out, tool_context):
    """Persist the measurement so the checklist never has to trust a retelling."""
    if tool_context is None:
        return
    try:
        tool_context.state["rung_measurement"] = {
            "verdict": out.get("verdict"),
            "ratio": out.get("ratio"),
            "region": out.get("region"),
            "source": "check_rung_resolution (code)",
        }
    except Exception:                                  # noqa: BLE001
        pass                    # never fail a measurement to record it
