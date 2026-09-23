"""Verifikasi prompt AETHER Consultant (deterministik, tanpa API key/model cloud).

Fokus: DISIPLIN TOOL pada prompt, terutama mode QUICK. Menguji bahwa prompt
yang benar-benar dibangun oleh `build_consultant_system_prompt(...)` memuat
aturan operasional yang mencegah tool-call berulang untuk pertanyaan kecil:

    1. atlas_query / rig_query = pencari LOKASI/RELASI symbol/module/file,
       BUKAN full-text search isi source.
    2. 0 hasil TIDAK memicu pengulangan query / percobaan sinonim tanpa batas.
    3. Pola `query -> evaluasi hasil -> sudah cukup? -> jawab`
       (evidence cukup -> berhenti memanggil tool & jawab).
    4. Pertanyaan kecil dijawab langsung dari Bible/context/evidence; QUICK
       TIDAK melakukan investigasi source-level yang tidak tersedia dan harus
       mengakui keterbatasan + mengarahkan user ke Investigate.
    5. Peta `stale` dipakai dengan caveat, bukan alasan retry tanpa batas.
    6. Regression boundary: prompt QUICK tetap tidak menyebut tool source/runtime
       (list_files/read_file/search_code/run_command) maupun refresh_project_map,
       dan Investigation tetap mempertahankan kemampuan investigasinya.

Jalankan:
    python scripts/check_consultant_prompt.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.consultant.models import (  # noqa: E402
    DEFAULT_CONSULTANT_MODE,
    MODE_INVESTIGATE,
    MODE_QUICK,
)
from agent_ai.consultant.prompt import build_consultant_system_prompt  # noqa: E402


def main() -> int:
    print("=== Verifikasi Prompt Consultant (disiplin tool) ===")

    quick = build_consultant_system_prompt(MODE_QUICK)
    inv = build_consultant_system_prompt(MODE_INVESTIGATE)
    quick_l = quick.lower()
    inv_l = inv.lower()

    # Default mode = quick -> prompt default harus identik dengan prompt quick.
    assert build_consultant_system_prompt(DEFAULT_CONSULTANT_MODE) == quick
    print("[1] prompt dibangun per-mode OK -> quick + investigate")

    # 2) atlas/rig BUKAN full-text search isi source code.
    assert "atlas_query" in quick_l and "rig_query" in quick_l
    assert "full-text" in quick_l, "quick prompt harus menyatakan map BUKAN full-text search"
    assert "bukan pencarian teks" in quick_l, "quick prompt harus menegaskan map bukan pencarian teks"
    assert "lokasi" in quick_l and "relasi" in quick_l, "quick prompt harus menjelaskan map = lokasi/relasi"
    print("[2] map = LOKASI/RELASI (bukan full-text search isi source) OK")

    # 3) 0 hasil tidak memicu retry sinonim tanpa batas.
    assert "0 hasil" in quick_l, "quick prompt harus membahas hasil kosong (0 hasil)"
    assert "sinonim" in quick_l, "quick prompt harus melarang percobaan sinonim tanpa alasan"
    assert "mengulang query" in quick_l, "quick prompt harus melarang mengulang query yang sama"
    assert "tanpa batas" in quick_l, "quick prompt harus melarang retry tanpa batas"
    print("[3] 0 hasil != retry/sinonim tanpa batas OK")

    # 4) Pola query -> evaluasi -> cukup? -> jawab; evidence cukup -> berhenti.
    assert "query -> evaluasi hasil -> sudah cukup? -> jawab" in quick_l, (
        "quick prompt harus memuat pola query -> evaluasi hasil -> sudah cukup? -> jawab"
    )
    assert "cukup" in quick_l and "berhenti" in quick_l and "jawab" in quick_l
    assert "memperbanyak evidence" in quick_l, "quick prompt harus melarang tool untuk memperbanyak evidence"
    print("[4] pola query -> evaluasi -> cukup? -> jawab OK")

    # 5) Pertanyaan kecil dijawab langsung; QUICK tidak investigasi source-level.
    assert "pertanyaan kecil" in quick_l, "quick prompt harus memprioritaskan jawaban langsung"
    assert "investigate" in quick_l, "quick prompt harus mengarahkan user ke Investigate bila perlu"
    assert "keterbatasan" in quick_l, "quick prompt harus meminta mengakui keterbatasan"
    assert "source-level" in quick_l, "quick prompt harus menyebut investigasi source-level tidak tersedia"
    print("[5] QUICK tetap QUICK (jawab langsung / akui keterbatasan -> Investigate) OK")

    # 6) Peta stale: pakai dengan caveat, bukan retry tanpa batas.
    assert "stale" in quick_l, "quick prompt harus membahas peta stale"
    assert "caveat" in quick_l, "quick prompt harus meminta evidence stale dipakai dengan caveat"
    print("[6] peta stale dipakai dengan caveat (bukan retry tanpa batas) OK")

    # 7) Disiplin tool berlaku juga untuk INVESTIGATE (tanpa menghilangkan
    #    kemampuannya) -> bagian generik ikut disuntik.
    assert "disiplin tool" in inv_l, "disiplin tool harus berlaku di semua mode"
    assert "query -> evaluasi hasil -> sudah cukup? -> jawab" in inv_l
    assert "0 hasil" in inv_l and "sinonim" in inv_l
    print("[7] disiplin tool berlaku di INVESTIGATE OK")

    # 8) Regression boundary: prompt QUICK tetap tidak menyebut tool source/
    #    runtime maupun refresh map (sesuai registry quick).
    for forbidden in (
        "list_files",
        "read_file",
        "search_code",
        "run_command",
        "refresh_project_map",
    ):
        assert forbidden not in quick_l, f"prompt QUICK tidak boleh menyebut tool {forbidden}"

    # 9) Investigation tetap mempertahankan kemampuannya.
    assert "investigate" in inv_l
    for required in ("list_files", "read_file", "search_code", "run_command"):
        assert required in inv_l, f"prompt INVESTIGATE harus tetap menyebut {required}"
    print("[8] boundary OK -> QUICK tanpa tool source; INVESTIGATE tetap utuh")

    # 10) Map tetap opsional (tidak dipaksa); cap 'wajib' bebas kecuali boundary.
    assert "hanya bila perlu" in quick_l, "prompt quick harus menyatakan map opsional"
    assert "wajib" not in quick_l.replace("wajib dipatuhi", ""), "prompt tidak boleh memaksa tool"
    print("[9] map tetap opsional (tidak dipaksa) OK")

    print()
    print("[OK] Prompt Consultant memuat disiplin tool anti-eksplorasi berulang.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
