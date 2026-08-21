#!/usr/bin/env bash
# Cloud step 1: the L1 origin on GCE, and nothing else.
#
# Deliberately the smallest possible cloud increment. The edges, viewer fleet
# and agent all stay local and simply point at the public origin, so if
# `make verify` and `make diagnose-checks` still pass, most of the deployment
# uncertainty is retired for the price of one VM -- and rollback is pointing
# ORIGIN_URL back at localhost.
#
#   ./scripts/cloud_origin.sh up        create VM, firewall, and start L1
#   ./scripts/cloud_origin.sh status    IP, state, and whether HLS is serving
#   ./scripts/cloud_origin.sh egress    bytes out so far, and the cost of them
#   ./scripts/cloud_origin.sh stop      STOP the VM (keeps the disk, ~$0/hr compute)
#   ./scripts/cloud_origin.sh down      DELETE everything
#
# Cost control, per brief §5: `stop` is the equivalent of make plant-down. A
# stopped instance bills only for its disk. Always stop when not measuring.

set -euo pipefail

PROJECT="${DEADAIR_GCP_PROJECT:-rich-wavelet-476502-k4}"
ZONE="${DEADAIR_GCP_ZONE:-us-central1-a}"
VM="${DEADAIR_VM_NAME:-deadair-origin}"
# §5 specifies e2-medium. Step 1 ANSWERED that question and the answer was no:
# e2-medium reports isSharedCpu, and the 4-rung 1080p ladder falls off realtime
# on it -- 29.3 fps idle, 19.2 fps under 201 viewers, against a 30 fps source.
# See docs/cloud-deployment-risk.md finding 2.
#
# This default said e2-medium until cloud step 2, which is how a VM came back up
# shared-core and served the ladder at 25.1 fps. A finding that lives only in a
# doc is a finding that gets re-discovered; it belongs in the default.
MACHINE="${DEADAIR_MACHINE:-e2-standard-2}"
FW_RULE="deadair-allow-origin"

# Scoped to the operator's IP rather than 0.0.0.0/0. The edges and Alloy both
# run on that machine, so one CIDR covers everything that needs access, and the
# origin is not left open to the internet.
MYIP="$(curl -s https://ifconfig.me || curl -s https://api.ipify.org)"
CIDR="${DEADAIR_ALLOW_CIDR:-${MYIP}/32}"

g() { gcloud --project="$PROJECT" "$@"; }

vm_ip() {
  g compute instances describe "$VM" --zone="$ZONE" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true
}

cmd_up() {
  echo "project=$PROJECT zone=$ZONE machine=$MACHINE"
  echo "firewall will allow 8080,9103 from $CIDR only"

  if ! g compute firewall-rules describe "$FW_RULE" >/dev/null 2>&1; then
    g compute firewall-rules create "$FW_RULE" \
      --allow=tcp:8080,tcp:9103 \
      --source-ranges="$CIDR" \
      --target-tags=deadair-origin \
      --description="DEAD AIR: HLS origin + encoder metrics, operator IP only"
  else
    echo "firewall rule exists; updating source range to $CIDR"
    g compute firewall-rules update "$FW_RULE" --source-ranges="$CIDR"
  fi

  if ! g compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1; then
    g compute instances create "$VM" \
      --zone="$ZONE" \
      --machine-type="$MACHINE" \
      --image-family=debian-12 \
      --image-project=debian-cloud \
      --boot-disk-size=20GB \
      --boot-disk-type=pd-balanced \
      --tags=deadair-origin \
      --metadata=startup-script='#!/bin/bash
set -e
# docker.io only. docker-compose-plugin is NOT in Debian 12 default repos --
# it lives in Docker'"'"'s own apt repo -- and asking for it makes the whole
# apt-get fail under set -e, so nothing installs at all. The two containers
# share a volume rather than a network, so plain docker run is enough and the
# extra dependency buys nothing.
apt-get update -qq
apt-get install -y -qq docker.io
systemctl enable --now docker
mkdir -p /opt/deadair
'
  else
    echo "instance exists; starting it if stopped"
    g compute instances start "$VM" --zone="$ZONE" 2>/dev/null || true
  fi

  echo "waiting for SSH and docker..."
  for _ in $(seq 1 40); do
    if g compute ssh "$VM" --zone="$ZONE" --command="docker --version" \
         --quiet >/dev/null 2>&1; then break; fi
    sleep 15
  done

  echo "copying the L1 build context (encoder + origin only)"
  tar czf /tmp/deadair-l1.tgz plant/encoder plant/origin
  g compute scp /tmp/deadair-l1.tgz "$VM":/tmp/ --zone="$ZONE" --quiet
  g compute ssh "$VM" --zone="$ZONE" --quiet --command='
    set -e
    sudo mkdir -p /opt/deadair && cd /opt/deadair
    sudo tar xzf /tmp/deadair-l1.tgz
    sudo docker volume create hls-data >/dev/null
    sudo docker build -q -t deadair-encoder ./plant/encoder
    sudo docker build -q -t deadair-origin  ./plant/origin
    sudo docker rm -f deadair-encoder deadair-origin 2>/dev/null || true
    sudo docker run -d --name deadair-encoder --restart unless-stopped \
      -e ENCODER_PRESET=ultrafast -v hls-data:/data/hls \
      -p 0.0.0.0:9103:9103 deadair-encoder
    sudo docker run -d --name deadair-origin --restart unless-stopped \
      -v hls-data:/data/hls:ro -p 0.0.0.0:8080:8080 deadair-origin
    sudo docker ps --format "{{.Names}} {{.Status}}"
  '
  IP="$(vm_ip)"
  echo
  echo "origin up at http://$IP:8080"
  echo "  point the local plant at it:"
  echo "    export DEADAIR_CLOUD_ORIGIN=http://$IP:8080"
  echo "    ./scripts/cloud_origin.sh point-edges"
}

