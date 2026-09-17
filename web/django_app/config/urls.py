"""URL configuration untuk AETHER Gateway (#50).

Root URLconf: meneruskan semua endpoint /api/ ke app `api`.

Hardening (#57): bila frontend production build (web/frontend/dist) tersedia,
Django menyajikan file statisnya (index.html + assets) sebagai fallback.
Ini HANYA static serving di layer web; tidak ada logic AETHER di Django.
"""

from __future__ import annotations

from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("api/", include("api.urls")),
]

# Sajikan frontend production build (bila ada). Tidak mengubah dev (Vite).
if getattr(settings, "FRONTEND_DIST_EXISTS", False):
    _dist = settings.FRONTEND_DIST_DIR
    urlpatterns += [
        # Root "/" menyajikan index.html (entry point Workbench).
        path("", serve, {"document_root": _dist, "path": "index.html"}),
        re_path(r"^(?P<path>assets/.*)$", serve, {"document_root": _dist}),
        re_path(r"^(?P<path>favicon\.ico)$", serve, {"document_root": _dist}),
        re_path(r"^(?P<path>index\.html)$", serve, {"document_root": _dist}),
    ]
