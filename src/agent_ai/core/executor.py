"""Tool Executor: bridge antara LLMResponse, ToolRegistry, dan AgentObservation.

Alur:
    LLMResponse(tool_calls) -> ToolExecutor -> ToolRegistry.execute()
        -> hasil/error -> AgentObservation

Prinsip:
    - Provider-agnostic: tidak ada logika khusus Ollama/OpenAI/DeepSeek.
    - Error tool ditangkap dan menjadi AgentObservation gagal (bukan crash).
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

from typing import TYPE_CHECKING, List, Optional

from agent_ai.core.models import AgentAction, AgentObservation
from agent_ai.core.response import ActionType, LLMAction, LLMResponse
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
