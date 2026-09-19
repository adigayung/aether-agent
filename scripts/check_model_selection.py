"""Verifikasi model selection: pilihan provider/model UI benar-benar dipakai runtime.

Menguji titik putus yang diperbaiki: pilihan provider/model dari UI (dikirim
sebagai metadata task) harus diteruskan ke Runtime sehingga task benar-benar
memakai provider DAN model yang dipilih, bukan selalu default provider.

Deterministik, TANPA model/API cloud nyata. Memakai provider fake (in-process)
yang mencatat provider+model yang benar-benar dipakai saat generate().

Fixture project dibuat di J:\\Agent_Ai\\dummy_test\\model_selection_fixture dan
dibersihkan setelah test.

Menguji:
    1. GET /api/config mengembalikan daftar provider instance + model (SQLite).
    2. POST /api/tasks dengan metadata.provider/model diteruskan ke runtime.
    3. Runtime memakai provider yang dipilih (bukan default).
    4. Runtime memakai model yang dipilih (GenerateOptions.model).
    5. Event provider_request/provider_response mencatat provider+model terpilih.
    6. Task berikutnya memakai pilihan terbaru (tanpa restart).
    7. Tanpa metadata & tanpa provider instance -> task FAILED (tidak ada
       default provider dari .env; provider aktif = Provider Instance + Model).

Jalankan:
    python scripts/check_model_selection.py
"""

from __future__ import annotations

import json
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
FIXTURE = DUMMY_ROOT / "model_selection_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _make_recording_provider(provider_name: str):
    """Provider fake yang mencatat provider+model yang dipakai saat generate().

    Mengembalikan (provider_instance, record) di mana record menyimpan
    provider_name dan model yang benar-benar diterima dari runtime.
    """
    from agent_ai.core.response import FinishReason, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    record = {"provider": provider_name, "model": None, "calls": 0}

    class RecordingProvider(BaseProvider):
        name = provider_name

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            record["calls"] += 1
            # Model yang benar-benar dipakai runtime (dari GenerateOptions).
            record["model"] = getattr(options, "model", None) if options else None
            return GenerateResult(text="selesai", provider=provider_name, model=record["model"] or "")

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            return LLMResponse(
                text="selesai",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider=provider_name,
                model=record["model"] or "",
            )

    return RecordingProvider(), record


