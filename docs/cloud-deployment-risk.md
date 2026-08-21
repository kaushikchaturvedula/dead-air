# Cloud deployment — step 1 executed, with results

**Status: step 1 done and measured. GCE origin created, tested under full load,
and STOPPED. Local plant rolled back and re-verified healthy.**

> ## Step 1 results, 2026-08-20
>
> | Question | Answer |
> | --- | --- |
> | Does `make verify` pass with a cloud origin? | **Yes, 14/14** |
> | Does `make diagnose-checks` pass? | **No** — the `healthy` control fails |
> | Egress with 201 viewers | **80.4 GB/hour = $9.65/hour** |
> | Does §5's `e2-medium` hold the ladder? | **No** — it is shared-core |
>
> Three findings below, each of which would have cost a day if met on Sep 3.

Step 1 was executed early, deliberately, because it was the only part of the
build with no evidence behind it. It returned three findings that would each
have cost a day if met during the Aug 29–Sep 4 window.

Steps 3–5 remain unbuilt. **Step 2 is done and measured — see below.**

---

# Cloud step 2 — three Cloud Run edges, executed and torn down

**Status: deployed, measured, and destroyed. Total spend ~$1.78 against a ~$2.01
estimate. VM stopped, all three services deleted, firewall narrowed back.**

Run with a **21-viewer** fleet (`CLIENTS_PER_REGION=7`), not the full 201,
because of the cost finding below.

## The cost finding, which changes how every later step gets budgeted

The `$1.40/hour` projection covers **only the origin→edge leg**. It omits
edge→viewer, which is **18x larger** and, whenever the fleet is outside GCP, is
billed as Cloud Run **internet** egress at $0.12/GB:

| fleet | edge→viewer | internet $ | origin $ | compute $ | **total/hr** |
| --- | --- | --- | --- | --- | --- |
| 201 | 211.6 GB | 25.39 | 0.65 | 0.10 | **$26.14** |
| 60 | 63.2 GB | 7.58 | 0.19 | 0.10 | $7.87 |
| 30 | 31.6 GB | 3.79 | 0.10 | 0.10 | $3.98 |
| **21** | **21.1 GB** | **2.53** | **0.06** | **0.10** | **$2.69** |

At full fleet the remaining ~$83 of credit is **about three hours**.

## Did the cache fix survive the WAN? Yes — but not by the metric you'd reach for

**Hit ratio is not comparable across fleet sizes.** With N viewers per region
pulling the same segment it is 1 miss + (N-1) hits, so the ceiling is (N-1)/N:
85.7% at 7/region, 98.5% at 67/region. The measured 68.6–77.9% therefore cannot
be compared against the local 95.5–97.2%, and any claim that "the hit ratio holds
at 21 viewers so it holds at 201" is wrong on its face.

**Fetches per distinct object is the fleet-independent metric** — perfect
single-flight is 1.0, and anything above it is TTL expiry plus manifest refetch:

| | fetches | distinct | **per object** |
| --- | --- | --- | --- |
| us-east1 | 421 | 129 | **3.26** |
| europe-west1 | 498 | 195 | **2.55** |
| asia-south1 | 289 | 118 | **2.44** |
| **local baseline, 201 viewers** | | | **2.42** |

Cloud 2.44–3.26 against local 2.42. **Coalescing survives the WAN**, which is
what the step was for. Zero cache evictions in every region.

## Cold starts: measured, and not a problem

A cold start looks exactly like `edge_latency` to the fleet, so it was measured
rather than assumed. With `--min-instances=1`, first-request TTFB against steady
state:

| region | first | p50 | p95 | verdict |
| --- | --- | --- | --- | --- |
| us-east1 | 0.250s | 0.135s | 0.211s | warm |
| europe-west1 | 0.442s | 0.210s | 0.415s | warm |
| asia-south1 | 1.352s | 0.654s | 1.165s | warm |

`--max-instances=1` is equally deliberate: the edge cache is in-memory and
per-instance, so autoscaling would fetch each segment once *per instance* and
depress the hit ratio for reasons unrelated to the fix under test.

## Finding 4 — the hybrid topology is not a faithful plant

This is the one that matters for planning. With viewers on the laptop and edges
in the cloud, **two signals are permanently wrong**, and both look like faults:

1. **Every region exceeds the TTFB threshold.** `TTFB_ELEVATED_SECONDS = 0.100`
   (`signatures.py:52`). Measured p95 at the edges: **0.5s / 1.0s / 2.5s** — 5x,
   10x and 25x over. `_ttfb_elevated_regions` would return all three, which
   breaks `edge_latency`'s required "confined to exactly one region" check, so
   `edge_latency` could never match.

