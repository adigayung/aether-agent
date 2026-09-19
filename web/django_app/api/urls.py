"""URL routing untuk AETHER Gateway API (#50).

Endpoint minimum:
    GET  /api/health
    GET  /api/projects
    GET  /api/tasks             (list/history, #53)
    POST /api/tasks
    GET  /api/tasks/<task_id>
    GET  /api/events            (SSE, #51)
"""

from __future__ import annotations

from django.urls import path

from api import views

urlpatterns = [
    path("health", views.health, name="health"),
    path("config", views.config, name="config"),
    # LLM Config / Settings (LLMConfigService AETHER existing).
    path("llm/config", views.llm_config, name="llm_config"),
    path("llm/credentials", views.llm_credentials, name="llm_credentials"),
    path(
        "llm/credentials/delete",
        views.delete_llm_credential,
        name="delete_llm_credential",
    ),
    path("llm/providers", views.llm_providers, name="llm_providers"),
    path(
        "llm/providers/<str:provider_id>",
        views.llm_provider_detail,
        name="llm_provider_detail",
    ),
    path("llm/models", views.llm_models, name="llm_models"),
    path("llm/models/<str:model_id>", views.llm_model_detail, name="llm_model_detail"),
    path("projects", views.projects, name="projects"),
    path("projects/<str:project_id>", views.delete_project, name="delete_project"),
    path("active-project", views.active_project, name="active_project"),
    path("open-in-explorer", views.open_in_explorer, name="open_in_explorer"),
    path("reveal-in-explorer", views.reveal_in_explorer, name="reveal_in_explorer"),
    path("delete-entry", views.delete_entry, name="delete_entry"),
    path("files", views.files, name="files"),
    path("tasks", views.tasks, name="tasks"),
    # Task History (reads .aether/log/ persistent store).
    # PENTING: route literal "tasks/history" HARUS mendahului
    # "tasks/<str:task_id>" agar tidak di-shadow (task_id="history").
    path("tasks/history", views.task_history, name="task_history"),
    path("tasks/history/<str:task_id>", views.task_history_detail, name="task_history_detail"),
    path("tasks/<str:task_id>", views.get_task, name="get_task"),
    path("tasks/<str:task_id>/cancel", views.cancel_task, name="cancel_task"),
    # Activity API (chronological events per task)
    path("tasks/<str:task_id>/activity", views.task_activity, name="task_activity"),
    # Report API (final Agent Report per task)
    path("tasks/<str:task_id>/report", views.task_report, name="task_report"),
    path("events", views.events, name="events"),
]
