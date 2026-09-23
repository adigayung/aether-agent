"""Query adapter untuk Project Map (CODE ATLAS + MAP_CODE_RIG).

Tujuan modul ini: memberi LLM *subset kecil yang relevan* dari map yang sudah
tersimpan (`.aether/map/atlas.json` / `.aether/map/rig.json`) - BUKAN seluruh
map. Modul ini:

    - HANYA membaca map yang sudah ada lewat `ProjectMapService` (tidak
      menjalankan/meregenerasi engine Atlas/RIG saat query).
    - Tidak membuat database/index/vector store/search engine baru: map JSON
      tetap menjadi sumber data (parsing langsung, stdlib-only).
    - Tidak meng-import engine Atlas/RIG ke proses AETHER (engine tetap
      terisolasi; AETHER hanya membaca output JSON-nya).

Bentuk output dirancang kecil + deterministik:

    {
      "query": "...",
      "total": 87,          # jumlah match sebelum limit
      "returned": 20,       # jumlah item yang benar-benar dikembalikan
      "truncated": True,    # True bila hasil dipotong limit
      "results": [ ... ],   # subset relevan (kecil)
      "map_status": "fresh",# "fresh" | "stale" (map lama tetap boleh dibaca)
      "hint": "..."         # OPSIONAL: hanya saat 0 hasil dan/atau map stale
    }

`message`/`hint` adalah sinyal RINGKAS untuk LLM: keduanya menegaskan bahwa map
adalah LOOKUP pada data index (bukan pencarian full-text isi source), bahwa 0
hasil berarti "tidak ditemukan di Project Map", dan bahwa `stale` BUKAN error
maupun alasan mengulang query/sinonim tanpa batas. Hint hanya ditambahkan saat
ada sinyal yang perlu ditindaklanjuti (0 hasil dan/atau stale) supaya output
tetap hemat token.

Catatan freshness: query SELALU boleh membaca map lama. `map_status` hanya
memberi tahu LLM apakah map yang dibaca masih merepresentasikan source terbaru;
query TIDAK pernah meregenerasi map.

Skema yang dibaca
=================

`atlas.json` (CODE ATLAS):
    {
      "project": {"name": ..., "language": ...},
      "files": ["rel/path.py", ...],
      "modules": {"mod.name": {"file": "rel/path.py"}, ...},
      "symbols": {"Class.method": {"kind": "class|function|method",
                                   "file": "...", "start_line": N,
                                   "end_line": N, "methods": [...]}, ...},
      "imports": [[from_mod, to_name, local], ...],
      "inherits": [[child_full, parent_full, local], ...],
      "calls": [[caller_full, target_full, local], ...],
      "entrypoints": ["mod.name", ...]
    }
    Catatan: key `symbols` adalah nama pendek (mis. "TaskExecutor.execute"),
    sedangkan relasi memakai nama module-prefixed (mis.
    "src.agent_ai.core.orchestrator.TaskExecutor.execute").

`rig.json` (MAP_CODE_RIG, canonical JSON):
    {
      "schema_version": "rig-json/v1",
      "components": [...], "aggregators": [...], "runners": [...],
      "tests": [...], "external_packages": [...], "package_managers": [...],
      "code_files": [...], "code_modules": [...], "code_classes": [...],
      "code_functions": [...], "code_symbols": [...],
      "edges": [{"id": 1, "type": "invokes", "source": 5, "target": 9}, ...],
      ...
    }
    Entity memakai ID integer; `edges[].source/target` menunjuk integer ID.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_ai.projects.project_map import (
    MAP_TYPE_ATLAS,
    MAP_TYPE_RIG,
    STATUS_STALE,
    ProjectMapError,
    ProjectMapService,
)

#: Default batas hasil (konservatif; aman untuk context LLM).
DEFAULT_MAX_RESULTS = 20
#: Default batas jumlah hubungan (relation) yang ditampilkan per hasil.
DEFAULT_MAX_RELATED = 20
#: Batas keras, apapun yang diminta caller (mencegah kirim map besar).
HARD_MAX_RESULTS = 50

# --------------------------------------------------------------------------- #
# Pesan/hint hasil query (ringkas; hemat token)
# --------------------------------------------------------------------------- #
#: Pesan singkat saat Atlas tidak menemukan match. Menegaskan bahwa ini LOOKUP
#: pada data map/index, BUKAN pencarian full-text isi source.
_ZERO_RESULT_MESSAGE_ATLAS = (
    "Tidak ada symbol/module/file yang cocok dengan query "
    "(lookup Atlas pada map/index, BUKAN pencarian full-text isi source)."
)
#: Pesan singkat saat RIG tidak menemukan match.
_ZERO_RESULT_MESSAGE_RIG = (
    "Tidak ada entity/relationship yang cocok dengan query "
    "(lookup RIG pada map/index, BUKAN pencarian full-text isi source)."
)

#: Hint actionable saat query menghasilkan 0 match (per tool). Ringkas, dan
#: TIDAK mendorong retry/sinonim tanpa batas.
_ZERO_RESULT_HINTS: Dict[str, str] = {
    "atlas_query": (
        "Tidak ditemukan match pada Project Map (Code Atlas). Tool ini melakukan "
        "lookup pada data map/index, bukan pencarian full-text isi source. Jangan "
        "mengulang query yang sama atau mengejar sinonim tanpa batas; bila butuh "
        "isi source, gunakan kemampuan source search pada mode yang "
        "menyediakannya (mis. mode Investigate)."
    ),
    "rig_query": (
        "Tidak ditemukan relationship pada Project Map (RIG). Tool ini melakukan "
        "lookup graph pada data map/index, bukan pencarian full-text isi source. "
        "Pastikan query adalah nama entity yang ter-index + relation yang "
        "didukung; jangan mengulang query yang sama atau mengejar sinonim tanpa "
        "batas."
    ),
}

#: Hint ringkas saat `map_status` = stale. Menegaskan stale BUKAN error dan
#: BUKAN alasan mengulang query/sinonim tanpa batas.
_STALE_HINT = (
    "map_status=stale: map tersedia tetapi mungkin belum merepresentasikan "
    "perubahan source terbaru - perlakukan hasil sebagai lokasi/evidence dengan "
    "keterbatasan, bukan sebagai error, dan bukan alasan untuk mengulang query "
    "yang sama atau mengejar sinonim tanpa batas."
)

#: Kind yang dikenal untuk query Atlas.
ATLAS_KINDS: Tuple[str, ...] = ("symbol", "class", "function", "method", "module", "file")
#: Kind yang dikenal untuk query RIG.
RIG_KINDS: Tuple[str, ...] = (
    "component",
    "aggregator",
    "runner",
    "test",
    "external_package",
    "package_manager",
    "file",
    "module",
    "class",
    "function",
    "symbol",
)


class MapQueryError(ProjectMapError):
    """Error umum saat query map (di luar missing/invalid)."""


class EmptyQueryError(MapQueryError):
    """`query` kosong / hanya whitespace."""


class UnsupportedRelationError(MapQueryError):
    """`relation` yang diminta tidak dikenali."""


# --------------------------------------------------------------------------- #
# RIG relation registry
# --------------------------------------------------------------------------- #
#: Alias relation -> (edge_type atau None untuk semua, arah).
#: arah: "both" | "in" (target == entity) | "out" (source == entity).
_RELATION_SPECS: Dict[str, Tuple[Optional[str], str]] = {
    "related": (None, "both"),
    "all": (None, "both"),
    "any": (None, "both"),
    "invokes": ("invokes", "both"),
    "calls": ("invokes", "both"),
    "callers": ("invokes", "in"),
    "callees": ("invokes", "out"),
    "imports": ("imports", "both"),
    "inherits": ("inherits", "both"),
    "inheritance": ("inherits", "both"),
    "bases": ("inherits", "both"),
    "contains": ("contains", "both"),
    "depends_on": ("depends_on", "both"),
    "dependencies": ("depends_on", "both"),
    "depends": ("depends_on", "both"),
    "tests": ("tests", "both"),
    "external": ("external", "both"),
    "includes": ("includes", "both"),
    "links": ("links", "both"),
}

#: Daftar relation yang didukung (deterministik, untuk pesan error).
SUPPORTED_RELATIONS: Tuple[str, ...] = tuple(sorted(_RELATION_SPECS))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clamp_limit(value: Any, default: int, hard_max: int) -> int:
    """Normalisasi `max_results`/`max_related` ke rentang [1, hard_max]."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < 1:
        number = 1
    if number > hard_max:
        number = hard_max
    return number


