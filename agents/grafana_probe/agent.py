"""Grafana MCP probe agent.

This agent does one job: prove that we can reach the (self-hosted) Grafana MCP
server and enumerate every tool it exposes. It is deliberately dumb -- no remediation
logic, no video inspection, no dashboard annotation. Those belong to the DEAD
AIR operations agent proper, which is built on top of the tool inventory this
probe produces.

The toolset is intentionally left unfiltered so that `adk web` can be used to
interrogate the full surface area of the MCP server.
"""

import os

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

# Self-hosted Grafana MCP server (grafana/mcp-grafana via docker-compose.yml),
# speaking streamable HTTP. We do NOT use Grafana's hosted mcp.grafana.com
# endpoint: it requires an interactive OAuth 2.1 browser handshake with no
# service-account option, so it cannot back a headless agent woken by an alert
# webhook. The self-hosted server authenticates to the Grafana stack itself
# (GRAFANA_URL + GRAFANA_SERVICE_ACCOUNT_TOKEN in its environment), so the
# client connection here needs no auth headers.
GRAFANA_MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8010/mcp")

# Optional caller-auth bearer token for the MCP server itself (not the Grafana
# stack). mcp-grafana warns at startup that serving without one "will become a
# startup error in a future release" -- if you set MCP_GRAFANA_SERVER_TOKEN in
# .env, docker-compose passes it to the server and this client presents it.
_MCP_SERVER_TOKEN = os.environ.get("MCP_GRAFANA_SERVER_TOKEN", "")

# Gemini 3.x flash models are served from the `global` Vertex location only;
# us-central1 tops out at gemini-2.5-flash. Note that the `gemini-flash-latest`
# alias is AI Studio-only and 404s on Vertex, so it is not a safe default here.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

INSTRUCTION = """\
You are a read-only observability probe for the DEAD AIR project. You are
connected to a Grafana Cloud stack through the Grafana MCP server.

Rules you must follow exactly:

1. Never invent, guess, extrapolate, or illustrate data. Every metric value,
   log line, trace, dashboard name, label, and tool name you report must have
   come back from an actual tool call in this conversation. If you have not
   called a tool, you do not know the answer.
2. If a tool call fails, report the error verbatim -- the exact error string,
   unedited and unsummarised -- and then stop. Do not retry silently, do not
   paper over the failure, and do not substitute plausible-looking output.
3. If you cannot answer because a tool does not exist or a query returned
   nothing, say so plainly. "No data returned" is a valid and useful answer.
4. When asked to inventory your tools, list them exhaustively from your actual
   tool registry. Include every tool, its parameters, and its stated purpose.
   Do not omit tools for brevity and do not embellish descriptions.

Accuracy matters more than helpfulness here. This probe's output is used to
build an autonomous remediation agent, so a confident fabrication is far worse
than an admission of ignorance.
"""

# NOTE: root_agent and its toolset are constructed synchronously at module
# import time. Do not switch to an async factory / await-based construction:
# that pattern happens to work under `adk web` (which has a running loop) but
# breaks when the agent is deployed to Cloud Run or Agent Engine.
root_agent = Agent(
    name="grafana_probe",
    model=GEMINI_MODEL,
    description=(
        "Read-only probe that connects to the Grafana Cloud MCP server and "
        "enumerates the tools it exposes."
    ),
    instruction=INSTRUCTION,
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=GRAFANA_MCP_URL,
                headers=(
                    {"Authorization": f"Bearer {_MCP_SERVER_TOKEN}"}
                    if _MCP_SERVER_TOKEN
                    else None
                ),
            ),
            # No tool_filter: the whole point of this agent is to see
            # everything the MCP server offers.
        ),
    ],
)
