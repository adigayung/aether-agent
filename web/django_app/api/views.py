"""Views HTTP untuk AETHER Gateway (#50).

Django views biasa (TANPA DRF). Views HANYA:
    - mem-parse & memvalidasi request HTTP,
    - memanggil GatewayService (facade tipis),
    - mengembalikan response JSON.

TIDAK ada logic Agent/Runtime/Planning/Tool di sini.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict

from django.conf import settings
from django.http import HttpRequest, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from api.services import GatewayError, GatewayService, get_service
from api.streaming import EventSubscription, sse_stream


def _json_response(data: Any, status: int = 200) -> JsonResponse:
    """Response JSON standar (tanpa indentasi)."""
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def _error_response(exc: GatewayError) -> JsonResponse:
    """Response error terstruktur dari GatewayError."""
    return _json_response(exc.to_dict(), status=exc.status_code)


def _parse_json_body(request: HttpRequest) -> Dict[str, Any]:
    """Parse body JSON dengan batas ukuran (bounded).

    Raises:
        GatewayError: bila body tidak valid / terlalu besar.
    """
    from api.services import ValidationError

    max_bytes = getattr(settings, "AETHER_GATEWAY_MAX_BODY_BYTES", 1_000_000)
    if len(request.body) > max_bytes:
        raise ValidationError(f"Body request melebihi batas {max_bytes} bytes.")

    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError(f"Body bukan JSON valid: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Body JSON harus berupa object.")
    return data


def _handle(handler: Callable[..., JsonResponse]) -> Callable:
    """Decorator: tangani GatewayError -> response error terstruktur.

    Meneruskan URL kwargs (mis. task_id) ke handler.
    """

    def wrapper(request: HttpRequest, **kwargs: Any) -> JsonResponse:
        service = get_service()
        try:
            return handler(request, service, **kwargs)
        except GatewayError as exc:
            return _error_response(exc)

    wrapper.__name__ = handler.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def health(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/health -> status gateway."""
    return _json_response(service.health())


