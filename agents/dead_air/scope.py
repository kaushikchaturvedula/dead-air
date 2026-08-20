"""Phase 1 (SCOPE): parallel fan-out across metrics, logs, traces, dashboards.

Four specialists query Grafana Cloud concurrently through the self-hosted MCP
server, then a synthesiser folds their findings into one validated
IncidentScope.

TOOL PINNING
------------
The MCP server exposes 73 tools. All 73 are NOT handed to the model. Two
reasons, and the second matters more:

1. Function-calling accuracy degrades as the declaration list grows. 73 tools
   is far past the point where a model reliably picks the right one.
2. Determinism. A pinned toolset means the agent's search space is a design
   decision we can review, not an emergent property of whatever the MCP server
   happens to expose that week. If the server adds tools tomorrow, this agent's
   behaviour does not change.

Each specialist gets its OWN McpToolset with its own filter, so no agent sees
more than four tools. Twelve distinct tools across the whole phase, each
justified below.

Datasource UIDs are hardcoded into the instructions rather than discovered with
list_datasources. They are stable properties of the stack, and hardcoding
removes an entire class of "the agent picked the wrong datasource" failure.
"""

import os

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

from .schemas import IncidentScope

GRAFANA_MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8010/mcp")
# Shared factory: one tier decision, one retry policy. See model.py for why
# backoff is mandatory given the ParallelAgent fan-out.
from .model import build_model

PROM_UID = "grafanacloud-prom"
LOKI_UID = "grafanacloud-logs"
TEMPO_UID = "grafanacloud-traces"


# Bounded so one unresponsive call cannot stall a whole phase. The library
# defaults are timeout=5s but sse_read_timeout=300s -- five minutes per read,
# multiplied by four parallel specialists making several calls each, which is
# how an investigation quietly turns into a hang instead of an error.
MCP_CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "10"))
MCP_READ_TIMEOUT = float(os.environ.get("MCP_READ_TIMEOUT", "60"))


def _toolset(tool_names):
    """One MCP connection, filtered to an explicit tool list."""
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=GRAFANA_MCP_URL,
            timeout=MCP_CONNECT_TIMEOUT,
            sse_read_timeout=MCP_READ_TIMEOUT,
        ),
        tool_filter=list(tool_names),
    )


# --- the pinned subsets, with justification --------------------------------
#
# METRICS (4)
#   query_prometheus              the workhorse; every quantitative claim
#   query_prometheus_histogram    p95 segment TTFB, which is how edge_latency
#                                 shows itself. Doing this by hand from _bucket
#                                 series is exactly the kind of PromQL a model
#                                 gets subtly wrong.
#   list_prometheus_label_values  enumerate regions/renditions actually present,
#                                 so "all regions" is checked, not assumed
#   list_prometheus_metric_names  discovery fallback when an expected metric is
#                                 missing -- distinguishes "metric absent" from
#                                 "metric healthy", which is a real diagnosis
METRICS_TOOLS = [
    "query_prometheus",
    "query_prometheus_histogram",
    "list_prometheus_label_values",
    "list_prometheus_metric_names",
]

# LOGS (3)
#   query_loki_logs         the 404 storm that identifies segment_gap
#   query_loki_stats        cheap volume check before pulling lines; keeps a
#                           high-volume window from flooding the context
#   list_loki_label_values  which regions/renditions actually appear in logs
LOGS_TOOLS = [
    "query_loki_logs",
    "query_loki_stats",
    "list_loki_label_values",
]

# TRACES (2)
#   tempo_traceql-search  find slow client->edge->origin fetches
#   tempo_get-trace       pull one full span tree to attribute latency to a hop
# Deliberately NOT the traceql-metrics tools: aggregate trace metrics duplicate
# what Mimir already answers better, and every extra tool costs accuracy.
TRACES_TOOLS = [
    "tempo_traceql-search",
    "tempo_get-trace",
]

# DASHBOARDS (3)
#   search_dashboards          locate the operator's view of this incident
#   get_dashboard_summary      compact metadata; get_dashboard_by_uid returns
#                              the entire JSON model and would swamp the context
#   get_dashboard_panel_queries  the queries operators actually watch, which is
#                              also what a postmortem annotation must reference
DASHBOARD_TOOLS = [
    "search_dashboards",
    "get_dashboard_summary",
    "get_dashboard_panel_queries",
]

ALL_PINNED = METRICS_TOOLS + LOGS_TOOLS + TRACES_TOOLS + DASHBOARD_TOOLS

