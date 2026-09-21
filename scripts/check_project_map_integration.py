"""Verifikasi integrasi capability Project Map (Atlas/RIG) ke Agent + Consultant.

Menguji (deterministik, tanpa API key / model cloud):

    [1] Registry Agent memuat 4 capability:
        atlas_query, rig_query, project_map_status, refresh_project_map.
    [2] Registry Consultant memuat 3 capability Project Map READ-ONLY
        (atlas_query, rig_query, project_map_status) di mode investigate MAUPUN
        mode quick, dan TIDAK memuat refresh_project_map. Mode quick memakai
        Bible + Map (tanpa tool source/runtime); investigate memakai Bible +
        Map + Source + Runtime.
    [3] Tool loop: capability Project Map benar-benar dipanggil lewat mekanisme
        tool execution AETHER yang existing, dan hasilnya dikembalikan ke LLM
        sebagai tool result (Agent: continuous loop; Consultant: consult()).
    [4] Tidak ada automatic query: membuat task/chat baru TIDAK menjalankan
        atlas_query / rig_query / project_map_status hanya karena capability
        tersedia. Registry juga tidak menyentuh `.aether/map` saat dibangun.
    [5] Tidak ada full-map injection: prompt/context awal Agent & Consultant
        TIDAK bertambah dengan seluruh isi atlas.json / rig.json.
    [6] Consultant read-only: refresh_project_map tidak dapat dipanggil
        Consultant (tidak terdaftar + ditolak policy) dan tidak menulis map.
    [7] Tool refresh_project_map: hanya Agent; sukses = map tergantikan,
        gagal = map lama tetap aman (atomic temp + replace, tanpa file sisa).

Fixture memakai workspace testing terisolasi `dummy_test/` (BUKAN source
AETHER). Engine Atlas/RIG tiruan (fake CLI) dipakai untuk menguji jalur
sukses/gagal refresh tanpa bergantung pada repo engine eksternal.

Jalankan:
    python scripts/check_project_map_integration.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ai.consultant import ConsultantService, build_consultant_registry  # noqa: E402
from agent_ai.consultant.policy import build_consultant_permission_manager  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.permission.manager import PermissionManager  # noqa: E402
from agent_ai.permission.models import PermissionConfig  # noqa: E402
from agent_ai.permission.policy import PermissionPolicy  # noqa: E402
from agent_ai.projects.project_map import (  # noqa: E402
    ProjectMapService,
)
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.tools.base import ToolValidationError  # noqa: E402
from agent_ai.tools.project_map import RefreshProjectMapTool  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "project_map_integration_fixture"
FIXTURE_EMPTY = DUMMY_ROOT / "project_map_integration_empty"
FIXTURE_BIG = DUMMY_ROOT / "project_map_integration_bigmap"
ENGINE_OK = DUMMY_ROOT / "project_map_integration_engine_ok"
ENGINE_FAIL = DUMMY_ROOT / "project_map_integration_engine_fail"

#: Penanda unik di dalam map; tidak boleh muncul di context/prompt LLM.
MAP_MARKER = "__AETHER_MAP_MARKER_9f3c7a__"

AGENT_MAP_TOOLS = (
    "atlas_query",
    "rig_query",
    "project_map_status",
    "refresh_project_map",
)
CONSULTANT_MAP_TOOLS = ("atlas_query", "rig_query", "project_map_status")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _atlas_payload() -> dict:
    return {
        "version": 1,
        "project": {"name": "fixture", "language": "python"},
        "files": ["pkg/core.py", "pkg/app.py"],
        "modules": {
            "pkg.core": {"file": "pkg/core.py"},
            "pkg.app": {"file": "pkg/app.py"},
        },
        "symbols": {
            "TaskExecutor": {
                "kind": "class",
                "file": "pkg/core.py",
                "start_line": 10,
                "end_line": 60,
                "methods": ["execute"],
            },
            "TaskExecutor.execute": {
                "kind": "method",
                "file": "pkg/core.py",
                "start_line": 20,
                "end_line": 40,
            },
            "AgentRuntime.run": {
                "kind": "method",
                "file": "pkg/app.py",
                "start_line": 8,
                "end_line": 20,
            },
        },
        "imports": [["pkg.app", "pkg.core", True]],
        "inherits": [],
        "calls": [["pkg.app.AgentRuntime.run", "pkg.core.TaskExecutor.execute", True]],
        "entrypoints": ["pkg.app"],
    }


def _rig_payload() -> dict:
    return {
        "schema_version": "rig-json/v1",
        "repo": {"name": "fixture", "primary_language": "python", "languages": ["python"]},
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
            {"id": 2, "kind": "module", "name": "pkg.core", "file_path": "pkg/core.py", "is_package": False}
        ],
        "code_classes": [
            {
                "id": 3,
                "kind": "class",
                "name": "TaskExecutor",
                "file_path": "pkg/core.py",
                "line_start": 10,
                "line_end": 60,
                "bases": [],
            }
        ],
        "code_functions": [
            {
                "id": 5,
                "kind": "function",
                "name": "execute",
                "file_path": "pkg/core.py",
                "line_start": 20,
                "line_end": 40,
                "is_method": True,
                "parent_class_id": 3,
            }
        ],
        "code_symbols": [],
        "edges": [],
        "evidence": [],
        "diagnostics": [],
        "unresolved_references": [],
    }


def _write_map(map_dir: Path, name: str, payload: dict) -> None:
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / name).write_text(
        json.dumps(payload, ensure_ascii=True), encoding="utf-8"
    )


_FAKE_ENGINE_OK = """\
import json
import sys

