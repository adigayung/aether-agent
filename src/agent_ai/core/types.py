"""Tipe data percakapan Native Tool Calling (provider-agnostic).

Modul ini mendefinisikan skema netral untuk percakapan agent:

    - ChatMessage       : satu pesan percakapan (system/user/assistant/tool).
    - ToolCall          : permintaan pemanggilan tool dari model.
    - ToolResultPayload : hasil eksekusi tool (untuk pesan role "tool").

Skema sengaja selaras dengan format `messages` OpenAI Chat Completions
(Native Tool Calling), sehingga `ChatMessage.to_provider_dict()` dapat
langsung dipakai oleh provider:

    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "...", "type": "function",
                     "function": {"name": "...", "arguments": "<json>"}}]}
    {"role": "tool", "tool_call_id": "...", "content": "..."}

Properti penting:
    - Hasil tool SELALU memakai role "tool" (bukan "user"), dikaitkan ke
      pemanggilnya lewat `tool_call_id`. Ini menghilangkan pola lama yang
      menjejalkan observation ke pesan role "user".
    - `arguments` pada tool call diserialisasi menjadi JSON string pada bentuk
      provider (kontrak tool calling), tetapi tetap boleh berupa dict pada
      bentuk internal agar mudah dipakai core.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ChatRole(str, Enum):
    """Role pesan yang valid dalam percakapan (Native Tool Calling)."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolResultStatus(str, Enum):
    """Status hasil eksekusi sebuah tool."""

    SUCCESS = "success"
    ERROR = "error"


_VALID_ROLES = {role.value for role in ChatRole}


def parse_tool_arguments(raw: Any) -> tuple[Dict[str, Any], Optional[str]]:
    """Parse argumen ToolCall dengan aman -> (dict, error_message|None).

    Menerima dict (sudah terstruktur) atau JSON string (format provider).
    TIDAK melempar exception: JSON tidak valid dikembalikan sebagai pesan error
    agar pemanggil bisa mengubahnya menjadi hasil gagal / klasifikasi aman.

    Helper ini dipakai bersama oleh ToolExecutor dan Tool Execution Coordinator
    sehingga tidak ada parser argumen kedua di codebase.
    """
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return dict(raw), None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            return {}, f"{type(exc).__name__}: {exc}"
        if isinstance(parsed, dict):
            return parsed, None
        return {}, f"arguments harus JSON object, bukan {type(parsed).__name__}"
    return {}, f"tipe arguments tidak didukung: {type(raw).__name__}"


def _serialize_arguments(arguments: Any) -> str:
    """Serialisasi argumen tool menjadi string JSON (format API).

    Menerima dict maupun string:
        - dict  -> JSON string (kontrak `function.arguments` pada tool calling).
        - str   -> diteruskan apa adanya (dianggap sudah JSON).
        - None  -> string kosong.
    """
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(arguments)


