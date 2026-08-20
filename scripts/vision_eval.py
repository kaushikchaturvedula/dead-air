#!/usr/bin/env python3
"""SEE-phase spike: can Gemini vision actually distinguish the faults?

This is the riskiest assumption in DEAD AIR. The whole premise is that frame
inspection catches what delivery telemetry cannot, so if vision cannot see
black_source and ladder_mismatch, the architecture has to change -- code
cross-compares rung resolution and vision only confirms.

WHAT "CORRECT" MEANS HERE
-------------------------
Only two of the five faults are visible in pixels at all:

    black_source     the frame is black                     -> vision must catch
    ladder_mismatch  the 1080p rung carries upscaled 720p   -> vision must catch

The other three produce completely normal frames. ladder_collapse removes a
rendition, segment_gap deletes segments, edge_latency slows delivery -- the
pixels that DO arrive are perfect. For those the correct vision answer is
"healthy", and a confident fault call is a FALSE POSITIVE that would send the
agent chasing a source problem when the fault is in delivery. They are scored
as healthy-expected, and false positives count against the model.

INPUT VARIANTS
--------------
Vision APIs downscale large images, which can destroy exactly the
high-frequency detail that separates real 1080p from upscaled 720p. So the same
frames are evaluated three ways:

    full     the whole 1920x1080 frame
    crop     a native-resolution crop of the detailed region (no downscale)
    pair     1080p and 720p crops together, asking for a relative judgement

If `pair` works where `full` and `crop` fail, that is a design finding: the SEE
phase should fetch two rungs, not one.

    python3 scripts/vision_eval.py                      # flash, all variants
    python3 scripts/vision_eval.py --models gemini-3.7-flash,gemini-2.5-pro
    python3 scripts/vision_eval.py --variants pair --modes ladder_mismatch,healthy
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO, "agents", "grafana_probe", ".env"))

from google import genai              # noqa: E402
from google.genai import types        # noqa: E402

FIXTURE_ROOT = os.path.join(REPO, "fixtures", "frames")
CROP_ROOT = os.path.join(REPO, "fixtures", "crops")

# Injected mode -> what vision SHOULD report. See the module docstring.
VISUAL_TRUTH = {
    "healthy": "healthy",
    "black_source": "black_source",
    "ladder_mismatch": "ladder_mismatch",
    "ladder_collapse": "healthy",
    "segment_gap": "healthy",
    "edge_latency": "healthy",
}

# What the model may answer, mapped onto our fault vocabulary. `frozen_frame`
# is offered deliberately: it is the most plausible wrong answer for
# black_source, and we want to see it if the model reaches for it.
ANSWER_TO_FAULT = {
    "healthy": "healthy",
    "black_frame": "black_source",
    "frozen_frame": "frozen_source",
    "upscaled_low_detail": "ladder_mismatch",
    "other_corruption": "other",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": list(ANSWER_TO_FAULT),
        },
        "confidence": {"type": "number"},
        "visual_evidence": {"type": "string"},
        "timecode_legible": {"type": "boolean"},
        "sharpness": {
            "type": "string",
            "enum": ["crisp", "slightly_soft", "very_soft", "not_applicable"],
        },
    },
    "required": ["classification", "confidence", "visual_evidence",
                 "timecode_legible", "sharpness"],
}

SINGLE_PROMPT = """\
You are inspecting a single decoded video frame pulled from a live HLS stream.

The stream carries a broadcast test pattern: vertical colour bars, a diagonal
sweeping line, fine checkerboard/noise blocks, and a burned-in timecode near
the bottom of the frame.

This frame was fetched from the rendition the manifest declares as {rung}
({width}x{height}).

Judge ONLY what you can see in the pixels. Classify the frame:

- healthy: a normal test pattern at the detail level you would expect for
  {rung}. Fine checkerboard/noise blocks resolve into crisp individual pixels.
- black_frame: the picture area is black or near-black (a burned-in timecode
  may still be visible over the black).
- frozen_frame: the picture appears to be a still, stuck image.
- upscaled_low_detail: a normal test pattern, but noticeably SOFTER than
  {rung} should be -- fine checkerboard/noise blocks look blurred, smeared or
  averaged rather than crisp, as if lower-resolution content was scaled up to
  fill a {rung} frame.
- other_corruption: anything else visibly wrong (tearing, blocking, colour
  corruption).

