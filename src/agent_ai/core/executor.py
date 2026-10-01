"""Tool Executor: bridge antara LLMResponse, ToolRegistry, dan AgentObservation.

Alur:
    LLMResponse(tool_calls) -> ToolExecutor -> ToolRegistry.execute()
        -> hasil/error -> AgentObservation

Native Tool Calling:
    ToolCall -> ToolExecutor.execute_tool_call() -> ToolResultPayload
        -> (diformat jadi message role="tool" oleh ConversationHistory)

Prinsip:
    - Provider-agnostic: tidak ada logika khusus Ollama/OpenAI/DeepSeek.
    - Error tool ditangkap dan menjadi AgentObservation gagal (bukan crash).
    - Execution-only: tidak reasoning, tidak menentukan task selesai, tidak
      memilih tool alternatif. Semua kegagalan (permission ditolak, tool error,
      exception runtime, command gagal) menjadi ToolResultPayload(status=error),
      bukan exception yang memutus agent loop.
    - Membedakan secara eksplisit:
        * tool_error      : tool gagal menjalankan operasinya (metadata
                            "tool_error": True, success=False).
        * command_failure : tool berhasil dijalankan tetapi command/program
                            menghasilkan exit_code != 0 (metadata
                            "command_failure": True, success=True).
    - Workspace/security boundary tetap dipegang oleh tool (filesystem tools).
    - Tidak ada autonomous planning/memory/RAG/Git/terminal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent_ai.core.models import AgentAction, AgentObservation
from agent_ai.core.response import ActionType, LLMAction, LLMResponse
from agent_ai.core.tool_coordinator import (
    ToolBatchResult,
    ToolExecutionCoordinator,
)
from agent_ai.core.types import ToolCall, ToolResultPayload, parse_tool_arguments
from agent_ai.tools.base import ToolError
from agent_ai.tools.registry import ToolRegistry, registry as default_registry

if TYPE_CHECKING:  # pragma: no cover - hindari import cycle saat runtime
    from agent_ai.permission.manager import PermissionManager


class ToolExecutor:
    """Menjalankan LLMAction melalui ToolRegistry dan menghasilkan AgentObservation.

    Args:
        registry: ToolRegistry yang dipakai. Default: registry global.
        permission_manager: PermissionManager opsional (#54). Bila diberikan,
            policy dievaluasi SEBELUM tool dieksekusi; action yang ditolak /
            butuh approval tidak dijalankan. Bila None, executor berperilaku
            persis seperti sebelumnya (backward compatible).
        workspace_root: root workspace project opsional. Diteruskan ke
            PermissionManager agar Project Permission Matrix dapat membedakan
            aksi DI DALAM vs DI LUAR workspace. Bila None, scope = INSIDE
            (perilaku existing tidak berubah).
        project_matrix: Project Permission Matrix opsional (project-local).
            Diteruskan ke PermissionManager sehingga policy project benar-benar
            berlaku pada execution path.
        approval_gate: callable opsional ``(context: dict) -> bool``. Dipanggil
            HANYA saat decision butuh approval (ASK). Bila mengembalikan True,
            action DILANJUTKAN; bila False/timeout, action dibatalkan dan hasil
            penolakan dikembalikan ke Agent. Bila None, perilaku existing
            dipertahankan (ASK = not executed / "Approval Required").
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        permission_manager: Optional["PermissionManager"] = None,
        *,
        max_parallel_tools: Optional[int] = None,
        workspace_root: Optional[str] = None,
        project_matrix: Optional[Any] = None,
        approval_gate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> None:
        self.registry = registry or default_registry
        self.permission_manager = permission_manager
        # Root workspace project (untuk matrix inside/outside). None = tidak
        # diketahui -> scope INSIDE (boundary tool tetap berlaku).
        self.workspace_root = str(workspace_root) if workspace_root is not None else None
        # Matrix project-local opsional (bila project punya `.aether/permissions.json`).
        self.project_matrix = project_matrix
        # Gate approval ASK opsional (dipasang pada execution path produksi).
        # Bila None, ASK diperlakukan seperti sebelumnya (tidak dijalankan).
        self.approval_gate = approval_gate
        # Tool Execution Coordinator: satu-satunya pengatur batch (sequential/
        # parallel) di atas execute_tool_call(). Bukan executor kedua.
        self.coordinator = ToolExecutionCoordinator(max_parallel=max_parallel_tools)

    def _permission_check(self, action: str, arguments: Dict[str, Any]):
        """Evaluasi permission dengan konteks workspace + matrix project.

        Memanggil `PermissionManager.check` dengan konteks matrix (workspace_root
        + project_matrix) BILA manager mendukungnya. Manager duck-typed lama
        (mis. verifier yang hanya menerima `check(action, arguments)`) tetap
        bekerja dengan signature lama — backward compatible.
        """
        manager = self.permission_manager
        check = getattr(manager, "check", None)
        if check is None:
            raise AttributeError("permission_manager tidak punya method 'check'.")
        if self.workspace_root is not None or self.project_matrix is not None:
            try:
                import inspect

                params = inspect.signature(check).parameters
                accepts = "workspace_root" in params or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
            except (TypeError, ValueError):
                accepts = False
            if accepts:
                return check(
                    action,
                    arguments,
                    workspace_root=self.workspace_root,
                    project_matrix=self.project_matrix,
                )
        return check(action, arguments)

    # ------------------------------------------------------------------ #
    # Konversi action
    # ------------------------------------------------------------------ #
    @staticmethod
    def to_agent_action(action: LLMAction) -> AgentAction:
        """Konversi LLMAction (protocol) menjadi AgentAction (loop)."""
        return AgentAction(
            name=action.name,
            arguments=dict(action.arguments or {}),
            raw=action.to_dict(),
        )

    # ------------------------------------------------------------------ #
    # Eksekusi
    # ------------------------------------------------------------------ #
    def execute_action(self, action: LLMAction) -> AgentObservation:
        """Jalankan satu LLMAction dan kembalikan AgentObservation.

        Error tool (ToolError) ditangkap dan dikembalikan sebagai observation
        gagal, sehingga loop tidak crash.
        """
        agent_action = self.to_agent_action(action)

        # Action non-tool (mis. FINAL) tidak dieksekusi sebagai tool.
        if action.type != ActionType.TOOL_CALL:
            return AgentObservation(
                content=None,
                action_id=agent_action.id,
                success=True,
                metadata={"skipped": True, "reason": "bukan tool_call"},
            )

        # Permission Policy (#54): evaluasi SEBELUM eksekusi. Bila policy
        # menolak / butuh approval, tool TIDAK dijalankan. Bila manager tidak
        # diberikan, langkah ini dilewati (backward compatible). Untuk ASK,
        # eksekusi DITAHAN lalu dimintakan approval user via `approval_gate`.
        if self.permission_manager is not None:
            decision = self._permission_check(action.name, dict(action.arguments or {}))
            if not decision.allowed:
                action_class_value = getattr(
                    getattr(decision, "action_class", None), "value", None
                )
                mode_value = getattr(getattr(decision, "mode", None), "value", None)
                requires_approval = getattr(decision, "requires_approval", False)
                approved = False
                if requires_approval:
                    approved = self._ask_approval(
                        action.name, dict(action.arguments or {}), decision
                    )
                if not approved:
                    return AgentObservation(
                        content=None,
                        action_id=agent_action.id,
                        success=False,
                        error=f"Permission denied: {getattr(decision, 'reason', '')}",
                        metadata={
                            "tool": action.name,
                            "permission_denied": True,
                            "action_class": action_class_value,
                            "policy_mode": mode_value,
                            "requires_approval": requires_approval,
                        },
                    )

        try:
            result = self.registry.execute(action.name, dict(action.arguments or {}))
        except ToolError as exc:
            # tool_error: tool gagal menjalankan operasinya sendiri
            # (invalid input, gagal spawn, dll).
            return AgentObservation(
                content=None,
                action_id=agent_action.id,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                metadata={"tool": action.name, "tool_error": True},
            )
        except Exception as exc:  # noqa: BLE001 - jangan biarkan loop crash
            return AgentObservation(
                content=None,
                action_id=agent_action.id,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                metadata={"tool": action.name, "tool_error": True, "unexpected": True},
            )

        # Tool berhasil dijalankan. Bila hasilnya menandai command_failure
        # (mis. run_command dengan exit_code != 0), bedakan secara eksplisit.
        metadata = {"tool": action.name}
        if isinstance(result, dict) and result.get("outcome") == "command_failure":
            metadata["command_failure"] = True
            metadata["exit_code"] = result.get("exit_code")

        return AgentObservation(
            content=result,
            action_id=agent_action.id,
            success=True,
            metadata=metadata,
        )

    def execute_response(self, response: LLMResponse) -> List[AgentObservation]:
        """Jalankan semua tool_call dalam sebuah LLMResponse.

        Returns:
            Daftar AgentObservation (satu per tool call, urut).
        """
        return [self.execute_action(action) for action in response.tool_calls()]

    # ------------------------------------------------------------------ #
    # Native Tool Calling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_tool_arguments(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
        """Parse argumen ToolCall dengan aman -> (dict, error_message|None).

        Menerima dict (sudah terstruktur) atau JSON string (format provider).
        Tidak melempar exception: JSON tidak valid dikembalikan sebagai pesan
        error agar pemanggil bisa mengubahnya menjadi ToolResultPayload gagal.

        Delegasi ke `agent_ai.core.types.parse_tool_arguments` (satu parser
        argumen untuk executor + coordinator).
        """
        return parse_tool_arguments(raw)

    @staticmethod
    def _failure_content(result: Any) -> Optional[str]:
        """Ubah hasil tool yang menandai kegagalan menjadi string error.

        Mengembalikan string (outcome/exit_code/stdout/stderr/error) bila hasil
        menandai kegagalan (command_failure/timeout/spawn_error atau
        success=False), atau None bila hasil dianggap sukses.
        """
        if not isinstance(result, dict):
            return None
        outcome = result.get("outcome")
        failed = outcome in {"command_failure", "timeout", "spawn_error"}
        if not failed and result.get("success") is not False:
            return None
        parts = [f"Tool execution failed (outcome={outcome or 'error'})."]
        if result.get("exit_code") is not None:
            parts.append(f"exit_code={result['exit_code']}")
        if result.get("error"):
            parts.append(f"error={result['error']}")
        if result.get("stdout"):
            parts.append(f"stdout:\n{result['stdout']}")
        if result.get("stderr"):
            parts.append(f"stderr:\n{result['stderr']}")
        return "\n".join(parts)

    def execute_tool_call(self, tool_call: ToolCall) -> ToolResultPayload:
        """Jalankan satu ToolCall dan kembalikan ToolResultPayload.

        Execution-only: tidak reasoning, tidak menentukan task selesai, tidak
        memilih tool alternatif setelah gagal. SEMUA kegagalan (permission
        ditolak, tool error, exception runtime, command gagal) dikembalikan
        sebagai payload status=error (bukan raise), sehingga agent loop tidak
        putus. Pembuatan message role="tool" adalah tanggung jawab
        ConversationHistory, bukan ToolExecutor.

        Args:
            tool_call: ToolCall (id, fungsi/nama, argumen) dari model.

        Returns:
            ToolResultPayload yang selalu membawa tool_call_id, tool_name,
            output/content, dan status.
        """
        tool_call_id = tool_call.id
        tool_name = tool_call.name

        # 1) Parse argumen dengan aman (dict atau JSON string).
        arguments, parse_error = self._parse_tool_arguments(tool_call.arguments)
        if parse_error is not None:
            return ToolResultPayload.error(
                tool_call_id, tool_name, f"Invalid arguments: {parse_error}"
            )
        if not tool_name:
            return ToolResultPayload.error(
                tool_call_id, tool_name, "Tool call tidak punya nama tool."
            )

        # 2) Permission Policy (#54): evaluasi SEBELUM eksekusi. Bila ditolak /
        #    butuh approval, kembalikan payload error (jangan raise). DENY dan
        #    ASK (require_approval) sama-sama MENAHAN eksekusi; untuk ASK,
        #    eksekusi DITAHAN lalu dimintakan approval user via `approval_gate`.
        #    Bila gate mengembalikan True -> action dilanjutkan; bila False/
        #    timeout -> action dibatalkan dan hasil penolakan dikembalikan.
        if self.permission_manager is not None:
            decision = self._permission_check(tool_name, dict(arguments))
            if not decision.allowed:
                if getattr(decision, "requires_approval", False):
                    allowed = self._ask_approval(tool_name, arguments, decision)
                    if not allowed:
                        return ToolResultPayload.error(
                            tool_call_id,
                            tool_name,
                            "Approval Required: tool "
                            f"'{tool_name}' requires user approval before execution "
                            f"({getattr(decision, 'reason', '')})",
                        )
                    return self._execute_registered_tool(
                        tool_call_id, tool_name, arguments
                    )
                return ToolResultPayload.error(
                    tool_call_id,
                    tool_name,
                    f"Permission Denied: User/Policy rejected execution of tool '{tool_name}'",
                )

        return self._execute_registered_tool(tool_call_id, tool_name, arguments)

    def _execute_registered_tool(
        self, tool_call_id: str, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResultPayload:
        """Jalankan tool yang sudah lolos permission (mekanisme registry existing)."""
        # 3) Eksekusi tool via mekanisme existing (ToolRegistry). Tool error /
        #    exception runtime -> payload error, bukan propagate ke loop.
        try:
            result = self.registry.execute(tool_name, arguments)
        except ToolError as exc:
            return ToolResultPayload.error(
                tool_call_id, tool_name, f"{type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - jangan putuskan agent loop
            return ToolResultPayload.error(
                tool_call_id, tool_name, f"{type(exc).__name__}: {exc}"
            )

        # 4) Hasil sukses, kecuali hasil menandai kegagalan command/program
        #    (exit_code != 0 / timeout / spawn) -> status error + detail string.
        failure = self._failure_content(result)
        if failure is not None:
            return ToolResultPayload.error(tool_call_id, tool_name, failure)
        return ToolResultPayload.success(tool_call_id, tool_name, result)

    def _approval_context(
        self, tool_name: str, arguments: Dict[str, Any], decision: Any
    ) -> Dict[str, Any]:
        """Bangun konteks approval (tool/target/class/scope) dari decision+args.

        Target path/command diambil dari argumen (path/command/...), action_class
        & mode dari decision policy existing. Dipakai UI approval untuk
        menampilkan jenis action + target/path yang akan digunakan.
        """
        from agent_ai.permission.matrix import describe_target

        matrix_action = getattr(decision, "matrix_action", None)
        scope = getattr(decision, "scope", None)
        action_class = getattr(decision, "action_class", None)
        try:
            target = describe_target(arguments)
        except Exception:  # noqa: BLE001 - deskripsi tidak boleh crash
            target = ""
        return {
            "tool": tool_name,
            "target": target,
            "action_class": getattr(action_class, "value", None) or "",
            "matrix_action": getattr(matrix_action, "value", None) or "",
            "scope": getattr(scope, "value", None) or "",
            "reason": str(getattr(decision, "reason", "") or ""),
        }

    def _ask_approval(
        self, tool_name: str, arguments: Dict[str, Any], decision: Any
    ) -> bool:
        """Tahan eksekusi lalu minta approval user (bila gate tersedia).

        Returns:
            True bila user ALLOW (action boleh dilanjutkan); False bila gate
            tidak tersedia / user DENY / timeout (action dibatalkan). TIDAK
            pernah raise: kegagalan gate -> False (aman, tidak auto-allow).
        """
        gate = self.approval_gate
        if gate is None:
            return False
        try:
            return bool(gate(self._approval_context(tool_name, arguments, decision)))
        except Exception:  # noqa: BLE001 - approval error tidak boleh crash loop
            return False

    def execute_tool_calls(
        self,
        tool_calls: Sequence[ToolCall],
        *,
        on_start: Optional[Callable[[ToolCall], None]] = None,
        on_complete: Optional[Callable[[ToolCall, ToolResultPayload], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        max_parallel: Optional[int] = None,
    ) -> ToolBatchResult:
        """Jalankan SEMUA ToolCall dari satu response LLM sebagai satu batch.

        Coordinator di atas `execute_tool_call()` (bukan executor kedua):
        mengklasifikasi, mendeteksi konflik/dependency, lalu menjalankan
        group demi group (paralel di dalam group, sequential antar group).
        Hasil dikembalikan TERURUT sesuai urutan input (bukan completion
        order), sehingga mapping `tool_call_id` -> hasil tetap deterministik.

        Semantics setiap tool TIDAK berubah: eksekusi individual tetap lewat
        `execute_tool_call()` (registry, permission, workspace boundary).

        Args:
            tool_calls: daftar ToolCall dari response LLM (urutan dipertahankan).
            on_start: callback sebelum satu tool call dimulai.
            on_complete: callback setelah satu tool call selesai/di-block.
            cancel_check: callable -> bool; bila True, tool berikutnya tidak
                dimulai (safe boundary).
            max_parallel: override batas paralel untuk batch ini (opsional).

        Returns:
            ToolBatchResult (payload terurut; cancelled bila dihentikan).
        """
        calls = list(tool_calls)
        coordinator = self.coordinator
        if max_parallel is not None:
            coordinator = ToolExecutionCoordinator(max_parallel=max_parallel)
        return coordinator.execute(
            calls,
            self.execute_tool_call,
            on_start=on_start,
            on_complete=on_complete,
            cancel_check=cancel_check,
        )
