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
#:
#: Kebijakan FAIL-CLOSED: marker tetap luas (termasuk substring "token") karena
#: nama key kredensial tidak dapat diprediksi sepenuhnya. Pengetatan dilakukan
#: lewat `_NON_SECRET_KEY_EXEMPTIONS` — daftar EKSPLISIT kunci METRIK internal
#: yang bukan secret, sehingga angka diagnostik tetap terbaca di log tanpa
#: melemahkan redaksi untuk kunci lain.
_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "api_token",
    "authorization",
    "auth",
    "token",
    "access_token",
    "auth_token",
    "refresh_token",
    "id_token",
    "session_token",
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

#: Kunci METRIK internal yang BUKAN secret (exact match, lowercase).
#:
#: Diperlukan karena marker substring "token" juga mengenai nama kunci metrik
#: (mis. `input_tokens`, `output_tokens`, `context_budget_tokens`) sehingga
#: seluruh angka context/retrieval hilang dari log dan diagnosis menjadi buta.
#: Hanya kunci yang secara semantik KUANTITAS yang didaftarkan di sini.
_NON_SECRET_KEY_EXEMPTIONS = frozenset(
    {
        "context_budget",
        "context_budget_tokens",
        "context_overhead",
        "context_overhead_tokens",
        "context_before",
        "context_before_tokens",
        "context_after",
        "context_after_tokens",
        "context_tokens",
        "context_window",
        "context_budget_source",
        "knowledge_budget_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "max_tokens",
        "min_tokens",
        "budget_tokens",
        "estimated_tokens",
        "tokens_used",
        "remaining_tokens",
        "prompt_tokens",
        "completion_tokens",
    }
)

#: Nilai pengganti untuk field sensitif.
_REDACTED = "[redacted]"

#: Batas panjang string DEFAULT dalam payload (anti payload raksasa / bocor tak
#: sengaja). Dipakai untuk payload metadata yang ringkas.
_MAX_STRING_LEN = 2000

#: Batas panjang string untuk event yang MEMBAWA HASIL TOOL. `observation_received`
#: mengangkut output tool (isi file, stdout, hasil pencarian) yang dibutuhkan untuk
#: diagnosis; batas 2000 karakter membuat Task Log kehilangan sebagian besar payload
#: sehingga isi yang benar-benar dilihat Agent tidak dapat diverifikasi. Batas ini
#: tetap BOUNDED (bukan penghapusan limit global).
_OBSERVATION_MAX_STRING_LEN = 20_000

#: Batas string per event (event_type -> batas). Event yang tidak terdaftar
#: memakai `_MAX_STRING_LEN`.
_EVENT_STRING_LIMITS: Dict[str, int] = {
    "observation_received": _OBSERVATION_MAX_STRING_LEN,
}

#: Batas kedalaman rekursi sanitasi.
_MAX_DEPTH = 6


def _is_sensitive_key(key: str) -> bool:
    """True bila nama key mengandung penanda sensitif.

    Kunci metrik internal yang terdaftar di `_NON_SECRET_KEY_EXEMPTIONS`
    dikecualikan (bukan secret) sehingga angkanya tetap terbaca di log.
    """
    lowered = str(key).lower()
    if lowered in _NON_SECRET_KEY_EXEMPTIONS:
        return False
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def event_string_limit(event_type: Optional[str]) -> int:
    """Batas panjang string untuk sebuah event (default `_MAX_STRING_LEN`)."""
    if not event_type:
        return _MAX_STRING_LEN
    return int(_EVENT_STRING_LIMITS.get(str(event_type), _MAX_STRING_LEN))


def sanitize_payload(
    value: Any,
    _depth: int = 0,
    *,
    max_string_len: int = _MAX_STRING_LEN,
) -> Any:
    """Bersihkan payload dari secret sebelum dicatat.

    - Field dengan nama sensitif (api_key, authorization, access_token, ...) ->
      redacted.
    - String dipotong bila melebihi `max_string_len` (KECUALI `VerbatimText`, mis.
      final Agent Report yang harus utuh sampai ke user).
    - Struktur dict/list ditelusuri secara rekursif (bounded depth).

    Args:
        value: nilai payload apa pun.
        max_string_len: batas panjang string (default `_MAX_STRING_LEN`). Event
            yang membawa hasil tool memakai batas lebih besar lewat
            `sanitize_event_payload`, tetap BOUNDED.

    TIDAK pernah mengembalikan nilai sensitif apa pun.
    """
    if _depth > _MAX_DEPTH:
        return "[truncated]"

    limit = max(int(max_string_len), 1)

    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, val in value.items():
            if _is_sensitive_key(key):
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = sanitize_payload(
                    val, _depth + 1, max_string_len=limit
                )
        return cleaned

    if isinstance(value, (list, tuple)):
        return [
            sanitize_payload(item, _depth + 1, max_string_len=limit)
            for item in value
        ]

    # VerbatimText = teks final report: dipertahankan utuh (tanpa potong).
    if isinstance(value, VerbatimText):
        return str(value)

    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + "...[truncated]"
        return value

    # Angka/bool/None/objek lain: kembalikan apa adanya (bukan string sensitif).
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:limit]


def sanitize_event_payload(
    event_type: Optional[str],
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Sanitasi payload event memakai batas string SPESIFIK event.

    Satu sumber kebenaran untuk seluruh penulis log (event sink, SessionStore,
    Task Log) agar event yang membawa hasil tool tidak kehilangan payload
    penting hanya karena batas default 2000 karakter.
    """
    return sanitize_payload(
        dict(payload or {}), max_string_len=event_string_limit(event_type)
    )


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
        sink(event_type, sanitize_event_payload(event_type, payload))
    except Exception:  # noqa: BLE001 - sink error tidak boleh crash
        return
