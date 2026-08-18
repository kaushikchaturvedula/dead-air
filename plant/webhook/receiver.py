"""Local alert webhook receiver -- Step 1d of the DEAD AIR plant.

Grafana Cloud alert rules POST here when they fire and when they resolve. This
is a stand-in for what eventually wakes the DEAD AIR agent: in production the
same payload triggers the ADK agent on Cloud Run, so the shape of what lands
here is the contract the agent will consume.

Prints each alert loudly and keeps the last N payloads for inspection:
  GET  /             human-readable log of what has arrived
  GET  /alerts       raw JSON of received payloads (for scripted assertions)
  POST /             Grafana webhook contact point target
  GET  /healthz      liveness

Stdlib only -- no pip install in the container.
"""

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("WEBHOOK_PORT", "9102"))
MAX_KEPT = 50

_lock = threading.Lock()
_received = deque(maxlen=MAX_KEPT)


def _summarise(payload: dict) -> str:
    status = payload.get("status", "?")
    alerts = payload.get("alerts") or []
    names = [
        (a.get("labels") or {}).get("alertname", "?")
        for a in alerts
    ] or [(payload.get("commonLabels") or {}).get("alertname", "?")]
    return f"{status.upper()} :: {', '.join(names)} ({len(alerts)} alert(s))"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._reply(200, "ok\n")
        elif path == "/alerts":
            with _lock:
                self._reply(200, json.dumps(list(_received), indent=1) + "\n", "application/json")
        elif path == "/":
            with _lock:
                items = list(_received)
            if not items:
                self._reply(200, "No alerts received yet.\n")
                return
            lines = [f"{len(items)} alert delivery(s) received:\n"]
            for item in reversed(items):
                lines.append(f"  {item['received_at']}  {item['summary']}")
            self._reply(200, "\n".join(lines) + "\n")
        else:
            self._reply(404, "not found\n")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"_unparsed": raw.decode(errors="replace")}

        summary = _summarise(payload) if isinstance(payload, dict) else "non-object payload"
        record = {
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": summary,
            "payload": payload,
        }
        with _lock:
            _received.append(record)

        print(f"\n{'=' * 72}\n[webhook] ALERT DELIVERY  {summary}\n{'=' * 72}", flush=True)
        print(json.dumps(payload, indent=1)[:4000], flush=True)
        self._reply(200, "ok\n")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"[webhook] listening on :{PORT} -- waiting for Grafana alerts", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
