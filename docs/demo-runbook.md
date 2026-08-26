# Demo runbook

Every timing here is measured on the local plant, not estimated. Where a number
is a worst case composed from parts rather than observed directly, it says so.

The short version: **start the sweep before you show the dashboard**, inject
`black_source`, and let the contradiction do the work.

## What you need open

**Three terminals and a browser.** Referred to below as T1, T2, T3.

| | holds | why |
| --- | --- | --- |
| **T1** | setup, then idle | `plant-up`, `verify`, `provision` |
| **T2** | `make agent-sweep` — **runs for the whole take** | the agent's live output |
| **T3** | free, for `make black-source` / `make restore-source` | T2 is occupied |
| browser tab A | the player | the picture |
| browser tab B | the Grafana dashboard | content row vs plant rows |
| browser tab C | Grafana Drilldown → Traces, service `deadair-agent` | the reflexive layer |

---

## Before you record

Run in **T1**, in this order. The wait is not optional.

```bash
make plant-up          # 20-42s cold. Blocks until encoder/origin/edges are healthy.

# WAIT ~2 MINUTES HERE.
# plant-up returns as soon as the plant is SERVING, but Alloy has not yet
# delivered enough to Mimir for the checks below to pass. Running verify
# immediately fails at the mimir hop and exits 1 — that is the pipeline being
# honest, not a broken plant.

make verify            # must print 15/15 PASS. Do not record until it does.
make provision         # pushes dashboard + alert rule; PRINTS THE DASHBOARD URL
```

`make provision` ends with the dashboard link — open that in **browser tab B**.
It is `/d/deadair-plant` on your Grafana stack.

**If `make verify` fails**, it names the broken hop and stops there rather than
reporting "no data". Two common ones:

- `FAIL config: N required variable(s) missing` — your `.env` is incomplete.
  Copy the missing keys from `agents/grafana_probe/.env.example`.
- `FAIL mimir` right after `plant-up` — you did not wait. Give it another minute.

**You do not need `make tunnel`.** ngrok is only for the alert path, and this
demo uses the sweep. Skipping it saves a terminal and a moving part.

---

## THE ORDERING CONSTRAINT

**Start the sweep (T2) BEFORE you open the dashboard (tab B).**

The content panels are fed by the agent's Stage 0 screen. With no sweep running
there are no samples, and Prometheus keeps serving the last value for ~5
minutes — so the panel that carries the whole thesis would sit there green over
a black stream.

That specific lie is fixed: past 90s without a sample the panels read
**NOT WATCHING** in orange rather than a green PICTURE OK. But NOT WATCHING is
not the shot you want either.

```bash
# T2 — leave this running for the entire take
make agent-sweep INTERVAL=10
```

`INTERVAL=10` is now the **default**, so a bare `make agent-sweep` gives the
demo cadence. It used to default to 300s — a five-minute tick that would have
silently made detection look terrible on camera. Pass `INTERVAL` only to
override.

Then wait before cutting to the dashboard. Two things have to happen, and only
the first is visible in T2:

| | |
| --- | --- |
| two `clear` ticks in T2 | ~13s (first tick lands at ~1.3s, second at ~12.6s) |
| that sample reaching Mimir and becoming queryable | a further ~20-30s |

So give it **~40s from starting the sweep** before the content panels show data.
If you cut over at 15s the panels are still empty and you will think something
is broken. They are not — the sample is in flight.

### Why `INTERVAL=10`

Stage 0 costs ~1.3s, so a 10s tick is ~1.3s of work per 11.3s cycle — about
**12% duty cycle**, and **zero model calls** while the plant is healthy. It buys
the headline number: 12.4 / 12.5 / 12.4s from injection to flag, measured across
three runs, flat to a tenth of a second. A 30s tick adds up to 20s of pure
quantisation for nothing.

---

## Pre-roll checklist

Before you hit record, all of this should be true:

- [ ] `make verify` printed **15/15 PASS**
- [ ] T2 shows at least two `tick N: clear (yavg=125.x in 1.3s)` lines, and the
      sweep has been running **~40s** so the panels have data
