"""Verifikasi freshness/stale + refresh terkontrol Project Map (Atlas/RIG).

Melanjutkan Task 1-3 (service + query + integrasi tool). Script ini menguji
secara DETERMINISTIK (tanpa API key / model cloud / engine eksternal):

    Freshness
      [F1] map belum ada -> missing
      [F2] setelah generate -> fresh (baseline fingerprint tersimpan)
      [F3] perubahan file NON-code -> TIDAK membuat stale
      [F4] `.py` diubah -> stale (map lama tetap available/valid)
      [F5] refresh Atlas saja -> atlas fresh, rig tetap stale
      [F6] `.py` baru -> stale
      [F7] `.py` di-rename -> stale
      [F8] `.py` dihapus -> stale
      [F9] map rusak -> invalid

    Refresh
      [R1]-[R3] target=atlas / rig / both (hanya yang diminta yang diganti)
      [R4] penulisan atomik + tanpa file sementara tertinggal
      [R5] Atlas sukses + RIG gagal -> atlas fresh, map rig lama aman
      [R6] kedua gagal -> error jelas, map lama utuh
      [R7] argumen target divalidasi

    Parallel
      [P1] target=both -> Atlas & RIG tumpang tindih (benar-benar paralel)

    Query vs stale
      [Q1] query pada map fresh -> map_status=fresh
      [Q2] query pada map stale TETAP membaca map lama + map_status=stale
      [Q3] query TIDAK memicu generation (file map tidak berubah)

    Permission
      [PERM1] Agent boleh refresh_project_map (workspace_write)
      [PERM2] Consultant TIDAK punya refresh & ditolak policy
      [PERM3] Consultant boleh query/status (read-only)

    Injection / payload
      [I1] context awal Agent TIDAK memuat isi map
      [I2] query result tetap dibatasi (max_results + hard cap)
      [I3] project_map_status() kecil & tanpa isi map

    Concurrency
      [C1] reader tidak pernah melihat partial/invalid JSON
      [C2] concurrent refresh tidak menghasilkan corrupt map

Fixture memakai workspace testing terisolasi `dummy_test/` (BUKAN source
AETHER) + engine Atlas/RIG tiruan (fake CLI) yang tidak butuh repo eksternal.

Jalankan:
    python scripts/check_project_map_freshness.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ai.consultant.policy import build_consultant_permission_manager  # noqa: E402
from agent_ai.consultant.tools import build_consultant_registry  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.response import FinishReason, LLMResponse  # noqa: E402
from agent_ai.permission.manager import PermissionManager  # noqa: E402
from agent_ai.permission.models import PermissionConfig  # noqa: E402
from agent_ai.permission.policy import PermissionPolicy  # noqa: E402
from agent_ai.projects.project_map import (  # noqa: E402
    MAP_TYPE_ATLAS,
    MAP_TYPE_RIG,
    STATUS_FRESH,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_STALE,
    MapInvalidError,
    MapNotFoundError,
    ProjectMapService,
)
from agent_ai.projects.project_map_query import (  # noqa: E402
    HARD_MAX_RESULTS,
    atlas_query,
    rig_query,
)
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.tools.base import ToolValidationError  # noqa: E402
from agent_ai.tools.filesystem import SearchCodeTool  # noqa: E402
from agent_ai.tools.project_map import (  # noqa: E402
    ProjectMapStatusTool,
    RefreshProjectMapTool,
)
from agent_ai.tools.registry import build_registry  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIX_MAIN = DUMMY_ROOT / "project_map_fresh_main"
FIX_BIG = DUMMY_ROOT / "project_map_fresh_big"
FIX_CONC = DUMMY_ROOT / "project_map_fresh_conc"
ENGINE_OK = DUMMY_ROOT / "project_map_fresh_engine_ok"
ENGINE_FAIL = DUMMY_ROOT / "project_map_fresh_engine_fail"
TIMING_LOG = DUMMY_ROOT / "project_map_fresh_timing.jsonl"

#: Penanda unik di dalam map; TIDAK boleh muncul di context/status LLM.
MAP_MARKER = "__AETHER_MAP_MARKER_freshness__"

_MAP_DIR = ".aether/map"


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# --------------------------------------------------------------------------- #
# Fake engines (parameterized via env, tanpa repo engine eksternal)
# --------------------------------------------------------------------------- #
_FAKE_ENGINE = '''\
import json
import os
import sys
import time


def _which():
    name = os.path.basename(sys.argv[0] or "")
    return name.split(".")[0] or "unknown"


def _timing(which, phase):
    log = os.environ.get("AETHER_FAKE_TIMING_LOG")
    if not log:
        return
    # File per-engine supaya dua engine paralel tidak berebut file yang sama.
    path = log + "." + which
    for _ in range(5):
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"which": which, "phase": phase, "t": time.time()}) + "\\n")
            return
        except OSError:
            time.sleep(0.02)


project = sys.argv[1] if len(sys.argv) > 1 else "."
output = sys.argv[2] if len(sys.argv) > 2 else "out.json"
which = _which()
_timing(which, "start")

try:
    sleep = float(os.environ.get("AETHER_FAKE_SLEEP", "0") or "0")
except ValueError:
    sleep = 0.0
if sleep > 0:
    time.sleep(sleep)

marker = os.environ.get("AETHER_FAKE_MARKER", "fake-generated")
module = "fake_" + which
payload = {
    "version": 1,
    "project": {"name": module, "language": "python"},
    "files": [module + ".py"],
    "modules": {module: {"file": module + ".py"}},
    "symbols": {
        "FakeSymbol": {
            "kind": "class",
            "file": module + ".py",
            "start_line": 1,
            "end_line": 5,
            "methods": ["fake_method"],
        }
    },
    "imports": [],
    "inherits": [],
    "calls": [],
    "entrypoints": [module],
    "schema_version": "rig-json/v1",
    "components": [],
    "aggregators": [],
    "runners": [],
    "tests": [],
    "external_packages": [],
    "package_managers": [],
    "code_files": [
        {"id": 1, "kind": "file", "name": module + ".py", "file_path": module + ".py"}
    ],
    "code_modules": [
        {"id": 4, "kind": "module", "name": module, "file_path": module + ".py"}
    ],
    "code_classes": [
        {
            "id": 2,
            "kind": "class",
            "name": "FakeSymbol",
            "file_path": module + ".py",
            "line_start": 1,
            "line_end": 5,
            "bases": [],
        }
    ],
    "code_functions": [
        {
            "id": 3,
            "kind": "function",
            "name": "fake_func",
            "file_path": module + ".py",
            "is_method": False,
        }
    ],
    "code_symbols": [],
    "edges": [{"id": 1, "type": "invokes", "source": 3, "target": 2}],
    "evidence": [],
    "diagnostics": [],
    "unresolved_references": [],
    "marker": marker,
    "engine": which,
}
with open(output, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(payload))
_timing(which, "end")
print("fake engine ok: " + which)
'''

_FAKE_ENGINE_FAIL = """\
import sys

