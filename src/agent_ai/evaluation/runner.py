"""Runner evaluation (#48).

Menjalankan evaluation case melalui callable/interface yang DIBERIKAN pemanggil.
TIDAK membuat AgentRuntime baru, TIDAK membuat AgentLoop baru, TIDAK membuat
engine eksekusi baru. Runner hanya mengorkestrasi: panggil target, ukur waktu,
isolasi kegagalan, dan bangun EvaluationResult terstruktur.

Target adalah callable `(EvaluationInput) -> Any` yang mengembalikan salah satu:
    - EvaluationResult (dipakai apa adanya, case_id diisi bila kosong),
    - dict dengan key yang dikenali (success, tools_used, iterations, dll),
    - objek dengan atribut serupa (mis. RuntimeResult).

Timeout dan bounded execution didukung. Satu case gagal TIDAK menghentikan
seluruh evaluation run.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Callable, Dict, List, Optional

from agent_ai.evaluation.cases import EvaluationCaseRegistry
from agent_ai.evaluation.metrics import MetricsCollector
from agent_ai.evaluation.models import (
    EvaluationCase,
    EvaluationInput,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
)

#: Tipe target: callable yang menerima EvaluationInput.
EvaluationTarget = Callable[[EvaluationInput], Any]


class EvaluationRunner:
    """Menjalankan evaluation case melalui target callable.

    Args:
        target: callable `(EvaluationInput) -> Any`. Wajib.
        registry: EvaluationCaseRegistry opsional (untuk menjalankan by id).
        metrics: MetricsCollector opsional.
        default_timeout: timeout default per case (detik).
    """

    def __init__(
        self,
        target: EvaluationTarget,
        registry: Optional[EvaluationCaseRegistry] = None,
        metrics: Optional[MetricsCollector] = None,
        default_timeout: float = 30.0,
    ) -> None:
        if target is None or not callable(target):
            raise ValueError("EvaluationRunner butuh target callable.")
        self.target = target
        self.registry = registry
        self.metrics = metrics or MetricsCollector()
        self.default_timeout = default_timeout

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def run_case(self, case: EvaluationCase) -> EvaluationResult:
        """Jalankan satu case (timeout + isolasi kegagalan).

        Satu case gagal/error TIDAK melempar exception ke pemanggil; selalu
        mengembalikan EvaluationResult terstruktur.

        Args:
            case: EvaluationCase.

        Returns:
            EvaluationResult (status passed/failed/error).
        """
        timeout = case.timeout or self.default_timeout
        start = time.perf_counter()

        try:
            raw = self._call_with_timeout(case.input, timeout)
        except FuturesTimeout:
            elapsed = time.perf_counter() - start
            return EvaluationResult(
                case_id=case.id,
                status=EvaluationStatus.ERROR,
                success=False,
                error=f"Timeout setelah {timeout}s.",
                reasons=["timeout"],
                elapsed=elapsed,
                metadata=self._expected_metadata(case),
            )
        except Exception as exc:  # noqa: BLE001 - isolasi kegagalan case
            elapsed = time.perf_counter() - start
            return EvaluationResult(
                case_id=case.id,
                status=EvaluationStatus.ERROR,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                reasons=["exception"],
                elapsed=elapsed,
                metadata=self._expected_metadata(case),
            )

        elapsed = time.perf_counter() - start
        result = self._normalize(raw, case.id, elapsed)
        result.metadata.update(self._expected_metadata(case))
        return self._evaluate(case, result)

    def run(
        self,
        cases: Optional[List[EvaluationCase]] = None,
        *,
        run_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EvaluationRun:
        """Jalankan sekumpulan case dan bangun EvaluationRun.

        Args:
            cases: daftar case. Bila None, pakai registry.list().
            run_id: id run (opsional).
            metadata: info tambahan (mis. provider/model yang diuji).

        Returns:
            EvaluationRun (results + metric aggregate).
        """
        if cases is None:
            if self.registry is None:
                raise ValueError("Tidak ada cases dan tidak ada registry.")
            cases = self.registry.list()

        results: List[EvaluationResult] = []
        for case in cases:
            # Isolasi: satu case gagal tidak menghentikan run.
            results.append(self.run_case(case))

        return EvaluationRun(
            run_id=run_id,
            results=results,
            metrics=self.metrics.aggregate(results),
            metadata=metadata or {},
        )

    def run_ids(self, case_ids: List[str], *, run_id: str = "", metadata: Optional[Dict[str, Any]] = None) -> EvaluationRun:
        """Jalankan case berdasarkan id (dari registry)."""
        if self.registry is None:
            raise ValueError("run_ids butuh registry.")
        cases = [self.registry.get(cid) for cid in case_ids]
        return self.run(cases, run_id=run_id, metadata=metadata)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _call_with_timeout(self, eval_input: EvaluationInput, timeout: float) -> Any:
        """Panggil target dengan timeout (bounded execution).

        Memakai ThreadPoolExecutor agar tidak bergantung pada signal (portable).
        """
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.target, eval_input)
            return future.result(timeout=timeout)

    def _normalize(self, raw: Any, case_id: str, elapsed: float) -> EvaluationResult:
        """Normalisasi hasil target menjadi EvaluationResult.

        Menerima EvaluationResult, dict, atau objek dengan atribut serupa.
        """
        if isinstance(raw, EvaluationResult):
            result = raw
            if not result.case_id:
                result.case_id = case_id
            if not result.elapsed:
                result.elapsed = elapsed
            return result

        if isinstance(raw, dict):
            return EvaluationResult(
                case_id=case_id,
                success=bool(raw.get("success", False)),
                error=raw.get("error"),
                tools_used=list(raw.get("tools_used", []) or []),
                validation_success=raw.get("validation_success"),
                iterations=int(raw.get("iterations", 0) or 0),
                recovery_count=int(raw.get("recovery_count", 0) or 0),
                replan_count=int(raw.get("replan_count", 0) or 0),
                provider_error_count=int(raw.get("provider_error_count", 0) or 0),
                elapsed=elapsed,
                metrics=dict(raw.get("metrics", {}) or {}),
                metadata=dict(raw.get("metadata", {}) or {}),
            )

        # Objek dengan atribut (mis. RuntimeResult).
        return EvaluationResult(
            case_id=case_id,
            success=bool(getattr(raw, "success", False)),
            error=getattr(raw, "error", None),
            tools_used=list(getattr(raw, "tools_used", []) or []),
            validation_success=getattr(raw, "validation_success", None),
            iterations=int(getattr(raw, "iterations", 0) or 0),
            recovery_count=int(getattr(raw, "recovery_count", 0) or 0),
            replan_count=int(getattr(raw, "replan_count", 0) or 0),
            provider_error_count=int(getattr(raw, "provider_error_count", 0) or 0),
            elapsed=elapsed,
            metadata=dict(getattr(raw, "metadata", {}) or {}),
        )

    def _evaluate(self, case: EvaluationCase, result: EvaluationResult) -> EvaluationResult:
        """Bandingkan result dengan ekspektasi case -> status passed/failed.

        Hanya mengevaluasi aspek yang ditentukan di expected (None = skip).
        """
        expected = case.expected
        reasons: List[str] = []

        if expected.success is not None and result.success != expected.success:
            reasons.append(f"success={result.success}, diharapkan {expected.success}")

        used = set(result.tools_used)
        for tool in expected.expected_tools:
            if tool not in used:
                reasons.append(f"tool '{tool}' tidak dipakai")
        for tool in expected.forbidden_tools:
            if tool in used:
                reasons.append(f"tool '{tool}' tidak boleh dipakai")

        if expected.validation_success is not None and result.validation_success != expected.validation_success:
            reasons.append(
                f"validation_success={result.validation_success}, diharapkan {expected.validation_success}"
            )

        if expected.max_iterations is not None and result.iterations > expected.max_iterations:
            reasons.append(f"iterations={result.iterations} > {expected.max_iterations}")
        if expected.max_recovery is not None and result.recovery_count > expected.max_recovery:
            reasons.append(f"recovery={result.recovery_count} > {expected.max_recovery}")
        if expected.max_replan is not None and result.replan_count > expected.max_replan:
            reasons.append(f"replan={result.replan_count} > {expected.max_replan}")
        if expected.max_provider_errors is not None and result.provider_error_count > expected.max_provider_errors:
            reasons.append(f"provider_errors={result.provider_error_count} > {expected.max_provider_errors}")
        if expected.max_elapsed is not None and result.elapsed > expected.max_elapsed:
            reasons.append(f"elapsed={result.elapsed:.3f} > {expected.max_elapsed}")

        result.reasons = reasons
        result.status = EvaluationStatus.PASSED if not reasons else EvaluationStatus.FAILED
        return result

    @staticmethod
    def _expected_metadata(case: EvaluationCase) -> Dict[str, Any]:
        """Metadata ekspektasi untuk metric aggregate (expected/forbidden tools)."""
        return {
            "expected_tools": list(case.expected.expected_tools),
            "forbidden_tools": list(case.expected.forbidden_tools),
        }