def _normalize_kind(kind: Any, allowed: Sequence[str]) -> Optional[str]:
    """Normalisasi `kind`. None/""/"any"/"all" -> None (tanpa filter)."""
    if kind is None:
        return None
    text = str(kind).strip().lower()
    if text in ("", "any", "all", "*"):
        return None
    if text in allowed:
        return text
    raise MapQueryError(
        "kind {!r} tidak dikenal (pilihan: any, {})".format(kind, ", ".join(allowed))
    )


def _require_query(query: Any) -> str:
    """Validasi `query` non-kosong."""
    text = "" if query is None else str(query).strip()
    if not text:
        raise EmptyQueryError("Argumen 'query' wajib diisi dan tidak boleh kosong.")
    return text


def _rank(names: Sequence[str], query: str) -> List[str]:
    """Urutkan kandidat match: exact -> case-insensitive exact -> substring."""
    q = query.lower()
    exact: List[str] = []
    ci: List[str] = []
    sub: List[str] = []
    for name in names:
        if name == query:
            exact.append(name)
        elif name.lower() == q:
            ci.append(name)
        elif q in name.lower():
            sub.append(name)
    return sorted(set(exact)) + sorted(set(ci) - set(exact)) + sorted(
        set(sub) - set(exact) - set(ci)
    )


