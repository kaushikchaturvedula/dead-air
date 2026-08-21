# The DEAD AIR plant

The plant is the live-streaming system the agent observes: encoder, origin,
edges, viewers, and the telemetry they emit. It is built as a thin vertical
slice first — prove the telemetry pipe end to end, then hang real video off it —
rather than layer by layer. A perfect encoder feeding a pipe that cannot carry a
number teaches you nothing.

## Step 1 — telemetry pipe ✅

```
  emitter ──scrape──▶ Alloy ──remote_write──▶ Grafana Cloud Mimir
  (:9101)             (:12345)                        │
                         │                            ├──▶ dashboard panel
                   cardinality guard                  └──▶ alert rule
                   (drops session_id &c)                      │
                                                    webhook contact point
                                                              │
                                                     ngrok tunnel
                                                              │
                                                  local receiver (:9102)
```

| Piece | Where | Role |
| --- | --- | --- |
| Emitter | [plant/emitter/](../plant/emitter/) | Synthetic gauge with an HTTP setter — the "change a number" control |
| Collector | [plant/alloy/config.alloy](../plant/alloy/config.alloy) | Scrape → cardinality guard → remote_write |
| Dashboard + alert | [scripts/provision_grafana.py](../scripts/provision_grafana.py) | Idempotent provisioning of both |
| Webhook receiver | [plant/webhook/](../plant/webhook/) | Stand-in for the agent trigger; prints and retains payloads |
| Verifier | [scripts/verify_pipe.py](../scripts/verify_pipe.py) | Per-hop assertions, fails at the first broken hop |

### Verified behaviour, 2026-08-18

- Metrics reach Mimir and are queryable: `local=10.0 cloud=10.0 (in sync)`
- Alloy delivered 74 samples, 0 failed
- Alert lifecycle on crossing the threshold:
  `inactive → pending → firing` in **1m43s** — consistent with a 15s scrape
  interval, 30s rule evaluation, and a 1m `for` clause
- The cardinality guard strips `session_id` in flight (see below)

### Timing

An end-to-end change takes up to ~2 minutes to turn Grafana red:

| Stage | Budget |
| --- | --- |
| Scrape interval | 15s |
| remote_write batch | a few seconds |
| Rule evaluation | 30s |
| `for` duration | 60s |

Shorten `for` in [scripts/provision_grafana.py](../scripts/provision_grafana.py)
if a tighter demo loop is wanted; 1m is deliberately short for development and
would be raised for anything resembling production.

## Cardinality: why the guard exists on day one

The stack is on the Grafana Cloud **free tier, ~10k active series**. A streaming
plant blows that budget in the most ordinary way imaginable: label a metric by
`session_id` and you get one series per viewer, per metric. A few hundred
simulated viewers across a handful of metrics exhausts the tier, and the failure
mode is not a clean error — it is silently dropped data and a dashboard that
looks fine.

So the guard is in the *first* version of the Alloy config, not retrofitted
after the first overrun. It drops, by label name:

```
session_id · viewer_id · client_id · request_id · trace_id · span_id
segment_uri · segment_id · media_seq · instance_ip · pod_ip
```

The rule of thumb for every metric added later: **aggregate the dimension,
don't label by it.** A gauge of active sessions costs one series; a gauge
labelled by session costs one per session.

### Proving the guard works

Asserting "no `session_id` in Mimir" against a metric that never had one is a
test that passes for the wrong reason. The emitter therefore publishes a
deliberate canary:

```
deadair_cardinality_canary{session_id="canary-0001"} 1
```

`make verify` fetches that exact series from Mimir and fails if `session_id`
survived the trip. Confirmed stripped: the series arrives carrying only
`__name__, component, env, instance, job, layer, project`.

Exactly one canary series is emitted, deliberately. Several series differing
only by `session_id` would collapse into duplicate samples once the label is
correctly dropped, breaking remote_write — the guard would fail in a way that
looks like a transport bug.

## Step 2 — L1 source + encoder ✅

ffmpeg `-re` generating `testsrc2` with a burned-in timecode → 4-rung ABR HLS
ladder → nginx origin, with encoder health flowing through the Step 1 pipe and
per-request detail flowing to Loki.