@require_http_methods(["GET"])
@_handle
def config(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/config -> provider/model/mode dari konfigurasi AETHER.

    Frontend TIDAK meng-hardcode nama model/provider; semua dari AETHER.
    """
    return _json_response(service.get_config())


# ---------------------------------------------------------------------------
# LLM Config (halaman Settings; LLMConfigService AETHER existing)
#
# Gateway HANYA memanggil facade konfigurasi LLM AETHER. Nilai secret (.env)
# TIDAK pernah dikembalikan ke klien: hanya versi masked.
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def llm_config(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/llm/config -> credential, provider type, provider + model."""
    return _json_response(service.get_llm_config())


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def llm_credentials(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/llm/credentials -> set API key .env (body: {name, value})."""
    body = _parse_json_body(request)
    record = service.create_llm_credential(
        name=body.get("name"), value=body.get("value")
    )
    return _json_response(record, status=201)


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def delete_llm_credential(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/llm/credentials/delete -> hapus API key .env (name, force?)."""
    body = _parse_json_body(request)
    return _json_response(
        service.delete_llm_credential(
            body.get("name"), force=bool(body.get("force", False))
        )
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def llm_providers(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/llm/providers -> daftar provider instance + nested model.

    POST /api/llm/providers -> buat provider instance baru.

    GET dipakai alur New Task: dropdown Provider Instance + Model diambil dari
    konfigurasi LLM tersimpan (SQLite), bukan dari settings/.env.
    """
    if request.method == "GET":
        return _json_response({"providers": service.list_llm_providers()})

    body = _parse_json_body(request)
    record = service.create_llm_provider(
        name=body.get("name"),
        provider_type=body.get("provider_type"),
        api_key_env=body.get("api_key_env") or "",
        api_url=body.get("api_url") or "",
        enabled=bool(body.get("enabled", True)),
    )
    return _json_response(record, status=201)


@csrf_exempt
@require_http_methods(["PUT", "DELETE"])
@_handle
def llm_provider_detail(
    request: HttpRequest, service: GatewayService, provider_id: str
) -> JsonResponse:
    """PUT/DELETE /api/llm/providers/<provider_id>."""
    if request.method == "DELETE":
        return _json_response(service.delete_llm_provider(provider_id))

    body = _parse_json_body(request)
    record = service.update_llm_provider(
        provider_id,
        name=body.get("name"),
        provider_type=body.get("provider_type"),
        api_key_env=body.get("api_key_env"),
        api_url=body.get("api_url"),
        enabled=body.get("enabled"),
    )
    return _json_response(record)


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def llm_models(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/llm/models -> tambah model pada provider instance."""
    body = _parse_json_body(request)
    record = service.create_llm_model(
        provider_id=body.get("provider_id"),
        model_name=body.get("model_name"),
        enabled=bool(body.get("enabled", True)),
    )
    return _json_response(record, status=201)


@csrf_exempt
@require_http_methods(["PUT", "DELETE"])
@_handle
def llm_model_detail(
    request: HttpRequest, service: GatewayService, model_id: str
) -> JsonResponse:
    """PUT/DELETE /api/llm/models/<model_id>."""
    if request.method == "DELETE":
        return _json_response(service.delete_llm_model(model_id))

    body = _parse_json_body(request)
    record = service.update_llm_model(
        model_id,
        model_name=body.get("model_name"),
        enabled=body.get("enabled"),
    )
    return _json_response(record)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def projects(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/projects -> daftar project launcher (SQLite store).

    POST /api/projects -> buat project baru (name + path), daftarkan ke
    ProjectRegistry AETHER, simpan record, jadikan active project.
    """
    if request.method == "GET":
        return _json_response({"projects": service.list_launcher_projects()})

    body = _parse_json_body(request)
    record = service.create_project(name=body.get("name"), path=body.get("path"))
    return _json_response(record, status=201)


@csrf_exempt
@require_http_methods(["DELETE"])
@_handle
def delete_project(request: HttpRequest, service: GatewayService, project_id: str) -> JsonResponse:
    """DELETE /api/projects/<project_id> -> hapus RECORD project (bukan file)."""
    return _json_response(service.delete_project(project_id))


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def open_in_explorer(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/open-in-explorer -> buka Windows Explorer pada ACTIVE PROJECT.

    Path TIDAK diterima dari frontend (anti arbitrary path): backend memakai
    active project yang tersimpan. Frontend hanya memicu aksi.
    """
    return _json_response(service.open_active_project_in_explorer())


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def reveal_in_explorer(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/reveal-in-explorer -> buka Windows Explorer highlight file.

    Body: { path: absolute filesystem path }.
    Backend memvalidasi path berada di dalam active project root.
    """
    body = _parse_json_body(request)
    file_path = body.get("path")
    if not file_path:
        from api.services import ValidationError
        raise ValidationError("Field 'path' wajib diisi.")
    return _json_response(service.reveal_file_in_explorer(file_path))


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def delete_entry(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/delete-entry -> hapus file atau folder dari project.

    Body: { path: relative path from project root, type: "file"|"dir" }.
    Backend memvalidasi path berada di dalam active project root.
    """
    body = _parse_json_body(request)
    rel_path = body.get("path")
    entry_type = body.get("type", "file")
    if not rel_path:
        from api.services import ValidationError
        raise ValidationError("Field 'path' wajib diisi.")
    return _json_response(service.delete_project_entry(rel_path, entry_type))


@require_http_methods(["GET"])
@_handle
def files(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/files?path=... -> daftar file project aktif (read-only).

    Memakai ListFilesTool AETHER (bukan abstraksi filesystem baru).
    """
    path = request.GET.get("path") or "."
    return _json_response(service.list_project_files(path))


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
@_handle
def active_project(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """Active project state (single-user local app; bukan login/session user).

    GET    -> active project saat ini (null bila tidak ada).
    POST   -> set active project (body: {project_id}).
    DELETE -> Close Project (clear active project; project tetap tersimpan).
    """
    if request.method == "GET":
        return _json_response({"active_project": service.get_active_project()})
    if request.method == "DELETE":
        return _json_response(service.clear_active_project())

    body = _parse_json_body(request)
    project_id = body.get("project_id")
    if not project_id:
        from api.services import ValidationError

        raise ValidationError("Field 'project_id' wajib diisi.")
    return _json_response({"active_project": service.set_active_project(project_id)})


@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def tasks(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/tasks -> daftar task (history); POST /api/tasks -> buat task.

    GET hanya membaca task yang sudah ada (in-memory, tanpa database).
    POST memvalidasi + menyiapkan task via AETHER (TaskPreparation).
    """
    if request.method == "GET":
        return _json_response({"tasks": service.list_tasks()})

    body = _parse_json_body(request)
    task = body.get("task")
    project_id = body.get("project_id")
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        from api.services import ValidationError

        raise ValidationError("Field 'metadata' harus berupa object bila diisi.")
    record = service.create_task(task=task, project_id=project_id, metadata=metadata)
    return _json_response(record, status=201)


@require_http_methods(["GET"])
@_handle
def get_task(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """GET /api/tasks/<task_id> -> detail task."""
    return _json_response(service.get_task(task_id))


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def cancel_task(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """POST /api/tasks/<task_id>/cancel -> minta penghentian task.

    Menandai task CANCELLED dan MEMICU cooperative cancellation pada eksekusi
    yang sedang berjalan (Agent loop berhenti di safe boundary, bukan
    thread.kill). Bukan stop engine kedua: memakai token cancellation tunggal
    yang dibagikan ke runtime/orchestrator AETHER yang sudah ada.
    """
    return _json_response(service.cancel_task(task_id))


# ---------------------------------------------------------------------------
# Task History API (reads .aether/log/ persistent store)
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def task_history(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/tasks/history -> daftar semua task dari .aether/log/.

    Query params (opsional):
        project_id: filter berdasarkan project (bila ada).

    Mengembalikan daftar task terurut terbaru ke terlama.
    Setiap task berisi: task_id, first_timestamp, last_timestamp, status, task.
    """
    project_id = request.GET.get("project_id") or None
    return _json_response({"tasks": service.list_task_history(project_id=project_id)})


@require_http_methods(["GET"])
@_handle
def task_history_detail(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """GET /api/tasks/history/<task_id> -> ringkasan task dari .aether/log/.

    Mengembalikan info task: task_id, first/last timestamp, status, prompt.
    """
    project_id = request.GET.get("project_id") or None
    return _json_response(service.get_task_history(task_id, project_id=project_id))


# ---------------------------------------------------------------------------
# Activity API (chronological events per task)
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def task_activity(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """GET /api/tasks/<task_id>/activity -> chronological activity satu task.

    Query params (opsional):
        event_types: daftar tipe event dipisah koma (opsional).
            Bila tidak diisi, semua event diambil (termasuk tool activity).
        project_id: project terkait (opsional).

    Mengembalikan daftar event terurut chronological berdasarkan timestamp.
    Event yang relevan: task_requested, task_started, agent_commentary,
    tool_called, tool_completed, observation_received, task_completed,
    task_failed, task_cancelled, task_finished, bible_update, dan lainnya.
    """
    project_id = request.GET.get("project_id") or None
    event_types_param = request.GET.get("event_types") or None
    event_types = (
        [e.strip() for e in event_types_param.split(",") if e.strip()]
        if event_types_param
        else None
    )
    return _json_response(
        {"task_id": task_id, "events": service.get_task_activity(task_id, project_id=project_id, event_types=event_types)}
    )


# ---------------------------------------------------------------------------
# Report API (final Agent Report per task)
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def task_report(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """GET /api/tasks/<task_id>/report -> final Agent Report dari .aether/log/.

    Source utama: task_completed.data.result.
    Fallback: task_finished.data.result.

    Query params (opsional):
        project_id: project terkait (opsional).
    """
    project_id = request.GET.get("project_id") or None
    return _json_response(service.get_task_report(task_id, project_id=project_id))


# ---------------------------------------------------------------------------
# Consultant API (AETHER reasoning layer — read-only terhadap CODE PROJECT)
# ---------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["POST"])
@_handle
def consultant_consult(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/consultant/consult -> satu giliran konsultasi Consultant.

    Body JSON:
        message (wajib)         : pertanyaan/permintaan user.
        mode (opsional)         : "quick" | "investigate" (default "quick").
        session_id (opsional)   : id sesi untuk konteks lintas giliran.
        provider_instance_id    : pilihan provider dari konfigurasi LLM (SQLite).
        model_id (opsional)     : pilihan model.
        project_id (opsional)   : project terkait (default active project).

    Consultant memakai loop & tool AETHER yang sudah ada (read-only terhadap
    CODE PROJECT, read+update terhadap Project Bible). Response memuat reply,
    tool_events, dan task_proposal (bila Consultant menghasilkan Task Proposal).
    """
    body = _parse_json_body(request)
    return _json_response(
        service.consult(
            message=body.get("message"),
            session_id=body.get("session_id") or None,
            provider_instance_id=body.get("provider_instance_id") or None,
            model_id=body.get("model_id") or None,
            project_id=body.get("project_id") or None,
            mode=body.get("mode") or None,
        )
    )


@require_http_methods(["GET"])
def events(request: HttpRequest) -> StreamingHttpResponse:
    """GET /api/events -> SSE stream event AETHER (server -> client).

    Query params (opsional):
        session_id: filter event berdasarkan session.
        task_id: filter event berdasarkan task.

    Django HANYA transport: event berasal dari SessionStore AETHER. Tidak ada
    event model kedua / broker / database.
    """
    service = get_service()
    session_id = request.GET.get("session_id") or None
    task_id = request.GET.get("task_id") or None

    subscription = EventSubscription(
        service.sessions,
        session_id=session_id,
        task_id=task_id,
    )
    subscription.start()

    def is_disconnected() -> bool:
        # Django menyediakan request.is_disconnected() pada versi modern.
        checker = getattr(request, "is_disconnected", None)
        return bool(checker()) if callable(checker) else False

    response = StreamingHttpResponse(
        sse_stream(subscription, is_disconnected=is_disconnected),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
