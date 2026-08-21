#!/usr/bin/env python3
"""Profile a DEAD AIR investigation from its own traces in Grafana Cloud.

The agent is an observable service (agents/dead_air/observability.py), so the
answer to "where does the time go" should come out of Tempo rather than out of
print statements. This is the reflexive layer being used for the thing it was
built for.

It answers, per run:

  * Are the four Phase-1 specialists ACTUALLY parallel, or serialised? Measured
    by span overlap, not by trusting that ParallelAgent parallelises.
  * How many tool calls does each specialist make?
  * How much of the wall clock is MCP round trips vs model time?

    python3 scripts/profile_agent_run.py                 # newest agent trace
    python3 scripts/profile_agent_run.py <trace_id>
    python3 scripts/profile_agent_run.py --lookback 7200
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agents"))

from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(os.path.join(REPO, "agents", "grafana_probe", ".env"))

BASE = os.environ.get("GRAFANA_URL", "").rstrip("/")
TOKEN = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
PROXY = f"{BASE}/api/datasources/proxy/uid/grafanacloud-traces"
SERVICE = os.environ.get("OTEL_SERVICE_NAME", "deadair-agent")

SPECIALISTS = ["scope_metrics", "scope_logs", "scope_traces", "scope_dashboards"]


def _get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def find_traces(lookback):
    end = int(time.time())
    url = f"{PROXY}/api/search?" + urllib.parse.urlencode({
        "q": f'{{resource.service.name="{SERVICE}"}}',
        "start": end - lookback, "end": end, "limit": 50})
    return _get(url).get("traces") or []


def fetch_trace(trace_id):
    """Tempo returns OTLP JSON: batches -> scopeSpans -> spans."""
    data = _get(f"{PROXY}/api/traces/{trace_id}")
    spans = []
    for batch in data.get("batches", []):
        for scope in batch.get("scopeSpans", []):
            for s in scope.get("spans", []):
                start = int(s.get("startTimeUnixNano", 0))
                end = int(s.get("endTimeUnixNano", 0))
                attrs = {}
                for a in s.get("attributes", []):
                    v = a.get("value", {})
                    attrs[a.get("key")] = (v.get("stringValue")
                                           or v.get("intValue")
                                           or v.get("doubleValue")
                                           or v.get("boolValue"))
                spans.append({
                    "name": s.get("name", "?"),
                    "id": s.get("spanId"),
                    "parent": s.get("parentSpanId") or None,
                    "start": start, "end": end,
                    "dur": (end - start) / 1e9,
                    "attrs": attrs,
                })
    spans.sort(key=lambda s: s["start"])
    return spans


def union_seconds(intervals):
    """Wall-clock covered by a set of [start,end] spans, overlaps merged once."""
    if not intervals:
        return 0.0
    merged = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return sum(e - s for s, e in merged) / 1e9


def classify(span):
    """Bucket a span into model time, MCP/tool time, or agent scaffolding.

    ADK DOUBLE-INSTRUMENTS EVERY LLM CALL: it emits `call_llm` wrapping a
    `generate_content <model>` span of near-identical duration. Counting both
    reports 1977s of model time inside a 900s run, which is how this was
    noticed. `generate_content` is classified as a duplicate and excluded from
    sums; wall-clock unions were always correct either way.
    """
    n = span["name"].lower()
    a = span["attrs"]
    if n.startswith("generate_content"):
        return "llm_dup"
    if "gen_ai.tool.name" in a or n.startswith(("execute_tool", "tool.", "call_tool")):
        return "tool"
    if n.startswith("call_llm") or "gen_ai.usage.input_tokens" in a:
        return "llm"
    if n.startswith(("invocation", "invoke_agent", "agent_run", "run_")):
        return "agent"
    return "other"


def profile(spans):
    if not spans:
        print("no spans in this trace")
        return

    t0 = min(s["start"] for s in spans)
    total = (max(s["end"] for s in spans) - t0) / 1e9
    by_id = {s["id"]: s for s in spans}

    def ancestors(s):
        out, cur, guard = [], s, 0
        while cur and cur["parent"] and guard < 50:
            cur = by_id.get(cur["parent"])
            guard += 1
            if cur:
                out.append(cur["name"])
        return out

    print(f"trace wall clock: {total:.1f}s   spans: {len(spans)}\n")

    # ---- Are the specialists actually parallel? -------------------------
    print("PHASE 1 SPECIALISTS -- parallel or serialised?")
    # "window", not "busy": this is the span envelope. A specialist is frozen
    # after every event it emits until a single serial consumer acknowledges
    # (parallel_agent.py:63-71 and 86-96, invocation_context.py:305-311,
    # runners.py:849-888), so the envelope includes handshake blocking that the
    # agent did not spend doing work.
    print(f"  {'specialist':<20}{'start':>8}{'end':>8}{'window':>8}"
          f"{'llm':>7}{'tool':>7}{'calls':>7}")
    print("  " + "-" * 65)

    windows = {}
    for name in SPECIALISTS:
        # ADK names agent spans "invoke_agent <name>", and tags LLM spans with
        # deadair.agent / gen_ai.agent.name. Match on all three so a span is
        # attributed whether it is the agent span, a descendant, or only
        # labelled.
        owned = [s for s in spans
                 if s["name"] == f"invoke_agent {name}"
                 or f"invoke_agent {name}" in ancestors(s)
                 or s["attrs"].get("deadair.agent") == name
                 or s["attrs"].get("gen_ai.agent.name") == name]
        if not owned:
            print(f"  {name:<20}{'-- no spans --':>38}")
            continue
        st = (min(s["start"] for s in owned) - t0) / 1e9
        en = (max(s["end"] for s in owned) - t0) / 1e9
        windows[name] = (min(s["start"] for s in owned),
                         max(s["end"] for s in owned))
        llm = sum(s["dur"] for s in owned if classify(s) == "llm")
        tools = [s for s in owned if classify(s) == "tool"]
        tool_t = sum(s["dur"] for s in tools)
        print(f"  {name:<20}{st:>7.1f}s{en:>7.1f}s{en - st:>7.1f}s"
              f"{llm:>6.1f}s{tool_t:>6.1f}s{len(tools):>7}")

    if len(windows) > 1:
        span_sum = sum((e - s) / 1e9 for s, e in windows.values())
        wall = union_seconds([[s, e] for s, e in windows.values()])
        starts = [(s - t0) / 1e9 for s, _ in windows.values()]
        ends = [(e - t0) / 1e9 for _, e in windows.values()]
        spread = max(starts) - min(starts)
        slowest = max(windows.items(), key=lambda kv: kv[1][1] - kv[1][0])

        print(f"\n  sum of specialist windows : {span_sum:.1f}s")
        print(f"  wall clock they occupy    : {wall:.1f}s")
        print(f"  start spread              : {spread:.1f}s "
              f"(0 = launched simultaneously)")
        print(f"  ratio sum/wall            : {span_sum / wall:.2f}x"
              if wall else "")

        # THE VERDICT RESTS ON OVERLAP, NOT ON START SPREAD.
        #
        # A ~0s start spread looks like proof of concurrency and is not.
        # ParallelAgent creates all sub-agent tasks with no await between them
        # (parallel_agent.py:82-84), and BaseAgent.run_async opens the
        # `invoke_agent <name>` span before any awaitable work
        # (base_agent.py:297-298). So four spans would stamp near-identical
        # start times even if the event loop then ran them strictly one after
        # another. Start spread is reported below as context, not as evidence.
        #
        # Overlap is the real test: if the windows genuinely sum to more than
        # the wall clock they occupy, work was in flight simultaneously.
        # A ratio alone is not a verdict either -- one hung specialist drags
        # the union out to its own length and makes three genuinely concurrent
        # agents look serialised -- so the critical path is reported alongside.
        ratio = span_sum / wall if wall else 0
        if ratio >= 2.5:
            print("  verdict                   : GENUINELY CONCURRENT "
                  f"({ratio:.2f}x more work than wall clock)")
        elif ratio >= 1.3:
            print(f"  verdict                   : CONCURRENT but dominated by "
                  f"one straggler ({ratio:.2f}x of a possible "
                  f"{len(windows)}.00x)")
        else:
            print(f"  verdict                   : NO REAL OVERLAP ({ratio:.2f}x)"
                  " -- either serialised, or one agent dwarfs the rest")
        print(f"  CRITICAL PATH             : {slowest[0]} at "
              f"{(slowest[1][1] - slowest[1][0]) / 1e9:.1f}s -- the phase "
              f"cannot finish sooner than this")

    # The synthesizer is serial AFTER the fan-out, so it adds directly.
    synth = [s for s in spans
             if s["name"] == "invoke_agent scope_synthesizer"
             or s["attrs"].get("deadair.agent") == "scope_synthesizer"
             or s["attrs"].get("gen_ai.agent.name") == "scope_synthesizer"]
    if synth:
        sd = (max(s["end"] for s in synth) - min(s["start"] for s in synth)) / 1e9
        print(f"  scope_synthesizer         : {sd:.1f}s (serial, adds on top)")

    # ---- Which phase owns the run? --------------------------------------
    # The question "is scope the problem" is only answerable against the other
    # four phases, so measure all five rather than scope in isolation.
    print("\nPHASE BREAKDOWN")
    phases = [("phase1_scope", "1 scope"), ("phase2_see", "2 see"),
              ("phase3_diagnose", "3 diagnose"), ("phase4_act", "4 act"),
              ("phase5_record", "5 record")]
    for pname, label in phases:
        owned = [s for s in spans if s["name"] == f"invoke_agent {pname}"]
        if not owned:
            # Fall back to descendants when the phase span itself is absent.
            owned = [s for s in spans if f"invoke_agent {pname}" in ancestors(s)]
        if not owned:
            print(f"  {label:<14}{'-- not run --':>12}")
            continue
        d = (max(s["end"] for s in owned) - min(s["start"] for s in owned)) / 1e9
        bar = "#" * max(1, int(40 * d / total)) if total else ""
        print(f"  {label:<14}{d:>7.1f}s  {100 * d / total if total else 0:>5.1f}%  {bar}")

    # ---- Where does the time actually go? -------------------------------
    print("\nTIME BREAKDOWN (union wall clock, overlaps counted once)")
    for kind in ("llm", "tool"):
        sel = [s for s in spans if classify(s) == kind]
        u = union_seconds([[s["start"], s["end"]] for s in sel])
        raw = sum(s["dur"] for s in sel)
        label = "model time" if kind == "llm" else "MCP / tool round trips"
        pct = 100 * u / total if total else 0
        print(f"  {label:<26}{u:>7.1f}s wall  ({pct:>4.1f}% of run)   "
              f"{raw:>7.1f}s summed over {len(sel)} spans")

    covered = union_seconds(
        [[s["start"], s["end"]] for s in spans if classify(s) in ("llm", "tool")])
    print(f"  {'unaccounted / scaffolding':<26}{total - covered:>7.1f}s wall  "
          f"({100 * (total - covered) / total if total else 0:>4.1f}% of run)")

    # ---- The slowest individual operations ------------------------------
    print("\nSLOWEST 12 OPERATIONS")
    ranked = sorted([s for s in spans if classify(s) in ("llm", "tool")],
                    key=lambda s: -s["dur"])[:12]
    for s in ranked:
        tool = s["attrs"].get("gen_ai.tool.name", "")
        tok = s["attrs"].get("gen_ai.usage.input_tokens", "")
        extra = f"  tool={tool}" if tool else (f"  in_tok={tok}" if tok else "")
        print(f"  {s['dur']:>7.1f}s  {classify(s):<5} {s['name'][:44]:<44}{extra}")

    # ---- Tool call census ------------------------------------------------
    print("\nTOOL CALL CENSUS")
    census = {}
    for s in spans:
        if classify(s) != "tool":
            continue
        key = s["attrs"].get("gen_ai.tool.name") or s["name"]
        c = census.setdefault(key, {"n": 0, "t": 0.0})
        c["n"] += 1
        c["t"] += s["dur"]
    if not census:
        print("  none recorded")
    for k, v in sorted(census.items(), key=lambda kv: -kv[1]["t"]):
        print(f"  {v['n']:>3}x  {v['t']:>7.1f}s total  {v['t']/v['n']:>6.1f}s avg  {k}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id", nargs="?")
    ap.add_argument("--lookback", type=int, default=7200)
    args = ap.parse_args()

    if not BASE or not TOKEN:
        sys.exit("Grafana credentials missing from agents/grafana_probe/.env")

    trace_id = args.trace_id
    if not trace_id:
        traces = find_traces(args.lookback)
        if not traces:
            sys.exit(f"no {SERVICE} traces in the last {args.lookback}s -- run an "
                     f"investigation first, and check `make agent-observability-check`")
        # Longest trace in the window is the investigation; short ones are
        # Stage 0 screens and the observability self-check.
        traces.sort(key=lambda t: -int(t.get("durationMs") or 0))
        trace_id = traces[0].get("traceID")
        print(f"profiling newest/longest trace {trace_id} "
              f"({int(traces[0].get('durationMs') or 0)/1000:.1f}s), "
              f"{len(traces)} candidates in window\n")

    profile(fetch_trace(trace_id))


if __name__ == "__main__":
    main()
