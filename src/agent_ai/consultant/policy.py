"""Boundary Consultant: memakai Permission Policy Layer AETHER yang sudah ada.

Consultant adalah READ-ONLY terhadap CODE PROJECT, tetapi boleh READ+UPDATE
Project Bible (lewat tool khusus `update_project_bible`).

Modul ini TIDAK membuat subsystem policy baru. Ia hanya MENGONFIGURASI
`PermissionManager`/`PermissionPolicy` existing (agent_ai.permission) dengan
mode yang sesuai untuk Consultant:

    READ_ONLY         -> ALLOW   (baca/inspeksi)
    WORKSPACE_WRITE   -> DENY    (jangan tulis/edit source)
    DELETE_MOVE       -> DENY    (jangan hapus/pindah/rename)
    COMMAND_EXECUTION -> ALLOW   (diagnosis/validasi/build/git read)
    EXTERNAL_NETWORK  -> DENY    (web access belum tersedia)
    UNKNOWN           -> ALLOW   (tool khusus Consultant yang dikurasi,
                                   mis. update_project_bible)

Catatan: registry tool Consultant juga DIKURASI (tanpa write/edit/delete/move),
sehingga boundary berlapis: layer policy + layer registry.
"""

from __future__ import annotations

from agent_ai.permission.manager import PermissionManager
from agent_ai.permission.models import PermissionConfig, PolicyMode
from agent_ai.permission.policy import PermissionPolicy


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
