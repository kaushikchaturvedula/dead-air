"""Stage 0's measurements, published to Mimir as a first-class plant signal.

WHY THIS EXISTS
---------------
Every metric in the plant describes DELIVERY: does the segment arrive, how fast,
does the viewer rebuffer, what bitrate did ABR settle on. All of them can be
perfectly green while the channel is showing black, because none of them look at
the picture. That gap is the entire thesis of DEAD AIR, and until now it was only
demonstrable by reading agent output.

Publishing Stage 0's luma to Mimir puts the two side by side on one dashboard:

    deadair_content_luma_avg      125  ->  17     RED
    rebuffer_ratio / TTFB / fps   unchanged       GREEN

The failure is now visible in Grafana as a contradiction between two rows, which
is a far better argument than a log line.

CARDINALITY
-----------
Labels are region and rendition only -- 3 x 4 = 12 series per metric, five
metrics, so 60 series at absolute worst and typically 5 (the sweep screens one
region/rendition at a time). Both labels are already on the collector's
`labelkeep` allowlist, so this needs no widening of the cardinality guard. That
is deliberate: a signal that forced the guard open would be the wrong signal.

TRANSPORT
---------
OTLP/HTTP to the same Alloy collector the agent already sends traces to, which
converts it to Prometheus and feeds it through the SAME cardinality guard as the
plant's scraped metrics before remote_write.

    Stage 0 ─▶ OTLP/HTTP ─▶ Alloy :4318 ─▶ prometheus conv ─▶ guard ─▶ Mimir

Push, not scrape. The agent is an intermittent CLI process, so a scrape target
would be down more often than up and every gap would render as `up=0` -- an
observability artefact indistinguishable from a real outage, on the one
dashboard whose job is to be unambiguous.

No new dependency: the OTLP HTTP exporter is already pinned for the reflexive
layer, and this uses its metrics half.
"""

import logging
import os
import threading

_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                           "http://localhost:4318")
_ENABLED = os.environ.get("DEADAIR_CONTENT_METRICS", "1") not in ("0", "false", "")
_SERVICE = os.environ.get("OTEL_SERVICE_NAME", "deadair-agent")

logger = logging.getLogger("deadair.content_metrics")

# The sentinel for "we do not currently know", kept in step with
# CONTENT_UNKNOWN in scripts/provision_grafana.py, which maps it to NOT
# WATCHING. It must never be 0: zero is a measured healthy picture.
UNKNOWN = -1

# How old the newest sample may be before a reader should stop believing it.
#
# THIS LIVES HERE, WITH THE PUBLISHER, because it is a property of the
# publishing contract -- how often these gauges are written -- not of any one
# reader. It has exactly two consumers and they must never disagree:
#
#   scripts/provision_grafana.py  builds the panel query that ENFORCES it
#   scripts/run_agent.py          self-reports whether the budget was MET
#
# It was previously defined in provision_grafana and restated as a bare `90` in
# run_agent's report(), twice, under a comment naming the constant it was
# copying. Both were 90, so behaviour was right -- but that is the same drift
# shape as the two genai clients before RETRY was shared, and here it would be
# worse: the restatement is inside the line that reports WHETHER the panel went
# stale, so a divergence would make the self-check lie about the one thing it
# exists to check.
#
# The value is coupled to the sweep interval: 90s is nine missed ticks at the
# demo's INTERVAL=10. Run the sweep slower than ~45s and readers will
# correctly, and permanently, report NOT WATCHING.
CONTENT_STALE_AFTER_SECONDS = 90

_lock = threading.Lock()
_installed = False
_gauges = None
_provider = None
_install_error = ""