| Piece | Where | Role |
| --- | --- | --- |
| Encoder | [plant/encoder/](../plant/encoder/) | ffmpeg supervisor + Prometheus exporter on :9103 |
| Origin | [plant/origin/](../plant/origin/) | nginx serving HLS on :8080, JSON access log |
| Player | [plant/origin/player/](../plant/origin/player/) | hls.js with live rendition/buffer/stall readout |

### Verified 2026-08-18

Ladder measured off the wire, not assumed:

| Rung | Resolution | Measured bitrate | Target |
| --- | --- | --- | --- |
| 1080p | 1920×1080 | 5186 kbps | 5M |
| 720p | 1280×720 | 3127 kbps | 3M |
| 480p | 854×480 | 1591 kbps | 1.5M |
| 360p | 640×360 | 882 kbps | 800k |

4s segments, one video stream per variant, keyframe-aligned across rungs
(`-g 120` at 30fps) so a player can switch cleanly. `encoder_fps` holds 30.0
with 0 dropped frames; `packager_segment_lag` sawtooths 0→4s per rendition.
Origin access logs are queryable in Loki, labelled `rendition` and `status`.

### Metric naming

L1 metric names come from brief §5 **verbatim and unprefixed** —
`encoder_fps`, `dropped_frames`, `packager_segment_lag` — because that is how
§5 and the eventual alert rules name them. The Step 1 synthetic metrics keep a
`deadair_` prefix (`deadair_synthetic_gauge`, `deadair_cardinality_canary`)
since they are pipe infrastructure rather than plant signals, and should not be
mistaken for real telemetry. This inconsistency is deliberate.

### The nginx log trap

`nginx:alpine` ships `/var/log/nginx/access.log` as a **symlink to
/dev/stdout**. Logging there sends everything to the container's stdout, where
no file-tailing shipper can reach it — Alloy tails happily and delivers
nothing, which looks exactly like an authentication failure and sends you
hunting the wrong problem. The origin therefore logs to `/var/log/deadair/`,
a plain directory backed by a named volume, with a second `access_log` line to
stdout so `docker logs` still works.

If the volume was created before this fix, the symlinks were copied into it and
persist: `docker volume rm dead-air_nginx-logs` and recreate.

## Chaos: `black_source`

Brief §5's headline failure is runnable today, at L1, before the edges exist:

```bash
make black-source     # swap the encoder input to color=black
make frame            # grab the current frame -> frames/latest.png
make restore-source
```

Verified: with the source black, `encoder_fps` stays 30.0, `dropped_frames`
stays 0, `encoder_up` stays 1, and `packager_segment_lag` keeps its normal
sawtooth. **Every delivery metric is green while the picture is gone** — which
is the entire premise of the project, reproducible on demand in about fifteen
seconds.

The burned-in timecode keeps running under the black source. That is useful
rather than incidental: it separates `black_source` (black picture, clock
running) from a frozen source (static picture, clock stopped) — two failures
that are identical in delivery telemetry and distinguishable only in the
pixels. Note the timecode is the *plant's* overlay, applied after the input, so
a black frame here carries white text rather than being uniformly black.

The encoder reads its input from `ENCODER_SOURCE`, so the swap is a container
restart rather than a code change — which is what lets L2's chaos endpoint
drive it later.

## Step 3 — L2 CDN edges ✅ (local)

Three caching reverse proxies standing in for `us-east1`, `europe-west1` and
`asia-south1`, each exporting `edge_cache_hit_ratio`, `segment_status`,
`segment_ttfb_seconds` (histogram) and `origin_shield_miss_total`, with
`POST /chaos {mode, severity}`.

```bash
make edges                                          # region, chaos state, hit ratio
make chaos REGION=europe-west1 MODE=edge_latency    # degrade one region
make chaos-clear                                    # restore all
```

### Why latency injection is application-level

Brief §5 specifies `tc netem delay 800ms 200ms`. **netem cannot work on Cloud
Run** — it needs `NET_ADMIN` on the container's network namespace, which the
runtime does not grant. Building on netem locally would produce a chaos
mechanism that has to be rewritten the moment the edges deploy.

