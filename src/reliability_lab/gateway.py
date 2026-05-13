from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str          # e.g. "primary:primary", "fallback:backup", "cache_hit:0.95", "static_fallback"
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers.

    Request flow:
    1. Cache check  → return cached response immediately (latency ~0 ms).
    2. Circuit breaker per provider — OPEN circuit raises CircuitOpenError (fail fast).
    3. Fallback chain: iterate providers in order; first success wins.
    4. Static fallback message when all providers fail / circuits are open.
    5. Cost budget: when cumulative cost_usd > budget_limit_usd, skip expensive
       providers and prefer cheaper alternatives / cached responses.
    """

    BUDGET_LIMIT_USD: float = 0.10  # halt expensive providers above this

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ) -> None:
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cumulative_cost_usd: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response; degrade gracefully if providers fail."""
        wall_start = time.perf_counter()

        # 1. Cache check (exact or semantic match)
        if self.cache is not None:
            try:
                cached, score = self.cache.get(prompt)
            except Exception:
                cached, score = None, 0.0
            if cached is not None:
                wall_ms = (time.perf_counter() - wall_start) * 1000
                return GatewayResponse(
                    text=cached,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=wall_ms,
                    estimated_cost=0.0,
                )

        # 2. Try each provider through its circuit breaker
        last_error: str | None = None
        for idx, provider in enumerate(self.providers):
            is_primary = idx == 0
            breaker = self.breakers[provider.name]

            # Cost budget guard: when over budget, skip the most expensive provider
            if self.cumulative_cost_usd >= self.BUDGET_LIMIT_USD and is_primary:
                last_error = f"cost_budget_exceeded:{self.cumulative_cost_usd:.4f}"
                continue

            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

            # Success — cache and return
            if self.cache is not None:
                try:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                except Exception:
                    pass  # cache write failure is non-fatal

            self.cumulative_cost_usd += response.estimated_cost
            wall_ms = (time.perf_counter() - wall_start) * 1000

            route_kind = "primary" if is_primary else "fallback"
            return GatewayResponse(
                text=response.text,
                route=f"{route_kind}:{provider.name}",
                provider=provider.name,
                cache_hit=False,
                latency_ms=wall_ms,
                estimated_cost=response.estimated_cost,
            )

        # 3. All providers failed — static fallback
        wall_ms = (time.perf_counter() - wall_start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=wall_ms,
            estimated_cost=0.0,
            error=last_error,
        )
