"""Tests for RedisCircuitBreaker.

Tests run in two modes:
  - If Redis is reachable on localhost:6379 → full integration tests.
  - If Redis is NOT reachable → degradation / fallback tests only (skip others).
"""
from __future__ import annotations

import time

import pytest

from reliability_lab.circuit_breaker import CircuitOpenError, CircuitState
from reliability_lab.redis_circuit_breaker import RedisCircuitBreaker

REDIS_URL = "redis://localhost:6379/0"


def _redis_available() -> bool:
    try:
        import redis as r_lib
        client = r_lib.from_url(REDIS_URL, socket_connect_timeout=1)
        client.ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis not reachable on localhost:6379 — run `make docker-up` first",
)


def make_rcb(name: str = "test_rcb", failure_threshold: int = 3, reset_timeout: float = 0.05) -> RedisCircuitBreaker:
    cb = RedisCircuitBreaker(
        name=name,
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset_timeout,
        redis_url=REDIS_URL,
        success_threshold=1,
    )
    cb.reset_redis()  # start with a clean slate
    return cb


# ---------------------------------------------------------------------------
# Graceful degradation (no Redis needed)
# ---------------------------------------------------------------------------

def test_graceful_degradation_no_redis() -> None:
    """RedisCircuitBreaker degrades to in-memory when Redis is unreachable."""
    cb = RedisCircuitBreaker(
        name="fallback_test",
        failure_threshold=2,
        reset_timeout_seconds=99.0,
        redis_url="redis://localhost:19999/0",  # nothing here
    )
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_graceful_degradation_call_raises_open() -> None:
    """Degraded RedisCircuitBreaker still raises CircuitOpenError when open."""
    cb = RedisCircuitBreaker(
        name="fallback_call_test",
        failure_threshold=1,
        reset_timeout_seconds=99.0,
        redis_url="redis://localhost:19999/0",
    )
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: None)


# ---------------------------------------------------------------------------
# Full Redis integration tests
# ---------------------------------------------------------------------------

@requires_redis
def test_starts_closed_redis() -> None:
    cb = make_rcb("t_closed")
    assert cb.state == CircuitState.CLOSED


@requires_redis
def test_opens_after_threshold_redis() -> None:
    cb = make_rcb("t_open", failure_threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


@requires_redis
def test_open_fails_fast_redis() -> None:
    cb = make_rcb("t_fastfail", failure_threshold=1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: None)


@requires_redis
def test_open_to_half_open_after_timeout_redis() -> None:
    cb = make_rcb("t_halfopen", failure_threshold=1, reset_timeout=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.08)
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN


@requires_redis
def test_half_open_success_closes_redis() -> None:
    cb = make_rcb("t_close2", failure_threshold=1, reset_timeout=0.05)
    cb.record_failure()
    time.sleep(0.08)
    cb.allow_request()
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


@requires_redis
def test_shared_state_across_instances() -> None:
    """Two separate RedisCircuitBreaker instances with the same name share state."""
    cb1 = make_rcb("t_shared", failure_threshold=3)
    cb2 = RedisCircuitBreaker(
        name="t_shared",
        failure_threshold=3,
        reset_timeout_seconds=0.05,
        redis_url=REDIS_URL,
    )
    # Drive failures through cb1
    for _ in range(3):
        cb1.record_failure()

    # cb2 should observe the same OPEN state without any local failures
    assert cb2.state == CircuitState.OPEN, (
        "cb2 should see OPEN state set by cb1 via Redis"
    )
