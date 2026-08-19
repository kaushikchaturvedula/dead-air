#!/usr/bin/env python3
"""Provision the DEAD AIR dashboard, contact point and alert rule in Grafana Cloud.

Idempotent: safe to re-run. Everything it creates is namespaced under the
"DEAD AIR" folder / the deadair-* UIDs, so it never touches unrelated objects.

Usage:
    python3 scripts/provision_grafana.py                  # dashboard + alert
    python3 scripts/provision_grafana.py --webhook-url URL # also (re)point the
                                                          # contact point

Reads GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN from
agents/grafana_probe/.env.

Note on why this uses the HTTP API rather than the MCP server: the Grafana MCP
server's alerting tools can create alert *rules*, but its routing tools
(contact points, notification policies) are read-only. Doing the whole
provisioning flow through one API keeps it coherent.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO, "agents", "grafana_probe", ".env")

FOLDER_UID = "deadair"
FOLDER_TITLE = "DEAD AIR"
DASHBOARD_UID = "deadair-plant"
RULE_UID = "deadair-synthetic-high"
RULE_GROUP = "deadair-plant"
CONTACT_POINT = "deadair-local-webhook"
DATASOURCE_UID = "grafanacloud-prom"
LOKI_DATASOURCE_UID = "grafanacloud-logs"

# The alert threshold. The emitter starts at 10; `make set VALUE=95` crosses it.
THRESHOLD = 50


def load_env(path):
    env = {}
    if not os.path.exists(path):
        sys.exit(f"error: {path} not found -- copy .env.example to .env first")
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class Grafana:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method, path, body=None, ok=(200, 201, 202, 204)):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                if resp.status not in ok:
                    raise RuntimeError(f"{method} {path} -> {resp.status}: {raw[:400]}")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            raise RuntimeError(f"{method} {path} -> {e.code}: {raw[:400]}") from None


def ensure_folder(gf):
    try:
        gf.request("GET", f"/api/folders/{FOLDER_UID}")
        print(f"  folder {FOLDER_UID!r} exists")
    except RuntimeError:
        gf.request("POST", "/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE})
        print(f"  folder {FOLDER_UID!r} created")


def dashboard_model():
    def target(expr, legend):
        return {
            "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
            "editorMode": "code",
            "expr": expr,
            "legendFormat": legend,
            "range": True,
            "refId": "A",
        }

    return {
        "uid": DASHBOARD_UID,
        "title": "DEAD AIR -- Plant",
        "tags": ["dead-air", "plant"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "title": "Synthetic gauge (alert threshold "
                         f"{THRESHOLD})",
                "description": (
                    "Step 1 proof-of-pipe signal. Drive it with "
                    "`make set VALUE=95` and this panel plus the alert "
                    "should go red within ~1 minute."
                ),
                "gridPos": {"h": 9, "w": 16, "x": 0, "y": 0},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("deadair_synthetic_gauge", "value")],
                "fieldConfig": {
                    "defaults": {
                        "custom": {"lineWidth": 2, "fillOpacity": 8},
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "green", "value": None},
                                {"color": "red", "value": THRESHOLD},
                            ],
                        },
                    },
                    "overrides": [],
                },
                "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
            },
            {
                "id": 2,
                "type": "stat",
                "title": "Current value",
                "gridPos": {"h": 9, "w": 8, "x": 16, "y": 0},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("deadair_synthetic_gauge", "value")],
                "fieldConfig": {
                    "defaults": {
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "green", "value": None},
                                {"color": "red", "value": THRESHOLD},
                            ],
                        },
                        "color": {"mode": "thresholds"},
                    },
                    "overrides": [],
                },
                "options": {
                    "colorMode": "background",
                    "graphMode": "area",
                    "reduceOptions": {"calcs": ["lastNotNull"]},
                },
            },
            {
                "id": 3,
                "type": "timeseries",
                "title": "Scrape liveness (deadair_emitter_up)",
                "description": "Proves the Alloy -> Mimir path is delivering, "
                               "independent of the gauge value.",
                "gridPos": {"h": 7, "w": 24, "x": 0, "y": 9},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("deadair_emitter_up", "{{component}}")],
                "fieldConfig": {"defaults": {"custom": {"lineWidth": 2}}, "overrides": []},
            },

            # --- L1: source + encoder ---------------------------------------
            {
                "id": 10,
                "type": "row",
                "title": "L1 — source + encoder",
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": 16},
                "collapsed": False,
                "panels": [],
            },
            {
                "id": 11,
                "type": "timeseries",
                "title": "encoder_fps",
                "description": "Realtime encode rate. Sustained drift below the "
                               "source frame rate means the encoder cannot keep up.",
                "gridPos": {"h": 8, "w": 8, "x": 0, "y": 17},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("encoder_fps", "fps")],
                "fieldConfig": {
                    "defaults": {"custom": {"lineWidth": 2, "fillOpacity": 8},
                                 "min": 0, "unit": "none"},
                    "overrides": [],
                },
            },
            {
                "id": 12,
                "type": "timeseries",
                "title": "packager_segment_lag by rendition",
                "description": (
                    "Seconds since each ladder rung last produced a segment. "
                    "Healthy: sawtooths between 0 and the 4s segment duration. "
                    "A single rung climbing while others stay flat is the "
                    "ladder_collapse signature."
                ),
                "gridPos": {"h": 8, "w": 16, "x": 8, "y": 17},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("packager_segment_lag", "{{rendition}}")],
                "fieldConfig": {
                    "defaults": {
                        "custom": {"lineWidth": 2},
                        "unit": "s",
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "green", "value": None},
                                {"color": "red", "value": 15},
                            ],
                        },
                    },
                    "overrides": [],
                },
            },
            {
                "id": 13,
                "type": "stat",
                "title": "encoder_up",
                "gridPos": {"h": 5, "w": 6, "x": 0, "y": 25},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("encoder_up", "up")],
                "fieldConfig": {
                    "defaults": {
                        "mappings": [{
                            "type": "value",
                            "options": {"0": {"text": "DOWN", "color": "red"},
                                        "1": {"text": "UP", "color": "green"}},
                        }],
                        "color": {"mode": "thresholds"},
                        "thresholds": {"mode": "absolute", "steps": [
                            {"color": "red", "value": None},
                            {"color": "green", "value": 1}]},
                    },
                    "overrides": [],
                },
                "options": {"colorMode": "background",
                            "reduceOptions": {"calcs": ["lastNotNull"]}},
            },
            {
                "id": 14,
                "type": "stat",
                "title": "dropped_frames",
                "description": "Cumulative. Any sustained increase means the "
                               "encoder is shedding frames.",
                "gridPos": {"h": 5, "w": 6, "x": 6, "y": 25},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("dropped_frames", "dropped")],
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "thresholds": {"mode": "absolute", "steps": [
                            {"color": "green", "value": None},
                            {"color": "orange", "value": 1}]},
                    },
                    "overrides": [],
                },
                "options": {"colorMode": "background",
                            "reduceOptions": {"calcs": ["lastNotNull"]}},
            },
            {
                "id": 15,
                "type": "logs",
                "title": "Origin access log (Loki)",
                "description": (
                    "Per-request detail lives here, not in Mimir: the "
                    "cardinality guard strips per-segment and per-session "
                    "labels from metrics, and this is where they belong."
                ),
                "gridPos": {"h": 10, "w": 12, "x": 12, "y": 25},
                "datasource": {"type": "loki", "uid": LOKI_DATASOURCE_UID},
                "targets": [{
                    "datasource": {"type": "loki", "uid": LOKI_DATASOURCE_UID},
                    "expr": '{job="deadair-origin"}',
                    "queryType": "range",
                    "refId": "A",
                }],
                "options": {"showTime": True, "sortOrder": "Descending",
                            "wrapLogMessage": True},
            },
        ],
    }


def upsert_dashboard(gf):
    res = gf.request(
        "POST",
        "/api/dashboards/db",
        {
            "dashboard": dashboard_model(),
            "folderUid": FOLDER_UID,
            "overwrite": True,
            "message": "DEAD AIR plant dashboard (provisioned)",
        },
    )
    print(f"  dashboard: {res.get('url', DASHBOARD_UID)}")
    return res


def upsert_contact_point(gf, webhook_url):
    existing = gf.request("GET", "/api/v1/provisioning/contact-points")
    match = next((c for c in existing if c.get("name") == CONTACT_POINT), None)
    body = {
        "name": CONTACT_POINT,
        "type": "webhook",
        "settings": {"url": webhook_url, "httpMethod": "POST"},
        "disableResolveMessage": False,
    }
    headers_note = ""
    if match:
        body["uid"] = match["uid"]
        gf.request("PUT", f"/api/v1/provisioning/contact-points/{match['uid']}", body)
        print(f"  contact point {CONTACT_POINT!r} updated -> {webhook_url}{headers_note}")
    else:
        gf.request("POST", "/api/v1/provisioning/contact-points", body)
        print(f"  contact point {CONTACT_POINT!r} created -> {webhook_url}{headers_note}")


def contact_point_exists(gf):
    try:
        existing = gf.request("GET", "/api/v1/provisioning/contact-points")
    except RuntimeError:
        return False
    return any(c.get("name") == CONTACT_POINT for c in existing)


def upsert_alert_rule(gf, route_to_webhook):
    rule = {
        "uid": RULE_UID,
        "title": "DEAD AIR / synthetic gauge high",
        "condition": "C",
        "folderUID": FOLDER_UID,
        "ruleGroup": RULE_GROUP,
        "noDataState": "NoData",
        "execErrState": "Error",
        # Short `for` so the Step 1 loop is fast to observe.
        "for": "1m",
        "annotations": {
            "summary": (
                "Synthetic gauge is above "
                f"{THRESHOLD} -- the DEAD AIR telemetry pipe is working "
                "end to end."
            ),
            "__dashboardUid__": DASHBOARD_UID,
            "__panelId__": "1",
        },
        "labels": {"project": "dead-air", "layer": "L0-synthetic"},
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": DATASOURCE_UID,
                "model": {
                    "refId": "A",
                    "expr": "deadair_synthetic_gauge",
                    "instant": True,
                    "editorMode": "code",
                },
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                    "datasource": {"type": "__expr__", "uid": "__expr__"},
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [THRESHOLD]},
                            "operator": {"type": "and"},
                            "query": {"params": ["B"]},
                            "reducer": {"type": "last", "params": []},
                            "type": "query",
                        }
                    ],
                    "datasource": {"type": "__expr__", "uid": "__expr__"},
                },
            },
        ],
    }

    # Grafana rejects a rule naming a receiver that does not exist yet, so only
    # attach routing once the contact point is really there. Without it the rule
    # still fires and still shows red -- it just follows the default policy.
    if route_to_webhook:
        rule["notification_settings"] = {"receiver": CONTACT_POINT}

    try:
        gf.request("GET", f"/api/v1/provisioning/alert-rules/{RULE_UID}")
        gf.request("PUT", f"/api/v1/provisioning/alert-rules/{RULE_UID}", rule)
        print(f"  alert rule {RULE_UID!r} updated (threshold > {THRESHOLD}, for 1m)")
    except RuntimeError:
        gf.request("POST", "/api/v1/provisioning/alert-rules", rule)
        print(f"  alert rule {RULE_UID!r} created (threshold > {THRESHOLD}, for 1m)")

    # Tighten evaluation interval so Step 1 is observable in ~1 minute.
    try:
        gf.request(
            "PUT",
            f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{RULE_GROUP}",
            {"title": RULE_GROUP, "folderUid": FOLDER_UID, "interval": 30},
        )
        print("  rule group evaluation interval: 30s")
    except RuntimeError as e:
        print(f"  note: could not set rule-group interval ({e})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook-url", help="public URL of the local webhook receiver")
    args = ap.parse_args()

    env = load_env(ENV_PATH)
    base = env.get("GRAFANA_URL")
    token = env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not base or not token:
        sys.exit("error: GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN must be set in .env")

    gf = Grafana(base, token)
    print(f"provisioning {base} ...")
    ensure_folder(gf)
    upsert_dashboard(gf)

    # Contact point must exist before the rule can route to it.
    webhook_url = args.webhook_url or env.get("DEADAIR_WEBHOOK_PUBLIC_URL")
    if webhook_url:
        upsert_contact_point(gf, webhook_url)
        route = True
    else:
        route = contact_point_exists(gf)
        if route:
            print(f"  contact point {CONTACT_POINT!r} already exists -- reusing")
        else:
            print("  no webhook URL and no existing contact point --")
            print("    rule will use the default notification policy.")
            print("    run `make tunnel`, then `make provision` to wire the webhook.")

    upsert_alert_rule(gf, route)
    print(f"\ndone. dashboard: {base}/d/{DASHBOARD_UID}")


if __name__ == "__main__":
    main()
