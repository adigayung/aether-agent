"""Retrieval Profile + Budget untuk Advanced Context / Token Budgeting.

Profile (minimal/balanced/deep) menentukan seberapa agresif retrieval context.
Policy TIDAK di-hardcode di Agent Core: nilai dibaca dari `ContextConfig`
(config/settings.py) dan dapat di-override lewat environment.

    from agent_ai.contextbudget import ProfileRegistry

    registry = ProfileRegistry.from_config(settings.context)
    budget = registry.budget("balanced")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Budget:
    """Batas context untuk sebuah retrieval profile.

    Attributes:
        max_files: batas jumlah file relevan.
        max_bytes: batas total bytes source.
        max_tokens: batas estimasi token context.
        max_depth: kedalaman dependency expansion.
        max_nodes: batas jumlah node dependency expansion.
        relevance_threshold: ambang skor relevansi minimum.
        partial_read_limit: batas baris untuk partial read.
        partial_read: aktifkan partial reading.
        duplicate_read_prevention: aktifkan pencegahan duplicate read.
    """

    max_files: int = 8
    max_bytes: int = 60_000
    max_tokens: int = 16_000
    max_depth: int = 1
    max_nodes: int = 30
    relevance_threshold: float = 1.0
    partial_read_limit: int = 200
    partial_read: bool = True
    duplicate_read_prevention: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_files": self.max_files,
            "max_bytes": self.max_bytes,
            "max_tokens": self.max_tokens,
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "relevance_threshold": self.relevance_threshold,
            "partial_read_limit": self.partial_read_limit,
            "partial_read": self.partial_read,
            "duplicate_read_prevention": self.duplicate_read_prevention,
        }


@dataclass(frozen=True)
class RetrievalProfile:
    """Sebuah retrieval profile bernama + budget-nya.

    Attributes:
        name: nama profile (minimal/balanced/deep).
        budget: Budget untuk profile ini.
        description: deskripsi singkat.
    """

    name: str
    budget: Budget
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "budget": self.budget.to_dict()}


#: Nama profile yang dikenal.
PROFILE_NAMES = ("minimal", "balanced", "deep")


class ProfileRegistry:
    """Registry retrieval profile (configurable, tidak hardcode di core).

    Args:
        profiles: mapping nama -> RetrievalProfile.
        default_name: nama profile default.
    """

    def __init__(
        self,
        profiles: Dict[str, RetrievalProfile],
        default_name: str = "balanced",
    ) -> None:
        if not profiles:
            raise ValueError("ProfileRegistry butuh minimal satu profile.")
        self._profiles = dict(profiles)
        self.default_name = default_name if default_name in self._profiles else next(iter(self._profiles))

    def get(self, name: Optional[str] = None) -> RetrievalProfile:
        """Ambil profile berdasarkan nama (fallback ke default)."""
        key = (name or self.default_name).lower()
        return self._profiles.get(key, self._profiles[self.default_name])

    def budget(self, name: Optional[str] = None) -> Budget:
        """Ambil budget untuk profile."""
        return self.get(name).budget

    def names(self) -> list:
        return sorted(self._profiles)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default": self.default_name,
            "profiles": {k: v.to_dict() for k, v in self._profiles.items()},
        }

    @classmethod
    def from_config(cls, config: Any) -> "ProfileRegistry":
        """Bangun registry dari `ContextConfig` (config/settings.py).

        Membaca nilai global + override per-profile. Tidak ada policy yang
        di-hardcode di sini selain struktur profile.
        """
        def _b(**overrides: Any) -> Budget:
            base = dict(
                max_files=config.max_files,
                max_bytes=config.max_bytes,
                max_tokens=config.max_tokens,
                max_depth=config.max_depth,
                max_nodes=config.max_nodes,
                relevance_threshold=config.relevance_threshold,
                partial_read_limit=config.partial_read_limit,
                partial_read=config.partial_read,
                duplicate_read_prevention=config.duplicate_read_prevention,
            )
            base.update(overrides)
            return Budget(**base)

        profiles = {
            "minimal": RetrievalProfile(
                name="minimal",
                description="Sangat ketat: file/symbol paling relevan saja, expansion kecil.",
                budget=_b(
                    max_files=config.minimal_max_files,
                    max_bytes=config.minimal_max_bytes,
                    max_tokens=config.minimal_max_tokens,
                    max_depth=config.minimal_max_depth,
                    max_nodes=config.minimal_max_nodes,
                    relevance_threshold=config.minimal_relevance_threshold,
                ),
            ),
            "balanced": RetrievalProfile(
                name="balanced",
                description="Default: context cukup untuk coding normal, expansion moderat.",
                budget=_b(
                    max_files=config.balanced_max_files,
                    max_bytes=config.balanced_max_bytes,
                    max_tokens=config.balanced_max_tokens,
                    max_depth=config.balanced_max_depth,
                    max_nodes=config.balanced_max_nodes,
                    relevance_threshold=config.balanced_relevance_threshold,
                ),
            ),
            "deep": RetrievalProfile(
                name="deep",
                description="Luas: dependency/dependent diperluas, tetap bounded.",
                budget=_b(
                    max_files=config.deep_max_files,
                    max_bytes=config.deep_max_bytes,
                    max_tokens=config.deep_max_tokens,
                    max_depth=config.deep_max_depth,
                    max_nodes=config.deep_max_nodes,
                    relevance_threshold=config.deep_relevance_threshold,
                ),
            ),
        }
        return cls(profiles, default_name=config.retrieval_profile)

    @classmethod
    def default(cls) -> "ProfileRegistry":
        """Registry default dari settings global."""
        from agent_ai.config.settings import settings

        return cls.from_config(settings.context)
