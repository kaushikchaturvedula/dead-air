#!/usr/bin/env bash
# Cloud step 2: the three "CDN" edges on Cloud Run, against the GCE origin.
#
#   ./scripts/cloud_edges.sh up        build once, deploy to all three regions
#   ./scripts/cloud_edges.sh status    URLs, readiness, and each edge's origin
#   ./scripts/cloud_edges.sh coldstart measure cold-start latency deliberately
#   ./scripts/cloud_edges.sh urls      print EDGES= line for the viewer fleet
#   ./scripts/cloud_edges.sh down      DELETE all three services
#
# TWO DELIBERATE CLOUD RUN SETTINGS, both about measuring honestly:
#
#   --max-instances=1
#     The edge cache is in-memory and per-instance. If Cloud Run autoscales to
#     N instances, the same segment is fetched from origin once PER INSTANCE and
#     the measured hit ratio drops for a reason that has nothing to do with the
#     coalescing fix we are here to verify. Pinning to one instance keeps the
#     cache singular, exactly as it is locally, so the number is comparable.
#
#   --min-instances=1
#     A cold start looks EXACTLY like edge_latency to the viewer fleet: slow
#     first byte, one region only. That is a self-inflicted fault that would
#     land mid-demo and be indistinguishable from the fault we inject on
#     purpose. Keeping one instance warm costs ~$0.0095/hr per service -- about
#     three cents an hour for all three, which is nothing against the egress.
#
# COST. Egress dominates and it is NOT the ~$1.40/hr that the origin->edge leg
# suggests. Cloud Run -> public internet is $0.12/GB, and the edge->viewer leg
# is ~18x the origin->edge leg. With the full 201-viewer fleet that is ~$26/hr.
# Run a REDUCED fleet (see `urls`) unless you specifically want the full number.

set -euo pipefail

PROJECT="${DEADAIR_GCP_PROJECT:-rich-wavelet-476502-k4}"
ZONE="${DEADAIR_GCP_ZONE:-us-central1-a}"
VM="${DEADAIR_VM:-deadair-origin}"
REGIONS=(us-east1 europe-west1 asia-south1)
IMAGE_TAG="${DEADAIR_EDGE_IMAGE:-}"
REPO_NAME="deadair"
SVC_PREFIX="deadair-edge"

g() { gcloud --project="$PROJECT" "$@"; }

origin_ip() {
  g compute instances describe "$VM" --zone="$ZONE" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null
}

svc_url() {
  g run services describe "${SVC_PREFIX}-$1" --region="$1" \
    --format='get(status.url)' 2>/dev/null
}

# THE FIREWALL PROBLEM, and why it is opened here rather than in cloud_origin.sh.
#
# Step 1 scoped deadair-allow-origin to the operator's single IP, which is right
# when the only client is the laptop. Cloud Run egresses from Google's dynamic
# ranges instead, so all three edges deployed cleanly, started cleanly ("proxying
# http://<ip>:8080 on :8080"), and returned 502 on every request -- the container
# was healthy and the network was not.
#
# A VPC connector plus Cloud NAT would give a single stable egress IP to allow,
# which is the right answer for a permanent deployment. It also costs ~$0.044/hr
# for the NAT gateway plus data processing, which is real money against an
# egress-dominated budget, for a 45-minute measurement.
#
# So the rule is widened to 0.0.0.0/0 for the life of the edges and narrowed
# again by `down`. It is paired with the edge lifecycle deliberately: an origin
# left world-open is a standing egress liability on a metered account, and the
# way that happens is a manual step someone forgets.
OPEN_RULE="deadair-allow-origin-cloudrun"

open_origin_firewall() {
  if g compute firewall-rules describe "$OPEN_RULE" >/dev/null 2>&1; then
    echo "origin firewall already open for Cloud Run"
    return
  fi
  echo "opening origin :8080 to Cloud Run egress (narrowed again by 'down')"
  g compute firewall-rules create "$OPEN_RULE" \
    --allow=tcp:8080,tcp:9103 --source-ranges=0.0.0.0/0 \
    --description="TEMPORARY: Cloud Run edges reach the origin. Delete with cloud_edges.sh down." \
    --quiet
}

close_origin_firewall() {
  g compute firewall-rules delete "$OPEN_RULE" --quiet 2>/dev/null \
    && echo "origin firewall narrowed back to the operator IP" \
    || true
}

