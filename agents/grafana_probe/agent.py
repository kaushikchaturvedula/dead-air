"""Grafana Cloud MCP probe agent.

This agent does one job: prove that we can reach the Grafana Cloud MCP server
and enumerate every tool it exposes. It is deliberately dumb -- no remediation
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

# Grafana Cloud's hosted MCP endpoint. Streamable HTTP only -- this endpoint
# does not serve the older SSE transport, so SseConnectionParams will not work.
GRAFANA_MCP_URL = "https://mcp.grafana.com/mcp"

# Which Grafana stack the hosted MCP server should act against, e.g.
# https://deadair.grafana.net -- passed per-request as a header.
GRAFANA_STACK_URL = os.environ.get("GRAFANA_STACK_URL", "")

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
    model="gemini-flash-latest",
    description=(
        "Read-only probe that connects to the Grafana Cloud MCP server and "
        "enumerates the tools it exposes."
    ),
    instruction=INSTRUCTION,
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=GRAFANA_MCP_URL,
                headers={"X-Grafana-URL": GRAFANA_STACK_URL},
            ),
            # No tool_filter: the whole point of this agent is to see
            # everything the MCP server offers.
        ),
    ],
)
