"""Property-based tests for CircuitBreaker using Hypothesis.

These tests verify that the circuit breaker state machine upholds its
invariants under *arbitrary* sequences of successes and failures —
not just the hand-picked scenarios in test_circuit_breaker.py.

Properties verified:
  P1  State is always a valid CircuitState (no corruption).
  P2  After ≥ failure_threshold consecutive failures from CLOSED, state is OPEN.
  P3  An OPEN circuit never lets calls through (fail-fast guarantee).
  P4  Transition log entries are monotonically increasing in timestamp.
  P5  From HALF_OPEN, a single success always closes the circuit
      (when success_threshold == 1).
  P6  From HALF_OPEN, a single failure immediately re-opens the circuit.
  P7  failure_count never goes negative.
  P8  success_count never goes negative.
  P9  A closed circuit with zero failures never opens spontaneously.
  P10 Transition log only contains valid (from, to) pairs.

Run with:
    pytest tests/test_property_based.py -v
"""
from __future__ import annotations

import time
from typing import Sequence

import pytest
from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: set[tuple[str, str]] = {
    ("closed", "open"),
    ("open", "half_open"),
    ("half_open", "closed"),
    ("half_open", "open"),
}


def make_cb(
    failure_threshold: int = 3,
    reset_timeout: float = 30.0,  # large — we don't sleep in property tests
    success_threshold: int = 1,
) -> CircuitBreaker:
    return CircuitBreaker(
        name="prop_test",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset_timeout,
        success_threshold=success_threshold,
    )


def apply_events(cb: CircuitBreaker, events: Sequence[str]) -> None:
    """Apply a sequence of 'success'/'failure' events to a circuit breaker."""
    for event in events:
        if event == "success":
            cb.record_success()
        else:
            cb.record_failure()


# ---------------------------------------------------------------------------
# P1 — state is always a valid CircuitState
# ---------------------------------------------------------------------------

