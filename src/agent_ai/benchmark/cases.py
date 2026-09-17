"""Registry benchmark case + task coding bawaan (#49).

Mendaftarkan benchmark case berdasarkan id. Case mendeskripsikan task coding,
fixture temporary, expected outcome, dan metadata. TIDAK mengandung logic agent.

Task bawaan sengaja KECIL dan benar-benar bisa menguji agent:
    - function_create : membuat fungsi Python sederhana
    - function_edit   : mengubah fungsi Python sederhana
    - bugfix          : memperbaiki bug Python
    - multi_file      : membaca beberapa file lalu melakukan perubahan
    - terminal_validation : task yang membutuhkan terminal + validasi
    - recovery        : task dengan failure/recovery sederhana

Fixture benchmark bersifat temporary/terisolasi (dibuat runner), BUKAN project
AETHER sendiri.
"""

from __future__ import annotations

from typing import Dict, List

from agent_ai.benchmark.models import (
    BenchmarkCase,
    BenchmarkExpected,
    BenchmarkInput,
    FixtureSpec,
)


class BenchmarkCaseRegistry:
    """Kumpulan benchmark case yang terdaftar, diakses lewat id unik."""

    def __init__(self) -> None:
        self._cases: Dict[str, BenchmarkCase] = {}

    def register(self, case: BenchmarkCase) -> None:
        """Daftarkan sebuah case berdasarkan atribut `id`.

        Raises:
            ValueError: bila id case kosong.
        """
        case_id = getattr(case, "id", None)
        if not case_id:
            raise ValueError("BenchmarkCase harus punya atribut 'id' yang unik.")
        self._cases[case_id] = case

    def get(self, case_id: str) -> BenchmarkCase:
        """Ambil case berdasarkan id.

        Raises:
            KeyError: bila id case belum terdaftar.
        """
        if case_id not in self._cases:
            available = ", ".join(sorted(self._cases)) or "(kosong)"
            raise KeyError(f"Benchmark case '{case_id}' tidak terdaftar. Tersedia: {available}")
        return self._cases[case_id]

    def has(self, case_id: str) -> bool:
        """Cek apakah case terdaftar."""
        return case_id in self._cases

    def list(self) -> List[BenchmarkCase]:
        """Daftar semua case (urut berdasarkan id)."""
        return [self._cases[key] for key in sorted(self._cases)]

    def ids(self) -> List[str]:
        """Daftar id case yang terdaftar."""
        return sorted(self._cases)

    def by_category(self, category: str) -> List[BenchmarkCase]:
        """Daftar case berdasarkan kategori."""
        return [c for c in self.list() if c.category == category]

    def __len__(self) -> int:
        return len(self._cases)


