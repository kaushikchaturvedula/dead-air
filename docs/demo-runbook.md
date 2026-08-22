# Demo runbook

Every timing here is measured on the local plant, not estimated. Where a number
is a worst case composed from parts rather than observed directly, it says so.

The short version: **start the sweep before you show the dashboard**, inject
`black_source`, and let the contradiction do the work.

---

## Before you record

```bash
make plant-up          # 20-42s cold; see "cold start" below
make verify            # 15/15 PASS, ~10s. Do not record until this is green.
make provision         # pushes dashboard + alert rule to Grafana Cloud
```

`make verify` is the gate. It now fails loudly on missing configuration rather
than reporting a warm-up delay, so a green run means the three signals are
genuinely live — not that they might be soon.

**You do not need `make tunnel`.** ngrok is only for the alert path, and the
demo below uses the sweep. Skipping it saves a terminal and a moving part.

### Windows to arrange

| window | what | why |
| --- | --- | --- |
| 1 | `make player` | the picture. This is the emotional beat. |
| 2 | Grafana dashboard `/d/deadair-plant` | content row on top, plant below |
| 3 | `make agent-sweep INTERVAL=30` | the agent's own output, live |
| 4 | Grafana Drilldown / AI Observability | the reflexive layer |

---

## THE ORDERING CONSTRAINT

**Start the sweep (window 3) BEFORE you show the dashboard (window 2).**

The content panels are fed by the agent's Stage 0 screen. With no sweep running
there are no samples, and Prometheus keeps serving the last value for ~5
minutes — so the panel that carries the whole thesis would sit there green over
a black stream.

That specific lie is now fixed: past 90s without a sample the panels read
**NOT WATCHING** in orange rather than a green PICTURE OK. But NOT WATCHING is
not the shot you want either. Start the sweep, wait one tick, then cut to the
dashboard.

---

## The three-minute story

**Use one fault: `black_source`.** It is the thesis and nothing else is close.
If you want a second beat, use `ladder_collapse` — it is the only other fault
with a visible player symptom (quality drops) and it demonstrates the
404-persistence discriminator. Do not attempt three.

| t | action | what the viewer sees | measured |
| --- | --- | --- | --- |
| 0:00 | (already running) | player sharp, timecode ticking. Dashboard all green. Terminal: `tick N: clear (yavg=125.6 in 1.3s)` | 1.3s/tick |
| 0:15 | `make black-source` | **player goes black, timecode still ticking** | instant |
| 0:20 | — | dashboard **still entirely green** — this is the point of the whole project | 6.4–8.1s propagation |
| 0:30 | — | terminal: `STAGE 0 SUSPECT -- black (yavg=17.1, dark_frames=1.0) in 1.28s` | 12.4–12.5s from injection at `INTERVAL=10`; ~20s typical, 39.5s worst at `INTERVAL=30` |
| 0:35 | cut to dashboard | **CONTENT row red. Every row beneath it green.** | — |
| 0:40 | — | terminal: `STAGE 1 vision -> black_frame (confidence 1.0, timecode 00:00:09.000)` | +5.4s |
| 0:45 | — | `STAGE 2 escalating to the full pipeline` | — |
| 0:45–3:20 | let it run | Phases 1→5 stream: scope fan-out, diagnosis, proposal, approval gate, postmortem | 169.2s measured for black_source |
| ~3:20 | — | `recovered` verdict, MTTR, viewer-minutes lost | — |

**Total arc ≈ 3m20s.** That is the whole video, so the investigation needs
cutting in the edit — see below for where.

### Where to cut

Phase 5 (record) is the longest phase at **91.0s mean, up to 149s**, and most of
it is `verify_recovery` deliberately waiting for the plant to settle. Cut there.
Phase 1 (scope, 66.0s mean) is the most visually interesting because four
specialists run concurrently — keep it.

| phase | mean | keep? |
| --- | --- | --- |
| 1 scope | 66.0s | keep — the fan-out looks like what it is |
| 2 see | 13.8s | keep — this is the vision beat |
| 3 diagnose | 27.7s | keep — the verdict |
| 4 act | 10.5s | keep — the approval gate is the trust story |
| 5 record | 91.0s | **cut heavily** |

### Restoring

```bash
make restore-source    # the ladder rebuilds in ~60-90s
```

ABR recovery is gradual: players step back up the ladder over a minute or two.
If you are doing a second take, wait for it or the plant reads as degraded.

---

## What each screen shows, and what to say

**Player.** Black, with the timecode still ticking. Say: *the encoder is
perfectly healthy. It is encoding, packaging and delivering black.*

**Dashboard.** `encoder_fps` 30. `dropped_frames` 0. `rebuffer_ratio` 0.0002
against a 0.02 alert threshold — **80× below the line that would page anyone.**
Cache hit ratio ~98%. Every delivery signal green. Say: *nothing here is wrong.
Nothing here is lying. They are all answering a different question.*

**Agent terminal.** Stage 0 is arithmetic — no model, 1.3s. Say: *deciding
whether a frame is black is not a job for a language model. The model is for
saying what KIND of wrong it is, and that question is only worth asking once
something is already suspect.*

**AI Observability.** The agent's own traces, token cost and tool calls, in the
same Grafana stack it is investigating. Say: *the agent is itself an observable
service.* Run `make agent-profile` afterwards if you want the phase breakdown on
screen.

---

## Cold start

Measured on a full teardown — no images, no volumes, build cache pruned:

| | |
| --- | --- |
| `make plant-up` | **20–42s** |
| containers healthy | 10/10 |
| ladder serving | t+2s, 6 segments/rung |
| `make verify` | 15/15 at ~t+2min |

`plant-up` now blocks until the encoder reports four active rungs, the origin
serves `master.m3u8`, and every edge can proxy it — readiness means *serving*,
not *started*. The 42s figure is with `grafana/alloy` and `mcp-grafana`
(780MB) already pulled; a genuinely fresh machine pays that too.

Telemetry needs **~90–150s** after `plant-up` before Mimir is trustworthy. The
eval harness waits 150s for exactly this reason. Do not start recording inside
that window.

---

## Known risks on camera

1. **Vision has a 120s timeout and 3 retries now, but the network is still the
   network.** If Vertex is unreachable at the moment Stage 0 fires, you get a
   loud failure rather than a hang — retry the take.
2. **Phase 5 is slow and always will be.** It waits on purpose. Plan the cut.
3. **`make screen-calibrate` needs ffmpeg on the host**, like everything else
   that touches pixels. `ffmpeg -version` before you start.
4. **A second take needs a settled plant.** Give ABR 120s after
   `make restore-source`.
5. **The content panel reads NOT WATCHING when the sweep is not running.** That
   is correct behaviour, not a bug — but do not film it.

---

## If something goes wrong mid-take

| symptom | check |
| --- | --- |
| content panel empty or orange | is `make agent-sweep` still running? |
| terminal silent after `STAGE 0 SUSPECT` | vision call failing — it will error within 120s, not hang |
| dashboard panels all "No data" | `make verify` — probably a config or credential issue, not the plant |
| agent diagnoses the wrong fault | `make chaos-status` — is more than one fault injected? |
| everything green but the picture is black | that is the demo working |

---

## Reference

- `docs/content-screen.md` — Stage 0's calibration and the timecode trap
- `docs/agent-performance.md` — where the time and tokens go
- `docs/audit-2026-08-21.md` — 30 verified findings, and what was not verified
- `docs/plant.md` — the fault menu and measured signatures
