"""Report terstruktur untuk Benchmark Coding Tasks (#49).

Meringkas hasil benchmark menjadi report terstruktur: per-case, aggregate,
success rate, tool usage, validation, iterations, recovery/replan, provider
errors, elapsed time. Menyediakan per-case comparison agar nantinya bisa
membandingkan Qwen vs DeepSeek atau konfigurasi lain. TIDAK membuat UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.benchmark.models import BenchmarkRun, BenchmarkStatus


@dataclass
class BenchmarkCaseReport:
    """Report per-case (terstruktur).

    Attributes:
        case_id: id case.
        category: kategori case.
        status: passed/failed/error.
        success: apakah target sukses.
        tools_used: tool yang dipakai.
        validation_success: hasil validation.
        iterations: jumlah iterasi.
        recovery_count: jumlah recovery.
        replan_count: jumlah replan.
        provider_error_count: jumlah provider error.
        elapsed: waktu eksekusi (detik).
        reasons: alasan kegagalan.
        error: pesan error (bila ERROR).
    """

    case_id: str = ""
    category: str = ""
    status: str = ""
    success: bool = False
    tools_used: List[str] = field(default_factory=list)
    validation_success: Optional[bool] = None
    iterations: int = 0
    recovery_count: int = 0
    replan_count: int = 0
    provider_error_count: int = 0
    elapsed: float = 0.0
    reasons: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "status": self.status,
            "success": self.success,
            "tools_used": list(self.tools_used),
            "validation_success": self.validation_success,
            "iterations": self.iterations,
            "recovery_count": self.recovery_count,
            "replan_count": self.replan_count,
            "provider_error_count": self.provider_error_count,
            "elapsed": self.elapsed,
            "reasons": list(self.reasons),
            "error": self.error,
        }


@dataclass
class BenchmarkReport:
    """Report terstruktur dari sebuah BenchmarkRun.

    Attributes:
        run_id: id run.
        total: jumlah case.
        passed: jumlah case passed.
        failed: jumlah case failed.
        errors: jumlah case error.
        success_rate: rasio passed/total.
        cases: report per-case.
        aggregate: metric aggregate sederhana.
        metadata: info tambahan bebas.
    """

    run_id: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    success_rate: float = 0.0
    cases: List[BenchmarkCaseReport] = field(default_factory=list)
    aggregate: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def case(self, case_id: str) -> Optional[BenchmarkCaseReport]:
        """Ambil report per-case berdasarkan id (None bila tidak ada)."""
        for c in self.cases:
            if c.case_id == case_id:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "success_rate": self.success_rate,
            "cases": [c.to_dict() for c in self.cases],
            "aggregate": self.aggregate,
            "metadata": self.metadata,
        }

    def summary(self) -> str:
        """Ringkasan teks singkat (bukan UI)."""
        return (
            f"Benchmark '{self.run_id}': total={self.total} passed={self.passed} "
            f"failed={self.failed} errors={self.errors} "
            f"success_rate={self.success_rate:.2%}"
        )


class BenchmarkReportBuilder:
    """Membangun BenchmarkReport dari BenchmarkRun."""

    @staticmethod
    def build(run: BenchmarkRun) -> BenchmarkReport:
        """Bangun report terstruktur dari run.

        Args:
            run: BenchmarkRun.

        Returns:
            BenchmarkReport (per-case + aggregate).
        """
        cases = [
            BenchmarkCaseReport(
                case_id=r.case_id,
                category=r.category,
                status=r.status.value,
                success=r.success,
                tools_used=list(r.tools_used),
                validation_success=r.validation_success,
                iterations=r.iterations,
                recovery_count=r.recovery_count,
                replan_count=r.replan_count,
                provider_error_count=r.provider_error_count,
                elapsed=r.elapsed,
                reasons=list(r.reasons),
                error=r.error,
            )
            for r in run.results
        ]

        total = run.total
        return BenchmarkReport(
            run_id=run.run_id,
            total=total,
            passed=run.passed,
            failed=run.failed,
            errors=run.errors,
            success_rate=(run.passed / total) if total else 0.0,
            cases=cases,
            aggregate=BenchmarkReportBuilder._aggregate(run),
            metadata=dict(run.metadata),
        )

    @staticmethod
    def _aggregate(run: BenchmarkRun) -> Dict[str, Any]:
        """Metric aggregate sederhana dari run."""
        results = run.results
        total = len(results)
        validation_total = sum(1 for r in results if r.validation_success is not None)
        validation_ok = sum(1 for r in results if r.validation_success is True)

        tool_usage: Dict[str, int] = {}
        for r in results:
            for tool in r.tools_used:
                tool_usage[tool] = tool_usage.get(tool, 0) + 1

        return {
            "total_cases": total,
            "passed": run.passed,
            "failed": run.failed,
            "errors": run.errors,
            "success_rate": (run.passed / total) if total else 0.0,
            "tool_usage": tool_usage,
            "validation_success": validation_ok,
            "validation_total": validation_total,
            "total_iterations": sum(r.iterations for r in results),
            "total_recovery": sum(r.recovery_count for r in results),
            "total_replan": sum(r.replan_count for r in results),
            "total_provider_errors": sum(r.provider_error_count for r in results),
            "total_elapsed": sum(r.elapsed for r in results),
        }

    @staticmethod
    def compare(reports: List[BenchmarkReport]) -> Dict[str, Any]:
        """Bandingkan beberapa report (mis. Qwen vs DeepSeek, konfigurasi lain).

        Returns:
            dict terstruktur berisi per-case comparison + ringkasan per-run.
        """
        # Kumpulkan semua case id (union, urut).
        case_ids: List[str] = []
        for report in reports:
            for c in report.cases:
                if c.case_id not in case_ids:
                    case_ids.append(c.case_id)
        case_ids.sort()

        per_case: Dict[str, Dict[str, Any]] = {}
        for case_id in case_ids:
            per_case[case_id] = {}
            for report in reports:
                c = report.case(case_id)
                per_case[case_id][report.run_id] = c.status if c else None

        runs = [
            {
                "run_id": report.run_id,
                "total": report.total,
                "passed": report.passed,
                "failed": report.failed,
                "errors": report.errors,
                "success_rate": report.success_rate,
                "metadata": report.metadata,
            }
            for report in reports
        ]

        return {"runs": runs, "per_case": per_case}
