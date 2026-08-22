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


class FfmpegMissing(RuntimeError):
    """ffmpeg is not on PATH. Fatal, and deliberately not catchable as 'clear'."""


def require_ffmpeg():
    """Fail loudly and early if the host has no ffmpeg.

    THIS IS A HOST PREREQUISITE AND ITS ABSENCE USED TO READ AS HEALTH. Stock
    macOS and Ubuntu do not ship ffmpeg; the venv and Docker Desktop do not
    supply it. Without it `ffmpeg` raises FileNotFoundError, which screen_media
    caught as a generic exception and returned as `{"suspect": False}` -- and
    "not suspect" is the same shape as "the picture is fine".

    Downstream that becomes: no frame available -> black_source unconfirmable ->
    HEALTHY's remaining checks are all delivery metrics, which pass during a
    blackout -> no_fault_detected at HIGH confidence. A missing binary reported
    as a healthy plant, during the one fault this project exists to catch.
    """
    import shutil
    if shutil.which("ffmpeg") is None:
        raise FfmpegMissing(
            "ffmpeg is not on PATH. It is a HOST prerequisite for the content "
            "screen, the frame grabs and the rung measurement -- install it "
            "(macOS: brew install ffmpeg; Debian/Ubuntu: apt install ffmpeg) "
            "and re-run. Without it the agent cannot see the picture at all.")


def _run_screen(path, timeout=60):
    """One ffmpeg pass. YAVG/YHIGH to stdout, freeze events to stderr."""
    require_ffmpeg()
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
    # NOT caught below. A missing ffmpeg is a broken installation, not an
    # observation about the picture, and every other exit from this function
    # returns suspect=False -- which reads as "the picture is fine".
    require_ffmpeg()
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
    import time

    from .video_tools import _edge, _http_get

    t0 = time.monotonic()
    base = _edge(region)
    try:
        playlist = _http_get(f"{base}/hls/{rendition}/index.m3u8").decode(
            errors="replace")
        segs = [l.strip() for l in playlist.splitlines()
                if l.strip().endswith(".ts")]
        if not segs:
            return _published({"suspect": False, "reason": "error",
                               "error": f"no segments for {rendition} at {region}",
                               "measurements": {}},
                              region, rendition, time.monotonic() - t0)
        seg = segs[-1]
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as fh:
            fh.write(_http_get(f"{base}/hls/{rendition}/{seg}"))
            tmp = fh.name
    except Exception as exc:                            # noqa: BLE001
        return _published({"suspect": False, "reason": "error",
                           "measurements": {},
                           "error": f"fetch failed: {type(exc).__name__}: {exc}"},
                          region, rendition, time.monotonic() - t0)

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
    return _published(verdict, region, rendition, time.monotonic() - t0)


def _published(verdict, region, rendition, seconds):
    """Publish the verdict to Mimir, then hand it back unchanged.

    Wraps every return path out of screen_live_segment, including the error
    ones -- a screen that failed is a fact about content health too, and a
    panel that silently stops updating is the ambiguity this whole plant exists
    to remove.
    """
    try:
        from .content_metrics import record_screen
        record_screen(verdict, region, rendition, seconds)
    except Exception:                                   # noqa: BLE001
        pass                # record_screen already logs; never fail a screen
    return verdict


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(json.dumps({arg: screen_media(arg)}, indent=1))
