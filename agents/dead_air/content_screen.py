"""Stage 0: deterministic content screen. No model, no PNG decode, one pass.

The cascade this belongs to:

    Stage 0  arithmetic over the segment the sweep already downloaded   ~1.2s
    Stage 1  Gemini, only when Stage 0 says suspect -- classifies WHAT
    Stage 2  the full five-phase pipeline, only on a confirmed finding

Putting a model in the *detection* path is what made "catches it in seconds"
untrue: a sweep tick cost a full LLM investigation, so detection latency was
minutes. Deciding whether a frame is black is arithmetic. A model is only
needed to say what KIND of wrong it is, which is a question worth paying for
and only worth asking once something is already suspect.

WHY YAVG AND YHIGH, NOT YMAX OR blackdetect
-------------------------------------------
Our black frames deliberately carry a burned-in timecode, so vision can confirm
the encoder is alive. That breaks the two obvious detectors:

    measured on a real black segment vs a healthy one
                YLOW    YAVG     YHIGH   YMAX
    black        16     17.14      16     236   <-- YMAX is the timecode
    healthy      41    126.02     210     255

  * YMAX is useless: the white timecode pins it to 236 on a fully black picture.
  * ffmpeg's blackdetect defaults to pic_th=0.98, requiring 98% of pixels below
    threshold. It happens to trip on our current timecode, but that is a
    property of this overlay's size -- a larger clock, a station logo or a slate
    would silently stop it tripping, and the detector would fail exactly when
    the picture is most obviously wrong.

YAVG (mean luma) separates by ~7x, and YHIGH (90th percentile) reads the black
floor of 16 because the timecode occupies under 10% of the frame. Those two are
measured directly rather than inferred from a pixel-fraction heuristic.

FREEZE
------
freezedetect writes to the ffmpeg log, not to frame metadata, so ffprobe's
frame_tags never surface it. It is captured here from stderr in the same pass
that produces YAVG on stdout -- one process, two streams.
"""

import json
import os
import re
import statistics
import subprocess

# Calibrated in scripts/calibrate_content_screen.py against fixtures/ and live
# segments. See docs/content-screen.md for the confusion matrix.
BLACK_YAVG_MAX = float(os.environ.get("SCREEN_BLACK_YAVG_MAX", "40.0"))
BLACK_YHIGH_MAX = float(os.environ.get("SCREEN_BLACK_YHIGH_MAX", "40.0"))
# Fraction of frames in the segment that must look black before we call it.
BLACK_FRAME_FRACTION = float(os.environ.get("SCREEN_BLACK_FRACTION", "0.75"))
FREEZE_NOISE_DB = os.environ.get("SCREEN_FREEZE_NOISE", "-55dB")
FREEZE_MIN_SECONDS = float(os.environ.get("SCREEN_FREEZE_SECONDS", "2"))

_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
_YHIGH_RE = re.compile(r"lavfi\.signalstats\.YHIGH=([0-9.]+)")
_FREEZE_RE = re.compile(r"freeze_start")


def _run_screen(path, timeout=60):
    """One ffmpeg pass. YAVG/YHIGH to stdout, freeze events to stderr."""
    cmd = [
        "ffmpeg", "-v", "info", "-nostdin", "-i", path,
        "-vf", ("signalstats,"
                "metadata=print:file=-,"
                f"freezedetect=n={FREEZE_NOISE_DB}:d={FREEZE_MIN_SECONDS}"),
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.stdout, proc.stderr


def screen_media(path: str) -> dict:
    """Screen one segment or still for black / frozen picture. No model.

    Args:
        path: a .ts segment or an image file.

    Returns:
        dict with suspect, reason, and the measurements behind the call.
    """
    try:
        out, err = _run_screen(path)
    except subprocess.TimeoutExpired:
        return {"suspect": False, "reason": "error", "measurements": {},
                "error": "ffmpeg timed out screening the segment"}
    except Exception as exc:                            # noqa: BLE001
        return {"suspect": False, "reason": "error", "measurements": {},
                "error": f"{type(exc).__name__}: {exc}"}

    yavg = [float(m) for m in _YAVG_RE.findall(out)]
    yhigh = [float(m) for m in _YHIGH_RE.findall(out)]
    if not yavg:
        return {"suspect": False, "reason": "error", "measurements": {},
                "error": "no signalstats produced; unreadable media"}

    dark_frames = sum(1 for v in yavg if v <= BLACK_YAVG_MAX)
    dark_fraction = dark_frames / len(yavg)
    yhigh_med = statistics.median(yhigh) if yhigh else None

    is_black = (dark_fraction >= BLACK_FRAME_FRACTION
                and (yhigh_med is None or yhigh_med <= BLACK_YHIGH_MAX))
    froze = bool(_FREEZE_RE.search(err))

    if is_black:
        reason = "black"
    elif froze:
        reason = "frozen"
    else:
        reason = "clear"

    return {
        "suspect": reason in ("black", "frozen"),
        "reason": reason,
        "measurements": {
            "frames": len(yavg),
            "yavg_mean": round(statistics.fmean(yavg), 2),
            "yavg_min": round(min(yavg), 2),
            "yavg_max": round(max(yavg), 2),
            "yhigh_median": round(yhigh_med, 2) if yhigh_med is not None else None,
            "dark_frame_fraction": round(dark_fraction, 3),
            "freeze_detected": froze,
        },
        "thresholds": {
            "black_yavg_max": BLACK_YAVG_MAX,
            "black_yhigh_max": BLACK_YHIGH_MAX,
            "black_frame_fraction": BLACK_FRAME_FRACTION,
        },
        "method": ("deterministic signalstats + freezedetect, single ffmpeg "
                   "pass, no model"),
    }


def screen_live_segment(region: str, rendition: str = "1080p") -> dict:
    """Screen the newest segment from a region's edge. Stage 0 of the cascade.

    Deliberately reuses the fetch path Phase 2 already uses rather than adding a
    second download.

    Args:
        region: region whose edge to fetch from.
        rendition: ladder rung to screen.

    Returns:
        Stage 0 verdict, plus the segment it screened.
    """
    import tempfile

    from .video_tools import _edge, _http_get

    base = _edge(region)
    try:
        playlist = _http_get(f"{base}/hls/{rendition}/index.m3u8").decode(
            errors="replace")
        segs = [l.strip() for l in playlist.splitlines()
                if l.strip().endswith(".ts")]
        if not segs:
            return {"suspect": False, "reason": "error",
                    "error": f"no segments for {rendition} at {region}",
                    "measurements": {}}
        seg = segs[-1]
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as fh:
            fh.write(_http_get(f"{base}/hls/{rendition}/{seg}"))
            tmp = fh.name
    except Exception as exc:                            # noqa: BLE001
        return {"suspect": False, "reason": "error", "measurements": {},
                "error": f"fetch failed: {type(exc).__name__}: {exc}"}

    try:
        verdict = screen_media(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    verdict["region"] = region
    verdict["rendition"] = rendition
    verdict["segment"] = seg
    return verdict


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(json.dumps({arg: screen_media(arg)}, indent=1))
