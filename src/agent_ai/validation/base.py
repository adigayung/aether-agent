"""Interface/abstract base untuk semua validator.

Validator adalah unit validasi yang provider-agnostic. Setiap validator
mendefinisikan:
    - name: nama unik validator (mis. "command", "syntax", "test").
    - validate(request): menjalankan validasi dan mengembalikan ValidationResult.

Validator berikutnya (syntax, test, build, lint, type check) dapat ditambahkan
tanpa mengubah core: cukup subclass Validator dan daftarkan ke ValidationRunner.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent_ai.validation.models import ValidationRequest, ValidationResult


class ValidatorError(Exception):
    """Base exception untuk error validator (mis. request tidak valid)."""


class Validator(ABC):
    """Abstract base class untuk semua validator."""

    #: Nama unik validator. Wajib di-override oleh subclass.
    name: str = "base"

    @abstractmethod
    def validate(self, request: ValidationRequest) -> ValidationResult:
        """Jalankan validasi untuk sebuah request.

        Args:
            request: ValidationRequest.

        Returns:
            ValidationResult yang sudah dinormalisasi.

        Catatan:
            Validator TIDAK boleh melempar exception untuk kegagalan validasi
            biasa (command gagal/timeout); kegagalan tersebut dikembalikan
            sebagai ValidationResult. Exception hanya untuk error tak terduga.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<{self.__class__.__name__} name={self.name!r}>"
