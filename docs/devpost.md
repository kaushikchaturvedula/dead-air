# DEAD AIR — Devpost writeup (draft)

Copy-paste source for the submission. Every number here is measured on the
running system and traceable to a doc in this repo; nothing is projected.

---

## Tagline

**Every dashboard is green and the screen is black.** DEAD AIR is an autonomous
broadcast operations agent that catches the failures observability is
structurally blind to.

---

## Inspiration

Delivery telemetry measures whether *bytes* arrived. It cannot see whether those
bytes contain a *picture*.

When an encoder's input goes black, the segments keep flowing on schedule.
Bitrate is nominal. Segment lag keeps its normal sawtooth. Error rates are zero.
Every panel is green — and every viewer is staring at a black rectangle.

This is not hypothetical, and we did not have to argue for it. We built the
plant and measured it. Under a total blackout, `rebuffer_ratio` reads
**0.0001 against an alert threshold of 0.02** — two hundred times below the line
that would page anyone. After ten minutes of black, all three regional alert
instances still read `Normal`. **Zero webhooks fired.**

That is the gap. An on-call engineer finds out when a viewer tweets.

---

## What it does

DEAD AIR watches a live video plant the way a broadcast engineer does — by
looking at the picture — and treats delivery telemetry as necessary but not
sufficient.

It runs a **confidence monitor** that pulls the actual HLS segment being served
to viewers, screens it, and escalates only when something is wrong. When it
finds a fault it queries Grafana Cloud across **metrics, logs and traces**
through a self-hosted Grafana MCP server, reaches a diagnosis from a
deterministic evidence checklist, proposes exactly one remediation, **stops for
human approval**, and then verifies recovery with the check that fault class
actually requires — before annotating the dashboard and filing a postmortem.

It is also an observable service in its own right: its traces, token counts and
estimated cost land in the same Grafana stack it is investigating.

---

## How we built it

**Google ADK + Gemini via Vertex AI, end to end.** A `SequentialAgent` phase
lifecycle with a `ParallelAgent` fan-out inside Phase 1 — four specialists
querying Mimir, Loki, Tempo and dashboards concurrently, each pinned to its own
tool subset. **The agent sees 12 of the 73 MCP tools**, no specialist more than
four; 73 tool declarations measurably degrades function-calling accuracy, and
pinning makes the search space a reviewable design decision.

### The vision spike that changed the architecture

Before building the SEE phase we ran vision against every fault, on every model
tier, in three prompt configurations. The result reshaped the design:

| fault | vision verdict | who decides |
| --- | --- | --- |
| `black_source` | **detected, 100%, every model and every variant** | vision |
| `ladder_mismatch` | **not detected by any configuration** | **code decides** |
| `ladder_collapse` / `segment_gap` / `edge_latency` | correctly read as healthy pixels | telemetry |

The `ladder_mismatch` result is the interesting one. `gemini-2.5-pro` scored 3/3
on mismatched rungs — and **0/3 on healthy ones**. It answers "upscaled" to
everything. That is not detection, it is a bias, and it would have fired a false
source alarm on a healthy stream every time it looked. Cross-rung pairing made
it worse: the models confabulate the comparison, producing the same fluent,
specific-sounding sentence regardless of which image is which.

So resolution is decided by a **round-trip PSNR measurement in code**, and
vision is never asked. The enum a model can emit has no "upscaled" option at
all, because offering it is what invites the confabulation.

### Detection is arithmetic, so it does not cost a model

Deciding whether a frame is black is arithmetic. Deciding *what kind* of wrong
it is, is worth a model. Splitting those is what makes continuous watching
affordable:

| stage | decides | cost |
| --- | --- | --- |
| **0** | is the picture wrong at all | **~1.3s, zero model calls** |
| **1** | what kind of wrong (Gemini vision) | ~5.4s, only behind a Stage 0 suspect |
| **2** | why, and what to do (five phases) | only on a confirmed finding |

