"""ValidationRunner: menjalankan validator dan menghasilkan ValidationResult.

Runner memegang kumpulan validator (registry sederhana) dan menjalankannya
berdasarkan nama atau semua. Validator baru dapat didaftarkan tanpa mengubah
core runner.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from agent_ai.validation.base import Validator
from agent_ai.validation.models import ValidationRequest, ValidationResult


class ValidationRunner:
    """Menjalankan validator terdaftar.

    Args:
        validators: daftar validator awal (opsional).
    """

    def __init__(self, validators: Optional[List[Validator]] = None) -> None:
        self._validators: Dict[str, Validator] = {}
        for validator in validators or []:
            self.register(validator)

    def register(self, validator: Validator) -> None:
        """Daftarkan validator berdasarkan atribut `name`.

        Raises:
            ValueError: bila nama validator kosong atau masih "base".
        """
        name = getattr(validator, "name", None)
        if not name or name == "base":
            raise ValueError("Validator harus punya atribut 'name' yang unik dan bukan 'base'.")
        self._validators[name.lower()] = validator

    def has(self, name: str) -> bool:
        """Cek apakah validator terdaftar."""
        return (name or "").lower() in self._validators

    def list_validators(self) -> List[str]:
        """Daftar nama validator yang terdaftar."""
        return sorted(self._validators)

    def get(self, name: str) -> Validator:
        """Ambil validator berdasarkan nama.

        Raises:
            KeyError: bila validator belum terdaftar.
        """
        key = (name or "").lower()
        if key not in self._validators:
            available = ", ".join(sorted(self._validators)) or "(kosong)"
            raise KeyError(f"Validator '{name}' tidak terdaftar. Tersedia: {available}")
        return self._validators[key]

    def run(self, request: ValidationRequest, validator: Optional[str] = None) -> ValidationResult:
        """Jalankan satu validator (default: validator pertama terdaftar).

        Args:
            request: ValidationRequest.
            validator: nama validator. Bila None, pakai validator pertama.

        Returns:
            ValidationResult.
        """
        if validator is not None:
            target = self.get(validator)
        else:
            if not self._validators:
                raise KeyError("Tidak ada validator terdaftar.")
            target = next(iter(self._validators.values()))
        return target.validate(request)

    def run_all(self, request: ValidationRequest) -> List[ValidationResult]:
        """Jalankan semua validator terdaftar (urut nama)."""
        return [self._validators[name].validate(request) for name in self.list_validators()]
