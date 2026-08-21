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

**That 5% is an undercount, and here is by how much.** It counts only
`execute_tool` spans. ADK re-resolves every toolset on *each model step*
(`base_llm_flow.py:494-498`, "the cache is refreshed each time"), and for an
`McpToolset` that means a live `list_tools` round trip
(`mcp_toolset.py:365-383`). None of it is instrumented: ADK wraps only
`execute_tool`, and `opentelemetry-instrumentation-httpx` is not installed, so
the HTTP call is invisible. Measured directly, `get_tools()` costs **~19ms
median** after a **1.4s** first-call session setup. Across ~63 model steps and
4 specialist sessions that is roughly **1-6s of hidden MCP traffic** on a 362s
run — so the real figure is nearer 7% than 5%, and the conclusion is unchanged.
It is recorded here because an unmeasured term should be named and sized, not
left out.

### How NOT to measure this — two traps I fell into

**Start spread proves nothing.** All four specialists stamp a start of 0.0s,
which looks like proof of concurrency. `ParallelAgent` creates all sub-agent
tasks with no await between them (`parallel_agent.py:82-84`), and
`BaseAgent.run_async` opens its `invoke_agent <name>` span before any awaitable
work (`base_agent.py:297-298`), so four spans stamp identical starts even under
strict serialisation. The profiler now rests its verdict on overlap and reports
start spread as context only.

**The specialists are not freely concurrent, and `busy` is not busy time.** They
are real asyncio tasks, but every event one emits blocks it until a *single
serial consumer* acknowledges: `process_an_agent` awaits `resume_signal.wait()`
after each `queue.put` (`parallel_agent.py:63-71`), the merge loop is sequential
(`parallel_agent.py:86-96`), non-partial events additionally block on session
append (`invocation_context.py:305-311`), and `_consume_event_queue`
(`runners.py:849-888`) is one loop for the whole invocation. So the `busy`
column is a span *envelope* that includes time frozen on those handshakes.

The 2.03x and 2.27x overlap ratios are still real evidence that work was in
flight simultaneously, and the conclusion — the fan-out is not what makes this
slow — survives. But "the specialists run freely in parallel" would be wrong:
they contend on one consumer, and that is a second reason a straggler hurts.

Correspondingly, **"model time 99.2%" is a union across four concurrent agents**
and saturates trivially — one agent's uninstrumented scaffolding hides beneath a
sibling's `call_llm` span. Read it as "the run is model-bound", not as "only
0.8% of each agent's time is non-model work".

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

---

# Proposed, not yet applied

## The 200k is dead weight, not context

Every agent after Phase 1 already receives **100% of what it needs through
instruction state-templating**, and templated instructions land in the
**system instruction**, not in `contents`:

* `instructions.py:108-111` — the templated instruction is appended via
  `append_instructions`, i.e. to `system_instruction`.
* `instructions.py:112-119` — the only path that puts a dynamic instruction into
  `contents` also requires `agent.static_instruction` to be set. **The repo uses
  `static_instruction` nowhere**, so that path is never taken.
* The templated inputs are explicit and auditable: `scope.py:302-314`
  (`{metrics_findings}` / `{logs_findings}` / `{traces_findings}` /
  `{dashboard_findings}`), `see.py:38,86,89`, `diagnose.py:33,90,96`,
  `act.py:73,108,111,145,148,210,213,219`.

So the ~180k of replayed Loki lines and Tempo JSON is carried **in addition to**
the findings each agent actually reads. Nothing downstream consumes it by
design. It is pure freight.

## The primitive, and why it is safe

`LlmAgent.include_contents: Literal['default','none']`
(`llm_agent.py:369`). Setting it to `'none'` swaps
`_get_contents(all session events)` for `_get_current_turn_contents`
(`contents.py:433-460`), which scans backward to the newest turn boundary
(`contents.py:900-925`).

The property that makes this safe rather than lobotomising:

```python
# functions.py:1302 -- a tool-response event is authored by the AGENT
author=invocation_context.agent.name
```

and the boundary test is `event.author == 'user' or _is_other_agent_reply(...)`
(`contents.py:913`). A tool response is therefore **never** a turn boundary, so
an agent keeps every one of its own calls and responses and drops only prior
phases. Contents can never go empty either — the user kickoff event is always a
floor match.

**Event compaction cannot substitute for this.** Its only call sites
(`runners.py:637-660`) run *after* the invocation completes, so it cannot reduce
context mid-run.

## Ranked

### Worth doing — the saving is measured and the risk is bounded

1. **`include_contents='none'` on the four synthesizers** — `scope_synthesizer`,
   `diagnose_synthesizer`, `act_synthesizer`, `record_synthesizer`. These have
   `output_schema` and consume *only* templated state, so the dropped contents
   are provably unread. Removes the ~200k tax from the four most expensive
   single calls in the run.

2. **Pre-resolve each `McpToolset` once at startup** rather than handing
   `LlmAgent` a live toolset (`scope.py:56-65` and its four call sites at
   `178,219,258,282`). Small (~7s) and low risk.

### Measure before committing

3. **`include_contents='none'` on the tool-using investigators**
   (`diagnose_investigator`, `act_proposer`, `record_investigator`) **plus**
   explicitly templating in the findings they currently pick up implicitly. The
   saving is larger than (1), but the risk is real: if one of them is silently
   relying on something in `contents`, it loses it. Requires a re-run of all six
   fault cases to confirm diagnosis quality is unchanged.

4. **A truncating `after_tool_callback`** capping each MCP response at a byte
   budget with an explicit "truncated, narrow your selector" marker.
   `agent.py:218-231` already sets `after_model_callback` and
   `before_tool_callback`; `after_tool_callback` is unused. This is the only
   proposal that also attacks the 60x growth *within* Phase 1. Risk is
   medium-high and specific: the truncated tail could be the log line naming the
   404'd URI, which is exactly what separates `segment_gap` from
   `ladder_collapse`. Do not ship this without re-running both.

### Rejected

5. **Prompt-level query budgets** ("you have N calls, report call k of N").
   Unenforced, and it cuts both ways — an agent told data is expensive may pull
   `limit=1` and report "no example lines" where five would have identified the
   fault. This agent's job is being *right* about broadcast faults; a fast wrong
   answer is worth nothing. Rejected in favour of the enforced byte cap in (4).

## The honest ceiling

Scope is 35-42% of the run and Phase 5 is another 34-36%, so no scope-only change
can win more than ~40%. The context fix is worth more than the scope fix
*because* it applies to every phase at once — which is the actual reason to
prefer it, not the raw token count.

