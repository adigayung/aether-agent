"""Model untuk Evaluation Framework (#48).

Provider/model agnostic. Framework evaluasi internal untuk mengukur perilaku
agent secara repeatable. Model di sini hanya data terstruktur untuk case,
input, expected, result, run, dan metric.

    EvaluationStatus  -> status terstruktur (passed/failed/error)
    EvaluationInput   -> input untuk satu case (task + metadata)
    EvaluationExpected-> ekspektasi terstruktur (tools, validation, dll)
    EvaluationCase    -> definisi case (id + input + expected)
    EvaluationResult  -> hasil menjalankan satu case (status + metrics)
    EvaluationRun     -> hasil satu run (kumpulan result + report)
    MetricResult      -> satu metric terstruktur (nama + nilai)

Tidak menyimpan chain-of-thought. Tidak ada penilaian subjektif kualitas model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EvaluationStatus(str, Enum):
    """Status terstruktur hasil evaluasi satu case."""

    PASSED = "passed"   # case memenuhi ekspektasi
    FAILED = "failed"   # case tidak memenuhi ekspektasi (bukan error)
    ERROR = "error"     # case gagal dijalankan (exception/timeout)


@dataclass
class EvaluationInput:
    """Input untuk satu evaluation case.

    Attributes:
        task: teks task yang diberikan ke target.
        metadata: info tambahan bebas (mis. konteks, provider, model).
    """

    task: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"task": self.task, "metadata": self.metadata}


@dataclass
class EvaluationExpected:
    """Ekspektasi terstruktur untuk sebuah case.

    Semua field opsional. Bila None, aspek tersebut tidak dievaluasi.

    Attributes:
        success: apakah target diharapkan sukses.
        expected_tools: tool yang diharapkan dipakai (subset).
        forbidden_tools: tool yang TIDAK boleh dipakai.
        validation_success: apakah validation diharapkan sukses.
        max_iterations: batas iterasi maksimum yang diharapkan.
        max_recovery: batas recovery maksimum yang diharapkan.
        max_replan: batas replan maksimum yang diharapkan.
        max_provider_errors: batas provider error maksimum yang diharapkan.
        max_elapsed: batas waktu (detik) yang diharapkan.
        metadata: info tambahan bebas.
    """

    success: Optional[bool] = None
    expected_tools: List[str] = field(default_factory=list)
    forbidden_tools: List[str] = field(default_factory=list)
    validation_success: Optional[bool] = None
    max_iterations: Optional[int] = None
    max_recovery: Optional[int] = None
    max_replan: Optional[int] = None
    max_provider_errors: Optional[int] = None
    max_elapsed: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "expected_tools": list(self.expected_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "validation_success": self.validation_success,
            "max_iterations": self.max_iterations,
            "max_recovery": self.max_recovery,
            "max_replan": self.max_replan,
            "max_provider_errors": self.max_provider_errors,
            "max_elapsed": self.max_elapsed,
            "metadata": self.metadata,
        }


@dataclass
class EvaluationCase:
    """Definisi satu evaluation case.

    Attributes:
        id: id unik case.
        input: EvaluationInput.
        expected: EvaluationExpected.
        description: deskripsi singkat.
        timeout: batas waktu menjalankan case (detik).
        metadata: info tambahan bebas.
    """

    id: str = ""
    input: EvaluationInput = field(default_factory=EvaluationInput)
    expected: EvaluationExpected = field(default_factory=EvaluationExpected)
    description: str = ""
    timeout: float = 30.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input.to_dict(),
            "expected": self.expected.to_dict(),
            "description": self.description,
            "timeout": self.timeout,
            "metadata": self.metadata,
        }


@dataclass
class EvaluationResult:
    """Hasil menjalankan satu case (terstruktur).

    Attributes:
        case_id: id case.
        status: passed/failed/error.
        success: apakah target melaporkan sukses.
        error: pesan error (bila status ERROR).
        reasons: alasan singkat kegagalan (bukan chain-of-thought).
        tools_used: tool yang dipakai target.
        validation_success: hasil validation (bila ada).
        iterations: jumlah iterasi.
        recovery_count: jumlah recovery.
        replan_count: jumlah replan.
        provider_error_count: jumlah provider error.
        elapsed: waktu eksekusi (detik).
        metrics: metric terstruktur tambahan.
        metadata: info tambahan bebas.
    """

    case_id: str = ""
    status: EvaluationStatus = EvaluationStatus.ERROR
    success: bool = False
    error: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    validation_success: Optional[bool] = None
    iterations: int = 0
    recovery_count: int = 0
    replan_count: int = 0
    provider_error_count: int = 0
    elapsed: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == EvaluationStatus.PASSED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "success": self.success,
            "error": self.error,
            "reasons": self.reasons,
            "tools_used": list(self.tools_used),
            "validation_success": self.validation_success,
            "iterations": self.iterations,
            "recovery_count": self.recovery_count,
            "replan_count": self.replan_count,
            "provider_error_count": self.provider_error_count,
            "elapsed": self.elapsed,
            "metrics": self.metrics,
            "metadata": self.metadata,
        }


@dataclass
class MetricResult:
    """Satu metric terstruktur (nama + nilai).

    Attributes:
        name: nama metric.
        value: nilai metric (numerik atau terstruktur).
        unit: satuan (mis. "count", "seconds", "ratio").
        metadata: info tambahan bebas.
    """

    name: str = ""
    value: Any = 0
    unit: str = "count"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "metadata": self.metadata,
        }


@dataclass
class EvaluationRun:
    """Hasil satu evaluation run (kumpulan result + report).

    Attributes:
        run_id: id run.
        results: daftar EvaluationResult.
        metrics: metric aggregate (MetricResult).
        metadata: info tambahan bebas (mis. provider/model yang diuji).
    """

    run_id: str = ""
    results: List[EvaluationResult] = field(default_factory=list)
    metrics: List[MetricResult] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.FAILED)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.ERROR)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "results": [r.to_dict() for r in self.results],
            "metrics": [m.to_dict() for m in self.metrics],
            "metadata": self.metadata,
        }
