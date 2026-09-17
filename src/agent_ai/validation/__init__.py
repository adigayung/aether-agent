"""Validation Subsystem AETHER.

Menyediakan validasi provider-agnostic berbasis validator:

    ValidationRequest -> Validator -> ValidationResult

    from agent_ai.validation import (
        ValidationRequest,
        ValidationResult,
        ValidationOutcome,
        Validator,
        CommandValidator,
        ValidationRunner,
    )

Validator berikutnya (syntax, test, build, lint, type check) dapat ditambahkan
sebagai subclass Validator tanpa mengubah core.
"""

from agent_ai.validation.base import Validator, ValidatorError
from agent_ai.validation.models import (
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
)
from agent_ai.validation.runner import ValidationRunner
from agent_ai.validation.validators import CommandValidator

__all__ = [
    "Validator",
    "ValidatorError",
    "ValidationRequest",
    "ValidationResult",
    "ValidationOutcome",
    "ValidationRunner",
    "CommandValidator",
]
