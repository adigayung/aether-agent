"""Runner benchmark coding tasks (#49).

Memakai Evaluation Framework #48 (EvaluationRunner). TIDAK membuat AgentRuntime
baru, TIDAK membuat AgentLoop baru, TIDAK membuat engine eksekusi baru.

Runner:
    - menerima target/callable dari luar,
    - membuat fixture temporary TERISOLASI per case (BUKAN project AETHER),
    - menjalankan case via EvaluationRunner (timeout + isolasi kegagalan),
    - membersihkan fixture setelah case selesai,
    - mengembalikan BenchmarkRun terstruktur.

Target adalah callable `(EvaluationInput) -> Any` (lihat #48). Fixture path
diteruskan ke target melalui `EvaluationInput.metadata["fixture_dir"]`.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_ai.benchmark.cases import BenchmarkCaseRegistry
from agent_ai.benchmark.models import (
    BenchmarkCase,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkStatus,
    FixtureSpec,
)
from agent_ai.evaluation import (
    EvaluationCase,
    EvaluationExpected,
    EvaluationInput,
    EvaluationRunner,
    EvaluationStatus,
)

#: Tipe target benchmark: callable `(EvaluationInput) -> Any`.
BenchmarkTarget = Callable[[EvaluationInput], Any]


class BenchmarkRunner:
    """Menjalankan benchmark coding tasks via Evaluation Framework #48.

    Args:
        target: callable `(EvaluationInput) -> Any`. Wajib.
        registry: BenchmarkCaseRegistry opsional (untuk menjalankan by id).
        default_timeout: timeout default per case (detik).
        fixture_root: direktori induk untuk fixture temporary. Default: sistem
            temp dir (terisolasi, BUKAN project AETHER).
    """

    def __init__(
        self,
        target: BenchmarkTarget,
        registry: Optional[BenchmarkCaseRegistry] = None,
        default_timeout: float = 60.0,
        fixture_root: Optional[Path] = None,
    ) -> None:
        if target is None or not callable(target):
            raise ValueError("BenchmarkRunner butuh target callable.")
        self.target = target
        self.registry = registry
        self.default_timeout = default_timeout
        self.fixture_root = Path(fixture_root) if fixture_root else None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def run_case(self, case: BenchmarkCase) -> BenchmarkResult:
        """Jalankan satu benchmark case (fixture terisolasi + cleanup).

        Fixture dibuat di direktori temporary, diteruskan ke target, lalu
        dibersihkan (selalu, termasuk saat error).

        Args:
            case: BenchmarkCase.

        Returns:
            BenchmarkResult (status passed/failed/error).
        """
        fixture_dir = self._materialize_fixture(case.input.fixture)
        try:
            eval_case = self._to_evaluation_case(case, fixture_dir)
            eval_runner = EvaluationRunner(
                target=self.target,
                default_timeout=case.timeout or self.default_timeout,
            )
            eval_result = eval_runner.run_case(eval_case)
            return self._to_benchmark_result(case, eval_result, fixture_dir)
        finally:
            # Fixture SELALU dibersihkan (isolasi + tidak meninggalkan sampah).
            self._cleanup_fixture(fixture_dir)

    def run(
        self,
        cases: Optional[List[BenchmarkCase]] = None,
        *,
        run_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BenchmarkRun:
        """Jalankan sekumpulan benchmark case.

        Args:
            cases: daftar case. Bila None, pakai registry.list().
            run_id: id run (opsional).
            metadata: info tambahan (mis. provider/model/konfigurasi).

        Returns:
            BenchmarkRun (results + metadata).
        """
        if cases is None:
            if self.registry is None:
                raise ValueError("Tidak ada cases dan tidak ada registry.")
            cases = self.registry.list()

        results: List[BenchmarkResult] = []
        for case in cases:
            # Isolasi: satu case gagal tidak menghentikan run.
            results.append(self.run_case(case))

        return BenchmarkRun(run_id=run_id, results=results, metadata=metadata or {})

    def run_ids(
        self,
        case_ids: List[str],
        *,
        run_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BenchmarkRun:
        """Jalankan case berdasarkan id (dari registry)."""
        if self.registry is None:
            raise ValueError("run_ids butuh registry.")
        cases = [self.registry.get(cid) for cid in case_ids]
        return self.run(cases, run_id=run_id, metadata=metadata)

    # ------------------------------------------------------------------ #
    # Fixture handling (temporary & terisolasi)
    # ------------------------------------------------------------------ #
    def _materialize_fixture(self, fixture: FixtureSpec) -> Path:
        """Buat fixture temporary terisolasi dari FixtureSpec.

        Returns:
            Path direktori fixture (temporary).
        """
        base = self.fixture_root
        if base is not None:
            base.mkdir(parents=True, exist_ok=True)
        fixture_dir = Path(tempfile.mkdtemp(prefix="aether_bench_", dir=str(base) if base else None))

        for rel_dir in fixture.directories:
            (fixture_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        for rel_path, content in fixture.files.items():
            target = fixture_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return fixture_dir

    @staticmethod
    def _cleanup_fixture(fixture_dir: Path) -> None:
        """Hapus direktori fixture (recursive, abaikan error)."""
        shutil.rmtree(fixture_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Conversion helpers
    # ------------------------------------------------------------------ #
    def _to_evaluation_case(self, case: BenchmarkCase, fixture_dir: Path) -> EvaluationCase:
        """Konversi BenchmarkCase -> EvaluationCase (#48).

        Fixture path diteruskan via metadata agar target dapat mengaksesnya.
        """
        metadata = dict(case.input.metadata)
        metadata["fixture_dir"] = str(fixture_dir)
        metadata["benchmark_case_id"] = case.id
        metadata["category"] = case.category

        return EvaluationCase(
            id=case.id,
            input=EvaluationInput(task=case.input.task, metadata=metadata),
            expected=EvaluationExpected(
                success=case.expected.success,
                expected_tools=list(case.expected.expected_tools),
                forbidden_tools=list(case.expected.forbidden_tools),
                validation_success=case.expected.validation_success,
                max_iterations=case.expected.max_iterations,
                max_recovery=case.expected.max_recovery,
                max_replan=case.expected.max_replan,
                max_provider_errors=case.expected.max_provider_errors,
                max_elapsed=case.expected.max_elapsed,
                metadata=dict(case.expected.metadata),
            ),
            description=case.description,
            timeout=case.timeout or self.default_timeout,
            metadata={"category": case.category},
        )

    @staticmethod
    def _to_benchmark_result(
        case: BenchmarkCase,
        eval_result: Any,
        fixture_dir: Path,
    ) -> BenchmarkResult:
        """Konversi EvaluationResult (#48) -> BenchmarkResult."""
        status_map = {
            EvaluationStatus.PASSED: BenchmarkStatus.PASSED,
            EvaluationStatus.FAILED: BenchmarkStatus.FAILED,
            EvaluationStatus.ERROR: BenchmarkStatus.ERROR,
        }
        return BenchmarkResult(
            case_id=case.id,
            status=status_map.get(eval_result.status, BenchmarkStatus.ERROR),
            success=eval_result.success,
            error=eval_result.error,
            reasons=list(eval_result.reasons),
            tools_used=list(eval_result.tools_used),
            validation_success=eval_result.validation_success,
            iterations=eval_result.iterations,
            recovery_count=eval_result.recovery_count,
            replan_count=eval_result.replan_count,
            provider_error_count=eval_result.provider_error_count,
            elapsed=eval_result.elapsed,
            category=case.category,
            fixture_dir=str(fixture_dir),
            metadata=dict(eval_result.metadata),
        )
