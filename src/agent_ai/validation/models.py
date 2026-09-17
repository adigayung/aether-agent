"""Model untuk Validation Subsystem.

Mendefinisikan struktur request/result validasi yang provider-agnostic:

    ValidationRequest -> Validator -> ValidationResult

Model di sini plain data (dataclass) dan tidak menyimpan chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ValidationOutcome(str, Enum):
    """Hasil normalisasi sebuah validasi."""

    SUCCESS = "success"                  # validasi lulus (exit_code == 0)
    COMMAND_FAILURE = "command_failure"  # command jalan tetapi exit_code != 0
    TIMEOUT = "timeout"                  # command melewati batas waktu
    EXECUTION_ERROR = "execution_error"  # gagal menjalankan (spawn/error lain)


@dataclass
class ValidationRequest:
    """Permintaan validasi.

    Attributes:
        target: apa yang divalidasi (mis. nama file, modul, atau deskripsi).
        command: command yang dijalankan (untuk validator berbasis command).
        timeout: batas waktu detik (opsional).
        metadata: info tambahan bebas (mis. jenis validator, cwd).
    """

    target: str = ""
    command: Optional[str] = None
    timeout: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "command": self.command,
            "timeout": self.timeout,
            "metadata": self.metadata,
        }


@dataclass
class ValidationResult:
    """Hasil validasi yang sudah dinormalisasi.

    Attributes:
        success: True bila validasi lulus.
        outcome: hasil normalisasi (success/command_failure/timeout/execution_error).
        exit_code: exit code command (None bila tidak tersedia).
        stdout: output standar.
        stderr: output error.
        duration: durasi eksekusi (detik).
        validator: nama validator yang menghasilkan result.
        metadata: info tambahan bebas.
    """

    success: bool
    outcome: ValidationOutcome
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    validator: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": self.duration,
            "validator": self.validator,
            "metadata": self.metadata,
        }