@given(
    failure_threshold=st.integers(min_value=1, max_value=10),
    events=st.lists(st.sampled_from(["success", "failure"]), min_size=0, max_size=50),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_state_always_valid(failure_threshold: int, events: list[str]) -> None:
    """P1: State is always one of the three valid enum values."""
    cb = make_cb(failure_threshold=failure_threshold)
    apply_events(cb, events)
    assert cb.state in (CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN)


# ---------------------------------------------------------------------------
# P2 — enough consecutive failures always opens the circuit
# ---------------------------------------------------------------------------

@given(failure_threshold=st.integers(min_value=1, max_value=8))
@settings(max_examples=200)
def test_consecutive_failures_open_circuit(failure_threshold: int) -> None:
    """P2: failure_threshold consecutive failures from CLOSED → OPEN."""
    cb = make_cb(failure_threshold=failure_threshold)
    for _ in range(failure_threshold):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# P3 — OPEN circuit never allows calls through
# ---------------------------------------------------------------------------

@given(
    failure_threshold=st.integers(min_value=1, max_value=5),
    extra_calls=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=200)
def test_open_circuit_fails_fast(failure_threshold: int, extra_calls: int) -> None:
    """P3: While OPEN (no timeout), every call raises CircuitOpenError."""
    cb = make_cb(failure_threshold=failure_threshold, reset_timeout=9999.0)
    for _ in range(failure_threshold):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    calls: list[int] = []
    circuit_errors = 0
    for _ in range(extra_calls):
        try:
            cb.call(lambda: calls.append(1))
        except CircuitOpenError:
            circuit_errors += 1

    # No calls should have leaked through
    assert len(calls) == 0
    assert circuit_errors == extra_calls


# ---------------------------------------------------------------------------
# P4 — transition log timestamps are monotonically non-decreasing
# ---------------------------------------------------------------------------

@given(
    failure_threshold=st.integers(min_value=1, max_value=4),
    events=st.lists(st.sampled_from(["success", "failure"]), min_size=5, max_size=40),
)
@settings(max_examples=200)
def test_transition_log_monotonic(failure_threshold: int, events: list[str]) -> None:
    """P4: Transition log timestamps never go backwards."""
    cb = make_cb(failure_threshold=failure_threshold)
    apply_events(cb, events)
    timestamps = [float(entry["ts"]) for entry in cb.transition_log]
    assert timestamps == sorted(timestamps), f"Non-monotonic timestamps: {timestamps}"


# ---------------------------------------------------------------------------
# P5 — HALF_OPEN + success → CLOSED (when success_threshold == 1)
# ---------------------------------------------------------------------------

def test_half_open_single_success_closes() -> None:
    """P5: From HALF_OPEN, one success always closes the circuit."""
    cb = make_cb(failure_threshold=1, reset_timeout=0.05, success_threshold=1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.1)  # > 50 ms — enough on Windows (~15 ms timer resolution)
    cb.allow_request()  # trips to HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# P6 — HALF_OPEN + failure → OPEN (immediate re-open)
# ---------------------------------------------------------------------------

def test_half_open_any_failure_reopens() -> None:
    """P6: From HALF_OPEN, any failure immediately re-opens.

    Uses 50 ms reset_timeout + 100 ms sleep to work reliably on Windows
    where time.sleep() has ~15 ms timer resolution.
    """
    cb = make_cb(failure_threshold=1, reset_timeout=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.1)  # > 50 ms — enough even on Windows
    result = cb.allow_request()  # transitions OPEN -> HALF_OPEN
    assert result is True, "allow_request() should return True after timeout"
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# P7 & P8 — counters never negative
# ---------------------------------------------------------------------------

@given(
    failure_threshold=st.integers(min_value=1, max_value=5),
    events=st.lists(st.sampled_from(["success", "failure"]), min_size=0, max_size=60),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_counters_never_negative(failure_threshold: int, events: list[str]) -> None:
    """P7 & P8: failure_count and success_count are always >= 0."""
    cb = make_cb(failure_threshold=failure_threshold)
    apply_events(cb, events)
    assert cb.failure_count >= 0, f"failure_count={cb.failure_count}"
    assert cb.success_count >= 0, f"success_count={cb.success_count}"


# ---------------------------------------------------------------------------
# P9 — closed circuit with only successes never opens spontaneously
# ---------------------------------------------------------------------------

@given(success_count=st.integers(min_value=1, max_value=100))
@settings(max_examples=200)
def test_successes_never_open_circuit(success_count: int) -> None:
    """P9: Applying only successes to a CLOSED circuit never opens it."""
    cb = make_cb(failure_threshold=3)
    for _ in range(success_count):
        cb.record_success()
    assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# P10 — transition log only contains valid (from, to) pairs
# ---------------------------------------------------------------------------

@given(
    failure_threshold=st.integers(min_value=1, max_value=4),
    events=st.lists(st.sampled_from(["success", "failure"]), min_size=3, max_size=50),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_transition_log_valid_pairs(failure_threshold: int, events: list[str]) -> None:
    """P10: Every (from, to) pair in the transition log is a valid transition."""
    cb = make_cb(failure_threshold=failure_threshold)
    apply_events(cb, events)
    for entry in cb.transition_log:
        pair = (str(entry["from"]), str(entry["to"]))
        assert pair in VALID_TRANSITIONS, f"Invalid transition {pair} in log: {cb.transition_log}"


# ---------------------------------------------------------------------------
# P11 — failure_count resets after circuit opens
# ---------------------------------------------------------------------------

@given(failure_threshold=st.integers(min_value=1, max_value=5))
@settings(max_examples=100)
def test_failure_count_resets_on_open(failure_threshold: int) -> None:
    """P11: failure_count is reset to 0 after circuit opens."""
    cb = make_cb(failure_threshold=failure_threshold)
    for _ in range(failure_threshold):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.failure_count == 0


# ---------------------------------------------------------------------------
# P12 — opening_at is set when circuit opens, None when closed
# ---------------------------------------------------------------------------

@given(failure_threshold=st.integers(min_value=1, max_value=4))
@settings(max_examples=100)
def test_opened_at_set_on_open(failure_threshold: int) -> None:
    """P12: opened_at is set (non-None) after circuit opens."""
    cb = make_cb(failure_threshold=failure_threshold)
    assert cb.opened_at is None
    for _ in range(failure_threshold):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.opened_at is not None