# --------------------------------------------------------------------------- #
# Atlas query
# --------------------------------------------------------------------------- #
class AtlasMapQuery:
    """Query read-only di atas data `atlas.json` yang sudah di-parse."""

    def __init__(self, data: Dict[str, Any]) -> None:
        data = data if isinstance(data, dict) else {}
        self._data = data
        self._symbols: Dict[str, Dict[str, Any]] = data.get("symbols") or {}
        self._modules: Dict[str, Dict[str, Any]] = data.get("modules") or {}
        self._files: List[str] = [str(f) for f in (data.get("files") or []) if f]
        self._imports: List[List[Any]] = data.get("imports") or []
        self._inherits: List[List[Any]] = data.get("inherits") or []
        self._calls: List[List[Any]] = data.get("calls") or []

        self._file_to_module: Dict[str, str] = {}
        for mod, info in self._modules.items():
            file_path = (info or {}).get("file")
            if file_path:
                self._file_to_module[str(file_path)] = str(mod)

        self._symbol_full: Dict[str, str] = {}
        for short, info in self._symbols.items():
            file_path = str((info or {}).get("file") or "")
            mod = self._file_to_module.get(file_path, "")
            self._symbol_full[str(short)] = "{}.{}".format(mod, short) if mod else str(short)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_project(
        cls, service: ProjectMapService, project_path: Any
    ) -> "AtlasMapQuery":
        """Muat atlas map lewat `ProjectMapService` (read-only)."""
        return cls(service.load_map(project_path, MAP_TYPE_ATLAS))

    # ------------------------------------------------------------------ #
    def _symbol_related(self, short: str, max_related: int) -> Dict[str, Any]:
        full = self._symbol_full.get(short)
        if not full:
            return {}
        related: Dict[str, Any] = {}

        callers = sorted({str(c) for c, t, *_ in self._calls if str(t) == full})
        if callers:
            related["callers"] = callers[:max_related]

        callees = sorted(
            {str(t) for c, t, *_ in self._calls if str(c) == full or str(c).startswith(full + ".")}
        )
        if callees:
            related["callees"] = callees[:max_related]

        parents = sorted({str(p) for child, p, *_ in self._inherits if str(child) == full})
        if parents:
            related["inherits"] = parents[:max_related]

        children = sorted({str(child) for child, p, *_ in self._inherits if str(p) == full})
        if children:
            related["inherited_by"] = children[:max_related]

        return related

    def _module_result(self, mod: str, max_related: int) -> Dict[str, Any]:
        info = self._modules.get(mod) or {}
        result: Dict[str, Any] = {
            "name": mod,
            "kind": "module",
            "file": info.get("file"),
        }
        imports = sorted({str(t) for frm, t, *_ in self._imports if str(frm) == mod})
        if imports:
            result["imports"] = imports[:max_related]
        return result

    # ------------------------------------------------------------------ #
    def query(
        self,
        query: Any,
        kind: Any = None,
        include_relations: bool = True,
        max_results: Any = DEFAULT_MAX_RESULTS,
        max_related: Any = DEFAULT_MAX_RELATED,
    ) -> Dict[str, Any]:
        """Cari symbol/module/file relevan + (opsional) relasinya.

        Semua hasil di-cap `max_results`; relasi per item di-cap `max_related`.
        """
        text = _require_query(query)
        normalized_kind = _normalize_kind(kind, ATLAS_KINDS)
        limit = _clamp_limit(max_results, DEFAULT_MAX_RESULTS, HARD_MAX_RESULTS)
        related_limit = _clamp_limit(max_related, DEFAULT_MAX_RELATED, HARD_MAX_RESULTS)

        results: List[Dict[str, Any]] = []

        if normalized_kind in (None, "symbol", "class", "function", "method"):
            for short in _rank(list(self._symbols), text):
                info = self._symbols.get(short) or {}
                symbol_kind = str(info.get("kind") or "symbol")
                if normalized_kind not in (None, "symbol") and symbol_kind != normalized_kind:
                    continue
                item: Dict[str, Any] = {
                    "name": short,
                    "kind": symbol_kind,
                    "file": info.get("file"),
                    "line_start": info.get("start_line"),
                    "line_end": info.get("end_line"),
                }
                methods = info.get("methods")
                if methods:
                    item["methods"] = list(methods)
                if include_relations:
                    item.update(self._symbol_related(short, related_limit))
                results.append(item)

        if normalized_kind in (None, "module"):
            for mod in _rank(list(self._modules), text):
                results.append(self._module_result(mod, related_limit))

        if normalized_kind in (None, "file"):
            lowered = text.lower()
            for file_path in sorted(f for f in self._files if lowered in f.lower()):
                results.append({"name": file_path, "kind": "file", "file": file_path})

        total = len(results)
        window = results[:limit]
        payload: Dict[str, Any] = {
            "query": text,
            "kind": normalized_kind or "any",
            "total": total,
            "returned": len(window),
            "truncated": total > len(window),
            "results": window,
        }
        if total == 0:
            payload["message"] = _ZERO_RESULT_MESSAGE_ATLAS
        return payload


