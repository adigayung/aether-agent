"""Verifier: bound retrieval Project Map Consultant (safety/control layer).

Membungkus `tests/test_consultant_retrieval_bound.py` agar konsisten dengan
verifier lain di `scripts/`. Menguji (tanpa API provider eksternal):

    A. QUICK   : 3 query Atlas berbeda boleh; ke-4 dibatasi; final answer tetap.
    B. Repeated: "safeguard" == "SAFEGUARD" -> query kedua diblokir.
    C. Zero    : query berbeda yang semuanya 0 result TIDAK menghasilkan 40 calls.
    D. Investigate: bound lebih tinggi daripada QUICK.
    E. Agent   : budget/perilaku Agent TIDAK berubah.

Jalankan:
    python scripts/check_consultant_retrieval_bound.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_consultant_retrieval_bound import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
