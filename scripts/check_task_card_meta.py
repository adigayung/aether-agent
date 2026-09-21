"""Verifikasi Task Card metadata: Provider + Model + Execution Timer.

Deterministik, offline, frontend-only, tanpa browser. Memakai pemeriksaan statis
source (Vue/CSS) + uji perilaku helper waktu (timeUtils.js) via Node.

Menguji (sesuai acceptance criteria):
  1. Task Card menampilkan Provider/Model/Duration (blok `.task-meta`).
  2. Provider/Model diambil dari event lifecycle provider_request/provider_response
     (SSE live / log history) — BUKAN default/global.
  3. Timer mulai dari timestamp `task_started` (bukan saat card dibuat).
  4. Timer berhenti pada task_completed / task_failed / task_cancelled.
  5. Durasi dihitung dari timestamp lifecycle yang SUDAH ADA (start/end).
  6. Format durasi: MM:SS, dan HH:MM:SS bila >= 1 jam.
  7. Tidak ada polling backend: hanya ticker TAMPILAN (setInterval) di helper,
     App.vue tidak memakai setInterval.
  8. Task lama tanpa timestamp tetap aman (fallback -> bagian durasi tidak tampil).
  9. Lifecycle stepper yang sekarang tetap dipertahankan.
 10. Kontrak scroll Explorer tidak berubah.
 11. Tidak ada perubahan backend/Python (git status -- src web/django_app).

Jalankan:
    python scripts/check_task_card_meta.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend"
SRC_FRONTEND = FRONTEND_DIR / "src"
DIST_ASSETS = FRONTEND_DIR / "dist" / "assets"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_node_time_utils() -> dict:
    """Uji perilaku timeUtils.js (formatDuration/eventTimeMs/ticker) via Node."""
    node = shutil.which("node")
    assert node, "node tidak tersedia"
    module_url = (SRC_FRONTEND / "timeUtils.js").as_uri()
    script = (
        "const url = process.argv[1];"
        "import(url).then((m) => {"
        "  const fmt = m.formatDuration;"
        "  const t = m.eventTimeMs;"
        "  const tick = m.createDurationTicker(() => {});"
        "  tick.start(); tick.start(); tick.stop(); tick.stop();"
        "  const out = {"
        "    zero: fmt(0),"
        "    sec: fmt(42 * 1000),"
        "    min: fmt((12 * 60 + 34) * 1000),"
        "    hour: fmt((3600 + 12 * 60 + 34) * 1000),"
        "    none: fmt(null),"
        "    neg: fmt(-5),"
        "    epSec: t({ timestamp: 1700000000 }),"
        "    epMs: t({ timestamp: 1700000000000 }),"
        "    iso: t({ timestamp: '1970-01-01T00:00:10Z' }),"
        "    ticker: typeof tick.start === 'function' && typeof tick.stop === 'function'"
        "  };"
        "  process.stdout.write(JSON.stringify(out));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, module_url],
        cwd=str(FRONTEND_DIR),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"node timeUtils test gagal:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def main() -> int:
    print("=== Verifikasi Task Card (Provider + Model + Execution Timer) ===")

    app_src = _read(SRC_FRONTEND / "App.vue")
    css_src = _read(SRC_FRONTEND / "styles.css")
    time_src = _read(SRC_FRONTEND / "timeUtils.js")

    # --- 1) Blok metadata Task Card ---
    assert 'class="task-meta"' in app_src, "Task Card harus punya blok .task-meta"
    for key in ("Provider", "Model", "Duration"):
        assert key in app_src, f"Task Card harus menampilkan '{key}'"
    assert "taskProvider" in app_src and "taskModel" in app_src, (
        "Task Card harus memakai taskProvider/taskModel"
    )
    assert "taskDurationLabel" in app_src, "Task Card harus memakai taskDurationLabel"
    print("[1] Task Card menampilkan Provider/Model/Duration OK")

    # --- 2) Provider/Model dari event lifecycle (bukan default/global) ---
    pm_start = app_src.find("const taskProviderModel = computed(")
    assert pm_start != -1, "App.vue harus punya computed taskProviderModel"
    pm_block = app_src[pm_start : app_src.find("const taskProvider =", pm_start)]
    assert '"provider_request"' in pm_block and '"provider_response"' in pm_block, (
        "Provider/Model harus dibaca dari event provider_request/provider_response"
    )
    assert "payload" in pm_block and "data" in pm_block, (
        "Provider/Model harus mendukung bentuk SSE (payload) dan log (data)"
    )
    assert "config.value" not in pm_block, (
        "Provider/Model Task Card TIDAK boleh memakai default/global config"
    )
    print("[2] Provider/Model dari event lifecycle (bukan default/global) OK")

    # --- 3) Timer mulai dari task_started ---
    assert "const taskStartedAt = ref(null)" in app_src, (
        "App.vue harus punya ref taskStartedAt"
    )
    assert "const taskEndedAt = ref(null)" in app_src, "App.vue harus punya ref taskEndedAt"
    ts_idx = app_src.find('case "task_started":')
    assert ts_idx != -1, "App.vue harus menangani event task_started"
    ts_block = app_src[ts_idx : ts_idx + 700]
    assert "taskStartedAt.value = eventTimeMs(evt)" in ts_block, (
        "task_started harus memulai timer dari timestamp event"
    )
    assert "taskStartedAt.value == null" in ts_block, (
        "task_started tidak boleh me-reset timer (guard == null)"
    )
    print("[3] Timer mulai dari task_started (diset sekali, tidak reset) OK")

    # --- 4) Timer berhenti pada status final ---
    for evt in ("task_completed", "task_failed", "task_cancelled"):
        idx = app_src.find(f'case "{evt}":')
        assert idx != -1, f"App.vue harus menangani event '{evt}'"
        block = app_src[idx : idx + 700]
        assert "taskEndedAt.value = eventTimeMs(evt)" in block, (
            f"event '{evt}' harus menghentikan timer (taskEndedAt)"
        )
    assert app_src.count("taskEndedAt.value = eventTimeMs(evt)") >= 3, (
        "timer harus berhenti untuk completed/failed/cancelled"
    )
    print("[4] Timer berhenti pada completed/failed/cancelled OK")

    # --- 5) Durasi dari timestamp lifecycle (start/end) ---
    assert "const taskDurationMs = computed(" in app_src, "harus ada computed taskDurationMs"
    dur_start = app_src.find("const taskDurationMs = computed(")
    dur_block = app_src[dur_start : app_src.find("const taskDurationLabel", dur_start)]
    assert "taskStartedAt.value" in dur_block and "taskEndedAt.value" in dur_block, (
        "durasi harus = end - start (atau now - start saat berjalan)"
    )
    assert "const taskTimerLive = computed(" in app_src, "harus ada taskTimerLive"
    assert "case \"task_started\"" in app_src or 'case "task_started"' in app_src
    # History task: timing dihitung dari event log.
    assert "function applyHistoryTiming(" in app_src, "harus ada applyHistoryTiming"
    assert "applyHistoryTiming(info, historyEvents.value)" in app_src, (
        "openHistoryTask harus memanggil applyHistoryTiming"
    )
    assert "function isCurrentTaskEvent(" in app_src, (
        "event task lain tidak boleh mengubah timing (guard isCurrentTaskEvent)"
    )
    print("[5] Durasi dari timestamp lifecycle (live + history log) OK")

    # --- 6) Format durasi MM:SS / HH:MM:SS (uji perilaku) ---
    out = _run_node_time_utils()
    assert out["zero"] == "00:00", f"formatDuration(0) salah: {out['zero']}"
    assert out["sec"] == "00:42", f"formatDuration(42s) salah: {out['sec']}"
    assert out["min"] == "12:34", f"formatDuration(12m34s) salah: {out['min']}"
    assert out["hour"] == "01:12:34", f"formatDuration(1h12m34s) salah: {out['hour']}"
    assert out["none"] == "", "formatDuration(null) harus '' (task lama aman)"
    assert out["neg"] == "", "formatDuration(<0) harus ''"
    assert out["epSec"] == 1700000000000, "eventTimeMs harus konversi epoch detik -> ms"
    assert out["epMs"] == 1700000000000, "eventTimeMs harus dukung epoch ms apa adanya"
    assert out["iso"] == 10000, "eventTimeMs harus parse ISO string"
    assert out["ticker"] is True, "createDurationTicker harus expose start/stop"
    assert "export function formatDuration" in time_src
    assert "export function eventTimeMs" in time_src
    assert "export function createDurationTicker" in time_src
    print("[6] Format durasi MM:SS / HH:MM:SS benar (uji perilaku Node) OK")

    # --- 7) Tanpa polling backend; ticker hanya untuk tampilan ---
    assert "setInterval" not in app_src, (
        "App.vue tidak boleh memakai setInterval (ticker tampilan ada di timeUtils)"
    )
    assert "setInterval" in time_src, "ticker tampilan hidup di helper timeUtils.js"
    assert "setInterval" not in css_src
    # Tidak ada endpoint/polling baru: hanya event SSE existing.
    for forbidden in ("fetch(\"/tasks\", ", "openEventStream(", "XMLHttpRequest"):
        assert forbidden not in time_src, "helper waktu tidak boleh memanggil backend"
    assert "taskStartedAt" in app_src and "taskEndedAt" in app_src
    print("[7] Tidak ada polling backend (ticker tampilan terpisah di helper) OK")

    # --- 8) Task lama tanpa timestamp aman ---
    assert "if (!taskStartedAt.value) return null" not in app_src
    assert app_src.count("stopDurationTimer();") >= 2, (
        "timer harus dibersihkan (resetWorkspace + applyHistoryTiming/unmount)"
    )
    assert "stopDurationTimer()" in app_src[app_src.find("onBeforeUnmount(") :][:200], (
        "stopDurationTimer harus dipanggil di onBeforeUnmount (tanpa timer nyangkut)"
    )
    print("[8] Task lama tanpa timestamp aman + timer dibersihkan (unmount) OK")

    # --- 9) Lifecycle stepper tetap dipertahankan ---
    assert "const LIFECYCLE_STEPS =" in app_src, "LIFECYCLE_STEPS harus tetap ada"
    assert app_src.count('"Planning", "Inspecting", "Editing", "Running", "Validating", "Completed"') == 1, (
        "6 langkah lifecycle harus tetap sama"
    )
    for token in ('class="lifecycle"', "lifecycleSteps", "lifecyclePct"):
        assert token in app_src, f"lifecycle stepper berubah: '{token}' hilang"
    print("[9] Lifecycle stepper yang sekarang tetap dipertahankan OK")

    # --- 10) Kontrak scroll Explorer tidak berubah ---
    explorer_idx = css_src.find(".explorer {")
    explorer_rule = css_src[explorer_idx : css_src.find("}", explorer_idx)]
    for token in ("flex: 1 1 auto", "min-height: 0", "overflow-y: auto", "overflow-x: hidden"):
        assert token in explorer_rule, f"kontrak scroll Explorer berubah: '{token}' hilang"
    exhead_idx = css_src.find(".ex-head {")
    exhead_rule = css_src[exhead_idx : css_src.find("}", exhead_idx)]
    assert "flex: 0 0 auto" in exhead_rule, "kontrak .ex-head berubah"
    assert ".task-meta {" in css_src, "styles.css harus punya .task-meta"
    assert ".tm-val {" in css_src, "styles.css harus punya .tm-val"
    assert ".tm-duration.live .tm-val" in css_src, "styles.css harus punya state live duration"
    print("[10] Kontrak scroll Explorer + styling Task Card OK")

    # --- Bundle ter-build memuat fitur ---
    assert DIST_ASSETS.is_dir(), "dist/assets tidak ada (jalankan vite build)"
    bundle_css = ""
    bundle_js = ""
    for path in DIST_ASSETS.iterdir():
        if path.suffix == ".css":
            bundle_css += _read(path)
        elif path.suffix == ".js" and path.name.startswith("index-"):
            bundle_js += _read(path)
    for token in ("task-meta", "tm-duration", "tm-val"):
        assert token in bundle_css, f"bundle CSS tidak memuat '{token}'"
    # Di bundle JS, nama variabel di-minify -> gunakan string yang stabil
    # (class/label yang dirender tetap literal).
    for token in ("task-meta", "tm-item", "tm-duration", "Duration"):
        assert token in bundle_js, f"bundle JS tidak memuat '{token}'"
    print("[*] Bundle build memuat Task Card metadata OK")

    # --- 11) Tidak ada perubahan backend/Python ---
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "web/django_app"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    dirty = proc.stdout.strip()
    assert dirty == "", f"backend/Python berubah (harusnya kosong):\n{dirty}"
    print("[11] Backend/Python TIDAK berubah (git status -- src web/django_app kosong) OK")

    print()
    print("[OK] Task Card lebih informatif: Provider + Model + Duration (live -> final),")
    print("     tanpa polling backend, tanpa mengubah lifecycle/scroll/backend.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:  # pragma: no cover
        print(f"[FAIL] {exc}")
        sys.exit(1)