Be decisive but honest. If the frame looks like a normal healthy test pattern,
say healthy -- do not invent a fault. Report confidence 0.0-1.0 and state the
specific visual evidence you used.
"""

PAIR_PROMPT = """\
You are inspecting two decoded video frames from the SAME live HLS stream,
captured at the same moment from two different renditions of the ABR ladder.

Both images are native-resolution crops of the SAME region of the picture, so
they are directly comparable pixel-for-pixel.

  IMAGE 1: from the rendition the manifest declares as 1080p (1920x1080)
  IMAGE 2: from the rendition the manifest declares as 720p (1280x720)

The stream carries a broadcast test pattern including fine checkerboard/noise
blocks -- the detail that survives or is lost under rescaling.

A correctly encoded ladder means IMAGE 1 must carry MEASURABLY MORE fine detail
than IMAGE 2: crisper checkerboard, more resolvable individual pixels, less
smearing. That is the entire point of a higher rung.

If IMAGE 1 shows no more real detail than IMAGE 2 -- if they look equivalently
soft, or IMAGE 1 looks like a blown-up version of IMAGE 2 -- then the 1080p rung
is carrying upscaled lower-resolution content, and you should classify this as
upscaled_low_detail.

If IMAGE 1 is clearly sharper than IMAGE 2, classify healthy.

