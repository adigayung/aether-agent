"""Verifikasi Statics Dashboard UI (TASK 4) pada frontend AETHER.

Deterministik & offline (tanpa browser/model/API cloud). Membuktikan:

    1. Menu "Statics" ada di sidebar TEPAT SETELAH "Extension" (urutan item
       navigasi lain tidak berubah) + page title/description tersedia.
    2. Halaman StaticsView dirender oleh App.vue pada activeNav === 'statics'.
    3. API client memanggil `GET /api/statics` dengan project_id (per-project).
    4. Composable `useStatics` mengisolasi data per-project (guard generasi:
       respons basi dibuang, error tidak menyisakan data project lama).
    5. Halaman memakai helper presentasi existing (`tokenFormat.js`) dan tidak
       melakukan scan filesystem / agregasi lintas-project di frontend.
    6. Uji perilaku Node (`useStatics.test.mjs`) lulus.
    7. (opsional) bundle build memuat fitur bila `dist/` ada.

Jalankan:
    python scripts/check_statics_ui.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND = PROJECT_ROOT / "web" / "frontend"
FRONTEND_SRC = FRONTEND / "src"
SHELL_STATE = FRONTEND_SRC / "composables" / "useShellState.js"


def main() -> int:
    print("=== Verifikasi Statics Dashboard UI (TASK 4) ===")
    return _run()


def _run() -> int:
    # -------------------------------------------------------------------
    # 1) Sidebar: "Statics" tepat setelah "Extension".
    # -------------------------------------------------------------------
    shell_src = SHELL_STATE.read_text(encoding="utf-8")
    assert 'id: "extension"' in shell_src, "item navigasi 'extension' harus ada"
    assert 'id: "statics"' in shell_src, "item navigasi 'statics' harus ditambahkan"
    ext_pos = shell_src.index('id: "extension"')
    sta_pos = shell_src.index('id: "statics"')
    settings_pos = shell_src.index('id: "settings"')
    assert ext_pos < sta_pos < settings_pos, (
        "Statics harus berada TEPAT SETELAH Extension dan SEBELUM Settings"
    )
    assert 'label: "Statics"' in shell_src, "label menu harus 'Statics'"
    assert 'if (activeNav.value === "statics") return "Statics";' in shell_src, (
        "pageTitle harus mengenali halaman Statics"
    )
    assert "Token usage, task activity, and provider/model usage" in shell_src, (
        "pageDesc Statics harus tersedia"
    )
    # Menu lain tidak berubah urutannya.
    for earlier in ('id: "agent"', 'id: "tasks"', 'id: "projects"', 'id: "backup"'):
        assert shell_src.index(earlier) < ext_pos, f"{earlier} harus tetap sebelum Extension"
    print("[1] Menu Statics ada tepat setelah Extension (+ title/desc) OK")

    # -------------------------------------------------------------------
    # 2) App.vue merender StaticsView pada activeNav === 'statics'.
    # -------------------------------------------------------------------
    app_src = (FRONTEND_SRC / "App.vue").read_text(encoding="utf-8")
    assert 'import StaticsView from "./components/StaticsView.vue";' in app_src, (
        "App.vue harus mengimpor StaticsView"
    )
    assert "activeNav === 'statics'" in app_src, "App.vue harus merender section Statics"
    assert "<StaticsView" in app_src, "StaticsView harus dipakai di template"
    assert ':project-id="activeProject ? activeProject.id : \'\'"' in app_src, (
        "StaticsView harus menerima project aktif (per-project)"
    )
    print("[2] App.vue merender StaticsView dengan project aktif OK")

    # -------------------------------------------------------------------
    # 3) API client: GET /api/statics + project_id.
    # -------------------------------------------------------------------
    api_src = (FRONTEND_SRC / "api.js").read_text(encoding="utf-8")
    assert "export function getStatics(" in api_src, "api.js harus punya getStatics()"
    assert 'return request(`/statics${' in api_src, "getStatics harus memanggil /statics"
    assert 'params.set("project_id", projectId)' in api_src, (
        "getStatics harus menyertakan project_id"
    )
    for key in ("group_by", "activity", "status", "from", "to", "limit", "offset"):
        assert f'params.set("{key}"' in api_src, f"getStatics harus mendukung parameter {key}"
    print("[3] api.js memanggil GET /api/statics (per-project + filter) OK")

    # -------------------------------------------------------------------
    # 4) Composable useStatics: isolasi project + guard respons basi.
    # -------------------------------------------------------------------
    statics_js = FRONTEND_SRC / "composables" / "useStatics.js"
    assert statics_js.exists(), "composables/useStatics.js harus ada"
    statics_src = statics_js.read_text(encoding="utf-8")
    assert "export function useStatics(" in statics_src, "useStatics harus di-export"
    assert "requestSeq" in statics_src, "harus ada penanda generasi (anti respons basi)"
    assert "if (seq !== requestSeq) return;" in statics_src, (
        "respons basi harus dibuang (guard generasi)"
    )
    assert "loadedProjectId" in statics_src, "harus melacak project yang dimuat"
    assert "isCurrent" in statics_src, "harus mengekspos isCurrent (data project aktif)"
    # Tidak ada scan filesystem / agregasi lintas-project di frontend.
    for bad in ("fetch(", "/files", "readdir", "glob(", "node:fs"):
        assert bad not in statics_src, f"useStatics tidak boleh memuat '{bad}'"
    print("[4] useStatics: per-project + guard respons basi OK")

    # -------------------------------------------------------------------
    # 5) Halaman: pakai helper existing + tidak scan filesystem.
    # -------------------------------------------------------------------
    view_src = (FRONTEND_SRC / "components" / "StaticsView.vue").read_text(encoding="utf-8")
    assert 'from "../tokenFormat.js"' in view_src, (
        "StaticsView harus memakai helper tokenFormat.js yang sudah ada"
    )
    assert "useStatics(" in view_src, "StaticsView harus memakai composable useStatics"
    assert "totals" in view_src and "breakdowns" in view_src and "daily" in view_src, (
        "dashboard harus menampilkan overview/daily/breakdowns"
    )
    for bad in ("readdir", "glob(", "fs.", 'from "fs'):
        assert bad not in view_src, f"StaticsView tidak boleh memuat '{bad}'"
    print("[5] StaticsView: pakai tokenFormat.js + useStatics (tanpa filesystem) OK")

    # -------------------------------------------------------------------
    # 6) Uji perilaku Node (guard project / respons basi).
    # -------------------------------------------------------------------
    test_file = FRONTEND_SRC / "useStatics.test.mjs"
    assert test_file.exists(), "useStatics.test.mjs harus ada (uji perilaku)"
    node = shutil.which("node")
    assert node, "node tidak tersedia untuk uji perilaku useStatics"
    proc = subprocess.run(
        [node, str(test_file)], cwd=str(FRONTEND), capture_output=True, text=True
    )
    assert proc.returncode == 0, f"useStatics.test.mjs gagal:\n{proc.stdout}\n{proc.stderr}"
    last_line = proc.stdout.strip().splitlines()[-1]
    print(f"[6] uji perilaku Node useStatics OK -> {last_line}")

    # -------------------------------------------------------------------
    # 7) Bundle build memuat fitur (bila dist ada).
    # -------------------------------------------------------------------
    dist_assets = FRONTEND / "dist" / "assets"
    if dist_assets.is_dir():
        bundle_js = ""
        for path in dist_assets.iterdir():
            if path.suffix == ".js" and path.name.startswith("index-"):
                bundle_js += path.read_text(encoding="utf-8", errors="replace")
        assert "/statics" in bundle_js, "bundle JS harus memuat endpoint /statics"
        assert "Statics" in bundle_js, "bundle JS harus memuat label Statics"
        print("[7] bundle build memuat fitur Statics OK")
    else:
        print("[7] SKIP -> dist belum di-build (jalankan 'npm run build' di web/frontend)")

    print()
    print("[OK] Statics Dashboard UI terpasang (sidebar -> Statics, per-project, API /api/statics).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
