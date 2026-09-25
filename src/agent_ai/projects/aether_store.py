"""Project-local `.aether` store: AI Project Bible + Task Log.

Struktur yang dikelola (di ROOT project target):

    <root project target>/
        .aether/
            bible/
                index.md          # manifest/navigation (dibaca LLM)
                architecture.md
                ui.md
                conventions.md
                decisions.md
                facts.md
                learnings.md
                problems.md
            log/
                <task_id>.log     # log task (JSON Lines)
            ENVIRONMENT.md        # Environment Context (deteksi OS/shell/runtime)

Prinsip:
    - Project-local: semua ditulis di dalam root project target, tidak di
      workspace AETHER.
    - AI-oriented: Bible ditulis sebagai markdown terstruktur (marker +
      key-value) yang padat & tidak ambigu untuk dibaca LLM.
    - Best-effort untuk Task Log: kegagalan menulis log TIDAK boleh
      menggagalkan eksekusi task.
    - Tidak ada dependency baru; tanpa DB/vektor/embeddings.
    - Idempotent: `ensure()` aman dipanggil berulang.

Modul ini adalah SATU-SATUNYA tempat penulisan `<root>/.aether/`. ProjectBrain/
ProjectIntelligence membaca & menulis Bible hanya lewat sini.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent_ai.projects.models import (
    BIBLE_CATEGORIES,
    CATEGORY_ALIASES,
    IntelligenceEntry,
    _now_iso,
)

#: Nama folder root metadata project.
AETHER_DIR_NAME = ".aether"
#: Subfolder log task.
LOG_DIR_NAME = "log"
#: Subfolder AI Project Bible.
BIBLE_DIR_NAME = "bible"
#: Nama file manifest Bible.
BIBLE_INDEX_NAME = "index.md"
#: Nama file Environment Context (didokumentasikan di environment.py).
ENVIRONMENT_FILE_NAME = "ENVIRONMENT.md"
#: Versi format file Bible (marker kompatibilitas).
BIBLE_FORMAT = "entry-v1"
#: Marker awal satu entri di file kategori Bible.
ENTRY_MARKER = "## entry"

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_TASK_ID_LEN = 96

#: Penjelasan tiap kategori Bible + kapan harus dibaca (untuk index.md).
_CATEGORY_GUIDE = (
    ("architecture", "struktur & layer utama project; baca sebelum mengubah struktur"),
    ("ui", "kontrak UI/frontend (komponen, layout, alur layar); baca sebelum menyentuh UI"),
    ("conventions", "aturan & konvensi coding project; baca sebelum menulis kode baru"),
    ("decisions", "keputusan teknis penting beserta alasannya; baca sebelum mengubah pendekatan"),
    ("facts", "fakta project (stack, dependency, versi, entry point); baca selalu"),
    ("learnings", "pelajaran dari task sebelumnya (termasuk yang gagal); baca untuk hindari kesalahan sama"),
    ("problems", "masalah/known-issue yang belum selesai; baca sebelum menyimpulkan selesai"),
    ("known_bugs", "bug/masalah yang sudah ditemukan/terverifikasi (BUG-xxx); baca sebelum memperbaiki"),
    ("known_gaps", "kekurangan/fitur yang belum tersedia tetapi bukan bug (GAP-xxx); baca sebelum menambah fitur"),
)


def new_task_id() -> str:
    """Buat task_id unik (bila execution tidak membawa task_id)."""
    return uuid.uuid4().hex


def safe_task_id(value: Any) -> str:
    """Normalisasi task_id menjadi nama file yang aman (tanpa path traversal).

    Mengembalikan string kosong bila `value` kosong / tidak menghasilkan
    karakter aman (pemanggil dapat memakai `new_task_id()`).
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    # Buang komponen path (cocok untuk '/' dan '\').
    text = text.replace("\\", "/").split("/")[-1]
    text = _SAFE_ID_RE.sub("_", text).strip("._-")
    if not text:
        return ""
    return text[:_MAX_TASK_ID_LEN]


def canonical_bible_category(category: str) -> str:
    """Petakan kategori (termasuk alias lama) ke nama kanonik Bible."""
    if category in BIBLE_CATEGORIES:
        return category
    return CATEGORY_ALIASES.get(category, category)


