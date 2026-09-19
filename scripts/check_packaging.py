"""Verifikasi Packaging / Deployment dasar AETHER (#56).

Deterministik, tanpa network/build. Memeriksa konfigurasi packaging Python,
dependency runtime, template environment (tanpa credential), dan cara
menjalankan backend/frontend.

Menguji:
    1. pyproject.toml ada & valid (build-system + project + src-layout)
    2. package agent_ai dapat ditemukan dari src/ (setuptools find)
    3. dependency runtime tercatat (pyproject + requirements.txt)
    4. template environment deployment ada & TANPA credential/API key
    5. script menjalankan backend & frontend tersedia
    6. README memuat bagian install/setup/run/deployment
    7. boundary: packaging tidak mengubah arsitektur (agent_ai tetap tanpa Django)
    8. tidak ada Docker/container/orchestrator besar yang ditambahkan

Jalankan:
    python scripts/check_packaging.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    print("=== Verifikasi Packaging / Deployment (#56) ===")
    return _run()


def _run() -> int:
    # 1) pyproject.toml ada & valid.
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml harus ada di root project"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    assert "build-system" in data, "pyproject harus punya [build-system]"
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"
    assert "project" in data, "pyproject harus punya [project]"
    assert data["project"]["name"], "project.name wajib diisi"
    print(f"[1] pyproject.toml valid OK -> name={data['project']['name']!r}")

    # 2) package agent_ai dapat ditemukan dari src/ (src-layout).
    tool = data.get("tool", {}).get("setuptools", {})
    assert tool.get("package-dir", {}).get("") == "src", "package-dir harus src (src-layout)"
    find = tool.get("packages", {}).get("find", {})
    assert find.get("where") == ["src"], "packages.find.where harus ['src']"
    assert any("agent_ai" in pat for pat in find.get("include", [])), "include harus memuat agent_ai*"
    # Package benar-benar ada di src/agent_ai.
    assert (PROJECT_ROOT / "src" / "agent_ai" / "__init__.py").exists(), "src/agent_ai harus ada"
    print("[2] package agent_ai dapat di-install dari root (src-layout) OK")

    # 3) dependency runtime tercatat.
    deps = data["project"].get("dependencies", [])
    dep_blob = " ".join(deps).lower()
    for required in ("django", "python-dotenv", "requests", "pillow"):
        assert required in dep_blob, f"dependency '{required}' harus tercatat di pyproject"
    req_path = PROJECT_ROOT / "requirements.txt"
    assert req_path.exists(), "requirements.txt harus ada"
    req_blob = req_path.read_text(encoding="utf-8").lower()
    for required in ("django", "python-dotenv", "requests", "pillow"):
        assert required in req_blob, f"dependency '{required}' harus tercatat di requirements.txt"
    print(f"[3] dependency runtime tercatat OK -> {len(deps)} dependency di pyproject")

    # 4) template environment deployment ada & TANPA credential/API key.
    template = PROJECT_ROOT / "deployment.template"
    assert template.exists(), "deployment.template harus ada"
    text = template.read_text(encoding="utf-8")
    # Tidak boleh ada nilai credential yang terisi (placeholder harus kosong).
    # Hanya key yang benar-benar sensitif (bukan mis. DEFAULT_MAX_TOKENS).
    sensitive_markers = ("api_key", "secret", "password", "authorization", "access_token")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key_l = key.strip().lower()
        if any(marker in key_l for marker in sensitive_markers):
            assert value.strip() == "", f"template tidak boleh berisi nilai untuk '{key.strip()}'"
    # Tidak boleh ada pola secret yang jelas.
    assert not re.search(r"sk-[A-Za-z0-9]{8,}", text), "template tidak boleh memuat API key nyata"
    # Provider aktif TIDAK lagi ditentukan lewat .env (legacy DEFAULT_PROVIDER
    # dihapus). Pemilihan provider = Provider Instance + Model (SQLite).
    assert "DEFAULT_PROVIDER" not in text, "template tidak boleh memuat DEFAULT_PROVIDER (legacy)"
    print("[4] template environment deployment ada & tanpa credential OK -> deployment.template")

    # 5) script menjalankan backend & frontend tersedia.
    backend = PROJECT_ROOT / "scripts" / "run_backend.ps1"
    frontend = PROJECT_ROOT / "scripts" / "run_frontend.ps1"
    assert backend.exists(), "scripts/run_backend.ps1 harus ada"
    assert frontend.exists(), "scripts/run_frontend.ps1 harus ada"
    backend_src = backend.read_text(encoding="utf-8")
    assert "manage.py" in backend_src and "runserver" in backend_src, "backend harus menjalankan Django runserver"
    frontend_src = frontend.read_text(encoding="utf-8")
    assert "npm run dev" in frontend_src, "frontend harus menjalankan npm run dev"
    print("[5] script menjalankan backend & frontend tersedia OK")

    # 6) README memuat bagian install/setup/run/deployment.
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8").lower()
    for section in ("install", "setup", "run", "deploy"):
        assert section in readme, f"README harus memuat bagian '{section}'"
    print("[6] README memuat bagian install/setup/run/deployment OK")

    # 7) boundary: packaging tidak mengubah arsitektur (agent_ai tetap tanpa Django).
    # Cek tidak ada import Django di package core (bukan sekadar kata "Django").
    core_dir = PROJECT_ROOT / "src" / "agent_ai"
    for py in core_dir.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "import django" not in src.lower(), f"{py.name} tidak boleh mengimpor Django"
        assert "from django" not in src.lower(), f"{py.name} tidak boleh mengimpor Django"
    print("[7] boundary arsitektur tetap valid OK -> agent_ai tetap tanpa Django")

    # 8) tidak ada Docker/container/orchestrator besar.
    for name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "k8s", "helm"):
        assert not (PROJECT_ROOT / name).exists(), f"tidak boleh menambahkan '{name}' (di luar scope #56)"
    print("[8] tidak ada Docker/container/orchestrator besar OK")

    print()
    print("[OK] Packaging / Deployment dasar AETHER siap (installable + run jelas).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