So the delay is injected in the request path instead
([plant/edge/edge.py](../plant/edge/edge.py)). It needs no privileges, runs
anywhere the container runs, and is indistinguishable from netem in the only
place that matters: what the client observes. Severity mirrors §5's netem
parameters — 1/2/3 → 200/800/2000 ms with proportional jitter.

### Verified 2026-08-19

`make chaos REGION=europe-west1 MODE=edge_latency SEVERITY=2`, p95 TTFB read
back out of Mimir with `histogram_quantile`:

| Region | p95 TTFB | State |
| --- | --- | --- |
| us-east1 | 4.8 ms | healthy |
| **europe-west1** | **981.2 ms** | **degraded** |
| asia-south1 | 4.9 ms | healthy |

A ~200× differential in exactly one region, rendered on the dashboard panel.
The degraded region also completes fewer requests per unit time, which is a
useful secondary signal.

### `le` survives the allowlist — confirmed end to end

The histogram was the specific risk flagged when the allowlist went in. Checked
in Mimir, not assumed: all 12 buckets present with
`le = 0.005 … 10.0, +Inf`, full label set
`__name__, component, env, instance, job, layer, le, project, region`, and
`histogram_quantile` returns per-region values. `make verify` now asserts this
on every run.

### Cloud Run's hidden prerequisite

L2-on-Cloud-Run silently depends on **L1-on-GCE**: an edge in `europe-west1`
must reach the origin over the public internet, and the origin is currently a
container on a laptop. Deploying the edges before the origin has a public
address produces three edges that cannot fetch anything. The local plant has no
such dependency, which is why it is worth completing first.

### Traffic generation

The differential is only visible if the edges are serving traffic, so
[plant/loadgen/](../plant/loadgen/) drives a few clients per region. It is
explicitly **not L3** — no playback clock, no buffer model, no rebuffer events,
no QoE beacons. It walks the ladder like a player so cache ratios and TTFB
distributions are realistic, and nothing more. L3 replaces it.

## Verifying labels, not just their absence

`make verify` asserts both directions, because they fail identically —
silently, with data that looks healthy:

- **Banned labels absent** — proven with a canary that deliberately carries
  `session_id`, so the check cannot pass because the label was never emitted.
- **Expected labels present** — `rendition` on `packager_segment_lag`,
  `region` + `status` on `segment_status`, `le` on the TTFB histogram, and so
  on. The `labelkeep` allowlist drops unknown labels *silently*: a metric
  arrives looking fine, just without the dimension the diagnosis depends on.

Both assertions were checked against a deliberately broken configuration to
confirm they actually fail, rather than passing for the wrong reason.

## Step 4 — L3 viewer fleet ✅

**201 modeled sessions** (67 × 3 regions) on a playback clock, running §5's
buffer model with ABR. This is where `rebuffer_ratio` is born — a client-side
signal no CDN metric can produce, because only a player knows its buffer
stalled.

### Scale, measured not assumed

§5 asks for ~200 clients; 201 runs comfortably here, so no compromise was
needed. On a 10-core / 8 GB-Docker laptop, alongside the 4-rung 1080p encoder
and three edges:

| | |
| --- | --- |
| Host load average | 6.96 (of 10 cores) |
| Viewer fleet | 5.4% CPU, 1.18 GB |
| Each edge | ~1% CPU, ~330 MB |
| Encoder (dominant cost) | ~120% CPU |
| **Self-inflicted rebuffering** | **0.000 across all 9 cohorts** |

That last row is the one that matters: the fleet is not starving itself, so the
rebuffering it reports is the plant's, not the laptop's. It stays cheap because
the edges serve most segments from cache and the sessions are I/O-bound; ffmpeg
is the real consumer. Lower `CLIENTS_PER_REGION` on a smaller box — a realistic
curve from fewer clients beats a starved one from many.

### Buffer model and ABR

```
buffer += segment_duration - download_time
buffer <= 0  =>  REBUFFER EVENT, stalled for |buffer| seconds
```

Device classes hold different buffers (tv 24s, desktop 16s, mobile 10s), so
mobile stalls first — a split invisible in any CDN metric. Measured under
`edge_latency` on europe-west1:

| Device class | rebuffer_ratio |
| --- | --- |
| mobile | 0.191 |
| desktop | 0.123 |
| tv | 0.048 |

