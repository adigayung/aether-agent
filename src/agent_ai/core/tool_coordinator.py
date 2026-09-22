"""Tool Execution Coordinator: eksekusi beberapa ToolCall dalam satu iteration.

Modul ini MENGOPTIMALKAN CARA eksekusi, bukan menentukan pekerjaan. LLM tetap
memilih tool apa yang dipanggil; coordinator hanya memutuskan tool mana yang
AMAN dijalankan bersamaan dan mengembalikan hasil ke LLM dalam urutan
deterministik (urutan tool call pada response LLM).

Alur:

    LLM response
        ↓
    tool_calls[]
        ↓
    ToolExecutionCoordinator
        ├── classify (read / write / exclusive)
        ├── detect conflicts + dependency
        ├── build execution groups
        └── jalankan group demi group (paralel di dalam group)
        ↓
    ToolResultPayload[]  (urutan = urutan input)
        ↓
    LLM

Klasifikasi & aturan konservatif (safety first):

    READ       : baca/inspeksi -> aman paralel (tidak mengubah state).
    WRITE      : tulis/edit/hapus/pindah -> paralel HANYA bila target resource
                 berbeda. Target sama -> dipisah ke group berbeda (sequential).
    EXCLUSIVE  : run_command / tool tak dikenal / tool tulis tanpa target yang
                 bisa dipastikan -> SELALU dijalankan sendiri (barrier). AETHER
                 tidak menebak dependency antar command: bila ragu -> sequential.

Dependency failure: bila operasi mutasi/exclusive SEBELUMNYA gagal, operasi
EXCLUSIVE berikutnya di-BLOCK (tidak dijalankan) dan hasilnya berupa error
payload "Blocked: ...".

Prinsip urutan: hasil selalu dipetakan kembali lewat tool_call id dan
dikembalikan pada URUTAN INPUT, bukan urutan selesai (completion order).

Tidak ada planner/decision-making baru di sini: hanya pembatasan eksekusi.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence

from agent_ai.core.types import (
    ToolCall,
    ToolResultPayload,
    ToolResultStatus,
    parse_tool_arguments,
)
from agent_ai.permission.classifier import ActionClassifier
from agent_ai.permission.models import ActionClass

#: Batas jumlah tool yang dieksekusi bersamaan dalam satu group.
#: Bukan setting UI: konstanta internal (dapat dioverride lewat konstruktor
#: ToolExecutionCoordinator untuk kebutuhan khusus/verifikasi).
MAX_PARALLEL_TOOLS = 4

#: Nilai path yang dianggap "root"/tidak spesifik sehingga tidak dijadikan
#: resource key (mis. search_code yang default ke "." tidak boleh menyerialkan
#: semua pembacaan).
_TRIVIAL_PATHS = {"", ".", "./", ".\\", "/", "\\"}

#: Field argumen yang menunjuk path sebagai RUANG BACA.
_READ_PATH_FIELDS = ("path", "file", "filename")

#: Field argumen yang menunjuk path sebagai RUANG TULIS.
_WRITE_PATH_FIELDS = ("path", "file", "filename", "source", "destination")

#: Callback opsional: dipanggil sebelum satu tool call dimulai (emit event).
StartCallback = Callable[[ToolCall], None]
#: Callback opsional: dipanggil setelah satu tool call selesai/di-block.
CompleteCallback = Callable[[ToolCall, ToolResultPayload], None]
#: Callback opsional: True bila pembatalan (stop) sudah diminta.
CancelCheck = Callable[[], bool]
#: Callback eksekusi satu ToolCall -> ToolResultPayload (biasanya
#: ToolExecutor.execute_tool_call). Tidak boleh melempar exception.
RunOne = Callable[[ToolCall], ToolResultPayload]


class ToolCategory(str, Enum):
    """Kategori eksekusi tool (untuk keputusan paralel/serial)."""

    READ = "read"            # read-only, aman paralel
    WRITE = "write"          # mengubah file, paralel bila target berbeda
    EXCLUSIVE = "exclusive"  # barrier: selalu sendiri (command/tak dikenal)


@dataclass(frozen=True)
class PlannedCall:
    """Rencana eksekusi satu ToolCall (klasifikasi + resource)."""

    index: int
    tool_call: ToolCall
    category: ToolCategory
    action_class: ActionClass
    arguments: Dict[str, Any] = field(default_factory=dict)
    read_keys: FrozenSet[str] = frozenset()
    write_keys: FrozenSet[str] = frozenset()

    @property
    def touches_state(self) -> bool:
        """True bila call ini berpotensi mengubah/memengaruhi state global."""
        return self.category in (ToolCategory.WRITE, ToolCategory.EXCLUSIVE)


@dataclass
class ToolBatchResult:
    """Hasil eksekusi batch ToolCall.

    Attributes:
        payloads: hasil per tool call, TERALIGNASI dengan urutan input
            (None = tidak dieksekusi, mis. karena pembatalan).
        cancelled: True bila batch dihentikan karena pembatalan (stop).
    """

    payloads: List[Optional[ToolResultPayload]] = field(default_factory=list)
    cancelled: bool = False

    def ordered_payloads(self) -> List[Optional[ToolResultPayload]]:
        """Salinan payload sesuai urutan input."""
        return list(self.payloads)


# --------------------------------------------------------------------------- #
# Klasifikasi & resource
# --------------------------------------------------------------------------- #
def _normalize_path(value: Any) -> str:
    """Normalisasi path menjadi key resource yang stabil (case-aware OS)."""
    text = str(value).strip()
    try:
        text = os.path.normcase(os.path.normpath(text))
    except (TypeError, ValueError):
        pass
    return text.replace("\\", "/")


def _extract_keys(arguments: Dict[str, Any], fields: Sequence[str]) -> FrozenSet[str]:
    """Ekstrak resource path dari argumen (abaikan nilai trivial)."""
    keys = set()
    for name in fields:
        value = arguments.get(name)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and stripped not in _TRIVIAL_PATHS:
                keys.add(_normalize_path(stripped))
    return frozenset(keys)


def classify_category(
    action_class: ActionClass, write_keys: FrozenSet[str]
) -> ToolCategory:
    """Tentukan kategori eksekusi dari ActionClass + resource tulis.

    Aturan konservatif: tulis tanpa target yang bisa dipastikan diperlakukan
    sebagai EXCLUSIVE (barrier) karena independensinya tidak bisa dibuktikan.
    """
    if action_class == ActionClass.READ_ONLY:
        return ToolCategory.READ
    if action_class in (ActionClass.WORKSPACE_WRITE, ActionClass.DELETE_MOVE):
        return ToolCategory.WRITE if write_keys else ToolCategory.EXCLUSIVE
    # COMMAND_EXECUTION / EXTERNAL_NETWORK / UNKNOWN -> barrier.
    return ToolCategory.EXCLUSIVE


def build_plan(
    tool_calls: Sequence[ToolCall],
    classifier: Optional[ActionClassifier] = None,
) -> List[PlannedCall]:
    """Bangun rencana eksekusi (klasifikasi + resource) untuk setiap ToolCall."""
    active_classifier = classifier or ActionClassifier()
    plans: List[PlannedCall] = []
    for index, tool_call in enumerate(tool_calls):
        arguments, parse_error = parse_tool_arguments(tool_call.arguments)
        if parse_error is not None:
            # Argumen tidak bisa diparse -> klasifikasi aman (barrier).
            arguments = {}
        action_class = active_classifier.classify(tool_call.name, arguments)

        write_keys: FrozenSet[str] = frozenset()
        read_keys: FrozenSet[str] = frozenset()
        if action_class in (ActionClass.WORKSPACE_WRITE, ActionClass.DELETE_MOVE):
            write_keys = _extract_keys(arguments, _WRITE_PATH_FIELDS)
        elif action_class == ActionClass.READ_ONLY:
            read_keys = _extract_keys(arguments, _READ_PATH_FIELDS)

        category = classify_category(action_class, write_keys)
        plans.append(
            PlannedCall(
                index=index,
                tool_call=tool_call,
                category=category,
                action_class=action_class,
                arguments=arguments,
                read_keys=read_keys,
                write_keys=write_keys,
            )
        )
    return plans


def calls_conflict(first: PlannedCall, second: PlannedCall) -> bool:
    """True bila dua call TIDAK aman dijalankan bersamaan."""
    if (
        first.category is ToolCategory.EXCLUSIVE
        or second.category is ToolCategory.EXCLUSIVE
    ):
        return True
    # Tulis vs tulis pada resource sama, atau tulis vs baca pada resource sama.
    if first.write_keys & (second.write_keys | second.read_keys):
        return True
    if second.write_keys & first.read_keys:
        return True
    return False


def plan_groups(plans: Sequence[PlannedCall]) -> List[List[int]]:
    """Kelompokkan index plan menjadi group yang dieksekusi berurutan.

    Di dalam satu group, seluruh call aman dijalankan bersamaan. Antar group
    dijalankan berurutan. Call yang BERKONFLIK selalu ditempatkan pada group
    yang lebih belakang, sehingga urutan input untuk operasi yang saling
    bergantung tetap terjaga (dependency tidak dilanggar).

    Algoritma: list-scheduling "earliest compatible group" dengan batas bawah
    (lower bound) = 1 + group terakhir dari setiap conflict sebelumnya.
    """
    by_index: Dict[int, PlannedCall] = {plan.index: plan for plan in plans}
    ordered = sorted(plans, key=lambda plan: plan.index)
    groups: List[List[int]] = []
    group_of: Dict[int, int] = {}
    for plan in ordered:
        lower_bound = 0
        for earlier in ordered:
            if earlier.index >= plan.index:
                break
            if calls_conflict(earlier, plan):
                lower_bound = max(lower_bound, group_of[earlier.index] + 1)
        target: Optional[int] = None
        for group_index in range(lower_bound, len(groups)):
            if all(
                not calls_conflict(by_index[j], plan) for j in groups[group_index]
            ):
                target = group_index
                break
        if target is None:
            groups.append([plan.index])
            group_of[plan.index] = len(groups) - 1
        else:
            groups[target].append(plan.index)
            group_of[plan.index] = target
    return groups


# --------------------------------------------------------------------------- #
# Coordinator
# --------------------------------------------------------------------------- #
class ToolExecutionCoordinator:
    """Eksekutor batch ToolCall: sequential/parallel sesuai klasifikasi.

    Args:
        max_parallel: batas jumlah tool paralel dalam satu group
            (default MAX_PARALLEL_TOOLS).
        classifier: ActionClassifier opsional (default instance baru).
    """

    def __init__(
        self,
        *,
        max_parallel: Optional[int] = None,
        classifier: Optional[ActionClassifier] = None,
    ) -> None:
        if max_parallel is None:
            self.max_parallel = MAX_PARALLEL_TOOLS
        else:
            self.max_parallel = max(1, int(max_parallel))
        self.classifier = classifier or ActionClassifier()

    # ------------------------------------------------------------------ #
    # Planning (dipisah agar bisa diverifikasi tanpa eksekusi)
    # ------------------------------------------------------------------ #
    def plan(self, tool_calls: Sequence[ToolCall]) -> List[PlannedCall]:
        """Bangun rencana eksekusi untuk daftar tool call."""
        return build_plan(tool_calls, self.classifier)

    # ------------------------------------------------------------------ #
    # Eksekusi
    # ------------------------------------------------------------------ #
    def execute(
        self,
        tool_calls: Sequence[ToolCall],
        run_one: RunOne,
        *,
        on_start: Optional[StartCallback] = None,
        on_complete: Optional[CompleteCallback] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> ToolBatchResult:
        """Eksekusi batch ToolCall dan kembalikan hasil TERURUT sesuai input.

        Args:
            tool_calls: daftar ToolCall (urutan = urutan dari LLM).
            run_one: callable `ToolCall -> ToolResultPayload` untuk satu call.
            on_start: callback sebelum satu call dimulai (emit event).
            on_complete: callback setelah satu call selesai/di-block.
            cancel_check: callable -> bool; bila True, call berikutnya tidak
                dimulai (perilaku safe boundary yang sama dengan sebelumnya).

        Returns:
            ToolBatchResult dengan payload terurut sesuai input.
        """
        calls = list(tool_calls)
        payloads: List[Optional[ToolResultPayload]] = [None] * len(calls)
        if not calls:
            return ToolBatchResult(payloads=payloads)

        plans = self.plan(calls)
        groups = plan_groups(plans)
        cancelled = False

        def _cancelled() -> bool:
            return bool(cancel_check()) if cancel_check is not None else False

        def _safe_run(call: ToolCall) -> ToolResultPayload:
            try:
                return run_one(call)
            except Exception as exc:  # noqa: BLE001 - jangan putuskan batch
                return ToolResultPayload.error(
                    call.id, call.name, f"{type(exc).__name__}: {exc}"
                )

        for group_index, group in enumerate(groups):
            # Cancel dicek sebelum memulai group baru (safe boundary).
            if _cancelled():
                cancelled = True
                break

            # Dependency failure: operasi EXCLUSIVE di-block bila mutasi /
            # exclusive sebelumnya gagal. (EXCLUSIVE selalu group tunggal.)
            prior_failure = self._prior_failure(plans, groups, group_index, payloads)
            is_exclusive_group = plans[group[0]].category is ToolCategory.EXCLUSIVE
            blocked = group if (prior_failure and is_exclusive_group) else []

            started: List[int] = []
            for index in group:
                if _cancelled():
                    cancelled = True
                    break
                if on_start is not None:
                    on_start(calls[index])
                if index in blocked:
                    payloads[index] = ToolResultPayload.error(
                        calls[index].id,
                        calls[index].name,
                        "Blocked: dependency sebelumnya gagal dieksekusi; "
                        "tool tidak dijalankan.",
                    )
                else:
                    started.append(index)

            # Jalankan call yang sudah dimulai (juga bila cancel datang di
            # tengah group: yang sudah start tetap diselesaikan).
            if len(started) == 1:
                payloads[started[0]] = _safe_run(calls[started[0]])
            elif len(started) > 1:
                workers = min(self.max_parallel, len(started))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(_safe_run, calls[i]) for i in started]
                    for index, future in zip(started, futures):
                        payloads[index] = future.result()

            # Event selesai SELALU diemit dari thread pemanggil (deterministik),
            # urut sesuai input group.
            if on_complete is not None:
                for index in group:
                    payload = payloads[index]
                    if payload is not None:
                        on_complete(calls[index], payload)

            if cancelled:
                break

        return ToolBatchResult(payloads=payloads, cancelled=cancelled)

    # ------------------------------------------------------------------ #
    # Dependency blocking
    # ------------------------------------------------------------------ #
    @staticmethod
    def _prior_failure(
        plans: Sequence[PlannedCall],
        groups: Sequence[Sequence[int]],
        group_index: int,
        payloads: Sequence[Optional[ToolResultPayload]],
    ) -> bool:
        """True bila ada operasi stateful di group sebelumnya yang gagal."""
        for earlier_group in groups[:group_index]:
            for index in earlier_group:
                payload = payloads[index]
                if payload is None:
                    continue
                if payload.status == ToolResultStatus.ERROR and plans[index].touches_state:
                    return True
        return False
