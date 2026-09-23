"""Verifikasi query capability Project Map (Atlas + RIG).

Membuat fixture project kecil di `dummy_test/project_map_query_fixture`
(workspace testing terisolasi, BUKAN bagian source AETHER), lalu memverifikasi
bahwa `atlas_query`, `rig_query`, dan `project_map_status`:

    - hanya membaca map yang sudah tersimpan (TIDAK regenerate map);
    - mengembalikan SUBSET kecil yang relevan (bukan seluruh map);
    - menerapkan limit hasil (max_results) + indikator truncated;
    - menangani map missing / invalid JSON / query kosong / entity tidak ada /
      relation tidak didukung dengan error ringkas.

Fixture map ditulis tangan (hand-crafted) mengikuti skema ASLI:
    - atlas.json : skema output `atlas.py` (CODE ATLAS)
    - rig.json   : skema canonical `map_code_rig` (RIG, ID integer)

Selain itu ada bagian (best-effort) yang meregenerasi atlas map untuk fixture
kode nyata memakai engine Atlas yang sudah ada (di-SKIP bila engine tidak ada).

Jalankan:
    python scripts/check_project_map_query.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.projects import (  # noqa: E402
    EmptyQueryError,
    HARD_MAX_RESULTS,
    MapQueryError,
    ProjectMapService,
    UnsupportedRelationError,
)
from agent_ai.projects.project_map_query import (  # noqa: E402
    AtlasMapQuery,
    RigMapQuery,
    atlas_query,
    rig_query,
)
from agent_ai.tools.base import ToolValidationError  # noqa: E402
from agent_ai.tools.project_map import build_project_map_registry  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "project_map_query_fixture"
FIXTURE_EMPTY = DUMMY_ROOT / "project_map_query_empty"
FIXTURE_BROKEN = DUMMY_ROOT / "project_map_query_broken"
FIXTURE_REAL = DUMMY_ROOT / "project_map_query_real"

_BULK_COUNT = 60


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _atlas_payload() -> dict:
    symbols = {
        "TaskExecutor": {
            "kind": "class",
            "file": "pkg/core.py",
            "start_line": 10,
            "end_line": 60,
            "methods": ["execute", "run"],
        },
        "TaskExecutor.execute": {
            "kind": "method",
            "file": "pkg/core.py",
            "start_line": 20,
            "end_line": 40,
        },
        "TaskExecutor.run": {
            "kind": "method",
            "file": "pkg/core.py",
            "start_line": 42,
            "end_line": 58,
        },
        "AgentRuntime": {
            "kind": "class",
            "file": "pkg/app.py",
            "start_line": 5,
            "end_line": 30,
            "methods": ["run"],
        },
        "AgentRuntime.run": {
            "kind": "method",
            "file": "pkg/app.py",
            "start_line": 8,
            "end_line": 20,
        },
        "load_config": {"kind": "function", "file": "pkg/util.py", "start_line": 3, "end_line": 9},
    }
    modules = {
        "pkg.core": {"file": "pkg/core.py"},
        "pkg.app": {"file": "pkg/app.py"},
        "pkg.util": {"file": "pkg/util.py"},
    }
    imports = [
        ["pkg.app", "pkg.core", True],
        ["pkg.core", "pkg.util", True],
    ]
    inherits = [["pkg.core.TaskExecutor", "pkg.app.BaseRuntime", True]]
    calls = [
        ["pkg.app.AgentRuntime.run", "pkg.core.TaskExecutor.execute", True],
        ["pkg.core.TaskExecutor.execute", "pkg.core.helper", True],
        ["pkg.core.TaskExecutor.execute", "pkg.core.TaskExecutor.run", True],
    ]
    # Bulk symbols supaya uji limit punya banyak hasil nyata.
    for index in range(_BULK_COUNT):
        symbols["Bulk{0:03d}".format(index)] = {
            "kind": "class",
            "file": "pkg/bulk.py",
            "start_line": index + 1,
            "end_line": index + 2,
            "methods": [],
        }
    modules["pkg.bulk"] = {"file": "pkg/bulk.py"}
    files = [
        "pkg/core.py",
        "pkg/app.py",
        "pkg/util.py",
        "pkg/bulk.py",
        "README.md",
    ]
    return {
        "version": 1,
        "project": {"name": "fixture", "language": "python"},
        "files": files,
        "modules": modules,
        "symbols": symbols,
        "imports": imports,
        "inherits": inherits,
        "calls": calls,
        "entrypoints": ["pkg.app"],
    }


def _rig_payload() -> dict:
    classes = [
        {
            "id": 3,
            "kind": "class",
            "name": "TaskExecutor",
            "file_path": "pkg/core.py",
            "line_start": 10,
            "line_end": 60,
            "bases": ["BaseRuntime"],
        },
        {
            "id": 4,
            "kind": "class",
            "name": "AgentRuntime",
            "file_path": "pkg/app.py",
            "line_start": 5,
            "line_end": 30,
            "bases": [],
        },
    ]
    for index in range(_BULK_COUNT):
        classes.append(
            {
                "id": 100 + index,
                "kind": "class",
                "name": "Bulk{0:03d}".format(index),
                "file_path": "pkg/bulk.py",
                "line_start": index + 1,
                "line_end": index + 2,
                "bases": [],
            }
        )
    functions = [
        {
            "id": 5,
            "kind": "function",
            "name": "execute",
            "file_path": "pkg/core.py",
            "line_start": 20,
            "line_end": 40,
            "is_method": True,
            "parent_class_id": 3,
        },
        {
            "id": 6,
            "kind": "function",
            "name": "run",
            "file_path": "pkg/app.py",
            "line_start": 8,
            "line_end": 20,
            "is_method": True,
            "parent_class_id": 4,
        },
        {
            "id": 7,
            "kind": "function",
            "name": "helper",
            "file_path": "pkg/core.py",
            "line_start": 62,
            "line_end": 70,
            "is_method": False,
        },
    ]
    return {
        "schema_version": "rig-json/v1",
        "generator": {"name": "map_code_rig", "version": "1.0.0"},
        "repo": {"name": "fixture", "primary_language": "python", "languages": ["python"]},
        "build": {"profiles": [], "primary_profile_id": None, "evidence_ids": []},
        "components": [],
        "aggregators": [],
        "runners": [],
        "tests": [],
        "external_packages": [],
        "package_managers": [],
        "code_files": [
            {
                "id": 1,
                "kind": "file",
                "name": "pkg/core.py",
                "file_path": "pkg/core.py",
                "language": "python",
                "parse_success": True,
            }
        ],
        "code_modules": [
            {"id": 2, "kind": "module", "name": "pkg.core", "file_path": "pkg/core.py", "is_package": False},
            {"id": 8, "kind": "module", "name": "pkg.util", "file_path": "pkg/util.py", "is_package": False},
        ],
        "code_classes": classes,
        "code_functions": functions,
        "code_symbols": [],
        "edges": [
            {"id": 10, "type": "invokes", "source": 6, "target": 5},
            {"id": 11, "type": "invokes", "source": 5, "target": 7},
            {"id": 12, "type": "inherits", "source": 3, "target": 4},
            {"id": 13, "type": "contains", "source": 3, "target": 5},
            {"id": 14, "type": "imports", "source": 2, "target": 8},
            {"id": 15, "type": "depends_on", "source": 2, "target": 8},
        ],
        "evidence": [],
        "diagnostics": [],
        "unresolved_references": [],
    }


def _write_maps(map_dir: Path, atlas: dict | None, rig: dict | str | None) -> None:
    map_dir.mkdir(parents=True, exist_ok=True)
    if atlas is not None:
        (map_dir / "atlas.json").write_text(json.dumps(atlas), encoding="utf-8")
    if rig is not None:
        if isinstance(rig, str):
            (map_dir / "rig.json").write_text(rig, encoding="utf-8")
        else:
            (map_dir / "rig.json").write_text(json.dumps(rig), encoding="utf-8")


def setup_fixtures() -> None:
    teardown_fixtures()
    map_dir = FIXTURE / ".aether" / "map"
    _write_maps(map_dir, _atlas_payload(), _rig_payload())

    FIXTURE_EMPTY.mkdir(parents=True, exist_ok=True)

    _write_maps(FIXTURE_BROKEN / ".aether" / "map", _atlas_payload(), "{ ini bukan json")

    # Fixture kode nyata kecil (untuk best-effort engine Atlas).
    (FIXTURE_REAL / "pkg").mkdir(parents=True, exist_ok=True)
    (FIXTURE_REAL / "pyproject.toml").write_text(
        '[project]\nname = "fixture_real"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (FIXTURE_REAL / "pkg" / "__init__.py").write_text(
        "from pkg.core import Widget\n", encoding="utf-8"
    )
    (FIXTURE_REAL / "pkg" / "core.py").write_text(
        "class Widget:\n"
        "    def render(self):\n"
        "        return 'w'\n"
        "\n"
        "def build():\n"
        "    return Widget()\n",
        encoding="utf-8",
    )


def teardown_fixtures() -> None:
    for path in (FIXTURE, FIXTURE_EMPTY, FIXTURE_BROKEN, FIXTURE_REAL):
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_atlas() -> None:
    print("-- Atlas query --")
    service = ProjectMapService()
    data = service.load_map(FIXTURE, "atlas")

    # (1) Symbol yang ada: hasil kecil + lokasi + relasi.
    result = atlas_query(service, FIXTURE, "TaskExecutor.execute")
    names = [item["name"] for item in result["results"]]
    _expect("TaskExecutor.execute" in names, "symbol TaskExecutor.execute harus ditemukan")
    target = next(i for i in result["results"] if i["name"] == "TaskExecutor.execute")
    _expect(target["file"] == "pkg/core.py", "file symbol harus benar")
    _expect(target["line_start"] == 20 and target["line_end"] == 40, "rentang baris harus benar")
    _expect(
        "pkg.app.AgentRuntime.run" in target.get("callers", []),
        "callers symbol harus memuat pemanggil dari relasi calls",
    )
    print("(1) query symbol yang ada (lokasi + callers) : OK -> {}".format(target["name"]))

    # (2) Module + imports.
    module_result = atlas_query(service, FIXTURE, "pkg.core", kind="module")
    _expect(len(module_result["results"]) == 1, "query module harus mengembalikan 1 hasil")
    _expect(module_result["results"][0]["kind"] == "module", "kind harus module")
    _expect("pkg.util" in module_result["results"][0].get("imports", []), "imports module harus ada")
    print("(2) query module + imports : OK")

    # (3) File.
    file_result = atlas_query(service, FIXTURE, "core.py", kind="file")
    _expect(
        any(i["kind"] == "file" and i["file"] == "pkg/core.py" for i in file_result["results"]),
        "query file harus menemukan pkg/core.py",
    )
    print("(3) query file : OK")

    # (4) Symbol tidak ditemukan -> hasil kosong + pesan jelas (bukan error).
    missing = atlas_query(service, FIXTURE, "TidakAdaSymbolIni")
    _expect(missing["total"] == 0 and missing["results"] == [], "query tak ada harus kosong")
    _expect(bool(missing.get("message")), "query tak ada harus memberi pesan")
    print("(4) symbol tidak ditemukan -> kosong + pesan : OK")

    # (5) Hasil HANYA subset relevan (bukan seluruh map).
    payload_keys = set(result.keys())
    _expect(
        payload_keys
        <= {"query", "kind", "total", "returned", "truncated", "results", "message", "map_status", "hint"},
        "payload query tidak boleh memuat dump map; keys={}".format(sorted(payload_keys)),
    )
    _expect(
        result.get("map_status") in {"fresh", "stale"},
        "payload query harus menyertakan map_status (fresh/stale)",
    )
    for forbidden in ("symbols", "modules", "files", "calls", "imports", "inherits", "entrypoints"):
        _expect(forbidden not in payload_keys, "key map '{}' tidak boleh ada di payload".format(forbidden))
    _expect(
        len(result["results"]) < len(data["symbols"]),
        "hasil harus lebih sedikit dari seluruh symbols",
    )
    _expect(
        len(json.dumps(result)) < len(json.dumps(data)),
        "payload query harus jauh lebih kecil dari map penuh",
    )
    print(
        "(5) hasil = subset relevan ({} item vs {} symbols) : OK".format(
            len(result["results"]), len(data["symbols"])
        )
    )

    # (6) Limit hasil + truncated.
    limited = atlas_query(
        service, FIXTURE, "Bulk", kind="class", max_results=5, include_relations=False
    )
    _expect(limited["total"] == _BULK_COUNT, "total harus {} (tanpa limit)".format(_BULK_COUNT))
    _expect(limited["returned"] == 5, "returned harus dibatasi 5")
    _expect(limited["truncated"] is True, "truncated harus True")
    print("(6) limit max_results=5 -> total={} returned={} truncated={} : OK".format(
        limited["total"], limited["returned"], limited["truncated"]
    ))

    # (7) Permintaan limit berlebihan di-clamp ke batas keras.
    clamped = atlas_query(
        service, FIXTURE, "Bulk", kind="class", max_results=100000, include_relations=False
    )
    _expect(clamped["returned"] <= HARD_MAX_RESULTS, "returned harus <= HARD_MAX_RESULTS")
    _expect(clamped["returned"] == HARD_MAX_RESULTS, "limit ekstrem harus di-clamp")
    print("(7) max_results ekstrem di-clamp ke {} : OK".format(HARD_MAX_RESULTS))

    # (8) Relation per item dibatasi max_related.
    rel = atlas_query(service, FIXTURE, "TaskExecutor.execute", max_related=1)
    target = rel["results"][0]
    for key in ("callers", "callees", "inherits", "inherited_by"):
        _expect(len(target.get(key, [])) <= 1, "relasi '{}' harus <= max_related".format(key))
    print("(8) max_related membatasi relasi per item : OK")

    # (9) Query kosong + kind aneh.
    for bad in ("", "   "):
        try:
            atlas_query(service, FIXTURE, bad)
        except EmptyQueryError:
            pass
        else:
            raise AssertionError("query kosong harus raise EmptyQueryError")
    try:
        atlas_query(service, FIXTURE, "TaskExecutor", kind="banana")
    except MapQueryError:
        pass
    else:
        raise AssertionError("kind tidak dikenal harus raise MapQueryError")
    print("(9) query kosong + kind tidak dikenal ditolak : OK")

    # (10) Adapter langsung (tanpa service) tetap bekerja pada data ter-parse.
    direct = AtlasMapQuery(data).query("AgentRuntime.run")
    _expect(direct["total"] >= 1, "adapter langsung harus bekerja")
    print("(10) AtlasMapQuery(data).query() : OK")

    # (11) Konsep yang TIDAK ter-index (mis. 'safeguard') -> 0 hasil + sinyal jelas
    #      bahwa Atlas adalah lookup map/index, BUKAN pencarian full-text source.
    concept = atlas_query(service, FIXTURE, "safeguard")
    _expect(concept["total"] == 0 and concept["results"] == [], "query konsep harus 0 hasil")
    _expect(bool(concept.get("message")), "0 hasil harus memberi message")
    msg = str(concept.get("message", "")).lower()
    _expect(
        "full-text" in msg,
        "message 0 hasil harus menyatakan Atlas BUKAN full-text source",
    )
    hint = str(concept.get("hint", ""))
    _expect(bool(hint), "0 hasil harus memberi hint actionable")
    low = hint.lower()
    _expect("tidak ditemukan" in low, "hint harus menyatakan tidak ditemukan di Project Map")
    _expect("full-text" in low, "hint harus menegaskan bukan full-text source")
    _expect("mengulang" in low, "hint harus mencegah retry/sinonim tanpa batas")
    _expect("safeguard" not in low, "hint tidak boleh menyuruh mencari sinonim 'safeguard'")
    print("(11) konsep tak ter-index ('safeguard') -> 0 hasil + sinyal bukan full-text : OK")


def check_rig() -> None:
    print()
    print("-- RIG query --")
    service = ProjectMapService()
    data = service.load_map(FIXTURE, "rig")

    # (1) Entity yang ada (class).
    result = rig_query(service, FIXTURE, "TaskExecutor")
    names = [i.get("name") for i in result["results"]]
    _expect("TaskExecutor" in names, "class TaskExecutor harus ditemukan")
    target = next(i for i in result["results"] if i.get("name") == "TaskExecutor")
    _expect(target["file"] == "pkg/core.py", "file entity harus benar")
    _expect(target["line_start"] == 10 and target["line_end"] == 60, "rentang baris harus benar")
    _expect(target.get("bases") == ["BaseRuntime"], "bases class harus ikut")
    print("(1) query entity yang ada (class) : OK")

    # (2) Method lewat qualified name (parent_class.method).
    method = rig_query(service, FIXTURE, "TaskExecutor.execute")
    _expect(method["total"] >= 1, "method qualified harus ditemukan")
    method_item = next(i for i in method["results"] if i.get("qualified") == "TaskExecutor.execute")
    _expect(method_item["kind"] == "function", "method adalah entity function")
    print("(2) query method lewat qualified name : OK")

    # (3) relation=callers (invokes inbound).
    callers = rig_query(service, FIXTURE, "TaskExecutor.execute", relation="callers")
    item = next(i for i in callers["results"] if i.get("qualified") == "TaskExecutor.execute")
    rel = item.get("related", [])
    _expect(len(rel) == 1, "harus ada tepat 1 caller")
    _expect(rel[0].get("qualified") == "AgentRuntime.run", "caller harus AgentRuntime.run")
    _expect(rel[0].get("edge") == "invokes" and rel[0].get("direction") == "in", "edge/direction callers")
    print("(3) relation=callers -> {} : OK".format(rel[0].get("qualified")))

    # (4) relation=callees (invokes outbound).
    callees = rig_query(service, FIXTURE, "TaskExecutor.execute", relation="callees")
    item = next(i for i in callees["results"] if i.get("qualified") == "TaskExecutor.execute")
    names = [r.get("name") for r in item.get("related", [])]
    _expect("helper" in names, "callees harus memuat helper")
    print("(4) relation=callees -> {} : OK".format(names))

    # (5) relation=inherits.
    inherits = rig_query(service, FIXTURE, "TaskExecutor", relation="inherits")
    item = next(i for i in inherits["results"] if i.get("name") == "TaskExecutor")
    _expect(
        any(r.get("name") == "AgentRuntime" for r in item.get("related", [])),
        "inherits harus memuat AgentRuntime",
    )
    print("(5) relation=inherits : OK")

    # (6) relation=imports (module).
    imports = rig_query(service, FIXTURE, "pkg.core", relation="imports")
    item = next(i for i in imports["results"] if i.get("name") == "pkg.core")
    _expect(
        any(r.get("name") == "pkg.util" for r in item.get("related", [])),
        "imports harus memuat pkg.util",
    )
    print("(6) relation=imports : OK")

    # (7) relation tidak didukung -> error yang menyebut pilihan.
    try:
        rig_query(service, FIXTURE, "TaskExecutor", relation="teleports")
    except UnsupportedRelationError as exc:
        _expect("callers" in str(exc), "pesan error harus menyebut relation yang didukung")
        print("(7) relation tidak didukung ditolak dengan pesan jelas : OK")
    else:
        raise AssertionError("relation tidak didukung harus raise UnsupportedRelationError")

    # (8) Entity tidak ditemukan.
    missing = rig_query(service, FIXTURE, "TidakAdaEntityIni")
    _expect(missing["total"] == 0 and missing["results"] == [], "query tak ada harus kosong")
    _expect(bool(missing.get("message")), "query tak ada harus memberi pesan")
    print("(8) entity tidak ditemukan -> kosong + pesan : OK")

    # (9) Limit + truncated.
    limited = rig_query(service, FIXTURE, "Bulk", relation="related", max_results=5)
    _expect(limited["total"] == _BULK_COUNT, "total harus {}".format(_BULK_COUNT))
    _expect(limited["returned"] == 5 and limited["truncated"] is True, "limit harus berlaku")
    print("(9) limit max_results=5 pada RIG : OK")

    # (10) Tidak ada dump graph.
    keys = set(result.keys())
    _expect(
        keys
        <= {"query", "kind", "relation", "total", "returned", "truncated", "results", "message", "map_status", "hint"},
        "payload RIG tidak boleh memuat dump graph; keys={}".format(sorted(keys)),
    )
    _expect(
        result.get("map_status") in {"fresh", "stale"},
        "payload RIG harus menyertakan map_status (fresh/stale)",
    )
    for forbidden in ("edges", "code_classes", "code_functions", "components", "evidence"):
        _expect(forbidden not in keys, "key graph '{}' tidak boleh ada".format(forbidden))
    _expect(
        len(json.dumps(result)) < len(json.dumps(data)),
        "payload RIG harus lebih kecil dari map penuh",
    )
    print("(10) hasil RIG = subset (bukan edges/nodes penuh) : OK")

    # (11) Adapter langsung.
    direct = RigMapQuery(data).query("AgentRuntime.run", relation="callers")
    _expect(direct["total"] >= 1, "adapter RIG langsung harus bekerja")
    print("(11) RigMapQuery(data).query() : OK")

    # (12) RIG tanpa match -> 0 hasil + penjelasan lookup graph (callers/callees/
    #      relationship), BUKAN pencarian full-text isi source.
    none = rig_query(service, FIXTURE, "safeguard")
    _expect(none["total"] == 0 and none["results"] == [], "query RIG tak ada harus 0")
    _expect(bool(none.get("message")), "0 hasil RIG harus memberi message")
    _expect(
        "full-text" in str(none.get("message", "")).lower(),
        "message RIG 0 hasil harus menyatakan bukan full-text",
    )
    rhint = str(none.get("hint", ""))
    _expect(bool(rhint), "0 hasil RIG harus memberi hint")
    rlow = rhint.lower()
    _expect(
        "relationship" in rlow,
        "hint RIG harus menyatakan relationship/graph lookup tidak ditemukan",
    )
    _expect("full-text" in rlow, "hint RIG harus menyatakan bukan full-text source")
    _expect("mengulang" in rlow, "hint RIG harus mencegah retry/sinonim tanpa batas")
    print("(12) RIG tanpa match -> 0 hasil + penjelasan graph lookup : OK")


def check_status() -> None:
    print()
    print("-- Status --")
    registry = build_project_map_registry(root=FIXTURE)

    status = registry.execute("project_map_status", {})
    _expect(status["atlas"] == "available", "atlas harus available")
    _expect(status["rig"] == "available", "rig harus available")
    _expect(status["overall"] == "available", "overall harus available")
    _expect("missing" not in status, "tidak boleh ada map missing")
    print("(1) atlas+rig available : OK -> {}".format(
        {"atlas": status["atlas"], "rig": status["rig"]}
    ))

    empty_registry = build_project_map_registry(root=FIXTURE_EMPTY)
    empty_status = empty_registry.execute("project_map_status", {})
    _expect(empty_status["atlas"] == "missing", "atlas harus missing")
    _expect(empty_status["rig"] == "missing", "rig harus missing")
    _expect(empty_status["overall"] == "missing", "overall harus missing")
    print("(2) map missing terdeteksi : OK")

    broken_registry = build_project_map_registry(root=FIXTURE_BROKEN)
    broken_status = broken_registry.execute("project_map_status", {})
    _expect(broken_status["rig"] == "invalid", "rig rusak harus invalid")
    _expect(broken_status["overall"] == "invalid", "overall harus invalid")
    _expect(bool(broken_status.get("errors")), "status invalid harus memberi error ringkas")
    print("(3) invalid JSON terdeteksi : OK")

    # Status tidak memuat isi map.
    for forbidden in ("maps_dump", "symbols", "edges", "modules"):
        _expect(forbidden not in status, "status tidak boleh memuat isi map")
    print("(4) status tidak memuat isi map : OK")


def check_tools() -> None:
    print()
    print("-- Tool integration --")

    registry = build_project_map_registry(root=FIXTURE)
    _expect(
        registry.list() == ["atlas_query", "project_map_status", "rig_query"],
        "registry harus memuat tiga capability",
    )
    print("(1) build_project_map_registry berisi 3 tool : OK")

    result = registry.execute(
        "atlas_query", {"query": "Bulk", "kind": "class", "max_results": 3}
    )
    _expect(result["returned"] == 3, "tool harus menerapkan max_results")
    _expect(result["truncated"] is True, "tool harus menandai truncated")
    print("(2) atlas_query via ToolRegistry : OK")

    result = registry.execute("rig_query", {"query": "TaskExecutor.execute", "relation": "callers"})
    _expect(result["total"] >= 1, "rig_query via ToolRegistry harus menemukan entity")
    print("(3) rig_query via ToolRegistry : OK")

    try:
        registry.execute("atlas_query", {})
    except ToolValidationError:
        print("(4) argumen wajib 'query' divalidasi : OK")
    else:
        raise AssertionError("atlas_query tanpa 'query' harus ToolValidationError")

    try:
        registry.execute("rig_query", {"query": "TaskExecutor", "relation": "teleports"})
    except ToolValidationError as exc:
        _expect("teleports" in str(exc), "pesan error harus menyebut relation yang salah")
        print("(5) relation tidak didukung -> ToolValidationError ringkas : OK")
    else:
        raise AssertionError("relation tidak didukung harus ToolValidationError")

    # Map missing -> error jelas (bukan traceback) DAN tidak meregenerasi map.
    missing_registry = build_project_map_registry(root=FIXTURE_EMPTY)
    for name, args in (("atlas_query", {"query": "x"}), ("rig_query", {"query": "x"})):
        try:
            missing_registry.execute(name, args)
        except ToolValidationError as exc:
            _expect("belum tersedia" in str(exc), "pesan harus menyebut map belum tersedia")
        else:
            raise AssertionError("{} pada map missing harus ToolValidationError".format(name))
    _expect(
        not (FIXTURE_EMPTY / ".aether").exists(),
        "query TIDAK boleh membuat/meregenerasi .aether/map",
    )
    print("(6) map missing -> ToolValidationError pesan jelas, tanpa regenerate : OK")

    # Invalid JSON -> error jelas.
    broken_registry = build_project_map_registry(root=FIXTURE_BROKEN)
    try:
        broken_registry.execute("rig_query", {"query": "x"})
    except ToolValidationError as exc:
        _expect("bukan JSON valid" in str(exc), "pesan invalid JSON harus jelas")
    else:
        raise AssertionError("rig_query pada map invalid harus ToolValidationError")
    print("(7) invalid JSON -> ToolValidationError pesan jelas : OK")

    # Wiring (Task 3): registry Agent memuat capability Project Map, termasuk
    # refresh_project_map. Detail Agent vs Consultant diuji di
    # scripts/check_project_map_integration.py.
    from agent_ai.tools.registry import build_registry

    agent_tools = set(build_registry(root=FIXTURE).list())
    for name in (
        "atlas_query",
        "rig_query",
        "project_map_status",
        "refresh_project_map",
    ):
        _expect(
            name in agent_tools,
            "tool '{}' harus terdaftar di registry Agent".format(name),
        )
    print("(8) registry Agent memuat capability Project Map (termasuk refresh) : OK")


def check_engine_best_effort() -> None:
    print()
    print("-- Engine Atlas (best-effort) --")
    service = ProjectMapService()
    if not service.engine_available("atlas"):
        print("[SKIP] engine Atlas tidak tersedia di {}".format(service.get_engine_dir("atlas")))
        return
    try:
        service.generate_map(FIXTURE_REAL, "atlas")
    except Exception as exc:  # noqa: BLE001 - engine eksternal
        print("[WARN] engine Atlas gagal: {}".format(exc))
        return
    result = atlas_query(service, FIXTURE_REAL, "Widget", include_relations=True)
    _expect(result["total"] >= 1, "query pada atlas hasil engine harus menemukan Widget")
    payload_size = len(json.dumps(result))
    map_size = service.get_map_path(FIXTURE_REAL, "atlas").stat().st_size
    _expect(payload_size < map_size, "payload harus lebih kecil dari map hasil engine")
    print(
        "[OK] atlas nyata: query 'Widget' -> {} hasil (payload {} bytes vs map {} bytes)".format(
            result["returned"], payload_size, map_size
        )
    )


def check_result_hints() -> None:
    print()
    print("-- Hint hasil (stale & backward compatibility) --")
    service = ProjectMapService()

    # (1) map_status stale TETAP terlihat + hint ringkas (BUKAN error, bukan
    #     dorongan retry tanpa batas). Fixture tulisan-tangan tanpa meta.json
    #     selalu stale.
    stale = atlas_query(service, FIXTURE, "TaskExecutor")
    _expect(stale["total"] >= 1, "query fixture harus punya hasil")
    _expect("map_status" in stale, "map_status tidak boleh disembunyikan")
    _expect(stale["map_status"] == "stale", "fixture tanpa meta harus stale")
    hint = str(stale.get("hint", ""))
    _expect(bool(hint), "map stale harus memberi hint")
    low = hint.lower()
    _expect("stale" in low, "hint harus menyebut stale")
    _expect(
        "bukan sebagai error" in low or "bukan error" in low,
        "hint harus menegaskan stale BUKAN error",
    )
    _expect("keterbatasan" in low, "hint harus menyatakan hasil berketerbatasan")
    _expect(
        "mengulang" in low,
        "hint harus melarang mengulang query/sinonim tanpa batas",
    )
    print("(1) map_status stale terlihat + hint keterbatasan (bukan error) : OK")

    rig_stale = rig_query(service, FIXTURE, "TaskExecutor")
    _expect(rig_stale["map_status"] == "stale", "rig fixture harus stale")
    _expect(bool(rig_stale.get("hint")), "rig stale harus memberi hint")
    print("(2) rig_query stale juga memberi hint : OK")

    # (3) Backward compatibility: field existing tetap ada + invariant konsisten.
    payloads = (
        ("atlas_query", atlas_query(service, FIXTURE, "TaskExecutor.execute")),
        ("rig_query", rig_query(service, FIXTURE, "TaskExecutor")),
        ("atlas_query(0)", atlas_query(service, FIXTURE, "safeguard")),
        ("rig_query(0)", rig_query(service, FIXTURE, "safeguard")),
    )
    for label, payload in payloads:
        for key in ("query", "kind", "total", "returned", "truncated", "results", "map_status"):
            _expect(key in payload, "{}: field '{}' harus tetap ada".format(label, key))
        _expect(isinstance(payload["results"], list), "{}: results harus list".format(label))
        _expect(
            payload["returned"] == len(payload["results"]),
            "{}: returned harus == len(results)".format(label),
        )
        _expect(
            bool(payload["truncated"]) is (payload["total"] > payload["returned"]),
            "{}: truncated harus konsisten dengan total/returned".format(label),
        )
        _expect(
            not payload.get("hint") or isinstance(payload["hint"], str),
            "{}: hint bila ada harus string ringkas".format(label),
        )
    print("(3) struktur result tetap backward compatible : OK")

    # (4) Adapter langsung (tanpa wrapper) tidak menambah map_status/hint:
    #     kontrak lama tetap berlaku, hanya `message` yang diperjelas.
    direct = AtlasMapQuery(service.load_map(FIXTURE, "atlas")).query("safeguard")
    _expect(direct["total"] == 0, "adapter langsung tetap 0 hasil")
    _expect(bool(direct.get("message")), "adapter langsung tetap memberi message")
    _expect("map_status" not in direct, "adapter langsung tidak menambah map_status")
    _expect("hint" not in direct, "adapter langsung tidak menambah hint")
    print("(4) adapter langsung tetap kompatibel (tanpa field baru) : OK")


def _run() -> int:
    check_atlas()
    check_rig()
    check_status()
    check_tools()
    check_result_hints()
    check_engine_best_effort()
    print()
    print("[OK] Project Map query capability terverifikasi.")
    return 0


def main() -> int:
    print("=== Verifikasi Project Map Query (atlas_query / rig_query / project_map_status) ===")
    setup_fixtures()
    try:
        return _run()
    finally:
        teardown_fixtures()


if __name__ == "__main__":
    raise SystemExit(main())
