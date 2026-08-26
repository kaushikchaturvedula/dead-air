# DEAD AIR

**An autonomous broadcast operations agent for live video streaming.**

> An active build for the Google Cloud "Agentic Cinema" hackathon (Grafana
> track, due 7 Sep 2026).
>
> **Working end to end, locally:** the full streaming plant (encoder → 3
> regional edges → 201-session viewer fleet), all five of the brief's fault
> modes, all three observability signals (Mimir · Loki · Tempo), and **all five
> agent phases** — scope, see, diagnose, human-gated act, and record with
> verified recovery.
>
> **Measured:** 6/6 correct across all five faults plus a healthy control,
> through the complete five-phase agent. 69/69 on the content screen's
> calibration corpus, 100% detection and 0% false positives. Black frame on air
> to a classified fault in **~18s** at a 10-second sweep; full closure with
> verified recovery in 3.1–4.4 minutes. The delivery-telemetry baseline for that
> fault is not slower — it is **never**: no threshold is crossed, so no alert
> can fire.
>
> **Cloud:** steps 1 and 2 executed and measured, then torn down —
> [docs/cloud-deployment-risk.md](docs/cloud-deployment-risk.md). The viewer
> fleet has not yet moved into GCP, which that document explains is a
> correctness prerequisite for any regional fault demo, not polish.

## The thesis: every dashboard is green and the screen is black

Delivery telemetry measures whether *bytes* arrived. It cannot see whether those
bytes contain a *picture*.

When an encoder's input goes black, or its source freezes on a single frame, the
segments keep flowing on schedule. Bitrate is nominal. Segment latency is flat.
Error rates are zero. Every panel in the dashboard is green — and every viewer is
staring at a black rectangle. Conventional observability is structurally blind to
this class of failure, because the failure is in the content, not the transport.

DEAD AIR closes that gap. It correlates delivery telemetry with the actual
delivered pixels, so a healthy-looking pipeline carrying dead air gets caught.

## How it works

