"""DEAD AIR -- autonomous broadcast operations agent.

Phase lifecycle as a SequentialAgent; Phase 1's fan-out as a ParallelAgent.

    root (SequentialAgent)
      phase1_scope (SequentialAgent)
        scope_fanout (ParallelAgent)   metrics | logs | traces | dashboards
        scope_synthesizer              -> IncidentScope   (validated)
      phase2_see (SequentialAgent)
        see_investigator               fetch manifest, frames, run detectors
        see_synthesizer                -> VisualFinding   (validated)

Phases 3-5 (propose remediation, human gate, verify recovery and annotate) are
not built yet.

The agent is woken by a Grafana alert webhook carrying the affected region --
see scripts/run_agent.py. That region is its starting scope, not its conclusion:
Phase 1 is explicitly instructed to check the regions it was NOT alerted about,
because a fault confined to one region rules out the encoder entirely.

Constructed synchronously at module import. Do not switch to an async factory:
that works under `adk web` but breaks on Cloud Run and Agent Engine.
"""

import os

from google.adk.agents import SequentialAgent

from .scope import phase1_scope
from .see import phase2_see

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

root_agent = SequentialAgent(
    name="dead_air",
    description=(
        "Diagnoses live video streaming incidents by correlating Grafana Cloud "
        "metrics, logs and traces with the actual delivered pixels."
    ),
    sub_agents=[phase1_scope, phase2_see],
)