cmd_point_edges() {
  IP="$(vm_ip)"
  [ -z "$IP" ] && { echo "no VM"; exit 1; }
  echo "pointing local edges at http://$IP:8080"
  ORIGIN_URL="http://$IP:8080" docker compose up -d --force-recreate \
    edge-us-east1 edge-europe-west1 edge-asia-south1
  echo "done. rollback: ./scripts/cloud_origin.sh unpoint-edges"
}

cmd_unpoint_edges() {
  echo "pointing local edges back at the local origin"
  docker compose up -d --force-recreate edge-us-east1 edge-europe-west1 edge-asia-south1
}

cmd_status() {
  IP="$(vm_ip)"
  STATE="$(g compute instances describe "$VM" --zone="$ZONE" \
            --format='value(status)' 2>/dev/null || echo ABSENT)"
  echo "vm=$VM state=$STATE ip=${IP:-none} machine=$MACHINE"
  [ -z "$IP" ] && return 0
  echo -n "  master.m3u8: "
  curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 10 \
    "http://$IP:8080/hls/master.m3u8" || echo "unreachable"
  echo -n "  encoder_fps: "
  curl -s --max-time 10 "http://$IP:9103/metrics" 2>/dev/null \
    | awk '/^encoder_fps/{print $2}' || echo "unreachable"
}

# Egress is the number docs/cloud-deployment-risk.md flags as the unknown.
# Read it from Cloud Monitoring rather than estimated.
cmd_egress() {
  local mins="${1:-60}"
  g compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1 || {
    echo "no VM"; exit 1; }
  python3 - "$PROJECT" "$VM" "$mins" <<'PY'
import json, subprocess, sys, datetime
project, vm, mins = sys.argv[1], sys.argv[2], int(sys.argv[3])
end = datetime.datetime.now(datetime.timezone.utc)
start = end - datetime.timedelta(minutes=mins)
token = subprocess.check_output(
    ["gcloud", "auth", "print-access-token"], text=True).strip()
filt = (f'metric.type="compute.googleapis.com/instance/network/sent_bytes_count" '
        f'AND resource.labels.instance_id!="" ')
import urllib.parse, urllib.request
url = (f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries?"
       + urllib.parse.urlencode({
           "filter": filt,
           "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "aggregation.alignmentPeriod": "60s",
           "aggregation.perSeriesAligner": "ALIGN_RATE",
       }))
req = urllib.request.Request(url); req.add_header("Authorization", f"Bearer {token}")
try:
    data = json.load(urllib.request.urlopen(req, timeout=60))
except Exception as e:
    print("monitoring query failed:", e); raise SystemExit
series = data.get("timeSeries", [])
if not series:
    print(f"no egress samples in the last {mins}m "
          "(Cloud Monitoring lags a few minutes)"); raise SystemExit
tot_bps, n = 0.0, 0
for s in series:
    for p in s.get("points", []):
        v = p.get("value", {}).get("doubleValue") or p.get("value", {}).get("int64Value")
        if v: tot_bps += float(v); n += 1
if not n:
    print("no points"); raise SystemExit
mean_bps = tot_bps / n
gb_hr = mean_bps * 3600 / 1e9
print(f"  samples            : {n} over {mins}m")
print(f"  mean egress        : {mean_bps/1e6:.2f} MB/s  ({mean_bps*8/1e6:.1f} Mbps)")
print(f"  projected          : {gb_hr:.2f} GB/hour   {gb_hr*24:.1f} GB/day")
# GCP internet egress pricing varies by tier and destination; both bounds shown.
print(f"  cost @ $0.12/GB    : ${gb_hr*0.12:.2f}/hour   ${gb_hr*24*0.12:.2f}/day")
print(f"  cost @ $0.085/GB   : ${gb_hr*0.085:.2f}/hour  ${gb_hr*24*0.085:.2f}/day")
PY
}

cmd_stop() {
  g compute instances stop "$VM" --zone="$ZONE"
  echo "stopped. compute billing ends; the boot disk still bills (~\$0.0011/hr for 20GB)."
}

cmd_down() {
  g compute instances delete "$VM" --zone="$ZONE" --quiet 2>/dev/null || true
  g compute firewall-rules delete "$FW_RULE" --quiet 2>/dev/null || true
  echo "deleted VM and firewall rule."
}

case "${1:-}" in
  up) cmd_up ;;
  point-edges) cmd_point_edges ;;
  unpoint-edges) cmd_unpoint_edges ;;
  status) cmd_status ;;
  egress) cmd_egress "${2:-60}" ;;
  stop) cmd_stop ;;
  down) cmd_down ;;
  *) sed -n '2,30p' "$0"; exit 1 ;;
esac
