"""Exception hierarchy untuk sistem konfigurasi LLM.

Dipisahkan agar modul lain dapat menangkap error konfigurasi secara spesifik
tanpa bergantung pada pesan string.
"""

from __future__ import annotations


class LLMConfigError(Exception):
    """Base error untuk sistem konfigurasi LLM."""


class LLMConfigValidationError(LLMConfigError, ValueError):
    """Input konfigurasi tidak valid (mis. nama kosong, provider type salah)."""


class LLMConfigNotFoundError(LLMConfigError, KeyError):
    """Resource konfigurasi (provider instance / model) tidak ditemukan."""

    def __str__(self) -> str:  # KeyError memakai repr() -> rapikan pesan.
        return self.args[0] if self.args else ""


class LLMConfigConflictError(LLMConfigValidationError):
    """Konflik uniqueness (mis. nama provider instance sudah dipakai)."""
