# Where an investigation's time actually goes

Measured with `make agent-profile`, which reads the agent's own traces back out
of Grafana Cloud Tempo. The reflexive layer built for §1 is doing the thing it
was built for: DEAD AIR is debugged by the technique DEAD AIR practises.

Two full runs, both verified correct end to end:

| | ladder_collapse | segment_gap |
| --- | --- | --- |
| wall clock | 362.2s | 568.9s |
| spans | 224 | 213 |
| LLM calls | 63 | 60 |
| input tokens | 4,329,077 | 4,573,692 |
| output tokens | 17,329 | 17,039 |
| **input:output** | **249:1** | **268:1** |
| cache-read | 52% | 46% |
| est. Vertex cost | $1.34 | $1.42 |

## Finding 1 — it is not Grafana, and it is not the fan-out

**MCP round trips are ~5% of the run. Model time is 99%.**

```
model time                359.4s wall   99.2% of run
MCP / tool round trips     ~18.7s        ~5% of run, 74 calls, ~0.25s each
```

Every MCP tool averages a quarter-second: `query_prometheus` 0.2s,
`query_loki_logs` 0.2s, `tempo_traceql-search` 0.2s, `query_loki_stats` 1.2s.
There is no latency problem to fix in the Grafana layer. (A 60.3s
`verify_recovery` span in Phase 5 is a *local* polling tool that deliberately
waits for the plant to settle, not an MCP call.)

The four specialists do overlap: 238.4s of specialist work inside 117.6s of wall
clock, 2.03x — and 404.4s inside 178.3s, 2.27x, on the second run.

**A caveat on how NOT to measure this.** All four specialists stamp a start time
of 0.0s, which looks like proof of concurrency and is not. `ParallelAgent`
creates all sub-agent tasks with no await between them
(`parallel_agent.py:82-84`), and `BaseAgent.run_async` opens its
`invoke_agent <name>` span before any awaitable work (`base_agent.py:297-298`),
so four spans would stamp identical starts even if the loop then ran them
strictly one after another. The profiler reports start spread as context and
rests its verdict on overlap, which is real evidence.

## Finding 2 — scope is not 52%, and Phase 5 is just as expensive

The working assumption was that Phase 1 dominates. Measured across both runs it
does not, and the phase nobody was looking at costs the same:

| phase | ladder_collapse | segment_gap |
| --- | --- | --- |
| 1 scope | 151.5s — 41.8% | 202.1s — 35.5% |
| 2 see | 0.0s — 0% (skipped) | 86.7s — 15.2% |
| 3 diagnose | 54.9s — 15.2% | 41.3s — 7.3% |
| 4 act | 33.2s — 9.2% | 35.1s — 6.2% |
| **5 record** | **122.6s — 33.8%** | **203.7s — 35.8%** |

Optimising scope alone therefore caps out at ~40% of the run even if it were
made free. **Phase 5 — writing the postmortem — is the equal-largest cost and
had never been looked at.**

The critical path inside scope also *moves between runs*: `scope_logs` at 117.5s
on one, `scope_metrics` at 178.3s on the other. It is not one bad specialist; it
is that no specialist has a bound, so whichever one wanders longest sets the
floor and the other three sit finished.

## Finding 3 — the real lever: context is never released

Input tokens per call grow monotonically inside each specialist, then never come
back down for the rest of the run:

```
scope_logs        1,669  ->  101,690   over 24 calls   (60x)
scope_traces        719  ->   64,799   over 19 calls
scope_metrics     1,977  ->    8,625
scope_dashboards    681  ->    5,113
```

Then **every single call after Phase 1 sits at ~200k input tokens**:

```
scope_synthesizer      199,609
diagnose_investigator  194,872 / 196,799
diagnose_synthesizer   200,298
act_proposer           198,642 / 198,639
act_synthesizer        200,463
record_investigator    201,456 / 201,846 / 202,267 / 202,743 / 203,372
record_synthesizer     203,306
```

That floor is why Phases 3-5 are slow. It is not what they are asked to do; it
is what they are carrying while they do it. The specialists' actual reports are
tiny — 2,291 / 2,483 / 2,649 characters. The 200k is raw tool payloads: Loki log
lines fetched at `limit=100`, whole Tempo traces pulled by ID.

### The mechanism, verified in ADK source

ADK is **not** context-blind. It has real per-agent scoping, and the scoping
works — for exactly one case.

```python
# flows/llm_flows/contents.py:1124-1137
def _is_event_belongs_to_branch(invocation_branch, event):
    if not invocation_branch or not event.branch:
        return True                       # <-- no branch = see EVERYTHING
    inv_path = _BranchPath.from_string(invocation_branch)
    evt_path = _BranchPath.from_string(event.branch)
    return inv_path == evt_path or inv_path.is_descendant_of(evt_path)
```

`ParallelAgent._create_branch_ctx_for_sub_agent` (`parallel_agent.py:40-51`) is
**the only place in ADK that ever sets a branch**. `SequentialAgent` never sets
one, and `InvocationContext.branch` defaults to `None`
(`invocation_context.py:158`).

So:

* The four Phase-1 specialists **are** genuinely firewalled from each other —
  each gets a distinct sub-branch of equal depth, which fails both the `==` and
  the `is_descendant_of` test. This part works.
* Everything else — `scope_synthesizer`, and all of Phases 2, 3, 4 and 5 — runs
  under `SequentialAgent` with `branch=None`, so the guard returns `True` on the
  first line and **every raw tool response from all four specialists is
  re-rendered into every downstream request**, for the rest of the run.

Branch isolation only works *downward from a ParallelAgent*. Nothing in the
five-phase spine is under one.

That is the whole 200k floor, and it is why this is a single fix with leverage
across four phases rather than a scope problem.

## What this cost

~$1.40 of Vertex tokens per investigation, 4.3-4.6M input tokens against 17k of
actual output. Roughly half is cache-read, so the billed figure is softer than
the raw count — but cache hits still have to be transmitted and attended to, and
latency is what the demo is judged on.
