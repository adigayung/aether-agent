"""Verifikasi Collapsible Panel Workbench right column (TASK / CHANGES / EXPLORER).

Deterministik, frontend-only, tanpa browser. Memeriksa kontrak collapse pada
source frontend (Vue + CSS) dan memastikan tidak ada perubahan backend.

Menguji:
  1. EXPLORER default EXPANDED (state lokal di FileExplorer.vue).
  2. CHANGES default COLLAPSED (state lokal di ChangesPanel.vue).
  3. TASK default COLLAPSED di Workbench (prop default-collapsed dari App.vue),
     namun TETAP EXPANDED di Consultant (tidak diubah).
  4. Setiap header dapat diklik -> toggleCollapse; chevron mengikuti state.
  5. Body memakai v-show (konten tidak di-unmount) sehingga Explorer/Task/
     Changes tetap berfungsi normal setelah collapse.
  6. Tombol header Explorer (refresh/up) memakai @click.stop agar tidak
     ikut men-toggle saat diklik.
  7. Kontrak scroll Explorer tetap utuh (single scroll owner .explorer).
  8. Tidak ada perubahan backend/Python (git status -- src web/django_app kosong).

Jalankan:
    python scripts/check_collapsible_panels.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend"
SRC_FRONTEND = FRONTEND_DIR / "src"
COMPONENTS = SRC_FRONTEND / "components"

EXPANDED_CARET = "\u25be"  # ▾
COLLAPSED_CARET = "\u25b8"  # ▸


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_header_collapse(src: str, name: str) -> None:
    """Kontrak umum: state lokal + toggle + header click + body v-show + caret."""
    assert "function toggleCollapse()" in src, f"{name}: toggleCollapse() tidak ada"
    assert "@click=\"toggleCollapse\"" in src, f"{name}: header tidak @click toggleCollapse"
    assert 'v-show="!collapsed"' in src, (
        f"{name}: body harus v-show (bukan v-if) agar konten tidak di-unmount"
    )
    assert (
        f'collapsed ? "{COLLAPSED_CARET}" : "{EXPANDED_CARET}"' in src
    ), f"{name}: chevron tidak mengikuti state collapsed"


def main() -> int:
    print("=== Verifikasi Collapsible Panel (TASK / CHANGES / EXPLORER) ===")

    # --- 1) EXPLORER: default expanded ---
    explorer_src = _read(COMPONENTS / "FileExplorer.vue")
    assert "const collapsed = ref(false)" in explorer_src, (
        "EXPLORER harus default EXPANDED (collapsed = ref(false))"
    )
    check_header_collapse(explorer_src, "FileExplorer.vue")
    # Tombol header tidak boleh ikut toggle collapse.
    assert '@click.stop="load(currentPath)"' in explorer_src, (
        "FileExplorer: tombol Refresh harus @click.stop"
    )
    assert '@click.stop="up"' in explorer_src, "FileExplorer: tombol Up harus @click.stop"
    print("[1] EXPLORER default EXPANDED + header toggle + chevron OK")

    # --- 2) CHANGES: default collapsed ---
    changes_src = _read(COMPONENTS / "ChangesPanel.vue")
    assert "const collapsed = ref(true)" in changes_src, (
        "CHANGES harus default COLLAPSED (collapsed = ref(true))"
    )
    check_header_collapse(changes_src, "ChangesPanel.vue")
    print("[2] CHANGES default COLLAPSED + header toggle + chevron OK")

    # --- 3) TASK: default collapsed di Workbench, tetap expanded di Consultant ---
    queue_src = _read(COMPONENTS / "QueuePanel.vue")
    assert "defaultCollapsed: { type: Boolean, default: false }" in queue_src, (
        "QueuePanel butuh prop defaultCollapsed"
    )
    assert "const collapsed = ref(props.defaultCollapsed)" in queue_src, (
        "QueuePanel harus memakai props.defaultCollapsed sebagai state awal"
    )
    check_header_collapse(queue_src, "QueuePanel.vue")
    print("[3a] TASK state awal mengikuti prop defaultCollapsed OK")

    app_src = _read(SRC_FRONTEND / "App.vue")
    assert ':default-collapsed="true"' in app_src, (
        "Workbench harus memasang QueuePanel dengan :default-collapsed=\"true\""
    )
    assert "<FileExplorer" in app_src and "<ChangesPanel" in app_src, (
        "App.vue harus merender FileExplorer + ChangesPanel di kolom kanan"
    )
    consultant_src = _read(COMPONENTS / "ConsultantChat.vue")
    assert "<QueuePanel" in consultant_src, "ConsultantChat harus tetap merender QueuePanel"
    assert "default-collapsed" not in consultant_src, (
        "Consultant TIDAK boleh diubah default-collapse (tetap expanded)"
    )
    print("[3b] Workbench TASK default COLLAPSED; Consultant TASK tidak diubah OK")

    # --- 4) CSS: chevron + rules collapsed + kontrak scroll Explorer utuh ---
    css_src = _read(SRC_FRONTEND / "styles.css")
    assert ".sec-caret {" in css_src, "CSS .sec-caret (chevron section) tidak ada"
    assert ".block.collapsed" in css_src, "CSS rule .block.collapsed tidak ada"
    assert ".explorer-block.collapsed" in css_src, "CSS rule .explorer-block.collapsed tidak ada"

    idx = css_src.find(".explorer {")
    assert idx != -1, "kontrak scroll Explorer (.explorer) hilang"
    explorer_rule = css_src[idx : css_src.find("}", idx)]
    for token in ("flex: 1 1 auto", "min-height: 0", "overflow-y: auto", "overflow-x: hidden"):
        assert token in explorer_rule, f"kontrak scroll Explorer berubah: '{token}' hilang"

    head_idx = css_src.find(".ex-head {")
    head_rule = css_src[head_idx : css_src.find("}", head_idx)]
    assert "flex: 0 0 auto" in head_rule, ".ex-head harus tetap flex: 0 0 auto"

    wsbody_idx = css_src.find(".ws-body {")
    wsbody_rule = css_src[wsbody_idx : css_src.find("}", wsbody_idx)]
    assert "minmax(0, 1fr)" in wsbody_rule, ".ws-body harus tetap grid-template-rows minmax(0,1fr)"
    print("[4] CSS: chevron + collapsed rules + kontrak scroll Explorer utuh OK")

    # --- 5) Tidak ada perubahan backend ---
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "web/django_app"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    dirty = proc.stdout.strip()
    assert dirty == "", f"backend/Python berubah (harusnya kosong):\n{dirty}"
    print("[5] Backend/Python TIDAK berubah (git status -- src web/django_app kosong) OK")

    print()
    print("[OK] Collapsible Panel (EXPLORER open; TASK & CHANGES closed) bekerja.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:  # pragma: no cover
        print(f"[FAIL] {exc}")
        sys.exit(1)
