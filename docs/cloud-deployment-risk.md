# Cloud deployment — the last untouched unknown

**Status: not started. Nothing has been deployed and no credits have been spent.**

Everything built so far runs locally, which was the right call — a fully working
local plant by Friday beats a half-deployed cloud one. But the Aug 29–Sep 4
criterion is *"a stranger can click the URL cold and see it work"*, and that
criterion depends entirely on work that has not begun.

This document exists so the risk is visible now rather than discovered on
Sep 3.

## What has to happen, and in what order

The ordering is forced, not preferred. Each step blocks the next.

| # | Step | Blocks | Why the order is fixed |
| --- | --- | --- | --- |
| 1 | **GCE origin** (e2-medium, encoder + nginx) | everything | An edge in europe-west1 must reach the origin over the public internet. Local containers have no public address. |
| 2 | **3× Cloud Run edges** (us-east1, europe-west1, asia-south1) | viewer fleet, regional demo | Needs step 1's public origin URL |
| 3 | **Viewer fleet** somewhere reachable | rebuffer_ratio, the alert | Must reach the edges |
| 4 | **Agent hosting** (Cloud Run) | the webhook trigger | Grafana Cloud must POST to a public URL. ngrok is a laptop, not a demo. |
| 5 | **Stable public URL** | the Sep 4 criterion | Everything above |

## What is already de-risked

Real work has gone into making this deployment boring rather than novel:

- **The edge image is Cloud Run-ready today.** Single stdlib process, listens on
  `$PORT`, no kernel capabilities. This is why `edge_latency` was built as
  application-level shaping rather than `tc netem` — netem needs `NET_ADMIN`,
  which Cloud Run does not grant, so a netem-based fault would have had to be
  rewritten at exactly this step.
- **Region is configuration, not code.** `EDGE_ENDPOINTS` in
  `video_tools.py` and `EDGE_PORT_*` in the Makefile are the only places that
  know a hostname. Cloud Run turns those into service URLs; nothing else moves.
- **The agent reaches Grafana through the hosted API**, not through anything
  local, so Phases 1 and 3 work unchanged from Cloud Run.
- **Phase 3's evidence collection uses the Grafana datasource proxy**, not the
  local exporters, for the same reason.
- **The webhook bridge is already the shape of a Cloud Run service.** The
  receiver records deliveries; `run_agent.py` consumes them. In production the
  contact point POSTs straight to the agent service, and the agent above that
  boundary does not change.

## What is genuinely unknown

Honest list — these are the things that could eat a day each:

1. **Origin egress cost and bandwidth.** 201 modeled viewers pulling a 4-rung
   ladder is real traffic. Locally that is free; from GCE it is egress, and the
   viewer fleet is the biggest consumer. Either the fleet runs *inside* GCP (and
   the edges cache hard), or the fleet shrinks for the cloud demo. **This needs
   a number before deploy, not after.** The $80 budget alert is the backstop,
   not the plan.
2. **Cloud Run cold starts** against a 4s segment deadline. `min-instances=0`
   is the cost-control default from §5, but a cold start on a segment fetch
   looks exactly like `edge_latency` to the viewer fleet — a self-inflicted
   fault that could pollute the demo.
3. **Agent run duration vs Cloud Run request timeout.** A full three-phase run
   takes minutes locally. Cloud Run's request timeout caps at 60 minutes, which
   is fine, but the webhook POST should not block for the whole run — the
   trigger needs to return immediately and run the agent asynchronously.
4. **The confidence monitor needs a scheduler.** Cloud Scheduler hitting the
   agent service is the obvious answer, and it is another resource to create,
   authenticate, and pay for.
5. **Secrets.** Everything currently reads a gitignored `.env`. Cloud Run wants
   Secret Manager, which is a small but non-zero amount of new plumbing.
6. **Public URL and the cold-click demo.** A stranger clicking a link needs the
   player, the dashboard, and ideally the agent's output all reachable without a
   login. Grafana dashboards can be made public; the player is static; the
   agent's output needs somewhere to live.

## Recommended sequencing

Do not deploy everything at once. The whole point of the local plant is that
each cloud step can be validated against a known-good baseline.

1. **GCE origin only.** Point the *local* edges at the public origin. If the
   plant still passes `make verify` and `make diagnose-checks`, step 1 is done
   and the blast radius of any failure is one VM.
2. **One Cloud Run edge**, in us-east1. Keep the other two local. A per-region
   differential between a cloud edge and a local edge is still a differential,
   and it isolates Cloud Run behaviour from everything else.
3. **The other two edges**, once the first is boring.
4. **Agent on Cloud Run**, triggered by the real webhook. This is where ngrok
   finally goes away.
5. **Cloud Scheduler** for the confidence monitor.

Each step has a rollback: point the config back at localhost.

## Cost control, restated

§5 requires `make plant-up` / `make plant-down` to take idle burn to near zero
(GCE stop, Cloud Run `min-instances=0`), and an $80 budget alert **before the
first `gcloud run deploy`**. Neither exists yet, because nothing has been
deployed. Both are prerequisites for step 1, not follow-ups.

## The honest risk statement

The plant, all five faults, all three signals, and Phases 1–3 of the agent work
today, locally and reproducibly. **None of that has ever run in the cloud.**
Every estimate above is an educated guess by someone who has not yet watched a
Cloud Run cold start miss a segment deadline.

The Aug 29–Sep 4 window is currently carrying: cloud deployment, Phases 4–5,
IRM incidents, dashboard annotations, AI observability, the public URL, *and*
the video and writeup in the final two days. Deployment is the only item on that
list with genuinely unknown unknowns, which argues for starting it early and in
the smallest possible increments — step 1 alone, against the local plant, would
retire most of the uncertainty for the price of one VM.