Ordered by buffer size, exactly as the physics demands. All three other regions
sat at 0.000.

Sessions also run simple ABR: a session that cannot fetch a rung faster than
realtime steps down, and steps back up given headroom. Without it a degraded
region pins `rebuffer_ratio` near 1.0, which is neither realistic nor
informative — real players trade quality for continuity, and the *residual*
rebuffering after they have downshifted is what viewers actually experience.

### Two modeling bugs worth remembering

**Playing time must be unconditional.** Crediting `playing_seconds` only when
the buffer stayed positive made it stop accumulating the moment a session began
struggling, pinning `rebuffer_ratio` at exactly 1.000. A segment that arrives
late still plays its full duration — the viewer stalls, *then* watches it. The
give-away was a ratio of precisely 1.0 rather than a plausible fraction.

**Latency alone cannot starve a buffer.** 800 ms of added TTFB against a 4s
segment deadline is comfortably survivable, so a pure-sleep `edge_latency`
produced no rebuffering at all. Real netem does not behave that way: at high
RTT, TCP throughput collapses to roughly window/RTT, so a delayed link also
transfers slowly. The edge therefore shapes throughput as well, calibrated
against the ladder — severity 2 caps at 600 kbps, *below* the 800k bottom rung,
so no amount of downshifting rescues it and the rebuffering sustains past a 2m
alert window.

### The real alert (§5)

`rebuffer_ratio > 0.02 for 2m, by region` — replacing the Step 1 placeholder,
which `make provision` now deletes.

The query derives the ratio from summed counter rates rather than averaging the
`rebuffer_ratio` gauge across device classes: averaging weights a handful of
mobile sessions equally with a large TV cohort, while summed rates weight by
actual viewing time, which is what a region-level rebuffer ratio means.

`region` is deliberately **not** hardcoded in the rule's labels — it arrives
from the query's series labels, producing one alert instance per region.
Verified end to end: one region `Alerting`, two `Normal`, and the webhook
payload carries `region: europe-west1` in both `commonLabels` and the instance
labels. The agent keys its entire investigation off that label.

Full lifecycle observed: `inactive → pending → firing` in 2m01s (matching
`for: 2m`), then `RESOLVED` automatically on `make chaos-clear`.

### Cardinality: both halves proven

| | Metrics (Mimir) | Logs (Loki) |
| --- | --- | --- |
| Aggregated by | `region`, `device_class` | — |
| Per-session detail | never | `session_id` in the log **line** |
| Proof | `viewer_cardinality_canary` | `hop_beacons` in `make verify` |

The L3 canary is emitted carrying `session_id`, `region` **and**
`device_class`, so one series proves both directions at once: it arrives in
Mimir with `session_id` stripped and `region`/`device_class` intact. Over-
stripping would be as damaging as under-stripping and is now equally caught.

The same discipline applies in Loki — a Loki label costs what a Prometheus one
does, so `session_id` stays in the beacon body while only bounded fields
(`region`, `device_class`, `rendition`) become labels. `make verify` asserts
`session_id` is absent from the stream labels *and* present in the line.

Total active series for the whole plant: **172**, against a ~10k free-tier
budget.

## The fault menu — ground truth for agent week

Every fault in brief §5's menu, driven end to end and measured. **This is the
answer key**: the agent's job is to reach the right diagnosis from these
signatures alone, so they must be genuinely distinguishable.

```bash
make chaos MODE=black_source                            # L1, plant-wide
make chaos MODE=ladder_collapse
make chaos MODE=ladder_mismatch
make chaos MODE=segment_gap [SEVERITY=7]
make chaos MODE=edge_latency REGION=europe-west1 [SEVERITY=2]   # L2, one region
make chaos-clear
make chaos-status
```

Regenerate the whole table with
[`scripts/fault_signatures.py`](../scripts/fault_signatures.py), which injects
each fault in turn and reports what moved. Run it after any change to the
encoder, edges or ladder.

### Measured signatures, 2026-08-19

