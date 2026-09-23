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
    - `compile_compacted_messages()` menyediakan runtime context compaction:
      mempertahankan SEMUA pesan tetapi MEMADATKAN konten pesan lama secara
      DETERMINISTIK (tanpa LLM), sehingga riwayat yang dikirim setiap round
      tidak tumbuh tanpa batas namun protokol tool calling tetap valid dan
      informasi penting tetap dapat diambil ulang lewat tool yang ada.
    - Untuk pesan role "tool", isi dipadatkan secara TERSTRUKTUR per tool
      (ToolResultCompactor) sehingga informasi penting (path, range/symbol,
      query, command/exit_code, error) dan locator retrieval tetap terjaga.
      Tool result TERBARU tetap utuh; hanya yang STALE yang dipadatkan.

Modul ini murni struktur data (tidak memanggil provider maupun tool).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

from agent_ai.core.types import (
    ChatMessage,
    ChatRole,
    ToolCall,
    ToolResultPayload,
    _serialize_arguments,
)

#: Jumlah pesan TERAKHIR yang selalu dipertahankan UTUH (belum dipadatkan)
#: saat kompilasi konteks continuous loop. Beberapa turn tool terakhir adalah
#: konteks paling relevan untuk melanjutkan pekerjaan.
_DEFAULT_KEEP_RECENT_MESSAGES = 8

#: Batas karakter cuplikan AWAL sebuah konten pesan lama yang dipadatkan.
_COMPACT_HEAD_CHARS = 400

#: Batas karakter cuplikan AKHIR sebuah konten pesan lama (menjaga pesan
#: error/ringkasan yang sering berada di akhir output tool tetap terlihat).
_COMPACT_TAIL_CHARS = 200

#: Batas panjang satu nilai string argumen tool-call sebelum dipadatkan.
_COMPACT_ARG_CHARS = 400

#: Penanda deterministik bahwa konten telah dipadatkan (tanpa LLM).
_COMPACT_MARKER = "[dipadatkan]"

#: Petunjuk retrieval yang disertakan saat memadatkan (bukan instruksi baru,
#: hanya penanda bahwa detail dapat diambil ulang lewat tool yang ada).
_COMPACT_RETRIEVAL_HINT = "ambil ulang via tool bila perlu"


def _digest_text(
    text: Optional[str], head_chars: int, tail_chars: int
) -> Optional[str]:
    """Padatkan teks panjang secara DETERMINISTIK: cuplikan awal + akhir.

    Teks pendek dikembalikan APA ADANYA. Teks panjang dipotong menjadi
    `head_chars` (awal) + penanda + `tail_chars` (akhir), sehingga informasi
    paling awal (mis. path/status) dan paling akhir (mis. error/ringkasan)
    tetap terlihat. Tidak ada panggilan LLM: murni pemotongan string.
    """
    if text is None:
        return None
    head_chars = max(int(head_chars), 0)
    tail_chars = max(int(tail_chars), 0)
    if head_chars + tail_chars <= 0 or len(text) <= head_chars + tail_chars:
        return text
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip() if tail_chars > 0 else ""
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}\n… {_COMPACT_MARKER} {omitted} karakter dipotong "
        f"({_COMPACT_RETRIEVAL_HINT}) …\n{tail}"
    )


def _compact_arg_value(value: Any, limit: int) -> Any:
    """Padatkan nilai argumen tool-call (rekursif; hanya string panjang)."""
    if isinstance(value, str):
        return _digest_text(value, limit, 0)
    if isinstance(value, dict):
        return {key: _compact_arg_value(val, limit) for key, val in value.items()}
    if isinstance(value, list):
        return [_compact_arg_value(item, limit) for item in value]
    return value


def _compact_arguments(arguments: Any, limit: int) -> Any:
    """Padatkan argumen tool-call (dict atau JSON string) tanpa mengubah tipe.

    Nilai scalar (path/query/command/line) TIDAK dipotong; hanya string sangat
    panjang (mis. isi file yang ditulis) yang dipadatkan. Tipe input
    dipertahankan (dict tetap dict, JSON string tetap JSON string) agar bentuk
    provider-format tidak berubah.
    """
    if isinstance(arguments, dict):
        return {key: _compact_arg_value(val, limit) for key, val in arguments.items()}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return _digest_text(arguments, limit, 0)
        if isinstance(parsed, dict):
            compacted = {
                key: _compact_arg_value(val, limit) for key, val in parsed.items()
            }
            return json.dumps(compacted, ensure_ascii=False, default=str)
        return arguments
    return arguments


#: Cache ToolResultCompactor (lazy import; fallback aman bila tidak tersedia).
_TOOL_COMPACTOR: Any = None
_TOOL_COMPACTOR_RESOLVED = False


