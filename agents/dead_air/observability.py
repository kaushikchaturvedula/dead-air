"""The reflexive layer: DEAD AIR observing itself.

Brief §1 asks for the agent to be observable in the same Grafana Cloud stack it
investigates, and §7 puts it on screen at 2:25-2:50. It is also the most
Grafana-specific thing in the submission: an ops agent that is itself an
observable service, whose traces, token cost and tool activity land next to the
plant's.

The tidy part is that DEAD AIR ends up debuggable by exactly the technique it
practises. When the forced-function-calling loop ran until the 900s ceiling, the
only evidence was a log tail. With this in place, that failure is a trace: one
agent span containing dozens of identical tool spans, visible at a glance.

HOW IT WORKS
------------
ADK already emits OpenTelemetry spans for LLM calls and tool calls
(google.adk.telemetry.trace_call_llm / trace_tool_call). It does not ship a
configured exporter, so those spans go nowhere by default. This module supplies
the missing piece: a TracerProvider exporting OTLP to the same Alloy collector
the plant already uses, which forwards to Grafana Cloud Tempo.

    agent spans ─▶ OTLP/HTTP ─▶ Alloy :4318 ─▶ Grafana Cloud Tempo
                                   ▲
                     the plant's own traces already go here

Nothing new is deployed. The transport was built for L2/L3 and is reused.

GenAI SEMANTIC CONVENTIONS
--------------------------
Grafana Cloud AI Observability keys off the OTel GenAI conventions, so token
counts are recorded as gen_ai.usage.input_tokens / output_tokens and the model
as gen_ai.request.model. Using the standard names rather than bespoke ones is
what makes the data show up in the AI Observability views instead of as
anonymous spans.

NO NEW RUNTIME DEPENDENCY ON AN AI FRAMEWORK
--------------------------------------------
opentelemetry-sdk arrives transitively with google-adk; only the OTLP HTTP
exporter is added. OpenTelemetry is vendor-neutral instrumentation, not an agent
framework or a model, so this does not touch the contest's Google-only rule.
"""

import logging
import os
import threading

_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                           "http://localhost:4318")
_SERVICE = os.environ.get("OTEL_SERVICE_NAME", "deadair-agent")
_ENABLED = os.environ.get("DEADAIR_AGENT_TRACING", "1") not in ("0", "false", "")

logger = logging.getLogger("deadair.observability")

_lock = threading.Lock()
_installed = False
_tracer = None
_install_error = ""

# Rough Vertex list pricing for gemini-3.7-flash, USD per 1M tokens. Cost is an
# ESTIMATE derived from token counts, not a billing figure -- it exists so the
# demo can show cost per incident, and so a runaway loop is visible as spend
# rather than only as latency.
_INPUT_USD_PER_MTOK = float(os.environ.get("DEADAIR_INPUT_USD_PER_MTOK", "0.30"))
_OUTPUT_USD_PER_MTOK = float(os.environ.get("DEADAIR_OUTPUT_USD_PER_MTOK", "2.50"))

# Per-invocation totals, so a run can report what it cost.
_totals = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0,
           "tool_calls": 0, "usd": 0.0}


def install_agent_tracing() -> bool:
    """Point ADK's existing OTel spans at Alloy. Idempotent.

    Returns True if tracing is active, False if disabled or unavailable. A
    missing exporter must never break an investigation -- observability that
    takes down the thing it observes is worse than none.
    """
    global _installed, _tracer, _install_error
    with _lock:
        if _installed:
            return _tracer is not None
        _installed = True
        if not _ENABLED:
            return False
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create({
                "service.name": _SERVICE,
                "service.namespace": "dead-air",
                "deployment.environment": os.environ.get("DEADAIR_ENV", "local"),
                # Marks this as the AGENT, distinct from the plant services
                # whose traces share this Tempo instance.
                "deadair.layer": "L5-agent",
            })
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{_ENDPOINT}/v1/traces")))
            trace.set_tracer_provider(provider)
            _tracer = trace.get_tracer("deadair.agent")
            logger.info("agent tracing active -> %s (service=%s)",
                        _ENDPOINT, _SERVICE)
            return True
        except Exception as exc:                       # noqa: BLE001
            # LOUD. A silently disabled reflexive layer shows an empty
            # dashboard during the §7 demo beat with nothing explaining why --
            # the same swallow-the-ImportError shape that made ADK's McpToolset
            # vanish without a word on day one.
            _tracer = None
            _install_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "REFLEXIVE LAYER DISABLED -- agent tracing failed to install: "
                "%s. The agent still runs, but its own traces, token cost and "
                "tool activity will NOT reach Grafana Cloud. Check that "
                "opentelemetry-exporter-otlp-proto-http is installed and that "
                "%s is reachable. Run `make agent-observability-check` to "
                "confirm once fixed.", _install_error, _ENDPOINT)
            return False


