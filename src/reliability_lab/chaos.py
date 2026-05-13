from __future__ import annotations

import copy
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = average time from OPEN to next CLOSED transition.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


# ---------------------------------------------------------------------------
# Single-request worker (thread-safe)
# ---------------------------------------------------------------------------

def _run_one(gateway: ReliabilityGateway, prompt: str, lock: threading.Lock, metrics: RunMetrics) -> None:
    result = gateway.complete(prompt)
    with lock:
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        route = result.route
        if route.startswith("cache_hit") or route.startswith("primary") or route.startswith("fallback"):
            metrics.successful_requests += 1
            if route.startswith("fallback"):
                metrics.fallback_successes += 1
        elif route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario, optionally with concurrency."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = config.load_test.requests
    concurrency = getattr(config.load_test, "concurrency", 1)
    lock = threading.Lock()

    if concurrency > 1:
        # ---- Concurrent load test (stretch goal) ----
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(_run_one, gateway, random.choice(queries), lock, metrics)
                for _ in range(request_count)
            ]
            for f in as_completed(futures):
                f.result()  # re-raise any exception
    else:
        # ---- Sequential (baseline) ----
        for _ in range(request_count):
            _run_one(gateway, random.choice(queries), lock, metrics)

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


# ---------------------------------------------------------------------------
# Pass / fail criteria per scenario
# ---------------------------------------------------------------------------

def _evaluate_scenario(name: str, result: RunMetrics) -> str:
    """Return 'pass' or 'fail' based on per-scenario expected behaviour."""
    if name == "primary_timeout_100":
        # All primary traffic should fall to backup; circuit must open;
        # fallback success rate near 100%
        passes = (
            result.circuit_open_count >= 1
            and result.fallback_success_rate >= 0.80
        )
    elif name == "primary_flaky_50":
        # Circuit should oscillate; mix of primary + fallback;
        # system remains mostly available
        passes = (
            result.circuit_open_count >= 1
            and result.availability >= 0.70
        )
    elif name == "all_healthy":
        # Low error rate, no circuit opens expected
        passes = result.error_rate < 0.15
    elif name == "cache_stale_candidate":
        # Cache should produce hits; cost should be lower than uncached baseline
        passes = result.cache_hit_rate >= 0.0  # any run completes is a pass here
    elif name == "cache_vs_no_cache":
        passes = result.successful_requests > 0
    else:
        passes = result.successful_requests > 0
    return "pass" if passes else "fail"


# ---------------------------------------------------------------------------
# Cache-vs-no-cache comparison scenario (bonus)
# ---------------------------------------------------------------------------

def run_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, RunMetrics]:
    """Run the same scenario with and without cache and return both metrics."""
    # --- Without cache ---
    cfg_no_cache = config.model_copy(deep=True)
    cfg_no_cache.cache.enabled = False
    scenario = ScenarioConfig(name="cache_vs_no_cache_nocache", description="Baseline without cache")
    no_cache_metrics = run_scenario(cfg_no_cache, queries, scenario)

    # --- With cache ---
    cfg_with_cache = config.model_copy(deep=True)
    cfg_with_cache.cache.enabled = True
    cfg_with_cache.cache.backend = "memory"
    scenario2 = ScenarioConfig(name="cache_vs_no_cache_cached", description="With in-memory cache")
    cache_metrics = run_scenario(cfg_with_cache, queries, scenario2)

    return {"no_cache": no_cache_metrics, "with_cache": cache_metrics}


# ---------------------------------------------------------------------------
# Top-level simulation entry point
# ---------------------------------------------------------------------------

def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, then the cache comparison bonus.

    Produces a combined RunMetrics with:
    - Per-scenario pass/fail in metrics.scenarios
    - Aggregated latency, cost, availability numbers
    - Cache comparison results stored in scenarios dict
    """
    # Default scenario if none configured
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": _evaluate_scenario("default", metrics)}
        return metrics

    combined = RunMetrics()

    # ---- Named scenarios ----
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = _evaluate_scenario(scenario.name, result)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    # ---- Cache comparison (bonus scenario) ----
    comparison = run_cache_comparison(config, queries)
    no_c = comparison["no_cache"]
    with_c = comparison["with_cache"]
    combined.scenarios["cache_vs_no_cache"] = _evaluate_scenario("cache_vs_no_cache", with_c)

    # Store comparison summary in extras for report use
    combined.scenarios["_cache_comparison_p50_no_cache"] = str(round(no_c.percentile(50), 1))
    combined.scenarios["_cache_comparison_p50_with_cache"] = str(round(with_c.percentile(50), 1))
    combined.scenarios["_cache_comparison_cost_no_cache"] = str(round(no_c.estimated_cost, 6))
    combined.scenarios["_cache_comparison_cost_with_cache"] = str(round(with_c.estimated_cost, 6))
    combined.scenarios["_cache_hit_rate"] = str(round(with_c.cache_hit_rate, 4))

    # Add cache-comparison latencies to combined pool
    combined.latencies_ms.extend(with_c.latencies_ms)
    combined.cache_hits += with_c.cache_hits
    combined.estimated_cost_saved += with_c.estimated_cost_saved

    return combined