def _get_tool_compactor() -> Any:
    """Ambil `ToolResultCompactor` (contextbudget) secara lazy + aman.

    Import ditunda agar modul core ini tidak memuat paket contextbudget saat
    diimpor, dan bila import gagal (mis. lingkungan minimal), compaction jatuh
    ke pemadatan head/tail generik (perilaku Task 1) tanpa menggagalkan task.
    """
    global _TOOL_COMPACTOR, _TOOL_COMPACTOR_RESOLVED
    if not _TOOL_COMPACTOR_RESOLVED:
        _TOOL_COMPACTOR_RESOLVED = True
        try:
            from agent_ai.contextbudget.tool_compaction import ToolResultCompactor

            _TOOL_COMPACTOR = ToolResultCompactor()
        except Exception:  # noqa: BLE001 - fallback aman, jangan gagalkan task
            _TOOL_COMPACTOR = None
    return _TOOL_COMPACTOR


def _compact_tool_content(
    tool_name: str,
    content: Optional[str],
    head_chars: int,
    tail_chars: int,
) -> Optional[str]:
    """Padatkan ISI hasil tool secara TERSTRUKTUR (fallback head/tail).

    Representasi ringkas mempertahankan informasi penting per tool (path,
    range/symbol, query, command/exit_code, error, dst) sehingga Agent tetap
    dapat melanjutkan pekerjaan dan dapat MENGAMBIL ULANG detail lewat tool
    yang sudah ada. Hanya ISI yang berubah: `role`/`tool_call_id`/`name` tetap
    dipegang pemanggil, sehingga protokol tool calling tetap valid.
    """
    if content is None:
        return None
    compactor = _get_tool_compactor()
    if compactor is None:
        return _digest_text(content, head_chars, tail_chars)
    try:
        return compactor.compact(
            tool_name or "", content, head_chars=head_chars, tail_chars=tail_chars
        )
    except Exception:  # noqa: BLE001 - compaction tidak boleh menggagalkan task
        return _digest_text(content, head_chars, tail_chars)


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

    # ------------------------------------------------------------------ #
    # Compacted provider format (runtime context compaction)
    # ------------------------------------------------------------------ #
    @staticmethod
    def estimate_messages_tokens(messages: List[ChatMessage]) -> int:
        """Estimasi total token sebuah daftar pesan (estimator heuristik sama).

        Memakai estimator internal yang SAMA dengan `compile_chat_messages`
        (tanpa tokenizer eksternal), sehingga keputusan compaction konsisten
        dengan mekanisme budget yang sudah ada.
        """
        return sum(ConversationHistory._estimate_tokens(m) for m in messages)

    def estimate_tokens(self) -> int:
        """Estimasi token SELURUH riwayat saat ini."""
        return ConversationHistory.estimate_messages_tokens(self._messages)

    @staticmethod
    def _split_protected_head(
        messages: List[ChatMessage],
    ) -> Tuple[List[ChatMessage], List[ChatMessage]]:
        """Pisahkan bagian yang SELALU dipertahankan utuh (system + task).

        Kepala = seluruh pesan system di depan + SATU pesan user pertama (task
        awal). Instruksi system/knowledge dan task TIDAK pernah dipadatkan.
        """
        index = 0
        while index < len(messages) and messages[index].role == ChatRole.SYSTEM.value:
            index += 1
        head = list(messages[:index])
        rest = list(messages[index:])
        if rest and rest[0].role == ChatRole.USER.value:
            head.append(rest[0])
            rest = rest[1:]
        return head, rest

    @staticmethod
    def _compact_message(
        message: ChatMessage,
        head_chars: int,
        tail_chars: int,
        arg_chars: int,
    ) -> ChatMessage:
        """Buat SALINAN pesan yang kontennya dipadatkan (riwayat asli utuh).

        Pasangan `assistant(tool_calls)` <-> `tool(tool_call_id)` tetap utuh:
        hanya isi (content/argument) yang dipadatkan, bukan pesannya. Untuk
        pesan role "tool", isi dipadatkan secara TERSTRUKTUR (per-tool, locator
        dipertahankan); pesan lain memakai pemadatan head/tail generik.
        """
        tool_calls: Optional[List[ToolCall]] = None
        if message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tool_call.id,
                    function={
                        "name": tool_call.name,
                        "arguments": _compact_arguments(
                            tool_call.arguments, arg_chars
                        ),
                    },
                    type=tool_call.type,
                )
                for tool_call in message.tool_calls
            ]
        if message.role == ChatRole.TOOL.value:
            content = _compact_tool_content(
                message.name or "", message.content, head_chars, tail_chars
            )
        else:
            content = _digest_text(message.content, head_chars, tail_chars)
        return ChatMessage(
            role=message.role,
            content=content,
            tool_calls=tool_calls,
            name=message.name,
            tool_call_id=message.tool_call_id,
            parts=list(message.parts) if message.parts else None,
        )

    @staticmethod
    def tool_compaction_stats(
        original: List[ChatMessage], compiled: List[ChatMessage]
    ) -> Dict[str, int]:
        """Statistik ringkas compaction hasil tool (untuk observability/benchmark).

        Matching lewat `tool_call_id` (robust terhadap pembuangan pesan paling
        lama). Tidak menyentuh isi konten, hanya menghitung.

        Returns:
            dict berisi: tool_results, tool_compacted, tool_raw_chars,
            tool_compacted_chars.
        """
        original_by_id: Dict[str, Optional[str]] = {
            m.tool_call_id: m.content
            for m in original
            if m.role == ChatRole.TOOL.value and m.tool_call_id
        }
        tool_results = 0
        tool_compacted = 0
        raw_chars = 0
        compacted_chars = 0
        for message in compiled:
            if message.role != ChatRole.TOOL.value:
                continue
            tool_results += 1
            before = original_by_id.get(message.tool_call_id)
            if before is None:
                continue
            raw_chars += len(before)
            compacted_chars += len(message.content or "")
            if (message.content or "") != before:
                tool_compacted += 1
        return {
            "tool_results": tool_results,
            "tool_compacted": tool_compacted,
            "tool_raw_chars": raw_chars,
            "tool_compacted_chars": compacted_chars,
        }

    def compile_compacted_messages(
        self,
        max_tokens: Optional[int],
        *,
        overhead_tokens: int = 0,
        keep_recent: int = _DEFAULT_KEEP_RECENT_MESSAGES,
        head_chars: int = _COMPACT_HEAD_CHARS,
        tail_chars: int = _COMPACT_TAIL_CHARS,
        arg_chars: int = _COMPACT_ARG_CHARS,
    ) -> List[ChatMessage]:
        """Kompilasi riwayat agar MUAT dalam anggaran token (deterministik).

        Berbeda dari `compile_chat_messages()` (yang MEMBUANG pesan paling lama),
        method ini MEMPERTAHANKAN seluruh pesan tetapi MEMADATKAN konten pesan
        lama. Karena itu:
            - pasangan `assistant(tool_calls)` <-> `tool(tool_call_id)` tetap
              utuh sehingga protokol tool calling tetap valid;
            - informasi penting (nama tool, argumen scalar, cuplikan awal/akhir
              output, status/error) tetap terlihat Agent;
            - detail mentah yang dipotong dapat diambil ulang lewat tool yang
              sudah ada (read_file/search_code/run_command).

        Urutan langkah (deterministik, tanpa LLM):
            1. Bila `max_tokens` None atau estimasi sudah <= budget ->
               kembalikan riwayat APA ADANYA (task pendek tidak berubah).
            2. Kepala (system + task) SELALU dipertahankan utuh.
            3. `keep_recent` pesan terakhir dipertahankan utuh.
            4. Pesan lama dipadatkan sampai muat.
            5. Bila masih lebih, jendela terbaru ikut dipadatkan dari yang
               PALING LAMA; pesan TERAKHIR tidak pernah dipadatkan.
            6. Upaya terakhir: buang pesan PALING LAMA (per grup, menjaga
               pasangan tool) sampai muat.

        Args:
            max_tokens: anggaran token untuk seluruh daftar pesan yang
                dikembalikan (termasuk kepala). None = tanpa batas.
            overhead_tokens: token non-pesan yang ikut satu request (mis.
                definisi tool + reserve output); dikurangkan dari `max_tokens`.
            keep_recent: jumlah pesan terakhir yang selalu utuh.
            head_chars/tail_chars: ukuran cuplikan saat memadatkan.
            arg_chars: batas nilai string argumen tool-call.

        Returns:
            List ChatMessage (kepala utuh + ekor padat/bounded), urutan terjaga.
        """
        messages = list(self._messages)
        if max_tokens is None:
            return messages
        budget = int(max_tokens) - max(int(overhead_tokens), 0)
        if self.estimate_messages_tokens(messages) <= budget:
            return messages

        head, tail = self._split_protected_head(messages)
        tail_budget = budget - self.estimate_messages_tokens(head)
        limit = max(tail_budget, 0)

        keep_recent = max(int(keep_recent), 0)
        recent_start = max(len(tail) - keep_recent, 0)
        compiled_tail: List[ChatMessage] = [
            self._compact_message(message, head_chars, tail_chars, arg_chars)
            if index < recent_start
            else message
            for index, message in enumerate(tail)
        ]

        # Tahap 5: padatkan jendela terbaru (dari yang PALING LAMA),
        # sisakan pesan TERAKHIR selalu utuh selama masih muat.
        index = recent_start
        while (
            self.estimate_messages_tokens(compiled_tail) > limit
            and index < len(compiled_tail) - 1
        ):
            compiled_tail[index] = self._compact_message(
                compiled_tail[index], head_chars, tail_chars, arg_chars
            )
            index += 1

        # Tahap 5b: bila MASIH lebih, padatkan juga pesan TERAKHIR (pesan tetap
        # ada -- hanya isinya dipadatkan) sebelum menempuh pembuangan.
        if compiled_tail and self.estimate_messages_tokens(compiled_tail) > limit:
            compiled_tail[-1] = self._compact_message(
                compiled_tail[-1], head_chars, tail_chars, arg_chars
            )

        # Tahap 6: upaya terakhir -- buang pesan PALING LAMA (per grup,
        # menjaga pasangan tool) sampai muat; sisakan minimal satu pesan.
        while (
            len(compiled_tail) > 1
            and self.estimate_messages_tokens(compiled_tail) > limit
        ):
            compiled_tail.pop(0)
            compiled_tail = self._drop_orphan_leading_tools(compiled_tail)

        return head + compiled_tail
