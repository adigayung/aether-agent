"""Verifikasi Live Agent Reasoning Status ("Agent reasoning." + titik animasi).

Deterministik, offline, frontend-only, tanpa browser. Memakai pemeriksaan
statis source (Vue/CSS) + SSR render AgentActivity.vue via Vite untuk membuktikan
bahwa elemen reasoning benar-benar dirender (dan tidak dobel).

Menguji (sesuai acceptance criteria):
  1. Sumber state TUNGGAL: App.vue punya `isReasoning` (ref) + `showReasoning`.
  2. Trigger memakai event SSE EXISTING: `provider_request` -> true,
     `provider_response` -> false. Tidak ada polling/endpoint baru.
  3. Terminal event (`task_completed`/`task_failed`/`task_cancelled`),
     `task_started`, dan `resetWorkspace()` -> false (tidak ada animasi nyangkut).
  4. AgentActivity menerima prop `isReasoning` dan merender TEPAT SATU elemen
     reasoning (bukan `v-for`, bukan entri timeline/log baru).
  5. Animasi titik 100% CSS (@keyframes) -> tidak ada timer/interval JS.
  6. SSR: isReasoning=true -> 1 baris reasoning + teks "Agent reasoning" + 3
     titik; isReasoning=false -> 0. Jumlah baris activity bertambah tepat 1.
  7. Reasoning TIDAK masuk ke daftar activity/`timeline` (tidak menambah log).
  8. Kontrak timeline Agent Activity + scroll Explorer tidak berubah.
  9. Tidak ada perubahan backend/Python (git status -- src web/django_app).

Jalankan:
    python scripts/check_reasoning_status.py
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
COMPONENTS = SRC_FRONTEND / "components"

PROBE_ENTRY = """// SSR probe khusus verifier reasoning status (bukan bagian runtime UI).
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";
import AgentActivity from "./src/components/AgentActivity.vue";