@dataclass
class ToolCall:
    """Permintaan pemanggilan tool dari model (Native Tool Calling).

    Struktur selaras format OpenAI:
        {"id": ..., "type": "function",
         "function": {"name": ..., "arguments": "<json string>"}}

    Attributes:
        id: identifier unik tool call; dipakai untuk mengaitkan hasil tool
            (`tool_call_id`) dengan pemanggilnya.
        function: dict berisi "name" (str) dan "arguments" (dict|str).
        type: tipe tool call; saat ini selalu "function".
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    function: Dict[str, Any] = field(default_factory=dict)
    type: str = "function"

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def create(
        cls,
        name: str,
        arguments: Any = None,
        *,
        id: Optional[str] = None,
        type: str = "function",
    ) -> "ToolCall":
        """Bangun ToolCall dari nama + argumen (id dibuat otomatis bila kosong)."""
        return cls(
            id=id or uuid.uuid4().hex,
            function={"name": name, "arguments": {} if arguments is None else arguments},
            type=type,
        )

    @classmethod
    def from_provider_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        """Bangun ToolCall dari dict bentuk provider (id/type/function).

        Toleran terhadap dict yang memakai "function" (format API) maupun
        yang langsung memuat "name"/"arguments" (format ringkas).
        """
        data = data or {}
        if "function" in data and isinstance(data.get("function"), dict):
            fn = data["function"]
            name = fn.get("name", "")
            arguments = fn.get("arguments", "")
        else:
            name = data.get("name", "")
            arguments = data.get("arguments", "")
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            function={"name": name, "arguments": arguments},
            type=str(data.get("type") or "function"),
        )

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        """Nama tool/function yang dipanggil."""
        return str(self.function.get("name", "") or "")

    @property
    def arguments(self) -> Any:
        """Argumen tool (dict atau JSON string) apa adanya."""
        return self.function.get("arguments", "")

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_provider_dict(self) -> Dict[str, Any]:
        """Representasi dict sesuai kontrak tool calling (arguments = JSON str)."""
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.name,
                "arguments": _serialize_arguments(self.arguments),
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        """Representasi internal (arguments tetap dict bila semula dict)."""
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class ToolResultPayload:
    """Hasil eksekusi satu tool (untuk pesan role "tool").

    Attributes:
        tool_call_id: id ToolCall yang menghasilkan output ini.
        tool_name: nama tool yang dieksekusi.
        output: isi hasil (str/dict/list) atau pesan error saat gagal.
        status: ToolResultStatus (SUCCESS/ERROR).
    """

    tool_call_id: str
    tool_name: str
    output: Any = None
    status: ToolResultStatus = ToolResultStatus.SUCCESS

    def __post_init__(self) -> None:
        if isinstance(self.status, str) and not isinstance(self.status, ToolResultStatus):
            self.status = ToolResultStatus(self.status)

    @classmethod
    def success(
        cls,
        tool_call_id: str,
        tool_name: str,
        output: Any = None,
    ) -> "ToolResultPayload":
        """Bangun payload hasil tool yang sukses."""
        return cls(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            output=output,
            status=ToolResultStatus.SUCCESS,
        )

    @classmethod
    def error(
        cls,
        tool_call_id: str,
        tool_name: str,
        error: Any = None,
    ) -> "ToolResultPayload":
        """Bangun payload hasil tool yang gagal."""
        return cls(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            output=error,
            status=ToolResultStatus.ERROR,
        )

    @property
    def is_success(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS

    def to_content(self) -> str:
        """Ubah output menjadi teks konten untuk pesan role "tool"."""
        if self.output is None:
            return ""
        if isinstance(self.output, str):
            return self.output
        try:
            return json.dumps(self.output, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(self.output)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "output": self.output,
            "status": self.status.value,
        }


@dataclass
class ChatMessage:
    """Satu pesan percakapan (provider-agnostic, Native Tool Calling).

    Attributes:
        role: salah satu "system" | "user" | "assistant" | "tool".
        content: teks pesan. Boleh None (mis. assistant yang hanya memanggil
            tool, atau saat hasil tool kosong).
        tool_calls: daftar ToolCall pada pesan assistant (bila ada).
        name: nama opsional (mis. nama tool pada pesan tool).
        tool_call_id: id ToolCall yang dijawab pesan role "tool".
        parts: content blocks tambahan (mis. image) untuk pesan multimodal.
            Format internal AETHER provider-agnostic
            ({"type": "image", "mime_type":..., "encoding":"base64", "data":...}).
            Kosong (default) = pesan text-only seperti sebelumnya; provider
            adapter yang menerjemahkan ke format API masing-masing.
    """

    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    parts: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        if isinstance(self.role, ChatRole):
            self.role = self.role.value
        if self.role not in _VALID_ROLES:
            raise ValueError(
                f"role tidak valid: {self.role!r} (harus salah satu dari {sorted(_VALID_ROLES)})"
            )
        if self.tool_calls is not None:
            self.tool_calls = [self._coerce_tool_call(tc) for tc in self.tool_calls]
        if self.role == ChatRole.TOOL.value and self.tool_call_id is None:
            raise ValueError("pesan role 'tool' wajib menyertakan tool_call_id.")
        if self.role != ChatRole.TOOL.value and self.tool_call_id is not None:
            raise ValueError("tool_call_id hanya valid untuk pesan role 'tool'.")

    @staticmethod
    def _coerce_tool_call(tool_call: Any) -> ToolCall:
        if isinstance(tool_call, ToolCall):
            return tool_call
        if isinstance(tool_call, dict):
            return ToolCall.from_provider_dict(tool_call)
        raise TypeError(f"tool_call tidak didukung: {type(tool_call).__name__}")

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_provider_dict(self) -> Dict[str, Any]:
        """Representasi dict siap pakai untuk `messages` provider.

        Mengikuti skema OpenAI Chat Completions:
            - assistant + tool_calls -> sertakan "tool_calls" (content boleh None).
            - tool -> sertakan "tool_call_id" (dan "name" bila ada).

        Bila pesan membawa `parts` (mis. image), field `parts` disertakan apa
        adanya (format internal AETHER); provider adapter yang mengonversinya
        ke content blocks format API masing-masing. Pesan text-only tidak
        berubah (tanpa field `parts`).
        """
        message: Dict[str, Any] = {"role": self.role}

        if self.role == ChatRole.ASSISTANT.value and self.tool_calls:
            message["content"] = self.content
            message["tool_calls"] = [tc.to_provider_dict() for tc in self.tool_calls]
            return self._with_parts(message)

        if self.role == ChatRole.TOOL.value:
            message["content"] = self.content if self.content is not None else ""
            message["tool_call_id"] = self.tool_call_id
            if self.name is not None:
                message["name"] = self.name
            return message

        message["content"] = self.content if self.content is not None else ""
        if self.name is not None:
            message["name"] = self.name
        return self._with_parts(message)

    def _with_parts(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Sertakan `parts` multimodal bila ada (additive, text-only tidak berubah)."""
        if self.parts:
            message["parts"] = [dict(p) for p in self.parts]
        return message

    def to_dict(self) -> Dict[str, Any]:
        """Representasi internal (tool_calls mempertahankan argumen asli)."""
        data: Dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.tool_calls is not None:
            data["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.name is not None:
            data["name"] = self.name
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.parts:
            data["parts"] = [dict(p) for p in self.parts]
        return data

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        n_calls = len(self.tool_calls or [])
        return f"<ChatMessage role={self.role!r} content={self.content!r} tool_calls={n_calls}>"
