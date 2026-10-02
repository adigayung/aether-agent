"""Agent Execution Policy (Fast / Balanced / Deep) — preferensi STRATEGI kerja.

Modul ini menambah SATU konsep baru: *execution policy* Agent. Policy BUKAN
hard limit dan BUKAN pengambil keputusan: ia hanya metadata/strategi yang
menjelaskan preferensi kerja (exploration level, planning depth, verification
preference, context preference) untuk sebuah task.

    requested_mode -> mode yang dipilih user (metadata task)
    effective_mode -> mode yang benar-benar dipakai Agent setelah assessment

Keduanya dipisahkan karena task nyata dapat menuntut mode yang berbeda dari
pilihan awal user, mis.:

    requested_mode: fast
    effective_mode: deep

Escalation (``fast -> balanced -> deep``) disediakan sebagai MEKANISME:
Agent (LLM) yang memutuskan kapan perlu escalate dan dengan alasan apa
(--> ``ExecutionPolicyResolver.escalate``). Modul ini SENGAJA TIDAK memuat
heuristik seperti "ada kata refactor -> deep" atau "file banyak -> deep":
tidak ada keyword matcher, tidak ada scoring, tidak ada rule engine.

Batas modul (sesuai scope task):

- TIDAK mengubah keputusan loop LLM (loop/completion/reasoning tidak disentuh).
- TIDAK mengubah status task, TaskPhase, TaskStatus, maupun TERMINAL_STATUSES.
- TIDAK melakukan context routing (Atlas/Bible) dan TIDAK menentukan strategi
  verifikasi; ``verification_preference``/``context_preference`` hanya metadata
  yang disiapkan untuk task berikutnya.
- TIDAK ada persistence/settings baru: state policy hidup per-run di runtime.
- Backward compatible: tanpa mode eksplisit, default ``balanced`` (behavior
  lama tidak berubah karena policy tidak menggerakkan eksekusi).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple


class ExecutionMode(str, Enum):
    """Mode execution policy Agent (bukan status task, bukan hard limit)."""

    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


#: Nilai mode yang dipakai runtime (string, agar mudah diserialisasi).
MODE_FAST = ExecutionMode.FAST.value
MODE_BALANCED = ExecutionMode.BALANCED.value
MODE_DEEP = ExecutionMode.DEEP.value

#: Mode default bila task/user tidak memberi mode (backward compatible).
DEFAULT_MODE = MODE_BALANCED

#: Urutan escalation yang disarankan: fast -> balanced -> deep.
ESCALATION_ORDER: Tuple[str, ...] = (MODE_FAST, MODE_BALANCED, MODE_DEEP)

#: Alias nilai mode LAMA/asing -> mode policy kanonik.
#:
#: ``minimal`` adalah nilai retrieval profile TaskComposer AETHER yang sudah
#: ada ("minimal" | "balanced" | "deep"). Dipetakan ke ``fast`` agar mode dari
#: TaskComposer/metadata tetap diteruskan tanpa memaksa perubahan UI/konfigurasi.
MODE_ALIASES: Dict[str, str] = {
    "minimal": MODE_FAST,
}

#: Kunci metadata task yang boleh membawa mode policy (urutan prioritas).
MODE_METADATA_KEYS: Tuple[str, ...] = ("agent_mode", "policy_mode", "mode")


class PolicyError(Exception):
    """Base error execution policy."""


class PolicyEscalationError(PolicyError, ValueError):
    """Escalation tidak valid (mundur, mode tertinggi, atau mode tak dikenal)."""


@dataclass(frozen=True)
class ExecutionPolicy:
    """Metadata policy satu mode (informasi/strategi, bukan enforcement).

    Attributes:
        mode: nilai mode kanonik ("fast" | "balanced" | "deep").
        label: label ramah-user (mis. "Fast").
        exploration_level: preferensi kedalaman eksplorasi project.
        planning_depth: preferensi kedalaman perencanaan.
        verification_preference: preferensi verifikasi hasil kerja.
        context_preference: preferensi keluasan konteks.
        description: ringkasan singkat mode.
    """

    mode: str
    label: str
    exploration_level: str
    planning_depth: str
    verification_preference: str
    context_preference: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "label": self.label,
            "exploration_level": self.exploration_level,
            "planning_depth": self.planning_depth,
            "verification_preference": self.verification_preference,
            "context_preference": self.context_preference,
            "description": self.description,
        }


#: Registry policy per mode (SATU sumber kebenaran metadata policy).
POLICIES: Dict[str, ExecutionPolicy] = {
    MODE_FAST: ExecutionPolicy(
        mode=MODE_FAST,
        label="Fast",
        exploration_level="minimal",
        planning_depth="light",
        verification_preference="on_demand",
        context_preference="minimal",
        description=(
            "Preferensi kerja ringkas: sedikit eksplorasi, rencana singkat, "
            "verifikasi seperlunya, konteks minimal."
        ),
    ),
    MODE_BALANCED: ExecutionPolicy(
        mode=MODE_BALANCED,
        label="Balanced",
        exploration_level="moderate",
        planning_depth="standard",
        verification_preference="targeted",
        context_preference="balanced",
        description=(
            "Preferensi kerja seimbang (default AETHER): eksplorasi secukupnya, "
            "rencana standar, verifikasi terarah, konteks seimbang."
        ),
    ),
    MODE_DEEP: ExecutionPolicy(
        mode=MODE_DEEP,
        label="Deep",
        exploration_level="thorough",
        planning_depth="detailed",
        verification_preference="strong",
        context_preference="broad",
        description=(
            "Preferensi kerja mendalam: eksplorasi menyeluruh, rencana rinci, "
            "verifikasi kuat, konteks luas."
        ),
    ),
}


def available_modes() -> List[str]:
    """Daftar mode policy yang tersedia (urut escalation)."""
    return list(ESCALATION_ORDER)


def is_valid_mode(value: Any) -> bool:
    """True bila ``value`` (setelah normalisasi) adalah mode policy kanonik."""
    text = str(value).strip().lower() if value is not None else ""
    if not text:
        return False
    return (MODE_ALIASES.get(text, text)) in POLICIES


def normalize_mode(value: Any, default: str = DEFAULT_MODE) -> str:
    """Normalisasi nilai mode menjadi mode kanonik.

    Aturan (deterministik, tanpa menebak):

    - ``None``/kosong            -> ``default`` (default: balanced).
    - alias dikenal (mis. "minimal") -> mode kanonik padanannya.
    - mode valid (fast/balanced/deep, case-insensitive) -> mode tersebut.
    - nilai tak dikenal          -> ``default`` (TIDAK error; task lama tetap
      berjalan).

    Args:
        value: nilai mode mentah (str/Enum/None).
        default: mode fallback (harus mode kanonik).

    Returns:
        Mode kanonik ("fast" | "balanced" | "deep").
    """
    fallback = str(default).strip().lower()
    if fallback not in POLICIES:
        fallback = DEFAULT_MODE
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if not text:
        return fallback
    text = MODE_ALIASES.get(text, text)
    return text if text in POLICIES else fallback


def get_policy(mode: Any) -> ExecutionPolicy:
    """Metadata policy untuk sebuah mode (mode tak dikenal -> default)."""
    return POLICIES[normalize_mode(mode)]


def mode_order(mode: Any) -> int:
    """Posisi mode dalam urutan escalation (fast=0, balanced=1, deep=2)."""
    return ESCALATION_ORDER.index(normalize_mode(mode))


def next_mode(mode: Any) -> Optional[str]:
    """Mode escalation berikutnya, atau None bila sudah di mode tertinggi."""
    index = mode_order(mode)
    if index + 1 >= len(ESCALATION_ORDER):
        return None
    return ESCALATION_ORDER[index + 1]


@dataclass
class ExecutionPolicyState:
    """State policy satu run: requested vs effective mode + riwayat escalation.

    State ini MUTABLE dan dipakai bersama runtime <-> orchestrator: escalation
    memperbarui ``effective_mode`` di tempat (in-place) sehingga pemegang
    referensi (mis. AgentOrchestrator) melihat policy terbaru tanpa perlu
    dibangun ulang. Mutable bukan berarti mengubah loop: policy tidak
    menggerakkan keputusan apa pun.

    Attributes:
        requested_mode: mode yang diminta (user/metadata), selalu kanonik.
        effective_mode: mode yang benar-benar dipakai Agent.
        reason: alasan bila effective_mode berbeda dari permintaan/tidak default.
        escalations: riwayat escalation (from/to/reason/step).
    """

    requested_mode: str = DEFAULT_MODE
    effective_mode: str = DEFAULT_MODE
    reason: Optional[str] = None
    escalations: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def policy(self) -> ExecutionPolicy:
        """Metadata policy mode efektif."""
        return POLICIES[self.effective_mode]

    @property
    def escalated(self) -> bool:
        """True bila mode efektif pernah dieskalasi dari mode awal."""
        return bool(self.escalations)

    def can_escalate(self) -> bool:
        """True bila masih ada mode escalation yang lebih tinggi."""
        return next_mode(self.effective_mode) is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "reason": self.reason,
            "escalated": self.escalated,
            "escalations": [dict(item) for item in self.escalations],
            "policy": self.policy.to_dict(),
        }

    # ------------------------------------------------------------------ #
    # Escalation (mekanisme; keputusan tetap milik Agent/LLM)
    # ------------------------------------------------------------------ #
    def escalate(
        self,
        reason: str,
        target_mode: Optional[str] = None,
    ) -> "ExecutionPolicyState":
        """Naikkan ``effective_mode`` (in-place) dengan alasan eksplisit.

        Escalation HANYA boleh maju menurut ``ESCALATION_ORDER``
        (fast -> balanced -> deep). Tidak ada de-escalation di sini: menurunkan
        mode bukan escalation, dan itu di luar scope (tidak ada rule otomatis).

        Args:
            reason: alasan escalation (wajib non-kosong; disimpan apa adanya).
            target_mode: mode tujuan opsional. Bila None -> mode berikutnya
                menurut urutan escalation. Bila diisi, boleh melompat
                (mis. fast -> deep) selama arahnya MAJU.

        Returns:
            State ini sendiri (sudah diperbarui) untuk memudahkan chaining.

        Raises:
            PolicyEscalationError: bila reason kosong, mode tujuan tidak valid
                atau arahnya tidak maju (termasuk saat sudah di mode tertinggi).
        """
        text_reason = str(reason or "").strip()
        if not text_reason:
            raise PolicyEscalationError("Escalation butuh alasan (reason) non-kosong.")

        current = self.effective_mode
        if target_mode is None:
            target = next_mode(current)
            if target is None:
                raise PolicyEscalationError(
                    f"Mode '{current}' sudah pada tingkat tertinggi; tidak bisa escalate."
                )
        else:
            if not is_valid_mode(target_mode):
                raise PolicyEscalationError(
                    f"Mode tujuan '{target_mode}' tidak dikenal. "
                    f"Gunakan salah satu dari {available_modes()}."
                )
            target = normalize_mode(target_mode)
            if mode_order(target) <= mode_order(current):
                raise PolicyEscalationError(
                    f"Escalation harus naik: '{current}' -> '{target}' bukan escalation."
                )

        self.effective_mode = target
        self.reason = text_reason
        self.escalations.append(
            {
                "from": current,
                "to": target,
                "reason": text_reason,
                "step": len(self.escalations) + 1,
            }
        )
        return self


def _label(mode: Any) -> str:
    """Label ramah-user untuk mode (mis. "Fast")."""
    return POLICIES[normalize_mode(mode)].label


def format_policy_block(state: ExecutionPolicyState) -> str:
    """Blok activity policy standar (tanpa escalation).

    Contoh:

        [POLICY]
        Requested Mode: Fast
        Effective Mode: Fast
    """
    return "\n".join(
        [
            "[POLICY]",
            f"Requested Mode: {_label(state.requested_mode)}",
            f"Effective Mode: {_label(state.effective_mode)}",
        ]
    )


def format_escalation_block(previous_mode: Any, state: ExecutionPolicyState) -> str:
    """Blok activity policy saat escalation terjadi.

    Selalu menampilkan requested dan effective mode agar perbedaan keputusan
    user vs policy terlihat jelas di Activity.
    """
    reason = (state.reason or "").strip() or "(alasan tidak diberikan)"
    return "\n".join(
        [
            "[POLICY]",
            f"Requested Mode: {_label(state.requested_mode)}",
            f"Effective Mode: {_label(state.effective_mode)}",
            "",
            "Escalated:",
            f"{_label(previous_mode)} → {_label(state.effective_mode)}",
            "",
            "Reason:",
            reason,
        ]
    )


def policy_activity_text(
    state: ExecutionPolicyState,
    *,
    previous_mode: Optional[Any] = None,
) -> str:
    """Teks activity policy: blok escalation bila ada, selain itu blok standar."""
    if previous_mode is not None:
        return format_escalation_block(previous_mode, state)
    if state.escalated:
        first = state.escalations[0]
        return format_escalation_block(first["from"], state)
    return format_policy_block(state)


class ExecutionPolicyResolver:
    """Resolver requested_mode -> effective_mode + mekanisme escalation.

    Args:
        default_mode: mode default bila task tidak memberi mode (default
            balanced) -> menjaga backward compatibility task lama.
    """

    def __init__(self, default_mode: str = DEFAULT_MODE) -> None:
        self.default_mode = normalize_mode(default_mode, DEFAULT_MODE)

    # ------------------------------------------------------------------ #
    # Resolve
    # ------------------------------------------------------------------ #
    def resolve(
        self,
        requested_mode: Any = None,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ExecutionPolicyState:
        """Bentuk state policy awal dari mode permintaan/metadata task.

        ``effective_mode`` SELALU dimulai sama dengan ``requested_mode``:
        AgentRuntime/policy TIDAK menebak tingkat kesulitan task (tidak ada
        heuristic). Perubahan ke mode lain terjadi lewat escalation eksplisit
        (keputusan Agent/LLM) -> ``escalate()``.

        Args:
            requested_mode: mode eksplisit (menang atas metadata).
            metadata: metadata task; dibaca via ``MODE_METADATA_KEYS`` bila
                ``requested_mode`` kosong.

        Returns:
            ``ExecutionPolicyState`` (reason diisi bila mode berasal dari
            default/alias, None bila permintaan user jelas).
        """
        raw = requested_mode
        if raw is None and isinstance(metadata, Mapping):
            for key in MODE_METADATA_KEYS:
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value
                    break

        if raw is None or not str(raw).strip():
            return ExecutionPolicyState(
                requested_mode=self.default_mode,
                effective_mode=self.default_mode,
                reason=(
                    "Mode tidak diberikan pada task metadata; "
                    f"memakai default '{self.default_mode}'."
                ),
            )

        text = str(raw).strip().lower()
        if text in MODE_ALIASES:
            mode = MODE_ALIASES[text]
            return ExecutionPolicyState(
                requested_mode=mode,
                effective_mode=mode,
                reason=f"Mode '{text}' dipetakan ke '{mode}'.",
            )
        if text not in POLICIES:
            return ExecutionPolicyState(
                requested_mode=self.default_mode,
                effective_mode=self.default_mode,
                reason=(
                    f"Mode '{text}' tidak dikenal; memakai default "
                    f"'{self.default_mode}'."
                ),
            )
        return ExecutionPolicyState(requested_mode=text, effective_mode=text)

    # ------------------------------------------------------------------ #
    # Escalate
    # ------------------------------------------------------------------ #
    def escalate(
        self,
        state: ExecutionPolicyState,
        reason: str,
        target_mode: Optional[str] = None,
    ) -> ExecutionPolicyState:
        """Terapkan escalation pada state (in-place) — thin wrapper."""
        if state is None:
            raise PolicyEscalationError(
                "Policy belum di-resolve; tidak ada mode efektif untuk dieskalasi."
            )
        return state.escalate(reason, target_mode=target_mode)


__all__ = [
    "DEFAULT_MODE",
    "ESCALATION_ORDER",
    "ExecutionMode",
    "ExecutionPolicy",
    "ExecutionPolicyResolver",
    "ExecutionPolicyState",
    "MODE_ALIASES",
    "MODE_BALANCED",
    "MODE_DEEP",
    "MODE_FAST",
    "MODE_METADATA_KEYS",
    "POLICIES",
    "PolicyError",
    "PolicyEscalationError",
    "available_modes",
    "format_escalation_block",
    "format_policy_block",
    "get_policy",
    "is_valid_mode",
    "mode_order",
    "next_mode",
    "normalize_mode",
    "policy_activity_text",
]
