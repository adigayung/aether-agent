"""Project Settings per-proyek (terpusat di `<root>/.aether/settings/`).

Modul ini adalah *persistence project-local* untuk konfigurasi yang melekat
pada SATU project (bukan Global Settings AETHER):

    <root project target>/
        .aether/
            settings/
                permissions.json   (Project Permission Matrix)
                agent.json         (System Prompt Agent)
                consultant.json    (System Prompt Consultant)

Prinsip:
    - Project-local: konfigurasi project A TIDAK boleh tercampur project B.
    - Satu mekanisme penyimpanan: JSON kecil + atomic write (tanpa DB kedua).
    - Aman untuk project LAMA: file yang belum ada -> default aman (default
      prompt AETHER existing) TANPA membuat file dan TANPA mengubah project.
    - TIDAK ada sistem prompt kedua: bila project tidak punya override, runtime
      tetap memakai sumber existing (Global Settings / prompt bawaan AETHER).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

from agent_ai.projects.permissions import (
    AETHER_DIR_NAME,
    SETTINGS_DIR_NAME,
    _atomic_write_bytes,
)

#: Nama file konfigurasi per-proyek (di `.aether/settings/`).
AGENT_FILE_NAME = "agent.json"
CONSULTANT_FILE_NAME = "consultant.json"

#: Batas maksimum panjang System Prompt per-proyek (konsisten dengan Global
#: Settings: mencegah penulisan nilai tak terbatas).
MAX_PROJECT_SYSTEM_PROMPT_CHARS = 200_000

#: Nilai mode Consultant yang valid (konsisten dengan consultant.models).
CONSULTANT_MODES = ("quick", "investigate")


def _coerce_prompt(value: Any, *, field: str) -> Optional[str]:
    """Validasi System Prompt per-proyek -> string non-kosong atau None.

    None = "TIDAK ada override" (pakai default existing). String kosong/None
    diperlakukan sebagai tidak ada override (bukan error) agar user dapat
    menghapus override dengan mengosongkan field.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectSettingsError(f"'{field}' harus berupa teks.")
    if not value.strip():
        return None
    if len(value) > MAX_PROJECT_SYSTEM_PROMPT_CHARS:
        raise ProjectSettingsError(
            f"'{field}' terlalu panjang (maksimum "
            f"{MAX_PROJECT_SYSTEM_PROMPT_CHARS} karakter)."
        )
    return value


class ProjectSettingsError(RuntimeError):
    """Gagal membaca/menulis konfigurasi project-local (mis. bukan JSON valid)."""


def _read_json_object(path: Path) -> Dict[str, Any]:
    """Baca file JSON sebagai object ({} bila absent/korup).

    TIDAK melempar: file hilang/korup diperlakukan sebagai object kosong agar
    runtime tetap memakai default aman.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class ProjectSettingsStore:
    """Store konfigurasi per-proyek di `<root>/.aether/settings/`.

    Args:
        root: root project target.
    """

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)

    @property
    def settings_dir(self) -> Path:
        return self.root / AETHER_DIR_NAME / SETTINGS_DIR_NAME

    @property
    def agent_path(self) -> Path:
        return self.settings_dir / AGENT_FILE_NAME

    @property
    def consultant_path(self) -> Path:
        return self.settings_dir / CONSULTANT_FILE_NAME

    # ------------------------------------------------------------------ #
    # Agent System Prompt
    # ------------------------------------------------------------------ #
    def load_agent_settings(self) -> Dict[str, Any]:
        """Muat konfigurasi Agent project ({} bila belum ada)."""
        return _read_json_object(self.agent_path)

    def agent_system_prompt(self) -> Optional[str]:
        """System Prompt Agent per-proyek (None bila project tidak override)."""
        raw = self.load_agent_settings().get("system_prompt")
        return _coerce_prompt(raw, field="agent.system_prompt")

    @property
    def agent_exists(self) -> bool:
        return self.agent_path.is_file()

    def save_agent_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Simpan System Prompt Agent project (atomic write).

        Payload menerima `system_prompt` (string). String kosong/None =
        hapus override (default existing berlaku kembali).
        """
        payload = payload or {}
        prompt = _coerce_prompt(payload.get("system_prompt"), field="agent.system_prompt")
        data: Dict[str, Any] = {}
        if prompt is not None:
            data["system_prompt"] = prompt
        self._write(self.agent_path, data)
        return data

    # ------------------------------------------------------------------ #
    # Consultant System Prompt
    # ------------------------------------------------------------------ #
    def load_consultant_settings(self) -> Dict[str, Any]:
        """Muat konfigurasi Consultant project ({} bila belum ada)."""
        return _read_json_object(self.consultant_path)

    def consultant_system_prompt(self) -> Optional[str]:
        """System Prompt Consultant per-proyek (None bila tidak override)."""
        raw = self.load_consultant_settings().get("system_prompt")
        return _coerce_prompt(raw, field="consultant.system_prompt")

    @property
    def consultant_exists(self) -> bool:
        return self.consultant_path.is_file()

    def save_consultant_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Simpan System Prompt Consultant project (atomic write).

        Payload menerima `system_prompt` (string). String kosong/None =
        hapus override (default existing berlaku kembali).
        """
        payload = payload or {}
        prompt = _coerce_prompt(
            payload.get("system_prompt"), field="consultant.system_prompt"
        )
        data: Dict[str, Any] = {}
        if prompt is not None:
            data["system_prompt"] = prompt
        self._write(self.consultant_path, data)
        return data

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _write(path: Path, data: Dict[str, Any]) -> None:
        body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_bytes(path, body.encode("utf-8"))


def project_agent_system_prompt(root: Optional[Union[str, Path]]) -> Optional[str]:
    """System Prompt Agent per-proyek untuk `root` (None bila tidak override).

    Fungsi ini TIDAK pernah melempar (root kosong / file korup -> None) agar
    runtime tetap memakai default existing (Global Settings).
    """
    if not root:
        return None
    try:
        return ProjectSettingsStore(root).agent_system_prompt()
    except Exception:  # noqa: BLE001 - konfigurasi tidak boleh crash runtime
        return None


def project_consultant_system_prompt(
    root: Optional[Union[str, Path]],
) -> Optional[str]:
    """System Prompt Consultant per-proyek untuk `root` (None bila tidak ada).

    Fungsi ini TIDAK pernah melempar (root kosong / file korup -> None) agar
    Consultant tetap memakai prompt bawaan AETHER.
    """
    if not root:
        return None
    try:
        return ProjectSettingsStore(root).consultant_system_prompt()
    except Exception:  # noqa: BLE001 - konfigurasi tidak boleh crash runtime
        return None
