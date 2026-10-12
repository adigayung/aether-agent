"""TASK 7 — Integration verification Statics (TASK 1–6).

Cakupan (tanpa provider LLM eksternal; semua fixture project sementara):

    A. Ekspor CSV/JSON lewat endpoint `GET /api/statics/export`:
       filter sama dengan dashboard, SELURUH record (tanpa batas pagination),
       escaping CSV, nilai unavailable tidak jadi 0, metadata lengkap, dan
       TIDAK ada prompt/raw response/API key.
    B. Isolasi project pada ekspor (dua project, data berbeda).
    C. Alur end-to-end: event Task Log + usage Consultant -> Statistics API
       -> dashboard payload -> ekspor.
    D. Blok TASK 5/6 (efficiency/reliability/tool_usage/attention/trends/
       anomalies) konsisten dengan sumber event.
    E. `provider_response_truncated` TIDAK dianggap pemulihan sukses otomatis
       (dibedakan dari event recovery yang benar-benar terjadi).

Jalankan:
    python -m pytest tests/test_statics_integration.py -v
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from agent_ai.statics import build_statistics  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixture helpers (project sementara — TIDAK menyentuh data pengguna)
# --------------------------------------------------------------------------- #
def _write_task_log(root: Path, task_id: str, events: list) -> None:
    log_dir = root / ".aether" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{task_id}.log").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _write_usage(root: Path, records: list) -> None:
    cdir = root / ".aether" / "consultant"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "usage.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _rich_task_events(
    *,
    prompt: str,
    start: str,
    end: str,
    provider: str = "openai",
    model: str = "gpt-x",
    usage: dict | None = None,
    status: str = "completed",
    tools: list | None = None,
    retries: int = 0,
    retry_succeeded: int = 0,
    retry_exhausted: int = 0,
    fallback: tuple[str, str] | None = None,
    truncated: int = 0,
    repeats: int = 0,
    complete_usage: bool = False,
) -> list:
    """Bangun Task Log event realistis: request/response/retry/tool/truncation.

    ``complete_usage`` menambahkan event `provider_request` (semantik TASK 2/3.1)
    supaya `llm_requests` dibaca dari event request, bukan dari jumlah respons.
    """
    events = [
        {"timestamp": start, "event": "task_requested", "data": {"prompt": prompt}},
        {"timestamp": start, "event": "task_started", "data": {}},
    ]
    if complete_usage:
        events.append({"timestamp": start, "event": "provider_request", "data": {}})
    first = {"provider": provider, "model": model}
    if usage is not None:
        first["usage"] = usage
    events.append({"timestamp": start, "event": "provider_response", "data": first})
    for i in range(retries):
        events.append({"timestamp": start, "event": "provider_retry", "data": {"attempt": i + 1}})
    for _ in range(retry_succeeded):
        events.append({"timestamp": start, "event": "provider_retry_succeeded", "data": {}})
    for _ in range(retry_exhausted):
        events.append({"timestamp": start, "event": "provider_retry_exhausted", "data": {}})
    if fallback is not None:
        events.append(
            {
                "timestamp": start,
                "event": "provider_fallback",
                "data": {"from_provider": fallback[0], "to_provider": fallback[1]},
            }
        )
    for _ in range(truncated):
        events.append(
            {"timestamp": start, "event": "provider_response_truncated", "data": {}}
        )
    for i, (tool_name, ok) in enumerate(tools or []):
        events.append(
            {"timestamp": start, "event": "tool_called", "data": {"tool": tool_name}}
        )
        events.append(
            {
                "timestamp": start,
                "event": "tool_completed",
                "data": {"tool": tool_name, "success": ok},
            }
        )
    for _ in range(repeats):
        events.append(
            {"timestamp": start, "event": "retrieval_repeat", "data": {"tool": "read_file"}}
        )
    if status == "completed":
        events.append({"timestamp": end, "event": "task_completed", "data": {"result": "ok"}})
    elif status == "failed":
        events.append({"timestamp": end, "event": "task_failed", "data": {"error": "boom"}})
    elif status == "cancelled":
        events.append({"timestamp": end, "event": "task_cancelled", "data": {}})
    events.append({"timestamp": end, "event": "task_finished", "data": {"status": status}})
    return events


@pytest.fixture()
def django_service(tmp_path: Path):
    """Gateway + Django client dengan dua project uji berisi data BERBEDA."""
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.projects.registry import ProjectRegistry

    root_a = tmp_path / "pa"
    root_b = tmp_path / "pb"
    root_a.mkdir()
    root_b.mkdir()

    # Project A: 3 task, usage actual/partial/unavailable, retry, fallback, tool.
    _write_task_log(
        root_a,
        "a1",
        _rich_task_events(
            prompt="Task A1",
            start="2026-01-10T00:00:00+00:00",
            end="2026-01-10T00:01:00+00:00",
            usage={"prompt": 100, "completion": 50},
            tools=[("read_file", True), ("edit_file", True)],
            complete_usage=True,
        ),
    )
    _write_task_log(
        root_a,
        "a2",
        _rich_task_events(
            prompt="Task A2",
            start="2026-01-11T00:00:00+00:00",
            end="2026-01-11T00:02:00+00:00",
            usage={"prompt": 40},
            status="failed",
            tools=[("run_command", False)],
            retries=1,
            retry_exhausted=1,
            fallback=("openai", "anthropic"),
        ),
    )
    _write_task_log(
        root_a,
        "a3",
        _rich_task_events(
            prompt="Task A3",
            start="2026-01-12T00:00:00+00:00",
            end="2026-01-12T00:00:30+00:00",
            usage=None,
            status="cancelled",
            truncated=1,
            repeats=2,
        ),
    )
    _write_usage(
        root_a,
        [
            {
                "timestamp": 1768003200.0,  # 2026-01-10 UTC
                # project_id ditulis belakangan (setelah id project diketahui)
                # agar identitas telemetry sama dengan id project nyata.
                "session_id": "s1",
                "turn_id": "t1",
                "response_index": 0,
                "provider": "openai",
                "model": "gpt-x",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "usage_status": "actual",
            }
        ],
    )

    # Project B: 1 task dengan angka yang JELAS berbeda.
    _write_task_log(
        root_b,
        "b1",
        _rich_task_events(
            prompt="Task B1 (rahasia)",
            start="2026-01-20T00:00:00+00:00",
            end="2026-01-20T00:05:00+00:00",
            provider="deepseek",
            model="ds-z",
            usage={"prompt": 777, "completion": 333},
        ),
    )

    workspace = tmp_path / "ws"
    store = ProjectStore(db_path=tmp_path / "test.db")
    svc = services_mod.GatewayService(
        project_registry=ProjectRegistry(workspace=workspace),
        project_store=store,
        auto_execute=False,
    )
    services_mod._default_service = svc
    client = Client()
    pa = svc.create_project(name="ProjectA", path=str(root_a))
    pb = svc.create_project(name="ProjectB", path=str(root_b))
    # Tulis ulang usage Consultant project A dengan project_id NYATA, agar
    # isolasi project diuji apa adanya (bukan id fiktif).
    _write_usage(
        root_a,
        [
            {
                "timestamp": 1768003200.0,  # 2026-01-10 UTC
                "project_id": pa["id"],
                "session_id": "s1",
                "turn_id": "t1",
                "response_index": 0,
                "provider": "openai",
                "model": "gpt-x",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "usage_status": "actual",
            }
        ],
    )
    yield client, pa["id"], pb["id"]
    services_mod._default_service = None


# --------------------------------------------------------------------------- #
# A. Ekspor CSV/JSON lewat endpoint
# --------------------------------------------------------------------------- #
def test_export_json_metadata_and_completeness(django_service) -> None:
    client, aid, _bid = django_service
    resp = client.get(f"/api/statics/export?project_id={aid}&format=json&activity=agent")
    assert resp.status_code == 200, resp.content
    assert "attachment" in resp["Content-Disposition"]
    assert f"aether-statics-{aid}.json" in resp["Content-Disposition"]
    assert resp["Cache-Control"] == "no-store"
    payload = json.loads(resp.content.decode("utf-8"))

    meta = payload["export_metadata"]
    assert meta["project_id"] == aid
    assert meta["filters"]["activity"] == "agent"
    assert meta["generated_at"]
    assert meta["task_rows"] == 3
    assert "usage_completeness_legend" in meta
    # Nilai unavailable tidak dikonversi menjadi nol.
    a3 = next(t for t in payload["tasks"] if t["task_id"] == "a3")
    assert a3["input_tokens"] is None and a3["total_tokens"] is None
    assert a3["usage_status"] == "unavailable"
    # Tidak ada prompt/raw/API key.
    raw = resp.content.decode("utf-8").lower()
    assert "rahasia" not in raw
    assert "api_key" not in raw
    assert '"raw"' not in raw
    assert "prompt" not in payload["tasks"][0]


def test_export_csv_header_escaping_and_full_records(django_service) -> None:
    client, aid, _bid = django_service
    resp = client.get(f"/api/statics/export?project_id={aid}&format=csv&activity=agent")
    assert resp.status_code == 200, resp.content
    assert "text/csv" in resp["Content-Type"]
    body = resp.content.decode("utf-8")
    # Baris task = jumlah task, bukan hanya halaman pagination default (50).
    assert body.count("a1") >= 1 and body.count("a2") >= 1 and body.count("a3") >= 1
    assert "# section = tasks" in body
    assert "# section = metadata" in body

    # CSV harus bisa di-parse: bagian task punya 3 baris data + 1 header.
    task_rows = _parse_csv_section(body, "tasks")
    headers = task_rows[0]
    data_rows = task_rows[1:]
    assert len(data_rows) == 3, task_rows
    assert headers[0] == "task_id"
    ids = {r[headers.index("task_id")] for r in data_rows}
    assert ids == {"a1", "a2", "a3"}
    # unavailable tetap kosong (bukan "0").
    a3_row = next(r for r in data_rows if r[headers.index("task_id")] == "a3")
    assert a3_row[headers.index("total_tokens")] == ""


def _parse_csv_section(body: str, section: str) -> list:
    """Ambil baris CSV pada satu `# section = <section>` (baris kosong pemisah)."""
    lines = body.split("\r\n")
    try:
        start = lines.index(f"# section = {section}") + 1
    except ValueError:
        return []
    block = []
    for line in lines[start:]:
        if line.startswith("# section ="):
            break
        if line == "":
            break
        block.append(line)
    return list(csv.reader(io.StringIO("\n".join(block))))


def test_export_csv_escaping_special_characters() -> None:
    from agent_ai.statics.analytics import TASK_EXPORT_COLUMNS, build_csv_export
    from agent_ai.statics.records import AgentTaskStats

    task = AgentTaskStats(
        task_id='weird,"id"\nline',
        project_id="p",
        prompt="PROMPT HARUS DIKECUALIKAN",
        status="completed",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:10+00:00",
        duration_seconds=10.0,
        providers=["modèle-ñ"],
        models=[],
        llm_requests=1,
        llm_rounds=1,
        input_tokens=1,
        output_tokens=None,
        total_tokens=None,
        usage_status="partial",
    )
    csv_text = build_csv_export(tasks=[task], metadata={"project_id": "p"})
    assert "PROMPT HARUS DIKECUALIKAN" not in csv_text
    rows = _parse_csv_section(csv_text, "tasks")
    assert rows[0][0] == "task_id"
    data = rows[1]
    assert data[0] == 'weird,"id"\nline'  # escaping benar saat di-parse ulang
    assert data[rows[0].index("providers")] == "modèle-ñ"  # unicode utuh
    assert data[rows[0].index("output_tokens")] == ""


def test_export_filters_match_dashboard(django_service) -> None:
    client, aid, _bid = django_service
    dash = client.get(
        f"/api/statics?project_id={aid}&activity=agent&status=failed"
    ).json()
    assert dash["tasks"]["total"] == 1

    resp = client.get(
        f"/api/statics/export?project_id={aid}&format=json&activity=agent&status=failed"
    )
    payload = json.loads(resp.content.decode("utf-8"))
    assert [t["task_id"] for t in payload["tasks"]] == ["a2"]
    assert payload["export_metadata"]["filters"]["status"] == "failed"

    # Filter provider juga diterapkan konsisten.
    resp2 = client.get(
        f"/api/statics/export?project_id={aid}&format=json&activity=agent&provider=openai"
    )
    payload2 = json.loads(resp2.content.decode("utf-8"))
    assert {t["task_id"] for t in payload2["tasks"]} == {"a1", "a2", "a3"}


def test_export_not_limited_by_pagination(django_service, tmp_path: Path) -> None:
    """Ekspor menyertakan SEMUA record meski > default limit halaman dashboard."""
    client, aid, _bid = django_service
    root = tmp_path / "pa"
    for i in range(60):
        _write_task_log(
            root,
            f"many{i:02d}",
            _rich_task_events(
                prompt=f"t{i}",
                start="2026-02-01T00:00:00+00:00",
                end="2026-02-01T00:00:01+00:00",
                usage={"prompt": 1, "completion": 1},
            ),
        )
    dash = client.get(f"/api/statics?project_id={aid}&activity=agent").json()
    assert len(dash["tasks"]["items"]) == 50  # halaman default
    assert dash["tasks"]["total"] == 63

    resp = client.get(f"/api/statics/export?project_id={aid}&format=json&activity=agent")
    payload = json.loads(resp.content.decode("utf-8"))
    assert len(payload["tasks"]) == 63


def test_export_invalid_format_rejected(django_service) -> None:
    client, aid, _bid = django_service
    resp = client.get(f"/api/statics/export?project_id={aid}&format=xml")
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# B. Isolasi project pada ekspor
# --------------------------------------------------------------------------- #
def test_export_project_isolation(django_service) -> None:
    client, aid, bid = django_service
    a = json.loads(
        client.get(f"/api/statics/export?project_id={aid}&format=json").content.decode()
    )
    b = json.loads(
        client.get(f"/api/statics/export?project_id={bid}&format=json").content.decode()
    )
    a_ids = {t["task_id"] for t in a["tasks"]}
    b_ids = {t["task_id"] for t in b["tasks"]}
    assert a_ids == {"a1", "a2", "a3"}
    assert b_ids == {"b1"}
    assert not (a_ids & b_ids)
    assert a["export_metadata"]["project_id"] == aid
    assert b["export_metadata"]["project_id"] == bid
    # Data project B tidak bocor ke ekspor project A.
    assert "rahasia" not in json.dumps(a).lower()
    assert b["totals"]["tokens"]["input_tokens"] == 777


def test_export_unknown_project_rejected(django_service) -> None:
    client, _aid, _bid = django_service
    resp = client.get("/api/statics/export?project_id=nope&format=json")
    assert resp.status_code == 400, resp.content


# --------------------------------------------------------------------------- #
# C. End-to-end: Task Log + usage -> API -> dashboard/ekspor
# --------------------------------------------------------------------------- #
def test_project_switch_and_restart_do_not_mix_data(tmp_path: Path) -> None:
    """Ganti active project + 'restart' backend tidak mencampur data project."""
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.projects.registry import ProjectRegistry

    root_a = tmp_path / "pa"
    root_b = tmp_path / "pb"
    root_a.mkdir()
    root_b.mkdir()
    _write_task_log(
        root_a,
        "a_only",
        _rich_task_events(
            prompt="A", start="2026-03-01T00:00:00+00:00", end="2026-03-01T00:00:10+00:00",
            usage={"prompt": 11, "completion": 9},
        ),
    )
    _write_task_log(
        root_b,
        "b_only",
        _rich_task_events(
            prompt="B", start="2026-03-02T00:00:00+00:00", end="2026-03-02T00:00:10+00:00",
            usage={"prompt": 101, "completion": 99},
        ),
    )

    db = tmp_path / "persist.db"
    svc = services_mod.GatewayService(
        project_registry=ProjectRegistry(workspace=tmp_path / "ws"),
        project_store=ProjectStore(db_path=db),
        auto_execute=False,
    )
    services_mod._default_service = svc
    client = Client()
    pa = svc.create_project(name="A", path=str(root_a))
    pb = svc.create_project(name="B", path=str(root_b))

    # Ganti active project (B aktif) -> data tanpa project_id = B.
    client.post(
        "/api/active-project",
        data=json.dumps({"project_id": pb["id"]}),
        content_type="application/json",
    )
    assert client.get("/api/statics").json()["project_id"] == pb["id"]
    assert {t["task_id"] for t in client.get("/api/statics").json()["tasks"]["items"]} == {"b_only"}

    # "Restart" backend: service baru + store dari DB yang sama (state persisten).
    svc2 = services_mod.GatewayService(
        project_registry=ProjectRegistry(workspace=tmp_path / "ws"),
        project_store=ProjectStore(db_path=db),
        auto_execute=False,
    )
    services_mod._default_service = svc2
    client2 = Client()
    # Active project tetap B -> data tetap B (tanpa pencampuran).
    body_b = client2.get("/api/statics").json()
    assert body_b["project_id"] == pb["id"]
    assert {t["task_id"] for t in body_b["tasks"]["items"]} == {"b_only"}
    # Pindah ke A setelah restart -> hanya data A.
    client2.post(
        "/api/active-project",
        data=json.dumps({"project_id": pa["id"]}),
        content_type="application/json",
    )
    body_a = client2.get("/api/statics").json()
    assert body_a["project_id"] == pa["id"]
    assert {t["task_id"] for t in body_a["tasks"]["items"]} == {"a_only"}
    assert body_a["totals"]["tokens"]["total_tokens"] == 20

    services_mod._default_service = None


def test_project_without_data_shows_empty_not_other_project(django_service) -> None:
    client, _aid, bid = django_service
    # Project B tidak punya record Consultant: blok Consultant B harus kosong,
    # bukan memakai record Consultant project A.
    body = client.get(f"/api/statics?project_id={bid}&activity=all").json()
    assert body["consultant"]["responses"] == 0
    assert body["consultant"]["total_tokens"] is None
    # Ekspor B juga tidak memuat data A.
    exported = json.loads(
        client.get(f"/api/statics/export?project_id={bid}&format=json").content.decode()
    )
    assert {t["task_id"] for t in exported["tasks"]} == {"b1"}


def test_query_does_not_accept_arbitrary_filesystem_path(django_service, tmp_path: Path) -> None:
    """project_id adalah id store; path filesystem arbitrer harus DITOLAK."""
    client, _aid, _bid = django_service
    # Path absolut tidak akan pernah cocok dengan id project -> error, bukan
    # membaca folder sembarang.
    for bogus in (str(tmp_path), "C:/Windows", "../../etc", ".."):
        resp = client.get(f"/api/statics?project_id={bogus}")
        assert resp.status_code == 400, (bogus, resp.status_code, resp.content)
        assert resp.json()["error"]["code"] == "validation_error"


def test_e2e_telemetry_to_api_and_export(django_service) -> None:
    client, aid, _bid = django_service
    body = client.get(f"/api/statics?project_id={aid}&activity=all").json()

    # Totals Agent + Consultant harus dapat direkonsiliasi dengan sumber.
    assert body["totals"]["total_tasks"] == 3
    # Consultant: 1 record actual 15 token.
    assert body["consultant"]["responses"] == 1
    assert body["consultant"]["total_tokens"] == 15
    # Agent: 100+40 input, 50 output, 150 total (task2 total tidak disimpulkan).
    assert body["totals"]["tokens"]["input_tokens"] == 140 + 10
    assert body["totals"]["tokens"]["output_tokens"] == 50 + 5
    assert body["totals"]["tokens"]["total_tokens"] == 150 + 15

    # Detail per-task konsisten dengan totals.
    items = body["tasks"]["items"]
    assert sum(t["llm_rounds"] for t in items) == body["totals"]["llm_rounds"]
    assert sum(t["tool_calls"] for t in items) == body["totals"]["tool_calls"]
    # Ekspor memakai definisi metrik yang sama.
    exported = json.loads(
        client.get(f"/api/statics/export?project_id={aid}&format=json&activity=all").content.decode()
    )
    assert exported["totals"]["tokens"]["total_tokens"] == body["totals"]["tokens"]["total_tokens"]


# --------------------------------------------------------------------------- #
# D. Blok TASK 5/6 konsisten dengan sumber event
# --------------------------------------------------------------------------- #
def_eff = None


def test_task5_blocks_consistent(django_service) -> None:
    client, aid, _bid = django_service
    body = client.get(f"/api/statics?project_id={aid}&activity=agent").json()

    eff = body["efficiency"]
    # 2 tool sukses + 1 gagal -> denominator 3, rate 1/3.
    assert eff["tool_success"] == 2
    assert eff["tool_failed"] == 1
    assert eff["tool_failure_rate_denominator"] == 3
    assert eff["tool_failure_rate"] == round(1 / 3, 4)
    assert eff["retrieval_repeats"] == 2

    rel = body["reliability"]
    assert rel["provider_retries"] == 1
    assert rel["provider_retry_exhausted"] == 1
    assert rel["provider_fallbacks"] == 1
    assert rel["fallback_transitions"] == [
        {"from_provider": "openai", "to_provider": "anthropic", "count": 1}
    ]
    assert rel["tasks_failed"] == 1
    assert rel["tasks_cancelled"] == 1
    # Gap ditandai eksplisit, bukan nol.
    assert rel["max_steps_aborts"]["available"] is False
    assert rel["max_steps_aborts"]["count"] is None
    assert eff["context_compaction"]["available"] is False

    tools = {r["tool"]: r["calls"] for r in body["tool_usage"]}
    assert tools == {"read_file": 1, "edit_file": 1, "run_command": 1}

    att = body["attention"]
    assert att["available"] is True
    flags = {t["task_id"] for t in att["reliability_flags"]}
    assert "a2" in flags and "a3" in flags


def test_task6_trends_and_anomalies(django_service) -> None:
    client, aid, _bid = django_service
    # Tanpa rentang -> trends tidak tersedia, bukan angka menyesatkan.
    no_range = client.get(f"/api/statics?project_id={aid}&activity=agent").json()
    assert no_range["trends"]["available"] is False
    assert "insufficient" in no_range["anomalies"]

    # Dengan rentang -> previous_range tepat satu periode sebelum, panjang sama.
    rng = client.get(
        f"/api/statics?project_id={aid}&activity=agent&from=2026-01-10&to=2026-01-12"
    ).json()
    tr = rng["trends"]
    assert tr["available"] is True
    assert tr["range"] == {"from": "2026-01-10", "to": "2026-01-12"}
    assert tr["previous_range"] == {"from": "2026-01-07", "to": "2026-01-09"}
    assert tr["metrics"]["total_tasks"]["current"] == 3
    assert tr["metrics"]["total_tasks"]["previous"] == 0
    # Persentase TIDAK dihitung bila basis 0 (tidak misleading).
    assert tr["metrics"]["total_tasks"]["change"]["percent_available"] is False

    # Sampel token 3 task < minimum -> indikator ditandai insufficient.
    kinds = {i["kind"]: i for i in rng["anomalies"]["indicators"]}
    assert kinds["high_token_tasks"]["sufficient"] is False
    assert rng["anomalies"]["sufficient"] is False


# --------------------------------------------------------------------------- #
# E. provider_response_truncated vs pemulihan sesungguhnya
# --------------------------------------------------------------------------- #
def test_truncated_event_is_not_automatic_success(django_service) -> None:
    client, aid, _bid = django_service
    body = client.get(f"/api/statics?project_id={aid}&activity=agent").json()
    # a3: 1 event truncated TAPI task berstatus cancelled (BUKAN success).
    a3 = next(t for t in body["tasks"]["items"] if t["task_id"] == "a3")
    assert a3["status"] == "cancelled"
    # Event truncation dihitung sebagai "terjadi", BUKAN otomatis "pulih".
    assert a3["truncation_events"] == 1
    assert a3["truncation_recoveries"] == 1  # truncated tanpa abort -> benar pulih
    assert body["reliability"]["truncation_events"] == 1
    assert body["reliability"]["truncation_aborts"] == 0
    # Success rate hanya menghitung task terminal completed.
    assert body["totals"]["success_rate_denominator"] == 3
    assert body["totals"]["success_rate"] == round(1 / 3, 4)
    # Truncation event tidak menaikkan jumlah respons sukses.
    assert body["totals"]["llm_failed_responses"] == 0


def test_truncation_abort_is_not_counted_as_recovery(tmp_path: Path) -> None:
    """Truncation yang berujung abort TIDAK boleh dihitung sebagai 'recovery'.

    Regresi: `provider_response_truncated` = truncation TERJADI; abort
    (`loop_truncation_abort`) menandai truncation yang TIDAK pulih.
    """
    root = tmp_path / "trunc"
    root.mkdir()
    _write_task_log(
        root,
        "t_abort",
        [
            {"timestamp": "2026-01-01T00:00:00+00:00", "event": "task_requested", "data": {"prompt": "x"}},
            {"timestamp": "2026-01-01T00:00:01+00:00", "event": "task_started", "data": {}},
            # Tiga kali truncated lalu menyerah (limit tercapai) -> FAILED.
            {"timestamp": "2026-01-01T00:00:02+00:00", "event": "provider_response", "data": {"provider": "openai", "model": "m"}},
            {"timestamp": "2026-01-01T00:00:02+00:00", "event": "provider_response_truncated", "data": {"recovery": 1}},
            {"timestamp": "2026-01-01T00:00:03+00:00", "event": "provider_response_truncated", "data": {"recovery": 2}},
            {"timestamp": "2026-01-01T00:00:04+00:00", "event": "provider_response_truncated", "data": {"recovery": 3}},
            {"timestamp": "2026-01-01T00:00:04+00:00", "event": "loop_truncation_abort", "data": {"recoveries": 3, "limit": 3}},
            {"timestamp": "2026-01-01T00:00:04+00:00", "event": "task_failed", "data": {"error": "truncated"}},
            {"timestamp": "2026-01-01T00:00:04+00:00", "event": "task_finished", "data": {"status": "failed"}},
        ],
    )
    out = build_statistics(project_id="t", root=str(root), activity="agent")
    rel = out["reliability"]
    assert rel["truncation_events"] == 3
    assert rel["truncation_aborts"] == 1
    # 3 truncation events, 1 abort -> hanya 2 yang benar-benar dipulihkan.
    assert rel["truncation_recoveries"] == 2
    # Totals konsisten dengan blok reliability (kontrak API sama).
    assert out["totals"]["truncation_events"] == 3
    assert out["totals"]["truncation_aborts"] == 1
    assert out["totals"]["truncation_recoveries"] == 2
    # Task tetap FAILED; abort tidak mengubah status menjadi sukses.
    assert rel["tasks_failed"] == 1
    assert out["totals"]["success_rate"] == 0.0


def test_truncation_event_alone_is_not_abort(tmp_path: Path) -> None:
    """Truncation yang pulih (tanpa abort) tetap dihitung recovery."""
    root = tmp_path / "trunc_ok"
    root.mkdir()
    _write_task_log(
        root,
        "t_ok",
        [
            {"timestamp": "2026-01-01T00:00:00+00:00", "event": "task_started", "data": {}},
            {"timestamp": "2026-01-01T00:00:01+00:00", "event": "provider_response", "data": {"provider": "openai", "model": "m"}},
            {"timestamp": "2026-01-01T00:00:01+00:00", "event": "provider_response_truncated", "data": {"recovery": 1}},
            {"timestamp": "2026-01-01T00:00:02+00:00", "event": "task_completed", "data": {"result": "ok"}},
            {"timestamp": "2026-01-01T00:00:02+00:00", "event": "task_finished", "data": {"status": "completed"}},
        ],
    )
    out = build_statistics(project_id="t", root=str(root), activity="agent")
    rel = out["reliability"]
    assert rel["truncation_events"] == 1
    assert rel["truncation_aborts"] == 0
    assert rel["truncation_recoveries"] == 1


def test_historical_log_without_new_fields_still_readable(tmp_path: Path) -> None:
    """Log historis tanpa `provider_request`/event baru tetap terbaca (legacy)."""
    root = tmp_path / "legacy"
    root.mkdir()
    _write_task_log(
        root,
        "legacy1",
        [
            {"timestamp": "2026-01-01T00:00:00+00:00", "event": "task_requested", "data": {"prompt": "x"}},
            {"timestamp": "2026-01-01T00:00:01+00:00", "event": "task_started", "data": {}},
            {
                "timestamp": "2026-01-01T00:00:05+00:00",
                "event": "provider_response",
                "data": {"provider": "openai", "model": "m", "usage": {"prompt_tokens": 5, "completion_tokens": 5}},
            },
            {"timestamp": "2026-01-01T00:00:10+00:00", "event": "task_completed", "data": {"result": "ok"}},
            {"timestamp": "2026-01-01T00:00:10+00:00", "event": "task_finished", "data": {"status": "completed"}},
        ],
    )
    out = build_statistics(project_id="legacy", root=str(root), activity="agent")
    assert out["totals"]["total_tasks"] == 1
    # Legacy: llm_requests jatuh ke jumlah respons sukses (tidak double count).
    assert out["totals"]["llm_requests"] == 1
    assert out["totals"]["llm_rounds"] == 1
    assert out["totals"]["tokens"]["total_tokens"] == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