export async function renderActivity(props) {
  const app = createSSRApp({ render: () => h(AgentActivity, props || {}) });
  return renderToString(app);
}
"""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ssr_render_states() -> tuple[str, str]:
    """Render AgentActivity dengan isReasoning true/false (SSR, tanpa browser)."""
    node = shutil.which("node")
    assert node, "node tidak tersedia"
    entry = FRONTEND_DIR / "reasoning-ssr-probe.js"
    out_dir = FRONTEND_DIR / ".reasoning-ssr-probe"
    shutil.rmtree(out_dir, ignore_errors=True)
    entry.write_text(PROBE_ENTRY, encoding="utf-8")
    try:
        build = subprocess.run(
            [
                node,
                str(FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"),
                "build",
                "--ssr",
                entry.name,
                "--outDir",
                out_dir.name,
            ],
            cwd=str(FRONTEND_DIR),
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, f"SSR build gagal:\n{build.stdout}\n{build.stderr}"
        script = (
            "import('./.reasoning-ssr-probe/reasoning-ssr-probe.js')"
            ".then(async m => {"
            " const events = [{event_type:'agent_commentary',"
            " payload:{text:'thinking'}, timestamp:'2024-01-01T00:00:00Z'}];"
            " const on = await m.renderActivity({isReasoning:true, status:'running', events});"
            " const off = await m.renderActivity({isReasoning:false, status:'running', events});"
            " process.stdout.write(JSON.stringify({on, off}));"
            "}).catch(e => { console.error(e); process.exit(1); });"
        )
        render = subprocess.run(
            [node, "-e", script], cwd=str(FRONTEND_DIR), capture_output=True, text=True
        )
        assert render.returncode == 0, f"SSR render gagal:\n{render.stdout}\n{render.stderr}"
        data = json.loads(render.stdout)
        return data["on"], data["off"]
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(out_dir, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Live Agent Reasoning Status ===")

    app_src = _read(SRC_FRONTEND / "App.vue")
    activity_src = _read(COMPONENTS / "AgentActivity.vue")
    css_src = _read(SRC_FRONTEND / "styles.css")
    api_src = _read(SRC_FRONTEND / "api.js")

    # --- 1) Sumber state tunggal + dipakai di template ---
    assert "const isReasoning = ref(false)" in app_src, (
        "App.vue harus punya state tunggal isReasoning (ref(false))"
    )
    assert "const showReasoning = computed(" in app_src, (
        "App.vue harus punya computed showReasoning (live-only)"
    )
    assert ':is-reasoning="showReasoning"' in app_src, (
        "App.vue harus meneruskan :is-reasoning ke AgentActivity"
    )
    print("[1] state tunggal isReasoning + showReasoning + prop ke AgentActivity OK")

    # --- 2) Trigger = event SSE existing ---
    assert '"provider_request"' in api_src and '"provider_response"' in api_src, (
        "api.js harus mendengarkan provider_request/provider_response (SSE existing)"
    )
    req_idx = app_src.find('case "provider_request":')
    resp_idx = app_src.find('case "provider_response":')
    assert req_idx != -1 and resp_idx != -1 and req_idx < resp_idx, (
        "App.vue harus menangani provider_request lalu provider_response"
    )
    req_block = app_src[req_idx:resp_idx]
    assert "isReasoning.value = true;" in req_block, (
        "provider_request harus menyalakan isReasoning"
    )
    resp_block = app_src[resp_idx : resp_idx + 400]
    assert "isReasoning.value = false;" in resp_block, (
        "provider_response harus mematikan isReasoning"
    )
    # Tidak ada mekanisme baru (polling/timer/endpoint) untuk fitur ini.
    assert "setInterval" not in app_src, "tidak boleh ada polling/timer baru di App.vue"
    print("[2] trigger memakai event SSE existing (provider_request/provider_response) OK")

    # --- 3) Terminal + lifecycle reset (tidak ada animasi nyangkut) ---
    for evt in ("task_started", "task_completed", "task_failed", "task_cancelled"):
        marker = f'case "{evt}":'
        idx = app_src.find(marker)
        assert idx != -1, f"App.vue harus menangani event '{evt}'"
        block = app_src[idx : idx + 500]
        assert "isReasoning.value = false;" in block, (
            f"event '{evt}' harus mematikan isReasoning"
        )
    assert app_src.count("isReasoning.value = false;") >= 6, (
        "isReasoning harus dimatikan di semua jalur selesai/gagal/cancel/reset"
    )
    print("[3] terminal/lifecycle event mematikan reasoning (resetWorkspace ikut) OK")

    # --- 4) AgentActivity: prop + TEPAT SATU elemen ---
    assert "isReasoning: { type: Boolean, default: false }" in activity_src, (
        "AgentActivity harus punya prop isReasoning"
    )
    assert activity_src.count('v-if="isReasoning"') == 1, (
        "harus TEPAT SATU elemen reasoning (tidak dobel/tidak v-for)"
    )
    assert "reasoning-dots" in activity_src, "indikator harus memakai .reasoning-dots"
    assert activity_src.count("<i>.</i>") == 3, "harus ada tepat 3 titik"
    assert "Agent reasoning" in activity_src, "teks harus 'Agent reasoning'"
    # Elemen berada DI LUAR v-for timeline (bukan entri timeline/log).
    for_idx = activity_src.find('v-for="item in timeline"')
    reasoning_idx = activity_src.find('v-if="isReasoning"')
    assert for_idx != -1 and reasoning_idx > for_idx, (
        "elemen reasoning harus elemen tersendiri, bukan bagian item timeline"
    )
    print("[4] AgentActivity: prop + tepat satu elemen di luar timeline OK")

    # --- 5) Animasi CSS, bukan timer JS ---
    assert "setInterval" not in activity_src, (
        "animasi reasoning TIDAK boleh memakai setInterval (harus CSS @keyframes)"
    )
    assert ".reasoning-dots" in css_src and "animation-iteration-count: infinite" in css_src, (
        "titik harus dianimasikan CSS (infinite)"
    )
    assert ".act-label.reasoning" in css_src, "styling label reasoning tidak ada"

    def _keyframes_block(name: str) -> str:
        start = css_src.find(f"@keyframes {name}")
        assert start != -1, f"@keyframes {name} tidak ada di styles.css"
        nxt = css_src.find("@keyframes", start + 1)
        return css_src[start : nxt if nxt != -1 else len(css_src)]

    # Titik pertama statis terlihat; titik ke-2 menyala pada fase ke-2 dan titik
    # ke-3 pada fase ke-3 -> urutan titik . -> .. -> ... -> . (loop).
    dot1_start = css_src.find(".reasoning-dots i:nth-child(1)")
    assert dot1_start != -1, "rule .reasoning-dots i:nth-child(1) tidak ada"
    dot1_block = css_src[dot1_start : css_src.find("}", dot1_start)]
    assert "opacity: 1" in dot1_block, "titik pertama harus selalu terlihat"
    kf2 = _keyframes_block("reasoning-dot-2")
    kf3 = _keyframes_block("reasoning-dot-3")
    assert "33.33%" in kf2 and "opacity: 1" in kf2, (
        "titik ke-2 harus menyala pada fase ke-2 (33.33%)"
    )
    assert "66.66%" in kf3 and "opacity: 1" in kf3, (
        "titik ke-3 harus menyala pada fase ke-3 (66.66%)"
    )
    assert "opacity: 1" not in kf3.split("66.66%")[0], (
        "titik ke-3 tidak boleh menyala sebelum fase ke-3"
    )
    print("[5] animasi titik murni CSS (@keyframes) dengan fase . -> .. -> ..., tanpa timer JS OK")

    # --- 6/7) SSR: render nyata + reasoning tidak menambah baris activity ---
    on_html, off_html = _ssr_render_states()
    assert on_html.count("reasoning-indicator") == 1, (
        f"isReasoning=true harus merender TEPAT 1 indikator, dapat "
        f"{on_html.count('reasoning-indicator')}"
    )
    assert off_html.count("reasoning-indicator") == 0, (
        "isReasoning=false tidak boleh merender indikator"
    )
    assert "Agent reasoning" in on_html and "Agent reasoning" not in off_html, (
        "teks 'Agent reasoning' hanya saat isReasoning=true"
    )
    assert on_html.count("</i>") == 3, (
        f"harus ada 3 titik ter-render, dapat {on_html.count('</i>')}"
    )
    rows_on = on_html.count('class="act-row')
    rows_off = off_html.count('class="act-row')
    assert rows_on == rows_off + 1, (
        "reasoning harus menambah TEPAT 1 baris (bukan duplikat/entri log): "
        f"on={rows_on} off={rows_off}"
    )
    print(
        f"[6] SSR render OK -> on: {rows_on} baris (1 reasoning), off: {rows_off} baris"
    )

    # Reasoning tidak masuk ke daftar activity/`timeline`.
    tl_start = activity_src.find("const timeline = computed(")
    tl_end = activity_src.find("const empty = computed(")
    assert tl_start != -1 and tl_end > tl_start, "blok timeline tidak ditemukan"
    assert "isReasoning" not in activity_src[tl_start:tl_end], (
        "reasoning tidak boleh masuk ke timeline (itu akan jadi entri activity baru)"
    )
    assert "pushRolling(events.value, isReasoning" not in app_src, (
        "reasoning tidak boleh dikirim ke feed activity"
    )
    print("[7] reasoning TIDAK menambah entri activity/timeline OK")

    # --- 8) Kontrak lama tidak berubah ---
    assert 'v-for="item in timeline"' in activity_src, "timeline Agent Activity harus tetap ada"
    for evt in ("agent_commentary", "tool_called", "tool_completed", "observation_received"):
        assert evt in activity_src, f"AgentActivity harus tetap menampilkan '{evt}'"
    explorer_idx = css_src.find(".explorer {")
    explorer_rule = css_src[explorer_idx : css_src.find("}", explorer_idx)]
    for token in ("flex: 1 1 auto", "min-height: 0", "overflow-y: auto", "overflow-x: hidden"):
        assert token in explorer_rule, f"kontrak scroll Explorer berubah: '{token}' hilang"
    print("[8] timeline Agent Activity + kontrak scroll Explorer tetap utuh OK")

    # --- 9) Tidak ada perubahan backend/Python ---
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "web/django_app"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    dirty = proc.stdout.strip()
    assert dirty == "", f"backend/Python berubah (harusnya kosong):\n{dirty}"
    print("[9] Backend/Python TIDAK berubah (git status -- src web/django_app kosong) OK")

    print()
    print('[OK] Live "Agent reasoning." tampil satu elemen, titik animasi CSS, berhenti')
    print("     saat provider_response/terminal, tanpa menambah entri activity/log.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:  # pragma: no cover
        print(f"[FAIL] {exc}")
        sys.exit(1)
