"""Chaos runner — executes all configured scenarios and writes reports.

Outputs:
  reports/metrics.json       — structured metrics + SLO compliance table
  reports/metrics.prom       — Prometheus text-format metrics
  reports/slo_report.txt     — human-readable SLO pass/fail table (stdout + file)
"""
from __future__ import annotations

import argparse

from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import load_config
from reliability_lab.prometheus_export import write_prom_file
from reliability_lab.slo import SLOChecker, DEFAULT_SLOS


def main() -> None:
    parser = argparse.ArgumentParser(description="Run chaos scenarios and check SLOs.")
    parser.add_argument("--config", default="configs/default.yaml", help="Path to YAML config")
    parser.add_argument("--out", default="reports/metrics.json", help="Output metrics JSON path")
    parser.add_argument(
        "--prom-out",
        default="reports/metrics.prom",
        help="Output Prometheus text-format path",
    )
    parser.add_argument(
        "--slo-out",
        default="reports/slo_report.txt",
        help="Output SLO report text path",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    metrics = run_simulation(config, load_queries())

    # ------------------------------------------------------------------ #
    # 1. Write metrics.json (includes SLO compliance block)
    # ------------------------------------------------------------------ #
    metrics.write_json(args.out)
    print(f"[chaos] wrote {args.out}")

    # ------------------------------------------------------------------ #
    # 2. Write Prometheus text-format metrics
    # ------------------------------------------------------------------ #
    write_prom_file(metrics, args.prom_out)
    print(f"[chaos] wrote {args.prom_out}")

    # ------------------------------------------------------------------ #
    # 3. Print + write SLO report
    # ------------------------------------------------------------------ #
    checker = SLOChecker(DEFAULT_SLOS)
    slo_summary = checker.summary(metrics)
    print()
    print(slo_summary)

    from pathlib import Path

    Path(args.slo_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.slo_out).write_text(slo_summary, encoding="utf-8")
    print(f"\n[chaos] wrote {args.slo_out}")

    # Exit with non-zero if any SLO violated (useful in CI)
    results = checker.check(metrics)
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