- [ ] player (tab A) shows a sharp picture with the timecode ticking
- [ ] dashboard (tab B): content row **PICTURE OK** green, plant rows green
- [ ] `make chaos-status` in T3 shows `mode: none` everywhere

---

## The three-minute story

**Use one fault: `black_source`.** It is the thesis and nothing else is close.
If you want a second beat, use `ladder_collapse` — the only other fault with a
visible player symptom (quality drops), and it demonstrates the 404-persistence
discriminator. Do not attempt three.

During Stage 2 the sweep keeps screening on a background thread, so the content
panel stays live for the whole investigation. You will see one extra line in T2
when the verdict first changes, and a summary when the investigation ends:

```
    [monitor] content now 'black' (yavg=17.08) -- panel stays live during the investigation
    content monitor published 28 screen(s) during the investigation, largest gap 13s -- panel stayed live
```

That summary is measured, not asserted: if the largest gap ever exceeds the 90s
budget it says so instead.

| t | you do | what the viewer sees | measured |
| --- | --- | --- | --- |
| 0:00 | — | player sharp, timecode ticking. Dashboard all green. T2: `tick N: clear (yavg=125.6 in 1.3s)` | 1.3s/tick |
| 0:15 | **T3:** `make black-source` | **player goes black, timecode still ticking** | takes effect at once |
| 0:23 | — | dashboard **still entirely green** — the point of the whole project | 6.4–8.1s propagation |
| 0:28 | — | T2: `STAGE 0 SUSPECT -- black (yavg=17.1, dark_frames=1.0) in 1.28s` | **12.4 / 12.5 / 12.4s** from injection (n=3) |
| 0:30 | **cut to tab B** | **CONTENT row red. Every row beneath it green.** | — |
| 0:33 | — | T2: `STAGE 1 vision -> black_frame (confidence 1.0, timecode ...)` | +5.4s |
| 0:34 | — | T2: `STAGE 2 escalating to the full pipeline` | ~18s injection→classified |
| 0:34–3:25 | let it run | Phases 1→5 stream: scope fan-out, vision, diagnosis, proposal, approval gate, postmortem | 169s measured; 169–245s range |
| ~3:25 | — | `recovered` verdict, viewer-minutes lost, postmortem | — |

**Total arc ≈ 3m10s, up to ≈ 4m20s** on a slower run. That is the whole video,
so the investigation gets cut in the edit — see below.

> **On camera, do not claim an MTTR improvement.** The agent prints an
> `mttr_seconds` field and it is real, but there is no delivery-telemetry
> baseline to compare it against: under `black_source` no threshold is crossed,
> so no alert ever fires. The honest line is *"never detected"* versus
> *"classified in eighteen seconds"*. See `docs/devpost.md`.

### Where to cut

These phase timings are properties of the investigation, not of the sweep
cadence, so they do not change with `INTERVAL`.

| phase | mean | keep? |
| --- | --- | --- |
| 1 scope | 66.0s | keep — four specialists running concurrently looks like what it is |
| 2 see | 13.8s | keep — this is the vision beat |
| 3 diagnose | 27.7s | keep — the verdict |
| 4 act | 10.5s | keep — the approval gate is the trust story |
| 5 record | 91.0s | **cut heavily** — mostly `verify_recovery` waiting on purpose |

A healthy-plant run is ~197s end to end; a `black_source` run 169–245s.

### Restoring, between takes

```bash
# T3
make restore-source    # ladder rebuilds in ~60-90s
make chaos-status      # confirm mode: none everywhere
```

Then **wait ~120s** before the next take. ABR recovery is gradual — players step
back up the ladder over a minute or two, and a still-recovering plant reads as
degraded. Watch T2 for `clear (yavg=125.x)` before rolling again.

---

## What each screen shows, and what to say

**Player.** Black, timecode still ticking. Say: *the encoder is perfectly
healthy. It is encoding, packaging and delivering black.*

