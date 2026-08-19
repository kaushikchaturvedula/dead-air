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

## Step 3 — L2 edges + chaos endpoints (next)

3× Cloud Run caching reverse proxies in us-east1 / europe-west1 / asia-south1,
exporting `edge_cache_hit_ratio`, `segment_status`, `segment_ttfb_seconds`,
`origin_shield_miss_total`, with `POST /chaos {region, mode, severity}`.

Note for L2: `segment_ttfb_seconds` will be a histogram, and histograms carry a
`le` label. The cardinality allowlist explicitly permits `le` and `quantile` —
dropping `le` silently destroys every histogram, which would look like a broken
exporter rather than a relabel bug.