2. **asia-south1 is pinned to the bottom rung.** `viewer_bitrate_avg` reads
   **800 kbps** for every device class there, against 5 Mbps in us-east1 and
   europe-west1. WAN RTT from a US laptop means ABR cannot sustain anything
   above 360p. That is the network, not the plant — but it is indistinguishable
   from a real regional degradation.

This is the same shape as step 1's finding 3 (a remote origin put the plant
above its own alert threshold), and it means **regional numbers are meaningless
until the viewer fleet moves into GCP**. Step 3 is not optional polish; it is a
correctness prerequisite for any regional fault demo.

## Three defects this step exposed in our own code

1. **`cloud_origin.sh` still defaulted to `e2-medium`.** Step 1 measured that
   e2-medium cannot hold the ladder and wrote it down *here* — but never in the
   default. The VM came back up shared-core and served at 25.1 fps. Fixed to
   `e2-standard-2`. On the resize it held ~26.9 fps idle, still under the 30 fps
   source and a little under the 28.7 this doc recorded.
2. **`EDGES` was hardcoded in `docker-compose.yml`** for both `viewers` and
   `loadgen`, immediately below an overridable `CLIENTS_PER_REGION`. Pointing
   the fleet at the cloud edges silently did nothing — the fleet kept pulling
   from the local docker edges while the Cloud Run services sat at 5 origin
   fetches and 1 distinct object. **A null measurement that looks like a working
   one.** This doc's claim that region is "configuration, not code" was false
   for that file. Now `${EDGES:-...}`.
3. **The firewall blocked Cloud Run.** Step 1 scoped `deadair-allow-origin` to
   the operator's `/32`; Cloud Run egresses from Google's dynamic ranges, so all
   three edges deployed clean, started clean, and 502'd on every request. A VPC
   connector plus Cloud NAT is the right permanent fix (~$0.044/hr); for a
   45-minute measurement the rule is widened by `cloud_edges.sh up` and narrowed
   by `down`, paired so nobody has to remember.

## What step 2 did NOT establish

* Nothing about behaviour at 201 viewers. Origin egress measured **0.94–1.12
  GB/hr**, far below the 13.9 GB/hr full-ladder floor, because at 7 viewers per
  region only a couple of rungs are ever requested. It does **not** extrapolate.
* Nothing about regional fault detection, per finding 4.
* No agent run was performed against the cloud topology.

## What has to happen, and in what order

The ordering is forced, not preferred. Each step blocks the next.

| # | Step | Blocks | Why the order is fixed |
| --- | --- | --- | --- |
| 1 | ~~**GCE origin**~~ **DONE** (use e2-standard-2, not e2-medium) | everything | An edge in europe-west1 must reach the origin over the public internet. Local containers have no public address. |
| 2 | **3× Cloud Run edges** (us-east1, europe-west1, asia-south1) | viewer fleet, regional demo | Needs step 1's public origin URL |
| 3 | **Viewer fleet** somewhere reachable | rebuffer_ratio, the alert | Must reach the edges |
| 4 | **Agent hosting** (Cloud Run) | the webhook trigger | Grafana Cloud must POST to a public URL. ngrok is a laptop, not a demo. |
| 5 | **Stable public URL** | the Sep 4 criterion | Everything above |

## What is already de-risked

Real work has gone into making this deployment boring rather than novel:

- **The edge image is Cloud Run-ready today.** Single stdlib process, listens on
  `$PORT`, no kernel capabilities. This is why `edge_latency` was built as
  application-level shaping rather than `tc netem` — netem needs `NET_ADMIN`,
  which Cloud Run does not grant, so a netem-based fault would have had to be
  rewritten at exactly this step.
- **Region is configuration, not code.** `EDGE_ENDPOINTS` in
  `video_tools.py` and `EDGE_PORT_*` in the Makefile are the only places that
  know a hostname. Cloud Run turns those into service URLs; nothing else moves.
- **The agent reaches Grafana through the hosted API**, not through anything
  local, so Phases 1 and 3 work unchanged from Cloud Run.
- **Phase 3's evidence collection uses the Grafana datasource proxy**, not the
  local exporters, for the same reason.
- **The webhook bridge is already the shape of a Cloud Run service.** The
  receiver records deliveries; `run_agent.py` consumes them. In production the
  contact point POSTs straight to the agent service, and the agent above that
  boundary does not change.

