"""Task 08 — Extension Full Integration & Validation.

End-to-end validation of the whole Extension System built in Tasks 01-07:

    AETHER startup -> discover/load/register -> capability registry ->
    Agent/Consultant/UI -> config/storage -> structured result -> artifact

and the lifecycle: git install -> validate -> load -> register -> enable ->
disable -> enable -> update -> uninstall.

Design rules respected here:
    * No new Extension subsystem is created; existing contracts are exercised.
    * Dummy Extensions are created under ``tmp_path`` only, never in the real
      ``Extension/`` runtime directory, and cleaned up by pytest automatically.
    * No network / GitHub dependency: local Git repositories only.
    * ``ExtensionStorage`` runtime data root is redirected to ``tmp_path`` so
      the tests never touch ``data/`` or an installed user Extension.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agent_ai.extensions.base import Extension
from agent_ai.extensions.capabilities import (
    CAPABILITY_TYPES,
    CapabilityRegistry,
    CapabilityValidationError,
    DuplicateCapabilityError,
)
from agent_ai.extensions.catalog import ExtensionCatalog
from agent_ai.extensions.config import ConfigValueStore, RequiredConfigMissingError
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.discovery import load_extension_from_dir
from agent_ai.extensions.errors import (
    ExtensionInstallError,
    ExtensionLifecycleError,
    ExtensionUpdateError,
    ExtensionValidationError,
)
from agent_ai.extensions.lifecycle import ExtensionLifecycleStore
from agent_ai.extensions.loader import ExtensionLoader
from agent_ai.extensions.manager import ExtensionManager
from agent_ai.extensions.manifest import Manifest
from agent_ai.extensions.registry import ExtensionRegistry
from agent_ai.extensions.storage import ExtensionStorage
from agent_ai.extensions.ui import (
    UICatalog,
    resolve_renderer_for_result,
    build_form_schema_for_extension,
    is_renderer_available,
    RENDERER_TYPES,
)
from agent_ai.tools.base import BaseTool
from agent_ai.tools.registry import ToolRegistry


# ===========================================================================
# Isolation / helpers
# ===========================================================================

@pytest.fixture(autouse=True)
def _isolate_extension_storage(tmp_path, monkeypatch):
    """Redirect ExtensionStorage runtime data root into ``tmp_path``.

    ``ExtensionStorage`` normally resolves its data root from the AETHER
    project root; for integration we redirect it so startup/disable/uninstall
    tests never write into the repository ``data/extensions`` directory.
    """
    import agent_ai.extensions.storage as storage_mod

    root = tmp_path / "aether_runtime_data" / "extensions"
    monkeypatch.setattr(storage_mod, "_get_data_root", lambda aether_root=None: root)
    return root


def _manifest_dict(ext_id: str, version: str = "1.0.0", api_version: str = "1", extra=None) -> dict:
    data = {
        "id": ext_id,
        "name": ext_id.split(".")[-1].title(),
        "version": version,
        "description": "integration dummy",
        "api_version": api_version,
    }
    if extra:
        data.update(extra)
    return data


def _write_extension(base: Path, folder: str, manifest: dict, extension_py: str) -> Path:
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(manifest))
    (d / "extension.py").write_text(extension_py)
    (d / "__init__.py").write_text("")
    (d / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    return d


def _git_repo(base: Path, manifest: dict, extension_py: str, pyproject: str = None) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps(manifest))
    (base / "extension.py").write_text(extension_py)
    (base / "__init__.py").write_text("")
    (base / "pyproject.toml").write_text(
        pyproject or '[project]\nname = "x"\nversion = "0.1.0"\n'
    )
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "t@t.com"],
        ["git", "config", "user.name", "T"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(args, cwd=str(base), capture_output=True, check=True)
    return base


def _manifest(ext_id: str, version: str = "1.0.0") -> Manifest:
    return Manifest(
        id=ext_id, name="T", version=version, description="D", api_version="1", raw={}, source_path=""
    )


def _make_manager(tmp_path, extensions_dir: Path, cap=None, tool=None, db=None):
    reg = ExtensionRegistry()
    cap = cap if cap is not None else CapabilityRegistry()
    tool = tool if tool is not None else ToolRegistry()
    lc = ExtensionLifecycleStore(str(db or (tmp_path / "lc.db")))
    mgr = ExtensionManager(
        registry=reg,
        capability_registry=cap,
        lifecycle_store=lc,
        extensions_dir=extensions_dir,
        tool_registry=tool,
        config_store=ConfigValueStore(str(tmp_path / "cfg.db")),
    )
    return mgr, reg, cap, tool, lc


# ---------------------------------------------------------------------------
# Dummy extension source templates
# ---------------------------------------------------------------------------

_TOOL_ONLY = '''
from agent_ai.extensions import Extension
from agent_ai.tools.base import BaseTool


class EchoTool(BaseTool):
    name = "integration.tool-only.echo"
    description = "Echo a text back"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": []}

    def execute(self, text="", **kwargs):
        return {"ok": True, "text": text}


class E(Extension):
    def register(self, ctx):
        ctx.tools.register(EchoTool())


extension = E()
'''

_SKILL_ONLY = '''
from agent_ai.extensions import Extension


class E(Extension):
    def register(self, ctx):
        ctx.skills.register(
            "integration.skill-only.summarize",
            name="Summarize",
            description="Summarize a document into bullets",
            scope="project",
        )


extension = E()
'''

_KNOWLEDGE_ONLY = '''
from agent_ai.extensions import Extension


class E(Extension):
    def register(self, ctx):
        ctx.knowledge.register(
            "integration.knowledge.glossary",
            content="Domain glossary for widgets and gadgets.",
        )


extension = E()
'''

_CONFIG_ONLY = '''
from agent_ai.extensions import Extension


class E(Extension):
    def register(self, ctx):
        ctx.config.register(key="api_key", type="secret", required=False, description="API key")
        ctx.config.register(key="base_url", type="url", default="http://localhost:9000", description="Base URL")
        ctx.config.register(key="max_items", type="integer", default=10, description="Max items")


extension = E()
'''

_UI_ONLY = '''
from agent_ai.extensions import Extension


class E(Extension):
    def register(self, ctx):
        ctx.ui.register("integration.ui.form", type="form", title="Settings Form")
        ctx.ui.register("integration.ui.table", type="table", title="Results Table",
                        props={"columns": [{"key": "a"}], "rows": [{"a": 1}]})
        ctx.ui.register("integration.ui.chart", type="chart", title="Chart",
                        props={"type": "bar", "labels": ["A"], "datasets": [{"data": [1]}]})
        ctx.ui.register("integration.ui.viewer", type="viewer", title="Viewer",
                        props={"viewer_type": "image", "mime_type": "image/png"})
        ctx.ui.register("integration.ui.modal", type="modal", title="Modal")
        ctx.ui.register("integration.ui.panel", type="panel", title="Panel")
        ctx.ui.register("integration.ui.wizard", type="wizard", title="Wizard",
                        props={"steps": [{"title": "one"}, {"title": "two"}]})
        ctx.ui.register("integration.ui.result_renderer", type="result_renderer", title="Result",
                        props={"renderer": "chart"})
        ctx.ui.register("integration.ui.custom_view", type="custom_view", title="Custom",
                        entry="ui/view.html")
        ctx.ui.register("integration.ui.action", type="action", title="Run",
                        actions=[{"id": "integration.ui.run"}])


extension = E()
'''

_FULL = '''
from agent_ai.extensions import Extension
from agent_ai.tools.base import BaseTool


class PingTool(BaseTool):
    name = "integration.full.ping"
    description = "Ping"
    input_schema = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs):
        return {"pong": True}


class E(Extension):
    def register(self, ctx):
        ctx.tools.register(PingTool())
        ctx.skills.register("integration.full.skill", name="Full Skill", description="d")
        ctx.knowledge.register("integration.full.knowledge", content="domain knowledge")
        ctx.config.register(key="api_key", type="secret", required=False, description="secret")
        ctx.config.register(key="region", type="string", default="us", description="region")
        ctx.services.register("integration.full.service")
        ctx.providers.register("integration.full.provider", name="FullProvider")
        ctx.resources.register("integration.full.resource", path="resources/x.json")
        ctx.hooks.register("integration.full.on_ping", event="on_ping", handler=lambda **k: None)
        ctx.commands.register("integration.full.cmd", description="command")
        ctx.ui.register("integration.full.panel", type="panel", title="Full Panel")
        # storage is part of the extension contract too
        ctx.storage.set("boot_count", 1)


extension = E()
'''

_ANALYSIS = '''
from agent_ai.extensions import Extension
from agent_ai.tools.base import BaseTool


class GenerateTool(BaseTool):
    name = "integration.analysis.generate"
    description = "Generate a structured chart result"
    input_schema = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs):
        return {
            "type": "chart",
            "renderer": "chart",
            "data": {
                "labels": ["A", "B", "C"],
                "datasets": [{"label": "Value", "data": [10, 20, 15]}],
            },
        }


class ArtifactTool(BaseTool):
    name = "integration.analysis.artifact"
    description = "Produce an artifact reference"
    input_schema = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs):
        return {
            "type": "viewer",
            "renderer": "viewer",
            "artifact": {"artifact_id": "art-1", "mime_type": "image/png", "name": "out.png"},
        }


class E(Extension):
    def register(self, ctx):
        ctx.tools.register(GenerateTool())
        ctx.tools.register(ArtifactTool())


extension = E()
'''

_FAILING = '''
from agent_ai.extensions import Extension


class E(Extension):
    def register(self, ctx):
        ctx.tools.register("integration.failing.tool")
        raise RuntimeError("boom during register")


extension = E()
'''


def _load(ext_dir: Path, cap=None, tool=None, cfg=None, lc=None, db=None, tmp_path=None):
    reg = ExtensionRegistry()
    cap = cap if cap is not None else CapabilityRegistry()
    tool = tool if tool is not None else ToolRegistry()
    lc = lc if lc is not None else ExtensionLifecycleStore(str((tmp_path or ext_dir) / "lc.db"))
    cfg = cfg if cfg is not None else ConfigValueStore(str((tmp_path or ext_dir) / "cfg.db"))
    loader = ExtensionLoader(
        registry=reg,
        extensions_dir=ext_dir,
        enable_entry_points=False,
        capability_registry=cap,
        tool_registry=tool,
        config_store=cfg,
        lifecycle_store=lc,
    )
    result = loader.load_all()
    return loader, reg, cap, tool, lc, cfg, result


# ===========================================================================
# 3. Full startup integration
# ===========================================================================

def test_startup_discover_load_register_activate(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "tool-ext", _manifest_dict("integration.tool-only"), _TOOL_ONLY)
    _write_extension(ext_dir, "skill-ext", _manifest_dict("integration.skill-only"), _SKILL_ONLY)
    _write_extension(ext_dir, "ui-ext", _manifest_dict("integration.ui"), _UI_ONLY)

    lc = ExtensionLifecycleStore(str(tmp_path / "lc.db"))
    lc.set_enabled("integration.skill-only", False)  # disabled before startup

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, lc=lc, tmp_path=tmp_path)

    # discover all -> load/import all -> register all
    assert res.count_loaded == 3
    assert res.count_failed == 0
    assert {r.id for r in reg.all()} == {"integration.tool-only", "integration.skill-only", "integration.ui"}

    # enabled extension -> capability active
    assert cap.is_enabled("tool", "integration.tool-only.echo") is True
    assert tool.has("integration.tool-only.echo") is True

    # disabled extension still KNOWN to registry but capability NOT active
    assert reg.exists("integration.skill-only") is True
    assert cap.get("skill", "integration.skill-only.summarize").enabled is False


def test_startup_failure_isolation(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "a", _manifest_dict("integration.good-a"), _TOOL_ONLY.replace("integration.tool-only", "integration.good-a"))
    _write_extension(ext_dir, "b", _manifest_dict("integration.failing"), _FAILING)
    _write_extension(ext_dir, "c", _manifest_dict("integration.good-c"), _SKILL_ONLY.replace("integration.skill-only", "integration.good-c"))

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    assert res.count_loaded == 2
    assert res.count_failed == 1
    assert reg.exists("integration.good-a")
    assert reg.exists("integration.good-c")
    assert not reg.exists("integration.failing")
    # failed extension registered no capability (rollback)
    assert cap.exists("tool", "integration.failing.tool") is False
    # failures tracked for observability
    assert any("integration.failing" == f.get("id") for f in reg.failures())


# ===========================================================================
# 5. Tool-only extension
# ===========================================================================

def test_tool_only_extension_namespace_and_agent_registry(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "tool-ext", _manifest_dict("integration.tool-only"), _TOOL_ONLY)

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    rec = cap.get("tool", "integration.tool-only.echo")
    assert rec is not None
    assert rec.extension_id == "integration.tool-only"
    assert rec.id.startswith("integration.tool-only.")
    assert tool.has("integration.tool-only.echo")
    # executes through the existing ToolRegistry
    assert tool.execute("integration.tool-only.echo", {"text": "hi"}) == {"ok": True, "text": "hi"}


# ===========================================================================
# 6. Skill-only extension
# ===========================================================================

def test_skill_only_extension_and_progressive_loading(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "skill-ext", _manifest_dict("integration.skill-only"), _SKILL_ONLY)

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    rec = cap.get("skill", "integration.skill-only.summarize")
    assert rec is not None
    assert rec.metadata.get("description")
    # catalog metadata is lightweight (no skill body/content auto-exposed)
    assert "content" not in rec.metadata or not rec.metadata.get("content")

    # existing Skill System still works (progressive loading) — not replaced
    from agent_ai.projects.skills import SkillStore

    store = SkillStore(tmp_path / "proj")
    store.ensure()
    store.create_skill(skill_id="proj-skill", name="P", description="d", content="# body")
    assert store.get_catalog()[0].skill_id == "proj-skill"
    assert "body" in store.load_skill("proj-skill").content


# ===========================================================================
# 7. Knowledge-only extension
# ===========================================================================

def test_knowledge_only_extension(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "knowledge-ext", _manifest_dict("integration.knowledge"), _KNOWLEDGE_ONLY)

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    rec = cap.get("knowledge", "integration.knowledge.glossary")
    assert rec is not None
    assert "glossary" in rec.metadata.get("content", "").lower()
    # knowledge-only extension has no tool capability
    assert cap.list_by_extension("integration.knowledge")[0].type == "knowledge"


# ===========================================================================
# 8. Config-only extension
# ===========================================================================

def test_config_only_extension_persist_and_restart(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "config-ext", _manifest_dict("integration.config"), _CONFIG_ONLY)

    db = tmp_path / "cfg.db"
    cfg = ConfigValueStore(str(db))
    loader, reg, cap, tool, lc, _cfg, res = _load(ext_dir, cfg=cfg, tmp_path=tmp_path)

    facade = loader.capability_registry
    # read definition + default
    ctx = ExtensionContext(manifest=_manifest("integration.config"), capability_registry=cap, config_store=cfg)
    assert ctx.config.get("base_url") == "http://localhost:9000"
    ctx.config.set("max_items", 25)
    assert ctx.config.get("max_items") == 25

    # restart: fresh store/registry over the same DB keeps the value
    cfg2 = ConfigValueStore(str(db))
    assert cfg2.get("integration.config", "max_items") == 25


def test_config_secret_safety(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "config-ext", _manifest_dict("integration.config"), _CONFIG_ONLY)

    db = tmp_path / "cfg.db"
    cfg = ConfigValueStore(str(db))
    loader, reg, cap, tool, lc, _cfg, res = _load(ext_dir, cfg=cfg, tmp_path=tmp_path)

    ctx = ExtensionContext(manifest=_manifest("integration.config"), capability_registry=cap, config_store=cfg)
    assert ctx.config.is_secret("api_key") is True
    ctx.config.set("api_key", "super-secret-value")

    # secret value never appears in catalog
    catalog = ExtensionCatalog(reg).get_catalog()
    assert "super-secret-value" not in json.dumps(catalog)

    # secret value never appears in generic form schema / UI metadata
    schema = build_form_schema_for_extension("integration.config", cap, config_store=cfg)
    dumped = json.dumps(schema)
    assert "super-secret-value" not in dumped
    api_field = [f for f in schema["fields"] if f["key"] == "api_key"][0]
    assert api_field["secret"] is True
    assert api_field["configured"] is True
    assert "value" not in api_field

    # capability metadata (definition) never carries the runtime value
    rec = cap.get("config", "integration.config.api_key")
    assert "super-secret-value" not in json.dumps(rec.metadata)


# ===========================================================================
# 9. UI-only extension
# ===========================================================================

def test_ui_only_extension_catalog_and_render(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "ui-ext", _manifest_dict("integration.ui"), _UI_ONLY)

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    ui_cat = UICatalog(cap, reg)
    contribs = ui_cat.list_contributions(extension_id="integration.ui")
    types = {c.type for c in contribs}
    for expected in ("form", "table", "chart", "viewer", "modal", "panel", "wizard", "result_renderer", "custom_view", "action"):
        assert expected in types
    # serializable generic contract
    dumped = ui_cat.to_serializable()
    assert dumped["count"] == len(contribs)
    json.dumps(dumped)


# ===========================================================================
# 10. Full extension (all 11 capability groups)
# ===========================================================================

def test_full_extension_registers_all_capabilities(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "full-ext", _manifest_dict("integration.full"), _FULL)

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    caps = cap.list_by_extension("integration.full")
    types = {c.type for c in caps}
    assert set(CAPABILITY_TYPES).issubset(types)
    # resource/hook/command/provider/service are present too
    assert tool.has("integration.full.ping")
    # storage contract usable
    st = ExtensionStorage("integration.full")
    assert st.get("boot_count") == 1


# ===========================================================================
# 11. Tool -> structured result -> UI
# ===========================================================================

def test_tool_structured_result_to_ui_renderer(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "analysis-ext", _manifest_dict("integration.analysis"), _ANALYSIS)

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    result = tool.execute("integration.analysis.generate", {})
    assert result["type"] == "chart"
    renderer = resolve_renderer_for_result(result)
    assert renderer == "chart"
    assert is_renderer_available(renderer) is True
    # generic renderer, not extension-specific
    assert "integration.analysis" not in renderer


# ===========================================================================
# 12. Artifact integration
# ===========================================================================

def test_artifact_reference_to_viewer(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "analysis-ext", _manifest_dict("integration.analysis"), _ANALYSIS)

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    result = tool.execute("integration.analysis.artifact", {})
    assert result["artifact"]["artifact_id"] == "art-1"
    # MIME-based resolution works even without an explicit renderer
    assert resolve_renderer_for_result({"artifact": {"mime_type": "image/png"}}) == "image"
    assert resolve_renderer_for_result({"artifact": {"mime_type": "text/plain"}}) == "text"
    json.dumps(result)


# ===========================================================================
# 13. Config -> UI form -> tool
# ===========================================================================

def test_config_form_to_tool_flow(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()

    # Extension registers both a config definition and a tool that reads it.
    _write_extension(
        ext_dir,
        "config-ext",
        _manifest_dict("integration.config"),
        '''
from agent_ai.extensions import Extension
from agent_ai.tools.base import BaseTool


class ReadTool(BaseTool):
    name = "integration.config.read"
    description = "Read a config value"
    input_schema = {"type": "object", "properties": {}, "required": []}
    _ctx = None

    def execute(self, **kwargs):
        return {"max_items": ReadTool._ctx.config.get("max_items")}


class E(Extension):
    def register(self, ctx):
        ReadTool._ctx = ctx
        ctx.config.register(key="max_items", type="integer", default=10, description="Max items")
        ctx.tools.register(ReadTool())


extension = E()
''',
    )

    db = tmp_path / "cfg.db"
    cfg = ConfigValueStore(str(db))
    loader, reg, cap, tool, lc, _cfg, res = _load(ext_dir, cfg=cfg, tmp_path=tmp_path)

    # Backend validation through the config facade (UI form posts go here)
    ctx = ExtensionContext(manifest=_manifest("integration.config"), capability_registry=cap, config_store=cfg)
    ctx.config.set("max_items", 7)

    # Tool reads the updated config value
    assert tool.execute("integration.config.read", {}) == {"max_items": 7}
    with pytest.raises(CapabilityValidationError):
        ctx.config.register(key="bad", type="not_a_type")


# ===========================================================================
# 14. Storage integration
# ===========================================================================

def test_storage_state_cache_temp_project_and_persistence(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "full-ext", _manifest_dict("integration.full"), _FULL)
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    st = ExtensionStorage("integration.full", project_root=tmp_path)
    st.set("state", {"a": 1})
    st.cache_set("thumb", {"b": 2})
    p = st.temp_write("render.tmp", "temp")
    assert p.exists()

    proj = tmp_path / "proj"
    proj.mkdir()
    st.project(proj).set("profile", "p1")
    assert st.project(proj).get("profile") == "p1"

    # disable keeps storage
    mgr, *_ = _make_manager(tmp_path, ext_dir, cap=cap, tool=tool)
    mgr.registry.register(load_extension_from_dir(ext_dir / "full-ext"))
    mgr.disable("integration.full")
    assert st.get("state") == {"a": 1}

    # uninstall keeps storage + config
    mgr.uninstall("integration.full")
    assert ExtensionStorage("integration.full").get("state") == {"a": 1}
    assert st.project(proj).get("profile") == "p1"


# ===========================================================================
# 15. Agent integration
# ===========================================================================

def test_agent_can_use_extension_tool(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "tool-ext", _manifest_dict("integration.tool-only"), _TOOL_ONLY)
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    # Agent builds its tool definitions from the registry it is given.
    from agent_ai.core.executor import ToolExecutor
    from agent_ai.core.orchestrator import AgentOrchestrator

    specs = {s["name"] for s in tool.specs()}
    assert "integration.tool-only.echo" in specs

    orch = AgentOrchestrator(provider=MagicMock(), executor=ToolExecutor(registry=tool))
    names = {d.name for d in orch._tool_definitions()}
    assert "integration.tool-only.echo" in names

    # Agent executes the extension tool -> result
    out = orch.executor.registry.execute("integration.tool-only.echo", {"text": "hello"})
    assert out["text"] == "hello"


def test_disable_removes_tool_from_agent_registry(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "tool-ext", _manifest_dict("integration.tool-only"), _TOOL_ONLY)

    cap = CapabilityRegistry()
    tool = ToolRegistry()
    lc = ExtensionLifecycleStore(str(tmp_path / "lc.db"))
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, cap=cap, tool=tool, lc=lc, tmp_path=tmp_path)

    mgr = ExtensionManager(
        registry=reg, capability_registry=cap, lifecycle_store=lc,
        extensions_dir=ext_dir, tool_registry=tool,
        config_store=ConfigValueStore(str(tmp_path / "cfg.db")),
    )
    assert tool.has("integration.tool-only.echo")

    mgr.disable("integration.tool-only")
    assert tool.has("integration.tool-only.echo") is False
    assert cap.is_enabled("tool", "integration.tool-only.echo") is False

    # 17. enable -> capability active again (tool must return to the registry)
    mgr.enable("integration.tool-only")
    assert cap.is_enabled("tool", "integration.tool-only.echo") is True
    assert tool.has("integration.tool-only.echo") is True
    assert tool.execute("integration.tool-only.echo", {"text": "back"})["text"] == "back"


# ===========================================================================
# 16. Consultant boundary
# ===========================================================================

def test_consultant_boundary_uses_audience_metadata(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(
        ext_dir,
        "audience-ext",
        _manifest_dict("integration.readonly"),
        '''
from agent_ai.extensions import Extension
from agent_ai.tools.base import BaseTool


class Analyzer(BaseTool):
    name = "integration.readonly.analyze"
    description = "read-only analysis"
    input_schema = {"type": "object", "properties": {}, "required": []}
    audience = "consultant"

    def execute(self, **kwargs):
        return {"ok": True}


class E(Extension):
    def register(self, ctx):
        ctx.tools.register("integration.readonly.write", audience="agent", description="write-ish")
        ctx.tools.register(Analyzer())


extension = E()
''',
    )
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    read_only = cap.get("tool", "integration.readonly.analyze")
    write_like = cap.get("tool", "integration.readonly.write")
    assert read_only.audience == "consultant"
    assert write_like.audience == "agent"

    # Consultant registry stays read-only: it does NOT auto-include extension write tools.
    from agent_ai.consultant.tools import build_consultant_registry

    consultant_reg = build_consultant_registry(root=tmp_path)
    assert consultant_reg.has("integration.readonly.write") is False
    assert consultant_reg.has("integration.readonly.analyze") is False


# ===========================================================================
# 17. Enable/disable integration (status + UI)
# ===========================================================================

def test_enable_disable_ui_availability(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "ui-ext", _manifest_dict("integration.ui"), _UI_ONLY)

    cap = CapabilityRegistry()
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, cap=cap, tmp_path=tmp_path)
    mgr = ExtensionManager(
        registry=reg, capability_registry=cap, lifecycle_store=lc,
        extensions_dir=ext_dir, tool_registry=tool,
        config_store=ConfigValueStore(str(tmp_path / "cfg.db")),
    )

    ui_cat = UICatalog(cap, reg)
    assert ui_cat.count(enabled_only=True) == 10

    mgr.disable("integration.ui")
    assert ui_cat.count(enabled_only=True) == 0
    assert mgr.get_status("integration.ui")["status"] == "disabled"

    mgr.enable("integration.ui")
    assert ui_cat.count(enabled_only=True) == 10
    assert mgr.get_status("integration.ui")["status"] == "enabled"


# ===========================================================================
# 18. Update integration
# ===========================================================================

def test_update_v1_to_v2_same_id(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-integration-up-v1"
    _git_repo(repo_v1, _manifest_dict("integration.updated", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.updated"))
    repo_v2 = tmp_path / "repo-integration-up-v2"
    _git_repo(repo_v2, _manifest_dict("integration.updated", "2.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.updated"))

    mgr, reg, cap, tool, lc = _make_manager(tmp_path, ext_dir)
    mgr.install(str(repo_v1))
    assert reg.get("integration.updated").manifest.version == "1.0.0"

    mgr.update("integration.updated", repository_url=str(repo_v2))
    assert reg.get("integration.updated").manifest.version == "2.0.0"
    assert (ext_dir / repo_v1.name / "manifest.json").is_file()
    # last folder name wins; verify the manifest on disk is v2 under the installed folder
    status = mgr.get_status("integration.updated")
    assert status["enabled"] is True


def test_update_rejects_identity_mismatch(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-integration-mismatch-v1"
    _git_repo(repo_v1, _manifest_dict("integration.mismatch", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.mismatch"))
    repo_other = tmp_path / "repo-integration-other"
    _git_repo(repo_other, _manifest_dict("integration.different", "2.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.different"))

    mgr, reg, cap, tool, lc = _make_manager(tmp_path, ext_dir)
    mgr.install(str(repo_v1))
    with pytest.raises(ExtensionUpdateError, match="manifest ID mismatch"):
        mgr.update("integration.mismatch", repository_url=str(repo_other))
    # old package preserved
    assert (ext_dir / repo_v1.name / "manifest.json").is_file()
    assert json.loads((ext_dir / repo_v1.name / "manifest.json").read_text())["version"] == "1.0.0"


def test_update_commit_failure_restores_old_package(tmp_path, monkeypatch):
    """Regression: rollback must actually restore the previous package.

    A failure while *committing* the new package (after the old one has been
    replaced) must restore the previous on-disk package. Previously the backup
    directory was created but never populated, so the old package was lost.
    """
    import agent_ai.extensions.manager as mgr_mod

    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-integration-restore-v1"
    _git_repo(repo_v1, _manifest_dict("integration.restore", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.restore"))
    repo_v2 = tmp_path / "repo-integration-restore-v2"
    _git_repo(repo_v2, _manifest_dict("integration.restore", "2.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.restore"))

    mgr, reg, cap, tool, lc = _make_manager(tmp_path, ext_dir)
    mgr.install(str(repo_v1))
    target = ext_dir / repo_v1.name
    assert target.is_dir()

    real_load = mgr_mod.load_extension_from_dir

    def _flaky(path):
        if Path(path).resolve() == target.resolve():
            raise RuntimeError("simulated post-move commit failure")
        return real_load(path)

    monkeypatch.setattr(mgr_mod, "load_extension_from_dir", _flaky)

    with pytest.raises(ExtensionUpdateError):
        mgr.update("integration.restore", repository_url=str(repo_v2))

    # old package restored
    assert target.is_dir()
    assert json.loads((target / "manifest.json").read_text())["version"] == "1.0.0"
    assert reg.get("integration.restore").manifest.version == "1.0.0"


# ===========================================================================
# 19. Uninstall integration
# ===========================================================================

def test_uninstall_removes_capabilities_but_keeps_config_and_state(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    repo = tmp_path / "repo-integration-uninstall"
    _git_repo(repo, _manifest_dict("integration.uninstall", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.uninstall"))

    mgr, reg, cap, tool, lc = _make_manager(tmp_path, ext_dir)
    mgr.install(str(repo))
    installed = ext_dir / repo.name
    assert installed.is_dir()
    assert tool.has("integration.uninstall.echo")

    cfg = ConfigValueStore(str(tmp_path / "cfg.db"))
    cfg.set("integration.uninstall", "api_key", "kept-secret")
    st = ExtensionStorage("integration.uninstall")
    st.set("state", {"k": "v"})

    # a second, unrelated extension must not be affected
    repo_b = tmp_path / "repo-integration-bystander"
    _git_repo(repo_b, _manifest_dict("integration.bystander", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.bystander"))
    mgr.install(str(repo_b))

    mgr.uninstall("integration.uninstall")

    assert not reg.exists("integration.uninstall")
    assert not installed.exists()
    assert cap.list_by_extension("integration.uninstall") == []
    assert tool.has("integration.uninstall.echo") is False
    # config + storage preserved
    assert cfg.get("integration.uninstall", "api_key") == "kept-secret"
    assert ExtensionStorage("integration.uninstall").get("state") == {"k": "v"}
    # bystander untouched
    assert reg.exists("integration.bystander")
    assert tool.has("integration.bystander.echo") is True


# ===========================================================================
# 20. Git install through UI service
# ===========================================================================

def _ensure_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    sys.path.insert(0, str(_ROOT / "web" / "django_app"))
    from django.conf import settings

    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS = ["testserver", "127.0.0.1", "localhost"] + list(settings.ALLOWED_HOSTS)
    import django

    try:
        django.setup()
    except RuntimeError:
        pass


def test_git_install_through_ui_service(tmp_path, monkeypatch):
    _ensure_django()
    import agent_ai.extensions.config as config_mod
    import api.services as services_mod

    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    monkeypatch.setenv("AETHER_EXTENSIONS_DIR", str(ext_dir))

    repo = tmp_path / "repo-integration-uipath"
    _git_repo(repo, _manifest_dict("integration.uipath", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.uipath"))

    cap = CapabilityRegistry()
    tool = ToolRegistry()
    lc = ExtensionLifecycleStore(str(tmp_path / "lc.db"))
    cfg = ConfigValueStore(str(tmp_path / "cfg.db"))
    monkeypatch.setattr(config_mod, "get_config_store", lambda *a, **k: cfg)

    service = services_mod.GatewayService(
        project_registry=MagicMock(),
        task_preparation=MagicMock(),
        session_store=MagicMock(),
        project_store=MagicMock(),
    )
    service._ext_capability_registry = cap
    service._ext_registry = ExtensionRegistry()
    service._ext_lifecycle_store = lc
    service._ext_tool_registry = tool

    # Sidebar -> Install Extension -> Git URL -> backend manager
    result = service.install_extension(str(repo))
    assert result["id"] == "integration.uipath"
    assert result["status"] in ("enabled", "loaded")

    listing = service.list_extensions()
    ids = [e["id"] for e in listing["extensions"]]
    assert "integration.uipath" in ids

    # sidebar filters (All / Enabled / Disabled / Failed) work off the same source
    enabled = [e for e in listing["extensions"] if e["status"] == "enabled"]
    assert "integration.uipath" in [e["id"] for e in enabled]
    disabled = [e for e in listing["extensions"] if e["status"] == "disabled"]
    assert "integration.uipath" not in [e["id"] for e in disabled]

    # disable/enable reflected in status
    assert service.disable_extension("integration.uipath")["status"] == "disabled"
    assert service.enable_extension("integration.uipath")["status"] == "enabled"

    # uninstall / cleanup
    assert service.uninstall_extension("integration.uipath")["status"] == "uninstalled"
    assert not (ext_dir / repo.name).exists()


# ===========================================================================
# 21. Windows cleanup regression (Task 07 preserved)
# ===========================================================================

def test_windows_cleanup_regression_readonly_git_objects(tmp_path, monkeypatch):
    import agent_ai.extensions.manager as mgr_mod

    repo = _git_repo(tmp_path / "repo-integration-win", _manifest_dict("integration.win", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.win"))
    frozen = 0
    for obj in (repo / ".git" / "objects").rglob("*"):
        if obj.is_file():
            os.chmod(obj, stat.S_IREAD)
            frozen += 1
    assert frozen > 0

    # force the cross-device fallback path (the previously buggy branch)
    monkeypatch.setattr(mgr_mod.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError(18, "cross-device")))

    ext_dir = tmp_path / "Extension"
    mgr, reg, cap, tool, lc = _make_manager(tmp_path, ext_dir)

    staging_parent = Path(mgr_mod.tempfile.gettempdir())
    before = {p.name for p in staging_parent.glob("aether_ext_staging_*")}

    result = mgr.install(str(repo))
    assert result["id"] == "integration.win"
    installed = ext_dir / repo.name
    assert installed.is_dir()
    assert not (installed / ".git").exists()

    after = {p.name for p in staging_parent.glob("aether_ext_staging_*")}
    assert after <= before, f"staging leaked: {after - before}"


# ===========================================================================
# 22. Sidebar integration
# ===========================================================================

def test_sidebar_list_and_filters(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "a", _manifest_dict("integration.enabled-one"), _TOOL_ONLY.replace("integration.tool-only", "integration.enabled-one"))
    _write_extension(ext_dir, "b", _manifest_dict("integration.disabled-one"), _TOOL_ONLY.replace("integration.tool-only", "integration.disabled-one"))

    lc = ExtensionLifecycleStore(str(tmp_path / "lc.db"))
    lc.set_enabled("integration.disabled-one", False)

    cap = CapabilityRegistry()
    tool = ToolRegistry()
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, cap=cap, tool=tool, lc=lc, tmp_path=tmp_path)
    mgr = ExtensionManager(
        registry=reg, capability_registry=cap, lifecycle_store=lc,
        extensions_dir=ext_dir, tool_registry=tool,
        config_store=ConfigValueStore(str(tmp_path / "cfg.db")),
    )
    items = mgr.list_installed()

    # statuses correct, and the two statuses differ
    by_id = {i["id"]: i for i in items}
    assert by_id["integration.enabled-one"]["status"] == "enabled"
    assert by_id["integration.disabled-one"]["status"] == "disabled"

    # sidebar filters
    enabled = [i["id"] for i in items if i["status"] == "enabled"]
    disabled = [i["id"] for i in items if i["status"] == "disabled"]
    assert "integration.enabled-one" in enabled
    assert "integration.disabled-one" in disabled
    assert [i["id"] for i in items] == sorted([i["id"] for i in items])  # stable order


# ===========================================================================
# 23. Catalog consistency
# ===========================================================================

def test_catalog_consistency_single_source_of_truth(tmp_path, monkeypatch):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "a", _manifest_dict("integration.tool-only"), _TOOL_ONLY)
    _write_extension(ext_dir, "b", _manifest_dict("integration.skill-only"), _SKILL_ONLY)

    lc = ExtensionLifecycleStore(str(tmp_path / "lc.db"))
    cap = CapabilityRegistry()
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, cap=cap, lc=lc, tmp_path=tmp_path)
    lc.set_enabled("integration.skill-only", False)

    # Catalog is derived from the registry (same id set), no frontend scanning.
    catalog = ExtensionCatalog(reg)
    catalog_ids = {e["id"] for e in catalog.get_catalog()["extensions"]}
    assert catalog_ids == {r.id for r in reg.all()}

    # Catalog status reflects the lifecycle store (single source of truth).
    monkeypatch.setattr(
        "agent_ai.extensions.lifecycle.get_lifecycle_store", lambda *a, **k: lc
    )
    statuses = {e["id"]: e["status"] for e in catalog.get_catalog()["extensions"]}
    assert statuses["integration.skill-only"] == "disabled"


# ===========================================================================
# 24. Restart validation
# ===========================================================================

def test_restart_restores_loaded_and_enabled_state(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "a", _manifest_dict("integration.restart"), _TOOL_ONLY.replace("integration.tool-only", "integration.restart"))

    db = tmp_path / "lc.db"
    # First run: load + disable
    cap1 = CapabilityRegistry()
    tool1 = ToolRegistry()
    lc1 = ExtensionLifecycleStore(str(db))
    _load(ext_dir, cap=cap1, tool=tool1, lc=lc1, tmp_path=tmp_path)
    mgr1 = ExtensionManager(
        registry=ExtensionRegistry(), capability_registry=cap1, lifecycle_store=lc1,
        extensions_dir=ext_dir, tool_registry=tool1,
        config_store=ConfigValueStore(str(tmp_path / "cfg.db")),
    )
    # disable via a manager bound to the same registries the loader populated
    lc1.set_enabled("integration.restart", False)

    # Second run (fresh registries, same lifecycle DB) -> extension known but disabled
    cap2 = CapabilityRegistry()
    tool2 = ToolRegistry()
    lc2 = ExtensionLifecycleStore(str(db))
    loader2, reg2, cap2, tool2, lc2, cfg2, res2 = _load(ext_dir, cap=cap2, tool=tool2, lc=lc2, tmp_path=tmp_path)
    assert reg2.exists("integration.restart") is True
    assert cap2.get("tool", "integration.restart.echo").enabled is False
    assert tool2.has("integration.restart.echo") is False


def test_restart_config_survives(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "config-ext", _manifest_dict("integration.config"), _CONFIG_ONLY)
    db = tmp_path / "cfg.db"
    cfg1 = ConfigValueStore(str(db))
    loader, reg, cap, tool, lc, _cfg, res = _load(ext_dir, cfg=cfg1, tmp_path=tmp_path)
    ctx = ExtensionContext(manifest=_manifest("integration.config"), capability_registry=cap, config_store=cfg1)
    ctx.config.set("base_url", "http://changed:1234")

    cfg2 = ConfigValueStore(str(db))
    ctx2 = ExtensionContext(manifest=_manifest("integration.config"), capability_registry=cap, config_store=cfg2)
    assert ctx2.config.get("base_url") == "http://changed:1234"


# ===========================================================================
# 25. Large extension count
# ===========================================================================

def test_many_extensions_startup(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    n = 40
    for i in range(n):
        eid = f"integration.bulk{i:03d}"
        _write_extension(
            ext_dir,
            f"bulk-{i:03d}",
            _manifest_dict(eid),
            _TOOL_ONLY.replace("integration.tool-only", eid),
        )
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)
    assert res.count_loaded == n
    assert res.count_failed == 0
    assert ExtensionCatalog(reg).get_catalog()["count"] == n
    assert cap.count("tool") == n


# ===========================================================================
# 26. Failure matrix
# ===========================================================================

def test_failure_matrix(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()

    # (1) manifest invalid -> only that extension fails
    d = ext_dir / "invalid"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"id": "integration.invalid", "name": "x"}))
    (d / "extension.py").write_text("from agent_ai.extensions import Extension\nextension = Extension()\n")

    # (2) API incompatible -> only that extension fails (startup validation)
    _write_extension(ext_dir, "incompatible", _manifest_dict("integration.incompat", api_version="999"), _TOOL_ONLY.replace("integration.tool-only", "integration.incompat"))

    # (3) duplicate id -> one rejected, other loaded
    _write_extension(ext_dir, "dup-a", _manifest_dict("integration.dup"), _TOOL_ONLY.replace("integration.tool-only", "integration.dup"))
    _write_extension(ext_dir, "dup-b", _manifest_dict("integration.dup"), _TOOL_ONLY.replace("integration.tool-only", "integration.dup"))

    # a healthy extension keeps the run alive
    _write_extension(ext_dir, "healthy", _manifest_dict("integration.healthy"), _TOOL_ONLY.replace("integration.tool-only", "integration.healthy"))

    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)

    assert reg.exists("integration.healthy")
    assert not reg.exists("integration.invalid")
    assert not reg.exists("integration.incompat")
    assert reg.exists("integration.dup")
    assert res.count_failed >= 3

    # (4) duplicate capability -> register raises, extension isolated
    cap2 = CapabilityRegistry()
    manifest = _manifest("integration.dupext")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap2)
    ctx.tools.register("integration.dupext.tool")
    with pytest.raises(DuplicateCapabilityError):
        ctx.tools.register("integration.dupext.tool")

    # (5) register failure -> partial capabilities rolled back by loader
    ext_dir2 = tmp_path / "Extension2"
    ext_dir2.mkdir()
    _write_extension(ext_dir2, "partial", _manifest_dict("integration.partial"), _FAILING)
    loader2, reg2, cap2b, tool2b, lc2, cfg2, res2 = _load(ext_dir2, tmp_path=tmp_path)
    assert not cap2b.exists("tool", "integration.partial.tool")
    assert not reg2.exists("integration.partial")

    # (6) enable failure -> extension inactive
    ext_dir3 = tmp_path / "Extension3"
    ext_dir3.mkdir()
    _write_extension(
        ext_dir3,
        "bad-enable",
        _manifest_dict("integration.bad-enable"),
        '''
from agent_ai.extensions import Extension


class E(Extension):
    def register(self, ctx):
        ctx.tools.register("integration.bad-enable.tool")

    def enable(self, ctx):
        raise RuntimeError("enable boom")


extension = E()
''',
    )
    cap3 = CapabilityRegistry()
    tool3 = ToolRegistry()
    lc3 = ExtensionLifecycleStore(str(tmp_path / "lc3.db"))
    loader3, reg3, cap3, tool3, lc3, cfg3, res3 = _load(ext_dir3, cap=cap3, tool=tool3, lc=lc3, tmp_path=tmp_path)
    mgr3 = ExtensionManager(
        registry=reg3, capability_registry=cap3, lifecycle_store=lc3,
        extensions_dir=ext_dir3, tool_registry=tool3,
        config_store=ConfigValueStore(str(tmp_path / "cfg3.db")),
    )
    with pytest.raises(ExtensionLifecycleError):
        mgr3.enable("integration.bad-enable")
    assert cap3.is_enabled("tool", "integration.bad-enable.tool") is False

    # (7) dependency failure -> install rejected
    repo_dep = tmp_path / "repo-integration-dep"
    _git_repo(
        repo_dep,
        _manifest_dict("integration.dep", "1.0.0"),
        _TOOL_ONLY.replace("integration.tool-only", "integration.dep"),
        pyproject='[project]\nname="x"\nversion="0.1.0"\ndependencies=["__fail_dependency_install__"]\n',
    )
    mgr, *_ = _make_manager(tmp_path, tmp_path / "Extension7")
    with pytest.raises(ExtensionInstallError):
        mgr.install(str(repo_dep))

    # (8) invalid package structure -> install rejected
    bad = tmp_path / "repo-integration-badstruct"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps(_manifest_dict("integration.badstruct")))
    for a in (["git", "init"], ["git", "config", "user.email", "t@t.com"], ["git", "config", "user.name", "T"], ["git", "add", "."], ["git", "commit", "-m", "i"]):
        subprocess.run(a, cwd=str(bad), capture_output=True, check=True)
    with pytest.raises(ExtensionValidationError):
        mgr.install(str(bad))


# ===========================================================================
# 27. No hardcoded extension logic (source-level regression)
# ===========================================================================

def test_no_hardcoded_extension_logic_in_core():
    import re

    core_files = list((_ROOT / "src" / "agent_ai" / "extensions").glob("*.py"))
    core_files.append(_ROOT / "web" / "django_app" / "api" / "services.py")
    product_names = ("comfyui", "veo3", "excel", "selenium", "tiktok", "stable-diffusion")
    for path in core_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        low = text.lower()
        assert not re.search(r'extension_id\s*==\s*["\']', text), f"hardcoded extension_id in {path}"
        assert not re.search(r'extension_name\s*==\s*["\']', text), f"hardcoded extension_name in {path}"
        for name in product_names:
            assert name not in low, f"hardcoded extension name {name!r} in {path}"


def test_no_heuristic_capability_selection():
    core_files = list((_ROOT / "src" / "agent_ai" / "extensions").glob("*.py"))
    forbidden = ("auto-selector", "auto selector", "capability recommendation", "extension classifier", "keyword matcher")
    for path in core_files:
        low = path.read_text(encoding="utf-8", errors="ignore").lower()
        for token in forbidden:
            assert token not in low, f"heuristic {token!r} in {path}"


# ===========================================================================
# 28. Capability type coverage
# ===========================================================================

def test_capability_type_coverage(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "full-ext", _manifest_dict("integration.full"), _FULL)
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)
    covered = {rec.type for rec in cap.list_by_extension("integration.full")}
    assert set(CAPABILITY_TYPES) == covered


# ===========================================================================
# 31. Observability + secret safety
# ===========================================================================

def test_observability_activity_and_secret_sanitized(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    mgr, reg, cap, tool, lc = _make_manager(tmp_path, ext_dir)

    repo = tmp_path / "repo-integration-obs"
    _git_repo(repo, _manifest_dict("integration.obs", "1.0.0"), _TOOL_ONLY.replace("integration.tool-only", "integration.obs"))
    mgr.install(str(repo))

    rows = lc.list_activity("integration.obs")
    assert any(r["event"] == "extension_installed" for r in rows)
    for r in rows:
        assert r["extension_id"] == "integration.obs"

    # secrets passed to the event sink are redacted by the existing sanitizer
    mgr._emit("integration_probe", {"extension_id": "integration.obs", "api_key": "TOP-SECRET", "capability_type": "tool"})
    all_detail = json.dumps(lc.list_activity("integration.obs"))
    assert "TOP-SECRET" not in all_detail
    assert "extension_id" in all_detail  # id/type/status still identifiable


# ===========================================================================
# 33. Token efficiency / compression invariant
# ===========================================================================

def test_compression_switch_still_disabled():
    from agent_ai.config.settings import compression_enabled

    assert compression_enabled() is False


def test_catalog_is_lightweight_metadata_only(tmp_path):
    ext_dir = tmp_path / "Extension"
    ext_dir.mkdir()
    _write_extension(ext_dir, "full-ext", _manifest_dict("integration.full"), _FULL)
    loader, reg, cap, tool, lc, cfg, res = _load(ext_dir, tmp_path=tmp_path)
    dumped = json.dumps(ExtensionCatalog(reg).get_catalog())
    # no source code / skill body / knowledge content in the catalog payload
    assert "class E" not in dumped
    assert "def register" not in dumped
    assert "domain knowledge" not in dumped


# ===========================================================================
# 29 / 30. Realistic Extension example mapping (contract representability)
# ===========================================================================

#: Example mapping from the task spec. Each entry maps a realistic Extension
#: concept onto the GENERIC capability contract — no core change per name.
_EXAMPLE_MAPPING = {
    "example.browser": {"tool", "service", "ui", "config"},
    "example.pdf": {"tool", "ui", "resource"},
    "example.excel": {"tool", "ui"},
    "example.veo": {"provider", "service", "tool", "config", "ui"},
    "example.comfyui": {"service", "tool", "resource", "ui"},
    "example.tts": {"provider", "service", "tool", "ui"},
    "example.websearch": {"tool", "config"},
    "example.tiktok": {"tool", "service", "ui"},
    "example.dataanalysis": {"tool", "skill", "ui"},
    "example.domainknowledge": {"knowledge", "skill"},
}


def test_realistic_extension_example_mapping_representable():
    cap = CapabilityRegistry()
    for ext_id, types in _EXAMPLE_MAPPING.items():
        manifest = _manifest(ext_id)
        ctx = ExtensionContext(manifest=manifest, capability_registry=cap)
        if "tool" in types:
            ctx.tools.register(f"{ext_id}.tool")
        if "skill" in types:
            ctx.skills.register(f"{ext_id}.skill", name="S", description="d")
        if "knowledge" in types:
            ctx.knowledge.register(f"{ext_id}.knowledge", content="domain")
        if "config" in types:
            ctx.config.register(key="api_key", type="secret", required=False, description="key")
        if "service" in types:
            ctx.services.register(f"{ext_id}.service")
        if "provider" in types:
            ctx.providers.register(f"{ext_id}.provider", name="P")
        if "resource" in types:
            ctx.resources.register(f"{ext_id}.resource", path="r.json")
        if "ui" in types:
            ctx.ui.register(f"{ext_id}.viewer", type="viewer", title="Viewer",
                            props={"viewer_type": "image"})
        got = {rec.type for rec in cap.list_by_extension(ext_id)}
        assert types.issubset(got), f"{ext_id}: expected {types}, got {got}"

    # viewer/result rendering used by the examples is generic and resolvable
    assert resolve_renderer_for_result({"artifact": {"mime_type": "video/mp4"}}) == "video"
    assert resolve_renderer_for_result({"artifact": {"mime_type": "audio/mpeg"}}) == "audio"
    assert resolve_renderer_for_result({"artifact": {"mime_type": "application/pdf"}}) == "pdf"
    assert resolve_renderer_for_result({"type": "table", "data": {}}) == "table"
    assert resolve_renderer_for_result({"type": "chart", "data": {}}) == "chart"
    for r in ("table", "chart", "image", "video", "audio", "pdf", "viewer"):
        assert is_renderer_available(r) is True
        assert r in RENDERER_TYPES