_COMMON_RULES = """
Rules that override anything else:
- Never invent a number. Every value you report must have come back from a tool
  call in this conversation.
- If a tool errors, report the error verbatim and say the signal is unavailable.
  Unavailable is NOT the same as healthy, and must never be reported as healthy.
- Check the regions you were NOT alerted about. Establishing that other regions
  are fine is what separates a regional fault from a plant-wide one, and it is
  the single most valuable thing this phase produces.
- Report observations, not a diagnosis. The next phase decides what it means.
"""

metrics_agent = LlmAgent(
    name="scope_metrics",
    model=build_model(),
    description="Queries Mimir for the metric shape of the incident.",
    instruction=f"""\
You are the metrics specialist for a live video streaming plant.

An alert has fired. Alert: {{alert_name}}. Region named in the alert:
{{alert_region}}. Fired at: {{alert_time}}.

Use datasourceUid "{PROM_UID}" for every query.

The plant's metrics (label names given in prose, not brace syntax):
  rebuffer_ratio                 labels region, device_class. Viewer stalling 0.0-1.0
  viewer_rebuffer_seconds_total  labels region, device_class. Counter, seconds
  viewer_playing_seconds_total   labels region, device_class. Counter, seconds
  viewer_bitrate_avg             labels region, device_class. Bits/sec delivered
  segment_ttfb_seconds_bucket    labels region, le. Histogram of edge TTFB
  segment_status                 labels region, status (2xx / 4xx / 5xx)
  edge_cache_hit_ratio           label region
  origin_shield_miss_total       label region
  encoder_fps                    no labels. Encoder frame rate
  dropped_frames                 no labels. Counter
  encoder_up                     no labels. 1 while ffmpeg runs
  encoder_rungs_active           no labels. Rungs currently produced
  packager_segment_lag           label rendition. Seconds since that rung
                                 last produced a segment

Regions are: us-east1, europe-west1, asia-south1.

Establish, with numbers:
1. rebuffer_ratio in ALL THREE regions, not only the alerted one.
2. viewer_bitrate_avg per region -- has delivered quality dropped?
3. p95 of segment_ttfb_seconds per region (use the histogram tool).
4. segment_status 4xx rate per region -- and whether it is ongoing or a burst.
5. encoder_fps, dropped_frames, encoder_up, encoder_rungs_active.
6. packager_segment_lag per rendition -- is any rung not producing?

Then state plainly whether the DELIVERY metrics look healthy or degraded. If
every delivery metric looks normal while an alert is firing, say so explicitly
and loudly -- that combination is the signature of a fault that telemetry
cannot see, and it is what tells the next phase to go and look at pixels.
{_COMMON_RULES}""",
    tools=[_toolset(METRICS_TOOLS)],
    output_key="metrics_findings",
)

logs_agent = LlmAgent(
    name="scope_logs",
    model=build_model(),
    description="Queries Loki for origin and viewer log evidence.",
    instruction=f"""\
You are the logs specialist for a live video streaming plant.

Alert: {{alert_name}}. Region named in the alert: {{alert_region}}.

Use datasourceUid "{LOKI_UID}".

Two log streams exist, selected by their job label:

  job "deadair-origin"    nginx origin access log. JSON lines carrying
                          uri, status, rendition, session_id, traceparent
  job "deadair-viewers"   per-session QoE beacons. JSON lines carrying
                          session_id, region, device_class, rebuffer_count,
                          rebuffer_sec, startup_time, bitrate

LogQL stream selectors look like {{job="deadair-origin"}}, and can be filtered
further, for example {{job="deadair-origin"}} |= "404".

Per-session detail lives ONLY here -- it is deliberately absent from metrics --
so this is where "which segment failed for whom" is answered.

Establish:
1. Volume of origin 4xx/5xx over the last 15 minutes (use the stats tool first
   so you do not pull an enormous window).
2. If errors exist: which URIs and renditions, and whether they are spread
   across all regions or concentrated. Sustained 404s across every region point
   at the packager; a burst that stops points at a rendition being withdrawn.
3. Recent viewer beacons for the alerted region -- what rebuffer_count and
   rebuffer_sec are real sessions reporting?
4. Whether origin logs show normal segment delivery continuing.

Quote actual log lines as evidence.
{_COMMON_RULES}""",
    tools=[_toolset(LOGS_TOOLS)],
    output_key="logs_findings",
)