# --------------------------------------------------------------------------- #
# RIG query
# --------------------------------------------------------------------------- #
#: Mapping key koleksi RIG -> kind entity.
_RIG_COLLECTIONS: Tuple[Tuple[str, str], ...] = (
    ("components", "component"),
    ("aggregators", "aggregator"),
    ("runners", "runner"),
    ("tests", "test"),
    ("external_packages", "external_package"),
    ("package_managers", "package_manager"),
    ("code_files", "file"),
    ("code_modules", "module"),
    ("code_classes", "class"),
    ("code_functions", "function"),
    ("code_symbols", "symbol"),
)


class RigMapQuery:
    """Query read-only di atas data `rig.json` yang sudah di-parse."""

    def __init__(self, data: Dict[str, Any]) -> None:
        data = data if isinstance(data, dict) else {}
        self._data = data
        self._by_id: Dict[Any, Dict[str, Any]] = {}
        self._entities: List[Dict[str, Any]] = []

        for key, kind in _RIG_COLLECTIONS:
            for raw in data.get(key) or []:
                if not isinstance(raw, dict):
                    continue
                entity = dict(raw)
                entity["_kind"] = kind
                self._entities.append(entity)
                if "id" in entity:
                    self._by_id[entity["id"]] = entity

        self._edges: List[Dict[str, Any]] = [
            e for e in (data.get("edges") or []) if isinstance(e, dict)
        ]

        # Index class id -> class name (untuk qualified name method).
        self._class_name: Dict[Any, str] = {}
        for key, _ in _RIG_COLLECTIONS:
            for raw in data.get(key) or []:
                if isinstance(raw, dict) and raw.get("kind") == "class":
                    self._class_name[raw.get("id")] = str(raw.get("name") or "")

        for entity in self._entities:
            entity["_qualified"] = self._qualified_name(entity)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_project(
        cls, service: ProjectMapService, project_path: Any
    ) -> "RigMapQuery":
        """Muat rig map lewat `ProjectMapService` (read-only)."""
        return cls(service.load_map(project_path, MAP_TYPE_RIG))

    # ------------------------------------------------------------------ #
    def _qualified_name(self, entity: Dict[str, Any]) -> str:
        name = str(entity.get("name") or "")
        if entity.get("kind") == "function" and entity.get("is_method"):
            parent = entity.get("parent_class_id")
            parent_name = self._class_name.get(parent) or ""
            if parent_name and name:
                return "{}.{}".format(parent_name, name)
        return name

    def _entity_view(self, entity: Dict[str, Any]) -> Dict[str, Any]:
        view: Dict[str, Any] = {
            "id": entity.get("id"),
            "kind": entity.get("_kind"),
            "name": entity.get("name"),
        }
        qualified = entity.get("_qualified")
        if qualified and qualified != entity.get("name"):
            view["qualified"] = qualified
        file_path = entity.get("file_path")
        if file_path:
            view["file"] = file_path
        if entity.get("line_start") is not None:
            view["line_start"] = entity.get("line_start")
        if entity.get("line_end") is not None:
            view["line_end"] = entity.get("line_end")
        bases = entity.get("bases")
        if bases:
            view["bases"] = list(bases)
        return view

    def _related(
        self, entity: Dict[str, Any], edge_type: Optional[str], direction: str, limit: int
    ) -> List[Dict[str, Any]]:
        eid = entity.get("id")
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for edge in self._edges:
            if edge_type is not None and str(edge.get("type")) != edge_type:
                continue
            source = edge.get("source")
            target = edge.get("target")
            if direction in ("both", "out") and source == eid:
                other = self._by_id.get(target)
                rel_dir = "out"
            elif direction in ("both", "in") and target == eid:
                other = self._by_id.get(source)
                rel_dir = "in"
            else:
                continue
            if other is None:
                continue
            view = self._entity_view(other)
            key = (view.get("id"), str(edge.get("type")), rel_dir)
            if key in seen:
                continue
            seen.add(key)
            view["edge"] = str(edge.get("type"))
            view["direction"] = rel_dir
            role = edge.get("role")
            if role:
                view["role"] = role
            out.append(view)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ #
    def query(
        self,
        query: Any,
        relation: Any = None,
        kind: Any = None,
        max_results: Any = DEFAULT_MAX_RESULTS,
        max_related: Any = DEFAULT_MAX_RELATED,
    ) -> Dict[str, Any]:
        """Cari entity RIG relevan + (opsional) relationship-nya.

        `relation` menerima alias natural (mis. "callers", "callees",
        "imports", "inherits", "contains", "depends_on", "tests", "related").
        """
        text = _require_query(query)
        normalized_kind = _normalize_kind(kind, RIG_KINDS)
        limit = _clamp_limit(max_results, DEFAULT_MAX_RESULTS, HARD_MAX_RESULTS)
        related_limit = _clamp_limit(max_related, DEFAULT_MAX_RELATED, HARD_MAX_RESULTS)

        relation_text = "" if relation is None else str(relation).strip().lower()
        if relation_text in ("", "none", "null"):
            relation_text = "related"
        if relation_text not in _RELATION_SPECS:
            raise UnsupportedRelationError(
                "relation {!r} tidak didukung (pilihan: {})".format(
                    relation, ", ".join(SUPPORTED_RELATIONS)
                )
            )
        edge_type, direction = _RELATION_SPECS[relation_text]

        # Kumpulkan kandidat (dedupe by id) lalu ranking by name/qualified.
        candidates: Dict[Any, Dict[str, Any]] = {}
        for entity in self._entities:
            if normalized_kind is not None and entity.get("_kind") != normalized_kind:
                continue
            candidates[entity.get("id")] = entity

        rank_source: List[str] = []
        for entity in candidates.values():
            name = str(entity.get("name") or "")
            if name:
                rank_source.append(name)
            qualified = entity.get("_qualified")
            if qualified and qualified != name:
                rank_source.append(qualified)
        ordered_names = _rank(rank_source, text)

        results: List[Dict[str, Any]] = []
        seen_ids: set = set()
        lowered = text.lower()
        for wanted in ordered_names:
            for entity in candidates.values():
                name = str(entity.get("name") or "")
                qualified = entity.get("_qualified") or ""
                if wanted not in (name, qualified):
                    continue
                eid = entity.get("id")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                view = self._entity_view(entity)
                if relation_text != "none":
                    related = self._related(entity, edge_type, direction, related_limit)
                    if related:
                        view["related"] = related
                results.append(view)

        # Bila tidak ada match nama, cari entity berdasarkan potongan file_path
        # (khusus file/module) supaya `rig_query("pkg/core.py")` tetap berguna.
        if not results and normalized_kind in (None, "file", "module"):
            for entity in candidates.values():
                file_path = str(entity.get("file_path") or "")
                if file_path and lowered in file_path.lower():
                    if entity.get("id") in seen_ids:
                        continue
                    seen_ids.add(entity.get("id"))
                    results.append(self._entity_view(entity))

        total = len(results)
        window = results[:limit]
        payload: Dict[str, Any] = {
            "query": text,
            "kind": normalized_kind or "any",
            "relation": relation_text,
            "total": total,
            "returned": len(window),
            "truncated": total > len(window),
            "results": window,
        }
        if total == 0:
            payload["message"] = _ZERO_RESULT_MESSAGE_RIG
        return payload


