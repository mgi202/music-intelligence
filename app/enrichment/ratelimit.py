"""
Per-service rate limiters for enrichment sources.

Each service gets one module-level limiter shared across threads. Instead of
an unconditional sleep before every request (which wastes the full interval
even when the last request was long ago), a limiter sleeps only the remainder
of the service's minimum interval since its previous request.

Intervals per service:
  MusicBrainz   1.0s   (hard requirement for anonymous clients)
  ListenBrainz  1.0s   (their guidance)
  Last.fm       0.25s  (limit is 5 req/s; stay well under)
  Discogs       1.0s with token, 2.5s without (unauth limit is 25/min)
  Bandcamp      1.0s   (unofficial; be polite)

ENRICHMENT_RATE_LIMIT_SECONDS, when set in the environment, overrides every
service's interval (the old global knob, kept as an escape hatch). Leave it
unset to get the per-service defaults above.
"""

from __future__ import annotations

import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()


class RateLimiter:
    """Thread-safe minimum-interval limiter for one service.

    wait() reserves the next request slot under the lock, then sleeps outside
    it — concurrent callers queue up spaced one interval apart rather than
    all sleeping and firing together.
    """

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0  # monotonic time the next request may fire

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = now + delay + self.interval
        if delay > 0:
            time.sleep(delay)


def service_interval(default: float) -> float:
    """A service's minimum interval, honouring the global override knob."""
    override = os.getenv("ENRICHMENT_RATE_LIMIT_SECONDS")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return default
