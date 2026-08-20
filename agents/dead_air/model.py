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

import os

from google.adk.models import Gemini
from google.genai import types

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

# Settled by docs/vision-spike.md: gemini-3.7-flash is the only tier with a zero
# false-positive rate on healthy frames, and no higher tier is used anywhere.
RETRY = types.HttpRetryOptions(
    attempts=5,
    initial_delay=2.0,
    max_delay=60.0,
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
    # invalid credential still fails all five attempts and surfaces.
    http_status_codes=[401, 429, 500, 502, 503, 504],
)


def build_model() -> Gemini:
    """A Gemini model with backoff, for every agent in the pipeline."""
    return Gemini(
        model=MODEL_NAME,
        retry_options=RETRY,
    )