def _install():
    """Build the meter provider once. Returns the gauge dict, or None."""
    global _installed, _gauges, _provider, _install_error
    with _lock:
        if _installed:
            return _gauges
        _installed = True
        if not _ENABLED:
            return None
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import (
                PeriodicExportingMetricReader,
            )
            from opentelemetry.sdk.resources import Resource

            resource = Resource.create({
                "service.name": _SERVICE,
                "service.namespace": "dead-air",
                "deployment.environment": os.environ.get("DEADAIR_ENV", "local"),
                "deadair.layer": "L5-agent",
            })
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{_ENDPOINT}/v1/metrics"),
                # Short, because this drives a live demo panel. The default 60s
                # would mean the operator sees black on screen up to a minute
                # before the dashboard agrees, which reads as the dashboard
                # being wrong rather than as the point being made.
                export_interval_millis=10_000,
            )
            _provider = MeterProvider(resource=resource, metric_readers=[reader])
            meter = _provider.get_meter("deadair.content_screen")

            # UNITS ARE DELIBERATELY EMPTY ON EVERYTHING BUT THE TIMER.
            #
            # OTel's Prometheus conversion appends a unit suffix to the metric
            # name. unit="1" becomes `_ratio`, so these shipped as
            # `deadair_content_luma_avg_ratio` on the first run and every
            # dashboard query for the declared name returned zero series --
            # an empty panel on the one dashboard whose job is to be
            # unambiguous, with nothing on screen explaining the rename. Same
            # family as the `exported_region` collision in the collector
            # config: the pipeline quietly renames a series and the failure
            # surfaces as missing data.
            #
            # `_ratio` would also be a lie. Luma is 0-255, not a ratio, and
            # `suspect` is a boolean. unit="s" on the timer is kept, because
            # there the convention produces the correct
            # `deadair_content_screen_seconds` and no double suffix.
            _gauges = {
                # THE panel. Healthy ~125, black ~17, threshold at 40.
                "luma": meter.create_gauge(
                    "deadair_content_luma_avg",
                    description=("Mean luma (signalstats YAVG) of the newest "
                                 "segment. Delivery metrics cannot see this.")),
                "suspect": meter.create_gauge(
                    "deadair_content_suspect",
                    description="1 if Stage 0 judged the picture wrong."),
                "dark_fraction": meter.create_gauge(
                    "deadair_content_dark_frame_fraction",
                    description="Fraction of frames in the segment below the "
                                "black luma threshold."),
                "frozen": meter.create_gauge(
                    "deadair_content_frozen",
                    description="1 if freezedetect fired on the segment."),
                # Publishes the cost of the check itself, so the claim that
                # detection is cheap is auditable on the dashboard rather than
                # only in a doc.
                "screen_seconds": meter.create_gauge(
                    "deadair_content_screen_seconds",
                    description="Wall-clock cost of one Stage 0 screen.",
                    unit="s"),
            }
            logger.info("content metrics active -> %s", _ENDPOINT)
            return _gauges
        except Exception as exc:                        # noqa: BLE001
            # Loud, for the same reason observability.py is loud: a silently
            # missing signal shows an empty panel during the demo with nothing
            # explaining why.
            _gauges = None
            _install_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "CONTENT METRICS DISABLED -- %s. Stage 0 still screens and the "
                "agent still works, but the content-health panel will stay "
                "empty. Check that %s is reachable.", _install_error, _ENDPOINT)
            return None


def install_error() -> str:
    return _install_error


def record_screen(verdict: dict, region: str, rendition: str,
                  duration_seconds: float = None, flush: bool = True):
    """Publish one Stage 0 verdict to Mimir. Never raises.

    Args:
        verdict: the dict returned by screen_media / screen_live_segment.
        region: region label (bounded, on the collector allowlist).
        rendition: ladder rung label (bounded, on the collector allowlist).
        duration_seconds: cost of the screen, if the caller timed it.
        flush: force an immediate export. True during a sweep so the panel
            tracks the stream; the periodic reader would otherwise batch it.
    """
    gauges = _install()
    if not gauges:
        return
    try:
        attrs = {"region": region, "rendition": rendition}
        m = verdict.get("measurements") or {}

        # An errored screen must not publish a healthy-looking luma, and must
        # not publish a fake zero either -- zero luma IS the black reading.
        # Skip the picture gauges entirely and let the series go stale, which
        # is the honest representation of "we did not get a look".
        if verdict.get("reason") == "error":
            # UNKNOWN, NOT ZERO. Zero is the value for "we looked and the
            # picture is fine", and a screen that failed is not an observation
            # about the picture at all -- it is the absence of one. Publishing
            # 0 here rendered a green PICTURE OK on the panel that carries the
            # whole thesis, from a failed segment fetch.
            #
            # That was latent while screening only happened between
            # investigations; it became reachable mid-shot the moment the
            # content monitor started screening DURING one, where a single
            # transient fetch error would have flipped the panel from red to
            # green in front of the camera.
            #
            # -1 matches CONTENT_UNKNOWN in scripts/provision_grafana.py, which
            # the stat panel maps to NOT WATCHING and colours from the orange
            # base step. Luma and the rest are deliberately NOT published, so
            # the timeseries breaks into a gap rather than flat-lining.
            gauges["suspect"].set(UNKNOWN, attrs)
            if duration_seconds is not None:
                gauges["screen_seconds"].set(round(duration_seconds, 3), attrs)
            _flush(flush)
            return

        if m.get("yavg_mean") is not None:
            gauges["luma"].set(float(m["yavg_mean"]), attrs)
        if m.get("dark_frame_fraction") is not None:
            gauges["dark_fraction"].set(float(m["dark_frame_fraction"]), attrs)
        gauges["frozen"].set(1 if m.get("freeze_detected") else 0, attrs)
        gauges["suspect"].set(1 if verdict.get("suspect") else 0, attrs)
        if duration_seconds is not None:
            gauges["screen_seconds"].set(round(duration_seconds, 3), attrs)
        _flush(flush)
    except Exception as exc:                            # noqa: BLE001
        logger.warning("content metric publish failed (%s: %s); screening "
                       "continues", type(exc).__name__, exc)


def _flush(do_it):
    if not do_it or _provider is None:
        return
    try:
        _provider.force_flush(5_000)
    except Exception as exc:                            # noqa: BLE001
        logger.warning("content metric flush failed (%s: %s)",
                       type(exc).__name__, exc)


def shutdown():
    """Flush and stop the reader, so a short CLI run does not lose its tail."""
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception:                                   # noqa: BLE001
        pass
