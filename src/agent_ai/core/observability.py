"""Observability helpers (#55): event sink opsional + sanitasi payload.

Provider-agnostic, tanpa dependency baru. Modul ini TIDAK membuat event bus
kedua: ia hanya menyediakan:
    - EventSink: callable `(event_type: str, payload: dict) -> None` (opsional).
    - sanitize_payload: membersihkan payload dari secret sebelum dicatat.
    - emit: helper aman untuk memanggil sink (error sink tidak boleh crash).

Sumber event tetap Session/Event System yang sudah ada. Runtime/Orchestrator
memakai sink ini untuk meneruskan event ke SessionStore (bila diberikan).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

#: Tipe sink event: menerima nama event + payload (dict).
EventSink = Callable[[str, Dict[str, Any]], None]


class VerbatimText(str):
    """String yang SENGAJA tidak dipotong oleh `sanitize_payload`.

    Dipakai HANYA untuk teks yang memang harus utuh sampai ke user, yaitu
    final Agent Report (`task_completed.data.result` / `task_finished.result`).

    String biasa TETAP dibatasi `_MAX_STRING_LEN` seperti sebelumnya, sehingga
    batas payload activity log / tool output internal tidak berubah. Marker ini
    adalah pemisahan eksplisit "final report" vs "internal output" — bukan
    penghapusan limit secara global.
    """


def verbatim(text: Any) -> VerbatimText:
    """Bungkus teks agar tidak dipotong sanitasi (mis. final Agent Report)."""
    return VerbatimText("" if text is None else str(text))


#: Kunci yang dianggap sensitif (case-insensitive substring match).
_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "bearer",
    "cookie",
    "session_key",
    "private_key",
    "access_key",
)

#: Nilai pengganti untuk field sensitif.
_REDACTED = "[redacted]"

#: Batas panjang string dalam payload (anti payload raksasa / bocor tak sengaja).
_MAX_STRING_LEN = 2000

#: Batas kedalaman rekursi sanitasi.
_MAX_DEPTH = 6


def _is_sensitive_key(key: str) -> bool:
    """True bila nama key mengandung penanda sensitif."""
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def sanitize_payload(value: Any, _depth: int = 0) -> Any:
    """Bersihkan payload dari secret sebelum dicatat.

    - Field dengan nama sensitif (api_key, authorization, token, ...) -> redacted.
    - String dipotong bila terlalu panjang (KECUALI `VerbatimText`, mis. final
      Agent Report yang harus utuh sampai ke user).
    - Struktur dict/list ditelusuri secara rekursif (bounded depth).

    TIDAK pernah mengembalikan nilai sensitif apa pun.
    """
    if _depth > _MAX_DEPTH:
        return "[truncated]"

    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, val in value.items():
            if _is_sensitive_key(key):
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = sanitize_payload(val, _depth + 1)
        return cleaned

    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item, _depth + 1) for item in value]

    # VerbatimText = teks final report: dipertahankan utuh (tanpa potong).
    if isinstance(value, VerbatimText):
        return str(value)

    if isinstance(value, str):
        if len(value) > _MAX_STRING_LEN:
            return value[:_MAX_STRING_LEN] + "...[truncated]"
        return value

    # Angka/bool/None/objek lain: kembalikan apa adanya (bukan string sensitif).
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STRING_LEN]


def emit(
    sink: Optional[EventSink],
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Panggil sink event dengan payload yang sudah disanitasi.

    Aman: bila sink None atau sink melempar error, tidak melakukan apa-apa
    (observability tidak boleh menggagalkan eksekusi).
    """
    if sink is None:
        return
    try:
        sink(event_type, sanitize_payload(dict(payload or {})))
    except Exception:  # noqa: BLE001 - sink error tidak boleh crash
        return