# --------------------------------------------------------------------------- #
# Convenience wrappers (dipakai tool layer / Task 3)
# --------------------------------------------------------------------------- #
def _map_freshness(service: ProjectMapService, project_path: Any, map_type: str) -> str:
    """Freshness map untuk dilampirkan pada hasil query (TIDAK raise).

    Query tetap boleh membaca map lama; nilai ini hanya memberi tahu LLM apakah
    map yang dibaca sudah `fresh` atau `stale`. Konservatif: bila freshness
    tidak dapat ditentukan, dianggap `stale` (bukan gagal).
    """
    try:
        return service.get_freshness(project_path, map_type)
    except Exception:  # noqa: BLE001 - status tidak boleh menggagalkan query
        return STATUS_STALE


def _result_hint(total: Any, map_status: str, tool_name: str) -> Optional[str]:
    """Hint ringkas & actionable untuk hasil query (None bila tak perlu).

    Ditambahkan HANYA ketika ada sinyal yang perlu ditindaklanjuti LLM:
        - ``total == 0``          -> tidak ada match pada data map/index;
        - ``map_status == stale`` -> map mungkin belum merepresentasikan source.

    Hasil normal pada map fresh TIDAK diberi hint (agar output hemat token).
    Hint tidak pernah mendorong retry/sinonim tanpa batas.
    """
    try:
        zero = int(total) == 0
    except (TypeError, ValueError):
        zero = False

    parts: List[str] = []
    if zero:
        parts.append(_ZERO_RESULT_HINTS.get(tool_name, ""))
    if map_status == STATUS_STALE:
        parts.append(_STALE_HINT)
    hint = " ".join(part for part in parts if part)
    return hint or None


