"""Boundary & policy Consultant (SATU sumber kebenaran untuk boundary).

Modul ini mengumpulkan dua hal:

1. Boundary PERMISSION (memakai Permission Policy Layer AETHER yang sudah ada).
   Consultant adalah READ-ONLY terhadap CODE PROJECT, tetapi boleh READ+UPDATE
   Project Bible (lewat tool khusus `update_project_bible`). Modul ini TIDAK
   membuat subsystem policy baru; ia hanya MENGONFIGURASI
   `PermissionManager`/`PermissionPolicy` existing (agent_ai.permission):

       READ_ONLY         -> ALLOW   (baca/inspeksi)
       WORKSPACE_WRITE   -> DENY    (jangan tulis/edit source)
       DELETE_MOVE       -> DENY    (jangan hapus/pindah/rename)
       COMMAND_EXECUTION -> ALLOW   (diagnosis/validasi/build/git read)
       EXTERNAL_NETWORK  -> DENY    (web access belum tersedia)
       UNKNOWN           -> ALLOW   (tool khusus Consultant yang dikurasi,
                                      mis. update_project_bible)

2. Boundary RETRIEVAL (bound jumlah query Project Map per giliran konsultasi).
   Ini SAFETY/CONTROL layer Consultant yang mencegah eksplorasi map yang
   jelas-jelas runaway (mis. `atlas_query` berulang dengan sinonim yang
   semuanya 0 hasil) sebelum menyentuh safeguard generik `max_steps`.

PENTING: nilai di modul ini adalah boundary CONSULTANT, BUKAN budget Agent.
Agent tidak pernah memakai nilai-nilai ini dan perilakunya TIDAK berubah.
Registry tool Consultant juga DIKURASI (tanpa write/edit/delete/move),
sehingga boundary berlapis: policy + registry + retrieval bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from agent_ai.consultant.models import (
    MODE_INVESTIGATE,
    MODE_QUICK,
    normalize_consultant_mode,
)
from agent_ai.permission.manager import PermissionManager
from agent_ai.permission.models import PermissionConfig, PolicyMode
from agent_ai.permission.policy import PermissionPolicy

#: Tool Project Map yang di-bound Consultant (tool PENCARIAN, bukan status).
#: `project_map_status` sengaja TIDAK di-bound: ia hanya membaca status ringkas
#: (tanpa pencarian) dan tidak memicu eksplorasi runaway.
CONSULTANT_MAP_QUERY_TOOLS = ("atlas_query", "rig_query")


@dataclass(frozen=True)
class ConsultantRetrievalBudget:
    """Batas retrieval Project Map untuk SATU giliran konsultasi Consultant.

    Satu sumber kebenaran untuk angka bound Consultant. Ini boundary/safety,
    BUKAN target & BUKAN budget Agent.

    Attributes:
        max_atlas_queries: maksimum query atlas_query yang BENAR-BENAR dijalankan.
        max_rig_queries: maksimum query rig_query yang BENAR-BENAR dijalankan.
        max_zero_result_queries: maksimum query map beruntun yang mengembalikan 0
            hasil sebelum pencarian map dihentikan (pola zero-result runaway).

    Setelah salah satu batas tersentuh, pencarian map dihentikan secara
    graceful: query map berikutnya TIDAK dieksekusi (mengembalikan ToolResult
    "bound" yang jelas), tool map dilepas dari penawaran ke LLM, dan LLM
    diminta menyusun jawaban final dari evidence yang sudah ada.
    """

    max_atlas_queries: int
    max_rig_queries: int
    max_zero_result_queries: int

    def max_queries_for(self, tool_name: str) -> Optional[int]:
        """Batas query untuk sebuah tool map, atau None bila tool bukan map query."""
        if tool_name == "atlas_query":
            return self.max_atlas_queries
        if tool_name == "rig_query":
            return self.max_rig_queries
        return None


#: Preset QUICK: konservatif, cocok untuk percakapan cepat (3 atlas + 3 rig).
QUICK_RETRIEVAL_BUDGET = ConsultantRetrievalBudget(
    max_atlas_queries=3,
    max_rig_queries=3,
    max_zero_result_queries=3,
)

#: Preset INVESTIGATE: lebih tinggi dari QUICK (6 atlas + 6 rig) agar investigasi
#: yang sah tetap punya ruang, tetapi tetap dibatasi (bukan tanpa batas).
INVESTIGATION_RETRIEVAL_BUDGET = ConsultantRetrievalBudget(
    max_atlas_queries=6,
    max_rig_queries=6,
    max_zero_result_queries=5,
)

#: Peta mode -> preset budget (satu sumber kebenaran).
_RETRIEVAL_BUDGETS: Dict[str, ConsultantRetrievalBudget] = {
    MODE_QUICK: QUICK_RETRIEVAL_BUDGET,
    MODE_INVESTIGATE: INVESTIGATION_RETRIEVAL_BUDGET,
}


def retrieval_budget_for_mode(mode: Optional[str]) -> ConsultantRetrievalBudget:
    """Kembalikan preset budget retrieval Consultant untuk sebuah mode.

    Nilai mode kosong / tak dikenal dipetakan ke mode default (``quick``),
    konsisten dengan `normalize_consultant_mode`.
    """
    return _RETRIEVAL_BUDGETS.get(
        normalize_consultant_mode(mode), QUICK_RETRIEVAL_BUDGET
    )


def build_consultant_permission_manager() -> PermissionManager:
    """Bangun PermissionManager dengan policy read-only untuk Consultant."""
    config = PermissionConfig(
        enabled=True,
        read_only=PolicyMode.ALLOW,
        workspace_write=PolicyMode.DENY,
        delete_move=PolicyMode.DENY,
        command_execution=PolicyMode.ALLOW,
        external_network=PolicyMode.DENY,
        unknown=PolicyMode.ALLOW,
    )
    return PermissionManager(policy=PermissionPolicy(config=config))
