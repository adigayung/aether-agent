"""Validator konkret untuk Validation Subsystem.

Saat ini menyediakan:
    - CommandValidator : validasi berbasis command.

CommandValidator TIDAK membuat executor terminal baru. Ia memakai tool
terminal yang sudah ada (agent_ai.tools.terminal.RunCommandTool) dan
menormalisasi hasilnya menjadi ValidationResult.

Validator berikutnya (syntax, test, build, lint, type check) dapat ditambahkan
sebagai subclass Validator tanpa mengubah core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from agent_ai.tools.base import ToolError
from agent_ai.tools.terminal import RunCommandTool
from agent_ai.validation.base import Validator, ValidatorError
from agent_ai.validation.models import (
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
)

# Pemetaan outcome tool terminal -> outcome validasi.
_OUTCOME_MAP: Dict[str, ValidationOutcome] = {
    "success": ValidationOutcome.SUCCESS,
    "command_failure": ValidationOutcome.COMMAND_FAILURE,
    "timeout": ValidationOutcome.TIMEOUT,
    "spawn_error": ValidationOutcome.EXECUTION_ERROR,
}


class CommandValidator(Validator):
    """Validator berbasis command (memakai RunCommandTool yang sudah ada).

    Args:
        root: working directory (workspace boundary). Default: root tool.
        tool: instance RunCommandTool opsional (untuk injeksi/testing).
    """

    name = "command"

    def __init__(
        self,
        root: Optional[Path] = None,
        tool: Optional[RunCommandTool] = None,
    ) -> None:
        self.tool = tool or RunCommandTool(root=root)

    def validate(self, request: ValidationRequest) -> ValidationResult:
        """Jalankan command dari request dan normalisasi hasilnya.

        Raises:
            ValidatorError: bila request tidak punya command.
        """
        command = request.command
        if not command or not str(command).strip():
            raise ValidatorError("CommandValidator butuh 'command' pada request.")

        arguments: Dict[str, Any] = {"command": command}
        if request.timeout is not None:
            arguments["timeout"] = request.timeout

        try:
            raw = self.tool.execute(**arguments)
        except ToolError as exc:
            # Error menjalankan tool (mis. workspace invalid) -> execution_error.
            return ValidationResult(
                success=False,
                outcome=ValidationOutcome.EXECUTION_ERROR,
                exit_code=None,
                stdout="",
                stderr=str(exc),
                duration=0.0,
                validator=self.name,
                metadata={"target": request.target, "error": f"{type(exc).__name__}: {exc}"},
            )

        outcome = _OUTCOME_MAP.get(raw.get("outcome"), ValidationOutcome.EXECUTION_ERROR)
        return ValidationResult(
            success=bool(raw.get("success")) and outcome == ValidationOutcome.SUCCESS,
            outcome=outcome,
            exit_code=raw.get("exit_code"),
            stdout=raw.get("stdout", "") or "",
            stderr=raw.get("stderr", "") or "",
            duration=float(raw.get("duration", 0.0) or 0.0),
            validator=self.name,
            metadata={
                "target": request.target,
                "command": command,
                "timed_out": raw.get("timed_out", False),
                **({"error": raw["error"]} if raw.get("error") else {}),
            },
        )
