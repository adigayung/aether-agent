"""Utilitas membaca/mengedit file `.env` secara AMAN.

Modul ini HANYA menangani baris assignment; ia TIDAK menafsirkan makna variabel.

Jaminan keamanan:
    - Mempertahankan komentar, baris kosong, urutan, dan variabel lain apa adanya.
    - Hanya baris dengan KEY yang cocok yang diubah/dihapus (baris lain utuh).
    - Penulisan ATOMIK (tulis file sementara lalu `os.replace`).
    - Tidak pernah mencetak/mengembalikan nilai secret dari operasi tulis/hapus;
      helper `mask_secret` tersedia bila perlu menampilkan sebagian.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Union

from agent_ai.llm_config.providers import is_api_key_env_name

# Satu baris assignment: `[export ]KEY=VALUE`.
_ENV_ASSIGNMENT = re.compile(
    r"^(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$"
)

PathLike = Union[str, "os.PathLike[str]"]


def mask_secret(value: Optional[str]) -> str:
    """Samarkan nilai secret agar aman ditampilkan.

    Contoh: "sk-abcdefghijkl" -> "sk-a********jkl".
    Nilai panjang <= 8 disamarkan seluruhnya.
    """
    if not value:
        return ""
    text = value.strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * (len(text) - 8)}{text[-4:]}"


def _unquote(value: str) -> str:
    """Buang tanda kutip pembungkus pada nilai .env (bila ada)."""
    stripped = value.strip()
    if (
        len(stripped) >= 2
        and stripped[0] == stripped[-1]
        and stripped[0] in ("'", '"')
    ):
        inner = stripped[1:-1]
        if stripped[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return stripped


def _format_value(value: str) -> str:
    """Format nilai agar aman ditulis ke .env (kutip bila perlu)."""
    if value == "":
        return ""
    needs_quote = any(ch in value for ch in (' ', "\t", "#", "'", '"', "="))
    if not needs_quote:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class EnvFile:
    """Akses file `.env` dengan operasi baca/edit yang aman.

    Args:
        path: lokasi file .env. File boleh belum ada (dibuat saat `set`).
    """

    def __init__(self, path: PathLike) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ #
    # Baca
    # ------------------------------------------------------------------ #
    def exists(self) -> bool:
        """True bila file .env ada."""
        return self.path.is_file()

    def _read_lines(self) -> List[str]:
        """Baca file menjadi daftar baris (tanpa newline)."""
        if not self.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        return text.splitlines()

    def entries(self) -> Dict[str, str]:
        """Semua pasangan key->value yang dapat diparse (urut file)."""
        result: Dict[str, str] = {}
        for line in self._read_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _ENV_ASSIGNMENT.match(stripped)
            if not match:
                continue
            result[match.group("key")] = _unquote(match.group("value"))
        return result

    def get(self, key: str) -> Optional[str]:
        """Nilai sebuah variabel dari FILE .env (None bila tidak ada)."""
        return self.entries().get(key)

    def api_key_names(self) -> List[str]:
        """Nama variabel .env yang mengikuti pola provider + `_API_KEY`."""
        return [name for name in self.entries() if is_api_key_env_name(name)]

    # ------------------------------------------------------------------ #
    # Tulis
    # ------------------------------------------------------------------ #
    def _write_lines(self, lines: List[str]) -> None:
        """Tulis daftar baris ke file secara atomik."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(lines)
        if content and not content.endswith("\n"):
            content += "\n"
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(str(tmp_path), str(self.path))

    def set(self, key: str, value: str) -> None:
        """Set/ubah satu variabel (upsert) TANPA mengganggu baris lain.

        Bila variabel sudah ada, hanya baris itu yang diganti (posisi dipertahankan).
        Bila belum ada, baris baru ditambahkan di akhir file. Environment proses
        saat ini juga diperbarui agar nilai langsung terpakai.
        """
        lines = self._read_lines()
        new_line = f"{key}={_format_value(value)}"
        replaced = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _ENV_ASSIGNMENT.match(stripped)
            if match and match.group("key") == key:
                lines[index] = new_line
                replaced = True
                break
        if not replaced:
            lines.append(new_line)
        self._write_lines(lines)
        os.environ[key] = value

    def delete(self, key: str) -> bool:
        """Hapus SATU variabel (hanya baris itu). Return True bila terhapus.

        Variabel lain, komentar, dan baris kosong tetap utuh.
        """
        lines = self._read_lines()
        kept: List[str] = []
        removed = False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                match = _ENV_ASSIGNMENT.match(stripped)
                if match and match.group("key") == key:
                    removed = True
                    continue
            kept.append(line)
        if removed:
            self._write_lines(kept)
            os.environ.pop(key, None)
        return removed