project, output = sys.argv[1], sys.argv[2]
payload = {
    "version": 1,
    "project": {"name": "fake-generated", "language": "python"},
    "files": ["generated.py"],
}
with open(output, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(payload))
print("fake engine ok")
"""

_FAKE_ENGINE_FAIL = """\
import sys

print("fake engine boom", file=sys.stderr)
sys.exit(3)
"""


def setup_fixtures() -> None:
    teardown_fixtures()
    # Project dengan map valid (atlas + rig) + marker di dalamnya.
    (FIXTURE / "pkg").mkdir(parents=True, exist_ok=True)
    (FIXTURE / "pkg" / "core.py").write_text(
        "class TaskExecutor:\\n    def execute(self):\\n        return 1\\n",
        encoding="utf-8",
    )
    atlas = _atlas_payload()
    atlas["marker"] = MAP_MARKER
    rig = _rig_payload()
    rig["marker"] = MAP_MARKER
    map_dir = FIXTURE / ".aether" / "map"
    _write_map(map_dir, "atlas.json", atlas)
    _write_map(map_dir, "rig.json", rig)

    # Project TANPA map (untuk membuktikan tidak ada auto-query/generate).
    FIXTURE_EMPTY.mkdir(parents=True, exist_ok=True)
    (FIXTURE_EMPTY / "main.py").write_text("print('hi')\\n", encoding="utf-8")

    # Project dengan map BESAR (untuk membuktikan context tidak diisi full map).
    (FIXTURE_BIG / "pkg").mkdir(parents=True, exist_ok=True)
    (FIXTURE_BIG / "pkg" / "core.py").write_text(
        "class TaskExecutor:\\n    pass\\n", encoding="utf-8"
    )
    big_atlas = _atlas_payload()
    big_atlas["marker"] = MAP_MARKER
    # ~200 KB field: kalau map ikut masuk context, ukurannya akan terlihat jelas.
    big_atlas["padding"] = "PAD" * 70000
    big_rig = _rig_payload()
    big_rig["marker"] = MAP_MARKER
    big_rig["padding"] = "PAD" * 70000
    _write_map(FIXTURE_BIG / ".aether" / "map", "atlas.json", big_atlas)
    _write_map(FIXTURE_BIG / ".aether" / "map", "rig.json", big_rig)

    # Engine tiruan: sukses menulis JSON, dan yang selalu gagal.
    for engine_dir, source in ((ENGINE_OK, _FAKE_ENGINE_OK), (ENGINE_FAIL, _FAKE_ENGINE_FAIL)):
        engine_dir.mkdir(parents=True, exist_ok=True)
        (engine_dir / "atlas.py").write_text(source, encoding="utf-8")
        (engine_dir / "rig.py").write_text(source, encoding="utf-8")


def teardown_fixtures() -> None:
    for path in (FIXTURE, FIXTURE_EMPTY, FIXTURE_BIG, ENGINE_OK, ENGINE_FAIL):
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Fake provider (deterministik, tanpa model cloud)
# --------------------------------------------------------------------------- #
class ScriptedProvider(BaseProvider):
    """Provider yang mengikuti script LLMResponse dan merekam messages."""

    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.received_messages = []
        self.received_tools = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.received_messages.append(messages)
        self.received_tools.append(tools)
        return GenerateResult(text="", model="scripted-model", provider="scripted")

    def normalize_response(self, result):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[index]


def _final(text: str) -> LLMResponse:
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


def _tool_response(name: str, **arguments) -> LLMResponse:
    return LLMResponse(
        actions=[LLMAction(name=name, arguments=dict(arguments))],
        finish_reason=FinishReason.TOOL_CALLS,
    )


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
# [1] + [2] Registry
# --------------------------------------------------------------------------- #
def check_registries() -> None:
    print()
    print("-- Registry Agent vs Consultant --")

    agent_tools = set(build_registry(root=FIXTURE).list())
    for name in AGENT_MAP_TOOLS:
        _expect(name in agent_tools, "registry Agent harus memuat '{}'".format(name))
    print("[1] registry Agent memuat 4 capability Project Map : OK -> {}".format(
        ", ".join(sorted(AGENT_MAP_TOOLS))
    ))

    investigate = set(build_consultant_registry(FIXTURE, mode="investigate").list())
    for name in CONSULTANT_MAP_TOOLS:
        _expect(
            name in investigate,
            "registry Consultant (investigate) harus memuat '{}'".format(name),
        )
    _expect(
        "refresh_project_map" not in investigate,
        "registry Consultant TIDAK boleh memuat 'refresh_project_map'",
    )
    print("[2] registry Consultant (investigate) = 3 capability read-only, tanpa refresh : OK")

    quick = set(build_consultant_registry(FIXTURE, mode="quick").list())
    for name in CONSULTANT_MAP_TOOLS:
        _expect(
            name in quick,
            "registry Consultant (quick) harus memuat '{}'".format(name),
        )
    for name in (
        "refresh_project_map",
        "read_file",
        "search_code",
        "list_files",
        "run_command",
    ):
        _expect(
            name not in quick,
            "mode quick TIDAK boleh memuat '{}'".format(name),
        )
    _expect(
        quick == set(CONSULTANT_MAP_TOOLS) | {"update_project_bible"},
        quick,
    )
    print(
        "[2b] registry Consultant (quick) = Bible + Map read-only "
        "(tanpa source/refresh) : OK -> {}".format(sorted(quick))
    )

    # Capability hanya boleh terdaftar sekali (tidak ada alias/duplikat).
    agent_list = build_registry(root=FIXTURE).list()
    _expect(len(agent_list) == len(set(agent_list)), "nama tool Agent harus unik")
    for wrong in ("get_code_map", "query_code_map", "get_call_graph", "get_dependents"):
        _expect(wrong not in agent_tools, "tidak boleh ada alias tool '{}'".format(wrong))
        _expect(wrong not in investigate, "tidak boleh ada alias tool '{}'".format(wrong))
    print("[2c] tidak ada alias/duplikat tool : OK")


# --------------------------------------------------------------------------- #
# [3] Tool loop (Agent + Consultant)
# --------------------------------------------------------------------------- #
def check_agent_tool_loop() -> None:
    print()
    print("-- Tool loop Agent (native tool calling existing) --")

    executor = ToolExecutor(
        registry=build_registry(root=FIXTURE),
        permission_manager=PermissionManager(PermissionPolicy(PermissionConfig())),
    )
    provider = ScriptedProvider(
        [
            _tool_response("atlas_query", query="TaskExecutor", kind="class"),
            _tool_response("rig_query", query="AgentRuntime.run", relation="callers"),
            _final("Selesai: TaskExecutor di pkg/core.py."),
        ]
    )
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=executor,
        use_continuous_loop=True,
    )
    result = orchestrator.run("Di mana TaskExecutor didefinisikan?")

    _expect(result.status.value == "done", "status harus done, dapat {}".format(result.status))
    steps = result.steps
    _expect(len(steps) == 2, "harus ada 2 langkah tool, dapat {}".format(len(steps)))
    first = steps[0]["observation"]
    _expect(first["success"] is True, "atlas_query harus sukses: {}".format(first.get("error")))
    _expect("pkg/core.py" in json.dumps(first["content"]), "hasil atlas harus memuat lokasi file")
    second = steps[1]["observation"]
    _expect(second["success"] is True, "rig_query harus sukses: {}".format(second.get("error")))
    print("[3a] atlas_query + rig_query dieksekusi lewat ToolExecutor : OK")

    # Hasil tool dikembalikan ke LLM sebagai tool result (role="tool").
    third_call = provider.received_messages[2]
    tool_messages = [m for m in third_call if m.get("role") == "tool"]
    _expect(len(tool_messages) == 2, "harus ada 2 pesan role=tool pada turn berikutnya")
    tool_names = {m.get("name") for m in tool_messages}
    _expect(tool_names == {"atlas_query", "rig_query"}, tool_names)
    joined = _join_messages(tool_messages)
    _expect("pkg/core.py" in joined, "tool result atlas harus dikirim ke LLM")
    print("[3b] hasil tool dikembalikan ke LLM sebagai tool result : OK")

    # Permission policy existing: tool map read-only & refresh diizinkan Agent.
    manager = PermissionManager(PermissionPolicy(PermissionConfig()))
    for name in AGENT_MAP_TOOLS:
        decision = manager.check(name, {"query": "x"} if name.endswith("query") else {})
        _expect(decision.allowed is True, "Agent harus boleh memanggil '{}'".format(name))
        _expect(
            decision.action_class.value in {"read_only", "workspace_write"},
            "klasifikasi '{}' tidak masuk akal: {}".format(name, decision.action_class),
        )
    print("[3c] permission policy Agent mengizinkan 4 capability : OK")


def check_consultant_tool_loop() -> None:
    print()
    print("-- Tool loop Consultant (consult existing) --")

    provider = ScriptedProvider(
        [
            _tool_response("atlas_query", query="TaskExecutor"),
            _final("Findings: TaskExecutor ada di pkg/core.py."),
        ]
    )
    service = ConsultantService()
    result = service.consult(
        "Di mana TaskExecutor didefinisikan?",
        provider=provider,
        root=str(FIXTURE),
        mode="investigate",
    )
    _expect(result.status == "done", result.status)
    used = [event["tool"] for event in result.tool_events]
    _expect("atlas_query" in used, "Consultant harus bisa memakai atlas_query: {}".format(used))
    _expect("refresh_project_map" not in used, "Consultant tidak boleh memakai refresh")
    print("[3d] Consultant dapat memanggil atlas_query via consult() : OK -> {}".format(used))

    tool_messages = [
        m for m in (provider.received_messages[-1] or []) if m.get("role") == "tool"
    ]
    _expect(tool_messages, "hasil tool harus dikembalikan ke LLM Consultant")
    _expect("pkg/core.py" in _join_messages(tool_messages), "tool result harus memuat hasil atlas")
    print("[3e] hasil tool dikembalikan ke LLM Consultant sebagai tool result : OK")


def check_quick_tool_loop() -> None:
    print()
    print("-- Tool loop Consultant QUICK (Bible + Map, tanpa source) --")

    # Quick PUNYA Project Map READ-ONLY dan dapat memanggilnya lewat loop
    # existing (consult). Tidak ada loop/tool baru.
    provider = ScriptedProvider(
        [
            _tool_response("atlas_query", query="TaskExecutor"),
            _tool_response("rig_query", query="AgentRuntime.run", relation="callers"),
            _final("Findings: TaskExecutor ada di pkg/core.py."),
        ]
    )
    service = ConsultantService()
    result = service.consult(
        "Di mana TaskExecutor didefinisikan?",
        provider=provider,
        root=str(FIXTURE),
        mode="quick",
    )
    _expect(result.status == "done", result.status)
    _expect(result.mode == "quick", "mode harus quick, dapat {}".format(result.mode))
    used = [event["tool"] for event in result.tool_events]
    _expect("atlas_query" in used, "QUICK harus bisa memakai atlas_query: {}".format(used))
    _expect("rig_query" in used, "QUICK harus bisa memakai rig_query: {}".format(used))
    map_events = [
        e
        for e in result.tool_events
        if e["tool"] in CONSULTANT_MAP_TOOLS and e.get("success") is not None
    ]
    _expect(
        map_events and all(e["success"] is True for e in map_events),
        "tool map QUICK harus sukses: {}".format(result.tool_events),
    )
    _expect(
        "refresh_project_map" not in used,
        "QUICK tidak boleh memakai refresh_project_map",
    )
    print("[3f] QUICK dapat memanggil atlas_query + rig_query via consult() : OK -> {}".format(used))

    tool_messages = [
        m for m in (provider.received_messages[-1] or []) if m.get("role") == "tool"
    ]
    _expect(tool_messages, "hasil tool harus dikembalikan ke LLM Quick")
    _expect(
        "pkg/core.py" in _join_messages(tool_messages),
        "tool result QUICK harus memuat hasil atlas",
    )
    print("[3g] hasil tool QUICK dikembalikan ke LLM sebagai tool result : OK")

    # Capability Quick benar-benar masuk ke tool definitions yang diterima LLM
    # (bukan hanya registry internal) — pakai mekanisme registry existing.
    offered = {getattr(t, "name", None) for t in (provider.received_tools[0] or [])}
    _expect(
        {"atlas_query", "rig_query", "project_map_status"} <= offered,
        "tool definitions Quick harus memuat capability map: {}".format(offered),
    )
    for forbidden in (
        "refresh_project_map",
        "read_file",
        "search_code",
        "list_files",
        "run_command",
    ):
        _expect(
            forbidden not in offered,
            "tool definitions Quick TIDAK boleh memuat '{}'".format(forbidden),
        )
    print(
        "[3h] tool definitions Quick memuat map & tanpa source/refresh : OK -> {}".format(
            sorted(name for name in offered if name)
        )
    )

    # Quick tetap TIDAK punya tool source/runtime maupun refresh (struktural:
    # tidak terdaftar di registry, sehingga executor menolaknya).
    executor = ToolExecutor(
        registry=build_consultant_registry(FIXTURE, mode="quick"),
        permission_manager=build_consultant_permission_manager(),
    )
    blocked_calls = (
        ("read_file", {"path": "pkg/core.py"}),
        ("search_code", {"query": "TaskExecutor"}),
        ("list_files", {"path": "."}),
        ("run_command", {"command": "git status"}),
        ("refresh_project_map", {"target": "both"}),
    )
    for name, args in blocked_calls:
        payload = executor.execute_tool_call(ToolCall.create(name, args))
        _expect(
            not payload.is_success,
            "QUICK tidak boleh mengeksekusi '{}'".format(name),
        )
    print("[3i] QUICK menolak read_file/search_code/list_files/run_command/refresh : OK")


# --------------------------------------------------------------------------- #
# [4] Tidak ada automatic query / auto-generate
# --------------------------------------------------------------------------- #
def check_no_automatic_query() -> None:
    print()
    print("-- Tidak ada automatic query --")

    before = sorted(p.name for p in (FIXTURE / ".aether").rglob("*"))
    map_dir_empty = FIXTURE_EMPTY / ".aether"

    # Membangun registry TIDAK boleh menyentuh `.aether/map`.
    build_registry(root=FIXTURE_EMPTY)
    build_consultant_registry(FIXTURE_EMPTY, mode="investigate")
    _expect(not map_dir_empty.exists(), "registrasi tool TIDAK boleh membuat .aether/map")
    print("[4a] registrasi tool tidak menyentuh .aether/map : OK")

    # Task Agent tanpa tool call -> tidak ada map tool yang dijalankan.
    provider = ScriptedProvider([_final("Tidak perlu peta.")])
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=build_registry(root=FIXTURE)),
        use_continuous_loop=True,
    )
    result = orchestrator.run("Sebutkan nama project.")
    _expect(result.status.value == "done", result.status)
    _expect(result.steps == [], "tidak boleh ada langkah tool otomatis: {}".format(result.steps))
    _expect(provider.calls == 1, "provider harus dipanggil sekali (tanpa tool)")
    print("[4b] task Agent biasa tidak menjalankan atlas_query/rig_query/status : OK")

    # Chat Consultant tanpa tool call -> tidak ada map tool yang dijalankan.
    provider2 = ScriptedProvider([_final("Belum terverifikasi dari Bible.")])
    result2 = ConsultantService().consult(
        "Apa isi project ini?",
        provider=provider2,
        root=str(FIXTURE),
        mode="investigate",
    )
    _expect(result2.status == "done", result2.status)
    map_events = [
        e for e in result2.tool_events if e["tool"] in AGENT_MAP_TOOLS
    ]
    _expect(not map_events, "Consultant tidak boleh otomatis query map: {}".format(map_events))
    print("[4c] chat Consultant biasa tidak menjalankan query map : OK")

    # Tidak ada efek samping pada map/registri ataupun file sementara.
    after = sorted(p.name for p in (FIXTURE / ".aether").rglob("*"))
    _expect(before == after, "map/folder tidak boleh berubah: {} != {}".format(before, after))
    leftovers = [
        p.name for p in (FIXTURE / ".aether" / "map").iterdir() if ".tmp-" in p.name
    ]
    _expect(not leftovers, "tidak boleh ada file sementara tertinggal: {}".format(leftovers))
    print("[4d] map tidak berubah & tanpa file sementara : OK")


# --------------------------------------------------------------------------- #
# [5] Tidak ada full-map injection
# --------------------------------------------------------------------------- #
def check_no_full_map_injection() -> None:
    print()
    print("-- Tidak ada full-map injection --")

    map_dir = FIXTURE_BIG / ".aether" / "map"
    map_size = (map_dir / "atlas.json").stat().st_size
    rig_size = (map_dir / "rig.json").stat().st_size
    total_map_size = map_size + rig_size

    provider = ScriptedProvider([_final("Selesai tanpa membaca peta.")])
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=build_registry(root=FIXTURE_BIG)),
        use_continuous_loop=True,
    )
    orchestrator.run("Jelaskan struktur project.")
    first_call = _join_messages(provider.received_messages[0])
    _expect(MAP_MARKER not in first_call, "context awal Agent tidak boleh memuat isi map")
    _expect(
        "PAD" * 100 not in first_call,
        "context awal Agent tidak boleh memuat isi map (padding map besar)",
    )
    _expect(
        len(first_call) < map_size,
        "context awal Agent ({}) harus lebih kecil dari map ({})".format(
            len(first_call), map_size
        ),
    )
    print(
        "[5a] context awal Agent bebas isi map : OK ({} bytes vs atlas {} / rig {})".format(
            len(first_call), map_size, rig_size
        )
    )

    system_prompt = orchestrator.system_prompt or ""
    for forbidden in ("Atlas Summary", "RIG Summary", "Project Map Overview"):
        _expect(forbidden not in system_prompt, "system prompt tidak boleh memuat '{}'".format(forbidden))
    _expect("Always use Atlas" not in system_prompt, "prompt tidak boleh memaksa Atlas")
    _expect("Always check RIG" not in system_prompt, "prompt tidak boleh memaksa RIG")
    print("[5b] system prompt Agent tanpa ringkasan/paksaan map : OK")

    provider2 = ScriptedProvider([_final("Tidak perlu peta.")])
    ConsultantService().consult(
        "Jelaskan arsitektur project ini.",
        provider=provider2,
        root=str(FIXTURE_BIG),
        mode="investigate",
    )
    consultant_call = _join_messages(provider2.received_messages[0])
    _expect(MAP_MARKER not in consultant_call, "context Consultant tidak boleh memuat isi map")
    _expect(
        "PAD" * 100 not in consultant_call,
        "context Consultant tidak boleh memuat isi map (padding map besar)",
    )
    _expect(
        len(consultant_call) < total_map_size,
        "context awal Consultant ({}) harus lebih kecil dari total map ({})".format(
            len(consultant_call), total_map_size
        ),
    )
    print(
        "[5c] context awal Consultant bebas isi map : OK ({} bytes vs total map {})".format(
            len(consultant_call), total_map_size
        )
    )


# --------------------------------------------------------------------------- #
# [6] Consultant read-only terhadap map
# --------------------------------------------------------------------------- #
def check_consultant_read_only() -> None:
    print()
    print("-- Consultant read-only terhadap project map --")

    registry = build_consultant_registry(FIXTURE_EMPTY, mode="investigate")
    _expect(not registry.has("refresh_project_map"), "refresh tidak boleh ada di registry Consultant")

    executor = ToolExecutor(
        registry=registry,
        permission_manager=build_consultant_permission_manager(),
    )
    payload = executor.execute_tool_call(
        ToolCall.create("refresh_project_map", {"target": "both"})
    )
    _expect(not payload.is_success, "Consultant tidak boleh sukses memanggil refresh_project_map")
    _expect(
        not (FIXTURE_EMPTY / ".aether").exists(),
        "Consultant TIDAK boleh membuat/menulis map",
    )
    print("[6a] refresh_project_map tidak terdaftar & tidak menulis map : OK")

    manager = build_consultant_permission_manager()
    decision = manager.check("refresh_project_map", {"target": "both"})
    _expect(decision.allowed is False, "policy Consultant harus menolak refresh_project_map")
    print("[6b] policy Consultant menolak refresh_project_map : OK -> {}".format(decision.reason))

    # Capability read-only tetap diizinkan (tidak ikut terkunci).
    for name, args in (
        ("atlas_query", {"query": "x"}),
        ("rig_query", {"query": "x"}),
        ("project_map_status", {}),
    ):
        _expect(manager.check(name, args).allowed is True, "Consultant harus boleh '{}'".format(name))
    print("[6c] capability map read-only tetap diizinkan untuk Consultant : OK")


# --------------------------------------------------------------------------- #
# [7] refresh_project_map (Agent-only) + atomicity
# --------------------------------------------------------------------------- #
def check_refresh_tool() -> None:
    print()
    print("-- refresh_project_map (Agent-only) --")

    map_path = FIXTURE / ".aether" / "map" / "atlas.json"
    original = map_path.read_text(encoding="utf-8")

    # Gagal: map lama harus tetap aman + tanpa file sementara.
    failing = RefreshProjectMapTool(
        root=FIXTURE, service=ProjectMapService(atlas_dir=ENGINE_FAIL, rig_dir=ENGINE_FAIL)
    )
    blocked = False
    try:
        failing.execute(target="atlas")
    except ToolValidationError as exc:
        blocked = True
        _expect("atlas" in str(exc), "error harus menyebut map yang gagal")
    _expect(blocked, "generation gagal harus menjadi ToolValidationError yang jelas")
    _expect(map_path.read_text(encoding="utf-8") == original, "map lama harus tetap aman")
    leftovers = [p.name for p in map_path.parent.iterdir() if ".tmp-" in p.name]
    _expect(not leftovers, "tidak boleh ada file sementara tertinggal: {}".format(leftovers))
    print("[7a] generation gagal -> error jelas + map lama utuh (atomic) : OK")

    # Sukses: map tergantikan.
    tool = RefreshProjectMapTool(
        root=FIXTURE, service=ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_OK)
    )
    result = tool.execute(target="atlas")
    _expect(result["target"] == "atlas", result)
    _expect(len(result["refreshed"]) == 1, result)
    _expect(result["refreshed"][0]["map"] == "atlas", result)
    new_content = map_path.read_text(encoding="utf-8")
    _expect(new_content != original, "map harus benar-benar digantikan")
    _expect("fake-generated" in new_content, "map harus berisi hasil engine")
    _expect(MAP_MARKER not in new_content, "map lama tidak boleh tertinggal")
    print("[7b] generation sukses -> map tergantikan : OK")

    # Gagal sebagian: `both` tetap melaporkan yang gagal tanpa menghapus yang sukses.
    partial = RefreshProjectMapTool(
        root=FIXTURE, service=ProjectMapService(atlas_dir=ENGINE_OK, rig_dir=ENGINE_FAIL)
    )
    partial_result = partial.execute(target="both")
    _expect(len(partial_result["refreshed"]) == 1, partial_result)
    _expect("rig" in (partial_result.get("failed") or {}), partial_result)
    print("[7c] kegagalan sebagian dilaporkan tanpa merusak hasil sukses : OK")

    # Parameter berubah yang tidak valid ditolak.
    invalid_blocked = False
    try:
        tool.execute(target="teleport")
    except ToolValidationError as exc:
        invalid_blocked = "target" in str(exc)
    _expect(invalid_blocked, "target tidak dikenal harus ditolak")
    print("[7d] argumen 'target' divalidasi : OK")


# --------------------------------------------------------------------------- #
def _run() -> int:
    check_registries()
    check_agent_tool_loop()
    check_consultant_tool_loop()
    check_quick_tool_loop()
    check_no_automatic_query()
    check_no_full_map_injection()
    check_consultant_read_only()
    check_refresh_tool()
    print()
    print("[OK] Integrasi Project Map (Agent + Consultant) terverifikasi.")
    return 0


def main() -> int:
    print("=== Verifikasi Integrasi Project Map (Agent + Consultant) ===")
    setup_fixtures()
    try:
        return _run()
    finally:
        teardown_fixtures()


if __name__ == "__main__":
    raise SystemExit(main())
