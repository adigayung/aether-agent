"""Approval coordination untuk kebijakan ASK (``require_approval``).

Modul ini BUKAN sistem permission kedua dan BUKAN policy engine: keputusan
tetap dibuat oleh ``PermissionPolicy``/``PermissionManager`` yang SUDAH ADA.
Ia hanya *coordination primitive* sinkron + bounded agar action yang ditahan
oleh mode ASK dapat dilanjutkan atau ditolak oleh user.

Alur::

    ToolExecutor (decision.requires_approval == True)
        -> gate(context)                         # dipanggil di thread eksekusi
             -> ApprovalCoordinator.request(...) -> emit approval_requested
             -> ApprovalCoordinator.wait(...)    # blok, bounded timeout
        <- user Allow/Deny lewat HTTP
             -> ApprovalCoordinator.resolve(...) -> emit approval_resolved
        -> gate mengembalikan True/False

Setiap request membawa ``task_id``/``session_id``/``tool_call_id`` sehingga
approval TIDAK tertukar antar task. Event memakai event system AETHER yang
sudah ada (SessionStore) — TIDAK ada channel/message bus kedua.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

#: Timeout default (detik) menunggu keputusan user sebelum dianggap DITOLAK.
#: Bounded agar execution slot queue tidak "nyangkut" selamanya bila user
#: menutup UI. Timeout -> DENIED (aman, tidak pernah otomatis ALLOW).
DEFAULT_APPROVAL_TIMEOUT = 300.0

#: Tipe sink event approval: ``(session_id, event_type, payload) -> None``.
#: Dipakai gateway untuk meneruskan ke SessionStore AETHER existing.
ApprovalSink = Callable[[str, str, Dict[str, Any]], None]


class ApprovalStatus(str, Enum):
    """Status sebuah permintaan approval."""

    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    EXPIRED = "expired"


def new_approval_id() -> str:
    """Buat id unik untuk satu permintaan approval."""
    return uuid.uuid4().hex


@dataclass
class ApprovalRequest:
    """Satu permintaan approval (action yang ditahan oleh mode ASK).

    Attributes:
        request_id: id unik permintaan (kunci resolve; tidak tertukar antar task).
        tool: nama tool/action yang ditahan.
        target: target/path/command yang akan dikenai action.
        action_class: ActionClass existing (mis. "workspace_write").
        matrix_action: aksi Project Permission Matrix (mis. "modify_files").
        scope: scope matrix INSIDE/OUTSIDE (bila dapat ditentukan).
        reason: alasan singkat dari policy.
        task_id: task yang memiliki action ini (anti tertukar antar task).
        session_id: session AETHER (untuk event streaming).
        tool_call_id: id tool call asal (traceability).
        status: ApprovalStatus saat ini.
        created_at/resolved_at: timestamp epoch.
    """

    request_id: str
    tool: str
    target: str = ""
    action_class: str = ""
    matrix_action: str = ""
    scope: str = ""
    reason: str = ""
    task_id: str = ""
    session_id: str = ""
    tool_call_id: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Representasi untuk API/UI/event (tanpa state internal)."""
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "target": self.target,
            "action_class": self.action_class,
            "matrix_action": self.matrix_action,
            "scope": self.scope,
            "reason": self.reason,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


