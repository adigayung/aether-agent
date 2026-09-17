"""Verifikasi Production Hardening AETHER (#57).

Deterministik, tanpa network/build. Memeriksa konfigurasi Django production,
secret tidak hardcoded, deployment template safety, static serving frontend,
boundary gateway/permission, dan backward compatibility development.

Menguji:
    1. secret tidak hardcoded (SECRET_KEY dari environment)
    2. production environment configuration (AETHER_ENV=production)
    3. DEBUG/ALLOWED_HOSTS behavior (dev vs production)
    4. deployment template safety (tanpa credential terisi)
    5. Django security configuration relevan (HSTS, SSL redirect, cookie, dll)
    6. frontend production/deployment path (static serving dist)
    7. gateway boundary (views tipis, tanpa logic AETHER)
    8. permission/security boundary (PermissionManager tetap sumber kebenaran)
    9. tidak ada credential yang bocor di source/template
   10. architecture boundary tetap sehat (agent_ai tanpa Django)
   11. backward compatibility development configuration

Jalankan:
    python scripts/check_production_hardening.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
SETTINGS_PATH = DJANGO_APP_DIR / "config" / "settings.py"


def main() -> int:
    print("=== Verifikasi Production Hardening (#57) ===")
    return _run()


def _load_settings(env: dict) -> dict:
    """Muat settings Django di subprocess dengan environment tertentu.

    Mengembalikan dict nilai settings yang relevan (deterministik).
    """
    code = (
        "import os, json;"
        "os.environ['DJANGO_SETTINGS_MODULE']='config.settings';"
        "import django; django.setup();"
        "from django.conf import settings;"
        "print(json.dumps({"
        "'DEBUG': settings.DEBUG,"
        "'ALLOWED_HOSTS': settings.ALLOWED_HOSTS,"
        "'SECRET_KEY': settings.SECRET_KEY,"
        "'SECURE_SSL_REDIRECT': settings.SECURE_SSL_REDIRECT,"
        "'SESSION_COOKIE_SECURE': settings.SESSION_COOKIE_SECURE,"
        "'CSRF_COOKIE_SECURE': settings.CSRF_COOKIE_SECURE,"
        "'SECURE_HSTS_SECONDS': settings.SECURE_HSTS_SECONDS,"
        "'SECURE_CONTENT_TYPE_NOSNIFF': settings.SECURE_CONTENT_TYPE_NOSNIFF,"
        "'X_FRAME_OPTIONS': settings.X_FRAME_OPTIONS,"
        "'FRONTEND_DIST_EXISTS': settings.FRONTEND_DIST_EXISTS,"
        "}))"
    )
    full_env = dict(os.environ)
    # Bersihkan variabel terkait agar hasil deterministik.
    for key in ("AETHER_ENV", "DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"):
        full_env.pop(key, None)
    full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(DJANGO_APP_DIR),
        env=full_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"Gagal memuat settings: {result.stderr.strip()}")
    import json

    return json.loads(result.stdout.strip().splitlines()[-1])


def _run() -> int:
    settings_src = SETTINGS_PATH.read_text(encoding="utf-8")

    # 1) secret tidak hardcoded (SECRET_KEY dari environment).
    assert "DJANGO_SECRET_KEY" in settings_src, "SECRET_KEY harus dibaca dari environment"
    assert "_env(" in settings_src, "settings harus memakai helper environment"
    # Tidak boleh ada SECRET_KEY literal panjang yang di-hardcode.
    assert not re.search(r'SECRET_KEY\s*=\s*"[A-Za-z0-9]{20,}"', settings_src), \
        "SECRET_KEY tidak boleh di-hardcode"
    print("[1] secret tidak hardcoded OK -> SECRET_KEY dari environment")

    # 2) production environment configuration.
    prod = _load_settings({
        "AETHER_ENV": "production",
        "DJANGO_SECRET_KEY": "prod-secret-xyz-1234567890",
        "DJANGO_DEBUG": "false",
        "DJANGO_ALLOWED_HOSTS": "aether.example.com",
    })
    assert prod["DEBUG"] is False, "production harus DEBUG=false"
    assert prod["ALLOWED_HOSTS"] == ["aether.example.com"], prod["ALLOWED_HOSTS"]
    assert prod["SECRET_KEY"] == "prod-secret-xyz-1234567890"
    print("[2] production environment configuration OK")

    # 3) DEBUG/ALLOWED_HOSTS behavior (dev vs production).
    dev = _load_settings({})
    assert dev["DEBUG"] is True, "development default harus DEBUG=true"
    assert "localhost" in dev["ALLOWED_HOSTS"], dev["ALLOWED_HOSTS"]
    # Production tanpa SECRET_KEY harus fail-fast.
    fail = subprocess.run(
        [sys.executable, "-c",
         "import os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; import django; django.setup()"],
        cwd=str(DJANGO_APP_DIR),
        env={**os.environ, "AETHER_ENV": "production", "DJANGO_SECRET_KEY": "", "DJANGO_DEBUG": "false"},
        capture_output=True, text=True,
    )
    assert fail.returncode != 0, "production tanpa SECRET_KEY harus gagal (fail-fast)"
    assert "SECRET_KEY" in (fail.stderr + fail.stdout), "pesan error harus menyebut SECRET_KEY"
    print("[3] DEBUG/ALLOWED_HOSTS behavior OK -> dev aman, production fail-fast")

    # 4) deployment template safety (tanpa credential terisi).
    template = (PROJECT_ROOT / "deployment.template").read_text(encoding="utf-8")
    sensitive_markers = ("api_key", "secret", "password", "authorization", "access_token")
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key_l = key.strip().lower()
        if any(m in key_l for m in sensitive_markers):
            assert value.strip() == "", f"template tidak boleh berisi nilai untuk '{key.strip()}'"
    assert not re.search(r"sk-[A-Za-z0-9]{8,}", template), "template tidak boleh memuat API key nyata"
    assert "AETHER_ENV" in template and "DJANGO_SECRET_KEY" in template, \
        "template harus memuat konfigurasi production"
    print("[4] deployment template safety OK -> tanpa credential terisi")

    # 5) Django security configuration relevan.
    assert prod["SECURE_SSL_REDIRECT"] is True, "production harus redirect ke HTTPS"
    assert prod["SESSION_COOKIE_SECURE"] is True, "production cookie harus secure"
    assert prod["CSRF_COOKIE_SECURE"] is True, "production CSRF cookie harus secure"
    assert prod["SECURE_HSTS_SECONDS"] > 0, "production harus mengaktifkan HSTS"
    assert prod["SECURE_CONTENT_TYPE_NOSNIFF"] is True
    assert prod["X_FRAME_OPTIONS"] == "DENY"
    # Development tidak memaksa TLS (agar lokal mudah).
    assert dev["SECURE_SSL_REDIRECT"] is False, "development tidak boleh memaksa HTTPS"
    print("[5] Django security configuration relevan OK -> HSTS/SSL/cookie/nosniff/frame")

    # 6) frontend production/deployment path (static serving dist).
    urls_src = (DJANGO_APP_DIR / "config" / "urls.py").read_text(encoding="utf-8")
    assert "FRONTEND_DIST" in settings_src, "settings harus mendefinisikan FRONTEND_DIST_DIR"
    assert "FRONTEND_DIST_EXISTS" in urls_src, "urls harus menyajikan dist bila ada"
    assert "serve" in urls_src, "urls harus memakai static serve untuk dist"
    print("[6] frontend production/deployment path OK -> static serving dist")

    # 7) gateway boundary (views tipis, tanpa logic AETHER).
    views_src = (DJANGO_APP_DIR / "api" / "views.py").read_text(encoding="utf-8")
    for forbidden in ("AgentRuntime", "AgentOrchestrator", "ToolExecutor", "class AgentLoop"):
        assert forbidden not in views_src, f"views tidak boleh memuat logic AETHER: {forbidden}"
    print("[7] gateway boundary OK -> views tipis tanpa logic AETHER")

    # 8) permission/security boundary (PermissionManager tetap sumber kebenaran).
    # Django tidak boleh mendefinisikan ulang policy permission.
    for py in (DJANGO_APP_DIR / "api").glob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "class PermissionManager" not in src, f"{py.name} tidak boleh membuat PermissionManager baru"
        assert "class PermissionPolicy" not in src, f"{py.name} tidak boleh membuat policy baru"
    print("[8] permission/security boundary OK -> PermissionManager tetap sumber kebenaran")

    # 9) tidak ada credential yang bocor di source/template.
    for py in (DJANGO_APP_DIR / "config").glob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert not re.search(r"sk-[A-Za-z0-9]{8,}", src), f"{py.name} tidak boleh memuat API key"
    print("[9] tidak ada credential yang bocor OK")

    # 10) architecture boundary tetap sehat (agent_ai tanpa Django).
    for py in (PROJECT_ROOT / "src" / "agent_ai").rglob("*.py"):
        src = py.read_text(encoding="utf-8").lower()
        assert "import django" not in src and "from django" not in src, \
            f"{py.name} tidak boleh mengimpor Django"
    print("[10] architecture boundary tetap sehat OK -> agent_ai tanpa Django")

    # 11) backward compatibility development configuration.
    assert dev["DEBUG"] is True and "localhost" in dev["ALLOWED_HOSTS"]
    assert dev["SECRET_KEY"], "development harus tetap punya SECRET_KEY default"
    print("[11] backward compatibility development configuration OK")

    print()
    print("[OK] Production Hardening AETHER siap (secret dari env, fail-fast, security aktif).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
