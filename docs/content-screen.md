# The fast content screen

Detection used to cost a model. Every sweep tick ran a full five-phase
investigation, so the cadence floor was the pipeline's own runtime — 543s — and
"DEAD AIR catches it in seconds" was not true at any interval.

Deciding whether a frame is black is arithmetic. A model is only needed to say
what *kind* of wrong it is, which is worth paying for and only worth asking once
something is already suspect. So detection and diagnosis were split:

| stage | what it decides | cost | runs |
|---|---|---|---|
| **0** | is this picture wrong at all | **1.30s**, no model | every tick |
| **1** | what kind of wrong (vision) | 5.4s | only when Stage 0 says suspect |
| **2** | why, and what to do (five phases) | 543s | only on a confirmed finding |

## The timecode trap

Our black frames deliberately carry a burned-in timecode, so vision can confirm
the encoder is alive rather than stopped. That breaks both obvious detectors.

Measured on a real black segment against a healthy one:

| | YLOW | YAVG | YHIGH | YMAX |
|---|---|---|---|---|
| black_source | 16 | **17.14** | **16** | 236 |
| healthy | 41 | **126.02** | **210** | 255 |

**YMAX is useless.** A white timecode on a fully black picture pins it to 236 —
five units off the healthy value. Any max-luma detector reads a blacked-out
channel as fine.

**blackdetect is luck.** Its `pic_th` defaults to 0.98, requiring 98% of pixels
below threshold. It happens to trip on our current timecode, but that is a
property of this overlay's size. A larger clock, a station logo, or a slate
would silently stop it tripping — the detector failing exactly when the picture
is most obviously wrong, and failing by returning *nothing*, which reads as
health.

So YAVG (mean luma, ~7x separation) and YHIGH (90th percentile, which sits on
the black floor of 16 because the timecode covers under 10% of the frame) are
measured directly instead of inferred from a pixel-fraction heuristic.

Freeze is a separate axis: `freezedetect` writes to the ffmpeg **log**, not to
frame metadata, so ffprobe's `frame_tags` never surface it. It is read from
stderr in the same pass that produces YAVG on stdout — one process, two streams,
no second decode.

## Calibration

`make screen-calibrate` — 69 fixture stills, 6 states x 4 rungs. (69, not 102:
`ladder_collapse` contributes 9 because its 1080p rung does not exist, which is
the fault.)

```
  measured YAVG by state (the separator)
    black_source        min=  17.05   mean=  17.08   max=  17.12    n=12
    edge_latency        min= 125.47   mean= 125.57   max= 125.62    n=12
    healthy             min= 125.48   mean= 125.55   max= 125.60    n=12
    ladder_collapse     min= 125.55   mean= 125.56   max= 125.57    n= 9
    ladder_mismatch     min= 125.54   mean= 125.56   max= 125.59    n=12
    segment_gap         min= 125.52   mean= 125.54   max= 125.57    n=12

    overall: 69/69 correct (100.0%)
    black_source detection : 100.0%   (bar: 100%)
    false positive rate    :   0.0%   (bar: 0%)
```

The threshold sits at 40, inside a **100-unit empty gap** between 17.12 and
125.47. It was not tuned until the table looked right — there is nothing to tune
it against, because no fixture lands between the two clusters.

`ladder_mismatch` is deliberately expected-clear here. It is a resolution fault,
invisible to luma; the deterministic rung check owns it (`docs/vision-spike.md`),
and vision scores 0% on it at every tier.

Stills cannot exercise `freezedetect` — a single frame has no temporal
dimension — so `--live` screens real segments from the running plant.

## Measured latency

Every number below is measured on the running plant, not derived.

**Stage 0 per tick**, n=12 back to back against a live edge:

```
min 1.22s   median 1.29s   mean 1.30s   max 1.41s
```

**Sweep interval floor ≈ 5s.** Not bounded by the screen. Segments are 4s, so at
a ~3.3s cadence 2 of 12 ticks re-screened a segment already screened. Below the
segment duration the extra ticks buy nothing.

**Plant propagation, the floor DEAD AIR cannot beat** — injection until the
fault is visible in the newest segment published at the edge, polled
continuously with no interval, n=3:

```
6.4s   7.5s   8.1s
```

That is the encoder finishing the current 4s segment, the origin taking it, and
the edge picking it up. No detector of any speed sees it sooner.

**Injection to Stage 0 flag**, `--interval 10`, n=3:

```
12.5s   12.5s   12.4s
```

Flat to 0.1s, because propagation dominates and the poll quantizes it.

**Worst case**, composed from the measured parts rather than observed once:

```
propagation 8.1  +  interval 10  +  screen 1.4  =  19.5s to a Stage 0 flag
                                    + vision 5.4 =  24.9s to a classified finding
```

At the 5s floor that worst case is **14.5s** to a flag. The honest ceiling for
"seconds" is *tens of seconds*, and the largest term is the plant's own
segment pipeline, not the agent.

**Cost inversion.** A healthy plant previously made 10–19 vision calls per hour
and ran a full investigation every tick. It now makes **zero** — Stage 1 only
fires behind a Stage 0 suspect, and Stage 0 does not call a model.

## In recovery, too

`verify_visual_recovery` uses Stage 0 as well. "Is it still black" is
arithmetic, and recovery now requires the screen, vision, and the rung check to
*all* agree before an incident closes — so a model cannot talk itself into
closing an incident against a measurably black frame.