# ---------------------------------------------------------------------------
# Task coding bawaan (kecil, terisolasi, provider/model agnostic).
# ---------------------------------------------------------------------------
def builtin_cases() -> List[BenchmarkCase]:
    """Bangun daftar task coding bawaan (kecil & terisolasi)."""
    return [
        BenchmarkCase(
            id="function_create",
            category="function",
            description="Membuat fungsi Python sederhana (add) di file baru.",
            input=BenchmarkInput(
                task="Buat fungsi `add(a, b)` di file `math_utils.py` yang mengembalikan a + b.",
                fixture=FixtureSpec(files={"math_utils.py": "# TODO: implement add\n"}),
                metadata={"language": "python", "entrypoint": "math_utils.py"},
            ),
            expected=BenchmarkExpected(
                success=True,
                expected_tools=["write_file"],
                validation_success=True,
                max_iterations=5,
            ),
        ),
        BenchmarkCase(
            id="function_edit",
            category="function",
            description="Mengubah fungsi Python sederhana (perbaiki return).",
            input=BenchmarkInput(
                task="Perbaiki fungsi `mul(a, b)` di `calc.py` agar mengembalikan a * b (saat ini salah).",
                fixture=FixtureSpec(
                    files={"calc.py": "def mul(a, b):\n    return a + b\n"}
                ),
                metadata={"language": "python", "entrypoint": "calc.py"},
            ),
            expected=BenchmarkExpected(
                success=True,
                expected_tools=["edit_file"],
                validation_success=True,
                max_iterations=5,
            ),
        ),
        BenchmarkCase(
            id="bugfix",
            category="bugfix",
            description="Memperbaiki bug off-by-one pada loop Python.",
            input=BenchmarkInput(
                task="Perbaiki bug pada `sum_list` di `buggy.py` (loop melewatkan elemen terakhir).",
                fixture=FixtureSpec(
                    files={
                        "buggy.py": (
                            "def sum_list(items):\n"
                            "    total = 0\n"
                            "    for i in range(len(items) - 1):\n"
                            "        total += items[i]\n"
                            "    return total\n"
                        )
                    }
                ),
                metadata={"language": "python", "entrypoint": "buggy.py"},
            ),
            expected=BenchmarkExpected(
                success=True,
                expected_tools=["edit_file"],
                validation_success=True,
                max_iterations=6,
            ),
        ),
        BenchmarkCase(
            id="multi_file",
            category="multi_file",
            description="Membaca beberapa file lalu melakukan perubahan konsisten.",
            input=BenchmarkInput(
                task=(
                    "Baca `config.py` dan `app.py`, lalu ubah `app.py` agar memakai "
                    "nilai `TIMEOUT` dari `config.py`."
                ),
                fixture=FixtureSpec(
                    files={
                        "config.py": "TIMEOUT = 30\n",
                        "app.py": "TIMEOUT = 10\n\ndef run():\n    return TIMEOUT\n",
                    }
                ),
                metadata={"language": "python", "entrypoint": "app.py"},
            ),
            expected=BenchmarkExpected(
                success=True,
                expected_tools=["read_file", "edit_file"],
                validation_success=True,
                max_iterations=8,
            ),
        ),
        BenchmarkCase(
            id="terminal_validation",
            category="terminal_validation",
            description="Task yang membutuhkan terminal + validasi (jalankan test).",
            input=BenchmarkInput(
                task=(
                    "Buat fungsi `is_even(n)` di `even.py`, lalu jalankan test "
                    "`python -m pytest test_even.py` untuk memastikan lulus."
                ),
                fixture=FixtureSpec(
                    files={
                        "even.py": "# TODO: implement is_even\n",
                        "test_even.py": (
                            "from even import is_even\n\n"
                            "def test_even():\n"
                            "    assert is_even(2) is True\n"
                            "    assert is_even(3) is False\n"
                        ),
                    }
                ),
                metadata={"language": "python", "entrypoint": "even.py"},
            ),
            expected=BenchmarkExpected(
                success=True,
                expected_tools=["write_file", "run_command"],
                validation_success=True,
                max_iterations=8,
            ),
        ),
        BenchmarkCase(
            id="recovery",
            category="recovery",
            description="Task dengan failure/recovery sederhana (command gagal lalu diperbaiki).",
            input=BenchmarkInput(
                task=(
                    "Jalankan `python broken.py`. Bila gagal, perbaiki `broken.py` "
                    "lalu jalankan ulang sampai berhasil."
                ),
                fixture=FixtureSpec(
                    files={
                        "broken.py": "def main():\n    raise RuntimeError('broken')\n\nmain()\n"
                    }
                ),
                metadata={"language": "python", "entrypoint": "broken.py"},
            ),
            expected=BenchmarkExpected(
                success=True,
                expected_tools=["run_command", "edit_file"],
                validation_success=True,
                max_recovery=3,
                max_iterations=10,
            ),
        ),
    ]


def build_registry() -> BenchmarkCaseRegistry:
    """Bangun registry berisi task coding bawaan."""
    registry = BenchmarkCaseRegistry()
    for case in builtin_cases():
        registry.register(case)
    return registry


# ---------------------------------------------------------------------------
# Registry global (berisi task bawaan). Case tambahan didaftarkan pemanggil.
# ---------------------------------------------------------------------------
registry = build_registry()
