"""Review capability untuk post-change analysis.

Menyediakan tool `review_changes` yang mengintegrasikan:
    - Git diff/status (dari perspektif repository)
    - ChangeTracker (perubahan selama task)
    - Validation results (hasil test/lint/build)
    - Architecture/impact information (dari Project Map)

Tool ini adalah CAPABILITY, bukan aturan wajib. LLM memutuskan:
    - apakah review diperlukan,
    - apa yang harus direview,
    - kapan task selesai.

Alur yang dituju:
    Edit -> Observe -> Review diff -> Review impact -> Verify -> Continue/Finish

Prinsip:
    - Menggunakan Git dan checker yang sudah ada; tidak membuat sistem baru.
    - Review tersedia secara natural dalam execution flow.
    - Hasil review adalah INFORMASI, bukan keputusan.
    - Tool ini READ-ONLY terhadap workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.base import BaseTool, ToolValidationError


@dataclass
class ReviewResult:
    """Hasil review perubahan.
    
    Attributes:
        git_status: status repository (branch, clean, files).
        git_diff: ringkasan diff per file.
        changed_files: daftar file yang berubah (dari ChangeTracker).
        validation_summary: ringkasan hasil validasi (bila ada).
        impact_hints: petunjuk impact dari Project Map (bila ada).
        recommendations: saran review (opsional).
    """
    git_status: Optional[Dict[str, Any]] = None
    git_diff: Optional[List[Dict[str, Any]]] = None
    changed_files: Optional[List[Dict[str, Any]]] = None
    validation_summary: Optional[Dict[str, Any]] = None
    impact_hints: Optional[Dict[str, Any]] = None
    recommendations: Optional[List[str]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if self.git_status is not None:
            result["git_status"] = self.git_status
        if self.git_diff is not None:
            result["git_diff"] = self.git_diff
        if self.changed_files is not None:
            result["changed_files"] = self.changed_files
        if self.validation_summary is not None:
            result["validation_summary"] = self.validation_summary
        if self.impact_hints is not None:
            result["impact_hints"] = self.impact_hints
        if self.recommendations is not None:
            result["recommendations"] = self.recommendations
        return result


class ReviewChangesTool(BaseTool):
    """Review perubahan workspace setelah edit.
    
    Menggabungkan informasi dari:
        1. Git (status, diff) - perspektif repository
        2. ChangeTracker - perubahan selama task
        3. Validation - hasil test/lint/build terakhir
        4. Project Map - impact analysis (opsional)
    
    Tool ini TIDak membuat keputusan. Ia hanya menyajikan informasi
    untuk LLM menentukan langkah selanjutnya.
    
    Args:
        root: workspace root (boundary).
        change_tracker: ChangeTracker opsional (untuk melihat perubahan task).
        validation_runner: ValidationRunner opsional (untuk menjalankan validasi).
        git_facade: GitRepositoryFacade opsional (untuk Git operations).
        project_map_service: ProjectMapService opsional (untuk impact analysis).
    """
    
    name = "review_changes"
    description = (
        "Review perubahan workspace setelah edit. "
        "Menggabungkan informasi dari Git (status, diff), ChangeTracker (perubahan task), "
        "validasi (test/lint/build), dan Project Map (impact analysis). "
        "Gunakan untuk meninjau hasil edit sebelum memutuskan task selesai. "
        "Tool ini READ-ONLY dan tidak mengubah workspace. "
        "LLM tetap memutuskan apakah review diperlukan dan kapan task selesai."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID untuk mengambil perubahan dari ChangeTracker (opsional).",
            },
            "run_validation": {
                "type": "boolean",
                "description": "Jalankan validasi (test/lint/build) bila tersedia (default: false).",
            },
            "validation_command": {
                "type": "string",
                "description": "Command validasi kustom (opsional, mis. 'pytest tests/').",
            },
            "check_impact": {
                "type": "boolean",
                "description": "Analisis impact via Project Map (default: false).",
            },
            "include_diff": {
                "type": "boolean",
                "description": "Sertakan ringkasan diff per file (default: true).",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter review ke file tertentu (opsional).",
            },
        },
    }
    
    def __init__(
        self,
        root: Optional[Path] = None,
        change_tracker: Optional[Any] = None,
        validation_runner: Optional[Any] = None,
        git_facade: Optional[Any] = None,
        project_map_service: Optional[Any] = None,
    ) -> None:
        from agent_ai.tools.filesystem import _DEFAULT_ROOT
        
        self.root = Path(root) if root else _DEFAULT_ROOT
        self._change_tracker = change_tracker
        self._validation_runner = validation_runner
        self._git_facade = git_facade
        self._project_map_service = project_map_service
    
    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _get_git_facade(self) -> Any:
        """Lazy-init Git facade."""
        if self._git_facade is None:
            from agent_ai.git.repository import GitRepositoryFacade
            self._git_facade = GitRepositoryFacade(root=self.root)
        return self._git_facade
    
    def _get_project_map_service(self) -> Any:
        """Lazy-init Project Map service."""
        if self._project_map_service is None:
            from agent_ai.projects.project_map import ProjectMapService
            self._project_map_service = ProjectMapService()
        return self._project_map_service
    
    def _collect_git_status(self) -> Optional[Dict[str, Any]]:
        """Kumpulkan status Git."""
        try:
            facade = self._get_git_facade()
            if not facade.is_repository():
                return None
            status = facade.status()
            return status.to_dict()
        except Exception:
            return None
    
    def _collect_git_diff(
        self,
        files: Optional[List[str]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Kumpulkan ringkasan diff Git."""
        try:
            facade = self._get_git_facade()
            if not facade.is_repository():
                return None
            diff_summaries = facade.diff()
            result = [d.to_dict() for d in diff_summaries]
            if files:
                file_set = set(files)
                result = [d for d in result if d.get("path") in file_set]
            return result if result else None
        except Exception:
            return None
    
    def _collect_changed_files(
        self,
        task_id: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Kumpulkan perubahan dari ChangeTracker."""
        if not task_id or not self._change_tracker:
            return None
        try:
            change_set = self._change_tracker.get_changes(task_id)
            if not change_set:
                return None
            return [c.to_dict() for c in change_set.changes]
        except Exception:
            return None
    
    def _run_validation(
        self,
        command: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Jalankan validasi dan kembalikan ringkasan."""
        if not self._validation_runner:
            return None
        try:
            from agent_ai.validation.models import ValidationRequest
            
            request = ValidationRequest(
                target="workspace",
                command=command,
            )
            result = self._validation_runner.run(request)
            return {
                "success": result.success,
                "outcome": result.outcome.value if result.outcome else None,
                "exit_code": result.exit_code,
                "duration": result.duration,
                "validator": result.validator,
                "error": result.stderr[:500] if result.stderr else None,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
            }
    
    def _analyze_impact(
        self,
        changed_files: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Analisis impact via Project Map."""
        try:
            service = self._get_project_map_service()
            status = service.get_status(self.root)
            
            # Jika map tidak tersedia, skip
            if status.get("status") in ("missing", "invalid"):
                return None
            
            result: Dict[str, Any] = {
                "map_freshness": status.get("freshness"),
            }
            
            # Jika ada file yang berubah, cek relasi via RIG
            if changed_files:
                from agent_ai.projects.project_map_query import rig_query
                
                impacted: List[Dict[str, Any]] = []
                for file_path in changed_files[:10]:  # Batasi 10 file
                    try:
                        # Query berdasarkan nama file
                        query_result = rig_query(
                            service,
                            self.root,
                            file_path,
                            relation="callers",
                            max_results=5,
                        )
                        if query_result.get("results"):
                            impacted.append({
                                "file": file_path,
                                "callers": [
                                    r.get("name") for r in query_result.get("results", [])
                                ],
                            })
                    except Exception:
                        continue
                
                if impacted:
                    result["potentially_impacted"] = impacted
            
            return result if result else None
        except Exception:
            return None
    
    def _generate_recommendations(
        self,
        git_status: Optional[Dict[str, Any]],
        git_diff: Optional[List[Dict[str, Any]]],
        changed_files: Optional[List[Dict[str, Any]]],
        validation_summary: Optional[Dict[str, Any]],
    ) -> List[str]:
        """Hasilkan rekomendasi berdasarkan hasil review."""
        recommendations: List[str] = []
        
        # Git status
        if git_status and not git_status.get("clean"):
            files = git_status.get("files", [])
            if files:
                recommendations.append(
                    f"Repository memiliki {len(files)} file berubah. "
                    "Pertimbangkan untuk review diff sebelum commit."
                )
        
        # Diff statistics
        if git_diff:
            total_add = sum(d.get("additions", 0) for d in git_diff)
            total_del = sum(d.get("deletions", 0) for d in git_diff)
            if total_add + total_del > 100:
                recommendations.append(
                    f"Perubahan cukup besar: +{total_add} -{total_del} baris. "
                    "Pertimbangkan untuk menjalankan test suite."
                )
        
        # Validation
        if validation_summary and not validation_summary.get("success"):
            recommendations.append(
                "Validasi gagal. Periksa error dan perbaiki sebelum melanjutkan."
            )
        
        # Changed files
        if changed_files and len(changed_files) > 5:
            recommendations.append(
                f"{len(changed_files)} file berubah. "
                "Pertimbangkan untuk review impact pada modul terkait."
            )
        
        return recommendations if recommendations else None
    
    # ------------------------------------------------------------------ #
    # Execute
    # ------------------------------------------------------------------ #
    def execute(self, **arguments: Any) -> Dict[str, Any]:
        """Review perubahan workspace.
        
        Args:
            task_id: Task ID untuk ChangeTracker.
            run_validation: Jalankan validasi (default: false).
            validation_command: Command validasi kustom.
            check_impact: Analisis impact via Project Map (default: false).
            include_diff: Sertakan diff (default: true).
            files: Filter ke file tertentu.
        
        Returns:
            Dict berisi hasil review (git_status, git_diff, changed_files, etc.).
        """
        task_id = arguments.get("task_id")
        run_validation = bool(arguments.get("run_validation", False))
        validation_command = arguments.get("validation_command")
        check_impact = bool(arguments.get("check_impact", False))
        include_diff = bool(arguments.get("include_diff", True))
        files = arguments.get("files")
        
        result = ReviewResult()
        
        # 1. Git status
        result.git_status = self._collect_git_status()
        
        # 2. Git diff
        if include_diff:
            result.git_diff = self._collect_git_diff(files=files)
        
        # 3. Changed files dari ChangeTracker
        result.changed_files = self._collect_changed_files(task_id=task_id)
        
        # 4. Validation
        if run_validation:
            result.validation_summary = self._run_validation(
                command=validation_command,
            )
        
        # 5. Impact analysis
        if check_impact:
            changed_paths = None
            if result.changed_files:
                changed_paths = [c.get("path") for c in result.changed_files]
            elif result.git_diff:
                changed_paths = [d.get("path") for d in result.git_diff]
            
            result.impact_hints = self._analyze_impact(
                changed_files=changed_paths,
            )
        
        # 6. Recommendations
        result.recommendations = self._generate_recommendations(
            git_status=result.git_status,
            git_diff=result.git_diff,
            changed_files=result.changed_files,
            validation_summary=result.validation_summary,
        )
        
        # Final output
        output = result.to_dict()
        output["reviewed"] = True
        output["workspace"] = str(self.root)
        
        return output


# --------------------------------------------------------------------------- #
# Unified Diff Tool (for detailed diff of specific files)
# --------------------------------------------------------------------------- #
class DiffFileTool(BaseTool):
    """Lihat unified diff untuk file tertentu.
    
    Menggunakan Git diff bila tersedia, atau fallback ke internal diff.
    
    Args:
        root: workspace root.
        git_facade: GitRepositoryFacade opsional.
    """
    
    name = "diff_file"
    description = (
        "Lihat unified diff untuk file tertentu. "
        "Menggunakan Git diff bila tersedia, atau fallback ke internal diff. "
        "Gunakan untuk melihat detail perubahan pada file spesifik. "
        "Tool ini READ-ONLY."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path file relatif terhadap root (wajib).",
            },
            "context_lines": {
                "type": "integer",
                "description": "Jumlah baris konteks di sekitar perubahan (default: 3).",
            },
        },
        "required": ["path"],
    }
    
    def __init__(
        self,
        root: Optional[Path] = None,
        git_facade: Optional[Any] = None,
    ) -> None:
        from agent_ai.tools.filesystem import _DEFAULT_ROOT
        
        self.root = Path(root) if root else _DEFAULT_ROOT
        self._git_facade = git_facade
    
    def _get_git_facade(self) -> Any:
        if self._git_facade is None:
            from agent_ai.git.repository import GitRepositoryFacade
            self._git_facade = GitRepositoryFacade(root=self.root)
        return self._git_facade
    
    def _git_diff(self, path: str, context_lines: int = 3) -> Optional[str]:
        """Ambil diff dari Git."""
        try:
            import subprocess
            from agent_ai.git.client import GitCommandError
            
            facade = self._get_git_facade()
            if not facade.is_repository():
                return None
            
            # git diff dengan context lines
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    f"-U{context_lines}",
                    "HEAD",
                    "--",
                    path,
                ],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
            return None
        except Exception:
            return None
    
    def _internal_diff(
        self,
        path: str,
        context_lines: int = 3,
    ) -> Optional[str]:
        """Fallback ke internal diff bila Git tidak tersedia."""
        try:
            from agent_ai.changes.diff import generate
            
            file_path = self.root / path
            if not file_path.exists():
                return f"# file tidak ditemukan: {path}\n"
            
            # Baca konten saat ini
            after = file_path.read_bytes()
            
            # Untuk "before", kita tidak punya snapshot tanpa Git
            # Jadi kita hanya menunjukkan bahwa file ada
            return f"# file saat ini: {path} ({len(after)} bytes)\n"
        except Exception as exc:
            return f"# error membaca file: {exc}\n"
    
    def execute(self, **arguments: Any) -> Dict[str, Any]:
        """Lihat diff file.
        
        Args:
            path: Path file relatif.
            context_lines: Baris konteks (default: 3).
        
        Returns:
            Dict berisi path dan diff.
        """
        path = arguments.get("path")
        if not path:
            raise ToolValidationError("Argumen 'path' wajib.")
        
        context_lines = int(arguments.get("context_lines", 3))
        
        # Coba Git diff dulu
        diff = self._git_diff(path, context_lines)
        source = "git"
        
        # Fallback ke internal diff
        if diff is None:
            diff = self._internal_diff(path, context_lines)
            source = "internal"
        
        return {
            "path": path,
            "diff": diff,
            "source": source,
        }


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_review_tools(
    root: Optional[Path] = None,
    change_tracker: Optional[Any] = None,
    validation_runner: Optional[Any] = None,
    git_facade: Optional[Any] = None,
    project_map_service: Optional[Any] = None,
) -> List[BaseTool]:
    """Bangun daftar tool review.
    
    Args:
        root: workspace root.
        change_tracker: ChangeTracker opsional.
        validation_runner: ValidationRunner opsional.
        git_facade: GitRepositoryFacade opsional.
        project_map_service: ProjectMapService opsional.
    
    Returns:
        List tool: review_changes, diff_file.
    """
    return [
        ReviewChangesTool(
            root=root,
            change_tracker=change_tracker,
            validation_runner=validation_runner,
            git_facade=git_facade,
            project_map_service=project_map_service,
        ),
        DiffFileTool(root=root, git_facade=git_facade),
    ]


__all__ = [
    "ReviewResult",
    "ReviewChangesTool",
    "DiffFileTool",
    "build_review_tools",
]
