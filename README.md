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

## License

[Apache-2.0](LICENSE)
