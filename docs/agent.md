# The DEAD AIR agent — Phases 1–2

```
root  SequentialAgent
├── phase1_scope  SequentialAgent
│   ├── scope_fanout  ParallelAgent
│   │   ├── scope_metrics      Mimir       4 MCP tools
│   │   ├── scope_logs         Loki        3 MCP tools
│   │   ├── scope_traces       Tempo       2 MCP tools
│   │   └── scope_dashboards   Grafana     3 MCP tools
│   └── scope_synthesizer  →  IncidentScope   (schema-validated)
└── phase2_see  SequentialAgent
    ├── see_investigator   manifest · frames · vision · rung check
    └── see_synthesizer    →  VisualFinding   (schema-validated)
```

Model: `gemini-3.7-flash`, `location=global`, everywhere. Settled by
[the vision spike](vision-spike.md); no higher tier is used anywhere, including
for vision.

## Phase 1 — SCOPE

Four specialists query Grafana Cloud **concurrently** through the self-hosted
MCP server, then a synthesiser folds their reports into one validated
`IncidentScope`.

Scope is deliberately separated from diagnosis. The first job is to bound the
blast radius, because that boundary alone eliminates most of the fault menu
before a single frame is fetched: a fault in exactly one region cannot be the
encoder, since all three regions are fed by the same one.

Each specialist is told to **check the regions it was not alerted about**.
Populating `healthy_regions` is as valuable as populating `affected_regions` —
it is the entire difference between a regional and a plant-wide fault.

### Tool pinning — 12 of 73

The MCP server exposes 73 tools. The agent sees 12, and no single specialist
sees more than 4. Each gets its own `McpToolset` with its own `tool_filter`.

```bash
make agent-tools     # prints the resolved subset, and fails if it drifts
```

Two reasons, and the second matters more:

1. **Accuracy.** Function-calling degrades as the declaration list grows. 73
   tool declarations is far past the point where a model reliably picks right.
2. **Determinism.** A pinned toolset makes the agent's search space a design
   decision that can be reviewed, rather than an emergent property of whatever
   the MCP server happens to expose that week. If Grafana ships new tools
   tomorrow, this agent's behaviour does not change.

| Specialist | Tools | Why these |
| --- | --- | --- |
| metrics | `query_prometheus` | the workhorse; every quantitative claim |
| | `query_prometheus_histogram` | p95 TTFB is how `edge_latency` shows itself, and hand-rolling quantiles from `_bucket` series is exactly the PromQL a model gets subtly wrong |
| | `list_prometheus_label_values` | enumerate the regions that exist, so "all regions" is checked rather than assumed |
| | `list_prometheus_metric_names` | discovery fallback — distinguishes *metric absent* from *metric healthy*, which is itself a diagnosis |
| logs | `query_loki_logs` | the 404 storm that identifies `segment_gap` |
| | `query_loki_stats` | cheap volume check first, so a busy window cannot flood the context |
| | `list_loki_label_values` | which regions/renditions actually appear in logs |
| traces | `tempo_traceql-search` | find slow client→edge→origin fetches |
| | `tempo_get-trace` | pull one span tree to attribute latency to a hop |
| dashboards | `search_dashboards` | locate the operator's view of the incident |
| | `get_dashboard_summary` | compact; `get_dashboard_by_uid` returns the whole JSON model and would swamp the context |
| | `get_dashboard_panel_queries` | what operators actually watch, and what a postmortem annotation must reference |

Deliberately excluded: the `tempo_traceql-metrics-*` tools (aggregate trace
metrics duplicate what Mimir answers better), and `list_datasources` —
datasource UIDs are stable and hardcoded into the instructions, which removes an
entire class of "the agent queried the wrong datasource" failure.

## Phase 2 — SEE

The phase that justifies the project. Delivery telemetry measures whether
*bytes* arrived; it cannot measure whether those bytes contain a *picture*.

Three local tools, split along the line the spike measured:

| Tool | Kind | Decides |
| --- | --- | --- |
| `get_stream_manifest` | HTTP | which renditions the ladder advertises — a missing rung is `ladder_collapse`, settled without fetching a frame |
| `inspect_frame` | **Gemini vision** | healthy / black / frozen / corrupted, plus the burned-in timecode |
| `check_rung_resolution` | **deterministic code** | whether a rung carries the detail it advertises |

