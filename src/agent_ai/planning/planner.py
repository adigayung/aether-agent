"""TaskPlanner: mengubah request user menjadi TaskPlan terstruktur.

Provider-agnostic dan deterministik untuk tahap awal. Planner TIDAK
mengeksekusi tool dan TIDAK menjadi AgentLoop baru: ia hanya memahami task
secara struktural dan menghasilkan langkah kerja.

#41: plan lebih informatif (objective, dependencies, prerequisites, expected
outcome) dan memakai repository intelligence/context yang tersedia bila
relevan untuk menentukan step. Tetap deterministic, bounded, provider-agnostic,
tidak menjalankan tool, dan tidak mengubah workspace.

    planner = TaskPlanner()
    plan = planner.create_plan("Perbaiki login yang gagal", context={...})

Interface sengaja dibuat agar nanti bisa memakai LLM (mis. `provider` opsional),
tetapi sekarang TIDAK mewajibkan LLM call.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_ai.planning.models import PlanStep, TaskPlan

# Kata kunci untuk mengklasifikasi jenis task (deterministik, lowercase).
_FIX_KEYWORDS = ("fix", "perbaiki", "bug", "error", "gagal", "broken", "repair")
_ADD_KEYWORDS = ("add", "tambah", "buat", "create", "implement", "new", "feature")
_REFACTOR_KEYWORDS = ("refactor", "bersihkan", "cleanup", "rename", "restructure")
_TEST_KEYWORDS = ("test", "uji", "spec")
_DOC_KEYWORDS = ("doc", "dokumentasi", "readme", "comment")


class TaskPlanner:
    """Planner deterministik untuk membuat TaskPlan.

    Args:
        provider: opsional, disiapkan untuk future LLM planning. Bila None
            (default), planner memakai strategi deterministik berbasis kata
            kunci task. Tidak ada LLM call yang dilakukan sekarang.
    """

    def __init__(self, provider: Optional[Any] = None, max_steps: Optional[int] = None) -> None:
        self.provider = provider
        if max_steps is None:
            try:
                from agent_ai.config.settings import settings

                max_steps = settings.planning.max_plan_steps
            except Exception:  # noqa: BLE001 - config error tidak boleh crash
                max_steps = 12
        self.max_steps = max(int(max_steps), 1)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def create_plan(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> TaskPlan:
        """Buat TaskPlan dari task dan context yang tersedia.

        Args:
            task: request user.
            context: context opsional (mis. hasil ContextBuilder, Code Index,
                Repository Intelligence, Project Brain, task history). Hanya
                dipakai untuk metadata/hint, bukan untuk reasoning panjang.

        Returns:
            TaskPlan dengan langkah-langkah terstruktur.

        Raises:
            ValueError: bila task kosong.
        """
        if not task or not task.strip():
            raise ValueError("Task tidak boleh kosong.")

        context = context or {}
        kind = self._classify(task)
        targets = self._extract_targets(context)
        steps = self._build_steps(kind, task, context, targets)

        # Batasi jumlah step (bounded).
        if len(steps) > self.max_steps:
            steps = steps[: self.max_steps]

        plan = TaskPlan(
            task=task.strip(),
            steps=steps,
            metadata={
                "kind": kind,
                "planner": "deterministic",
                "context_keys": sorted(context.keys()),
                "targets": targets,
                "max_steps": self.max_steps,
            },
        )
        plan.refresh_readiness()
        return plan

    # ------------------------------------------------------------------ #
    # Context targets (repository intelligence / context)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_targets(context: Dict[str, Any]) -> List[str]:
        """Ambil target file relevan dari context (deterministik, bounded).

        Mendukung beberapa bentuk context:
            - {"files": [{"path": ...}, ...]} (ContextResult.to_dict)
            - {"targets": [...]}
        """
        targets: List[str] = []
        raw = context.get("files")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("path"):
                    targets.append(str(item["path"]))
                elif isinstance(item, str):
                    targets.append(item)
        extra = context.get("targets")
        if isinstance(extra, list):
            targets.extend(str(t) for t in extra)
        return sorted(set(targets))

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    @staticmethod
    def _classify(task: str) -> str:
        """Klasifikasi jenis task berdasarkan kata kunci (deterministik)."""
        text = task.lower()
        if any(k in text for k in _FIX_KEYWORDS):
            return "fix"
        if any(k in text for k in _REFACTOR_KEYWORDS):
            return "refactor"
        if any(k in text for k in _ADD_KEYWORDS):
            return "add"
        if any(k in text for k in _TEST_KEYWORDS):
            return "test"
        if any(k in text for k in _DOC_KEYWORDS):
            return "doc"
        return "generic"

    # ------------------------------------------------------------------ #
    # Step building
    # ------------------------------------------------------------------ #
    def _build_steps(
        self,
        kind: str,
        task: str,
        context: Dict[str, Any],
        targets: List[str],
    ) -> List[PlanStep]:
        """Bangun langkah kerja sesuai jenis task."""
        if kind == "fix":
            return self._fix_steps(task, context, targets)
        if kind == "add":
            return self._add_steps(task, context, targets)
        if kind == "refactor":
            return self._refactor_steps(task, context, targets)
        if kind == "test":
            return self._test_steps(task, context, targets)
        if kind == "doc":
            return self._doc_steps(task, context, targets)
        return self._generic_steps(task, context, targets)

    @staticmethod
    def _step(
        title: str,
        description: str,
        objective: str = "",
        expected_outcome: str = "",
        prerequisites: Optional[List[str]] = None,
        **metadata: Any,
    ) -> PlanStep:
        return PlanStep(
            title=title,
            description=description,
            objective=objective,
            expected_outcome=expected_outcome,
            prerequisites=list(prerequisites or []),
            metadata=metadata,
        )

    @staticmethod
    def _chain(steps: List[PlanStep]) -> List[PlanStep]:
        """Hubungkan step secara berurutan (dependency linear deterministik)."""
        for prev, curr in zip(steps, steps[1:]):
            if prev.id not in curr.dependencies:
                curr.dependencies.append(prev.id)
        return steps

    def _fix_steps(self, task: str, context: Dict[str, Any], targets: List[str]) -> List[PlanStep]:
        return self._chain([
            self._step("locate relevant code", "Temukan simbol/file terkait masalah.",
                       objective="Identifikasi lokasi kode bermasalah.",
                       expected_outcome="Daftar file/symbol relevan.",
                       targets=targets),
            self._step("inspect dependencies", "Periksa dependency dan relasi kode.",
                       objective="Pahami dependency kode terkait.",
                       expected_outcome="Dependency/relasi teridentifikasi."),
            self._step("inspect tests", "Periksa test yang relevan.",
                       objective="Temukan test penjaga.",
                       expected_outcome="Test relevan teridentifikasi."),
            self._step("modify code", "Perbaiki kode yang bermasalah.",
                       objective="Perbaiki akar masalah.",
                       expected_outcome="Kode diperbaiki.",
                       prerequisites=["lokasi masalah diketahui"]),
            self._step("run relevant tests", "Jalankan test terkait.",
                       objective="Verifikasi perbaikan.",
                       expected_outcome="Test lulus."),
            self._step("validate result", "Validasi hasil perbaikan.",
                       objective="Pastikan hasil sesuai harapan.",
                       expected_outcome="Hasil tervalidasi."),
        ])

    def _add_steps(self, task: str, context: Dict[str, Any], targets: List[str]) -> List[PlanStep]:
        return self._chain([
            self._step("locate relevant code", "Temukan lokasi penambahan fitur.",
                       objective="Identifikasi titik integrasi fitur.",
                       expected_outcome="Lokasi penambahan diketahui.",
                       targets=targets),
            self._step("inspect dependencies", "Periksa dependency yang dibutuhkan.",
                       objective="Pahami dependency fitur.",
                       expected_outcome="Dependency teridentifikasi."),
            self._step("implement feature", "Implementasikan fitur baru.",
                       objective="Tambahkan fitur.",
                       expected_outcome="Fitur terimplementasi.",
                       prerequisites=["lokasi dan dependency diketahui"]),
            self._step("add tests", "Tambahkan test untuk fitur baru.",
                       objective="Uji fitur baru.",
                       expected_outcome="Test fitur tersedia."),
            self._step("run relevant tests", "Jalankan test terkait.",
                       objective="Verifikasi fitur.",
                       expected_outcome="Test lulus."),
            self._step("validate result", "Validasi hasil.",
                       objective="Pastikan hasil sesuai harapan.",
                       expected_outcome="Hasil tervalidasi."),
        ])

    def _refactor_steps(self, task: str, context: Dict[str, Any], targets: List[str]) -> List[PlanStep]:
        return self._chain([
            self._step("locate relevant code", "Temukan kode yang akan di-refactor.",
                       objective="Identifikasi kode target refactor.",
                       expected_outcome="Kode target diketahui.",
                       targets=targets),
            self._step("inspect dependencies", "Periksa dependency dan pemakai kode.",
                       objective="Pahami pemakai kode.",
                       expected_outcome="Pemakai/dependency teridentifikasi."),
            self._step("inspect tests", "Pastikan test penjaga tersedia.",
                       objective="Pastikan ada test penjaga.",
                       expected_outcome="Test penjaga teridentifikasi."),
            self._step("refactor code", "Lakukan refactor.",
                       objective="Perbaiki struktur tanpa mengubah perilaku.",
                       expected_outcome="Kode ter-refactor.",
                       prerequisites=["pemakai dan test diketahui"]),
            self._step("run relevant tests", "Jalankan test terkait.",
                       objective="Verifikasi refactor.",
                       expected_outcome="Test lulus."),
            self._step("validate result", "Validasi hasil refactor.",
                       objective="Pastikan perilaku tidak berubah.",
                       expected_outcome="Hasil tervalidasi."),
        ])

    def _test_steps(self, task: str, context: Dict[str, Any], targets: List[str]) -> List[PlanStep]:
        return self._chain([
            self._step("locate relevant code", "Temukan kode yang akan diuji.",
                       objective="Identifikasi kode target test.",
                       expected_outcome="Kode target diketahui.",
                       targets=targets),
            self._step("inspect tests", "Periksa test yang ada.",
                       objective="Pahami test yang sudah ada.",
                       expected_outcome="Test existing teridentifikasi."),
            self._step("write tests", "Tulis/perbarui test.",
                       objective="Tambahkan cakupan test.",
                       expected_outcome="Test baru tersedia.",
                       prerequisites=["kode target diketahui"]),
            self._step("run relevant tests", "Jalankan test.",
                       objective="Verifikasi test.",
                       expected_outcome="Test lulus."),
            self._step("validate result", "Validasi hasil test.",
                       objective="Pastikan cakupan memadai.",
                       expected_outcome="Hasil tervalidasi."),
        ])

    def _doc_steps(self, task: str, context: Dict[str, Any], targets: List[str]) -> List[PlanStep]:
        return self._chain([
            self._step("locate relevant code", "Temukan kode yang akan didokumentasikan.",
                       objective="Identifikasi kode target dokumentasi.",
                       expected_outcome="Kode target diketahui.",
                       targets=targets),
            self._step("write documentation", "Tulis/perbarui dokumentasi.",
                       objective="Dokumentasikan kode.",
                       expected_outcome="Dokumentasi tersedia.",
                       prerequisites=["kode target diketahui"]),
            self._step("validate result", "Validasi dokumentasi.",
                       objective="Pastikan dokumentasi akurat.",
                       expected_outcome="Dokumentasi tervalidasi."),
        ])

    def _generic_steps(self, task: str, context: Dict[str, Any], targets: List[str]) -> List[PlanStep]:
        return self._chain([
            self._step("understand task", "Pahami task secara struktural.",
                       objective="Pahami tujuan task.",
                       expected_outcome="Task dipahami."),
            self._step("locate relevant code", "Temukan kode terkait.",
                       objective="Identifikasi kode relevan.",
                       expected_outcome="Kode relevan diketahui.",
                       targets=targets),
            self._step("inspect dependencies", "Periksa dependency terkait.",
                       objective="Pahami dependency.",
                       expected_outcome="Dependency teridentifikasi."),
            self._step("perform changes", "Lakukan perubahan yang diperlukan.",
                       objective="Lakukan perubahan.",
                       expected_outcome="Perubahan dilakukan.",
                       prerequisites=["kode dan dependency diketahui"]),
            self._step("validate result", "Validasi hasil.",
                       objective="Pastikan hasil sesuai harapan.",
                       expected_outcome="Hasil tervalidasi."),
        ])
