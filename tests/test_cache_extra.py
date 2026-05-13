"""Extra tests for ResponseCache: privacy, false-hit, similarity."""
from __future__ import annotations

import pytest

from reliability_lab.cache import ResponseCache, _is_uncacheable, _looks_like_false_hit


def test_privacy_query_not_stored() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("account balance for user 123", "Balance: $500")
    cached, _ = cache.get("account balance for user 123")
    assert cached is None


def test_privacy_query_not_retrieved() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    # Store manually bypassing set() guard
    from reliability_lab.cache import CacheEntry
    import time as _t
    cache._entries.append(CacheEntry("account balance for user 123", "secret", _t.time(), {}))
    cached, _ = cache.get("account balance for user 123")
    assert cached is None  # get() should reject on read too


def test_exact_match_returns_score_1() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("hello world", "response A")
    cached, score = cache.get("hello world")
    assert cached == "response A"
    assert score == 1.0


def test_false_hit_different_years_blocked() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, _ = cache.get("Summarize refund policy for 2026 deadline")
    assert cached is None
    assert len(cache.false_hit_log) >= 1


def test_is_uncacheable_patterns() -> None:
    assert _is_uncacheable("my account balance is low")
    assert _is_uncacheable("user 999 reset password")
    assert not _is_uncacheable("explain circuit breaker pattern")


def test_looks_like_false_hit_year_mismatch() -> None:
    assert _looks_like_false_hit("policy 2024", "policy 2026")
    assert not _looks_like_false_hit("policy 2024", "policy 2024")
    assert not _looks_like_false_hit("policy info", "policy help")  # no 4-digit numbers
