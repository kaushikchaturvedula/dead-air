"""Trivial synthetic metrics emitter -- Step 1a of the DEAD AIR plant.

Exists to prove the telemetry pipe (emitter -> Alloy -> Grafana Cloud Mimir ->
dashboard -> alert -> webhook) before any video exists. Once L1 is real, the
encoder exports its own metrics and this can be retired or kept as a canary.

Exposes:
  GET  /metrics       Prometheus text exposition
  GET  /healthz       liveness
  POST /set?value=N   set the synthetic gauge (this is the "change a number
                      locally and watch Grafana turn red" control)
  GET  /value         read the current gauge value

Stdlib only, deliberately: this runs in a slim Python container with no pip
install, so there is nothing to break in a clean-clone rebuild.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("EMITTER_PORT", "9101"))

# The synthetic gauge. Starts healthy; the alert rule fires above a threshold.
_START_VALUE = float(os.environ.get("EMITTER_START_VALUE", "10"))

_lock = threading.Lock()
_value = _START_VALUE
_started = time.time()


def _render_metrics() -> str:
    with _lock:
        value = _value
    uptime = time.time() - _started
    return "".join(
        [
            "# HELP deadair_synthetic_gauge Operator-controlled synthetic signal "
            "used to prove the telemetry pipe end to end.\n",
            "# TYPE deadair_synthetic_gauge gauge\n",
            f"deadair_synthetic_gauge {value}\n",
            "# HELP deadair_emitter_uptime_seconds Seconds since the emitter started.\n",
            "# TYPE deadair_emitter_uptime_seconds counter\n",
            f"deadair_emitter_uptime_seconds {uptime:.3f}\n",
            "# HELP deadair_emitter_up Always 1; presence proves the scrape path.\n",
            "# TYPE deadair_emitter_up gauge\n",
            "deadair_emitter_up 1\n",
            # Cardinality canary: deliberately carries a session_id label so the
            # relabel guard in plant/alloy/config.alloy has something real to
            # strip. If session_id ever shows up on this series in Mimir, the
            # guard has regressed and the free-tier series budget is at risk.
            # Exactly ONE canary series is emitted -- emitting several that
            # differ only by session_id would collapse into duplicate samples
            # once the label is (correctly) dropped.
            "# HELP deadair_cardinality_canary Always 1; labelled with session_id "
            "to prove the collector strips per-session labels.\n",
            "# TYPE deadair_cardinality_canary gauge\n",
            'deadair_cardinality_canary{session_id="canary-0001"} 1\n',
        ]
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8"):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/metrics":
            self._reply(200, _render_metrics())
        elif path == "/healthz":
            self._reply(200, "ok\n")
        elif path == "/value":
            with _lock:
                self._reply(200, json.dumps({"value": _value}) + "\n", "application/json")
        else:
            self._reply(404, "not found\n")

    def do_POST(self):
        global _value
        parsed = urlparse(self.path)
        if parsed.path != "/set":
            self._reply(404, "not found\n")
            return
        raw = parse_qs(parsed.query).get("value", [None])[0]
        try:
            new = float(raw)
        except (TypeError, ValueError):
            self._reply(400, "usage: POST /set?value=<number>\n")
            return
        with _lock:
            _value = new
        print(f"[emitter] deadair_synthetic_gauge := {new}", flush=True)
        self._reply(200, json.dumps({"value": new}) + "\n", "application/json")

    def log_message(self, *args):
        # Silence per-request scrape logging; Alloy scrapes every few seconds.
        pass


if __name__ == "__main__":
    print(
        f"[emitter] listening on :{PORT} "
        f"(deadair_synthetic_gauge={_START_VALUE})",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