**A healthy plant now makes zero vision calls per hour.**

### The trap we had to design around

Our black frames carry a burned-in timecode, so vision can confirm the encoder
is alive rather than stopped. That breaks both obvious black detectors. Measured
on a real black segment:

| | YLOW | YAVG | YHIGH | YMAX |
| --- | --- | --- | --- | --- |
| black | 16 | **17.14** | **16** | **236** |
| healthy | 41 | **126.02** | 210 | 255 |

`YMAX` is pinned to 236 by the clock — five units off healthy, on a fully black
picture. Any max-luma detector reads a blacked-out channel as fine. ffmpeg's
`blackdetect` *does* trip, but only by luck: its `pic_th=0.98` default happens to
suit this overlay's size, and a larger clock or a station logo would silently
stop it tripping — failing by returning nothing, which reads as health.

So mean luma and the 90th percentile are measured directly. Calibrated against a
corpus of 69 stills spanning six plant states and four ladder rungs:

```
black_source      YAVG 17.05 – 17.12     detected  12/12  = 100%
every other state YAVG 125.47 – 125.62   false positives 0/57 = 0%
overall 69/69
```

The threshold sits at 40, inside a **100-unit empty gap**. It was not tuned
until the table looked right — nothing lands between the clusters. The corpus is
committed, so this is reproducible rather than merely claimed.

---

## Deterministic where it matters

The word we kept coming back to. An LLM ranks hypotheses and writes for humans;
**evidence decides**, and the decisions that matter are made in code:

| decision | decided by |
| --- | --- |
| is the picture black | `ffmpeg signalstats` thresholds |
| does a rung carry its detail | round-trip PSNR ratio |
| which fault the evidence supports | fixed predicates over collected evidence |
| which remediation to propose | fixed fault→action table, via a **forced** tool call |
| **whether it may execute** | **a token only a human can issue** |
| which SLO verifies recovery | fault class, dispatched in code |
| viewer impact | arithmetic over a measured window |

**The gated tool sequence.** `propose_remediation` has no code path that touches
the plant — it returns one action from a fixed table and stops. Execution lives
in a separate function that refuses to run without an approval token the model
cannot produce. There are exactly five remediations plus an explicit no-op, and
no way to express a sequence — an agent proposing three steps is an agent asking
a human to approve something they cannot reason about at a glance. The model
cannot invent a sixth.

**The evidence checklists.** Each fault is a set of predicates over collected
evidence. A fault is *confirmed* only when every required check passes; anything
whose required checks are unavailable comes back **unconfirmable, never ruled
out** — missing evidence can never masquerade as elimination. Where the model's
ranking disagrees with the checklist, the disagreement is recorded rather than
resolved silently.

`ladder_collapse` and `segment_gap` both produce 404s, so **presence** of 404s
separates nothing. **Persistence** does, and the discriminator compares a recent
window against an earlier one — proven in both directions.

**And it cannot read the answer.** The plant knows which fault is injected. That
flag was reaching the same Mimir the agent queries with arbitrary PromQL, so we
dropped it at the collector: `encoder_chaos_active` and `edge_chaos_active` now
return **0 series** to the agent while remaining visible to the operator. Not a
promise that it doesn't cheat — a demonstration that it can't.

---

## Results, measured

| | |
| --- | --- |
| **Diagnostic accuracy** | **6/6** — all five faults plus a healthy control, through the complete five-phase agent |
| **Content screen** | **69/69**, 100% detection, 0% false positives |
| **Black frame on air → Stage 0 flag** | **12.4 / 12.5 / 12.4s** (n=3) |
| **→ classified fault** | **~18s** at a 10s sweep (see cadence note below) |
| **→ full closure with verified recovery** | **3.1 – 4.4 min**, measured |
| **Plant's own floor** | 6.4–8.1s — encoder finishing a 4s segment, origin, edge. No detector beats it. |

### About the baseline

We are deliberately **not** quoting an MTTR improvement, and the reason is the
whole point of the project.

