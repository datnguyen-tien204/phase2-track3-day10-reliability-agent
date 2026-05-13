from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    # Route reasons are now prefixed with kind: e.g. "primary:primary", "cache_hit:0.95"
    valid_prefixes = ("primary:", "fallback:", "cache_hit:", "static_fallback")
    assert any(result.route.startswith(p) for p in valid_prefixes), f"Unexpected route: {result.route}"


def test_circuit_opens_and_fallback_serves() -> None:
    """Force primary to fail N times, verify circuit opens then backup serves."""
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.006)
    breaker_p = CircuitBreaker("primary", failure_threshold=3, reset_timeout_seconds=60)
    breaker_b = CircuitBreaker("backup", failure_threshold=3, reset_timeout_seconds=60)
    gateway = ReliabilityGateway(
        [primary, backup],
        {"primary": breaker_p, "backup": breaker_b},
    )
    # Drive primary circuit open
    for _ in range(5):
        r = gateway.complete("test prompt")
        assert r.text  # should be served by backup, not fail

    # Circuit must have opened
    assert breaker_p.state.value in ("open", "half_open")
    # Backup should have served at least one request
    assert any(r.route.startswith("fallback") for r in [gateway.complete("another")])


def test_static_fallback_when_all_providers_fail() -> None:
    """All providers fail → static fallback message returned."""
    p1 = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    p2 = FakeLLMProvider("backup", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.006)
    b1 = CircuitBreaker("primary", failure_threshold=3, reset_timeout_seconds=60)
    b2 = CircuitBreaker("backup", failure_threshold=3, reset_timeout_seconds=60)
    gateway = ReliabilityGateway([p1, p2], {"primary": b1, "backup": b2})
    # Burn through thresholds
    for _ in range(10):
        r = gateway.complete("irrelevant")
    # Now both circuits should be open → static fallback
    final = gateway.complete("anything")
    assert final.route == "static_fallback"
    assert "degraded" in final.text.lower()


def test_cache_hit_skips_providers() -> None:
    """A warm cache should return instantly without touching providers."""
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=200, cost_per_1k_tokens=0.01)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=60)
    cache = ResponseCache(ttl_seconds=300, similarity_threshold=0.5)
    cache.set("hello world", "cached response")
    gateway = ReliabilityGateway([primary], {"primary": breaker}, cache)
    result = gateway.complete("hello world")
    assert result.cache_hit is True
    assert result.text == "cached response"
    assert result.route.startswith("cache_hit:")