traces_agent = LlmAgent(
    name="scope_traces",
    model=build_model(),
    description="Queries Tempo for request-path latency attribution.",
    instruction=f"""\
You are the traces specialist for a live video streaming plant.

Alert: {{alert_name}}. Region named in the alert: {{alert_region}}.

Use datasourceUid "{TEMPO_UID}".

Traces follow one segment fetch through the plant:
  viewer.segment_fetch    [deadair-viewers]            root, the client
    edge.serve_segment    [deadair-edge-<region>]      the CDN edge
      origin.fetch_segment                             only on a cache miss

Useful span attributes: deadair.region, deadair.session_id, deadair.rendition,
deadair.cache (HIT/MISS), deadair.injected_delay_seconds, http.status_code.

TraceQL examples (substitute the alerted region where shown):
  {{resource.service.name="deadair-viewers"}}
  {{name="origin.fetch_segment"}}
  {{span.deadair.region="us-east1"}}

Establish:
1. Are there recent traces for the alerted region at all?
2. How long are viewer.segment_fetch spans there, versus other regions?
3. For a slow fetch, WHERE did the time go -- in edge.serve_segment itself, or
   in the nested origin.fetch_segment? That distinction separates a sick edge
   from a sick origin, and no metric or log can answer it.
4. Any spans with error status.

Traces are head-sampled at about 2%, so a thin result set is normal and is NOT
evidence of a problem. Say so rather than reading significance into it.
{_COMMON_RULES}""",
    tools=[_toolset(TRACES_TOOLS)],
    output_key="traces_findings",
)

dashboard_agent = LlmAgent(
    name="scope_dashboards",
    model=build_model(),
    description="Locates the operator-facing dashboards for this incident.",
    instruction=f"""\
You are the dashboard specialist for a live video streaming plant.

Alert: {{alert_name}}. Region named in the alert: {{alert_region}}.

Find the dashboards a human on-call engineer would open for this incident.

1. Search for dashboards tagged or titled for this plant (try "DEAD AIR",
   "plant").
2. For the most relevant one, get its SUMMARY -- not the full JSON model, which
   is enormous and would swamp this conversation.
3. Retrieve the panel queries for the panels relevant to the alert, so a later
   postmortem annotation can reference exactly what an operator was looking at.

Report dashboard UIDs, titles, and the panels that matter for this alert.
{_COMMON_RULES}""",
    tools=[_toolset(DASHBOARD_TOOLS)],
    output_key="dashboard_findings",
)

scope_fanout = ParallelAgent(
    name="scope_fanout",
    description="Fans out across metrics, logs, traces and dashboards at once.",
    sub_agents=[metrics_agent, logs_agent, traces_agent, dashboard_agent],
)

# An agent with output_schema cannot use tools in ADK, so synthesis is a
# separate step that reads the specialists' findings out of session state.
scope_synthesizer = LlmAgent(
    name="scope_synthesizer",
    model=build_model(),
    description="Folds the four specialist reports into one IncidentScope.",
    instruction="""\
You are the incident commander for a live video streaming plant. Four
specialists have just reported in parallel. Produce a single IncidentScope.

Alert: {alert_name}. Region named in the alert: {alert_region}.

METRICS
{metrics_findings}

LOGS
{logs_findings}

TRACES
{traces_findings}

DASHBOARDS
{dashboard_findings}

Your job is SCOPE -- what is affected -- not root cause. Be strict:

- affected_regions and healthy_regions must both be populated from evidence. If
  a specialist did not check a region, it belongs in neither list, and the gap
  belongs in signals_unavailable.
- blast_radius follows from that: one region degraded with the others confirmed
  healthy is single_region; all three degraded is all_regions or plant_wide.
- Use blast radius to rule faults out, and record each elimination in ruled_out
  with the observation that justified it. A fault in exactly one region cannot
  be the encoder, because all three regions are fed by the same encoder.
- candidate_faults lists only what survives, most likely first.

The fault menu:
  edge_latency     one region slow; rebuffer and TTFB up there, others normal
  segment_gap      SUSTAINED 4xx in every region; bitrate normal
  ladder_collapse  a rung stops being produced; bitrate drops with NO
                   rebuffering; brief 4xx burst that then STOPS
  black_source     every delivery metric normal; only the picture is wrong
  ladder_mismatch  every delivery metric normal; only picture DETAIL is wrong

Note the trap: ladder_collapse also produces 404s. What separates it from
segment_gap is whether the 4xx are ongoing or stopped. If you cannot tell from
the evidence, keep both candidates and say why.

Set needs_visual_inspection to true whenever delivery metrics look healthy but
an alert is firing, or whenever the evidence cannot separate the last two
faults. Those are exactly the cases pixels resolve and telemetry cannot.

Set inspect_region to the region to fetch from -- the alerted region if it is
degraded, otherwise any region, since a content fault affects all of them
equally. Set inspect_rendition to 1080p unless evidence points elsewhere.

Do not overstate confidence. If the signals disagree, say so in scope_summary
and lower the number.""",
    output_schema=IncidentScope,
    output_key="incident_scope",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

phase1_scope = SequentialAgent(
    name="phase1_scope",
    description="Phase 1: bound the blast radius and emit an IncidentScope.",
    sub_agents=[scope_fanout, scope_synthesizer],
)