class ApprovalCoordinator:
    """Koordinator approval: daftar permintaan pending + sinkronisasi keputusan.

    Args:
        sink: callback opsional ``(session_id, event_type, payload) -> None``
            untuk memancarkan event ``approval_requested``/``approval_resolved``
            lewat event system existing (SessionStore).
        timeout: timeout default (detik) menunggu keputusan user.

    Thread-safe: ``request``/``resolve``/``pending`` dikunci dengan satu lock;
    ``wait`` memblokir pada ``threading.Event`` per-request (bukan lock global).
    """

    def __init__(
        self,
        sink: Optional[ApprovalSink] = None,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT,
    ) -> None:
        self._sink = sink
        self.timeout = float(timeout)
        self._requests: Dict[str, ApprovalRequest] = {}
        self._events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _emit(self, event_type: str, request: ApprovalRequest) -> None:
        """Pancarkan event approval lewat sink (kegagalan tidak boleh crash)."""
        if self._sink is None:
            return
        try:
            self._sink(request.session_id, event_type, request.to_dict())
        except Exception:  # noqa: BLE001 - event tidak boleh memutus eksekusi
            return

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def request(
        self,
        *,
        tool: str,
        target: str = "",
        action_class: str = "",
        matrix_action: str = "",
        scope: str = "",
        reason: str = "",
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
    ) -> ApprovalRequest:
        """Buat permintaan approval baru dan pancarkan ``approval_requested``."""
        req = ApprovalRequest(
            request_id=new_approval_id(),
            tool=tool,
            target=target,
            action_class=action_class,
            matrix_action=matrix_action,
            scope=scope,
            reason=reason,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
        with self._lock:
            self._requests[req.request_id] = req
            self._events[req.request_id] = threading.Event()
        self._emit("approval_requested", req)
        return req

    def wait(
        self, request_id: str, timeout: Optional[float] = None
    ) -> ApprovalStatus:
        """Blokir sampai keputusan diterima atau timeout (bounded).

        Returns:
            ApprovalStatus final: ALLOWED/DENIED (dari resolve) atau EXPIRED
            (timeout / request tidak dikenal). TIDAK pernah mengembalikan
            PENDING setelah fungsi ini kembali.
        """
        with self._lock:
            event = self._events.get(request_id)
        if event is None:
            return ApprovalStatus.EXPIRED
        event.wait(self.timeout if timeout is None else float(timeout))
        with self._lock:
            req = self._requests.get(request_id)
        if req is None:
            return ApprovalStatus.EXPIRED
        if req.status is ApprovalStatus.PENDING:
            # Timeout: tandai EXPIRED agar tidak menggantung & tidak pernah ALLOW.
            with self._lock:
                if req.status is ApprovalStatus.PENDING:
                    req.status = ApprovalStatus.EXPIRED
                    req.resolved_at = time.time()
            return ApprovalStatus.EXPIRED
        return req.status

    def resolve(self, request_id: str, allow: bool) -> Optional[ApprovalRequest]:
        """Selesaikan permintaan (Allow/Deny) dan pancarkan ``approval_resolved``.

        Returns:
            ApprovalRequest yang (sudah) selesai, atau None bila tidak dikenal.
            Idempotent: resolve kedua tidak mengubah keputusan pertama.
        """
        with self._lock:
            req = self._requests.get(request_id)
            event = self._events.get(request_id)
            if req is None or event is None:
                return None
            if req.status is not ApprovalStatus.PENDING:
                return req
            req.status = ApprovalStatus.ALLOWED if allow else ApprovalStatus.DENIED
            req.resolved_at = time.time()
        event.set()
        self._emit("approval_resolved", req)
        return req

    def status(self, request_id: str) -> Optional[ApprovalStatus]:
        """Status sebuah permintaan (None bila tidak dikenal)."""
        with self._lock:
            req = self._requests.get(request_id)
        return req.status if req is not None else None

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        """Ambil permintaan berdasarkan id (None bila tidak dikenal)."""
        with self._lock:
            return self._requests.get(request_id)

    def pending(self, task_id: Optional[str] = None) -> List[ApprovalRequest]:
        """Daftar permintaan berstatus PENDING (opsional difilter task_id)."""
        with self._lock:
            items = [
                r
                for r in self._requests.values()
                if r.status is ApprovalStatus.PENDING
            ]
        if task_id:
            items = [r for r in items if r.task_id == task_id]
        return items

    def cancel_task(self, task_id: str) -> int:
        """Tolak semua approval PENDING milik ``task_id`` (mis. saat Stop).

        Returns:
            Jumlah permintaan yang ditolak.
        """
        with self._lock:
            ids = [
                r.request_id
                for r in self._requests.values()
                if r.status is ApprovalStatus.PENDING and r.task_id == task_id
            ]
        resolved = 0
        for rid in ids:
            if self.resolve(rid, False) is not None:
                resolved += 1
        return resolved

    def clear(self) -> None:
        """Bersihkan seluruh riwayat permintaan (housekeeping)."""
        with self._lock:
            self._requests.clear()
            self._events.clear()


def make_approval_gate(
    coordinator: ApprovalCoordinator,
    *,
    task_id: str = "",
    session_id: str = "",
    timeout: Optional[float] = None,
):
    """Bangun callable gate terikat ke satu task/session.

    Gate menerima ``context`` (tool/target/action_class/matrix_action/scope/
    reason/tool_call_id dari ToolExecutor), membuat permintaan approval, lalu
    memblokir sampai keputusan user diterima. Mengembalikan True hanya bila
    user ALLOW; selain itu (Deny/timeout/error) -> False.

    Args:
        coordinator: ApprovalCoordinator gateway-level.
        task_id: task pemilik action (agar approval tidak tertukar antar task).
        session_id: session AETHER untuk event streaming.
        timeout: override timeout (detik); None = pakai timeout coordinator.
    """

    def gate(context: Optional[Dict[str, Any]] = None) -> bool:
        ctx = dict(context or {})
        req = coordinator.request(
            tool=str(ctx.get("tool", "") or ""),
            target=str(ctx.get("target", "") or ""),
            action_class=str(ctx.get("action_class", "") or ""),
            matrix_action=str(ctx.get("matrix_action", "") or ""),
            scope=str(ctx.get("scope", "") or ""),
            reason=str(ctx.get("reason", "") or ""),
            tool_call_id=str(ctx.get("tool_call_id", "") or ""),
            task_id=task_id,
            session_id=session_id,
        )
        status = coordinator.wait(req.request_id, timeout=timeout)
        return status is ApprovalStatus.ALLOWED

    return gate


__all__ = [
    "ApprovalCoordinator",
    "ApprovalRequest",
    "ApprovalStatus",
    "DEFAULT_APPROVAL_TIMEOUT",
    "make_approval_gate",
    "new_approval_id",
]
