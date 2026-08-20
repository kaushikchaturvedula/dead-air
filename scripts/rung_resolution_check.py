#!/usr/bin/env python3
"""Detect ladder_mismatch in code: does a rung carry the detail it advertises?

WHY THIS EXISTS
---------------
The SEE-phase spike (docs/vision-spike.md) found that no Gemini tier separates
ladder_mismatch from healthy -- the flash tiers call everything crisp, 2.5-pro
calls everything upscaled, and cross-rung pairing makes it worse because the
models confabulate the comparison in confident, exactly-backwards prose.

The same frames are trivially separable in code. So for this one fault the
architecture inverts:

    code    measures whether the rung carries its advertised detail  <- decides
    vision  describes what an operator would see                     <- confirms

THE TEST
--------
Downscale a frame to the next rung down, scale it back up, compare to the
original. Genuine 1080p loses real high-frequency detail on that trip. Content
that was ALREADY upscaled 720p loses almost nothing, because the detail was
never there.

Absolute PSNR alone depends on how busy the content is, so a threshold tuned on
testsrc2 would not survive a switch to real video. The primary signal is
therefore a RATIO against the rung below, measured on the same stream at the
same moment:

    ratio = round_trip_psnr(1080p) / round_trip_psnr(720p)

Both rungs see the same picture, so content complexity cancels. A healthy
ladder gives ratio slightly below 1.0 -- the top rung has the most real detail
to lose, so it scores the LOWEST PSNR. A mismatched top rung inverts that: it
survives downscaling better than the rung beneath it, which is physically
impossible for honest content.

Measured on the fixture set (docs/vision-spike.md):

    healthy          0.935
    segment_gap      0.944
    edge_latency     0.934
    ladder_mismatch  1.198   <-- 25x the healthy cluster's own spread
"""

import argparse
import json
import os
import subprocess
import tempfile

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

RUNG_DIMS = {"1080p": (1920, 1080), "720p": (1280, 720),
             "480p": (854, 480), "360p": (640, 360)}
NEXT_RUNG_DOWN = {"1080p": "720p", "720p": "480p", "480p": "360p"}

# Healthy measures 0.934-0.944; a mismatched top rung measures ~1.20. 1.05 sits
# far from both clusters, and above 1.0 it is also physically motivated: a rung
# can only out-survive the rung below it if it is not carrying real detail.
RATIO_SUSPECT = 1.05

# Fallback only, for when a comparison rung is unavailable. Content-dependent --
# calibrated on testsrc2 and NOT trustworthy on other material.
ABSOLUTE_PSNR_SUSPECT_DB = 41.0


def luma(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.float64)


def laplacian_var(a):
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)
    w = sliding_window_view(a, (3, 3))
    return float((w * k).sum(axis=(-1, -2)).var())


def round_trip_psnr(path, rung):
    """PSNR of a frame against itself after a downscale/upscale round trip."""
    down = NEXT_RUNG_DOWN.get(rung)
    if not down:
        return None
    dw, dh = RUNG_DIMS[down]
    uw, uh = RUNG_DIMS[rung]
    with tempfile.TemporaryDirectory() as td:
        rt = os.path.join(td, "rt.png")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", path,
             "-vf", f"scale={dw}:{dh}:flags=bicubic,scale={uw}:{uh}:flags=bicubic",
             rt], check=True, capture_output=True, timeout=90)
        a, b = luma(path), luma(rt)
    if a.shape != b.shape:
        return None
    mse = float(((a - b) ** 2).mean())
    return 99.0 if mse <= 1e-9 else float(10.0 * np.log10((255.0 ** 2) / mse))


def compare_rungs(high_path, low_path, high_rung="1080p", low_rung="720p"):
    """Content-independent verdict for one rung against the rung below it.

    Returns a dict suitable for handing straight to an agent as tool output.
    """
    hp = round_trip_psnr(high_path, high_rung)
    lp = round_trip_psnr(low_path, low_rung)
    if hp is None or lp is None or lp == 0:
        return {"verdict": "inconclusive",
                "reason": "could not compute round-trip PSNR for both rungs"}

    ratio = hp / lp
    suspect = ratio > RATIO_SUSPECT
    return {
        "verdict": "suspect_upscaled" if suspect else "carries_expected_detail",
        "high_rung": high_rung,
        "low_rung": low_rung,
        "high_round_trip_psnr_db": round(hp, 2),
        "low_round_trip_psnr_db": round(lp, 2),
        "ratio": round(ratio, 3),
        "threshold": RATIO_SUSPECT,
        "high_laplacian_var": round(laplacian_var(luma(high_path)), 1),
        "low_laplacian_var": round(laplacian_var(luma(low_path)), 1),
        "interpretation": (
            f"The {high_rung} rung survives downscaling BETTER than {low_rung} "
            f"(ratio {ratio:.3f} > {RATIO_SUSPECT}). A higher rung can only "
            f"out-survive the rung beneath it if it is not carrying the extra "
            f"detail it advertises, so {high_rung} is very likely upscaled "
            f"lower-resolution content."
            if suspect else
            f"The {high_rung} rung loses more detail under downscaling than "
            f"{low_rung} (ratio {ratio:.3f} <= {RATIO_SUSPECT}), which is what "
            f"an honest ladder looks like: the top rung has the most real "
            f"detail to lose."
        ),
    }


def check_single(path, rung):
    """Absolute check, for when no comparison rung exists. Content-dependent."""
    a = luma(path)
    psnr = round_trip_psnr(path, rung)
    return {
        "frame": path,
        "rung": rung,
        "dimensions": f"{a.shape[1]}x{a.shape[0]}",
        "laplacian_var": round(laplacian_var(a), 1),
        "round_trip_psnr_db": round(psnr, 2) if psnr is not None else None,
        "verdict": ("suspect_upscaled"
                    if psnr is not None and psnr > ABSOLUTE_PSNR_SUSPECT_DB
                    else "carries_expected_detail"),
        "caveat": "absolute threshold calibrated on testsrc2; prefer compare_rungs",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", nargs="*", help="frames for the absolute check")
    ap.add_argument("--rung", default="1080p")
    ap.add_argument("--high", help="high-rung frame for the ratio check")
    ap.add_argument("--low", help="low-rung frame for the ratio check")
    ap.add_argument("--high-rung", default="1080p")
    ap.add_argument("--low-rung", default="720p")
    ap.add_argument("--json")
    args = ap.parse_args()

    if args.high and args.low:
        out = compare_rungs(args.high, args.low, args.high_rung, args.low_rung)
        print(json.dumps(out, indent=1))
        if args.json:
            open(args.json, "w").write(json.dumps(out, indent=1))
        return

    if not args.frames:
        ap.error("pass frames for the absolute check, or --high/--low for the ratio")

    out = []
    for f in args.frames:
        try:
            out.append(check_single(f, args.rung))
        except Exception as e:
            out.append({"frame": f, "error": f"{type(e).__name__}: {e}"})

    print(f"{'frame':<52} {'lap_var':>9} {'rt_psnr':>9}  verdict")
    print("-" * 92)
    for r in out:
        if "error" in r:
            print(f"{os.path.basename(r['frame']):<52} {r['error']}")
            continue
        print(f"{r['frame'][-52:]:<52} {r['laplacian_var']:>9.1f} "
              f"{r['round_trip_psnr_db']:>9.2f}  {r['verdict']}")
    if args.json:
        open(args.json, "w").write(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
