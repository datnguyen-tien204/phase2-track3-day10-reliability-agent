"""Tests for circuit breaker state machine correctness."""
from __future__ import annotations

import time

import pytest

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def make_cb(failure_threshold: int = 3, reset_timeout: float = 0.05) -> CircuitBreaker:
    return CircuitBreaker(
        name="test",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset_timeout,
        success_threshold=1,
    )


# ------------------------------------------------------------------
# Basic state transitions
# ------------------------------------------------------------------

def test_starts_closed() -> None:
    cb = make_cb()
    assert cb.state == CircuitState.CLOSED


def test_opens_after_threshold_failures() -> None:
    cb = make_cb(failure_threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_open_raises_immediately() -> None:
    cb = make_cb(failure_threshold=1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: None)


def test_open_to_half_open_after_timeout() -> None:
    cb = make_cb(failure_threshold=1, reset_timeout=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.06)
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_success_closes() -> None:
    cb = make_cb(failure_threshold=1, reset_timeout=0.05)
    cb.record_failure()
    time.sleep(0.06)
    cb.allow_request()  # transition to HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_half_open_failure_reopens() -> None:
    cb = make_cb(failure_threshold=1, reset_timeout=0.05)
    cb.record_failure()
    time.sleep(0.06)
    cb.allow_request()  # transition to HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_no_retry_storm_when_open() -> None:
    """Circuit OPEN must fail fast — no calls leak through to provider."""
    cb = make_cb(failure_threshold=1)
    cb.record_failure()
    calls: list[int] = []
    for _ in range(10):
        try:
            cb.call(lambda: calls.append(1))
        except CircuitOpenError:
            pass
    assert len(calls) == 0


def test_full_cycle_closed_open_half_open_closed() -> None:
    cb = make_cb(failure_threshold=2, reset_timeout=0.05)
    # CLOSED → OPEN
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    # OPEN → HALF_OPEN
    time.sleep(0.06)
    cb.allow_request()
    assert cb.state == CircuitState.HALF_OPEN
    # HALF_OPEN → CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    # Transition log should capture the full cycle
    states_seen = [t["to"] for t in cb.transition_log]
    assert "open" in states_seen
    assert "half_open" in states_seen
    assert "closed" in states_seen
