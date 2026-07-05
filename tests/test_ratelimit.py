"""Per-service rate limiter (efficiency pass, 2026-07-05)."""

import threading
import time

from app.enrichment.ratelimit import RateLimiter, service_interval


def test_first_call_does_not_sleep():
    rl = RateLimiter(5.0)
    start = time.monotonic()
    rl.wait()
    assert time.monotonic() - start < 0.1


def test_second_call_sleeps_only_remainder():
    rl = RateLimiter(0.2)
    rl.wait()
    time.sleep(0.1)  # burn half the interval elsewhere
    start = time.monotonic()
    rl.wait()
    elapsed = time.monotonic() - start
    assert 0.05 <= elapsed < 0.2  # ~0.1s remainder, not the full 0.2s


def test_call_after_interval_elapsed_does_not_sleep():
    rl = RateLimiter(0.1)
    rl.wait()
    time.sleep(0.15)
    start = time.monotonic()
    rl.wait()
    assert time.monotonic() - start < 0.05


def test_zero_interval_never_sleeps():
    rl = RateLimiter(0.0)
    start = time.monotonic()
    for _ in range(100):
        rl.wait()
    assert time.monotonic() - start < 0.1


def test_concurrent_callers_are_spaced():
    """N threads hitting one limiter get slots spaced ≥ interval apart."""
    rl = RateLimiter(0.1)
    stamps = []
    lock = threading.Lock()

    def hit():
        rl.wait()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.08 for g in gaps), gaps  # small scheduling tolerance


def test_service_interval_default_and_override(monkeypatch):
    monkeypatch.delenv("ENRICHMENT_RATE_LIMIT_SECONDS", raising=False)
    assert service_interval(0.25) == 0.25

    monkeypatch.setenv("ENRICHMENT_RATE_LIMIT_SECONDS", "2.0")
    assert service_interval(0.25) == 2.0

    monkeypatch.setenv("ENRICHMENT_RATE_LIMIT_SECONDS", "not-a-number")
    assert service_interval(0.25) == 0.25
