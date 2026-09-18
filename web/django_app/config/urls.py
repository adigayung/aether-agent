from __future__ import annotations

from django.conf import settings
from django.http import HttpResponseBase
from django.urls import include, path, re_path
from django.views.static import serve


def serve_frontend(request, path, document_root=None) -> HttpResponseBase:
    """Sajikan file frontend production build + header cache yang benar.

    Akar masalah "UI Settings lama setelah npm run build": `static.serve`
    TIDAK mengirim `Cache-Control`, sehingga browser menyimpan `index.html`
    (yang menunjuk bundle lama) via heuristic caching dan terus menampilkan UI
    lama walau bundle baru sudah ada di disk. Server sendiri sudah menyajikan
    file terbaru; yang stale adalah cache browser.

    Aturan:
        - `index.html` (entry point) -> JANGAN di-cache: selalu revalidate agar
          selalu menunjuk bundle (hash) terbaru hasil build.
        - `assets/**` (nama ber-hash, immutable) -> boleh di-cache lama.
    """
    response = serve(request, path, document_root=document_root)
    if (path or "").startswith("assets/"):
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
    return response


urlpatterns = [
    path("api/", include("api.urls")),
]

# Sajikan frontend production build (bila ada). Tidak mengubah dev (Vite).
if getattr(settings, "FRONTEND_DIST_EXISTS", False):
    _dist = settings.FRONTEND_DIST_DIR
    urlpatterns += [
        # Root "/" menyajikan index.html (entry point Workbench).
        path("", serve_frontend, {"document_root": _dist, "path": "index.html"}),
        re_path(r"^(?P<path>assets/.*)$", serve_frontend, {"document_root": _dist}),
        re_path(r"^(?P<path>favicon\.ico)$", serve_frontend, {"document_root": _dist}),
        re_path(r"^(?P<path>index\.html)$", serve_frontend, {"document_root": _dist}),
    ]
