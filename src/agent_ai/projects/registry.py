"""Project Registry.

Mendaftarkan banyak project di workspace Agent-Ai. Semua data project
(project.json + intelligence) disimpan DI BAWAH workspace Agent-Ai
(mis. J:\\Agent_Ai\\projects\\<id>\\), TIDAK PERNAH di root project target.

    from agent_ai.projects import ProjectRegistry

    reg = ProjectRegistry()  # default: <workspace>/projects
    project = reg.register(name="MyApp", root="J:\\MyApp")
    intel = reg.intelligence(project.id)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from agent_ai.projects.intelligence import ProjectIntelligence
from agent_ai.projects.models import ProjectConfig, _now_iso


class ProjectError(Exception):
    """Base error untuk project registry."""


class ProjectNotFoundError(ProjectError):
    """Project tidak terdaftar."""


class ProjectRootNotFoundError(ProjectError):
    """Root project target tidak ada."""


class ProjectRegistry:
    """Registry project dengan storage JSON sederhana.

    Args:
        workspace: folder workspace Agent-Ai. Default: <repo>/projects
            (yaitu J:\\Agent_Ai\\projects).
    """

    def __init__(self, workspace: Optional[Path] = None) -> None:
        if workspace is None:
            # src/agent_ai/projects/registry.py -> parents[3] = root repo (J:\Agent_Ai)
            workspace = Path(__file__).resolve().parents[3] / "projects"
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #
    def project_dir(self, project_id: str) -> Path:
        """Direktori project di bawah workspace Agent-Ai."""
        return self.workspace / project_id

    def _project_json(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    # ------------------------------------------------------------------ #
    # Register / load
    # ------------------------------------------------------------------ #
    def register(
        self,
        name: str,
        root: str,
        permission_mode: str = "workspace",
    ) -> ProjectConfig:
        """Daftarkan project baru (name + absolute root path).

        Raises:
            ProjectRootNotFoundError: bila root project target tidak ada.
        """
        root_path = Path(root)
        if not root_path.exists() or not root_path.is_dir():
            raise ProjectRootNotFoundError(f"Root project tidak ditemukan: {root}")

        config = ProjectConfig(
            name=name,
            root=str(root_path.resolve()),
            permission_mode=permission_mode,
        )

        project_dir = self.project_dir(config.id)
        project_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self._project_json(config.id), config.to_dict())

        # Buat struktur intelligence di bawah workspace Agent-Ai.
        ProjectIntelligence(project_dir).create()
        return config

    def load(self, project_id: str) -> ProjectConfig:
        """Muat project berdasarkan id.

        Raises:
            ProjectNotFoundError: bila project.json tidak ada.
        """
        path = self._project_json(project_id)
        if not path.exists():
            raise ProjectNotFoundError(f"Project '{project_id}' tidak terdaftar.")
        return ProjectConfig.from_dict(self._read_json(path))

    def find_by_name(self, name: str) -> Optional[ProjectConfig]:
        """Cari project berdasarkan name (None bila tidak ada)."""
        for config in self.list():
            if config.name == name:
                return config
        return None

    def get(self, id_or_name: str) -> ProjectConfig:
        """Ambil project berdasarkan id atau name.

        Raises:
            ProjectNotFoundError: bila tidak ditemukan.
        """
        try:
            return self.load(id_or_name)
        except ProjectNotFoundError:
            found = self.find_by_name(id_or_name)
            if found is None:
                raise ProjectNotFoundError(
                    f"Project '{id_or_name}' tidak ditemukan (id/name)."
                )
            return found

    def list(self) -> List[ProjectConfig]:
        """Daftar semua project terdaftar."""
        projects: List[ProjectConfig] = []
        for child in sorted(self.workspace.iterdir()):
            if child.is_dir() and (child / "project.json").exists():
                projects.append(ProjectConfig.from_dict(self._read_json(child / "project.json")))
        return projects

    def intelligence(self, project_id: str) -> ProjectIntelligence:
        """Akses ProjectIntelligence untuk sebuah project."""
        # Pastikan project terdaftar sebelum mengakses intelligence.
        self.load(project_id)
        return ProjectIntelligence(self.project_dir(project_id))

    def touch(self, project_id: str) -> ProjectConfig:
        """Update `updated_at` project."""
        config = self.load(project_id)
        config.updated_at = _now_iso()
        self._write_json(self._project_json(project_id), config.to_dict())
        return config

    # ------------------------------------------------------------------ #
    # JSON helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_json(path: Path) -> Dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectError(f"Gagal membaca '{path}': {exc}") from exc

    @staticmethod
    def _write_json(path: Path, data: Dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
