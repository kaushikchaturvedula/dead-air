# Grafana Cloud MCP — Tool Inventory

Tools exposed by the hosted Grafana MCP server at `https://mcp.grafana.com/mcp`,
as enumerated by the `grafana_probe` agent against the DEAD AIR Grafana stack.

This inventory drives the design of the DEAD AIR operations agent: which tools it
gets, which it is denied, and which need wrapping.

> 🚧 **Placeholder.** To be populated by running the probe agent — see
> [the setup instructions](../README.md#setup). Ask it:
> *"List every tool you have access to, grouped by category, with parameters."*

| Tool name | Category | Parameters | Notes |
| --- | --- | --- | --- |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |

## Method

- **Server:** `https://mcp.grafana.com/mcp` (streamable HTTP)
- **Stack:** set via the `X-Grafana-URL` header from `GRAFANA_STACK_URL`
- **Probe agent:** [`agents/grafana_probe`](../agents/grafana_probe/), unfiltered toolset
- **Date enumerated:** _TBD_
