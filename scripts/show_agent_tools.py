#!/usr/bin/env python3
"""Print which MCP tools each Phase-1 specialist is pinned to.

The pinning is half the determinism story: the MCP server exposes 73 tools, and
handing all of them to a model degrades function-calling accuracy badly. This
makes the actual, resolved subset visible so it can be reviewed rather than
assumed -- and so a change in what the server exposes cannot silently widen the
agent's search space.
"""

import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agents"))

from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(os.path.join(REPO, "agents", "grafana_probe", ".env"))

from dead_air.scope import (                                      # noqa: E402
    ALL_PINNED, dashboard_agent, logs_agent, metrics_agent, traces_agent,
)


async def main():
    total = set()
    for agent in (metrics_agent, logs_agent, traces_agent, dashboard_agent):
        toolset = agent.tools[0]
        names = sorted(t.name for t in await toolset.get_tools())
        total |= set(names)
        print(f"  {agent.name:<18} {len(names)}  {', '.join(names)}")
        await toolset.close()

    print(f"\n  {len(total)} distinct tools pinned, of 73 the MCP server exposes")
    if total != set(ALL_PINNED):
        missing = sorted(set(ALL_PINNED) - total)
        extra = sorted(total - set(ALL_PINNED))
        print(f"  WARNING: resolved set differs from ALL_PINNED "
              f"(missing={missing} extra={extra})")
        sys.exit(1)
    print("  resolved set matches the declared allowlist")


if __name__ == "__main__":
    asyncio.run(main())