def _wait_for_terminal(service, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["status"] in ("completed", "failed"):
            return rec
        time.sleep(0.02)
    return service.get_task(task_id)


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django

    django.setup()

    from django.test import Client

    import api.services as services_mod
    from api.execution import TaskExecutor

    client = Client()

    # 1) GET /api/config mengembalikan daftar provider instance + model (SQLite).
    resp = client.get("/api/config")
    assert resp.status_code == 200, resp.status_code
    cfg = resp.json()
    assert "providers" in cfg and cfg["providers"], cfg
    # Sumber tunggal provider aktif = Provider Instance + Model (SQLite).
    assert "provider_instances" in cfg, cfg
    for inst in cfg["provider_instances"]:
        assert inst.get("id") and inst.get("provider_type"), inst
        for m in inst.get("models") or []:
            assert m.get("id") and m.get("model_name"), m
    print(f"[1] GET /api/config OK -> providers={cfg['providers']}")
    print(f"    provider_instances={len(cfg['provider_instances'])} instance(s)")

    # Untuk menguji provider+model yang benar-benar dipakai, kita monkeypatch
    # get_provider agar mengembalikan provider recording sesuai nama.
    # Catatan: `agent_ai.providers.registry` di-shadow oleh atribut `registry`
    # (objek) di package, jadi ambil modul sebenarnya via sys.modules.
    import sys as _sys

    registry_mod = _sys.modules["agent_ai.providers.registry"]

    original_get_provider = registry_mod.get_provider
    records = {}

    def fake_get_provider(name):
        # Nama provider WAJIB eksplisit (tidak ada default dari .env).
        # Meniru registry.get_provider: nama kosong -> ValueError.
        if not name or not str(name).strip():
            raise ValueError(
                "Nama provider wajib diisi. Provider aktif ditentukan oleh "
                "Provider Instance + Model (SQLite), bukan .env."
            )
        provider, record = _make_recording_provider(name)
        records[name] = record
        return provider

    registry_mod.get_provider = fake_get_provider

    store = services_mod.InMemorySessionStore()
    executor_bridge = TaskExecutor(store)
    service = services_mod.GatewayService(
        session_store=store,
        task_executor=executor_bridge,
        auto_execute=True,
    )
    services_mod._default_service = service

    project = service.create_project(name="ModelSelFixture", path=str(FIXTURE))
    project_id = project["id"]

    try:
        # 2) POST /api/tasks dengan metadata.provider/model diteruskan ke runtime.
        resp = client.post(
            "/api/tasks",
            data=json.dumps(
                {
                    "task": "buat file index.html sederhana",
                    "project_id": project_id,
                    "metadata": {"provider": "deepseek", "model": "deepseek-chat"},
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201, (resp.status_code, resp.content)
        task_id = resp.json()["task_id"]
        final = _wait_for_terminal(service, task_id)
        assert final["status"] == "completed", final

        # 3) Runtime memakai provider yang dipilih (bukan default).
        assert "deepseek" in records, f"provider deepseek harus dibangun: {records}"
        assert records["deepseek"]["calls"] >= 1, records
        print("[3] runtime memakai provider terpilih OK -> provider=deepseek")

        # 4) Runtime memakai model yang dipilih (GenerateOptions.model).
        assert records["deepseek"]["model"] == "deepseek-chat", records["deepseek"]
        print(f"[4] runtime memakai model terpilih OK -> model={records['deepseek']['model']}")

        # 5) Event provider_request/provider_response mencatat provider+model.
        events = service.sessions.get_events(task_id=task_id)
        req_events = [e for e in events if e.event_type.value == "provider_request"]
        assert req_events, [e.event_type.value for e in events]
        payload = req_events[0].payload
        assert payload.get("provider") == "deepseek", payload
        assert payload.get("model") == "deepseek-chat", payload
        print(f"[5] event provider_request mencatat provider+model OK -> {payload}")

        # 6) Task berikutnya memakai pilihan terbaru (tanpa restart).
        resp = client.post(
            "/api/tasks",
            data=json.dumps(
                {
                    "task": "task kedua",
                    "project_id": project_id,
                    "metadata": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.status_code
        task2_id = resp.json()["task_id"]
        final2 = _wait_for_terminal(service, task2_id)
        assert final2["status"] == "completed", final2
        assert "ollama" in records, records
        assert records["ollama"]["model"] == "qwen2.5-coder:7b", records["ollama"]
        print(
            "[6] task berikutnya memakai pilihan terbaru OK -> "
            f"provider=ollama, model={records['ollama']['model']}"
        )

        # 7) Tanpa metadata & tanpa provider instance -> task FAILED.
        #    Tidak ada lagi default provider dari .env: provider aktif berasal
        #    dari Provider Instance + Model (SQLite).
        records.clear()
        resp = client.post(
            "/api/tasks",
            data=json.dumps({"task": "task tanpa metadata", "project_id": project_id}),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.status_code
        task3_id = resp.json()["task_id"]
        final3 = _wait_for_terminal(service, task3_id)
        assert final3["status"] == "failed", final3
        assert not records, f"tidak boleh ada provider dibangun tanpa pilihan: {records}"
        print("[7] tanpa metadata -> task FAILED (tanpa default provider dari .env) OK")
    finally:
        registry_mod.get_provider = original_get_provider

    print()
    print("[OK] Model selection diteruskan UI -> API -> runtime (provider+model benar-benar berubah).")
    return 0


def main() -> int:
    print("=== Verifikasi Model Selection (UI -> API -> Runtime provider/model) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
