"""Verifikasi Token Usage pada Task Card / Task Activity.

Deterministik & offline (tanpa browser/model/API cloud). Membuktikan:

    1. Sumber angka = token usage AKTUAL dari provider (payload `usage`),
       BUKAN counter/estimasi token baru.
    2. Backend menyertakan `usage` pada event `provider_response` HANYA bila
       provider melaporkannya (bentuk OpenAI / kanonik / Ollama).
    3. Helper `response_usage` menormalkan bentuk provider -> total.
    4. Format tampilan sesuai spesifikasi revisi:
         825 -> 825 | 1,250 -> 1.25K | 12,500 -> 12.5K | 140,500 -> 140.5K
         1,250,000 -> 1.25M | 140,500,000 -> 140.5M | 1,250,000,000 -> 1.25B
    5. Provider TIDAK melaporkan usage -> "—" (bukan nilai dummy).
    6. Tooltip memakai angka penuh.
    7. Boundary: TIDAK ada tokenizer/estimator lokal; frontend tipis.

Jalankan:
    python scripts/check_token_usage.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
FRONTEND = PROJECT_ROOT / "web" / "frontend"
FRONTEND_SRC = FRONTEND / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> int:
    print("=== Verifikasi Token Usage (Task Card / Task Activity) ===")
    return _run()


def _run() -> int:
    # -------------------------------------------------------------------
    # 1) Backend helper: normalisasi usage provider (tanpa counter baru).
    # -------------------------------------------------------------------
    from agent_ai.core.response import response_usage

    # OpenAI-compatible.
    assert response_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 50}}) == {
        "prompt": 100,
        "completion": 50,
        "total": 150,
    }
    assert response_usage({"usage": {"total_tokens": 999}}) == {"total": 999}
    # Kanonik AETHER.
    assert response_usage({"usage": {"prompt": 10, "completion": 5, "total": 15}}) == {
        "prompt": 10,
        "completion": 5,
        "total": 15,
    }
    # Ollama native.
    assert response_usage({"prompt_eval_count": 20, "eval_count": 12}) == {
        "prompt": 20,
        "completion": 12,
        "total": 32,
    }
    # Tanpa usage -> None (UI menampilkan "—").
    assert response_usage({}) is None
    assert response_usage({"usage": {}}) is None
    assert response_usage(None) is None
    print("[1] response_usage menormalkan usage provider (bukan counter baru) OK")

    # Pastikan payload `usage` TIDAK dibuang sanitasi observability.
    from agent_ai.core.observability import sanitize_event_payload

    cleaned = sanitize_event_payload(
        "provider_response", {"usage": {"prompt": 1, "completion": 2, "total": 3}}
    )
    assert cleaned.get("usage") == {"prompt": 1, "completion": 2, "total": 3}, cleaned
    print("[2] sanitasi observability mempertahankan angka usage OK")

    # -------------------------------------------------------------------
    # 3) Orchestrator mengirim `usage` pada event provider_response.
    # -------------------------------------------------------------------
    orch = (SRC_DIR / "agent_ai" / "core" / "orchestrator.py").read_text(encoding="utf-8")
    assert "_provider_response_payload" in orch, "orchestrator harus punya helper payload"
    assert 'payload["usage"] = usage' in orch, "payload harus menyertakan usage bila ada"
    assert orch.count("self._provider_response_payload(response)") >= 2, (
        "kedua jalur provider_response (run + continuous) harus memakai helper"
    )
    print("[3] orchestrator menyertakan usage pada provider_response (kedua jalur) OK")

    # -------------------------------------------------------------------
    # 4) Frontend: helper formatter murni + uji perilaku (Node).
    # -------------------------------------------------------------------
    token_js = FRONTEND_SRC / "tokenFormat.js"
    assert token_js.exists(), "tokenFormat.js harus ada (helper murni)"
    token_src = token_js.read_text(encoding="utf-8")
    for name in ("export function usageTokens(", "export function formatTokens(", "export function formatTokensFull("):
        assert name in token_src, f"tokenFormat.js harus punya {name}"
    # TIDAK ada tokenizer/estimasi lokal.
    for bad in ("tokenizer", "estimateTokens", "encode("):
        assert bad not in token_src, f"tokenFormat.js tidak boleh memuat '{bad}'"

    test_file = FRONTEND_SRC / "tokenFormat.test.mjs"
    assert test_file.exists(), "tokenFormat.test.mjs harus ada (uji perilaku)"
    node = shutil.which("node")
    assert node, "node tidak tersedia untuk uji perilaku formatter"
    proc = subprocess.run(
        [node, str(test_file)], cwd=str(FRONTEND), capture_output=True, text=True
    )
    assert proc.returncode == 0, f"tokenFormat.test.mjs gagal:\n{proc.stdout}\n{proc.stderr}"
    print(f"[4] formatter token + uji perilaku Node OK -> {proc.stdout.strip()}")

    # -------------------------------------------------------------------
    # 5) App.vue: memakai helper + menampilkan Tokens di Task Card.
    # -------------------------------------------------------------------
    # App.vue = composition root; formatter token dipakai di ./composables/*.
    app_src = (FRONTEND_SRC / "App.vue").read_text(encoding="utf-8") + "\n" + "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((FRONTEND_SRC / "composables").glob("*.js"))
    )
    assert ('from "./tokenFormat.js"' in app_src or 'from "../tokenFormat.js"' in app_src), (
        "frontend harus mengimpor helper tokenFormat.js"
    )
    assert "<span class=\"tm-key\">Tokens</span>" in app_src, "Task Card harus menampilkan Tokens"
    assert "taskTokensLabel" in app_src and "taskTokensTooltip" in app_src, (
        "Task Card harus memakai taskTokensLabel + tooltip angka penuh"
    )
    assert "function usageTokens(" not in app_src, (
        "App.vue TIDAK boleh mendefinisikan ulang usageTokens (satu sumber: tokenFormat.js)"
    )
    for key in ('"total_tokens"', '"prompt_tokens"', '"completion_tokens"'):
        assert key in token_src, f"helper harus mengenali {key}"
    print("[5] App.vue memakai helper + menampilkan Tokens (tanpa duplikasi) OK")

    # -------------------------------------------------------------------
    # 6) Bundle build memuat fitur (bila dist ada).
    # -------------------------------------------------------------------
    dist_assets = FRONTEND / "dist" / "assets"
    if dist_assets.is_dir():
        bundle_js = ""
        for path in dist_assets.iterdir():
            if path.suffix == ".js" and path.name.startswith("index-"):
                bundle_js += path.read_text(encoding="utf-8", errors="replace")
        assert "Tokens" in bundle_js, "bundle JS harus memuat label Tokens"
        print("[6] bundle build memuat label Tokens OK")
    else:
        print("[6] SKIP -> dist belum di-build (jalankan 'npm run build' di web/frontend)")

    print()
    print("[OK] Token Usage tampil di Task Card (usage provider aktual, format compact, tooltip penuh).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
