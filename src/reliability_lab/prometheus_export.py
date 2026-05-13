"""Prometheus metrics export for the reliability lab.

Metric names match the slide spec:
  agent_requests_total      counter   (labels: route, provider)
  agent_latency_seconds     histogram (labels: provider)
  cache_hits_total          counter
  circuit_state             gauge     (labels: breaker; value: 0=closed, 1=half_open, 2=open)
  agent_cost_usd_total      counter
  agent_availability_ratio  gauge
  slo_compliance            gauge     (labels: slo_name; value: 1=pass, 0=fail)

The module exposes:
  - ``record(response, metrics_acc)``    — update counters after each request
  - ``export_text(metrics)``             — return Prometheus text-format string
  - ``write_prom_file(metrics, path)``   — write metrics file to disk
"""
from __future__ import annotations

import time
from pathlib import Path

from reliability_lab.metrics import RunMetrics
from reliability_lab.slo import SLOChecker, DEFAULT_SLOS


# ---------------------------------------------------------------------------
# Internal state (module-level, reset between test runs via reset_registry())
# ---------------------------------------------------------------------------

_counters: dict[str, float] = {}
_gauges: dict[str, float] = {}
_histograms: dict[str, list[float]] = {}
_start_time: float = time.time()


def reset_registry() -> None:
    """Clear all accumulated metrics (useful between test runs)."""
    global _start_time
    _counters.clear()
    _gauges.clear()
    _histograms.clear()
    _start_time = time.time()


def _inc(counter: str, value: float = 1.0) -> None:
    _counters[counter] = _counters.get(counter, 0.0) + value


def _set_gauge(gauge: str, value: float) -> None:
    _gauges[gauge] = value


def _observe(histogram: str, value: float) -> None:
    _histograms.setdefault(histogram, []).append(value)


# ---------------------------------------------------------------------------
# Public recording API
# ---------------------------------------------------------------------------

def record_request(
    route: str,
    provider: str | None,
    latency_ms: float,
    cost_usd: float,
    cache_hit: bool,
) -> None:
    """Record a single gateway response into Prometheus counters."""
    label = f'route="{route}",provider="{provider or "none"}"'
    _inc(f"agent_requests_total{{{label}}}")

    prov_label = f'provider="{provider or "none"}"'
    _observe(f"agent_latency_seconds{{{prov_label}}}", latency_ms / 1000.0)

    if cache_hit:
        _inc("cache_hits_total")

    _inc("agent_cost_usd_total", cost_usd)


def record_circuit_state(breaker_name: str, state_str: str) -> None:
    """Record circuit breaker state as a gauge (0=closed, 1=half_open, 2=open)."""
    state_map = {"closed": 0, "half_open": 1, "open": 2}
    value = float(state_map.get(state_str, 0))
    _set_gauge(f'circuit_state{{breaker="{breaker_name}"}}', value)


# ---------------------------------------------------------------------------
# Export RunMetrics snapshot to Prometheus format
# ---------------------------------------------------------------------------

def export_from_run_metrics(metrics: RunMetrics) -> None:
    """Populate the registry from a completed RunMetrics snapshot."""
    reset_registry()

    _inc("agent_requests_total", float(metrics.total_requests))
    _inc("cache_hits_total", float(metrics.cache_hits))
    _inc("agent_cost_usd_total", metrics.estimated_cost)

    for lat in metrics.latencies_ms:
        _observe('agent_latency_seconds{provider="all"}', lat / 1000.0)

    _set_gauge("agent_availability_ratio", metrics.availability)
    _set_gauge("agent_error_rate_ratio", metrics.error_rate)
    _set_gauge("agent_cache_hit_rate", metrics.cache_hit_rate)
    _set_gauge("agent_circuit_open_total", float(metrics.circuit_open_count))

    if metrics.recovery_time_ms is not None:
        _set_gauge("agent_recovery_time_seconds", metrics.recovery_time_ms / 1000.0)

    # SLO compliance gauges
    checker = SLOChecker(DEFAULT_SLOS)
    results = checker.check(metrics)
    for slo_name, passed in results.items():
        safe_name = slo_name.replace(" ", "_").replace(".", "_").replace("/", "_")
        _set_gauge(f'slo_compliance{{slo="{safe_name}"}}', 1.0 if passed else 0.0)