**Vision never judges resolution.** The spike found no tier can, and that merely
*offering* an "upscaled" answer produces confident, specific, exactly-backwards
prose. The vision prompt does not mention sharpness or resolution at all, and
`VisualFinding.frame_verdict` has no upscaled option to select.

**Vision runs inside a tool**, not by handing images to the agent's own context.
That keeps the prompt pinned: it cannot drift as the conversation grows, which
is the only reason this fault class is reliably detectable.

### The timecode is the discriminator

Over a black picture, the burned-in clock separates two faults that are
otherwise identical:

- **running** → the encoder is alive and the *source* went black → `black_source`
- **stopped** → the source froze, or the encoder died

The agent is instructed to inspect a **second** frame and compare timecode
values whenever the first is black. In the verified run it did exactly that,
reporting `00:05:25.000 → 00:05:33.000`.

## Trigger

```bash
make agent REGION=us-east1     # run once
make agent-watch               # wake on every firing alert
```

`scripts/run_agent.py --watch` polls the webhook receiver and starts a run per
new *firing* delivery, seeding the alert's region into session state as the
agent's starting scope. The receiver itself stays a dependency-free container;
this script is the bridge, and in production becomes the Cloud Run service the
contact point posts to directly. The agent above it does not change.

## Verified end to end, 2026-08-19

`make chaos MODE=black_source`, then a run against a plant where every delivery
metric was green:

**IncidentScope** — checked all three regions, found all three healthy, called
blast radius plant-wide, and ruled out three faults *with the observation that
eliminated each*:

```
ruled_out:
  edge_latency:    TTFB ~4.77ms and rebuffer <0.03% across all regions
  segment_gap:     zero 4xx in metrics, logs and traces
  ladder_collapse: all 4 rungs active, lag ~2.1s, bitrate not dropped
candidate_faults: [black_source, ladder_mismatch]
needs_visual_inspection: true
```

Those are exactly the two faults that survive telemetry — the two the project
exists to separate.

**VisualFinding** — `black_frame`, confidence 1.0, timecode legible and
**advancing** across two frames, `contradicts_telemetry: true`,
`suspected_fault: black_source`. It also ran `check_rung_resolution` despite the
black frame and correctly reported `carries_expected_detail` (ratio 0.973), so
`ladder_mismatch` was eliminated by measurement rather than by omission.

## Known gap: `black_source` fires no alert

**The fault the whole demo is built around cannot wake the agent through the
alert path.** This is not a defect in the alerting — it is the premise, working:

| | |
| --- | --- |
| Alert rule | `rebuffer_ratio > 0.02 for 2m, by region` |
| `rebuffer_ratio` under `black_source` | ~0.0001 |
| Alert state after 10 minutes of black | `Normal`, all three regions |
| Webhook deliveries | none |

Black frames stream perfectly. Nothing rebuffers, nothing 404s, no metric moves,
so no threshold is crossed. An alert-driven agent would sleep through it
forever.

Options for Phase 3 onward, cheapest first:

1. **Scheduled content sweep** — run Phase 2 on a timer regardless of alerts.
   This is what real broadcast operations do: a confidence monitor is *always*
   watching, not woken by delivery thresholds. Cheap (one frame per rung per
   interval) and it makes the demo honest: the agent finds the fault because it
   is always looking, not because something told it to.
2. **A content-health metric** — have the encoder or a sidecar publish a cheap
   frame statistic (mean luma, or the existing Laplacian variance) so a
   *content* alert can exist alongside the delivery ones. Turns the invisible
   fault into an alertable one without pretending delivery telemetry saw it.
3. Leave it manual for the demo, and say so.

Option 1 is recommended, and option 2 is worth pairing with it — but note that
option 2 partly dissolves the story, since the point is that conventional
telemetry cannot see this. A confidence monitor that *looks at pictures* is
still DEAD AIR's thesis; a metric that makes it a threshold crossing is closer
to admitting delivery telemetry could have caught it.

## Not built yet

Phases 3–5: propose remediation, human approval gate, verify recovery, annotate
the dashboard and open an IRM incident.
