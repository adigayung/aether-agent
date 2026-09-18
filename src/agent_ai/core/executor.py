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

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from agent_ai.core.models import AgentAction, AgentObservation
from agent_ai.core.response import ActionType, LLMAction, LLMResponse
from agent_ai.core.types import ToolCall, ToolResultPayload
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
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        permission_manager: Optional["PermissionManager"] = None,
    ) -> None:
        self.registry = registry or default_registry
        self.permission_manager = permission_manager

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
        # diberikan, langkah ini dilewati (backward compatible).
        if self.permission_manager is not None:
            decision = self.permission_manager.check(
                action.name, dict(action.arguments or {})
            )
            if not decision.allowed:
                return AgentObservation(
                    content=None,
                    action_id=agent_action.id,
                    success=False,
                    error=f"Permission denied: {decision.reason}",
                    metadata={
                        "tool": action.name,
                        "permission_denied": True,
                        "action_class": decision.action_class.value,
                        "policy_mode": decision.mode.value,
                        "requires_approval": decision.requires_approval,
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

        # 2) Permission Policy (#54): evaluasi SEBELUM eksekusi. Bila ditolak,
        #    kembalikan payload error (jangan raise).
        if self.permission_manager is not None:
            decision = self.permission_manager.check(tool_name, dict(arguments))
            if not decision.allowed:
                return ToolResultPayload.error(
                    tool_call_id,
                    tool_name,
                    f"Permission Denied: User/Policy rejected execution of tool '{tool_name}'",
                )

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
