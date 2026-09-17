"""LIVE TEST: pilihan provider/model UI benar-benar dipakai runtime (provider NYATA).

Berbeda dari check_model_selection.py (provider fake), script ini memakai
provider NYATA dari konfigurasi AETHER (.env) dan memverifikasi lewat event
`provider_request` bahwa provider+model yang dipilih benar-benar dipanggil.

Fixture project dibuat di J:\\Agent_Ai\\dummy_test\\live_project dan
dibersihkan setelah test.

Jalankan:
    python scripts/live_test_model_selection.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "live_project"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _wait_for_terminal(service, task_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["status"] in ("completed", "failed"):
            return rec
        time.sleep(0.5)
    return service.get_task(task_id)


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django

    django.setup()

    from agent_ai.config.settings import settings

    import api.services as services_mod
    from api.execution import TaskExecutor

    # Provider NYATA dari konfigurasi AETHER.
    provider_name = "deepseek"
    model_name = settings.deepseek.model
    print(f"Provider dipilih : {provider_name}")
    print(f"Model dipilih    : {model_name}")
    print(f"Default provider : {settings.default_provider} (harus BERBEDA)")
    assert provider_name != settings.default_provider, "test butuh provider != default"

    store = services_mod.InMemorySessionStore()
    executor_bridge = TaskExecutor(store)
    service = services_mod.GatewayService(
        session_store=store,
        task_executor=executor_bridge,
        auto_execute=True,
    )
    services_mod._default_service = service

    project = service.create_project(name="LiveModelSel", path=str(FIXTURE))
    project_id = project["id"]

    # Task dengan provider/model eksplisit (deepseek, bukan default ollama).
    record = service.create_task(
        task="Buat file live.txt berisi teks: LIVE OK",
        project_id=project_id,
        metadata={"provider": provider_name, "model": model_name},
    )
    task_id = record["task_id"]
    print(f"\nTask dibuat: {task_id[:8]}... (menunggu provider nyata)...")

    final = _wait_for_terminal(service, task_id)
    print(f"Status akhir: {final['status']} (iterations={final['runtime'].get('iterations')})")

    # Verifikasi provider+model yang benar-benar dipakai dari event.
    events = service.sessions.get_events(task_id=task_id)
    req_events = [e for e in events if e.event_type.value == "provider_request"]
    assert req_events, "harus ada event provider_request"
    used_providers = {e.payload.get("provider") for e in req_events}
    used_models = {e.payload.get("model") for e in req_events}
    print(f"\nProvider yang BENAR-BENAR dipakai (dari event): {used_providers}")
    print(f"Model yang BENAR-BENAR dipakai (dari event)   : {used_models}")

    assert used_providers == {provider_name}, f"provider harus {provider_name}, dapat {used_providers}"
    assert used_models == {model_name}, f"model harus {model_name}, dapat {used_models}"
    print("\n[OK] Provider+model terpilih BENAR-BENAR dipakai runtime (bukan default).")

    # Bukti tambahan: file dibuat di project root.
    written = FIXTURE / "live.txt"
    if written.exists():
        print(f"[OK] File dibuat di project root: {written} -> {written.read_text(encoding='utf-8').strip()!r}")
    else:
        print(f"[INFO] File live.txt belum dibuat (status={final['status']}); provider tetap terverifikasi via event.")

    return 0


def main() -> int:
    print("=== LIVE TEST: Model Selection (provider NYATA) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