# ---------------------------------------------------------------------------
# Text format renderer
# ---------------------------------------------------------------------------

_HISTOGRAM_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]


def _histogram_lines(name_with_labels: str, values: list[float]) -> list[str]:
    """Render a histogram in Prometheus text format."""
    # Strip closing brace to inject bucket label
    base = name_with_labels.rstrip("}")
    suffix = "}" if name_with_labels.endswith("}") else ""

    lines: list[str] = []
    total = 0
    sum_v = sum(values)
    for le in _HISTOGRAM_BUCKETS:
        count = sum(1 for v in values if v <= le)
        le_label = f'le="{le}"'
        if base.endswith("{"):
            bucket_key = f"{base}{le_label}{suffix}"
        else:
            bucket_key = f"{base},{le_label}{suffix}"
        lines.append(f"{bucket_key} {count}")
    lines.append(f"{name_with_labels.replace('{', '{').rstrip('}')}_sum{suffix} {sum_v:.6f}")
    lines.append(f"{name_with_labels.rstrip('}')}_count{suffix} {len(values)}")
    return lines


def export_text(metrics: RunMetrics | None = None) -> str:
    """Return Prometheus text exposition format string.

    If *metrics* is provided, the registry is first populated from it.
    """
    if metrics is not None:
        export_from_run_metrics(metrics)

    lines: list[str] = [
        "# HELP agent_requests_total Total requests handled by the gateway",
        "# TYPE agent_requests_total counter",
    ]
    for key, val in sorted(_counters.items()):
        if key.startswith("agent_requests_total"):
            lines.append(f"{key} {val:.0f}")

    lines += [
        "# HELP cache_hits_total Total cache hits",
        "# TYPE cache_hits_total counter",
        f"cache_hits_total {_counters.get('cache_hits_total', 0):.0f}",
        "# HELP agent_cost_usd_total Cumulative cost in USD",
        "# TYPE agent_cost_usd_total counter",
        f"agent_cost_usd_total {_counters.get('agent_cost_usd_total', 0):.6f}",
        "# HELP agent_availability_ratio Current availability ratio (0–1)",
        "# TYPE agent_availability_ratio gauge",
        f"agent_availability_ratio {_gauges.get('agent_availability_ratio', 0):.4f}",
        "# HELP agent_error_rate_ratio Current error rate ratio (0–1)",
        "# TYPE agent_error_rate_ratio gauge",
        f"agent_error_rate_ratio {_gauges.get('agent_error_rate_ratio', 0):.4f}",
        "# HELP agent_cache_hit_rate Cache hit rate (0–1)",
        "# TYPE agent_cache_hit_rate gauge",
        f"agent_cache_hit_rate {_gauges.get('agent_cache_hit_rate', 0):.4f}",
        "# HELP circuit_state Circuit breaker state (0=closed, 1=half_open, 2=open)",
        "# TYPE circuit_state gauge",
    ]
    for key, val in sorted(_gauges.items()):
        if key.startswith("circuit_state"):
            lines.append(f"{key} {val:.0f}")

    lines += [
        "# HELP slo_compliance SLO compliance (1=pass, 0=fail)",
        "# TYPE slo_compliance gauge",
    ]
    for key, val in sorted(_gauges.items()):
        if key.startswith("slo_compliance"):
            lines.append(f"{key} {val:.0f}")

    lines += [
        "# HELP agent_latency_seconds Request latency in seconds",
        "# TYPE agent_latency_seconds histogram",
    ]
    for key, vals in sorted(_histograms.items()):
        if key.startswith("agent_latency_seconds"):
            lines.extend(_histogram_lines(key, vals))

    lines.append("")  # trailing newline
    return "\n".join(lines)


def write_prom_file(metrics: RunMetrics, path: str | Path = "reports/metrics.prom") -> None:
    """Write Prometheus text format to a file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(export_text(metrics), encoding="utf-8")
