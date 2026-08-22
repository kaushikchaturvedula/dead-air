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
RULE_UID = "deadair-rebuffer-ratio"
RULE_GROUP = "deadair-plant"
# The Step 1 placeholder, deleted on provision now that the real signal exists.
LEGACY_RULE_UIDS = ["deadair-synthetic-high"]
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


# Panel placement, applied after the panels are built.
#
# CONTENT IS FIRST, ABOVE THE PLANT. Everything below it -- encoder fps, edge
# TTFB, rebuffer ratio -- describes DELIVERY, and all of it stays green while
# the channel is showing black, because none of it looks at the picture. Putting
# the content row at the top means the contradiction is visible without
# scrolling: one row red, every row under it green, in a single frame.
#
# Then the plant in its own order -- L1 encoder, L2 edges, L3 viewers, logs --
# with the synthetic pipe canary last and collapsed, because it is
# infrastructure for diagnosing the observability stack rather than a signal
# about the stream.
LAYOUT = {
    50: (0, 0, 24, 1),                                    # row: content truth
    51: (0, 1, 12, 9),   52: (12, 1, 6, 9),  53: (18, 1, 6, 9),
    10: (0, 10, 24, 1),                                   # row: L1
    11: (0, 11, 8, 8),   12: (8, 11, 16, 8),
    13: (0, 19, 6, 5),   14: (6, 19, 6, 5),
    20: (0, 24, 24, 1),                                   # row: L2
    21: (0, 25, 16, 9),  22: (16, 25, 8, 9),
    23: (0, 34, 12, 8),  24: (12, 34, 12, 8),
    30: (0, 42, 24, 1),                                   # row: L3
    31: (0, 43, 16, 9),  32: (16, 43, 8, 9),
    33: (0, 52, 12, 8),  34: (12, 52, 12, 8),
    15: (0, 60, 24, 10),                                  # origin access log
    40: (0, 70, 24, 1),                                   # row: L0 (collapsed)
    1: (0, 71, 16, 8),   2: (16, 71, 8, 8),  3: (0, 79, 24, 7),
}
CANARY_PANEL_IDS = [1, 2, 3]

# Stage 0's black threshold, kept in step with agents/dead_air/content_screen.py
# (SCREEN_BLACK_YAVG_MAX). Measured separation is ~17 black vs ~125 healthy, so
# 40 sits in a 100-unit empty gap -- see docs/content-screen.md.
CONTENT_LUMA_THRESHOLD = 40

# How old the newest Stage 0 sample may be before the content panels stop
# claiming to know anything.
#
# WHY THIS EXISTS. Prometheus keeps serving a series' last value for ~5 minutes
# after samples stop. The content metrics are PUSHED by the agent's Stage 0
# screen, so when the sweep is not running there are no samples -- and for those
# five minutes `max(deadair_content_suspect)` keeps returning the last value.
# Measured: 72 seconds after the final sample, with nothing watching at all, the
# naive query still returned 0 and the panel still rendered a green "PICTURE
# OK". The one panel carrying the entire thesis would sit there reassuring the
# operator over a black stream, for the same reason the rest of the dashboard
# does. A dashboard that lies toward "fine" is the exact failure this project
# exists to attack, so it must not be committed by our own panel.
#
# 90s is three missed ticks at the demo's `INTERVAL=30`. It is deliberately
# coupled to the sweep interval: run the sweep slower than ~45s and the panels
# will correctly, and permanently, report NOT WATCHING.
CONTENT_STALE_AFTER_SECONDS = 90

# Absence must be representable, so it gets its own value rather than being
# folded into 0 (healthy) or dropped (Grafana renders empty as "No data", which
# a stat panel still paints with the BASE threshold colour -- green).
CONTENT_UNKNOWN = -1


def _fresh(expr, metric):
    """Wrap an instant expression so a stale series reports UNKNOWN, not health.

    `unless` drops the reading when the newest sample is older than the
    staleness budget; the `or vector()` then supplies the sentinel, which also
    covers the case where the series has aged out of Prometheus entirely and
    the left-hand side is empty.
    """
    return (f"({expr} unless on() "
            f"(time() - max(timestamp({metric})) > {CONTENT_STALE_AFTER_SECONDS}))"
            f" or on() vector({CONTENT_UNKNOWN})")