_warned = set()


def _warn_once(where, exc):
    """Report an instrumentation failure once per site, not once per span."""
    if where in _warned:
        return
    _warned.add(where)
    logger.warning("agent instrumentation error in %s (%s: %s). Telemetry for "
                   "this path will be incomplete; the investigation continues.",
                   where, type(exc).__name__, exc)


def install_error() -> str:
    """Why tracing is off, or empty string if it is on."""
    return _install_error


def _tracer_or_none():
    if not _installed:
        install_agent_tracing()
    return _tracer


def record_llm_usage(callback_context, llm_response):
    """after_model_callback: record token usage in GenAI convention terms.

    ADK spans the LLM call already; this adds the usage numbers Grafana Cloud
    AI Observability reads, plus an estimated cost.
    """
    usage = getattr(llm_response, "usage_metadata", None)
    if usage is None:
        return None

    prompt = getattr(usage, "prompt_token_count", 0) or 0
    candidates = getattr(usage, "candidates_token_count", 0) or 0
    usd = (prompt / 1e6) * _INPUT_USD_PER_MTOK + \
          (candidates / 1e6) * _OUTPUT_USD_PER_MTOK

    with _lock:
        _totals["input_tokens"] += prompt
        _totals["output_tokens"] += candidates
        _totals["llm_calls"] += 1
        _totals["usd"] += usd

    tracer = _tracer_or_none()
    if tracer is None:
        return None
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            agent_name = getattr(getattr(callback_context, "_invocation_context",
                                         None), "agent", None)
            span.set_attribute("gen_ai.system", "vertex_ai")
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model",
                               os.environ.get("GEMINI_MODEL", "gemini-3.7-flash"))
            span.set_attribute("gen_ai.usage.input_tokens", prompt)
            span.set_attribute("gen_ai.usage.output_tokens", candidates)
            span.set_attribute("deadair.estimated_cost_usd", round(usd, 6))
            if agent_name is not None:
                span.set_attribute("deadair.agent",
                                   getattr(agent_name, "name", str(agent_name)))
    except Exception as exc:                           # noqa: BLE001
        # Never fail an investigation to record telemetry -- but say so once,
        # rather than losing every token count in silence.
        _warn_once("llm_usage", exc)
    return None


def record_tool_call(tool, args, tool_context):
    """before_tool_callback: mark which tool an agent reached for.

    Tool activity is the part of an agent's behaviour that is hardest to
    reconstruct after the fact, and the part that goes wrong most visibly -- a
    loop, a wrong datasource, a tool that should never have been called.
    """
    with _lock:
        _totals["tool_calls"] += 1
    tracer = _tracer_or_none()
    if tracer is None:
        return None
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("gen_ai.tool.name", getattr(tool, "name", "?"))
            span.set_attribute("deadair.tool.arg_count", len(args or {}))
    except Exception as exc:                           # noqa: BLE001
        _warn_once("tool_call", exc)
    return None


def usage_totals() -> dict:
    """What this process has spent so far. Estimated cost, real token counts."""
    with _lock:
        t = dict(_totals)
    t["tracing_active"] = _tracer is not None
    if _install_error:
        t["tracing_error"] = _install_error
    t["usd"] = round(t["usd"], 4)
    t["note"] = ("cost is estimated from token counts at configured list "
                 "prices; token counts themselves are reported by Vertex")
    return t


def reset_usage():
    with _lock:
        for k in ("input_tokens", "output_tokens", "llm_calls", "tool_calls"):
            _totals[k] = 0
        _totals["usd"] = 0.0