Report confidence 0.0-1.0 and state the specific visual evidence.
"""

RUNG_DIMS = {"1080p": (1920, 1080), "720p": (1280, 720),
             "480p": (854, 480), "360p": (640, 360)}

# The fine-detail region of testsrc2, as a fraction of frame size, so the same
# picture area is cropped from every rung regardless of resolution.
CROP_FRAC = (0.60, 0.58, 0.32, 0.30)   # x, y, w, h

_print_lock = threading.Lock()


def make_crop(src, rung, size=768):
    """Native-resolution crop of the detail region, upscaled with NEAREST.

    Nearest-neighbour matters: any smooth interpolation here would invent or
    destroy exactly the high-frequency detail the test is about.
    """
    w, h = RUNG_DIMS[rung]
    fx, fy, fw, fh = CROP_FRAC
    cw, ch = int(w * fw), int(h * fh)
    cx, cy = int(w * fx), int(h * fy)
    rel = os.path.relpath(src, FIXTURE_ROOT).replace(os.sep, "__")
    out = os.path.join(CROP_ROOT, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        return out
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", src,
             "-vf", f"crop={cw}:{ch}:{cx}:{cy},scale={size}:-1:flags=neighbor",
             out],
            check=True, capture_output=True, timeout=60)
        return out
    except Exception:
        return None


def load(path):
    with open(path, "rb") as fh:
        return fh.read()


def classify(client, model, parts, prompt):
    contents = [types.Part.from_text(text=prompt)] + [
        types.Part.from_bytes(data=load(p), mime_type="image/png") for p in parts
    ]
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SCHEMA,
            temperature=0.0,
        ),
    )
    return json.loads(resp.text)


def fixtures_for(mode, rung):
    d = os.path.join(FIXTURE_ROOT, mode)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.startswith(f"{rung}_") and f.endswith(".png"))


FALLBACK_ORDER = ["1080p", "720p", "480p", "360p"]


def frames_with_fallback(mode, rung, limit):
    """Frames for `rung`, falling back to the best rung that exists.

    ladder_collapse deletes the 1080p rung outright, so it has no top-rung
    fixtures at all. Skipping it would drop the row from the matrix; instead we
    evaluate the highest rung that survived, which is exactly what an agent
    inspecting a collapsed ladder would fetch.
    """
    frames = fixtures_for(mode, rung)[:limit]
    if frames:
        return frames, rung
    for alt in FALLBACK_ORDER[FALLBACK_ORDER.index(rung) + 1:]:
        frames = fixtures_for(mode, alt)[:limit]
        if frames:
            return frames, alt
    return [], rung


def build_cases(modes, variants, rung="1080p", limit=3):
    cases = []
    for mode in modes:
        frames, actual_rung = frames_with_fallback(mode, rung, limit)
        if actual_rung != rung:
            print(f"  note: {mode} has no {rung} frames "
                  f"(rung absent) -- evaluating {actual_rung}", flush=True)
        for variant in variants:
            if variant == "pair":
                # Pair mode compares the top rung against 720p. If the top rung
                # is absent there is nothing to cross-compare, which is itself
                # the ladder_collapse finding -- skip rather than fake it.
                if actual_rung != rung:
                    continue
                partners = fixtures_for(mode, "720p")[:limit]
                for i, f in enumerate(frames):
                    if i >= len(partners):
                        break
                    a, b = make_crop(f, actual_rung), make_crop(partners[i], "720p")
                    if a and b:
                        cases.append({"mode": mode, "variant": "pair",
                                      "parts": [a, b], "rung": actual_rung,
                                      "prompt": PAIR_PROMPT})
            elif variant == "crop":
                for f in frames:
                    c = make_crop(f, actual_rung)
                    if c:
                        w, h = RUNG_DIMS[actual_rung]
                        cases.append({"mode": mode, "variant": "crop",
                                      "parts": [c], "rung": actual_rung,
                                      "prompt": SINGLE_PROMPT.format(
                                          rung=actual_rung, width=w, height=h)})
            else:
                w, h = RUNG_DIMS[actual_rung]
                for f in frames:
                    cases.append({"mode": mode, "variant": "full",
                                  "parts": [f], "rung": actual_rung,
                                  "prompt": SINGLE_PROMPT.format(
                                      rung=actual_rung, width=w, height=h)})
    return cases


def run(client, model, cases, workers=6):
    results = []

    def one(case):
        try:
            out = classify(client, model, case["parts"], case["prompt"])
        except Exception as e:
            out = {"classification": "ERROR", "confidence": 0.0,
                   "visual_evidence": f"{type(e).__name__}: {str(e)[:200]}",
                   "timecode_legible": False, "sharpness": "not_applicable"}
        predicted = ANSWER_TO_FAULT.get(out.get("classification"), "ERROR")
        rec = {**case, "model": model, "raw": out,
               "expected": VISUAL_TRUTH[case["mode"]],
               "predicted": predicted,
               "correct": predicted == VISUAL_TRUTH[case["mode"]]}
        with _print_lock:
            mark = "OK " if rec["correct"] else "XX "
            print(f"  {mark} {model:<17} {case['variant']:<5} "
                  f"{case['mode']:<16} -> {predicted:<16} "
                  f"conf={out.get('confidence', 0):.2f}", flush=True)
        return rec

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rec in ex.map(one, cases):
            results.append(rec)
    return results


def matrix(results, model, variant):
    subset = [r for r in results if r["model"] == model and r["variant"] == variant]
    if not subset:
        return
    labels = ["healthy", "black_source", "ladder_mismatch", "frozen_source",
              "other", "ERROR"]
    counts = defaultdict(Counter)
    for r in subset:
        counts[r["mode"]][r["predicted"]] += 1

    print(f"\n  {model} / {variant}")
    header = "    " + f"{'injected':<17}" + "".join(f"{l[:13]:>15}" for l in labels)
    print(header)
    print("    " + "-" * (len(header) - 4))
    for mode in VISUAL_TRUTH:
        if mode not in counts:
            continue
        row = f"    {mode:<17}"
        for l in labels:
            n = counts[mode][l]
            row += f"{(str(n) if n else '.'):>15}"
        exp = VISUAL_TRUTH[mode]
        ok = counts[mode][exp]
        tot = sum(counts[mode].values())
        print(row + f"   (expect {exp}: {ok}/{tot})")
    acc = sum(1 for r in subset if r["correct"]) / len(subset)
    print(f"    accuracy: {acc:.0%}  ({sum(1 for r in subset if r['correct'])}/{len(subset)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gemini-3.7-flash")
    ap.add_argument("--variants", default="full,crop,pair")
    ap.add_argument("--modes", default=",".join(VISUAL_TRUTH))
    ap.add_argument("--rung", default="1080p")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--json", help="write raw results here")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ["GOOGLE_CLOUD_LOCATION"],
    )

    cases = build_cases(modes, variants, rung=args.rung, limit=args.limit)
    if not cases:
        sys.exit("no fixtures found -- run: "
                 "python3 scripts/fault_signatures.py --fixtures")
    print(f"{len(cases)} case(s) x {len(models)} model(s)\n")

    results = []
    for model in models:
        results += run(client, model, cases)

    print("\n" + "=" * 78)
    print("CONFUSION MATRICES  (rows = injected fault, cols = what vision said)")
    print("=" * 78)
    for model in models:
        for variant in variants:
            matrix(results, model, variant)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=1)
        print(f"\nraw results -> {args.json}")


if __name__ == "__main__":
    main()