cmd_up() {
  local ip; ip="$(origin_ip)"
  if [[ -z "$ip" ]]; then
    echo "origin VM has no IP -- is it running? ./scripts/cloud_origin.sh status" >&2
    exit 1
  fi
  local origin="http://${ip}:8080"
  echo "origin: $origin"

  # Verify the origin actually serves before deploying three services against
  # it. Deploying first and discovering the origin is down turns one clear
  # failure into three confusing ones.
  if ! curl -sf --max-time 15 "${origin}/hls/master.m3u8" >/dev/null; then
    echo "origin is not serving HLS at ${origin} -- fix that first" >&2
    exit 1
  fi
  echo "origin is serving HLS"

  open_origin_firewall

  g services enable run.googleapis.com artifactregistry.googleapis.com \
    cloudbuild.googleapis.com --quiet

  g artifacts repositories describe "$REPO_NAME" --location=us-central1 \
    >/dev/null 2>&1 || \
    g artifacts repositories create "$REPO_NAME" --repository-format=docker \
      --location=us-central1 --description="DEAD AIR images" --quiet

  local image="us-central1-docker.pkg.dev/${PROJECT}/${REPO_NAME}/edge:latest"
  if [[ -n "$IMAGE_TAG" ]]; then
    image="$IMAGE_TAG"
    echo "reusing image $image"
  else
    # Build ONCE and deploy the same digest to all three regions. Three separate
    # builds would be three chances to deploy subtly different edges and then
    # attribute the difference to geography.
    # --config, not --tag: `builds submit --tag` requires the Dockerfile at the
    # context root, but the context must be ./plant so shared/deadair_trace.py
    # is copyable while the Dockerfile lives at edge/Dockerfile.
    echo "building edge image (one build, three regions)..."
    ( cd "$(dirname "$0")/../plant" && \
      g builds submit --config edge/cloudbuild.yaml \
        --substitutions="_IMAGE=${image}" . --quiet )
  fi

  for r in "${REGIONS[@]}"; do
    echo "deploying ${SVC_PREFIX}-${r} to ${r}..."
    g run deploy "${SVC_PREFIX}-${r}" \
      --image="$image" --region="$r" --platform=managed \
      --allow-unauthenticated \
      --min-instances=1 --max-instances=1 \
      --cpu=1 --memory=512Mi --concurrency=80 --timeout=60 \
      --set-env-vars="EDGE_REGION=${r},ORIGIN_URL=${origin},OTEL_SERVICE_NAME=deadair-edge-${r},TRACE_SAMPLE_RATIO=0.02" \
      --quiet
  done
  cmd_status
}

cmd_status() {
  printf "  %-14s %-46s %-8s %s\n" region url ready origin
  for r in "${REGIONS[@]}"; do
    local u; u="$(svc_url "$r")"
    if [[ -z "$u" ]]; then printf "  %-14s %s\n" "$r" "-- not deployed --"; continue; fi
    local code; code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "${u}/hls/master.m3u8" || echo ERR)"
    printf "  %-14s %-46s %-8s %s\n" "$r" "$u" "$code" "$(origin_ip)"
  done
}

# A cold start is the one Cloud Run behaviour that would masquerade as our own
# injected fault, so measure it on purpose rather than hoping min-instances
# handled it. Compares first-byte latency on a warm service against the steady
# state; a large gap means min-instances is not holding.
cmd_coldstart() {
  echo "cold-start / TTFB check (edge_latency looks identical to this)"
  printf "  %-14s %10s %10s %10s %10s\n" region first p50 p95 verdict
  for r in "${REGIONS[@]}"; do
    local u; u="$(svc_url "$r")"
    [[ -z "$u" ]] && continue
    local first; first="$(curl -s -o /dev/null -w '%{time_starttransfer}' --max-time 30 "${u}/hls/master.m3u8")"
    local times=()
    for _ in $(seq 12); do
      times+=("$(curl -s -o /dev/null -w '%{time_starttransfer}' --max-time 30 "${u}/hls/master.m3u8")")
      sleep 0.4
    done
    python3 - "$r" "$first" "${times[@]}" <<'PY'
import sys, statistics
r, first, *t = sys.argv[1:]
t = sorted(float(x) for x in t)
p50, p95 = statistics.median(t), t[int(0.95*len(t))-1]
first = float(first)
# A cold start shows as a first request many times the steady state.
verdict = "COLD START" if first > max(4*p50, p50+1.0) else "warm"
print(f"  {r:<14}{first:>9.3f}s{p50:>9.3f}s{p95:>9.3f}s{verdict:>11}")
PY
  done
}

cmd_urls() {
  local parts=()
  for r in "${REGIONS[@]}"; do
    local u; u="$(svc_url "$r")"
    [[ -n "$u" ]] && parts+=("${r}=${u}")
  done
  local joined; joined="$(IFS=,; echo "${parts[*]}")"
  echo "EDGES=\"${joined}\""
  echo
  echo "# Point the viewer fleet at the cloud edges with a REDUCED session count."
  echo "# 201 sessions costs ~\$26/hr in Cloud Run internet egress; 20 costs ~\$2.7/hr."
  echo "EDGES=\"${joined}\" VIEWERS_PER_REGION=7 docker compose up -d viewers"
}

cmd_down() {
  close_origin_firewall
  for r in "${REGIONS[@]}"; do
    echo "deleting ${SVC_PREFIX}-${r}..."
    g run services delete "${SVC_PREFIX}-${r}" --region="$r" --quiet 2>/dev/null || true
  done
  echo "edges deleted. The origin VM is separate: ./scripts/cloud_origin.sh stop"
}

case "${1:-status}" in
  up) cmd_up ;;
  status) cmd_status ;;
  coldstart) cmd_coldstart ;;
  urls) cmd_urls ;;
  down) cmd_down ;;
  *) sed -n '2,28p' "$0"; exit 1 ;;
esac
