"""Minimal OTLP/HTTP tracing for the DEAD AIR plant. Stdlib only.

Brief §5's L4 wants traces alongside metrics and logs, and the agent's Phase 1
fans out across all three. The plant's services are deliberately dependency-free
stdlib processes, so rather than pull in the OpenTelemetry SDK this emits OTLP
JSON directly to Alloy, which forwards it to Grafana Cloud Tempo.

What a trace carries that the other two signals cannot:

    metrics  "rebuffer ratio in europe-west1 is 0.19"      -- aggregate, no causality
    logs     "this segment 404'd for this session"         -- discrete, no timing tree
    traces   "this viewer's fetch took 6.2s, of which 5.9s was the edge waiting
              on origin"                                    -- causality and blame

Traces are where per-session detail belongs alongside Loki: span attributes are
not a cardinality problem the way labels are, so session_id, segment name and
rendition all ride along.

Sampling is head-based and decided by the viewer, then honoured downstream via
the W3C traceparent sampled flag -- so a sampled request produces a complete
tree rather than disconnected fragments.
"""

import json
import os
import queue
import random
import threading
import time
import urllib.request

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "deadair-unknown")
ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").rstrip("/")
SAMPLE_RATIO = float(os.environ.get("TRACE_SAMPLE_RATIO", "0.02"))
ENABLED = bool(ENDPOINT)

_SPAN_KIND = {"internal": 1, "server": 2, "client": 3}

_q = queue.Queue(maxsize=4096)
_dropped = 0
_exported = 0
_failed = 0
_lock = threading.Lock()


def _hex(nbytes):
    return "%0*x" % (nbytes * 2, random.getrandbits(nbytes * 8))


def new_trace_id():
    return _hex(16)


def new_span_id():
    return _hex(8)


def should_sample():
    return ENABLED and random.random() < SAMPLE_RATIO


def format_traceparent(trace_id, span_id, sampled=True):
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def parse_traceparent(header):
    """Return (trace_id, parent_span_id, sampled) or None."""
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None
    trace_id, span_id, flags = parts[1], parts[2], parts[3]
    if len(trace_id) != 32 or len(span_id) != 16:
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    try:
        sampled = bool(int(flags, 16) & 0x01)
    except ValueError:
        return None
    return trace_id, span_id, sampled


def _attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


class Span:
    """Context manager. Records nothing and costs nothing when not sampled."""

    __slots__ = ("trace_id", "span_id", "parent_id", "name", "kind",
                 "attributes", "sampled", "_start", "_status")

    def __init__(self, name, trace_id=None, parent_id=None, kind="internal",
                 sampled=True, attributes=None):
        self.name = name
        self.kind = kind
        self.sampled = sampled and ENABLED
        self.trace_id = trace_id or new_trace_id()
        self.span_id = new_span_id()
        self.parent_id = parent_id
        self.attributes = dict(attributes or {})
        self._start = None
        self._status = 0

    def set(self, key, value):
        if self.sampled:
            self.attributes[key] = value
        return self

    def error(self, message=""):
        self._status = 2
        if message:
            self.set("error.message", message)
        return self

    def traceparent(self):
        return format_traceparent(self.trace_id, self.span_id, self.sampled)

    def __enter__(self):
        self._start = time.time_ns()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.error(str(exc)[:200])
        if not self.sampled:
            return False
        end = time.time_ns()
        payload = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": _SPAN_KIND.get(self.kind, 1),
            "startTimeUnixNano": str(self._start),
            "endTimeUnixNano": str(end),
            "attributes": [_attr(k, v) for k, v in self.attributes.items()],
            "status": {"code": self._status},
        }
        if self.parent_id:
            payload["parentSpanId"] = self.parent_id
        _enqueue(payload)
        return False


def _enqueue(span_json):
    global _dropped
    try:
        _q.put_nowait(span_json)
    except queue.Full:
        # Never block the request path to emit telemetry.
        with _lock:
            _dropped += 1


def _post(batch):
    global _exported, _failed
    body = json.dumps({
        "resourceSpans": [{
            "resource": {"attributes": [
                _attr("service.name", SERVICE_NAME),
                _attr("deployment.environment", os.environ.get("DEADAIR_ENV", "local")),
            ]},
            "scopeSpans": [{"spans": batch}],
        }]
    }).encode()
    req = urllib.request.Request(
        f"{ENDPOINT}/v1/traces", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        with _lock:
            _exported += len(batch)
    except Exception:
        with _lock:
            _failed += len(batch)


def _worker():
    batch = []
    last = time.time()
    while True:
        timeout = max(0.1, 2.0 - (time.time() - last))
        try:
            batch.append(_q.get(timeout=timeout))
        except queue.Empty:
            pass
        if batch and (len(batch) >= 128 or time.time() - last >= 2.0):
            _post(batch)
            batch = []
            last = time.time()


def stats():
    with _lock:
        return {"exported": _exported, "failed": _failed, "dropped": _dropped}


def render_metrics():
    """Exporter health, so a silent trace pipeline is visible in metrics."""
    s = stats()
    return (
        "# HELP trace_spans_exported_total Spans successfully sent to the collector.\n"
        "# TYPE trace_spans_exported_total counter\n"
        f"trace_spans_exported_total {s['exported']}\n"
        "# HELP trace_spans_failed_total Spans whose export failed.\n"
        "# TYPE trace_spans_failed_total counter\n"
        f"trace_spans_failed_total {s['failed']}\n"
        "# HELP trace_spans_dropped_total Spans dropped because the queue was full.\n"
        "# TYPE trace_spans_dropped_total counter\n"
        f"trace_spans_dropped_total {s['dropped']}\n"
    )


if ENABLED:
    threading.Thread(target=_worker, daemon=True).start()
