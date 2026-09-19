"""ConversationHistory: pengelola riwayat percakapan Native Tool Calling.

Menyimpan urutan `ChatMessage` dan mengubahnya menjadi format provider
(`messages`). Mendukung pola multi-turn tool calling:

    user -> assistant(tool_calls) -> tool(tool_call_id) -> assistant(final)

Prinsip:
    - Hasil tool SELALU disimpan sebagai pesan role "tool" (bukan "user"),
      dikaitkan lewat `tool_call_id`. Tidak ada tool result yang disisipkan
      sebagai pesan user.
    - `to_provider_format()` menghasilkan list dict sesuai skema provider
      (OpenAI Chat Completions) dan aman dikirim ke API.
    - `compile_chat_messages()` menyediakan token management sederhana:
      memangkas riwayat paling lama bila melebihi anggaran, sambil
      mempertahankan pesan system dan integritas pasangan
      assistant(tool_calls)/tool.

Modul ini murni struktur data (tidak memanggil provider maupun tool).
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from agent_ai.core.types import (
    ChatMessage,
    ChatRole,
    ToolCall,
    ToolResultPayload,
    _serialize_arguments,
)


class ConversationHistory:
    """Riwayat percakapan berurutan dalam bentuk `ChatMessage`.

    Args:
        messages: pesan awal (opsional) untuk pra-isi riwayat, mis. system
            prompt atau riwayat percakapan yang dilanjutkan.
    """

    def __init__(self, messages: Optional[List[ChatMessage]] = None) -> None:
        self._messages: List[ChatMessage] = list(messages or [])

    # ------------------------------------------------------------------ #
    # Container protocol & introspection
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[ChatMessage]:
        return iter(self._messages)

    @property
    def messages(self) -> List[ChatMessage]:
        """Salinan daftar pesan (tidak membocorkan list internal)."""
        return list(self._messages)

    @property
    def last_message(self) -> Optional[ChatMessage]:
        """Pesan terakhir, atau None bila riwayat kosong."""
        return self._messages[-1] if self._messages else None

    def clear(self) -> None:
        """Kosongkan riwayat."""
        self._messages.clear()

    # ------------------------------------------------------------------ #
    # Append helpers
    # ------------------------------------------------------------------ #
    def append_message(self, message: ChatMessage) -> ChatMessage:
        """Tambahkan satu ChatMessage apa adanya ke akhir riwayat."""
        if not isinstance(message, ChatMessage):
            raise TypeError(
                f"message harus ChatMessage, bukan {type(message).__name__}"
            )
        self._messages.append(message)
        return message

    def append_system_message(self, content: str) -> ChatMessage:
        """Tambahkan pesan system (instruksi/konteks)."""
        return self.append_message(ChatMessage(role=ChatRole.SYSTEM.value, content=content))

    def append_user_message(
        self, content: str, parts: Optional[List[Dict[str, Any]]] = None
    ) -> ChatMessage:
        """Tambahkan pesan user (permintaan/observasi manusia).

        Args:
            content: teks pesan user.
            parts: content blocks opsional (mis. image, format internal AETHER).
                Kosong (default) = pesan text-only seperti sebelumnya.
        """
        return self.append_message(
            ChatMessage(role=ChatRole.USER.value, content=content, parts=parts)
        )

    def append_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[Any]] = None,
        name: Optional[str] = None,
    ) -> ChatMessage:
        """Tambahkan pesan assistant, opsional dengan daftar tool_calls.

        Args:
            content: teks assistant (boleh None bila hanya memanggil tool).
            tool_calls: daftar ToolCall atau dict bentuk provider.
            name: nama opsional.

        Returns:
            ChatMessage yang ditambahkan.
        """
        return self.append_message(
            ChatMessage(
                role=ChatRole.ASSISTANT.value,
                content=content,
                tool_calls=tool_calls,
                name=name,
            )
        )

    def append_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> ChatMessage:
        """Tambahkan hasil tool sebagai pesan role "tool".

        Pesan ini dikaitkan ke tool call lewat `tool_call_id` (bukan disisipkan
        sebagai role "user"), sesuai kontrak Native Tool Calling.

        Args:
            tool_call_id: id ToolCall yang dijawab.
            tool_name: nama tool yang dieksekusi.
            content: teks hasil tool (atau pesan error pada kegagalan).
        """
        return self.append_message(
            ChatMessage(
                role=ChatRole.TOOL.value,
                content=content,
                name=tool_name,
                tool_call_id=tool_call_id,
            )
        )

    def append_tool_result_payload(self, payload: ToolResultPayload) -> ChatMessage:
        """Tambahkan hasil tool dari sebuah ToolResultPayload."""
        return self.append_tool_result(
            tool_call_id=payload.tool_call_id,
            tool_name=payload.tool_name,
            content=payload.to_content(),
        )

    # ------------------------------------------------------------------ #
    # Provider format
    # ------------------------------------------------------------------ #
    def to_provider_format(self) -> List[Dict[str, Any]]:
        """Ubah riwayat menjadi `messages` siap kirim ke provider.

        Returns:
            List dict sesuai skema OpenAI Chat Completions, termasuk
            `tool_calls` pada pesan assistant dan `tool_call_id` pada pesan
            role "tool".
        """
        return [message.to_provider_dict() for message in self._messages]

    def to_dict(self) -> List[Dict[str, Any]]:
        """Representasi internal seluruh pesan (untuk audit/debug)."""
        return [message.to_dict() for message in self._messages]

    # ------------------------------------------------------------------ #
    # Token management / pruning
    # ------------------------------------------------------------------ #
    @staticmethod
    def _estimate_tokens(message: ChatMessage) -> int:
        """Estimasi kasar jumlah token satu pesan (panjang/4 + overhead).

        Heuristik sederhana tanpa tokenizer eksternal: cukup untuk memutuskan
        pemangkasan riwayat, bukan untuk penagihan token presisi.
        """
        tokens = max(1, len(message.content or "") // 4)
        for tool_call in message.tool_calls or []:
            tokens += max(1, len(_serialize_arguments(tool_call.arguments)) // 4)
            tokens += len(tool_call.name) // 4
        tokens += len(message.name or "") // 4
        tokens += 4  # overhead per pesan (role/tag/pemisah)
        return tokens

    @staticmethod
    def _drop_orphan_leading_tools(messages: List[ChatMessage]) -> List[ChatMessage]:
        """Buang pesan role "tool" di awal yang kehilangan induknya.

        Setelah pemangkasan dari depan, sebuah tool result bisa berada di awal
        tanpa didahului assistant(tool_calls) yang memanggilnya. Pesan seperti
        itu tidak valid untuk provider, jadi dibuang sampai pesan pertama bukan
        lagi role "tool".
        """
        index = 0
        while index < len(messages) and messages[index].role == ChatRole.TOOL.value:
            index += 1
        return messages[index:]

    def compile_chat_messages(
        self,
        max_tokens: Optional[int] = None,
    ) -> List[ChatMessage]:
        """Kembalikan riwayat yang dipangkas agar muat dalam anggaran token.

        Strategi sederhana (bounded):
            1. Bila `max_tokens` None -> kembalikan seluruh riwayat.
            2. Pesan system di depan SELALU dipertahankan.
            3. Pesan paling lama dibuang lebih dulu sampai muat.
            4. Integritas pasangan tool_call/tool dijaga: tool result di awal
               yang kehilangan assistant(tool_calls) induknya ikut dibuang.

        Args:
            max_tokens: anggaran token perkiraan. None = tanpa pemangkasan.

        Returns:
            Daftar ChatMessage (subset riwayat asli, urutan tetap).
        """
        if max_tokens is None:
            return list(self._messages)

        # 1) Pisahkan pesan system di depan (selalu dipertahankan).
        head: List[ChatMessage] = []
        index = 0
        while (
            index < len(self._messages)
            and self._messages[index].role == ChatRole.SYSTEM.value
        ):
            head.append(self._messages[index])
            index += 1
        body = self._messages[index:]

        if max_tokens <= 0:
            return head

        budget = max_tokens - sum(self._estimate_tokens(m) for m in head)
        if budget <= 0:
            return head

        # 2) Ambil dari yang paling baru sampai anggaran habis.
        kept_reversed: List[ChatMessage] = []
        used = 0
        for message in reversed(body):
            cost = self._estimate_tokens(message)
            if used + cost > budget:
                break
            kept_reversed.append(message)
            used += cost

        kept = list(reversed(kept_reversed))
        # 3) Buang tool result yatim di batas pemangkasan.
        kept = self._drop_orphan_leading_tools(kept)
        return head + kept
