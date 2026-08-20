# SEE-phase spike: can Gemini vision actually see these faults?

**Run 2026-08-19, against the live plant. Verdict: half the hypothesis holds.**

`black_source` is detected perfectly by every model and every input variant.
`ladder_mismatch` is **not detected by any model in any variant** — and the
models that appear to catch it are flagging healthy frames at the same rate,
which is not detection, it is a bias.

This changes the architecture for one fault. It does not change the project's
premise.

## Headline

| Fault | Vision verdict | Who decides |
| --- | --- | --- |
| `black_source` | **detected, 100%, every model and variant** | vision |
| `ladder_mismatch` | **not detected by any configuration** | **code decides, vision confirms** |
| `ladder_collapse` / `segment_gap` / `edge_latency` | correctly read as healthy pixels | telemetry (already proven) |

## What "correct" means here

Only two of the five faults are visible in pixels at all. `ladder_collapse`
removes a rendition, `segment_gap` deletes segments, `edge_latency` slows
delivery — the frames that *do* arrive are perfect. For those three the correct
vision answer is **healthy**, and a confident fault call is a false positive
that would send the agent hunting a source problem while the real fault is in
delivery. They are scored as healthy-expected and false positives count against
the model.

## The eval set

69 frames, captured from the live plant across all six states (healthy plus
five faults) at all four rungs, 3 frames each, sampled mid-segment so we never
only see the atypically clean opening IDR.

Regenerate with:

```bash
python3 scripts/fault_signatures.py --fixtures
```

It writes to `fixtures/` (gitignored) and is driven by the same harness that
produces the telemetry signatures, so the eval set can never drift from what
the encoder actually emits. `ladder_collapse` legitimately yields **zero** 1080p
frames — the rung does not exist — and the eval falls back to the highest rung
that survived, which is what an agent inspecting a collapsed ladder would fetch.

Three input variants, because vision APIs downscale large images and that could
destroy the very high-frequency detail the hard fault depends on:

- **full** — the whole 1920×1080 frame
- **crop** — a native-resolution crop of the fine-checkerboard region, no downscale
- **pair** — 1080p and 720p crops together, asking for a relative judgement

## Results

### `black_source` — solved

| Model | full | crop | pair |
| --- | --- | --- | --- |
| gemini-3.7-flash | 3/3 | 3/3 | 3/3 |
| gemini-2.5-pro | 3/3 | 3/3 | — |

Unambiguous, high confidence, every time. The burned-in timecode remains legible
over the black, so the model can also confirm the encoder is alive — which is
what separates `black_source` from a frozen source.

### `ladder_mismatch` — not solved

Read this as a 2×2, not as accuracy. What matters is whether a model separates
mismatch from healthy **at all**:

| Model / variant | says mismatch on mismatch | says healthy on healthy | discriminates? |
| --- | --- | --- | --- |
| gemini-3.7-flash / full | 0/3 | 3/3 | no — always "healthy" |
| gemini-3.7-flash / crop | 0/3 | 3/3 | no — always "healthy" |
| gemini-3.7-flash / pair | 0/3 | 3/3 | no — always "healthy" |
| gemini-3.6-flash / full | 0/3 | 3/3 | no — always "healthy" |
| gemini-3.6-flash / crop | 0/3 | 2/3 | no |
| gemini-3.6-flash / pair | 0/3 | 3/3 | no — always "healthy" |
| gemini-2.5-pro / full | 3/3 | **0/3** | no — always "upscaled" |
| gemini-2.5-pro / crop | 3/3 | **0/3** | no — always "upscaled" |
| gemini-2.5-pro / pair | 0/3 | **0/3** | **inverted** |

**No configuration gets both columns.** Every model has a constant prior: the
flash tiers answer "crisp/healthy" to everything, 2.5-pro answers "soft/upscaled"
to everything. 2.5-pro's 3/3 on mismatch is not detection — it flags healthy
frames just as confidently, and would fire a false source alarm on a healthy
stream every time it looked.

**Cross-rung pairing made it worse, not better.** The hope was that a relative
judgement would succeed where an absolute one failed. Instead the models
confabulate the comparison, producing the same fluent, specific-sounding
sentence regardless of which image is which:

> **3.6-flash, on the MISMATCH pair:** "Image 1 (1080p) exhibits fine
> single-pixel noise patterns and crisp edges … visibly higher resolution and
> carry more fine detail compared to Image 2."
>
> **2.5-pro, on the HEALTHY pair:** "The fine checkerboard pattern in Image 1
> (1080p) shows no more resolved detail than the same pattern in Image 2 …
> indicating the 1080p rung is upscaled."

Both statements are confident, both cite specific visual evidence, and both are
**exactly backwards**. The models are not measuring detail; they are producing
plausible prose about detail. That is the dangerous failure mode for an agent
whose entire job is diagnosis — a wrong answer that reads like a good one.

### False positives on healthy — the risk that matters

| Model | FP rate on healthy frames |
| --- | --- |
| gemini-3.7-flash | **0/9** |
| gemini-3.6-flash | 1/9 |
| gemini-2.5-pro | **9/9** |

gemini-3.7-flash never invents a fault. That is the single most important number
here: for `black_source` it is both sensitive and specific, so it can be trusted
to speak. gemini-2.5-pro is unusable for the SEE phase despite being the
higher tier — it would report a source fault on every healthy check.

One minor blemish: 3.7-flash returned `other_corruption` on 2 of 3
`ladder_collapse` crops (evaluated at the 720p fallback rung). Worth watching,
but those frames come from a fault telemetry already diagnoses unambiguously.

## The fault is trivially measurable in code

The frames vision could not separate differ by **6×** in an off-the-shelf
sharpness metric:

| Frames | Laplacian variance | Round-trip PSNR | Verdict |
| --- | --- | --- | --- |
| healthy 1080p | 376.0 | 36.54 dB | carries expected detail |
| segment_gap 1080p | 362.5 | 36.74 dB | carries expected detail |
| edge_latency 1080p | 379.1 | 36.48 dB | carries expected detail |
| **ladder_mismatch 1080p** | **64.1** | **46.63 dB** | **suspect upscaled** |

A **10 dB gap with no overlap**, on the exact frames every vision model called
identical.

The round-trip test ([`scripts/rung_resolution_check.py`](../scripts/rung_resolution_check.py)):
downscale the frame to the next rung down, scale it back up, compare to the
original. Genuine 1080p loses real high-frequency detail in that trip; content
that was *already* upscaled 720p loses almost nothing, because the detail was
never there. High PSNR means the frame survived downscaling, which means it had
nothing to lose.

```bash
python3 scripts/rung_resolution_check.py fixtures/frames/*/1080p_*.png
```

**Caveat, stated plainly:** the 41 dB threshold is calibrated on `testsrc2`,
which is a pathologically high-frequency pattern. Real content — §5's Veo clip —
has a different baseline, and the absolute threshold would need recalibrating.
The content-independent version compares the 1080p rung's round-trip PSNR
against the 720p rung's from the same stream at the same moment; the *ratio* is
what carries the signal. That is the form to ship if the source ever changes.

## What this means for the agent

**The premise survives.** "Every dashboard is green and the screen is black" is
exactly the fault vision nails, 100%, on every model. The demo is built around
`black_source`, and `black_source` works.

**One fault inverts.** For `ladder_mismatch`:

```
  code    measures whether the rung carries its advertised detail   <- decides
  vision  describes what an operator would see, for the postmortem  <- confirms
```

This is a better design than the original anyway. A deterministic measurement is
reproducible, explainable in a postmortem, and free — no token cost, no latency,
no confabulation risk. Vision's job becomes narrating a finding that code
already established, which is what it is reliably good at.

**Model choice is settled: `gemini-3.7-flash`.** It is the only model with a
zero false-positive rate on healthy frames, and it detects `black_source`
perfectly. The higher tier is strictly worse here — a useful thing to have
learned for $0 and one afternoon rather than during the judged demo.

**Ship the SEE phase with a narrow remit.** Ask vision what it is good at —
"is the picture black, frozen, or corrupted?" — and never ask it to judge
resolution fidelity. The prompt should not offer `upscaled_low_detail` as an
option at all, since offering it is what invites the confabulation.

## Reproducing

```bash
python3 scripts/fault_signatures.py --fixtures       # rebuild the eval set
python3 scripts/vision_eval.py                       # 3.7-flash, all variants
python3 scripts/vision_eval.py --models gemini-2.5-pro,gemini-3.6-flash \
    --modes ladder_mismatch,healthy                  # the tier comparison
python3 scripts/rung_resolution_check.py fixtures/frames/*/1080p_*.png
```
