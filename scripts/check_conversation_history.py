"""Verifikasi ConversationHistory + tipe percakapan Native Tool Calling.

Menguji skema pesan baru (Task 1):
    - ChatMessage USER -> ASSISTANT(tool_calls) -> TOOL(tool_call_id).
    - to_provider_format() menghasilkan dict valid sesuai skema provider.
    - Hasil tool TIDAK pernah disisipkan sebagai role "user".
    - Serialisasi argumen tool (dict -> JSON string).
    - compile_chat_messages() memangkas riwayat tanpa merusak pasangan
      assistant(tool_calls)/tool dan tetap mempertahankan pesan system.
    - ToolResultPayload success/error.

Murni struktur data (tanpa fixture workspace, tanpa API).

Jalankan:
    python scripts/check_conversation_history.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import (  # noqa: E402
    ChatMessage,
    ConversationHistory,
    ToolCall,
    ToolResultPayload,
    ToolResultStatus,
)
from agent_ai.core.types import _serialize_arguments  # noqa: E402


def main() -> int:
    print("=== Verifikasi Conversation History (Native Tool Calling) ===")

    history = ConversationHistory()

    # 1) Urutan USER -> ASSISTANT(tool_calls) -> TOOL(tool_call_id) tersimpan.
    history.append_system_message("Kamu adalah agent coding.")
    history.append_user_message("Baca file a.txt lalu ringkas.")
    call = ToolCall.create("read_file", {"path": "a.txt"})
    history.append_assistant_message(content=None, tool_calls=[call])
    history.append_tool_result(
        tool_call_id=call.id,
        tool_name="read_file",
        content="isi file a.txt",
    )
    history.append_assistant_message(content="Ringkasan: file berisi teks.")

    roles = [m.role for m in history.messages]
    print(f"roles        -> {roles}")
    assert roles == ["system", "user", "assistant", "tool", "assistant"], roles
    tool_msg = history.messages[3]
    assert tool_msg.role == "tool", tool_msg.role
    assert tool_msg.tool_call_id == call.id, tool_msg
    print("OK: urutan & role pesan benar (tool memakai tool_call_id)")

    # 2) Hasil tool TIDAK pernah berrole "user".
    assert not any(
        m.role == "user" and m.tool_call_id for m in history.messages
    ), "tool result tidak boleh berrole user"
    print("OK: tidak ada tool result yang disisipkan sebagai role 'user'")

    # 3) to_provider_format() valid sesuai skema provider.
    messages = history.to_provider_format()
    print("provider msg -> " + json.dumps(messages[3], ensure_ascii=False))
    assert messages[0] == {"role": "system", "content": "Kamu adalah agent coding."}
    assistant_call = messages[2]
    assert assistant_call["role"] == "assistant"
    assert assistant_call["content"] is None
    assert assistant_call["tool_calls"][0]["id"] == call.id
    assert assistant_call["tool_calls"][0]["type"] == "function"
    assert assistant_call["tool_calls"][0]["function"]["name"] == "read_file"
    # arguments WAJIB string JSON pada bentuk provider.
    raw_args = assistant_call["tool_calls"][0]["function"]["arguments"]
    assert isinstance(raw_args, str), raw_args
    assert json.loads(raw_args) == {"path": "a.txt"}, raw_args
    provider_tool = messages[3]
    assert provider_tool["role"] == "tool"
    assert provider_tool["tool_call_id"] == call.id
    assert provider_tool["content"] == "isi file a.txt"
    # Setiap message valid: selalu punya "role".
    assert all("role" in m for m in messages)
    print("OK: to_provider_format() menghasilkan messages valid (tool_calls + tool_call_id)")

    # 4) Serialisasi argumen: dict -> JSON string, string tetap string.
    assert _serialize_arguments({"a": 1}) == '{"a": 1}'
    assert _serialize_arguments('{"a": 1}') == '{"a": 1}'
    assert _serialize_arguments(None) == ""
    print("OK: serialisasi argumen tool (dict -> JSON string) benar")

    # 5) ToolResultPayload success/error.
    ok = ToolResultPayload.success("id-1", "list_files", {"files": ["a.txt"]})
    err = ToolResultPayload.error("id-2", "read_file", "file tidak ada")
    assert ok.status == ToolResultStatus.SUCCESS and ok.is_success
    assert err.status == ToolResultStatus.ERROR and not err.is_success
    assert json.loads(ok.to_content()) == {"files": ["a.txt"]}
    assert err.to_content() == "file tidak ada"
    print("OK: ToolResultPayload success/error benar")

    # 6) compile_chat_messages: pertahankan system, pangkas paling lama,
    #    dan jangan tinggalkan tool result yatim di awal.
    long_history = ConversationHistory()
    long_history.append_system_message("SYS")
    for i in range(40):
        long_history.append_user_message(f"pesan user nomor {i} " + "x" * 40)
    compiled = long_history.compile_chat_messages(max_tokens=60)
    print(f"compiled     -> {len(compiled)} dari {len(long_history)} pesan")
    assert len(compiled) < len(long_history), "harus terpangkas"
    assert compiled[0].role == "system", "pesan system harus dipertahankan"
    assert all("system" or True for _ in compiled)
    # Pesan terbaru dipertahankan.
    assert compiled[-1].content == long_history.messages[-1].content
    print("OK: compile_chat_messages memangkas & mempertahankan system + pesan terbaru")

    # 7) Pemangkasan tidak menyisakan tool result yatim di batas.
    orphan_history = ConversationHistory()
    for i in range(20):
        orphan_history.append_user_message(f"u{i} " + "y" * 60)
    parent_call = ToolCall.create("read_file", {"path": "big.txt"})
    orphan_history.append_assistant_message(content=None, tool_calls=[parent_call])
    orphan_history.append_tool_result(parent_call.id, "read_file", "z" * 400)
    orphan_history.append_assistant_message(content="selesai")
    pruned = orphan_history.compile_chat_messages(max_tokens=30)
    print(f"pruned first role -> {pruned[0].role}")
    assert pruned[0].role != "tool", "tool result yatim di awal harus dibuang"
    assert pruned[-1].role == "assistant"
    print("OK: tidak ada tool result yatim setelah pemangkasan")

    # 8) Validasi skema dasar.
    try:
        ChatMessage(role="invalid", content="x")
        print("[ERROR] role invalid seharusnya ditolak")
        return 1
    except ValueError:
        print("OK: role tidak valid ditolak")
    try:
        ChatMessage(role="tool", content="x")  # tanpa tool_call_id
        print("[ERROR] tool tanpa tool_call_id seharusnya ditolak")
        return 1
    except ValueError:
        print("OK: pesan tool tanpa tool_call_id ditolak")

    print()
    print("[OK] ConversationHistory + tipe percakapan Native Tool Calling bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