## Finding 1 — egress is 80 GB/hour, not the ~14 estimated

**Measured: 80.40 GB/hour, 178.7 Mbps sustained, $9.65/hour at $0.12/GB.**
That is $231/day, and it would exhaust the remaining ~$97 of credit in **about
ten hours**.

My pre-deploy estimate was ~14 GB/hour, and it was wrong for an instructive
reason. I assumed each edge fetches each segment **once** and serves 67 viewers
from cache, which would make origin egress a function of the *ladder bitrate*
(3 edges × 10.3 Mbps). In practice the measured cache hit ratio was **62%**, so
38% of viewer demand reached the origin — and viewer demand is 201 × ~5 Mbps,
not 3 × 10.3 Mbps. Origin egress scales with **viewer demand × miss ratio**,
which is an order of magnitude larger.

Two things follow:

- **This is the hybrid worst case, not the shipping architecture.** Laptop edges
  pulling from a GCE origin cross the public internet at $0.12/GB. Once the
  edges are on Cloud Run, origin→edge becomes GCP-internal: free in-region,
  ~$0.02/GB cross-region. The same traffic gets 6–12× cheaper simply by moving
  the edges, which is step 2.
- **Cache hit ratio is the lever, and 62% was low.** FIXED -- see below.

### Cache fix, measured locally 2026-08-21

Two independent bugs, both real, neither the whole story on its own:

1. **Cache stampede.** 67 viewers per edge want the newest segment at the same
   moment. Every one of them missed and pulled it from origin independently,
   because the first fetch had not returned yet. Locally a fetch completes in
   ~5ms so few requests collided; over a WAN each fetch stays open ~1s, making
   the collision window ~200x wider. Origin latency was the amplifier, not the
   cause. Fixed with **single-flight coalescing**: the first caller fetches,
   everyone else waits for it and is served from cache.
2. **Expiry was lazy, so the size cap evicted live segments.** An entry only
   disappeared when someone asked for it again -- and nothing asks for a segment
   that has left the live playlist. Dead entries accumulated to the 192MB cap,
   which then evicted objects that were still hot, and those were immediately
   re-fetched. Fixed by **reaping expired entries before size-based eviction**,
   and by cutting segment TTL from 60s to 30s (the live window is ~24s).

| | Before | After |
| --- | --- | --- |
| Cache hit ratio | 62% | **95.5 - 97.2%** |
| Evictions | 85 - 150 per 6 min | **0** |
| Cache size | pinned at the 200MB cap | 18 - 33MB (the true working set) |
| Fetches per segment | — | **2.42** (3.00 = one per edge = perfect) |
| Origin egress | 80.4 GB/hour | **11.5 GB/hour** |

11.5 GB/hour is *below* the 13.9 GB/hour theoretical floor because not every
edge pulls every rung. Projected cloud cost falls from **$9.65/hour to roughly
$1.40/hour**, and coalescing specifically neutralises the latency amplifier, so
the improvement should survive the WAN -- but that is a projection from a local
measurement and needs re-checking on the VM before step 2 is trusted.

A measurement caveat worth recording: the first "duplicate fetch" metric said
72% even after both fixes, which looked like failure. It was counting requests,
and 73.5% of origin requests are manifest refreshes that carry **0.0% of the
bytes**. A live playlist *must* be re-fetched; it changes. Bytes are 100%
segments, and segments are what egress bills for.

**The plant must never be left running in the cloud unattended.** `stop` is not
housekeeping here, it is the difference between a $2 experiment and a dead
budget.

## Finding 2 — §5's `e2-medium` cannot hold the ladder

`e2-medium` reports `isSharedCpu: True`. It has 2 vCPU of burst but roughly
**1 vCPU of sustained** capacity, and ffmpeg needs ~1.6 cores for a 4-rung 1080p
ladder. The failure mode is deceptive:

| Condition | encoder_fps |
| --- | --- |
| e2-medium, idle (bursting) | 29.3 |
| e2-medium, 201 viewers | 19.2 |
| e2-medium, after burst credits deplete | 15.8 |
| **e2-standard-2 (dedicated), 201 viewers** | **28.7** |

It looks fine on first inspection and degrades as burst credits run out, which
is exactly the kind of thing that would have surfaced mid-recording. **Use
`e2-standard-2` or larger; the brief's `e2-medium` is undersized.**