print("fake engine boom", file=sys.stderr)
sys.exit(3)
"""


def _write_engine_scripts(engine_dir: Path, source: str) -> None:
    engine_dir.mkdir(parents=True, exist_ok=True)
    (engine_dir / "atlas.py").write_text(source, encoding="utf-8")
    (engine_dir / "rig.py").write_text(source, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def setup_fixtures() -> None:
    teardown_fixtures()
    (FIX_MAIN / "pkg").mkdir(parents=True, exist_ok=True)
    (FIX_MAIN / "pkg" / "__init__.py").write_text("from pkg.core import greet\n", encoding="utf-8")
    (FIX_MAIN / "pkg" / "core.py").write_text(
        "def greet(name):\n    return 'hi ' + name\n", encoding="utf-8"
    )
    (FIX_MAIN / "main.py").write_text("from pkg.core import greet\n", encoding="utf-8")

    (FIX_BIG / "pkg").mkdir(parents=True, exist_ok=True)
    (FIX_BIG / "pkg" / "core.py").write_text("class FakeSymbol:\n    pass\n", encoding="utf-8")
    big = _big_map_payload()
    map_dir = FIX_BIG / ".aether" / "map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "atlas.json").write_text(json.dumps(big), encoding="utf-8")
    (map_dir / "rig.json").write_text(json.dumps(big), encoding="utf-8")

    (FIX_CONC / "pkg").mkdir(parents=True, exist_ok=True)
    (FIX_CONC / "pkg" / "core.py").write_text("value = 1\n", encoding="utf-8")

    _write_engine_scripts(ENGINE_OK, _FAKE_ENGINE)
    _write_engine_scripts(ENGINE_FAIL, _FAKE_ENGINE_FAIL)


def _big_map_payload() -> dict:
    payload = {
        "version": 1,
        "project": {"name": "bigmap", "language": "python"},
        "files": ["pkg/core.py"],
        "modules": {"pkg.core": {"file": "pkg/core.py"}},
        "symbols": {
            "FakeSymbol": {
                "kind": "class",
                "file": "pkg/core.py",
                "start_line": 1,
                "end_line": 2,
            }
        },
        "imports": [],
        "inherits": [],
        "calls": [],
        "entrypoints": ["pkg.core"],
        "code_files": [
            {"id": 1, "kind": "file", "name": "pkg/core.py", "file_path": "pkg/core.py"}
        ],
        "code_classes": [
            {
                "id": 2,
                "kind": "class",
                "name": "FakeSymbol",
                "file_path": "pkg/core.py",
                "line_start": 1,
                "line_end": 2,
            }
        ],
        "code_functions": [],
        "code_modules": [],
        "code_symbols": [],
        "edges": [],
        "marker": MAP_MARKER,
        "padding": "PAD" * 70000,
    }
    return payload


def teardown_fixtures() -> None:
    for path in (FIX_MAIN, FIX_BIG, FIX_CONC, ENGINE_OK, ENGINE_FAIL):
        shutil.rmtree(path, ignore_errors=True)
    _clear_timing()
    for key in ("AETHER_FAKE_MARKER", "AETHER_FAKE_SLEEP", "AETHER_FAKE_TIMING_LOG"):
        os.environ.pop(key, None)


def _clear_timing() -> None:
    for path in TIMING_LOG.parent.glob(TIMING_LOG.name + "*"):
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# Fake provider (untuk injection test; deterministik, tanpa model cloud)
# --------------------------------------------------------------------------- #
class ScriptedProvider(BaseProvider):
    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.received_messages = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.received_messages.append(messages)
        return GenerateResult(text="", model="scripted-model", provider="scripted")

    def normalize_response(self, result):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[index]


def _final(text: str) -> LLMResponse:
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


def _join_messages(messages) -> str:
    if not messages:
        return ""
    chunks = []
    for message in messages:
        if isinstance(message, dict):
            chunks.append(str(message.get("content") or ""))
            for call in message.get("tool_calls") or []:
                chunks.append(json.dumps(call, default=str))
        else:
            chunks.append(str(getattr(message, "content", "") or ""))
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# [F] Freshness
# --------------------------------------------------------------------------- #
def check_freshness() -> None:
    print()
    print("-- Freshness --")
    svc = ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_OK)

    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_MISSING, "harus missing")
    status = svc.get_status(FIX_MAIN)
    _expect(status["status"] == STATUS_MISSING, status)
    _expect(status["freshness"] == STATUS_MISSING, status)
    print("[F1] map belum ada -> missing : OK")

    svc.generate_map(FIX_MAIN, MAP_TYPE_ATLAS)
    svc.generate_map(FIX_MAIN, MAP_TYPE_RIG)
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_FRESH, "atlas harus fresh")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_RIG) == STATUS_FRESH, "rig harus fresh")
    status = svc.get_status(FIX_MAIN)
    _expect(status["freshness"] == STATUS_FRESH, status)
    _expect(status["fresh"] == [MAP_TYPE_ATLAS, MAP_TYPE_RIG], status)
    _expect(status["maps"][MAP_TYPE_ATLAS]["tracked"] is True, status)
    meta_path = svc.get_meta_path(FIX_MAIN, MAP_TYPE_ATLAS)
    _expect(meta_path.is_file(), "metadata freshness harus ditulis")
    _expect(meta_path.stat().st_size < 4096, "metadata freshness harus kecil")
    _expect("fingerprint" in json.loads(meta_path.read_text(encoding="utf-8")), "meta harus punya fingerprint")
    print("[F2] setelah generate -> fresh (baseline fingerprint) : OK")

    (FIX_MAIN / "NOTES.txt").write_text("catatan\n", encoding="utf-8")
    (FIX_MAIN / "README.md").write_text("# readme\n", encoding="utf-8")
    (FIX_MAIN / "config.json").write_text("{}\n", encoding="utf-8")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_FRESH, "non-code tidak boleh stale")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_RIG) == STATUS_FRESH, "non-code tidak boleh stale")
    print("[F3] perubahan file non-code -> TIDAK stale : OK")

    core = FIX_MAIN / "pkg" / "core.py"
    core.write_text(core.read_text(encoding="utf-8") + "\n\ndef extra():\n    return 2\n", encoding="utf-8")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_STALE, "ubah .py harus stale")
    status = svc.get_status(FIX_MAIN)
    _expect(status["freshness"] == STATUS_STALE, status)
    _expect(status["stale"] == [MAP_TYPE_ATLAS, MAP_TYPE_RIG], status)
    _expect(status["maps"][MAP_TYPE_ATLAS]["status"] == "available", status)
    _expect(bool(status["maps"][MAP_TYPE_ATLAS]["reason"]), "stale harus punya alasan")
    print("[F4] .py diubah -> stale (map lama tetap valid) : OK")

    svc.generate_map(FIX_MAIN, MAP_TYPE_ATLAS)
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_FRESH, "atlas harus fresh")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_RIG) == STATUS_STALE, "rig harus tetap stale")
    print("[F5] refresh Atlas saja -> atlas fresh, rig tetap stale : OK")

    (FIX_MAIN / "pkg" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_STALE, ".py baru harus stale")
    print("[F6] .py baru -> stale : OK")

    svc.generate_map(FIX_MAIN, MAP_TYPE_ATLAS)
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_FRESH, "atlas harus fresh")
    (FIX_MAIN / "pkg" / "extra.py").rename(FIX_MAIN / "pkg" / "renamed.py")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_STALE, "rename .py harus stale")
    print("[F7] .py di-rename -> stale : OK")

    svc.generate_map(FIX_MAIN, MAP_TYPE_ATLAS)
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_FRESH, "atlas harus fresh")
    (FIX_MAIN / "pkg" / "renamed.py").unlink()
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_STALE, "hapus .py harus stale")
    print("[F8] .py dihapus -> stale : OK")

    svc.generate_map(FIX_MAIN, MAP_TYPE_ATLAS)
    atlas_path = svc.get_map_path(FIX_MAIN, MAP_TYPE_ATLAS)
    atlas_path.write_text("{ bukan json", encoding="utf-8")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_INVALID, "rusak harus invalid")
    status = svc.get_status(FIX_MAIN)
    _expect(status["freshness"] == STATUS_INVALID, status)
    _expect(status["maps"][MAP_TYPE_ATLAS]["freshness"] == STATUS_INVALID, status)
    print("[F9] map rusak -> invalid : OK")

    # Kembalikan kondisi fresh untuk seksi berikutnya.
    svc.generate_maps(FIX_MAIN, [MAP_TYPE_ATLAS, MAP_TYPE_RIG], parallel=False)
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_FRESH, "restore atlas")


# --------------------------------------------------------------------------- #
# [R] Refresh
# --------------------------------------------------------------------------- #
def check_refresh() -> None:
    print()
    print("-- Refresh (atlas / rig / both) --")
    svc = ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_OK)
    atlas_path = svc.get_map_path(FIX_MAIN, MAP_TYPE_ATLAS)
    rig_path = svc.get_map_path(FIX_MAIN, MAP_TYPE_RIG)
    tool = RefreshProjectMapTool(root=FIX_MAIN, service=svc)

    svc.generate_maps(FIX_MAIN, [MAP_TYPE_ATLAS, MAP_TYPE_RIG], parallel=False)
    rig_before = rig_path.read_bytes()

    os.environ["AETHER_FAKE_MARKER"] = "atlas-only"
    result = tool.execute(target=MAP_TYPE_ATLAS)
    _expect(result["target"] == MAP_TYPE_ATLAS, result)
    _expect(len(result["refreshed"]) == 1, result)
    _expect(result["refreshed"][0]["map"] == MAP_TYPE_ATLAS, result)
    _expect(b"atlas-only" in atlas_path.read_bytes(), "atlas harus diganti")
    _expect(rig_path.read_bytes() == rig_before, "rig tidak boleh tersentuh")
    _expect(result["freshness"] == STATUS_FRESH, result)
    print("[R1] target=atlas -> hanya atlas diganti : OK")

    os.environ["AETHER_FAKE_MARKER"] = "rig-only"
    result = tool.execute(target=MAP_TYPE_RIG)
    _expect(result["refreshed"][0]["map"] == MAP_TYPE_RIG, result)
    _expect(b"rig-only" in rig_path.read_bytes(), "rig harus diganti")
    print("[R2] target=rig -> hanya rig diganti : OK")

    os.environ["AETHER_FAKE_MARKER"] = "both-mark"
    result = tool.execute(target="both")
    _expect(len(result["refreshed"]) == 2, result)
    _expect(b"both-mark" in atlas_path.read_bytes(), "atlas harus diganti")
    _expect(b"both-mark" in rig_path.read_bytes(), "rig harus diganti")
    _expect(result["freshness"] == STATUS_FRESH, result)
    print("[R3] target=both -> keduanya diganti : OK")

    leftovers = [p.name for p in atlas_path.parent.iterdir() if ".tmp-" in p.name]
    _expect(not leftovers, "tidak boleh ada file sementara: {}".format(leftovers))
    _expect(svc.load_map(FIX_MAIN, MAP_TYPE_ATLAS).get("marker") == "both-mark", "map harus utuh")
    print("[R4] penulisan atomik + tanpa file sementara : OK")

    failing = RefreshProjectMapTool(
        root=FIX_MAIN, service=ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_FAIL)
    )
    os.environ["AETHER_FAKE_MARKER"] = "atlas-after-fail"
    rig_before = rig_path.read_bytes()
    result = failing.execute(target="both")
    _expect(len(result["refreshed"]) == 1, result)
    _expect(result["refreshed"][0]["map"] == MAP_TYPE_ATLAS, result)
    _expect("rig" in (result.get("failed") or {}), result)
    _expect(b"atlas-after-fail" in atlas_path.read_bytes(), "atlas (sukses) boleh diganti")
    _expect(rig_path.read_bytes() == rig_before, "map rig lama harus tetap aman")
    print("[R5] Atlas sukses + RIG gagal -> atlas fresh, map rig lama aman : OK")

    both_fail = RefreshProjectMapTool(
        root=FIX_MAIN, service=ProjectMapService(atlas_dir=ENGINE_FAIL, rig_dir=ENGINE_FAIL)
    )
    atlas_before = atlas_path.read_bytes()
    rig_before = rig_path.read_bytes()
    blocked = False
    try:
        both_fail.execute(target="both")
    except ToolValidationError as exc:
        blocked = True
        _expect("atlas" in str(exc) and "rig" in str(exc), "error harus menyebut atlas & rig")
    _expect(blocked, "kedua gagal harus ToolValidationError")
    _expect(atlas_path.read_bytes() == atlas_before, "map lama atlas harus tetap aman")
    _expect(rig_path.read_bytes() == rig_before, "map lama rig harus tetap aman")
    leftovers = [p.name for p in atlas_path.parent.iterdir() if ".tmp-" in p.name]
    _expect(not leftovers, "tidak boleh ada file sementara: {}".format(leftovers))
    print("[R6] kedua gagal -> error jelas, map lama utuh : OK")

    blocked = False
    try:
        tool.execute(target="teleport")
    except ToolValidationError as exc:
        blocked = "target" in str(exc)
    _expect(blocked, "target tidak dikenal harus ditolak")
    print("[R7] argumen target divalidasi : OK")

    os.environ.pop("AETHER_FAKE_MARKER", None)
    svc.generate_maps(FIX_MAIN, [MAP_TYPE_ATLAS, MAP_TYPE_RIG], parallel=False)


# --------------------------------------------------------------------------- #
# [P] Parallel generation
# --------------------------------------------------------------------------- #
def check_parallel() -> None:
    print()
    print("-- Parallel generation (target=both) --")
    _clear_timing()
    svc = ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_OK)
    tool = RefreshProjectMapTool(root=FIX_MAIN, service=svc)

    os.environ["AETHER_FAKE_TIMING_LOG"] = str(TIMING_LOG)
    os.environ["AETHER_FAKE_SLEEP"] = "1.2"
    os.environ.pop("AETHER_FAKE_MARKER", None)
    try:
        start = time.time()
        result = tool.execute(target="both")
        elapsed = time.time() - start
    finally:
        os.environ.pop("AETHER_FAKE_TIMING_LOG", None)
        os.environ.pop("AETHER_FAKE_SLEEP", None)

    _expect(len(result["refreshed"]) == 2, result)
    events = []
    for which in ("atlas", "rig"):
        timing_path = Path(str(TIMING_LOG) + "." + which)
        _expect(timing_path.is_file(), "timing '{}' tidak ada".format(which))
        events.extend(
            json.loads(line)
            for line in timing_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    starts = {e["which"]: e["t"] for e in events if e["phase"] == "start"}
    ends = {e["which"]: e["t"] for e in events if e["phase"] == "end"}
    _expect({"atlas", "rig"} <= set(starts), "timing start tidak lengkap: {}".format(starts))
    _expect({"atlas", "rig"} <= set(ends), "timing end tidak lengkap: {}".format(ends))
    overlap = starts["atlas"] < ends["rig"] and starts["rig"] < ends["atlas"]
    _expect(
        overlap,
        "Atlas & RIG harus tumpang tindih (paralel). starts={} ends={}".format(starts, ends),
    )
    _expect(elapsed < 2.0, "durasi {:.2f}s terlalu lama untuk paralel".format(elapsed))
    print(
        "[P1] target=both -> Atlas & RIG tumpang tindih (paralel) : OK "
        "(elapsed={:.2f}s)".format(elapsed)
    )


# --------------------------------------------------------------------------- #
# [Q] Query vs stale
# --------------------------------------------------------------------------- #
def check_query_stale() -> None:
    print()
    print("-- Query terhadap map stale --")
    svc = ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_OK)
    svc.generate_maps(FIX_MAIN, [MAP_TYPE_ATLAS, MAP_TYPE_RIG], parallel=False)
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_FRESH, "harus fresh dulu")

    fresh = atlas_query(svc, FIX_MAIN, "FakeSymbol")
    _expect(fresh["returned"] >= 1, "query pada map fresh harus berhasil")
    _expect(fresh["map_status"] == STATUS_FRESH, fresh)
    print("[Q1] query pada map fresh -> map_status=fresh : OK")

    core = FIX_MAIN / "pkg" / "core.py"
    core.write_text(core.read_text(encoding="utf-8") + "\n# ubah lagi\n", encoding="utf-8")
    _expect(svc.get_freshness(FIX_MAIN, MAP_TYPE_ATLAS) == STATUS_STALE, "harus stale")

    atlas_path = svc.get_map_path(FIX_MAIN, MAP_TYPE_ATLAS)
    before_bytes = atlas_path.read_bytes()
    before_files = sorted(p.name for p in atlas_path.parent.iterdir())

    stale = atlas_query(svc, FIX_MAIN, "FakeSymbol")
    _expect(stale["returned"] >= 1, "query pada map stale harus tetap membaca map lama")
    _expect(stale["map_status"] == STATUS_STALE, stale)
    rig_stale = rig_query(svc, FIX_MAIN, "FakeSymbol")
    _expect(rig_stale["map_status"] == STATUS_STALE, rig_stale)
    print("[Q2] query map stale tetap baca map lama + map_status=stale : OK")

    _expect(atlas_path.read_bytes() == before_bytes, "query TIDAK boleh meregenerasi map")
    after_files = sorted(p.name for p in atlas_path.parent.iterdir())
    _expect(before_files == after_files, "query TIDAK boleh membuat file map baru")
    print("[Q3] query tidak memicu generation (file map tidak berubah) : OK")

    svc.generate_maps(FIX_MAIN, [MAP_TYPE_ATLAS, MAP_TYPE_RIG], parallel=False)


# --------------------------------------------------------------------------- #
# [PERM] Permission
# --------------------------------------------------------------------------- #
def check_permission() -> None:
    print()
    print("-- Permission (Agent vs Consultant) --")
    agent_tools = set(build_registry(root=FIX_MAIN).list())
    _expect(
        {"atlas_query", "rig_query", "project_map_status", "refresh_project_map"} <= agent_tools,
        agent_tools,
    )
    manager = PermissionManager(PermissionPolicy(PermissionConfig()))
    decision = manager.check("refresh_project_map", {"target": "both"})
    _expect(decision.allowed is True, decision)
    _expect(
        decision.action_class.value == "workspace_write",
        "refresh_project_map harus workspace_write, dapat {}".format(decision.action_class),
    )
    print("[PERM1] Agent boleh refresh_project_map (workspace_write) : OK")

    consult = build_consultant_registry(FIX_MAIN, mode="investigate")
    _expect(not consult.has("refresh_project_map"), "Consultant tidak boleh punya refresh")
    for name in ("atlas_query", "rig_query", "project_map_status"):
        _expect(consult.has(name), "Consultant harus punya '{}'".format(name))
    cmanager = build_consultant_permission_manager()
    cdecision = cmanager.check("refresh_project_map", {"target": "both"})
    _expect(cdecision.allowed is False, cdecision)
    print("[PERM2] Consultant TIDAK punya refresh & ditolak policy : OK")

    for name, args in (
        ("atlas_query", {"query": "FakeSymbol"}),
        ("rig_query", {"query": "FakeSymbol"}),
        ("project_map_status", {}),
    ):
        _expect(
            cmanager.check(name, args).allowed is True,
            "Consultant harus boleh '{}'".format(name),
        )
    print("[PERM3] Consultant boleh query/status (read-only) : OK")


# --------------------------------------------------------------------------- #
# [I] Injection / payload
# --------------------------------------------------------------------------- #
def check_injection() -> None:
    print()
    print("-- Injection / payload --")
    map_dir = FIX_BIG / ".aether" / "map"
    atlas_size = (map_dir / "atlas.json").stat().st_size

    provider = ScriptedProvider([_final("Selesai tanpa membaca peta.")])
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=build_registry(root=FIX_BIG)),
        use_continuous_loop=True,
    )
    orchestrator.run("Jelaskan struktur project.")
    first = _join_messages(provider.received_messages[0])
    _expect(MAP_MARKER not in first, "context awal Agent tidak boleh memuat isi map")
    _expect("PAD" * 100 not in first, "context awal Agent tidak boleh memuat padding map")
    _expect(len(first) < atlas_size, "context awal ({}) < map ({})".format(len(first), atlas_size))
    print("[I1] context awal Agent tanpa isi map : OK ({} bytes vs map {})".format(len(first), atlas_size))

    svc = ProjectMapService()
    limited = atlas_query(svc, FIX_BIG, "FakeSymbol", max_results=100000)
    _expect(limited["returned"] <= HARD_MAX_RESULTS, limited)
    _expect(len(json.dumps(limited)) < atlas_size, "query result harus jauh lebih kecil dari map")
    print(
        "[I2] query result dibatasi (hard cap {}) : OK ({} bytes)".format(
            HARD_MAX_RESULTS, len(json.dumps(limited))
        )
    )

    status = ProjectMapStatusTool(root=FIX_BIG).execute()
    status_payload = json.dumps(status)
    _expect(len(status_payload) < 2048, "status harus kecil: {}".format(len(status_payload)))
    _expect(MAP_MARKER not in status_payload, "status tidak boleh memuat isi map")
    _expect("PAD" * 10 not in status_payload, "status tidak boleh memuat padding map")
    print("[I3] project_map_status() kecil & tanpa isi map : OK ({} bytes)".format(len(status_payload)))


# --------------------------------------------------------------------------- #
# [C] Concurrency
# --------------------------------------------------------------------------- #
def check_concurrency() -> None:
    print()
    print("-- Concurrency (reader vs writer) --")
    svc = ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_OK)
    svc.generate_maps(FIX_CONC, [MAP_TYPE_ATLAS, MAP_TYPE_RIG], parallel=False)

    stop = threading.Event()
    errors: list = []
    invalid_reads = [0]
    total_reads = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                data = svc.load_map(FIX_CONC, MAP_TYPE_ATLAS)
                if not isinstance(data, dict):
                    invalid_reads[0] += 1
            except (MapNotFoundError, MapInvalidError, OSError, ValueError):
                invalid_reads[0] += 1
            total_reads[0] += 1

    def writer(name: str) -> None:
        for index in range(6):
            os.environ["AETHER_FAKE_MARKER"] = "{}-{}".format(name, index)
            try:
                svc.generate_map(FIX_CONC, MAP_TYPE_ATLAS)
            except Exception as exc:  # noqa: BLE001
                errors.append("generate gagal: {}: {}".format(type(exc).__name__, exc))
                continue
            try:
                status = svc.get_status(FIX_CONC)
            except Exception as exc:  # noqa: BLE001
                errors.append("status gagal: {}: {}".format(type(exc).__name__, exc))
                continue
            if status["maps"][MAP_TYPE_ATLAS]["status"] != "available":
                errors.append("status tidak available: {}".format(status["maps"][MAP_TYPE_ATLAS]))

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    writers = [threading.Thread(target=writer, args=("w{}".format(i),)) for i in range(2)]
    for thread in readers:
        thread.start()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    for thread in readers:
        thread.join(timeout=5)

    _expect(not errors, "error selama concurrency: {}".format(errors))
    _expect(invalid_reads[0] == 0, "reader melihat JSON tidak valid: {}".format(invalid_reads[0]))
    _expect(total_reads[0] > 0, "reader harus benar-benar membaca map")
    print("[C1] reader tidak pernah melihat partial/invalid JSON ({} read) : OK".format(total_reads[0]))

    final = svc.load_map(FIX_CONC, MAP_TYPE_ATLAS)
    _expect(isinstance(final, dict) and bool(final.get("marker")), "map akhir harus valid")
    leftovers = [p.name for p in svc.get_map_dir(FIX_CONC).iterdir() if ".tmp-" in p.name]
    _expect(not leftovers, "tidak boleh ada file sementara: {}".format(leftovers))
    print("[C2] concurrent refresh tidak menghasilkan corrupt map : OK")

    os.environ.pop("AETHER_FAKE_MARKER", None)


# --------------------------------------------------------------------------- #
# Payload sizes (contoh nyata bila map repo tersedia)
# --------------------------------------------------------------------------- #
def report_payload_sizes() -> None:
    print()
    print("-- Ukuran payload (contoh) --")
    svc = ProjectMapService()
    big_map = FIX_BIG / ".aether" / "map" / "atlas.json"
    print(
        "fixture : full atlas map = {} bytes".format(big_map.stat().st_size)
    )
    limited = atlas_query(svc, FIX_BIG, "FakeSymbol")
    status = ProjectMapStatusTool(root=FIX_BIG).execute()
    print("fixture : atlas_query result = {} bytes".format(len(json.dumps(limited))))
    print("fixture : project_map_status  = {} bytes".format(len(json.dumps(status))))

    real_map = PROJECT_ROOT / ".aether" / "map" / "atlas.json"
    if real_map.is_file():
        elapsed_start = time.time()
        result = atlas_query(svc, PROJECT_ROOT, "TaskExecutor")
        elapsed = time.time() - elapsed_start
        print(
            "repo    : full atlas map = {} bytes".format(real_map.stat().st_size)
        )
        print(
            "repo    : atlas_query('TaskExecutor') = {} bytes (returned={}, {:.2f}s)".format(
                len(json.dumps(result)), result.get("returned"), elapsed
            )
        )
        print("repo    : project_map_status = {} bytes".format(
            len(json.dumps(ProjectMapStatusTool(root=PROJECT_ROOT).execute()))
        ))
    else:
        print("repo    : .aether/map/atlas.json tidak ada (skip pengukuran nyata)")


def report_consultant_case() -> None:
    """Benchmark deterministik kasus Consultant 'backup panel hilang listnya'.

    TIDAK memakai LLM (tidak ada API key provider & Ollama tidak aktif di
    environment ini), jadi yang diukur adalah BIAYA NAVIGASI (ukuran payload +
    apakah lokasi file/baris langsung ditemukan) - bukan jumlah langkah LLM.
    """
    print()
    print("-- Benchmark navigasi kasus Consultant: 'backup panel hilang listnya' --")
    svc = ProjectMapService()
    root = PROJECT_ROOT

    if not svc.map_exists(root, MAP_TYPE_ATLAS):
        print("[B] repo map atlas tidak tersedia -> skip benchmark navigasi")
        return

    target = "GithubBackupService"
    atlas = atlas_query(svc, root, target, include_relations=False, max_results=8)
    hit = next((i for i in atlas["results"] if i.get("kind") == "class"), None) or {}
    print(
        "[B1] atlas_query('{}') -> total={} {} bytes; class di {}:{}-{} (map_status={})".format(
            target,
            atlas["total"],
            len(json.dumps(atlas)),
            hit.get("file"),
            hit.get("line_start"),
            hit.get("line_end"),
            atlas["map_status"],
        )
    )

    method = atlas_query(svc, root, "list_checkpoints", include_relations=False, max_results=3)
    if method["results"]:
        first = method["results"][0]
        print(
            "[B2] atlas_query('list_checkpoints') -> {} bytes; method di {}:{}-{}".format(
                len(json.dumps(method)),
                first.get("file"),
                first.get("line_start"),
                first.get("line_end"),
            )
        )

    search = SearchCodeTool(root=root).execute(query=target, max_results=50)
    print(
        "[B3] search_code('{}') -> count={} {} bytes (cocok tekstual; perlu follow-up "
        "read_file untuk tahu struktur/rentang baris)".format(
            target, search.get("count"), len(json.dumps(search))
        )
    )

    if svc.map_exists(root, MAP_TYPE_RIG):
        rig = rig_query(svc, root, target, relation="related", max_results=8)
        print("[B4] rig_query('{}', related) -> {} bytes".format(target, len(json.dumps(rig))))
    else:
        print(
            "[B4] rig_query TIDAK tersedia di mesin ini (rig.json absen karena defect engine "
            "MAP_CODE_RIG eksternal) -> relationship apa pun fallback ke read_file/manual"
        )

    note = atlas_query(svc, root, "GithubBackupPanel", include_relations=False, max_results=3)
    print(
        "[B5] atlas_query('GithubBackupPanel') -> total={} (komponen Vue TIDAK ada di Atlas/RIG "
        "yang Python-only -> frontend tetap perlu search_code/read_file)".format(note["total"])
    )
    print(
        "[B6] CATATAN: before/after jumlah LANGKAH LLM tidak dapat diukur di environment ini "
        "(tidak ada provider LLM aktif). Angka di atas adalah biaya navigasi deterministik."
    )


# --------------------------------------------------------------------------- #
def _run() -> int:
    check_freshness()
    check_refresh()
    check_parallel()
    check_query_stale()
    check_permission()
    check_injection()
    check_concurrency()
    report_payload_sizes()
    report_consultant_case()
    print()
    print("[OK] Freshness + refresh terkontrol Project Map terverifikasi.")
    return 0


def main() -> int:
    print("=== Verifikasi Freshness + Refresh Project Map (Atlas/RIG) ===")
    setup_fixtures()
    try:
        return _run()
    finally:
        teardown_fixtures()


if __name__ == "__main__":
    raise SystemExit(main())
