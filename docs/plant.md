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

## Step 2 — L1 encoder (next)

ffmpeg `-re` with `testsrc2` and burned-in timecode → a 4-rung ABR HLS ladder
(1080p/5M, 720p/3M, 480p/1.5M, 360p/800k, 4s segments) → nginx origin, with
`encoder_fps`, `dropped_frames` and `packager_segment_lag` flowing through the
Step 1 pipe.

Exit criteria: hls.js plays the ladder in a browser and the encoder metrics are
visible in Grafana.