| Fault | Scope | rebuffer_ratio | Delivered bitrate | 4xx | Other |
| --- | --- | --- | --- | --- | --- |
| **edge_latency** | one region | **0.437 in europe-west1 only** | 3.7 → 0.8 Mbps, that region only | none | p95 TTFB ~1s in that region |
| **segment_gap** | all regions | 0.034 / 0.068 / 0.035 — **every region** | unchanged | **sustained, every region** | `packager_segments_deleted_total` climbing |
| **ladder_collapse** | all regions | **0 everywhere** | **3.6 → 2.0 Mbps, every region** | transient only | manifest **4 → 3 rungs**; `packager_segment_lag{1080p}` climbs unbounded |
| **black_source** | all regions | 0 | unchanged | none | **nothing moves.** fps 30, 0 drops, lag normal |
| **ladder_mismatch** | all regions | 0 | unchanged | none | **nothing moves at all** |

### How to tell them apart

**Regional vs plant-wide** is the first cut. `edge_latency` moves one region and
leaves the other two untouched; everything else moves all three. A fault in one
region is a delivery-path fault, and the agent should not go looking at the
encoder.

**Rebuffering vs bitrate** separates the plant-wide faults. `ladder_collapse`
drops bitrate with *zero* rebuffering — players quietly settled onto a lower
rung because the top one vanished from the manifest. `segment_gap` does the
opposite: bitrate is untouched, but segments are missing, so viewers stall.

**4xx persistence, not presence.** `ladder_collapse` also emits a burst of 404s
while players discover the withdrawn rung — realistic, and a trap. What
distinguishes it from `segment_gap` is that its 404s *stop* once players re-read
the master manifest, while `segment_gap`'s continue indefinitely. An agent that
keys on "are there 404s" rather than "are 404s ongoing" will confuse the two.

**The last two are invisible.** `black_source` and `ladder_mismatch` are
identical in every metric, log and trace — and identical to healthy. They are
the faults only frame inspection catches, and the reason this project exists:

- **black_source** — the frame is black, but the burned-in timecode keeps
  running. Black picture, live encoder.
- **ladder_mismatch** — the 1080p rung is visibly soft: 720p detail upscaled to
  1920×1080, at the full 1080p bitrate. The manifest, segment sizes and cadence
  are all exactly right. Verified by cropping the test pattern's fine
  checkerboard from a healthy frame and a faulted one: the healthy frame has
  crisp high-frequency noise, the faulted one is smoothed.

`make frame` grabs the current top-rung frame; the signature harness writes one
per mode to `frames/signature-<mode>.png`.

### Chaos is a POST, not a restart

L1 faults switch the running encoder in place (`POST /chaos` on :9103), so the
demo can move between faults in seconds. Modes that change what is encoded
restart ffmpeg internally; `segment_gap` does not restart anything, it just
deletes segments after the packager writes them.

`segment_gap` is deliberately at the **origin**, not the edge, because that
placement *is* the diagnosis: a gap at the packager 404s in every region at
once, which is what separates "packager fault" from "one edge is sick".

### Two bugs this exercise caught

**Filename-keyed dedup wedged the fleet.** Viewers tracked played segments by
filename, but ffmpeg renumbers from `seg_00000.ts` on every restart — which
chaos does constantly. After one restart every "new" segment looked already
played, `fresh` was empty forever, and the whole fleet sat at
`rebuffer_ratio` 1.0 regardless of the injected fault. Every signature looked
identical because the dominant signal was the harness, not the fault. Viewers
now track the absolute **HLS media sequence** and resync when the stream
renumbers, exactly as a real player does.

**Clearing all segments on restart drowned out the fault.** Wiping every rung on
each mode switch 404'd the segments viewers were actively fetching, so every
mode presented as "404 storm everywhere". Only rungs that are actually being
withdrawn are cleared now.

Both produced *plausible* telemetry, which is the dangerous kind of wrong: the
signatures looked like real faults and would have trained agent week against
noise.

## Traces — the third signal

Brief §5's L4 asks for traces, and the agent's Phase 1 fans out across all
three signals. What each one can and cannot say about the same slow fetch:

| Signal | Answers | Cannot say |
| --- | --- | --- |
| Metrics | "rebuffer ratio in europe-west1 is 0.19" | which request, or why |
| Logs | "this segment 404'd for this session" | how long each hop took |
| Traces | "this fetch took 7.8ms, of which 5.9ms was the edge and 3.5ms was origin" | aggregate rates |

