"""Shared model configuration for every DEAD AIR agent.

One place, so the tier decision and the retry policy cannot drift between
phases.

WHY RETRY IS NOT OPTIONAL HERE
------------------------------
Phase 1's fan-out is a ParallelAgent: four specialists issue LLM calls
concurrently, and each then makes several tool round-trips that each cost
another call. A single investigation is therefore a burst, not a trickle, and
Vertex answers bursts past quota with 429 RESOURCE_EXHAUSTED.

That surfaced while running the six-case diagnosis evaluation back to back --
the fan-out plus consecutive cases exhausted quota and killed a run outright.
Without backoff the same thing happens during a demo, at the worst possible
moment, and looks like the agent crashing rather than a rate limit.

Exponential backoff on the retryable status codes turns a hard failure into a
slower success.
"""

import logging
import os
import re

from google.adk.models import Gemini
from google.genai import types

logger = logging.getLogger("deadair.model")

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

# Settled by docs/vision-spike.md: gemini-3.7-flash is the only tier with a zero
# false-positive rate on healthy frames, and no higher tier is used anywhere.
# Deliberately SMALL. An earlier config (5 attempts, 60s max delay) meant a
# single throttled call could burn ~62s before failing, and a phase making
# dozens of calls could sleep for tens of minutes. One evaluation case ran 128
# minutes against a ~54 minute average for its peers -- not slowness, a stall.
#
# This is a DEMO risk before it is an eval risk: mid-recording, an agent that
# hangs is unrecoverable on camera, while an agent that errors can be retried.
# Fail fast and loudly, and let the caller decide.
#
# Worst case per call now: 1 + 2 + 4 = 7s of backoff across 3 attempts.
RETRY = types.HttpRetryOptions(
    attempts=3,
    initial_delay=1.0,
    max_delay=8.0,
    exp_base=2.0,
    jitter=0.3,
    # 429 is the one that actually bites; the 5xx codes are cheap insurance.
    #
    # 401 is here deliberately and slightly uncomfortably. Under the fan-out's
    # concurrency, several clients refresh Application Default Credentials at
    # once and one occasionally comes back
    # "401 ACCESS_TOKEN_TYPE_UNSUPPORTED" -- a refresh race, not a bad
    # credential. It killed an evaluation run on its first case while a direct
    # call moments later succeeded. Retrying absorbs the race; a genuinely
    # invalid credential still fails all three attempts and surfaces.
    #
    # Because retrying auth is otherwise silent -- an expired credential would
    # read as nothing worse than a slow call -- install_retry_logging() below
    # makes every retried 401 loud and distinct from a 429.
    http_status_codes=[401, 429, 500, 502, 503, 504],
)


def build_model() -> Gemini:
    """A Gemini model with backoff, for every agent in the pipeline."""
    return Gemini(
        model=MODEL_NAME,
        retry_options=RETRY,
    )


# --- make retried failures visible ----------------------------------------

_STATUS_RE = re.compile(r'"HTTP/[\d.]+ (\d{3})')

_EXPLAIN = {
    "401": ("AUTH", "credential rejected. Usually the ADC refresh race under "
                    "concurrency, which retry absorbs -- but if these repeat, "
                    "the credential is genuinely expired. Run: "
                    "gcloud auth application-default login"),
    "403": ("AUTH", "permission denied -- not a transient condition; retrying "
                    "will not help."),
    "429": ("QUOTA", "rate limited by Vertex. Backoff will retry. Metric: "
                     "aiplatform.googleapis.com/"
                     "global_generate_content_requests_per_minute_"
                     "per_project_per_base_model"),
}


class _RetryVisibilityHandler(logging.Handler):
    """Surface retryable HTTP failures that would otherwise be silent.

    google-genai retries internally, so a 401 and a 429 both present to the
    caller as nothing more than a slower call. That is fine for quota and
    dangerous for auth: an expired credential looks identical to a busy one
    right up until it exhausts its attempts. This separates them at the point
    they happen.
    """

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:                              # noqa: BLE001
            return
        if "aiplatform" not in msg and "generativelanguage" not in msg:
            return
        m = _STATUS_RE.search(msg)
        if not m:
            return
        kind, detail = _EXPLAIN.get(m.group(1), (None, None))
        if not kind:
            return
        logger.warning("[%s] HTTP %s from Vertex -- %s", kind, m.group(1), detail)


_installed = False


def install_retry_logging():
    """Attach the visibility handler to httpx's request log. Idempotent."""
    global _installed
    if _installed:
        return
    httpx_logger = logging.getLogger("httpx")
    # httpx logs each request at INFO with the response status in the message.
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.addHandler(_RetryVisibilityHandler())
    _installed = True


install_retry_logging()
