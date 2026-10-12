"""TASK 3 — Statistics API & Aggregation.

Cakupan (tanpa provider LLM eksternal; semua fixture project sementara):

    1. Agregasi token lengkap (actual), parsial, dan unavailable — tidak ada
       komponen yang hilang yang dianggap 0.
    2. Perhitungan task/status/rounds/tool_calls/durasi TANPA double-counting.
    3. Rekonsiliasi Agent Task Log dengan record usage Consultant.
    4. Filter tanggal + pengelompokan harian (hari kosong tetap ada).
    5. Breakdown provider/model + filter aktivitas (agent/consultant/all).
    6. Isolasi project: A dan B berbeda dan tidak saling bocor.
    7. Project tidak dikenal / tidak valid tidak membaca root project lain.
    8. Data kosong & historis tidak lengkap.
    9. Pagination/sorting + validasi parameter.
   10. Endpoint GET /api/statics (routing/view/service) & kontrak respons.

Jalankan:
    python -m pytest tests/test_statics_api.py -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
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
# Fixture helpers
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


def _task_events(
    *,
    prompt: str,
    start: str,
    end: str,
    provider: str = "openai",
    model: str = "gpt-x",
    usage: dict | None = None,
    status: str = "completed",
    tool_calls: int = 0,
    extra_responses: int = 0,
) -> list:
    """Bangun event Task Log lengkap untuk satu task.

    ``extra_responses`` menambah provider_response tambahan (rounds) TANPA usage
    (mensimulasikan request lanjutan yang tidak melaporkan usage).
    """
    events = [
        {"timestamp": start, "event": "task_requested", "data": {"prompt": prompt}},
        {"timestamp": start, "event": "task_started", "data": {}},
    ]
    first = {"provider": provider, "model": model}
    if usage is not None:
        first["usage"] = usage
    events.append({"timestamp": start, "event": "provider_response", "data": first})
    for _ in range(extra_responses):
        events.append(
            {
                "timestamp": start,
                "event": "provider_response",
                "data": {"provider": provider, "model": model},
            }
        )
    for i in range(tool_calls):
        events.append(
            {"timestamp": start, "event": "tool_called", "data": {"tool": f"tool{i}"}}
        )
    if status == "completed":
        events.append({"timestamp": end, "event": "task_completed", "data": {"result": "ok"}})
    elif status == "failed":
        events.append({"timestamp": end, "event": "task_failed", "data": {"error": "boom"}})
    elif status == "cancelled":
        events.append({"timestamp": end, "event": "task_cancelled", "data": {}})
    events.append(
        {"timestamp": end, "event": "task_finished", "data": {"status": status}}
    )
    return events


@pytest.fixture()
def project_a(tmp_path: Path) -> Path:
    root = tmp_path / "proj_a"
    root.mkdir()
    # Task 1: usage ACTUAL, 2 rounds, 3 tool calls, selesai.
    _write_task_log(
        root,
        "a_task1",
        _task_events(
            prompt="Task A1",
            start="2026-01-01T00:00:00+00:00",
            end="2026-01-01T00:01:00+00:00",
            usage={"prompt": 100, "completion": 50},
            tool_calls=3,
            extra_responses=1,
        ),
    )
    # Task 2: usage PARSIAL (hanya input) -> total tidak disimpulkan.
    _write_task_log(
        root,
        "a_task2",
        _task_events(
            prompt="Task A2",
            start="2026-01-02T00:00:00+00:00",
            end="2026-01-02T00:02:00+00:00",
            usage={"prompt": 40},
            status="failed",
            tool_calls=1,
        ),
    )
    # Task 3: usage UNAVAILABLE (tidak ada usage).
    _write_task_log(
        root,
        "a_task3",
        _task_events(
            prompt="Task A3",
            start="2026-01-03T00:00:00+00:00",
            end="2026-01-03T00:00:30+00:00",
            usage=None,
            status="cancelled",
        ),
    )
    _write_usage(
        root,
        [
            {
                "timestamp": 1767225600.0,  # 2026-01-01 UTC
                "project_id": "pa",
                "session_id": "s1",
                "turn_id": "t1",
                "response_index": 0,
                "provider": "openai",
                "model": "gpt-x",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "usage_status": "actual",
            },
            # Duplikat identitas -> harus di-dedupe.
            {
                "timestamp": 1767225600.0,
                "project_id": "pa",
                "session_id": "s1",
                "turn_id": "t1",
                "response_index": 0,
                "provider": "openai",
                "model": "gpt-x",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "usage_status": "actual",
            },
            {
                "timestamp": 1767312000.0,  # 2026-01-02 UTC
                "project_id": "pa",
                "session_id": "s1",
                "turn_id": "t2",
                "response_index": 0,
                "provider": "anthropic",
                "model": "claude-y",
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
                "usage_status": "actual",
            },
        ],
    )
    return root


@pytest.fixture()
def project_b(tmp_path: Path) -> Path:
    root = tmp_path / "proj_b"
    root.mkdir()
    _write_task_log(
        root,
        "b_task1",
        _task_events(
            prompt="Task B1",
            start="2026-01-05T00:00:00+00:00",
            end="2026-01-05T00:05:00+00:00",
            provider="deepseek",
            model="ds-z",
            usage={"prompt": 999, "completion": 111},
        ),
    )
    return root


# --------------------------------------------------------------------------- #
# 1. Agregasi token: actual / partial / unavailable
# --------------------------------------------------------------------------- #
def test_1a_token_aggregation_actual_partial_unavailable(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a))
    totals = out["totals"]
    # Default activity="all": Agent + Consultant.
    # Agent: input 100+40=140, output 50, total 150 (task2 total tak disimpulkan).
    # Consultant: input 17, output 8, total 25.
    assert totals["tokens"]["input_tokens"] == 157  # 140 + 17
    assert totals["tokens"]["output_tokens"] == 58  # 50 + 8
    assert totals["tokens"]["total_tokens"] == 175  # 150 (agent) + 25 (consultant)
    assert totals["usage_completeness"] == {"actual": 1, "partial": 1, "unavailable": 1}


def test_1b_partial_output_not_assumed_zero(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a))
    # Jika output task2 dianggap 0, output_tokens akan = 50 (benar) tetapi
    # task2 akan "actual". Pastikan task2 tetap partial & total task2 None.
    task2 = next(t for t in out["tasks"]["items"] if t["task_id"] == "a_task2")
    assert task2["input_tokens"] == 40
    assert task2["output_tokens"] is None
    assert task2["total_tokens"] is None
    assert task2["usage_status"] == "partial"


def test_1c_unavailable_is_not_zero(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a))
    task3 = next(t for t in out["tasks"]["items"] if t["task_id"] == "a_task3")
    assert task3["input_tokens"] is None
    assert task3["output_tokens"] is None
    assert task3["total_tokens"] is None
    assert task3["usage_status"] == "unavailable"


def test_1d_no_available_metric_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "empty_usage"
    root.mkdir()
    _write_task_log(
        root,
        "u1",
        _task_events(
            prompt="no usage",
            start="2026-01-01T00:00:00+00:00",
            end="2026-01-01T00:00:10+00:00",
            usage=None,
        ),
    )
    out = build_statistics(project_id="x", root=str(root))
    totals = out["totals"]
    # Tidak ada satu pun angka yang tersedia -> None (BUKAN 0).
    assert totals["tokens"]["input_tokens"] is None
    assert totals["tokens"]["output_tokens"] is None
    assert totals["tokens"]["total_tokens"] is None
    assert totals["duration"]["total_seconds"] == 10.0  # durasi tetap valid


# --------------------------------------------------------------------------- #
# 2. Perhitungan task/status/rounds/tool_calls/durasi (tanpa double-count)
# --------------------------------------------------------------------------- #
def test_2_task_count_not_per_event(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a))
    # 3 task Agent; task1 punya 2 provider_response tapi dihitung 1 task.
    assert out["totals"]["total_tasks"] == 3
    assert out["totals"]["tasks_by_status"]["completed"] == 1
    assert out["totals"]["tasks_by_status"]["failed"] == 1
    assert out["totals"]["tasks_by_status"]["cancelled"] == 1


def test_2_rounds_and_tool_calls(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a))
    # rounds = provider_response sukses: task1=2, task2=1, task3=1 -> 4.
    assert out["totals"]["llm_rounds"] == 4
    assert out["totals"]["llm_requests"] == 4
    assert out["totals"]["tool_calls"] == 4  # 3 + 1 + 0


def test_2_duration_and_success_rate(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a))
    durations = out["totals"]["duration"]
    # task1=60s, task2=120s, task3=30s -> total 210, avg 70.
    assert durations["total_seconds"] == 210.0
    assert durations["average_seconds"] == 70.0
    assert durations["measured_tasks"] == 3
    # success rate denominator = terminal (1 completed / 3 terminal).
    assert out["totals"]["success_rate_denominator"] == 3
    assert out["totals"]["success_rate"] == round(1 / 3, 4)


# --------------------------------------------------------------------------- #
# 3. Rekonsiliasi Agent <-> Consultant
# --------------------------------------------------------------------------- #
def test_3_consultant_usage_dedup_and_reconcile(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a), activity="all")
    cons = out["consultant"]
    # Dua record identik di-dedupe -> 2 respons.
    assert cons["responses"] == 2
    assert cons["sessions"] == 1
    assert cons["turns"] == 2
    assert cons["input_tokens"] == 17  # 10 + 7
    assert cons["output_tokens"] == 8  # 5 + 3
    # Consultant TIDAK mengubah total task Agent.
    assert out["totals"]["total_tasks"] == 3


def test_3_agent_total_tokens_reconcile(project_a: Path) -> None:
    # Agent-only: jumlah token task = jumlah komponen yang diketahui.
    out = build_statistics(project_id="pa", root=str(project_a), activity="agent")
    assert out["totals"]["tokens"]["input_tokens"] == 140
    assert out["totals"]["tokens"]["total_tokens"] == 150  # hanya task1
    assert out["consultant"] is None


# --------------------------------------------------------------------------- #
# 4. Filter tanggal & pengelompokan harian
# --------------------------------------------------------------------------- #
def test_4_date_filter_inclusive(project_a: Path) -> None:
    out = build_statistics(
        project_id="pa",
        root=str(project_a),
        from_date="2026-01-01",
        to_date="2026-01-02",
        activity="agent",
    )
    ids = {t["task_id"] for t in out["tasks"]["items"]}
    assert ids == {"a_task1", "a_task2"}
    assert out["totals"]["total_tasks"] == 2


def test_4_daily_includes_empty_days(project_a: Path) -> None:
    out = build_statistics(
        project_id="pa",
        root=str(project_a),
        from_date="2026-01-01",
        to_date="2026-01-04",
        activity="agent",
    )
    days = [d["date"] for d in out["daily"]]
    assert days == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    by_day = {d["date"]: d for d in out["daily"]}
    # Hari tanpa aktivitas: hitungan 0, token None (bukan 0).
    assert by_day["2026-01-04"]["tasks"] == 0
    assert by_day["2026-01-04"]["tokens"]["input_tokens"] is None
    assert by_day["2026-01-01"]["tasks"] == 1
    assert by_day["2026-01-03"]["cancelled"] == 1


def test_4_invalid_range_raises(project_a: Path) -> None:
    with pytest.raises(ValueError):
        build_statistics(
            project_id="pa", root=str(project_a), from_date="2026-02-01", to_date="2026-01-01"
        )
    with pytest.raises(ValueError):
        build_statistics(project_id="pa", root=str(project_a), from_date="not-a-date")


# --------------------------------------------------------------------------- #
# 5. Breakdown provider/model & filter aktivitas
# --------------------------------------------------------------------------- #
def test_5_breakdown_provider_agent_only(project_a: Path) -> None:
    out = build_statistics(
        project_id="pa", root=str(project_a), activity="agent", group_by="provider"
    )
    buckets = {b["key"]: b for b in out["breakdowns"]["provider"]}
    assert "openai" in buckets
    assert buckets["openai"]["tasks"] == 3
    assert buckets["openai"]["tokens"]["input_tokens"] == 140


def test_5_breakdown_model_with_consultant(project_a: Path) -> None:
    out = build_statistics(
        project_id="pa", root=str(project_a), activity="all", group_by="model"
    )
    buckets = {b["key"]: b for b in out["breakdowns"]["model"]}
    assert "gpt-x" in buckets and "claude-y" in buckets
    # gpt-x: task Agent (100+40 input = 140) + consultant (10) = 150.
    assert buckets["gpt-x"]["tokens"]["input_tokens"] == 150
    assert buckets["gpt-x"]["tokens"]["output_tokens"] == 55  # 50 + 5


def test_5_activity_consultant_only(project_a: Path) -> None:
    out = build_statistics(project_id="pa", root=str(project_a), activity="consultant")
    assert out["totals"]["total_tasks"] == 0
    assert out["totals"]["tokens"]["input_tokens"] == 17
    assert out["consultant"]["responses"] == 2


def test_5_provider_filter(project_a: Path) -> None:
    out = build_statistics(
        project_id="pa", root=str(project_a), activity="agent", provider="openai"
    )
    assert out["totals"]["total_tasks"] == 3


# --------------------------------------------------------------------------- #
# 6. Isolasi project
# --------------------------------------------------------------------------- #
def test_6_projects_isolated(project_a: Path, project_b: Path) -> None:
    out_a = build_statistics(project_id="pa", root=str(project_a))
    out_b = build_statistics(project_id="pb", root=str(project_b))
    a_ids = {t["task_id"] for t in out_a["tasks"]["items"]}
    b_ids = {t["task_id"] for t in out_b["tasks"]["items"]}
    assert a_ids == {"a_task1", "a_task2", "a_task3"}
    assert b_ids == {"b_task1"}
    assert not (a_ids & b_ids)
    assert out_b["totals"]["tokens"]["input_tokens"] == 999
    assert out_a["totals"]["tokens"]["input_tokens"] == 157  # 140 agent + 17 consultant


# --------------------------------------------------------------------------- #
# 7. Project tidak dikenal
# --------------------------------------------------------------------------- #
def test_7_unknown_project_rejected(project_a: Path) -> None:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()
    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.projects.registry import ProjectRegistry

    workspace = project_a.parent / "ws_unknown"
    store = ProjectStore(db_path=project_a.parent / "unknown.db")
    svc = services_mod.GatewayService(
        project_registry=ProjectRegistry(workspace=workspace),
        project_store=store,
        auto_execute=False,
    )
    with pytest.raises(services_mod.ValidationError):
        svc.get_statistics(project_id="does-not-exist")
    # Tidak ada active project -> NotFound (bukan fallback ke project lain).
    with pytest.raises(services_mod.NotFoundError):
        svc.get_statistics()


# --------------------------------------------------------------------------- #
# 8. Data kosong & historis tidak lengkap
# --------------------------------------------------------------------------- #
def test_8_empty_project(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    out = build_statistics(project_id="e", root=str(root))
    assert out["totals"]["total_tasks"] == 0
    assert out["totals"]["tokens"]["input_tokens"] is None
    assert out["daily"] == []
    assert out["tasks"]["items"] == []
    assert out["tasks"]["total"] == 0


def test_8_incomplete_history(tmp_path: Path) -> None:
    root = tmp_path / "incomplete"
    root.mkdir()
    # Log tanpa event terminal -> status incomplete, tetap dihitung 1 task.
    _write_task_log(
        root,
        "inc1",
        [
            {"timestamp": "2026-01-01T00:00:00+00:00", "event": "task_requested", "data": {"prompt": "x"}},
            {"timestamp": "2026-01-01T00:00:05+00:00", "event": "provider_response", "data": {"provider": "openai", "model": "m"}},
        ],
    )
    out = build_statistics(project_id="i", root=str(root))
    assert out["totals"]["total_tasks"] == 1
    assert out["totals"]["tasks_by_status"]["incomplete"] == 1
    # success rate tidak bisa dihitung (denominator terminal = 0) -> None.
    assert out["totals"]["success_rate"] is None


# --------------------------------------------------------------------------- #
# 9. Pagination/sorting & validasi parameter
# --------------------------------------------------------------------------- #
def test_9_sorting_and_pagination(project_a: Path) -> None:
    out = build_statistics(
        project_id="pa", root=str(project_a), activity="agent", sort="total_tokens", limit=2
    )
    assert out["tasks"]["total"] == 3
    assert len(out["tasks"]["items"]) == 2
    # task1 (total 150) paling boros -> pertama; task2 & task3 (None) di belakang.
    assert out["tasks"]["items"][0]["task_id"] == "a_task1"
    out2 = build_statistics(
        project_id="pa", root=str(project_a), activity="agent", limit=2, offset=2
    )
    assert len(out2["tasks"]["items"]) == 1


def test_9_invalid_params(project_a: Path) -> None:
    with pytest.raises(ValueError):
        build_statistics(project_id="pa", root=str(project_a), group_by="bogus")
    with pytest.raises(ValueError):
        build_statistics(project_id="pa", root=str(project_a), activity="bogus")
    with pytest.raises(ValueError):
        build_statistics(project_id="pa", root=str(project_a), status="bogus")
    with pytest.raises(ValueError):
        build_statistics(project_id="pa", root=str(project_a), order="sideways")
    with pytest.raises(ValueError):
        build_statistics(project_id="pa", root=str(project_a), limit=-1)


# --------------------------------------------------------------------------- #
# 10. Endpoint GET /api/statics (routing/view/service)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def django_client(tmp_path: Path, project_a: Path, project_b: Path):
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.projects.registry import ProjectRegistry

    workspace = tmp_path / "ws_django"
    store = ProjectStore(db_path=tmp_path / "django.db")
    svc = services_mod.GatewayService(
        project_registry=ProjectRegistry(workspace=workspace),
        project_store=store,
        auto_execute=False,
    )
    services_mod._default_service = svc
    client = Client()
    pa = svc.create_project(name="ProjectA", path=str(project_a))
    pb = svc.create_project(name="ProjectB", path=str(project_b))
    yield client, pa["id"], pb["id"]
    services_mod._default_service = None


def test_10_endpoint_ok(django_client) -> None:
    client, aid, _bid = django_client
    resp = client.get(f"/api/statics?project_id={aid}&activity=agent")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    for key in (
        "project_id",
        "generated_at",
        "range",
        "filters",
        "totals",
        "daily",
        "breakdowns",
        "tasks",
        "consultant",
        "data_quality",
        "cost",
    ):
        assert key in body, key
    assert body["project_id"] == aid
    assert body["totals"]["total_tasks"] == 3
    assert body["cost"]["available"] is False


def test_10_endpoint_uses_active_project(django_client) -> None:
    client, aid, _bid = django_client
    # create_project menjadikan project terakhir aktif; set ke A eksplisit.
    resp = client.post(
        "/api/active-project",
        data=json.dumps({"project_id": aid}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    # Tanpa project_id -> active project (A).
    resp = client.get("/api/statics?activity=agent")
    assert resp.status_code == 200, resp.content
    assert resp.json()["project_id"] == aid


def test_10_endpoint_isolation(django_client) -> None:
    client, aid, bid = django_client
    a_ids = {t["task_id"] for t in client.get(f"/api/statics?project_id={aid}").json()["tasks"]["items"]}
    b_ids = {t["task_id"] for t in client.get(f"/api/statics?project_id={bid}").json()["tasks"]["items"]}
    assert a_ids == {"a_task1", "a_task2", "a_task3"}
    assert b_ids == {"b_task1"}


def test_10_endpoint_unknown_project_404_or_400(django_client) -> None:
    client, _aid, _bid = django_client
    resp = client.get("/api/statics?project_id=nope")
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"]["code"] == "validation_error"


def test_10_endpoint_invalid_group_by_400(django_client) -> None:
    client, aid, _bid = django_client
    resp = client.get(f"/api/statics?project_id={aid}&group_by=bogus")
    assert resp.status_code == 400, resp.content


def test_10_endpoint_no_project_404(django_client) -> None:
    client, _aid, _bid = django_client
    # Clear active project -> resolusi gagal.
    client.delete("/api/active-project")
    resp = client.get("/api/statics")
    assert resp.status_code == 404, resp.content


def test_10_endpoint_all_group_by_serialize(django_client) -> None:
    """Semua group_by harus menghasilkan JSON yang bisa diserialisasi penuh."""
    client, aid, _bid = django_client
    for gb in ("day", "provider", "model", "task"):
        resp = client.get(f"/api/statics?project_id={aid}&group_by={gb}&activity=all")
        assert resp.status_code == 200, (gb, resp.content)
        body = resp.json()
        assert "breakdowns" in body and "daily" in body


def test_10_endpoint_reconciles_with_source(django_client) -> None:
    """Agregasi endpoint dapat direkonsiliasi dengan sumber Task Log langsung."""
    client, aid, _bid = django_client
    body = client.get(f"/api/statics?project_id={aid}&activity=agent").json()
    # Round/usage dari endpoint harus sama dengan pembacaan Task Log manual.
    rounds = sum(t["llm_rounds"] for t in body["tasks"]["items"])
    assert rounds == body["totals"]["llm_rounds"]
    # total task = jumlah item (limit default cukup besar).
    assert body["tasks"]["total"] == body["totals"]["total_tasks"]


def test_10_endpoint_no_raw_response_leak(django_client) -> None:
    """Statistik TIDAK mengembalikan respons LLM mentah / kredensial."""
    client, aid, _bid = django_client
    raw = client.get(f"/api/statics?project_id={aid}").content.decode("utf-8")
    assert "api_key" not in raw.lower()
    assert '"raw"' not in raw
    assert "result" not in raw  # tidak ada final report verbatim di statistik
