"""Persistent telemetry usage Consultant (PROJECT-LOCAL, append-only JSONL).

Audit TASK 1 menemukan bahwa Consultant menjalankan ``AgentOrchestrator``
lewat jalur tersendiri sehingga metadata ``provider_response.usage`` TIDAK
pernah dipersistenkan (Task Log Agent hanya milik Agent). Modul ini menutup
gap itu TANPA menduplikasi sistem pencatatan Agent.

DESAIN PENYIMPANAN
    Format : JSON Lines (satu record JSON per baris).
    Lokasi : ``<project_root>/.aether/consultant/usage.jsonl``

Alasan memilih JSONL (bukan menambah array di ``sessions.json``):
    * Usage adalah deret-waktu append-only yang tumbuh terus; menulis ulang
      seluruh ``sessions.json`` setiap kemunculan provider MAHAL dan berisiko
      memperlambat percakapan.
    * JSONL memberi ``O(1)`` append dan aman dibaca bertahap (streaming) untuk
      Statistics API (TASK 3) tanpa memuat seluruh riwayat ke memori.
    * Isolasi per-project OTOMATIS: file berada di bawah ``.aether/`` project
      yang bersangkutan, konsisten dengan arsitektur project-local AETHER
      (mis. ``.aether/log``, ``.aether/consultant/sessions.json``).

BATAS
    * HANYA menyimpan METADATA penggunaan (angka token + identitas). TIDAK
      menyimpan prompt, respons lengkap, atau kredensial.
    * Best-effort: seluruh kegagalan I/O ditelan (telemetry TIDAK boleh
      menggagalkan percakapan Consultant).
    * Read-only untuk konsumen (verifier/Statistics API): TIDAK ada agregasi
      lintas-project di sini.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.core.response import (
    USAGE_STATUS_ACTUAL,
    USAGE_STATUS_PARTIAL,
    USAGE_STATUS_UNAVAILABLE,
    usage_status,
)

#: Relative location of the per-project Consultant usage file under a project root.
PROJECT_USAGE_RELPATH = (".aether", "consultant", "usage.jsonl")

#: Jenis aktivitas (kontrak telemetry TASK 3).
ACTIVITY_CONSULTANT = "consultant"
ACTIVITY_AGENT = "agent"


def project_usage_path(project_root: str) -> str:
    """Absolute path ke file usage Consultant MILIK satu project.

    Args:
        project_root: root otoritatif project.

    Returns:
        ``<project_root>/.aether/consultant/usage.jsonl``
    """
    return str(Path(project_root).joinpath(*PROJECT_USAGE_RELPATH))


@dataclass
class ConsultantUsageRecord:
    """Satu record metadata penggunaan satu respons provider Consultant.

    Identitas sesi/giliran (``session_id`` + ``turn_id`` + ``response_index``)
    cukup untuk mencegah duplikasi tanpa menyimpan teks percakapan.

    Attributes:
        timestamp: epoch detik saat record ditulis.
        project_id: project pemilik sesi (isolasi).
        activity: jenis aktivitas — selalu ``consultant`` di jalur ini.
        session_id: id sesi Consultant.
        turn_id: id giliran (satu panggilan ``consult()``).
        response_index: urutan respons provider DI DALAM giliran (0-based).
        provider: nama provider bila diketahui (string kosong bila tidak).
        model: nama model bila diketahui (string kosong bila tidak).
        input_tokens: token input AKTUAL provider (None bila tidak tersedia).
        output_tokens: token output AKTUAL provider (None bila tidak tersedia).
        total_tokens: token total (provider atau turunan input+output).
        usage_status: ``actual`` | ``partial`` | ``unavailable``.
        usage_source: sumber angka eksplisit (bukan asumsi nama provider).
    """

    timestamp: float
    project_id: str
    session_id: str
    turn_id: str
    response_index: int
    provider: str = ""
    model: str = ""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    usage_status: str = USAGE_STATUS_UNAVAILABLE
    usage_source: str = "unavailable"
    activity: str = ACTIVITY_CONSULTANT

    def identity(self) -> tuple:
        """Identitas unik record (untuk pencegahan duplikasi)."""
        return (self.project_id, self.session_id, self.turn_id, self.response_index)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_usage_record(
    *,
    project_id: str,
    session_id: str,
    turn_id: str,
    response_index: int,
    provider: str = "",
    model: str = "",
    usage: Optional[Dict[str, Any]] = None,
    source: str = "provider_response.usage",
    timestamp: Optional[float] = None,
) -> ConsultantUsageRecord:
    """Bangun record telemetry dari ``usage`` yang SUDAH dinormalisasi.

    Args:
        usage: dict ``{"prompt","completion","total"}`` (hasil ``response_usage``)
            atau ``None``. Komponen yang hilang dipertahankan ``None`` (BUKAN 0).
        source: sumber angka EKSPLISIT. ``provider_response.usage`` menandai angka
            berasal dari ``usage`` mentah provider (bukan estimasi lokal).

    Returns:
        :class:`ConsultantUsageRecord`.
    """
    data = usage if isinstance(usage, dict) else {}
    prompt = data.get("prompt")
    completion = data.get("completion")
    total = data.get("total")
    status = usage_status(data)
    if status == USAGE_STATUS_UNAVAILABLE:
        # Tidak ada angka: sumber pun ditandai tidak tersedia.
        effective_source = "unavailable"
    else:
        effective_source = source
    return ConsultantUsageRecord(
        timestamp=timestamp if timestamp is not None else time.time(),
        project_id=str(project_id or ""),
        session_id=str(session_id or ""),
        turn_id=str(turn_id or ""),
        response_index=int(response_index),
        provider=str(provider or ""),
        model=str(model or ""),
        input_tokens=prompt,
        output_tokens=completion,
        total_tokens=total,
        usage_status=status,
        usage_source=effective_source,
    )


class ConsultantUsageStore:
    """JSONL append-only store untuk telemetry usage Consultant per-project.

    Thread-safe (lock pada tulis/baca). Best-effort: kegagalan I/O ditelan dan
    dilaporkan lewat return value ``False`` (bukan exception), supaya telemetry
    TIDAK pernah menggagalkan percakapan.
    """

    def __init__(self, path: Optional[str] = None, *, ephemeral: bool = False) -> None:
        self._ephemeral = bool(ephemeral)
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        # Identitas yang sudah ditulis (dedup in-process): mencegah record
        # ganda bila sink yang sama dipanggil lebih dari sekali.
        self._seen: set = set()

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def append(self, record: ConsultantUsageRecord) -> bool:
        """Append satu record. Returns True bila tertulis (atau sudah ada)."""
        try:
            with self._lock:
                ident = record.identity()
                if ident in self._seen:
                    # Duplikat: sudah pernah ditulis -> tidak menulis ulang.
                    return False
                if self._ephemeral or self._path is None:
                    self._seen.add(ident)
                    return True
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
                    )
                    f.write("\n")
                self._seen.add(ident)
                return True
        except OSError:
            # Best effort: telemetry tidak boleh menggagalkan konsultasi.
            return False
        except Exception:  # noqa: BLE001 - pertahanan berlapis
            return False

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def read_all(self) -> List[Dict[str, Any]]:
        """Baca semua record (dict), mengabaikan baris rusak. Best-effort."""
        with self._lock:
            return self._read_locked()

    def _read_locked(self) -> List[Dict[str, Any]]:
        if self._path is None or not self._path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        records.append(obj)
        except OSError:
            return []
        return records

    def load_seen(self) -> None:
        """Muat identitas yang sudah ada di disk ke memori (dedup lintas restart)."""
        for obj in self.read_all():
            ident = (
                obj.get("project_id", ""),
                obj.get("session_id", ""),
                obj.get("turn_id", ""),
                obj.get("response_index"),
            )
            self._seen.add(ident)