def atlas_query(
    service: ProjectMapService,
    project_path: Any,
    query: Any,
    kind: Any = None,
    include_relations: bool = True,
    max_results: Any = DEFAULT_MAX_RESULTS,
    max_related: Any = DEFAULT_MAX_RELATED,
) -> Dict[str, Any]:
    """Query atlas map project (read-only, hasil kecil).

    Hasil menyertakan `map_status` (`fresh`/`stale`) sehingga LLM tahu apakah
    map yang dibaca masih merepresentasikan source terbaru. Query TIDAK pernah
    meregenerasi map.

    Bila ``total == 0`` dan/atau ``map_status == stale``, hasil menambahkan
    field ``hint`` ringkas yang menjelaskan arti kondisi tersebut (lookup
    map/index bukan full-text; stale bukan error) TANPA mendorong pencarian
    berulang.
    """
    payload = AtlasMapQuery.from_project(service, project_path).query(
        query,
        kind=kind,
        include_relations=include_relations,
        max_results=max_results,
        max_related=max_related,
    )
    map_status = _map_freshness(service, project_path, MAP_TYPE_ATLAS)
    payload["map_status"] = map_status
    hint = _result_hint(payload.get("total"), map_status, "atlas_query")
    if hint:
        payload["hint"] = hint
    return payload


def rig_query(
    service: ProjectMapService,
    project_path: Any,
    query: Any,
    relation: Any = None,
    kind: Any = None,
    max_results: Any = DEFAULT_MAX_RESULTS,
    max_related: Any = DEFAULT_MAX_RELATED,
) -> Dict[str, Any]:
    """Query rig map project (read-only, hasil kecil).

    Hasil menyertakan `map_status` (`fresh`/`stale`). Query TIDAK pernah
    meregenerasi map.

    Bila ``total == 0`` dan/atau ``map_status == stale``, hasil menambahkan
    field ``hint`` ringkas (lookup graph bukan full-text; stale bukan error)
    TANPA mendorong pencarian berulang.
    """
    payload = RigMapQuery.from_project(service, project_path).query(
        query,
        relation=relation,
        kind=kind,
        max_results=max_results,
        max_related=max_related,
    )
    map_status = _map_freshness(service, project_path, MAP_TYPE_RIG)
    payload["map_status"] = map_status
    hint = _result_hint(payload.get("total"), map_status, "rig_query")
    if hint:
        payload["hint"] = hint
    return payload


__all__ = [
    "AtlasMapQuery",
    "RigMapQuery",
    "MapQueryError",
    "EmptyQueryError",
    "UnsupportedRelationError",
    "atlas_query",
    "rig_query",
    "ATLAS_KINDS",
    "RIG_KINDS",
    "SUPPORTED_RELATIONS",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_MAX_RELATED",
    "HARD_MAX_RESULTS",
]