There is no delivery-telemetry baseline to improve on for this fault class. Under
`black_source`, no threshold is crossed, no alert fires, and no webhook is
delivered — **ever**. The honest comparison is not "twelve minutes versus
seconds"; it is *never detected* versus *detected in seconds*. Inventing a
plausible-sounding baseline would trade a strong true claim for a weak false one.

We also do not claim a time-to-**repair**, because repair is human-gated by
design: nothing touches the plant without a person approving it, and how fast
that person clicks is not ours to take credit for. What is ours is everything up
to the gate, and the verification after it.

Times depend on sweep cadence, so we quote it:

| | at a 10s sweep | at a 30s sweep |
| --- | --- | --- |
| black on air → flagged | **12.4 / 12.5 / 12.4s** measured | ~22s typical |
| → classified fault | **~18s** | ~29s typical, 45s worst |

Full closure — detection through diagnosis, proposal, approval gate and
**verified** recovery — measured at **3.1 to 4.4 minutes**.

### Making it affordable

Profiling the agent through **its own traces in Tempo** showed every call after
Phase 1 carrying ~200k input tokens — 4.3M input against 17k output, a 250:1
ratio. The cause was not context: ADK's per-agent branch isolation only applies
downward from a `ParallelAgent`, so every phase under the sequential spine
inherited all four specialists' raw tool payloads. **Nothing downstream read
them** — each agent already receives its inputs through templated state.

Scoping contents per agent, verified per agent before applying:

| | input tokens | wall clock | cost |
| --- | --- | --- | --- |
| `ladder_collapse` | 4,329,077 → **919,973** (−79%) | 362s → **187s** | $1.34 → **$0.32** |
| `segment_gap` | 4,573,692 → **555,140** (−88%) | 569s → **244s** | $1.42 → **$0.20** |

Output tokens barely moved (17.2k → 14.8k). The agent does the same work and
says the same things; it was carrying freight, not context.

---

## Challenges

**The failure mode we kept hitting was our own.** Ten times, in the same shape:
*a value that is missing read as a value that is fine, and the check written to
catch it printed green.* A dead viewer fleet made four required health checks
pass on empty results and returned `no_fault_detected` at **high** confidence
during a live fault. A failed manifest fetch actively *ruled out* a fault. Our
own observability self-test emitted a unique marker and then searched for
something else, so it passed with the exporter pointed at a closed port.

We ran a full read-only audit against the repo — 30 verified findings, bucketed
by whether they would break the demo or merely embarrass us in judging, with a
gap list longer than the findings. It is committed. Fixing it meant teaching the
system to distinguish *"no data"* from *"no problem"* everywhere.

**A model that stops answering looks exactly like a fast one.** A single vision
call ran **1831 seconds** and then succeeded. The per-request timeout could not
catch it, because httpx resets its read timeout on every chunk received and has
no total-elapsed bound. The 900s run ceiling could not catch it either: ADK runs
sync tools on the event loop, so the timer was never scheduled. There is now a
hard wall-clock deadline in a worker thread — and the resulting failure is a
*distinct* verdict, because letting a Vertex stall read as "no fault in the
picture" would have been the same bug again.

---

## What's next

Move the viewer fleet into GCP so regional numbers mean something — we measured
that a hybrid topology (cloud edges, local viewers) puts every region above its
own TTFB threshold and pins the furthest one to the bottom ABR rung, which is
the WAN, not the plant. Then a stable public endpoint so the agent runs on the
webhook rather than a laptop.

---

## Built with

`google-adk` · `google-genai` · Gemini via Vertex AI · Grafana Cloud (Mimir,
Loki, Tempo) · self-hosted `grafana/mcp-grafana` · Grafana Alloy · ffmpeg ·
Docker · Python

Google Cloud AI tooling only, by contest rule. No LangChain, no LangGraph, no
non-Google agent framework, and no third-party models anywhere in the project.
