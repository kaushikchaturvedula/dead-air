# DEAD AIR

**An autonomous broadcast operations agent for live video streaming.**

> ⚠️ **Work in progress.** This repo is an active build for the Google Cloud
> "Agentic Cinema" hackathon (Grafana track, due 7 Sep 2026). Right now it
> contains scaffolding and a Grafana Cloud MCP connectivity probe — the
> streaming plant, chaos injection endpoints, and the DEAD AIR agent itself are
> not built yet.

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

## Planned architecture

```
  encoder  ──▶  CDN edges  ──▶  viewer fleet
     │              │               │
     └──────────────┴───────────────┘
                    │  metrics · logs · traces
                    ▼
             Grafana Cloud
                    │
                    ▼
      grafana/mcp-grafana  (self-hosted, Docker)
                    │  MCP (streamable HTTP)
                    ▼
              ADK agent  ──▶  Gemini vision on delivered segments
                    │
                    └──▶  human-gated remediation  ──▶  dashboard annotation
```

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

Requires Python 3.10+, Docker, and a Google Cloud project with Vertex AI
enabled.

```bash
git clone https://github.com/kaushikchaturvedula/dead-air.git
cd dead-air

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp agents/grafana_probe/.env.example agents/grafana_probe/.env
# then edit .env: set GOOGLE_CLOUD_PROJECT and GRAFANA_SERVICE_ACCOUNT_TOKEN
# (a Grafana service-account token -- create one in your Grafana stack under
#  Administration > Users and access > Service accounts)

gcloud auth application-default login

# start the self-hosted Grafana MCP server on localhost:8010
docker compose up -d mcp-grafana
```

Run the Grafana MCP connectivity probe:

```bash
cd agents
adk web
```

Open the URL it prints, pick the `grafana_probe` agent, and ask it about your
stack. The MCP server authenticates to Grafana with the service-account token —
no browser handshake involved.

The tools the MCP server exposes are catalogued in
[docs/mcp-tool-inventory.md](docs/mcp-tool-inventory.md).

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

**Step 4 — L3 viewer fleet (next).** ~200 modeled clients on a playback clock,
where `rebuffer_ratio` is born.

### Everything stays local until it has to move

The plant runs entirely on Docker today. That is not just cost control: an edge
in `europe-west1` needs a publicly reachable origin, so deploying L2 to Cloud
Run silently requires L1 on GCE first. Local has no such ordering constraint,
which makes it the cheaper place to be wrong.

## License

[Apache-2.0](LICENSE)
