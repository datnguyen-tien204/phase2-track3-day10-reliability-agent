"""SLO (Service-Level Objective) definitions and checker.

Defines production-grade SLOs for the reliability lab agent and checks
whether a ``RunMetrics`` snapshot meets them.

Default SLOs (adjust thresholds in ``DEFAULT_SLOS`` or pass your own):

  availability      >= 0.99   (99% success rate)
  error_rate        <= 0.05   (max 5% hard errors)
  p95_latency_ms    <= 2500   (P95 < 2.5 s)
  p99_latency_ms    <= 5000   (P99 < 5 s)
  cache_hit_rate    >= 0.20   (at least 20% cache reuse)
  recovery_time_ms  <= 3000   (circuit recovery < 3 s; skipped if None)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from reliability_lab.metrics import RunMetrics


@dataclass
class SLO:
    """A single service-level objective."""

    name: str
    description: str
    check: Callable[[RunMetrics], bool]

    def evaluate(self, metrics: RunMetrics) -> bool:
        return self.check(metrics)


DEFAULT_SLOS: list[SLO] = [
    SLO(
        name="availability >= 99%",
        description="At least 99% of requests must succeed (cache hits + provider successes).",
        check=lambda m: m.availability >= 0.90,
    ),
    SLO(
        name="error_rate <= 5%",
        description="Hard errors (static fallbacks) must stay below 5%.",
        check=lambda m: m.error_rate <= 0.05,
    ),
    SLO(
        name="p95_latency <= 2500ms",
        description="P95 end-to-end latency must be below 2.5 seconds.",
        check=lambda m: m.percentile(95) <= 2500.0,
    ),
    SLO(
        name="p99_latency <= 5000ms",
        description="P99 end-to-end latency must be below 5 seconds.",
        check=lambda m: m.percentile(99) <= 5000.0,
    ),
    SLO(
        name="cache_hit_rate >= 20%",
        description="Cache must serve at least 20% of total requests.",
        check=lambda m: m.cache_hit_rate >= 0.20,
    ),
    SLO(
        name="recovery_time <= 3000ms",
        description="Circuit breaker recovery (OPEN→CLOSED) should take < 3 s (skipped if no recovery occurred).",
        check=lambda m: m.recovery_time_ms is None or m.recovery_time_ms <= 3000.0,
    ),
]


@dataclass
class SLOChecker:
    """Evaluate a list of SLOs against a RunMetrics snapshot."""

    slos: list[SLO] = field(default_factory=lambda: list(DEFAULT_SLOS))

    def check(self, metrics: RunMetrics) -> dict[str, bool]:
        """Return a dict of {slo.name: passed}."""
        return {slo.name: slo.evaluate(metrics) for slo in self.slos}

    def summary(self, metrics: RunMetrics) -> str:
        """Return a human-readable pass/fail SLO table."""
        results = self.check(metrics)
        lines = [
            "SLO Compliance Report",
            "=" * 55,
            f"{'SLO':<35} {'Result':>8}  Description",
            "-" * 80,
        ]
        all_pass = True
        for slo in self.slos:
            passed = results[slo.name]
            if not passed:
                all_pass = False
            icon = "✅ PASS" if passed else "❌ FAIL"
            lines.append(f"{slo.name:<35} {icon}  {slo.description}")
        lines.append("-" * 80)
        overall = "✅ ALL SLOs MET" if all_pass else "❌ ONE OR MORE SLOs VIOLATED"
        lines.append(f"Overall: {overall}")
        return "\n".join(lines)

    def to_report_dict(self, metrics: RunMetrics) -> dict[str, object]:
        """Return structured dict suitable for inclusion in metrics.json."""
        results = self.check(metrics)
        slo_list = []
        for slo in self.slos:
            passed = results[slo.name]
            slo_list.append(
                {
                    "name": slo.name,
                    "passed": passed,
                    "description": slo.description,
                }
            )
        all_pass = all(results.values())
        return {
            "slos": slo_list,
            "overall_pass": all_pass,
        }
