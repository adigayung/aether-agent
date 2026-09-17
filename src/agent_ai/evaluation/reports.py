"""Report terstruktur untuk Evaluation Framework (#48).

Meringkas hasil evaluation menjadi report terstruktur: total cases, passed,
failed, errors, dan metric aggregate sederhana. TIDAK membuat UI.

Report dapat dipakai untuk membandingkan konfigurasi (mis. model A vs model B,
routing aktif vs nonaktif) dengan menjalankan run terpisah dan membandingkan
report-nya.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.evaluation.models import EvaluationRun, MetricResult


@dataclass
class EvaluationReport:
    """Report terstruktur dari sebuah EvaluationRun.

    Attributes:
        run_id: id run.
        total: jumlah case.
        passed: jumlah case passed.
        failed: jumlah case failed.
        errors: jumlah case error.
        success_rate: rasio passed/total.
        metrics: metric aggregate (MetricResult).
        failed_cases: id case yang failed.
        error_cases: id case yang error.
        metadata: info tambahan bebas.
    """

    run_id: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    success_rate: float = 0.0
    metrics: List[MetricResult] = field(default_factory=list)
    failed_cases: List[str] = field(default_factory=list)
    error_cases: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def metric(self, name: str) -> Optional[MetricResult]:
        """Ambil metric berdasarkan nama (None bila tidak ada)."""
        for m in self.metrics:
            if m.name == name:
                return m
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "success_rate": self.success_rate,
            "metrics": [m.to_dict() for m in self.metrics],
            "failed_cases": list(self.failed_cases),
            "error_cases": list(self.error_cases),
            "metadata": self.metadata,
        }

    def summary(self) -> str:
        """Ringkasan teks singkat (bukan UI)."""
        return (
            f"Run '{self.run_id}': total={self.total} passed={self.passed} "
            f"failed={self.failed} errors={self.errors} "
            f"success_rate={self.success_rate:.2%}"
        )


class ReportBuilder:
    """Membangun EvaluationReport dari EvaluationRun."""

    @staticmethod
    def build(run: EvaluationRun) -> EvaluationReport:
        """Bangun report terstruktur dari run.

        Args:
            run: EvaluationRun.

        Returns:
            EvaluationReport.
        """
        from agent_ai.evaluation.models import EvaluationStatus

        failed_cases = [r.case_id for r in run.results if r.status == EvaluationStatus.FAILED]
        error_cases = [r.case_id for r in run.results if r.status == EvaluationStatus.ERROR]
        total = run.total

        return EvaluationReport(
            run_id=run.run_id,
            total=total,
            passed=run.passed,
            failed=run.failed,
            errors=run.errors,
            success_rate=(run.passed / total) if total else 0.0,
            metrics=list(run.metrics),
            failed_cases=failed_cases,
            error_cases=error_cases,
            metadata=dict(run.metadata),
        )

    @staticmethod
    def compare(reports: List[EvaluationReport]) -> Dict[str, Any]:
        """Bandingkan beberapa report (mis. model A vs model B).

        Returns:
            dict terstruktur berisi perbandingan success_rate + metric kunci.
        """
        comparison: Dict[str, Any] = {"runs": []}
        for report in reports:
            comparison["runs"].append(
                {
                    "run_id": report.run_id,
                    "total": report.total,
                    "passed": report.passed,
                    "failed": report.failed,
                    "errors": report.errors,
                    "success_rate": report.success_rate,
                    "metadata": report.metadata,
                }
            )
        return comparison