def apply_layout(panels):
    """Position panels and fold the canary into a collapsed row.

    Grafana keeps a collapsed row's children inside the row's own `panels`
    array rather than at the top level, so the nesting has to happen here.
    """
    for p in panels:
        if p["id"] in LAYOUT:
            x, y, w, h = LAYOUT[p["id"]]
            p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}

    canary = [p for p in panels if p["id"] in CANARY_PANEL_IDS]
    rest = [p for p in panels if p["id"] not in CANARY_PANEL_IDS]
    x, y, w, h = LAYOUT[40]
    rest.append({
        "id": 40,
        "type": "row",
        "title": "L0 — pipe health (synthetic canary, not plant telemetry)",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "collapsed": True,
        "panels": canary,
    })
    rest.sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))
    return rest


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

    panels = [
            # --- CONTENT: the only row that looks at the picture ------------
            {
                "id": 50,
                "type": "row",
                "title": "CONTENT — what the picture actually shows "
                         "(agent Stage 0, no model)",
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0},
                "collapsed": False,
                "panels": [],
            },
            {
                "id": 51,
                "type": "timeseries",
                "title": "Content luma — the signal no delivery metric carries",
                "description": (
                    "Mean luma (ffmpeg signalstats YAVG) of the newest segment "
                    "at the edge, measured by the agent's Stage 0 screen. This "
                    "is arithmetic, not a model: ~1.3s per check, no LLM call.\n\n"
                    "Healthy reads ~125. A black source reads ~17. The "
                    "threshold at 40 sits in a 100-unit empty gap — 69/69 "
                    "fixtures classify correctly with zero false positives.\n\n"
                    "WHY IT MATTERS: when this line falls off a cliff, every "
                    "panel below it stays green. The encoder is still encoding, "
                    "segments still arrive on time, viewers still are not "
                    "rebuffering. Delivery is perfect and the channel is dead. "
                    "That contradiction is what DEAD AIR exists to catch, and "
                    "you are looking at both halves of it at once."
                ),
                "gridPos": {"h": 9, "w": 12, "x": 0, "y": 1},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                # Gated the same way as the verdict stat. Without this the line
                # simply continues flat at its last value for Prometheus's ~5
                # minute staleness window after the sweep stops, which reads as
                # "still 125, still fine" rather than "nobody is measuring".
                # Gated, it breaks into a visible gap instead (spanNulls is
                # false below, so the gap is drawn as a gap).
                "targets": [target(
                    "deadair_content_luma_avg unless on() "
                    "(time() - max(timestamp(deadair_content_luma_avg)) > "
                    f"{CONTENT_STALE_AFTER_SECONDS})",
                    "{{region}} / {{rendition}}")],
                "fieldConfig": {
                    "defaults": {
                        "custom": {"lineWidth": 3, "fillOpacity": 10,
                                   "spanNulls": False},
                        "min": 0, "max": 255, "unit": "none",
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                # Inverted against every other panel here:
                                # LOW is the failure, so red is the floor.
                                {"color": "red", "value": None},
                                {"color": "green", "value": CONTENT_LUMA_THRESHOLD},
                            ],
                        },
                    },
                    "overrides": [],
                },
                "options": {
                    "legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi"},
                },
            },
            {
                "id": 52,
                "type": "stat",
                "title": "What the picture shows",
                "description": (
                    "Stage 0's verdict on the newest segment. Goes red on a "
                    "black or frozen source — the two failures that are "
                    "invisible to every delivery metric in this dashboard."
                ),
                "gridPos": {"h": 9, "w": 6, "x": 12, "y": 1},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target(
                    _fresh("max(deadair_content_suspect)",
                           "deadair_content_suspect"), "content")],
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "mappings": [{
                            "type": "value",
                            "options": {
                                "-1": {"text": "NOT WATCHING", "index": 0},
                                "0": {"text": "PICTURE OK", "index": 1},
                                "1": {"text": "DEAD AIR", "index": 2},
                            },
                        }],
                        # The BASE step is the one that matters. Grafana paints
                        # anything below the first threshold -- including the
                        # UNKNOWN sentinel and a "No data" cell -- with this
                        # colour, so it must never be green. Green starts at 0
                        # and is reachable only by a fresh, measured zero.
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "orange", "value": None},
                                {"color": "green", "value": 0},
                                {"color": "red", "value": 1},
                            ],
                        },
                        "noValue": "NOT WATCHING",
                    },
                    "overrides": [],
                },
                "options": {
                    "colorMode": "background",
                    "graphMode": "none",
                    "textMode": "value",
                    "reduceOptions": {"calcs": ["lastNotNull"]},
                },
            },
            {
                "id": 53,
                "type": "stat",
                "title": "What delivery reports, same moment",
                "description": (
                    "The worst rebuffer ratio across all regions — the plant's "
                    "own health verdict, and the signal the only alert rule "
                    "fires on.\n\n"
                    "Placed HERE, beside the content verdict, on purpose. "
                    "During a black-source fault this stays green while the "
                    "panel to its left is red. Delivery telemetry is not wrong; "
                    "it is answering a different question, and nothing in a "
                    "conventional stack asks the one that matters."
                ),
                "gridPos": {"h": 9, "w": 6, "x": 18, "y": 1},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target(f"max({REBUFFER_EXPR})", "worst region")],
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "unit": "percentunit",
                        "decimals": 2,
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "green", "value": None},
                                {"color": "red", "value": REBUFFER_THRESHOLD},
                            ],
                        },
                    },
                    "overrides": [],
                },
                "options": {
                    "colorMode": "background",
                    "graphMode": "area",
                    "textMode": "value",
                    "reduceOptions": {"calcs": ["lastNotNull"]},
                },
            },

            {
                "id": 1,
                "type": "timeseries",
                "title": "Synthetic gauge — operator-driven canary",
                "description": (
                    "DELIBERATE, not a leftover. This gauge is driven by hand "
                    "(`make set VALUE=95`) and has nothing to do with the "
                    "video plant. It answers the question you cannot answer "
                    "from plant metrics alone: when encoder or viewer metrics "
                    "go missing, is the PLANT broken or is the PIPE broken? "
                    "If this canary still moves, Alloy -> Mimir is healthy and "
                    "the fault is in the plant. It carries no alert — the only "
                    "alert is L3's rebuffer_ratio."
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
                "title": "Canary value now",
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
                "title": "Pipe liveness (deadair_emitter_up)",
                "description": (
                    "Flat line at 1 = the collector is scraping and "
                    "remote_write is delivering. A gap here means the "
                    "telemetry pipe failed, not the plant — which is exactly "
                    "the ambiguity this row exists to resolve."
                ),
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
            # --- L2: CDN edges ----------------------------------------------
            {
                "id": 20,
                "type": "row",
                "title": "L2 — CDN edges",
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": 35},
                "collapsed": False,
                "panels": [],
            },
            {
                "id": 21,
                "type": "timeseries",
                "title": "p95 segment TTFB by region",
                "description": (
                    "The regional differential. One region climbing while the "
                    "others stay flat is the edge_latency signature — CDN edge "
                    "degradation, not an encoder or packager fault. Depends on "
                    "the `le` label surviving the cardinality allowlist."
                ),
                "gridPos": {"h": 9, "w": 16, "x": 0, "y": 36},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [{
                    "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                    "editorMode": "code",
                    "expr": ("histogram_quantile(0.95, sum by (region, le) "
                             "(rate(segment_ttfb_seconds_bucket[5m])))"),
                    "legendFormat": "{{region}}",
                    "range": True,
                    "refId": "A",
                }],
                "fieldConfig": {
                    "defaults": {
                        "custom": {"lineWidth": 2, "fillOpacity": 6},
                        "unit": "s",
                        "thresholds": {"mode": "absolute", "steps": [
                            {"color": "green", "value": None},
                            {"color": "red", "value": 0.5}]},
                    },
                    "overrides": [],
                },
            },
            {
                "id": 22,
                "type": "timeseries",
                "title": "Injected chaos by region",
                "description": "Which region is currently being degraded, "
                               "reported by the edges themselves.",
                "gridPos": {"h": 9, "w": 8, "x": 16, "y": 36},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("edge_chaos_active", "{{region}}")],
                "fieldConfig": {
                    "defaults": {"custom": {"lineWidth": 2, "fillOpacity": 20},
                                 "min": 0, "max": 1},
                    "overrides": [],
                },
            },
            {
                "id": 23,
                "type": "timeseries",
                "title": "Edge cache hit ratio by region",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 45},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("edge_cache_hit_ratio", "{{region}}")],
                "fieldConfig": {
                    "defaults": {"custom": {"lineWidth": 2}, "unit": "percentunit",
                                 "min": 0, "max": 1},
                    "overrides": [],
                },
            },
            {
                "id": 24,
                "type": "timeseries",
                "title": "Segment responses by status class",
                "description": "A 4xx storm across all regions points at the "
                               "packager (segment_gap), not any single edge.",
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": 45},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [{
                    "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                    "editorMode": "code",
                    "expr": "sum by (region, status) (rate(segment_status[5m]))",
                    "legendFormat": "{{region}} {{status}}",
                    "range": True,
                    "refId": "A",
                }],
                "fieldConfig": {
                    "defaults": {"custom": {"lineWidth": 2}, "unit": "reqps"},
                    "overrides": [],
                },
            },
            # --- L3: viewer fleet -------------------------------------------
            {
                "id": 30,
                "type": "row",
                "title": "L3 — viewer fleet (QoE)",
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": 53},
                "collapsed": False,
                "panels": [],
            },
            {
                "id": 31,
                "type": "timeseries",
                "title": f"Rebuffer ratio by region (alert > {REBUFFER_THRESHOLD:.0%})",
                "description": (
                    "The client-side signal. Rebuffer ratio cannot be measured "
                    "at the CDN — only a player knows its buffer stalled. This "
                    "is what the alert watches, and what the agent investigates."
                ),
                "gridPos": {"h": 9, "w": 16, "x": 0, "y": 54},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [{
                    "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                    "editorMode": "code",
                    "expr": REBUFFER_EXPR,
                    "legendFormat": "{{region}}",
                    "range": True,
                    "refId": "A",
                }],
                "fieldConfig": {
                    "defaults": {
                        "custom": {"lineWidth": 2, "fillOpacity": 8},
                        "unit": "percentunit",
                        "min": 0,
                        "thresholds": {"mode": "absolute", "steps": [
                            {"color": "green", "value": None},
                            {"color": "red", "value": REBUFFER_THRESHOLD}]},
                    },
                    "overrides": [],
                },
            },
            {
                "id": 32,
                "type": "timeseries",
                "title": "Delivered bitrate by region (ABR response)",
                "description": (
                    "Bitrate falling WITH rebuffering means the link degraded. "
                    "Bitrate falling WITHOUT rebuffering is ladder_collapse — "
                    "an encoder rung died and players quietly settled lower."
                ),
                "gridPos": {"h": 9, "w": 8, "x": 16, "y": 54},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [{
                    "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                    "editorMode": "code",
                    "expr": "avg by (region) (viewer_bitrate_avg)",
                    "legendFormat": "{{region}}",
                    "range": True,
                    "refId": "A",
                }],
                "fieldConfig": {
                    "defaults": {"custom": {"lineWidth": 2}, "unit": "bps", "min": 0},
                    "overrides": [],
                },
            },
            {
                "id": 33,
                "type": "timeseries",
                "title": "Rebuffer ratio by device class",
                "description": "Mobile holds the smallest buffer and stalls "
                               "first — a device-class split that is invisible "
                               "in any CDN metric.",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 63},
                "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
                "targets": [target("rebuffer_ratio", "{{region}} / {{device_class}}")],
                "fieldConfig": {
                    "defaults": {"custom": {"lineWidth": 2}, "unit": "percentunit",
                                 "min": 0},
                    "overrides": [],
                },
            },
            {
                "id": 34,
                "type": "logs",
                "title": "Viewer QoE beacons (Loki)",
                "description": (
                    "Per-session detail: session_id, startup time, rebuffer "
                    "count. Deliberately absent from Mimir — 200 sessions as "
                    "metric labels would exhaust the free-tier series budget."
                ),
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": 63},
                "datasource": {"type": "loki", "uid": LOKI_DATASOURCE_UID},
                "targets": [{
                    "datasource": {"type": "loki", "uid": LOKI_DATASOURCE_UID},
                    "expr": '{job="deadair-viewers"}',
                    "queryType": "range",
                    "refId": "A",
                }],
                "options": {"showTime": True, "sortOrder": "Descending",
                            "wrapLogMessage": True},
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
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 71},
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
    ]

    return {
        "uid": DASHBOARD_UID,
        "title": "DEAD AIR -- Plant",
        "tags": ["dead-air", "plant"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": apply_layout(panels),
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


def delete_legacy_rules(gf):
    for uid in LEGACY_RULE_UIDS:
        try:
            gf.request("GET", f"/api/v1/provisioning/alert-rules/{uid}")
        except RuntimeError:
            continue
        gf.request("DELETE", f"/api/v1/provisioning/alert-rules/{uid}")
        print(f"  removed placeholder rule {uid!r}")


# Brief §5: rebuffer_ratio > 0.02 for 2m, BY REGION.
#
# Computed from counters rather than averaging the rebuffer_ratio gauge across
# device classes. Averaging gauges weights a handful of mobile sessions equally
# with a large TV cohort; deriving the ratio from summed rates weights it by
# actual viewing time, which is what a region-level rebuffer ratio means.
REBUFFER_EXPR = (
    "sum by (region) (rate(viewer_rebuffer_seconds_total[5m])) / "
    "clamp_min("
    "  sum by (region) (rate(viewer_rebuffer_seconds_total[5m])) + "
    "  sum by (region) (rate(viewer_playing_seconds_total[5m]))"
    ", 0.0001)"
)
REBUFFER_THRESHOLD = 0.02


def upsert_alert_rule(gf, route_to_webhook):
    rule = {
        "uid": RULE_UID,
        "title": "DEAD AIR / rebuffer ratio high",
        "condition": "C",
        "folderUID": FOLDER_UID,
        "ruleGroup": RULE_GROUP,
        "noDataState": "NoData",
        "execErrState": "Error",
        "for": "2m",
        "annotations": {
            "summary": (
                "Viewer rebuffer ratio in {{ $labels.region }} is above "
                f"{REBUFFER_THRESHOLD:.0%} -- viewers in this region are "
                "stalling. Rebuffer ratio is a client-side signal; delivery "
                "telemetry may look healthy."
            ),
            "__dashboardUid__": DASHBOARD_UID,
            "__panelId__": "31",
        },
        # `region` is NOT hardcoded here -- it arrives from the query's series
        # labels, giving one alert instance per region. The agent keys its whole
        # investigation off that label, so it must survive into the payload.
        "labels": {"project": "dead-air", "layer": "L3"},
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": DATASOURCE_UID,
                "model": {
                    "refId": "A",
                    "expr": REBUFFER_EXPR,
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
                            "evaluator": {"type": "gt", "params": [REBUFFER_THRESHOLD]},
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

    desc = f"rebuffer_ratio > {REBUFFER_THRESHOLD} for 2m, by region"
    try:
        gf.request("GET", f"/api/v1/provisioning/alert-rules/{RULE_UID}")
        gf.request("PUT", f"/api/v1/provisioning/alert-rules/{RULE_UID}", rule)
        print(f"  alert rule {RULE_UID!r} updated ({desc})")
    except RuntimeError:
        gf.request("POST", "/api/v1/provisioning/alert-rules", rule)
        print(f"  alert rule {RULE_UID!r} created ({desc})")

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

    delete_legacy_rules(gf)
    upsert_alert_rule(gf, route)
    print(f"\ndone. dashboard: {base}/d/{DASHBOARD_UID}")


if __name__ == "__main__":
    main()
