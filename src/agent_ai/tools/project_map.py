"""Tool capability untuk Project Map (Atlas/RIG).

Empat capability:

    atlas_query(query, ...)          -> navigasi project lewat CODE ATLAS
    rig_query(query, relation, ...)  -> structural/code relationship lewat RIG
    project_map_status()             -> status ringkas map (available/missing/invalid
                                        + freshness fresh/stale)
    refresh_project_map(target, ...) -> regenerate map (HANYA Agent; bukan Consultant)

Alur yang dituju:

    LLM -> project_map_status() -> (stale?) -> refresh_project_map() -> query -> read_file()

Prinsip (lihat juga `agent_ai.projects.project_map_query`):
    - Map dibaca dari `.aether/map/atlas.json` / `.aether/map/rig.json` yang
      SUDAH ada lewat `ProjectMapService` (tidak regenerate saat query).
    - Query pada map `stale` TETAP membaca map lama dan melaporkan
      `map_status`; query != regenerate.
    - Hasil SELALU subset kecil (limit + indikator truncated); map penuh tidak
      pernah dikirim ke LLM.
    - Tidak ada subsystem baru: memakai tool framework AETHER existing
      (`BaseTool`/`ToolRegistry`) + query adapter di layer `agent_ai.projects`.
    - Map BUKAN context permanen: tidak ada map yang di-inject ke prompt, dan
      tidak ada query/refresh yang dijalankan otomatis. LLM yang memutuskan
      kapan tool ini perlu dipanggil.

Wiring (Agent vs Consultant):
    - Agent (`agent_ai.tools.registry.build_registry`)  : 4 tool (termasuk
      `refresh_project_map`).
    - Consultant (`agent_ai.consultant.tools.build_consultant_registry`,
      mode INVESTIGATE)                                 : 3 tool READ-ONLY
      (`atlas_query`, `rig_query`, `project_map_status`) TANPA
      `refresh_project_map`, sehingga Consultant tetap read-only terhadap map.
    - `build_project_map_tools()` / `build_project_map_registry()` dipakai
      kedua registry tersebut agar konstruksi tool hanya punya satu sumber.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.base import BaseTool, ToolValidationError

#: Batas default hasil (lihat `project_map_query.DEFAULT_MAX_RESULTS`).
_DEFAULT_MAX_RESULTS = 20

#: Target yang didukung `refresh_project_map`.
_REFRESH_TARGETS = ("atlas", "rig", "both")


class _ProjectMapToolBase(BaseTool):
    """Base untuk tool Project Map: root project + service yang bisa di-inject."""

    def __init__(self, root: Optional[Any] = None, service: Any = None) -> None:
        self.root = Path(root).resolve() if root is not None else None
        self._service = service

    # ------------------------------------------------------------------ #
    def _get_service(self) -> Any:
        if self._service is None:
            from agent_ai.projects.project_map import ProjectMapService

            self._service = ProjectMapService()
        return self._service

    def _require_root(self) -> Path:
        if self.root is None:
            raise ToolValidationError(
                "Project root tidak tersedia; capability Project Map tidak dapat "
                "dijalankan (root project harus di-set saat tool dibuat)."
            )
        return self.root


class AtlasQueryTool(_ProjectMapToolBase):
    """Cari symbol/module/file relevan di project lewat CODE ATLAS."""

    name = "atlas_query"
    description = (
        "Mencari LOKASI kode yang relevan di project ini melalui peta navigasi "
        "Code Atlas (.aether/map/atlas.json). Gunakan untuk menemukan di mana "
        "sebuah class/function/method/module berada (nama file + rentang baris) "
        "dan relasi dasarnya (callers/callees/inherits). Hasil yang dikembalikan "
        "SELALU subset kecil (dibatasi max_results) - bukan seluruh peta. Setelah "
        "menemukan lokasi, gunakan read_file(path, start_line, end_line) untuk "
        "membaca kode aslinya. Tool ini READ-ONLY dan tidak meregenerasi peta; "
        "bila peta belum ada, akan dikembalikan error yang jelas."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Nama yang dicari: symbol/class/function/method (mis. "
                    "'TaskExecutor' atau 'TaskExecutor.execute'), nama module, "
                    "atau potongan path file."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Filter opsional: any | symbol | class | function | method | "
                    "module | file (default: any)."
                ),
            },
            "include_relations": {
                "type": "boolean",
                "description": (
                    "Sertakan relasi dasar (callers/callees/inherits) bila True "
                    "(default: True)."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Batas jumlah hasil (default 20, maksimum 50).",
            },
        },
        "required": ["query"],
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        from agent_ai.projects.project_map import MapInvalidError, MapNotFoundError
        from agent_ai.projects.project_map_query import MapQueryError, atlas_query

        root = self._require_root()
        query = arguments.get("query")
        try:
            return atlas_query(
                self._get_service(),
                root,
                query,
                kind=arguments.get("kind"),
                include_relations=bool(arguments.get("include_relations", True)),
                max_results=arguments.get("max_results", _DEFAULT_MAX_RESULTS),
            )
        except (MapQueryError, MapNotFoundError, MapInvalidError) as exc:
            raise ToolValidationError(str(exc)) from exc


class RigQueryTool(_ProjectMapToolBase):
    """Cari entity + relationship struktural lewat MAP_CODE_RIG."""

    name = "rig_query"
    description = (
        "Mencari entity kode (class/function/method/module/component) dan "
        "RELATIONSHIP-nya melalui Repository Intelligence Graph "
        "(.aether/map/rig.json). Gunakan untuk pertanyaan seperti 'siapa yang "
        "memanggil X' (relation='callers'), 'X memanggil siapa' "
        "(relation='callees'), 'apa yang di-import/di-inherit X', atau relasi "
        "lain (imports, inherits, contains, depends_on, tests, related). Hasil "
        "SELALU subset kecil (dibatasi max_results) - bukan seluruh graph. "
        "READ-ONLY: tidak meregenerasi peta; bila peta belum ada, dikembalikan "
        "error yang jelas."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Nama entity yang dicari, mis. 'TaskExecutor' atau "
                    "'TaskExecutor.execute'."
                ),
            },
            "relation": {
                "type": "string",
                "description": (
                    "Relasi opsional: callers | callees | calls | imports | "
                    "inherits | contains | depends_on | tests | external | "
                    "related (default: related = semua relasi entity)."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Filter opsional: component | aggregator | runner | test | "
                    "external_package | package_manager | file | module | class | "
                    "function | symbol (default: any)."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Batas jumlah hasil (default 20, maksimum 50).",
            },
        },
        "required": ["query"],
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        from agent_ai.projects.project_map import MapInvalidError, MapNotFoundError
        from agent_ai.projects.project_map_query import MapQueryError, rig_query

        root = self._require_root()
        query = arguments.get("query")
        try:
            return rig_query(
                self._get_service(),
                root,
                query,
                relation=arguments.get("relation"),
                kind=arguments.get("kind"),
                max_results=arguments.get("max_results", _DEFAULT_MAX_RESULTS),
            )
        except (MapQueryError, MapNotFoundError, MapInvalidError) as exc:
            raise ToolValidationError(str(exc)) from exc


class ProjectMapStatusTool(_ProjectMapToolBase):
    """Status ringkas map project (keberadaan + freshness, tanpa isi map)."""

    name = "project_map_status"
    description = (
        "Mengembalikan status ringkas peta project (atlas/rig): keberadaan "
        "(available/missing/invalid) dan FRESHNESS (fresh/stale). Gunakan untuk "
        "memeriksa apakah atlas_query / rig_query bisa dipakai DAN apakah peta "
        "masih merepresentasikan source terbaru sebelum memutuskan perlu "
        "refresh_project_map. 'stale' berarti source code berubah sejak peta "
        "digenerate; hasilnya HANYA status (tanpa isi peta)."
    )
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, **arguments: Any) -> Dict[str, Any]:  # noqa: D401
        root = self._require_root()
        status = self._get_service().get_status(root)
        maps = status.get("maps") or {}

        def entry(name: str) -> Dict[str, Any]:
            return maps.get(name) or {}

        result: Dict[str, Any] = {
            "project": status.get("project"),
            "map_dir": status.get("map_dir"),
            "overall": status.get("status"),
            "atlas": entry("atlas").get("status"),
            "rig": entry("rig").get("status"),
            "freshness": status.get("freshness"),
            "atlas_freshness": entry("atlas").get("freshness"),
            "rig_freshness": entry("rig").get("freshness"),
        }
        stale = status.get("stale") or []
        if stale:
            result["stale"] = list(stale)
            reasons = {
                name: entry(name).get("reason")
                for name in stale
                if entry(name).get("reason")
            }
            if reasons:
                result["reasons"] = reasons
        invalid = status.get("invalid") or []
        if invalid:
            result["invalid"] = list(invalid)
        missing = status.get("missing") or []
        if missing:
            result["missing"] = list(missing)
        errors = {
            name: entry(name).get("error")
            for name in maps
            if entry(name).get("error")
        }
        if errors:
            result["errors"] = errors
        return result


class RefreshProjectMapTool(_ProjectMapToolBase):
    """Regenerate Project Map (Atlas/RIG) memakai engine yang sudah ada.

    CAPABILITY AGENT-ONLY: registry Consultant tidak pernah mendaftarkan tool
    ini, sehingga Consultant tetap read-only terhadap Project Map.

    Tidak ada refresh otomatis: LLM/Agent yang memutuskan kapan peta perlu
    di-refresh. Generation memakai `ProjectMapService.generate_maps()` (engine
    CLI existing, penulisan atomik per map). Untuk `target="both"`, Atlas dan
    RIG digenerate PARALEL. Bila salah satu gagal, map lain yang berhasil tetap
    diganti dan map lama yang gagal tetap aman.
    """

    name = "refresh_project_map"
    description = (
        "Meregenerasi peta project (.aether/map/atlas.json dan/atau rig.json) "
        "dengan engine Atlas/RIG yang sudah ada. Panggil HANYA bila peta sudah "
        "stale / belum ada dan Anda memang membutuhkannya - bukan setiap task. "
        "Untuk target='both', Atlas dan RIG digenerate paralel. Penulisan "
        "atomik: bila generation gagal, map lama tetap aman (kegagalan satu map "
        "tidak merusak yang lain). Setelah selesai, baca hasilnya lewat "
        "atlas_query / rig_query."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Bagian yang di-refresh: atlas | rig | both (default: both)."
                ),
            }
        },
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        from agent_ai.projects.project_map import MAP_TYPE_ATLAS, MAP_TYPE_RIG

        root = self._require_root()
        target = str(arguments.get("target") or "both").strip().lower()
        if target not in _REFRESH_TARGETS:
            raise ToolValidationError(
                "Argumen 'target' harus salah satu dari: {}.".format(
                    ", ".join(_REFRESH_TARGETS)
                )
            )

        types = [MAP_TYPE_ATLAS, MAP_TYPE_RIG] if target == "both" else [target]
        service = self._get_service()
        # `both` -> Atlas & RIG digenerate paralel (independen).
        outcomes = service.generate_maps(
            root, types, parallel=(target == "both")
        )

        refreshed: List[Dict[str, Any]] = []
        failed: Dict[str, str] = {}
        for map_type in types:
            outcome = outcomes.get(map_type) or {}
            if outcome.get("ok"):
                refreshed.append(
                    {
                        "map": map_type,
                        "path": outcome.get("path"),
                        "size": outcome.get("size"),
                    }
                )
            else:
                failed[map_type] = str(outcome.get("error") or "generation gagal")

        if not refreshed:
            raise ToolValidationError(
                "; ".join(
                    "{}: {}".format(name, detail) for name, detail in failed.items()
                )
                or "Refresh project map gagal."
            )

        status = service.get_status(root) or {}
        result: Dict[str, Any] = {
            "target": target,
            "refreshed": refreshed,
            "status": status.get("status"),
            "freshness": status.get("freshness"),
        }
        if failed:
            result["failed"] = failed
        return result


def build_project_map_tools(
    root: Optional[Any] = None,
    service: Any = None,
    include_refresh: bool = False,
) -> List[BaseTool]:
    """Bangun daftar capability Project Map (satu sumber konstruksi tool).

    Dipakai registry Agent (``include_refresh=True``) dan registry Consultant
    (``include_refresh=False``) sehingga hanya ada satu tempat yang tahu tool
    Project Map mana yang ada.

    Args:
        root: root project target (lokasi `.aether/map/`).
        service: `ProjectMapService` opsional (mis. override engine dir).
        include_refresh: bila True, sertakan `refresh_project_map` (menulis
            map). Registry Consultant TIDAK boleh memakai True.

    Returns:
        List tool: atlas_query, rig_query, project_map_status
        (+ refresh_project_map bila include_refresh).
    """
    tools: List[BaseTool] = [
        AtlasQueryTool(root=root, service=service),
        RigQueryTool(root=root, service=service),
        ProjectMapStatusTool(root=root, service=service),
    ]
    if include_refresh:
        tools.append(RefreshProjectMapTool(root=root, service=service))
    return tools


def build_project_map_registry(
    root: Optional[Any] = None,
    service: Any = None,
    include_refresh: bool = False,
) -> Any:
    """Bangun ToolRegistry berisi capability Project Map.

    Default = tiga capability READ-ONLY (registry Consultant). Agent memakai
    `include_refresh=True` untuk menambahkan `refresh_project_map`.

    Args:
        root: root project target (lokasi `.aether/map/`).
        service: `ProjectMapService` opsional (mis. untuk override engine dir).
        include_refresh: sertakan `refresh_project_map` (HANYA untuk Agent).

    Returns:
        `ToolRegistry` berisi capability Project Map.
    """
    from agent_ai.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for tool in build_project_map_tools(
        root=root, service=service, include_refresh=include_refresh
    ):
        registry.register(tool)
    return registry


__all__ = [
    "AtlasQueryTool",
    "RigQueryTool",
    "ProjectMapStatusTool",
    "RefreshProjectMapTool",
    "build_project_map_tools",
    "build_project_map_registry",
]
