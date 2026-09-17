"""Model untuk Benchmark Coding Tasks (#49).

Provider/model agnostic. Benchmark adalah ALAT UKUR AETHER (memakai Evaluation
Framework #48), bukan bagian dari cara AETHER berpikir. Model di sini hanya
data terstruktur untuk case, input, expected, result, dan run.

    BenchmarkStatus   -> status terstruktur (passed/failed/error)
    FixtureSpec       -> spesifikasi fixture temporary (file -> konten)
    BenchmarkInput    -> input benchmark (task + fixture + metadata)
    BenchmarkExpected -> ekspektasi terstruktur (outcome + tools + batas)
    BenchmarkCase     -> definisi satu task coding
    BenchmarkResult   -> hasil menjalankan satu case
    BenchmarkRun      -> hasil satu run (kumpulan result + metadata)

Tidak menyimpan chain-of-thought. Tidak ada penilaian subjektif / basis data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BenchmarkStatus(str, Enum):
    """Status terstruktur hasil benchmark satu case."""

    PASSED = "passed"   # case memenuhi ekspektasi
    FAILED = "failed"   # case tidak memenuhi ekspektasi (bukan error)
    ERROR = "error"     # case gagal dijalankan (exception/timeout)


@dataclass
class FixtureSpec:
    """Spesifikasi fixture temporary untuk sebuah benchmark case.

    Fixture dibuat di direktori temporary terisolasi (BUKAN project AETHER),
    lalu dibersihkan setelah case selesai.

    Attributes:
        files: pemetaan path relatif -> konten file.
        directories: daftar direktori kosong yang perlu dibuat.
        metadata: info tambahan bebas.
    """

    files: Dict[str, str] = field(default_factory=dict)
    directories: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": dict(self.files),
            "directories": list(self.directories),
            "metadata": self.metadata,
        }


@dataclass
class BenchmarkInput:
    """Input untuk satu benchmark case.

    Attributes:
        task: deskripsi task coding yang diberikan ke target.
        fixture: FixtureSpec (fixture temporary terisolasi).
        metadata: info tambahan bebas (mis. bahasa, entrypoint).
    """

    task: str = ""
    fixture: FixtureSpec = field(default_factory=FixtureSpec)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "fixture": self.fixture.to_dict(),
            "metadata": self.metadata,
        }


@dataclass
class BenchmarkExpected:
    """Ekspektasi terstruktur untuk sebuah benchmark case.

    Semua field opsional. Bila None, aspek tersebut tidak dievaluasi.

    Attributes:
        success: apakah target diharapkan sukses.
        expected_tools: tool yang diharapkan dipakai (subset).
        forbidden_tools: tool yang TIDAK boleh dipakai.
        validation_success: apakah validation diharapkan sukses.
        max_iterations: batas iterasi maksimum.
        max_recovery: batas recovery maksimum.
        max_replan: batas replan maksimum.
        max_provider_errors: batas provider error maksimum.
        max_elapsed: batas waktu (detik) maksimum.
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
class BenchmarkCase:
    """Definisi satu task coding untuk benchmark.

    Attributes:
        id: id unik case.
        input: BenchmarkInput (task + fixture).
        expected: BenchmarkExpected.
        description: deskripsi singkat.
        category: kategori task (mis. "function", "bugfix", "multi_file",
            "terminal_validation", "recovery").
        timeout: batas waktu menjalankan case (detik).
        metadata: info tambahan bebas.
    """

    id: str = ""
    input: BenchmarkInput = field(default_factory=BenchmarkInput)
    expected: BenchmarkExpected = field(default_factory=BenchmarkExpected)
    description: str = ""
    category: str = ""
    timeout: float = 60.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input.to_dict(),
            "expected": self.expected.to_dict(),
            "description": self.description,
            "category": self.category,
            "timeout": self.timeout,
            "metadata": self.metadata,
        }


@dataclass
class BenchmarkResult:
    """Hasil menjalankan satu benchmark case (terstruktur).

    Attributes:
        case_id: id case.
        status: passed/failed/error.
        success: apakah target melaporkan sukses.
        error: pesan error (bila status ERROR).
        reasons: alasan singkat kegagalan.
        tools_used: tool yang dipakai target.
        validation_success: hasil validation (bila ada).
        iterations: jumlah iterasi.
        recovery_count: jumlah recovery.
        replan_count: jumlah replan.
        provider_error_count: jumlah provider error.
        elapsed: waktu eksekusi (detik).
        category: kategori case.
        fixture_dir: direktori fixture (untuk audit; sudah dibersihkan).
        metadata: info tambahan bebas.
    """

    case_id: str = ""
    status: BenchmarkStatus = BenchmarkStatus.ERROR
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
    category: str = ""
    fixture_dir: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == BenchmarkStatus.PASSED

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
            "category": self.category,
            "fixture_dir": self.fixture_dir,
            "metadata": self.metadata,
        }


@dataclass
class BenchmarkRun:
    """Hasil satu benchmark run (kumpulan result + metadata).

    Attributes:
        run_id: id run.
        results: daftar BenchmarkResult.
        metadata: info tambahan bebas (mis. provider/model/konfigurasi).
    """

    run_id: str = ""
    results: List[BenchmarkResult] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == BenchmarkStatus.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == BenchmarkStatus.FAILED)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.status == BenchmarkStatus.ERROR)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "results": [r.to_dict() for r in self.results],
            "metadata": self.metadata,
        }