When viewer quality-of-experience degrades, a [Google ADK](https://google.github.io/adk-docs/)
agent powered by Gemini:

1. **Queries Grafana Cloud** through a self-hosted [Grafana MCP
   server](https://github.com/grafana/mcp-grafana), across metrics, logs, and
   traces.
2. **Pulls the actual video segment** being delivered to viewers.
3. **Inspects the frame with Gemini vision** — catching black frames, frozen
   sources, and other content failures that delivery telemetry reports as
   perfectly healthy.
4. **Proposes a remediation**, gated on human approval. Nothing acts on the
   plant without a person saying yes.
5. **Re-queries to verify recovery**, then **annotates the Grafana dashboard**
   with a postmortem.

## Architecture

The whole system — plant layers, both triggers, the three-stage cascade, the
five phases and the reflexive loop — is one diagram in
**[docs/architecture.md](docs/architecture.md)**, along with the list of what is
decided by code rather than by the model.

The short version:

```
  L1 encoder ──▶ L2 edges ──▶ L3 viewer fleet
                     │  metrics · logs · traces (Alloy, cardinality-guarded)
                     ▼
              Grafana Cloud ──▶ alert ──┐
                                        ├──▶  five-phase ADK agent
  confidence sweep ──▶ Stage 0 screen ──┘      scope · see · diagnose
   (every 30s)         ~1.3s, no model         act (human-gated) · record
                            │
                            └─▶ Stage 1 vision, only when Stage 0 says suspect
```

Two triggers, because the fault this project is named after **fires no alert**:
under `black_source`, `rebuffer_ratio` reads ~0.0001 against a 0.02 threshold.
An alert-driven agent sleeps through it.

## Tech stack

Google Cloud AI tooling only, by contest rule.

| Layer | Choice |
| --- | --- |
| Agent framework | Google ADK (`google-adk`) |
| Model | Gemini via Vertex AI (`google-genai`) |
| Observability | Grafana Cloud |
| MCP server | [grafana/mcp-grafana](https://github.com/grafana/mcp-grafana), self-hosted via Docker |
| Tool transport | MCP over streamable HTTP |

No LangChain, no LangGraph, no non-Google agent framework, and no OpenAI,
Anthropic, HuggingFace, or Whisper models anywhere in the project.

The MCP server is self-hosted rather than Grafana's hosted `mcp.grafana.com`
endpoint deliberately: the hosted endpoint only authenticates through an
interactive OAuth 2.1 browser handshake, which a headless agent woken by an
alert webhook can never complete. Self-hosting authenticates with a Grafana
service-account token instead.

## Setup

Requires Python 3.10+, Docker, **ffmpeg on the host**, and a Google Cloud
project with Vertex AI enabled.

`ffmpeg` is not optional and is not supplied by the venv or by Docker Desktop:
the Stage 0 content screen, the frame grabs and the rung-resolution measurement
all shell out to it on the host. Stock macOS and Ubuntu do not ship it.

```bash
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian / Ubuntu
ffmpeg -version              # must print a version
```

```bash
git clone https://github.com/kaushikchaturvedula/dead-air.git
cd dead-air

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp agents/grafana_probe/.env.example agents/grafana_probe/.env
# then fill it in. Every key is documented in the example file; `make verify`
# below fails loudly and by name if any are missing.

gcloud auth application-default login
```

### See it work

The shortest path from a fresh clone to watching the agent catch dead air.
**Three terminals**, called T1/T2/T3 here.

```bash
# ---- T1: bring up the plant ----
make plant-up          # 20-42s. Blocks until encoder/origin/edges are healthy.

# WAIT ~2 MINUTES. plant-up returns as soon as the plant is SERVING, but Alloy
# has not yet delivered enough to Mimir. Running verify immediately fails at the
# mimir hop — that is the pipeline being honest, not a broken plant.

make verify            # must print 15/15 PASS before anything below is meaningful
make provision         # pushes the dashboard + alert rule; prints the dashboard URL
```

```bash
# ---- T2: START THIS BEFORE OPENING THE DASHBOARD ----
make agent-sweep INTERVAL=10
```

The content panels are fed by this sweep. With nothing running there are no
samples, and Prometheus serves the last value for ~5 minutes — so the panel
carrying the whole thesis would read green over a black stream. Start the sweep,
wait for two `clear` ticks, *then* open the dashboard.

```bash
# ---- browser ----
make player            # the picture

# ---- T3: break it, then fix it ----
make black-source      # the screen goes black; every delivery metric stays green
make restore-source    # and back. Give ABR ~120s to settle before a second run.
```

Watch T2: Stage 0 flags the black frame in **~12.5s** with no model call at all,
vision classifies it ~5s later, and the five-phase investigation runs from there.
The full beat sheet, with measured timings and what to say over each screen, is
in **[docs/demo-runbook.md](docs/demo-runbook.md)**.

<details>
<summary>How this was bootstrapped — the day-one MCP connectivity probe</summary>

Before any of the above existed, the first thing built was a bare ADK agent that
did nothing but list the Grafana MCP server's tools, to prove a headless agent
could authenticate at all. It is still in the repo and still works:

```bash
cd agents
adk web
```

Open the URL it prints, pick the `grafana_probe` agent, and ask it about your
stack. The MCP server authenticates with the service-account token — no browser
handshake involved, which is the whole reason it is self-hosted.

This is a connectivity check, not the product. The tools it exposes are
catalogued in [docs/mcp-tool-inventory.md](docs/mcp-tool-inventory.md).

</details>

## The plant

The plant is the streaming system DEAD AIR observes. It is built bottom-up as a
telemetry pipe first, video second — there is no point rendering pixels into a
pipe that cannot carry a number.

**Step 1 — telemetry pipe (working).** A synthetic emitter feeds Grafana Alloy,
which remote-writes to Grafana Cloud Mimir, where a dashboard panel and an alert
rule watch it. Crossing the threshold turns Grafana red and delivers a webhook
to a local receiver — the same trigger that will later wake the agent.

```bash
make plant-up                # emitter + Alloy + webhook receiver + MCP server
make tunnel                  # (separate shell) expose the webhook publicly
make provision               # dashboard + alert rule + contact point
make verify                  # assert every hop of the pipe is delivering
make set VALUE=95            # cross the threshold -> Grafana goes red
make watch                   # see the alert delivery arrive
make set VALUE=10            # back to healthy -> resolved delivery
```

`make verify` checks each hop in order and stops at the first break, so a
failure names the broken hop instead of just reporting "no data".

**Cardinality.** The stack is on the Grafana Cloud free tier (~10k active
series), and a streaming plant is the classic way to blow that budget: one
series per viewer session, per request, or per 4-second segment multiplies every
metric by the number of live viewers. The collector strips those labels before
remote_write ([plant/alloy/config.alloy](plant/alloy/config.alloy)) — aggregate
the dimension, don't label by it. The emitter publishes a deliberate canary
series tagged `session_id` so `make verify` proves the guard is still stripping
rather than merely asserting the label is absent.

**Step 2 — L1 source + encoder (working).** ffmpeg generates a 4-rung ABR HLS
ladder (1080p/5M · 720p/3M · 480p/1.5M · 360p/800k, 4s segments) with burned-in
timecode, served by an nginx origin. `encoder_fps`, `dropped_frames` and
`packager_segment_lag` flow to Mimir; origin access logs flow to Loki.

```bash
make player     # hls.js player against the local origin
make ladder     # show the ABR ladder being served
make frame      # grab the current frame as a PNG
```

**The demo, runnable now.** Brief §5's headline failure works at L1 already:

```bash
make black-source     # swap the encoder input to color=black
make frame            # ...the picture is gone
make restore-source
```

With the source black, `encoder_fps` holds 30.0, `dropped_frames` stays 0,
`encoder_up` stays 1 and segment lag keeps its normal sawtooth. Every delivery
metric is green while the screen is black — the thesis, on demand, in about
fifteen seconds.

**Step 3 — L2 CDN edges (working, local).** Three caching reverse proxies
standing in for `us-east1`, `europe-west1` and `asia-south1`, exporting cache
hit ratio, segment status, TTFB histograms and origin shield misses — with
fault injection per region.

```bash
make edges                                          # per-region state
make chaos REGION=europe-west1 MODE=edge_latency    # degrade one region
make chaos-clear
```

Measured p95 segment TTFB with one region degraded: **europe-west1 981 ms vs
us-east1 4.8 ms and asia-south1 4.9 ms** — a ~200× differential in exactly one
region.

Latency injection is application-level, not `tc netem`, deliberately: netem
needs `NET_ADMIN`, which Cloud Run does not grant, so a netem-based mechanism
would have to be rewritten the moment the edges deploy.

**Step 4 — L3 viewer fleet (working).** 201 modeled sessions on a playback
clock running `buffer += segment_duration - download_time`, with ABR and
per-device buffer sizes. This is where `rebuffer_ratio` is born — a client-side
signal no CDN metric can produce, because only a player knows its buffer
stalled.

Degrade one region and the fleet reports it, stratified by device:

| Device class | rebuffer_ratio | |
| --- | --- | --- |
| mobile | 0.191 | smallest buffer, stalls first |
| desktop | 0.123 | |
| tv | 0.048 | largest buffer, most resilient |

All in `europe-west1`; the other two regions stayed at `0.000`. The real alert
— `rebuffer_ratio > 0.02 for 2m, by region` — fires for that region alone and
delivers a webhook carrying `region`, which is what the agent will key its
investigation off.

**The cardinality split, both halves proven.** Metrics aggregate by `region`
and `device_class` and never by session; per-session QoE beacons go to Loki
with `session_id` in the log line, never as a label. A canary carrying
`session_id`, `region` and `device_class` proves the guard strips the first and
keeps the other two. Whole plant: **172 active series** against a ~10k budget.

**The fault menu (complete).** All five of §5's faults run end to end, each with
a distinct, measured telemetry signature — the answer key the agent is graded
against ([docs/plant.md](docs/plant.md#the-fault-menu--ground-truth-for-agent-week)):

| Fault | rebuffer | bitrate | 4xx | tell |
| --- | --- | --- | --- | --- |
| `edge_latency` | 0.44, **one region** | drops, one region | none | regional, not plant-wide |
| `segment_gap` | ~0.05, all regions | unchanged | **sustained** | packager fault |
| `ladder_collapse` | **zero** | 3.6 → 2.0 Mbps | transient only | manifest 4 → 3 rungs |
| `black_source` | zero | unchanged | none | **nothing moves** |
| `ladder_mismatch` | zero | unchanged | none | **nothing moves** |

The last two are invisible in every metric, log and trace — and that is the
point. `black_source` shows a black frame with the timecode still running;
`ladder_mismatch` shows a visibly soft 1080p rung carrying upscaled 720p detail
at full bitrate. Only frame inspection catches either.

```bash
make chaos MODE=black_source
make chaos MODE=edge_latency REGION=europe-west1
make chaos-status && make chaos-clear
```

**All three signals are live.** Metrics → Mimir, logs → Loki, traces → Tempo,
with `traceparent` carried into the origin access log so a log line joins to its
trace. A captured trace reads *viewer 7.8ms → edge 5.9ms (cache MISS) → origin
3.5ms* — causality neither metrics nor logs can express, and what the agent's
Phase 1 fans out across.

**Vision spike — the premise holds, with one correction.** Before building the
agent, the riskiest assumption was tested directly: can Gemini actually see
these faults? Full results in [docs/vision-spike.md](docs/vision-spike.md).

| Fault | Vision | Who decides |
| --- | --- | --- |
| `black_source` | **100%, every model and variant** | vision |
| `ladder_mismatch` | **no model separates it from healthy** | code decides, vision confirms |

`black_source` — the fault the whole demo is built around — is detected
perfectly and never confused with a healthy frame. `ladder_mismatch` is not:
the flash tiers call everything crisp, `gemini-2.5-pro` calls everything
upscaled (flagging **9/9 healthy frames** as faulty), and cross-rung pairing
makes it *worse* because the models confabulate the comparison in fluent,
confident, exactly-backwards prose.

That fault is trivially measurable in code, though — a downscale/upscale
round-trip separates the same frames by **10 dB with no overlap**
([`scripts/rung_resolution_check.py`](scripts/rung_resolution_check.py)). So for
`ladder_mismatch` the architecture inverts: code decides, vision narrates. Model
choice is settled at `gemini-3.7-flash`, the only tier with a zero false-positive
rate on healthy frames.

## The agent — all five phases

**6/6 correct** across every fault plus a healthy control, through the complete
five-phase pipeline — scope, see, diagnose, human-gated act, and record with
verified recovery. Reproduce with `make diagnose-eval`.

```bash
make agent REGION=us-east1     # run once
make agent-watch               # REACTIVE: wake on every firing alert
make agent-sweep               # PROACTIVE: confidence monitor on a timer
make agent-tools               # show the pinned MCP subset
make diagnose-checks           # score all 6 cases, deterministic, no model
make diagnose-eval             # drive all 6 through the full agent
make agent-profile             # where a run's time went, from its own traces
```

**Two triggers, one pipeline.** Reactive (an alert fires) and proactive (a
scheduled content sweep) both feed the same phases. The sweep is first-class,
not a fallback: `black_source` moves no metric, so **no alert can ever fire for
it** — after ten minutes of black, all three alert instances read `Normal`. An
alert-driven agent sleeps through the fault this project exists to catch.

A confidence monitor that is always watching is also what real broadcast
operations run, which makes the thesis self-consistent: the agent finds dead air
*because it is looking*, not because telemetry told it to.

A `SequentialAgent` phase lifecycle with a `ParallelAgent` fan-out inside Phase
1 — four specialists querying Mimir, Loki, Tempo and dashboards concurrently
through the self-hosted MCP server, then a synthesiser emitting a
schema-validated `IncidentScope`. Phase 2 fetches the real segment from the
affected edge and emits a `VisualFinding`. Details in [docs/agent.md](docs/agent.md).

**The agent sees 12 of the 73 MCP tools**, no specialist more than 4. 73 tool
declarations degrades function-calling accuracy, and pinning makes the search
space a reviewable design decision rather than whatever the MCP server happens
to expose that week.

**Verified on three different faults:**

| Injected | Phase 1 narrowed to | Phase 2 found | Correct |
| --- | --- | --- | --- |
| `black_source` | `black_source`, `ladder_mismatch` | `black_frame`, timecode **advancing** | ✅ |
| `edge_latency` | `edge_latency` (single region) | pixels healthy, no content fault | ✅ |
| `ladder_mismatch` | — | vision said **healthy**; code measured ratio **1.199** → `ladder_mismatch` | ✅ |

That last row is the spike's finding paying off. Vision looked straight at the
upscaled frame and called it healthy at confidence 1.0 — and the agent still
got the right answer, because resolution is decided by
[`check_rung_resolution`](scripts/rung_resolution_check.py) and vision is never
asked. The rung check is content-independent: it compares the 1080p rung's
downscale round-trip against the 720p rung's from the same stream, so the ratio
carries the signal and no `testsrc2` calibration is baked in.

**Phase 3 — DIAGNOSE.** Gemini ranks which hypotheses are worth testing; **code
runs the confirming checks and computes the verdict; evidence decides.**
[`signatures.py`](agents/dead_air/signatures.py) encodes §5's five faults as
deterministic predicates that reproduce the table measured by
[`fault_signatures.py`](scripts/fault_signatures.py) — that harness stays the
ground truth. Verified **6/6** on the live plant: all five faults plus a healthy
control.

The `ladder_collapse` vs `segment_gap` discriminator is computed, not reasoned
about. Both produce 404s, so presence separates nothing — persistence does:

| `fourxx_status` | Meaning | Fault |
| --- | --- | --- |
| `ongoing` | segments still being deleted | `segment_gap` |
| `stopped` | burst died out as players re-read the manifest | `ladder_collapse` |

And missing evidence is never elimination: a fault whose required checks cannot
be evaluated is **`unconfirmable`**, kept in a separate field from `ruled_out`.
Conflating the two is how an agent reports false certainty.

**Step 5 — cloud deployment (not started).** GCE origin, then three Cloud Run
edges. Nothing is blocked on it — all four layers run locally today.

### Everything stays local until it has to move

The plant runs entirely on Docker today. That is not just cost control: an edge
in `europe-west1` needs a publicly reachable origin, so deploying L2 to Cloud
Run silently requires L1 on GCE first. Local has no such ordering constraint,
which makes it the cheaper place to be wrong.

## Documentation

| | |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | **start here** — the whole system in one diagram, and what is decided by code rather than by the model |
| [docs/demo-runbook.md](docs/demo-runbook.md) | how to drive the demo, with measured timings for every beat |
| [docs/plant.md](docs/plant.md) | the simulated broadcast plant, and the fault menu with measured signatures |
| [docs/agent.md](docs/agent.md) | the five-phase agent, and why the sweep exists |
| [docs/content-screen.md](docs/content-screen.md) | Stage 0's calibration, and the burned-in-timecode trap |
| [docs/agent-performance.md](docs/agent-performance.md) | where the time and the tokens actually go |
| [docs/vision-spike.md](docs/vision-spike.md) | what vision can and cannot see, measured per model tier |
| [docs/cloud-deployment-risk.md](docs/cloud-deployment-risk.md) | cloud steps 1 and 2, executed and measured |
| [docs/audit-2026-08-21.md](docs/audit-2026-08-21.md) | a full read-only audit: 30 verified findings, and an honest list of what was **not** verified |
| [docs/mcp-tool-inventory.md](docs/mcp-tool-inventory.md) | which Grafana MCP tools each specialist is pinned to |

## License

[Apache-2.0](LICENSE)