A real captured trace:

```
viewer.segment_fetch        [deadair-viewers]            7.8ms
  session_id=europe-west1-desktop-052  region=europe-west1  status=200
  edge.serve_segment        [deadair-edge-europe-west1]  5.9ms
    cache=MISS  response_size=1559460  status=200
    origin.fetch_segment    [deadair-edge-europe-west1]  3.5ms
      response_size=1559460  status=200
```

That tree is what tells the agent whether a slow edge is slow *itself* or just
waiting on origin — a distinction metrics and logs cannot express.

**Implementation.** The plant's services are dependency-free stdlib processes,
so rather than pull in the OpenTelemetry SDK they emit OTLP JSON directly
([`plant/shared/deadair_trace.py`](../plant/shared/deadair_trace.py), ~200
lines) to Alloy, which forwards to Tempo.

- **Head-based sampling at the viewer** (2% by default, `TRACE_SAMPLE_RATIO`).
  The client owns the decision and propagates it via the W3C `traceparent`
  sampled flag, so a sampled request yields a *complete* tree rather than
  disconnected middle spans.
- **Per-session detail rides in span attributes** — `session_id`, `segment`,
  `rendition`, `buffer_seconds`. Attributes are not a cardinality problem the
  way labels are, so this is where that detail belongs alongside Loki.
- **`traceparent` is forwarded to the origin and logged by nginx**, bridging
  Loki and Tempo: from a slow trace you can find the exact origin log line, and
  from a 404 log line you can pull up the trace that produced it.
- **Exporter health is a metric** (`trace_spans_exported_total`,
  `_failed_total`, `_dropped_total`), so a silently dead trace pipeline shows up
  in Mimir rather than as an empty Tempo query nobody notices.

**Endpoint gotcha.** Grafana Cloud Tempo ingests OTLP over **gRPC on :443**. The
`tempo-*.grafana.net` host serves the *query* API over HTTP and 404s on
`/otlp/v1/traces`, while the `otlp-gateway-*` host expects a different instance
ID from the one the Tempo datasource uses. gRPC to the Tempo host with the
datasource's own instance ID is the combination that works.

## L0 — the synthetic canary is deliberate

The `deadair_synthetic_gauge` panels are **not leftovers**. They live in a
collapsed row at the bottom of the dashboard, labelled "pipe health (synthetic
canary, not plant telemetry)", and they answer a question no plant metric can:

> Encoder metrics just disappeared. Is the **plant** broken, or is the **pipe**
> broken?

The canary is driven by hand (`make set VALUE=95`) and touches no video path.
If it still moves while plant metrics are missing, Alloy → Mimir is healthy and
the fault is in the plant; if it stops too, the collector or remote_write is the
problem. The emitter also carries `deadair_cardinality_canary`, which is how
`make verify` proves the relabel guard still strips `session_id`.

It carries **no alert** — the only alert is L3's `rebuffer_ratio`. The Step 1
placeholder that once watched this gauge is deleted on every `make provision`.

## Correction to brief §5: `e2-medium` is wrong for L1

§5 specifies an `e2-medium` for the encoder VM. **It cannot hold the ladder.**

`e2-medium` reports `isSharedCpu: True` — 2 vCPU of burst against roughly 1
vCPU sustained — and ffmpeg needs ~1.6 cores for four 1080p rungs. The failure
is gradual, which is what makes it dangerous:

| Condition | encoder_fps |
| --- | --- |
| e2-medium, idle (bursting) | 29.3 |
| e2-medium, 201 viewers | 19.2 |
| e2-medium, burst credits depleted | 15.8 |
| **e2-standard-2 (dedicated cores)** | **28.7** |

It looks healthy on first inspection and decays over minutes, so a smoke test
passes and the demo degrades on camera. **Use `e2-standard-2` as the minimum.**

Measured on GCE 2026-08-20 under the full 201-viewer fleet; see
[cloud-deployment-risk.md](cloud-deployment-risk.md).

## Step 5 — cloud deployment (step 1 done, see cloud-deployment-risk.md)

GCE origin first, then the three Cloud Run edges — in that order, because an
edge needs a publicly reachable origin. Nothing else is blocked on it: the
entire plant, all four layers, runs locally today.
