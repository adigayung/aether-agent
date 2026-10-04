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
        except Exception as exc:  # noqa: BLE001 - generic fallback for extension UI
            # Map CapabilityValidationError etc. to 400
            msg = str(exc)
            if "validation" in msg.lower() or "enum" in msg.lower() or "unknown" in msg.lower():
                return _json_response({"error": {"code": "validation_error", "message": msg}}, status=400)
            # Map known extension errors to 404/409
            if "not found" in msg.lower() or "unknown" in msg.lower():
                return _json_response({"error": {"code": "not_found", "message": msg}}, status=404)
            raise

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
# Global Settings (`data/settings.json` — SATU sumber konfigurasi global).
#
# View HANYA meneruskan ke facade AETHER (loader `agent_ai.config.settings`).
# Perubahan MERGE ke file yang sama; key/setting lain tidak hilang.
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def global_settings(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/settings -> nilai AKTUAL konfigurasi global `data/settings.json`."""
    return _json_response(service.get_global_settings())


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def global_settings_update(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/settings -> simpan perubahan konfigurasi global (deep-merge)."""
    body = _parse_json_body(request)
    return _json_response(service.update_global_settings(body))


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
def llm_provider_test(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/llm/providers/test -> test connection ke provider instance.

    Body: {provider_id: str}
    """
    body = _parse_json_body(request)
    return _json_response(service.test_llm_provider(provider_id=body.get("provider_id")))


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


# ---------------------------------------------------------------------------
# Project Policy / Permission (PROJECT-LOCAL: `<root>/.aether/permissions.json`).
#
# Satu sumber policy per project. Mode/scope di-enforce oleh PermissionManager
# EXISTING; TIDAK ada sistem permission kedua. Kelola dari Sidebar -> Projects
# (Project Settings / Policy), bukan dari Settings global.
# ---------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def project_policy(
    request: HttpRequest, service: GatewayService, project_id: str
) -> JsonResponse:
    """GET/POST /api/projects/<project_id>/policy -> Project Policy (per project).

    GET  -> nilai policy AKTUAL project (`<root>/.aether/permissions.json`).
    POST -> simpan policy (body: {mode, scope}); hanya project ini terpengaruh.
    """
    if request.method == "GET":
        return _json_response(service.get_project_policy(project_id))

    body = _parse_json_body(request)
    return _json_response(service.save_project_policy(project_id, body))


# ---------------------------------------------------------------------------
# GitHub Backup (OPTIONAL per project). Satu sumber konfigurasi dipakai
# bersama oleh halaman Backup (sidebar, project aktif) dan Projects.
# Credential (token) TIDAK pernah dikembalikan ke klien.
# ---------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def project_github(
    request: HttpRequest, service: GatewayService, project_id: str
) -> JsonResponse:
    """GET/POST /api/projects/<project_id>/github -> konfigurasi GitHub Backup.

    GET  -> status konfigurasi (tanpa token).
    POST -> simpan konfigurasi (body: repository, branch, exclude, token?).
    """
    if request.method == "GET":
        return _json_response(service.github_backup_status(project_id))

    body = _parse_json_body(request)
    return _json_response(service.save_github_backup_config(project_id, body))


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def project_github_test(
    request: HttpRequest, service: GatewayService, project_id: str
) -> JsonResponse:
    """POST /api/projects/<project_id>/github/test -> uji token/repo/branch."""
    body = _parse_json_body(request)
    return _json_response(service.test_github_backup_connection(project_id, body))


@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def project_github_checkpoints(
    request: HttpRequest, service: GatewayService, project_id: str
) -> JsonResponse:
    """GET/POST /api/projects/<project_id>/github/checkpoints.

    GET  -> daftar checkpoint (Git history).
    POST -> buat checkpoint (body: description) -> add/commit/push.
    """
    if request.method == "GET":
        return _json_response(service.list_github_checkpoints(project_id))

    body = _parse_json_body(request)
    record = service.create_github_checkpoint(project_id, body.get("description"))
    return _json_response(record, status=201)


@csrf_exempt
@require_http_methods(["GET"])
@_handle
def project_github_commits(
    request: HttpRequest, service: GatewayService, project_id: str
) -> JsonResponse:
    """GET /api/projects/<project_id>/github/commits -> paginated Git history.

    Query params:
        page     (int, default 1)
        per_page (int, default 20, maks 100)
    """
    try:
        page = int(request.GET.get("page", "1") or 1)
    except ValueError:
        page = 1
    try:
        per_page = int(request.GET.get("per_page", "20") or 20)
    except ValueError:
        per_page = 20

    return _json_response(
        service.get_github_backup_commits(project_id, page=page, per_page=per_page)
    )


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def project_github_restore(
    request: HttpRequest, service: GatewayService, project_id: str
) -> JsonResponse:
    """POST /api/projects/<project_id>/github/restore -> recovery ke checkpoint.

    Body: { commit: <hash>, force?: bool }.
    """
    body = _parse_json_body(request)
    return _json_response(
        service.restore_github_checkpoint(
            project_id, body.get("commit"), force=bool(body.get("force", False))
        )
    )


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


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def create_file(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/create-file -> buat file baru di dalam active project."""
    body = _parse_json_body(request)
    rel_path = body.get("path")
    content = body.get("content", "")
    if not rel_path:
        from api.services import ValidationError
        raise ValidationError("Field 'path' wajib diisi.")
    return _json_response(service.create_project_file(rel_path, content), status=201)


@require_http_methods(["GET"])
@_handle
def files(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/files?path=... -> daftar file project aktif (read-only).

    Memakai ListFilesTool AETHER (bukan abstraksi filesystem baru).
    """
    path = request.GET.get("path") or "."
    return _json_response(service.list_project_files(path))


@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def file_content(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """Isi file project aktif untuk Code Editor (Workbench).

    GET  /api/files/content?path=...  -> baca isi file (ReadFileTool AETHER).
    POST /api/files/content           -> simpan isi file (WriteFileTool AETHER),
                                         body: {path, content}.

    Backend tetap satu-satunya yang menyentuh filesystem: frontend TIDAK
    menulis file dari browser. Path divalidasi terhadap active project root
    oleh tool AETHER existing (workspace boundary sama dengan Explorer).
    """
    if request.method == "GET":
        path = request.GET.get("path")
        return _json_response(service.read_project_file(path))

    body = _parse_json_body(request)
    path = body.get("path")
    content = body.get("content")
    return _json_response(service.write_project_file(path, content))


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
        project_id = request.GET.get("project_id") or None
        return _json_response({"tasks": service.list_tasks(project_id=project_id)})

    body = _parse_json_body(request)
    task = body.get("task")
    project_id = body.get("project_id")
    metadata = body.get("metadata")
    execution_mode = body.get("execution_mode")
    # Attachment gambar (multimodal, opsional). Daftar {data, mime_type,
    # filename?}; divalidasi/dinormalisasi service (batas sama Consultant).
    images = body.get("images")
    if execution_mode is None and metadata and isinstance(metadata, dict):
        execution_mode = metadata.get("execution_mode")
    if metadata is not None and not isinstance(metadata, dict):
        from api.services import ValidationError

        raise ValidationError("Field 'metadata' harus berupa object bila diisi.")
    record = service.create_task(
        task=task,
        project_id=project_id,
        metadata=metadata,
        execution_mode=execution_mode,
        images=images,
    )
    return _json_response(record, status=201)


# ---------------------------------------------------------------------------
# Approval (ASK) — keputusan user untuk action yang ditahan policy.
#
# BUKAN sistem permission kedua: keputusan tetap dibuat PermissionManager
# existing. Endpoint ini hanya menyampaikan Allow/Deny user ke action yang
# DITAHAN, terikat ke task/session agar tidak tertukar antar task.
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def task_approvals(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/tasks/approvals -> approval yang masih PENDING (opsional filter).

    Query param: ?task_id=<id> (opsional).
    """
    task_id = request.GET.get("task_id") or None
    return _json_response({"approvals": service.list_approvals(task_id=task_id)})


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def task_approval_resolve(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/tasks/approvals/resolve -> Allow/Deny sebuah approval.

    Body: { request_id: str, allow: bool }.
    """
    body = _parse_json_body(request)
    allow = body.get("allow")
    if isinstance(allow, str):
        allow = allow.strip().lower() in ("1", "true", "yes", "allow", "on")
    return _json_response(
        service.resolve_approval(body.get("request_id"), bool(allow))
    )


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
# Task Queue API (TAMPILAN/kontrol UI antrian — belum ada scheduler serial)
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def task_queue(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/tasks/queue -> daftar antrian task (pending/running/disabled).

    Satu queue GLOBAL AETHER: sumber data tetap TaskRecord in-memory yang sama
    dengan GET /api/tasks. Endpoint ini hanya memproyeksikan status antrian.
    """
    project_id = request.GET.get("project_id") or None
    return _json_response({"tasks": service.list_queue(project_id=project_id)})


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def task_queue_disable(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """POST /api/tasks/queue/<task_id>/disable -> tandai task jangan dieksekusi.

    Disable != cancel: task tetap ada di antrian (queue_state="disabled") dan
    tidak dihapus. Hanya berlaku untuk task yang belum running.
    """
    return _json_response(service.set_queue_state(task_id, "disabled"))


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def task_queue_enable(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """POST /api/tasks/queue/<task_id>/enable -> kembalikan task ke pending."""
    return _json_response(service.set_queue_state(task_id, "pending"))


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def task_queue_move(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """POST /api/tasks/queue/<task_id>/move -> geser posisi task di antrian.

    Body JSON: {"direction": "up"|"down"}. Hanya task non-running.
    """
    body = _parse_json_body(request)
    direction = body.get("direction")
    return _json_response({"tasks": service.move_task(task_id, direction)})


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def task_queue_remove(request: HttpRequest, service: GatewayService, task_id: str) -> JsonResponse:
    """POST /api/tasks/queue/<task_id>/remove -> hapus task dari antrian.

    HANYA task non-running (running harus di-Stop/cancel). Ini bukan cancel.
    """
    return _json_response(service.remove_task(task_id))


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
# Extension UI System (Task 05) - generic contract
# ---------------------------------------------------------------------------
def _ext_ui_context() -> Any:
    """Build runtime UI context: registry + capability + loader state.

    We reuse existing singletons where possible. For generic API we create
    ephemeral registries populated via ExtensionLoader from default extensions dir.
    Results are dynamic (reads loader state on each call).
    """
    # Attempt to reuse global loader if Django has one; otherwise build fresh via loader
    # For simplicity, build a fresh loader reading existing Extension directory
    from agent_ai.extensions.capabilities import CapabilityRegistry
    from agent_ai.extensions.registry import ExtensionRegistry
    from agent_ai.extensions.loader import ExtensionLoader
    from agent_ai.extensions.ui import UICatalog, build_form_schema_for_extension

    # We load extensions each time? For UI listing we need current capabilities.
    # To avoid heavy reload, we try to find existing global registry cached on service
    service = get_service()
    cap_reg = getattr(service, "_ext_capability_registry", None)
    ext_reg = getattr(service, "_ext_registry", None)
    if cap_reg is None:
        cap_reg = CapabilityRegistry()
        ext_reg = ExtensionRegistry()
        loader = ExtensionLoader(registry=ext_reg, capability_registry=cap_reg, enable_entry_points=False)
        try:
            loader.load_all()
        except Exception:
            pass
        # Cache on service for subsequent calls
        try:
            service._ext_capability_registry = cap_reg  # type: ignore[attr-defined]
            service._ext_registry = ext_reg  # type: ignore[attr-defined]
        except Exception:
            pass
    return cap_reg, ext_reg


@require_http_methods(["GET"])
@_handle
def extensions_ui(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/extensions/ui -> list UI contributions (generic).

    Query params (optional):
        extension_id: filter by extension
        type: filter by UI type (modal|form|panel|table|chart|viewer|wizard|result_renderer|action|custom_view)
        enabled_only: 1/0 (default 1)
    """
    from agent_ai.extensions.ui import UICatalog

    cap_reg, ext_reg = _ext_ui_context()
    extension_id = request.GET.get("extension_id") or None
    ui_type = request.GET.get("type") or None
    enabled_raw = request.GET.get("enabled_only", "1")
    enabled_only = str(enabled_raw).lower() not in ("0", "false", "no")
    catalog = UICatalog(cap_reg, ext_reg)
    items = catalog.list_contributions(extension_id=extension_id, ui_type=ui_type, enabled_only=enabled_only)
    return _json_response({"count": len(items), "contributions": [c.to_dict() for c in items]})


@require_http_methods(["GET"])
@_handle
def extension_config(request: HttpRequest, service: GatewayService, extension_id: str) -> JsonResponse:
    """GET /api/extensions/config/<extension_id> -> form schema for extension config.

    Returns declarative form schema derived from capability metadata (secret-safe).
    """
    from agent_ai.extensions.ui import build_form_schema_for_extension
    from agent_ai.extensions.config import get_config_store

    cap_reg, ext_reg = _ext_ui_context()
    # Verify extension exists
    if not cap_reg.list_by_extension(extension_id) and (ext_reg is None or not ext_reg.exists(extension_id)):
        # Not a hard fail: if no config, return empty form schema
        pass
    try:
        store = get_config_store()
    except Exception:
        store = None
    project_id = request.GET.get("project_id") or None
    schema = build_form_schema_for_extension(extension_id, cap_reg, config_store=store, project_id=project_id)
    # Also include raw definitions for debugging (without secret values)
    return _json_response({"extension_id": extension_id, "schema": schema})


@csrf_exempt
@require_http_methods(["GET", "POST", "PUT"])
@_handle
def extension_config_key(request: HttpRequest, service: GatewayService, extension_id: str, key: str) -> JsonResponse:
    """GET/POST/PUT /api/extensions/config/<extension_id>/<key> -> get/set config value.

    GET  -> returns value (for non-secret) or configured flag (for secret)
    POST/PUT -> sets value (validated via existing ConfigFacade/config store)
    """
    import json as _json

    from agent_ai.extensions.context import ExtensionContext
    from agent_ai.extensions.manifest import Manifest
    from agent_ai.extensions.config import get_config_store, ConfigValidationError
    from agent_ai.extensions.capabilities import CapabilityValidationError as _CVE

    cap_reg, ext_reg = _ext_ui_context()
    store = get_config_store()
    manifest = Manifest(id=extension_id, name=extension_id, version="1", description="", api_version="1", raw={}, source_path="")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)

    if request.method == "GET":
        try:
            # Determine if secret
            rec = cap_reg.get("config", f"{extension_id}.{key}")
            is_secret = False
            if rec is not None:
                is_secret = bool(rec.metadata.get("secret") or rec.metadata.get("type") == "secret")
            if is_secret:
                # Never return value, just configured flag
                has = ctx.config.has(key)
                return _json_response({"key": key, "extension_id": extension_id, "configured": bool(has), "secret": True})
            else:
                val = ctx.config.get(key)
                return _json_response({"key": key, "extension_id": extension_id, "value": val})
        except Exception as exc:
            # Map to validation/not found
            raise
    else:
        # POST/PUT set value
        body = _parse_json_body(request)
        value = body.get("value")
        scope = body.get("scope") or None
        project_id = body.get("project_id") or request.GET.get("project_id") or None
        try:
            ctx.config.set(key, value, scope=scope, project_id=project_id)
        except (ConfigValidationError, _CVE) as exc:
            from api.services import ValidationError
            raise ValidationError(str(exc)) from exc
        return _json_response({"key": key, "extension_id": extension_id, "value": value if not ctx.config.is_secret(key) else "[redacted]", "saved": True})


# ---------------------------------------------------------------------------
# Extension Management (Task 07) — generic management API (thin over ExtensionManager)
# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@_handle
def extensions_list(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """GET /api/extensions -> list installed extensions (generic, UI-friendly)."""
    data = service.list_extensions()
    return _json_response(data)


@require_http_methods(["GET", "DELETE"])
@_handle
def extensions_detail(request: HttpRequest, service: GatewayService, extension_id: str) -> JsonResponse:
    """GET /api/extensions/<id> -> detail; DELETE -> uninstall."""
    if request.method == "DELETE":
        result = service.uninstall_extension(extension_id)
        return _json_response(result)
    data = service.get_extension(extension_id)
    return _json_response(data)


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def extensions_install(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/extensions/install -> install from Git URL (generic). Body: {repository_url, ref?}"""
    body = _parse_json_body(request)
    # Accept both repository_url and repository for flexibility
    repo = body.get("repository_url") or body.get("repository") or body.get("url") or ""
    ref = body.get("ref") or body.get("branch") or None
    # Light frontend-style validation: empty check; backend is authoritative for git/manifest/etc.
    if not repo or not str(repo).strip():
        from api.services import ValidationError

        raise ValidationError("Field 'repository_url' wajib diisi.")
    result = service.install_extension(str(repo).strip(), ref=str(ref).strip() if ref and str(ref).strip() else None)
    return _json_response(result, status=201)


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def extensions_enable(request: HttpRequest, service: GatewayService, extension_id: str) -> JsonResponse:
    """POST /api/extensions/<id>/enable -> enable extension."""
    result = service.enable_extension(extension_id)
    return _json_response(result)


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def extensions_disable(request: HttpRequest, service: GatewayService, extension_id: str) -> JsonResponse:
    """POST /api/extensions/<id>/disable -> disable extension."""
    result = service.disable_extension(extension_id)
    return _json_response(result)


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def extensions_update(request: HttpRequest, service: GatewayService, extension_id: str) -> JsonResponse:
    """POST /api/extensions/<id>/update -> update extension (generic). Body: {repository_url?, ref?}"""
    body = _parse_json_body(request)
    # Optional: repository_url may be omitted to use stored source; Task 07 prioritizes existing source
    repo = body.get("repository_url") or body.get("repository") or body.get("url") or None
    ref = body.get("ref") or body.get("branch") or None
    result = service.update_extension(extension_id, repository_url=str(repo).strip() if repo and str(repo).strip() else None, ref=str(ref).strip() if ref and str(ref).strip() else None)
    return _json_response(result)


@csrf_exempt
@require_http_methods(["POST"])
@_handle
def extensions_result(request: HttpRequest, service: GatewayService) -> JsonResponse:
    """POST /api/extensions/result -> validate/resolve a structured result payload.

    Body: {renderer?, type?, data?, artifact?, metadata?}
    Returns: {renderer, type, data, artifact, metadata} with resolved renderer.
    Generic, no hardcode extension.
    """
    from agent_ai.extensions.ui import resolve_renderer_for_result

    body = _parse_json_body(request)
    renderer = body.get("renderer") or body.get("type") or resolve_renderer_for_result(body)
    result: Dict[str, Any] = {
        "renderer": renderer,
        "type": body.get("type") or renderer,
        "data": body.get("data"),
        "metadata": body.get("metadata") or {},
    }
    if body.get("artifact") is not None:
        result["artifact"] = body["artifact"]
    return _json_response({"result": result, "resolved_renderer": renderer})


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
        images (opsional)       : daftar gambar multimodal. Setiap item:
            {"data": "<base64>", "mime_type": "image/png", "filename": opsional}.
            Diteruskan ke Consultant untuk diproses modul vision existing.

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
            images=body.get("images") or None,
        )
    )


# ---------------------------------------------------------------------------
# Consultant Session API (list / get / create / rename / delete / reset)
# ---------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handle
def consultant_sessions(
    request: HttpRequest, service: GatewayService
) -> JsonResponse:
    """GET /api/consultant/sessions -> list consultant sessions (newest first).

    Query params:
        project_id (opsional): filter sessions by project.

    POST /api/consultant/sessions -> create a new consultant session.

    Body JSON (opsional):
        project_id: project terkait (default active project).
        title: judul sesi (default auto dari pesan user pertama).
    """
    if request.method == "GET":
        project_id = request.GET.get("project_id") or None
        sessions = service.list_consultant_sessions(project_id=project_id)
        # Light metadata for the list (UI hanya butuh judul/waktu/turn count).
        items = []
        for s in sessions:
            turns = s.get("turns") or []
            items.append(
                {
                    "session_id": s.get("session_id"),
                    "project_id": s.get("project_id"),
                    "title": s.get("title"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                    "turn_count": len(turns),
                }
            )
        return _json_response({"sessions": items})

    body = _parse_json_body(request)
    project_id = body.get("project_id") or None
    title = body.get("title") or None
    if not project_id:
        try:
            project_id = service.project_store.get_active_project_id() or None
        except Exception:  # noqa: BLE001
            project_id = None
    session = service.create_consultant_session(project_id=project_id, title=title)
    return _json_response(session, status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
@_handle
def consultant_session_detail(
    request: HttpRequest, service: GatewayService, session_id: str
) -> JsonResponse:
    """GET /api/consultant/sessions/<id> -> get full session (incl. turns).

    PATCH /api/consultant/sessions/<id> -> rename (body: {\"title\": \"...\"}).

    DELETE /api/consultant/sessions/<id> -> delete session.
    """
    # project_id filter (opsional, agar scope per-project konsisten)
    project_id = request.GET.get("project_id") or None
    if not project_id:
        try:
            project_id = service.project_store.get_active_project_id() or None
        except Exception:  # noqa: BLE001
            project_id = None

    if request.method == "GET":
        session = service.get_consultant_session(session_id, project_id=project_id)
        if session is None:
            from api.services import NotFoundError

            raise NotFoundError("Session not found")
        return _json_response(session)

    if request.method == "PATCH":
        body = _parse_json_body(request)
        title = body.get("title")
        if not title or not str(title).strip():
            from api.services import ValidationError

            raise ValidationError("Field 'title' wajib diisi.")
        updated = service.rename_consultant_session(
            session_id, str(title).strip(), project_id=project_id
        )
        if updated is None:
            from api.services import NotFoundError

            raise NotFoundError("Session not found")
        return _json_response(updated)

    # DELETE
    if project_id:
        deleted = service.delete_consultant_session(session_id, project_id=project_id)
    else:
        deleted = service.delete_consultant_session(session_id)
    if not deleted:
        from api.services import NotFoundError

        raise NotFoundError("Session not found")
    return _json_response({"deleted": True, "session_id": session_id})


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