**Dashboard.** `encoder_fps` 30. `dropped_frames` 0. `rebuffer_ratio` ~0.00025
against a 0.02 alert threshold — **roughly 80× below the line that would page
anyone** (it varies a little per run; the point is the order of magnitude).
Cache hit ratio ~98%. Every delivery signal green. Say: *nothing here is wrong.
Nothing here is lying. They are all answering a different question.*

**Agent terminal (T2).** Stage 0 is arithmetic — no model, 1.3s. Say: *deciding
whether a frame is black is not a job for a language model. The model is for
saying what KIND of wrong it is, and that is only worth asking once something is
already suspect.*

**AI Observability (tab C).** The agent's own traces, token cost and tool calls,
in the same Grafana stack it is investigating. Say: *the agent is itself an
observable service.* Run `make agent-profile` in T1 afterwards for the phase
breakdown on screen.

---

## Cold start

Measured on a full teardown — no images, no volumes, build cache pruned:

| | |
| --- | --- |
| `make plant-up` | **20–42s** |
| containers healthy | 10/10 |
| ladder serving | t+2s, 6 segments/rung |
| `make verify` | 15/15, but only from **~t+2min** |

`plant-up` blocks until the encoder reports four active rungs, the origin serves
`master.m3u8`, and every edge can proxy it — readiness means *serving*, not
*started*. The 42s figure is with `grafana/alloy` and `mcp-grafana` (780MB)
already pulled; a genuinely fresh machine pays that too.

Telemetry needs **~90–150s** after `plant-up` before Mimir is trustworthy. This
is the single most likely thing to trip you up, because everything *looks* ready.

---

## Known risks on camera

1. **Vision is bounded at 150s TOTAL, and that bound was earned.** A per-request
   timeout is not enough: httpx resets its read timeout on every chunk, so a
   slowly-dribbled response never trips it. Measured during a six-case eval, one
   vision call ran **1831 seconds** and then succeeded, dragging that
   investigation to 2093.8s — past the 900s run ceiling, which could not fire
   because ADK runs sync tools on the event loop. There is now a hard wall-clock
   deadline in a worker thread. If it trips you get `vision_unavailable` and a
   loud error, not a hang — retry the take.
2. **Phase 5 is slow and always will be.** It waits on purpose. Plan the cut.
3. **ffmpeg must be on the host.** `ffmpeg -version` before you start —
   everything that touches pixels shells out to it.
4. **A second take needs a settled plant.** 120s after `make restore-source`.
5. **The content panel reads NOT WATCHING when the sweep is not running.**
   Correct behaviour, not a bug — but do not film it.

---

## If something goes wrong mid-take

| symptom | check |
| --- | --- |
| content panel orange / NOT WATCHING | is `make agent-sweep` still alive in T2? Check the monitor summary line for the largest gap. |
| panel red on a healthy plant between takes | should not happen: the panels are pinned to the swept region. If it does, a stale series is in Prometheus's ~5min lookback -- wait it out or re-run `make provision`. |
| content panel empty, "No data" | sweep never started, or started under 90s ago |
| T2 silent after `STAGE 0 SUSPECT` | vision is slow — bounded at 150s, returns `vision_unavailable` rather than hanging |
| dashboard panels all "No data" | `make verify` in T1 — config or credentials, not the plant |
| agent diagnoses the wrong fault | `make chaos-status` — is more than one fault injected? |
| second take reads degraded | ABR still recovering; wait for `clear (yavg=125.x)` in T2 |
| everything green but the picture is black | that is the demo working |

---

## Reference

- [architecture.md](architecture.md) — the whole system in one diagram
- [content-screen.md](content-screen.md) — Stage 0's calibration and the timecode trap
- [agent-performance.md](agent-performance.md) — where the time and tokens go
- [devpost.md](devpost.md) — the writeup, and the honest framing of the timing claim
- [audit-2026-08-21.md](audit-2026-08-21.md) — 30 verified findings, and what was not verified
- [plant.md](plant.md) — the fault menu and measured signatures
