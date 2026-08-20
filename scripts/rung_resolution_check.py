#!/usr/bin/env python3
"""Detect ladder_mismatch in code: does a rung carry the detail it advertises?

WHY THIS EXISTS
---------------
The SEE-phase spike (scripts/vision_eval.py) found that Gemini cannot see
ladder_mismatch from pixels -- 0/3 across full frames, native-resolution crops,
and side-by-side cross-rung comparison, at 0.95+ confidence that the frame was
healthy. The fault is nonetheless trivially measurable: the same frames differ
by ~6x in Laplacian variance.

So the architecture inverts for this fault. Code decides, vision confirms:

    code    measures whether the rung carries its advertised detail  <- decides
    vision  describes what the operator would see                    <- confirms

THE TEST
--------
Downscale the frame to the next rung down, scale it back up, and compare to the
original. Genuine 1080p content loses real high-frequency detail in that round
trip. Content that was ALREADY upscaled 720p loses almost nothing, because the
detail was never there -- the round trip is close to a no-op.

    round_trip_psnr    high  => frame survives downscaling => already soft
                       low   => frame loses real detail    => genuinely 1080p

Laplacian variance is reported alongside as an absolute sharpness measure. The
round-trip ratio is the primary signal because it is self-referential: it needs
no healthy baseline to compare against, so it works on a single frame at
whatever the ladder happens to be doing right now.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

RUNG_DIMS = {"1080p": (1920, 1080), "720p": (1280, 720),
             "480p": (854, 480), "360p": (640, 360)}
NEXT_RUNG_DOWN = {"1080p": "720p", "720p": "480p", "480p": "360p"}

# Calibrated on the measured fixture set; see the table in docs/vision-spike.md.
PSNR_SUSPECT_DB = 41.0


def luma(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.float64)


def laplacian_var(a):
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)
    w = sliding_window_view(a, (3, 3))
    return float((w * k).sum(axis=(-1, -2)).var())


def round_trip_psnr(path, rung):
    """PSNR of frame vs itself after a downscale/upscale round trip."""
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
    if mse <= 1e-9:
        return 99.0
    return float(10.0 * np.log10((255.0 ** 2) / mse))


def check(path, rung):
    a = luma(path)
    psnr = round_trip_psnr(path, rung)
    return {
        "frame": path,
        "rung": rung,
        "dimensions": f"{a.shape[1]}x{a.shape[0]}",
        "laplacian_var": round(laplacian_var(a), 1),
        "round_trip_psnr_db": round(psnr, 2) if psnr is not None else None,
        "verdict": ("suspect_upscaled"
                    if psnr is not None and psnr > PSNR_SUSPECT_DB
                    else "carries_expected_detail"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", nargs="+")
    ap.add_argument("--rung", default="1080p")
    ap.add_argument("--json")
    args = ap.parse_args()

    out = []
    for f in args.frames:
        try:
            out.append(check(f, args.rung))
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
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
