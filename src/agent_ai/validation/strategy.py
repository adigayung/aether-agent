"""Mode-aware verification preferences.

This module deliberately describes verification; it does not impose a stop
condition and it does not resolve Agent policy.  The active mode is supplied
by the runtime after policy resolution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from agent_ai.runtime.policy import DEFAULT_MODE, normalize_mode


@dataclass(frozen=True)
class VerificationStrategy:
    mode: str
    checks: List[str]
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "checks": list(self.checks), "description": self.description}


def strategy_for_mode(mode: Any = DEFAULT_MODE) -> VerificationStrategy:
    """Return advisory checks for the effective Agent mode."""
    effective = normalize_mode(mode)
    if effective == "fast":
        return VerificationStrategy(
            effective, ["syntax validation", "import/check dasar", "changed files"],
            "Pemeriksaan ringan; regression besar tidak dipilih secara default.",
        )
    if effective == "deep":
        return VerificationStrategy(
            effective, ["regression suite", "architecture validation", "integration validation", "impact review"],
            "Pemeriksaan luas untuk perubahan berisiko tinggi.",
        )
    return VerificationStrategy(
        effective, ["related tests", "existing checker", "normal validation"],
        "Validasi normal AETHER.",
    )


def format_verification_activity(strategy: VerificationStrategy, previous_mode: Any = None) -> str:
    lines = ["[VERIFY]"]
    if previous_mode is not None and normalize_mode(previous_mode) != strategy.mode:
        lines.append(f"Effective Mode changed: {normalize_mode(previous_mode).title()} → {strategy.mode.title()}")
    else:
        lines.append(f"Mode: {strategy.mode.title()}")
    lines.append("Checks:")
    lines.extend(f"- {check}" for check in strategy.checks)
    return "\n".join(lines)


__all__ = ["VerificationStrategy", "strategy_for_mode", "format_verification_activity"]