def _atomic_write(path: Path, text: str) -> None:
    """Tulis file secara atomik (temp + os.replace) agar tidak ada file parsial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".aether_tmp_", suffix=".swp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


class AetherProjectStore:
    """Resolver path + pembuatan struktur `<root>/.aether/` (idempotent).

    Args:
        root: root project target (folder project yang dikerjakan AETHER).
    """

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root).resolve()
        self.aether_dir = self.root / AETHER_DIR_NAME
        self.log_dir = self.aether_dir / LOG_DIR_NAME
        self.bible_dir = self.aether_dir / BIBLE_DIR_NAME

    def ensure(self) -> bool:
        """Pastikan `.aether/`, `.aether/log/`, `.aether/bible/` ada.

        Returns:
            True bila struktur siap, False bila gagal (tidak melempar error).
        """
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.bible_dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

    def log_path(self, task_id: Any) -> Path:
        """Path log task untuk `task_id` (task_id dibuat bila kosong)."""
        name = safe_task_id(task_id) or new_task_id()
        return self.log_dir / f"{name}.log"

    def bible_path(self, category: str) -> Path:
        """Path file kategori Bible (alias dinormalisasi)."""
        return self.bible_dir / f"{canonical_bible_category(category)}.md"

    def index_path(self) -> Path:
        """Path file manifest Bible (`index.md`)."""
        return self.bible_dir / BIBLE_INDEX_NAME

    def environment_path(self) -> Path:
        """Path file Environment Context (`<root>/.aether/ENVIRONMENT.md`)."""
        return self.aether_dir / ENVIRONMENT_FILE_NAME

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<AetherProjectStore root={self.root}>"

    # ------------------------------------------------------------------ #
    # Task Log Discovery
    # ------------------------------------------------------------------ #
    def list_task_logs(self) -> List[Path]:
        """Daftar semua file log task di `.aether/log/`.

        Returns:
            Daftar Path file .log, terurut terbaru ke terlama
            (berdasarkan mtime file).
        """
        if not self.log_dir.exists():
            return []
        return sorted(
            [p for p in self.log_dir.glob("*.log") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )


class TaskLog:
    """Writer log task project-local (append-only JSON Lines, best-effort).

    Satu file per task: `<root>/.aether/log/<task_id>.log`. Semua kegagalan
    (folder tidak bisa dibuat, disk penuh, dsb.) ditelan dan hanya menghasilkan
    `False` agar TIDAK pernah menggagalkan eksekusi task.
    """

    def __init__(self, root: Union[str, Path, AetherProjectStore], task_id: Any = None) -> None:
        self.store = root if isinstance(root, AetherProjectStore) else AetherProjectStore(root)
        self.task_id = safe_task_id(task_id) or new_task_id()
        self.store.ensure()
        self.path = self.store.log_path(self.task_id)

    @staticmethod
    def resolve_task_id(task_id: Any) -> str:
        """Gunakan task_id yang ada, atau buat baru bila kosong/tidak valid."""
        return safe_task_id(task_id) or new_task_id()

    def append(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        """Tambahkan satu event ke log (best-effort). Returns True bila tertulis."""
        record: Dict[str, Any] = {
            "timestamp": _now_iso(),
            "task_id": self.task_id,
            "event": str(event_type),
        }
        if payload:
            record["data"] = payload
        # Sanitasi (tanpa secret) memakai helper observability existing.
        # Batas string SPESIFIK EVENT dipakai agar event yang membawa hasil tool
        # (mis. `observation_received`) tidak kehilangan payload diagnostik,
        # sementara payload lain tetap dibatasi seperti sebelumnya.
        try:
            from agent_ai.core.observability import sanitize_event_payload

            event_type = str(event_type)
            record["event"] = event_type
            record = sanitize_event_payload(event_type, record)
        except Exception:  # noqa: BLE001 - sanitasi gagal -> tetap catat
            pass
        try:
            self.store.ensure()
            line = json.dumps(record, ensure_ascii=False, default=str)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            return True
        except Exception:  # noqa: BLE001 - log tidak boleh menggagalkan task
            return False


class BibleStore:
    """Storage AI Project Bible berbasis markdown project-local.

    Kategori dipetakan ke file `<kategori>.md` di `<root>/.aether/bible/`.
    Format berorientasi LLM: header marker + blok entri key-value (lihat
    `BIBLE_FORMAT`). Append-only secara semantik (knowledge lama dipertahankan).
    """

    categories = BIBLE_CATEGORIES

    def __init__(self, root: Union[str, Path, AetherProjectStore]) -> None:
        self.store = root if isinstance(root, AetherProjectStore) else AetherProjectStore(root)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def ensure(self) -> bool:
        """Buat folder + file kategori + index bila belum ada (idempotent)."""
        if not self.store.ensure():
            return False
        try:
            index = self.store.index_path()
            if not index.exists():
                _atomic_write(index, self._index_text())
            for category in BIBLE_CATEGORIES:
                path = self.store.bible_path(category)
                if not path.exists():
                    _atomic_write(path, self._header(category))
            return True
        except OSError:
            return False

    @property
    def root(self) -> Path:
        return self.store.root

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def read_category(self, category: str) -> List[IntelligenceEntry]:
        """Baca semua entri satu kategori (toleran terhadap file rusak)."""
        path = self.store.bible_path(category)
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return self._parse(text)

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def add_entry(self, category: str, entry: IntelligenceEntry) -> IntelligenceEntry:
        """Tambahkan entri ke kategori (append; knowledge lama dipertahankan)."""
        self.ensure()
        entries = self.read_category(category)
        entries.append(entry)
        self._write_category(category, entries)
        return entry

    def update_entry(
        self,
        category: str,
        entry_id: str,
        **changes: Any,
    ) -> IntelligenceEntry:
        """Update entri berdasarkan id (raise EntryNotFoundError bila tidak ada)."""
        from agent_ai.projects.intelligence import EntryNotFoundError

        entries = self.read_category(category)
        for entry in entries:
            if entry.id == entry_id:
                for key, value in changes.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
                entry.updated_at = _now_iso()
                self._write_category(category, entries)
                return entry
        raise EntryNotFoundError(f"Entry '{entry_id}' tidak ditemukan di kategori '{category}'.")

    def get_entry(self, category: str, entry_id: str) -> Optional[IntelligenceEntry]:
        """Ambil entri berdasarkan id (None bila tidak ada)."""
        for entry in self.read_category(category):
            if entry.id == entry_id:
                return entry
        return None

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def _header(self, category: str) -> str:
        return (
            f"# bible:{category}\n"
            f"<!-- AETHER BIBLE (machine-readable). format={BIBLE_FORMAT}. -->\n"
            f"<!-- Setiap entri dimulai dengan marker '{ENTRY_MARKER}'. -->\n"
            "<!-- Tambahkan knowledge lewat ProjectBrain/AETHER; jangan edit manual. -->\n"
            "\n"
        )

    def _write_category(self, category: str, entries: List[IntelligenceEntry]) -> None:
        blocks = [self._header(category).rstrip("\n")]
        for entry in entries:
            blocks.append(self._serialize_entry(entry))
        _atomic_write(self.store.bible_path(category), "\n\n".join(blocks) + "\n")

    @staticmethod
    def _serialize_entry(entry: IntelligenceEntry) -> str:
        content = json.dumps(entry.content, ensure_ascii=False, default=str)
        return "\n".join(
            [
                ENTRY_MARKER,
                f"- id: {entry.id}",
                f"- source: {entry.source}",
                f"- confidence: {entry.confidence}",
                f"- created_at: {entry.created_at}",
                f"- updated_at: {entry.updated_at}",
                f"- content: {content}",
            ]
        )

    @staticmethod
    def _parse(text: str) -> List[IntelligenceEntry]:
        """Parse file kategori menjadi entri (toleran; bagian rusak dilewati)."""
        raw_entries: List[Dict[str, str]] = []
        current: Optional[Dict[str, str]] = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == ENTRY_MARKER:
                if current is not None:
                    raw_entries.append(current)
                current = {}
                continue
            if current is None or not stripped.startswith("- "):
                continue
            body = stripped[2:]
            if ": " in body:
                key, value = body.split(": ", 1)
            elif body.endswith(":"):
                key, value = body[:-1], ""
            else:
                continue
            current[key.strip()] = value
        if current is not None:
            raw_entries.append(current)

        entries: List[IntelligenceEntry] = []
        for raw in raw_entries:
            content = BibleStore._parse_content(raw.get("content"))
            if content is None:
                continue
            if isinstance(content, str) and not content.strip():
                continue
            entries.append(
                IntelligenceEntry(
                    id=raw.get("id") or uuid.uuid4().hex,
                    content=content,
                    source=raw.get("source", "ai"),
                    confidence=_parse_confidence(raw.get("confidence")),
                    created_at=raw.get("created_at") or _now_iso(),
                    updated_at=raw.get("updated_at") or _now_iso(),
                )
            )
        return entries

    @staticmethod
    def _parse_content(value: Optional[str]) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value  # fallback: pakai teks mentah

    def _index_text(self) -> str:
        lines = [
            "# Project Bible",
            "",
            "<!-- AETHER BIBLE index (machine-readable). Manifest/navigation. -->",
            "<!-- Knowledge project untuk agen AI. Baca kategori relevan sebelum",
            "     bekerja; tulis lewat ProjectBrain/AETHER, jangan edit manual. -->",
            "",
            "## categories",
        ]
        for category, guide in _CATEGORY_GUIDE:
            lines.append(f"- {category} (file: {category}.md) — {guide}")
        lines.append("")
        lines.append("## format")
        lines.append(f"- {BIBLE_FORMAT}; satu entri dipisah marker '{ENTRY_MARKER}'")
        lines.append("- field: id, source, confidence, created_at, updated_at, content")
        lines.append("")
        return "\n".join(lines)


def _parse_confidence(value: Optional[str]) -> float:
    """Parse confidence dari teks; fallback 1.0 bila tidak valid."""
    if value is None:
        return 1.0
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 1.0
    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return confidence


#: Alias lama agar pemanggil dapat memakai nama yang lebih deskriptif.
AetherTaskLog = TaskLog


class TaskLogReader:
    """Reader log task project-local (read-only, JSON Lines).

    Membaca file `.aether/log/<task_id>.log` yang sudah ada.
    Tidak menulis atau memodifikasi log apa pun.

    Args:
        root: root project target (string atau Path).
        task_id: identifier task.
    """

    def __init__(self, root: Union[str, Path, AetherProjectStore], task_id: Any = None) -> None:
        self.store = root if isinstance(root, AetherProjectStore) else AetherProjectStore(root)
        self.task_id = safe_task_id(task_id) or new_task_id()
        self.path = self.store.log_path(self.task_id)

    def _read_lines(self) -> List[str]:
        """Baca semua baris dari file log (best-effort, toleran terhadap error)."""
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
            return [line for line in text.splitlines() if line.strip()]
        except OSError:
            return []

    def load_events(self) -> List[Dict[str, Any]]:
        """Baca semua event dari log task, parse JSON, abaikan baris rusak.

        Returns:
            Daftar event terurut chronological (sesuai urutan penulisan).
            Setiap event adalah dict dengan kunci: timestamp, task_id, event, data.
            Baris yang tidak valid secara JSON dilewati tanpa merusak event lain.
        """
        events: List[Dict[str, Any]] = []
        for line in self._read_lines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
        return events

    def get_task_info(self) -> Optional[Dict[str, Any]]:
        """Ambil ringkasan task dari log (task_id, first/last timestamp, status, prompt).

        Returns:
            Dict dengan task_id, first_timestamp, last_timestamp, status, task (prompt).
            None bila log tidak ditemukan atau kosong.
        """
        events = self.load_events()
        if not events:
            return None

        first_ts = events[0].get("timestamp")
        last_ts = events[-1].get("timestamp")

        # Cari status terminal dan prompt.
        status = "incomplete"
        prompt = ""
        result = None
        error = None

        for ev in events:
            evt = ev.get("event", "")
            data = ev.get("data", {}) or {}
            if evt == "task_requested":
                prompt = data.get("prompt", "")
            elif evt == "task_completed":
                status = "completed"
                result = data.get("result")
            elif evt == "task_failed":
                status = "failed"
                error = data.get("error")
            elif evt == "task_cancelled":
                status = "cancelled"
            elif evt == "task_finished":
                # task_finished bisa jadi fallback status; gunakan bila belum ada status terminal.
                if status not in ("completed", "failed", "cancelled"):
                    status = data.get("status", "incomplete")
                    if data.get("result") is not None and result is None:
                        result = data.get("result")
                    if data.get("error") is not None and error is None:
                        error = data.get("error")

        return {
            "task_id": self.task_id,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "status": status,
            "task": prompt,
            "result": result,
            "error": error,
        }

    def get_activity(self, event_types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Ambil seluruh activity chronological untuk task ini.

        Args:
            event_types: daftar tipe event yang relevan untuk activity.
                Bila None, semua event termasuk tool activity ikut diambil.

        Returns:
            Daftar event terurut chronological berdasarkan timestamp.
        """
        events = self.load_events()
        if event_types is not None:
            events = [e for e in events if e.get("event") in event_types]
        return events

    def get_report(self) -> Optional[str]:
        """Ambil final Agent Report dari task_completed atau task_finished.

        Source utama: task_completed.data.result.
        Fallback: task_finished.data.result.

        Returns:
            Teks report final atau None bila tidak ditemukan.
        """
        events = self.load_events()
        for ev in reversed(events):
            evt = ev.get("event", "")
            data = ev.get("data", {}) or {}
            if evt == "task_completed" and "result" in data:
                return data["result"]
            if evt == "task_finished" and "result" in data:
                return data["result"]
        return None