A related trap: stopping and starting the instance to resize **changed its
ephemeral public IP**. Anything holding that address breaks. A static IP is a
prerequisite for the stable public URL the Sep 4 criterion needs.

## Finding 3 — a remote origin puts the plant above its own alert threshold

`make verify` passes 14/14 against the cloud origin: every hop delivers, all
three signals confirmed. But `make diagnose-checks` **fails its healthy
control**, and the reason matters.

| Metric | Local origin | Cloud origin (laptop edges) |
| --- | --- | --- |
| Mean segment TTFB | ~5 ms | **~1,000 ms** |
| rebuffer_ratio floor | 0.000 | **0.028 – 0.095** |

The alert threshold is 0.02. With a WAN between the edges and the origin, the
plant's *baseline* sits above its own alert threshold, so the healthy control
correctly reports a fault — the plant genuinely is not healthy in that topology.
Everything else was clean: no 4xx, full ladder, encoder healthy, pixels healthy,
rung resolution 0.943.

This is again a **hybrid artifact**. Laptop edges + cloud origin is the
worst-case latency configuration and is not what ships; in the real topology the
edges sit beside the origin inside GCP. But it does mean **step 2 cannot be
skipped** — a cloud origin with local edges is not a usable demo configuration,
only a test of the plumbing.

## Still unknown

1. **Cloud Run cold starts** against a 4s segment deadline. `min-instances=0`
   is the cost-control default from §5, but a cold start on a segment fetch
   looks exactly like `edge_latency` to the viewer fleet — a self-inflicted
   fault that could pollute the demo.
2. **Agent run duration vs Cloud Run request timeout.** A full three-phase run
   takes minutes locally. Cloud Run's request timeout caps at 60 minutes, which
   is fine, but the webhook POST should not block for the whole run — the
   trigger needs to return immediately and run the agent asynchronously.
3. **The confidence monitor needs a scheduler.** Cloud Scheduler hitting the
   agent service is the obvious answer, and it is another resource to create,
   authenticate, and pay for.
4. **Secrets.** Everything currently reads a gitignored `.env`. Cloud Run wants
   Secret Manager, which is a small but non-zero amount of new plumbing.
5. **Public URL and the cold-click demo.** A stranger clicking a link needs the
   player, the dashboard, and ideally the agent's output all reachable without a
   login. Grafana dashboards can be made public; the player is static; the
   agent's output needs somewhere to live.

## Recommended sequencing

Do not deploy everything at once. The whole point of the local plant is that
each cloud step can be validated against a known-good baseline.

1. **GCE origin only.** Point the *local* edges at the public origin. If the
   plant still passes `make verify` and `make diagnose-checks`, step 1 is done
   and the blast radius of any failure is one VM.
2. **One Cloud Run edge**, in us-east1. Keep the other two local. A per-region
   differential between a cloud edge and a local edge is still a differential,
   and it isolates Cloud Run behaviour from everything else.
3. **The other two edges**, once the first is boring.
4. **Agent on Cloud Run**, triggered by the real webhook. This is where ngrok
   finally goes away.
5. **Cloud Scheduler** for the confidence monitor.

Each step has a rollback: point the config back at localhost.

## Cost control, restated

`scripts/cloud_origin.sh stop` is the GCE half of `make plant-down`, and after
finding 1 it is not optional. At $9.65/hour of egress, an afternoon of leaving
the plant running costs more than the whole remaining budget.

Actual spend for step 1: **25.73 GB served across ~1.5 hours** (~$3 of egress)
plus a few cents of compute. The instance is now TERMINATED, billing only for
its 20 GB boot disk (~$0.001/hour).

The $80 budget alert is a backstop that fires *after* the damage. The real
control is stopping the instance, and doing measurement in bounded windows.

## The honest risk statement

The plant, all five faults, all three signals, and Phases 1–3 of the agent work
today, locally and reproducibly. **L1 has now also run in the cloud**, under
full 201-viewer load, and the plumbing works — `make verify` passed 14/14
against it.

What remains untested is everything above L1: Cloud Run edges, agent hosting,
Cloud Scheduler, Secret Manager, and the public URL. The cold-start question in
particular is still a guess.

The Aug 29–Sep 4 window is currently carrying: cloud deployment, Phases 4–5,
IRM incidents, dashboard annotations, AI observability, the public URL, *and*
the video and writeup in the final two days. Deployment is the only item on that
list with genuinely unknown unknowns, which argues for starting it early and in
the smallest possible increments — step 1 alone, against the local plant, would
retire most of the uncertainty for the price of one VM.
