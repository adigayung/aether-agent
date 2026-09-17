"""Metric dasar untuk Evaluation Framework (#48).

Metric berupa DATA TERSTRUKTUR (bukan penilaian subjektif kualitas model).
Metric dihitung dari EvaluationResult yang sudah ada.

Metric dasar:
    - success/failure
    - expected tool usage
    - unexpected tool usage
    - validation success
    - iteration count
    - recovery count
    - replan count
    - provider error count
    - elapsed time
"""

from __future__ import annotations

from typing import List

from agent_ai.evaluation.models import (
    EvaluationResult,
    EvaluationStatus,
    MetricResult,
)


class MetricsCollector:
    """Menghitung metric terstruktur dari hasil evaluasi.

    Args:
        None. Stateless; semua method menerima data sebagai argumen.
    """

    # ------------------------------------------------------------------ #
    # Per-case metrics
    # ------------------------------------------------------------------ #
    def case_metrics(self, result: EvaluationResult) -> List[MetricResult]:
        """Metric untuk satu case (terstruktur).

        Returns:
            Daftar MetricResult (success, iterations, recovery, replan,
            provider_errors, elapsed, tools_used).
        """
        return [
            MetricResult(
                name="success",
                value=1 if result.success else 0,
                unit="bool",
            ),
            MetricResult(
                name="iterations",
                value=result.iterations,
                unit="count",
            ),
            MetricResult(
                name="recovery_count",
                value=result.recovery_count,
                unit="count",
            ),
            MetricResult(
                name="replan_count",
                value=result.replan_count,
                unit="count",
            ),
            MetricResult(
                name="provider_error_count",
                value=result.provider_error_count,
                unit="count",
            ),
            MetricResult(
                name="elapsed",
                value=result.elapsed,
                unit="seconds",
            ),
            MetricResult(
                name="tools_used",
                value=list(result.tools_used),
                unit="list",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Aggregate metrics
    # ------------------------------------------------------------------ #
    def aggregate(self, results: List[EvaluationResult]) -> List[MetricResult]:
        """Metric aggregate dari sekumpulan result (terstruktur).

        Returns:
            Daftar MetricResult aggregate (total, passed, failed, errors,
            success_rate, expected_tool_usage, unexpected_tool_usage,
            validation_success, total_iterations, total_recovery,
            total_replan, total_provider_errors, total_elapsed).
        """
        total = len(results)
        passed = sum(1 for r in results if r.status == EvaluationStatus.PASSED)
        failed = sum(1 for r in results if r.status == EvaluationStatus.FAILED)
        errors = sum(1 for r in results if r.status == EvaluationStatus.ERROR)

        expected_hits = 0
        expected_total = 0
        unexpected_hits = 0
        validation_ok = 0
        validation_total = 0

        for r in results:
            expected = r.metadata.get("expected_tools") or []
            forbidden = r.metadata.get("forbidden_tools") or []
            used = set(r.tools_used)
            expected_total += len(expected)
            expected_hits += sum(1 for t in expected if t in used)
            unexpected_hits += sum(1 for t in forbidden if t in used)
            if r.validation_success is not None:
                validation_total += 1
                if r.validation_success:
                    validation_ok += 1

        return [
            MetricResult(name="total_cases", value=total, unit="count"),
            MetricResult(name="passed", value=passed, unit="count"),
            MetricResult(name="failed", value=failed, unit="count"),
            MetricResult(name="errors", value=errors, unit="count"),
            MetricResult(
                name="success_rate",
                value=(passed / total) if total else 0.0,
                unit="ratio",
            ),
            MetricResult(
                name="expected_tool_usage",
                value=expected_hits,
                unit="count",
                metadata={"expected_total": expected_total},
            ),
            MetricResult(
                name="unexpected_tool_usage",
                value=unexpected_hits,
                unit="count",
            ),
            MetricResult(
                name="validation_success",
                value=validation_ok,
                unit="count",
                metadata={"validation_total": validation_total},
            ),
            MetricResult(
                name="total_iterations",
                value=sum(r.iterations for r in results),
                unit="count",
            ),
            MetricResult(
                name="total_recovery",
                value=sum(r.recovery_count for r in results),
                unit="count",
            ),
            MetricResult(
                name="total_replan",
                value=sum(r.replan_count for r in results),
                unit="count",
            ),
            MetricResult(
                name="total_provider_errors",
                value=sum(r.provider_error_count for r in results),
                unit="count",
            ),
            MetricResult(
                name="total_elapsed",
                value=sum(r.elapsed for r in results),
                unit="seconds",
            ),
        ]
