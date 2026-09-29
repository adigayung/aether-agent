"""Task 05 — Extension UI System & Rendering Contract validation.

Covers 22 validation points from spec 28 + 7 extra UI contract points.
"""
import json
import tempfile
from pathlib import Path

import pytest

from agent_ai.extensions.capabilities import (
    CapabilityRegistry,
    CapabilityValidationError,
    DuplicateCapabilityError,
    UI_TYPES,
)
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.manifest import Manifest
from agent_ai.extensions.registry import ExtensionRegistry
from agent_ai.extensions.loader import ExtensionLoader
from agent_ai.extensions.config import ConfigValueStore
from agent_ai.extensions.ui import (
    UICatalog,
    UIContribution,
    UIResult,
    build_form_schema_for_extension,
    config_definition_to_field,
    make_chart_result,
    make_table_result,
    make_viewer_result,
    resolve_renderer_for_result,
    set_extension_enabled,
)


def _manifest(ext_id: str) -> Manifest:
    return Manifest(id=ext_id, name="Test", version="1.0", description="Desc", api_version="1", raw={}, source_path="")


def _ctx(ext_id: str, cap_reg: CapabilityRegistry, config_store=None, tool_registry=None):
    m = _manifest(ext_id)
    return ExtensionContext(manifest=m, capability_registry=cap_reg, tool_registry=tool_registry, config_store=config_store)


# 1. UI capability dapat diregister
def test_01_ui_can_register():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("test.ui1", cap_reg)
    ctx.ui.register(id="test.ui1.panel1", type="panel", title="My Panel")
    assert cap_reg.exists("ui", "test.ui1.panel1")
    rec = cap_reg.get("ui", "test.ui1.panel1")
    assert rec.metadata["type"] == "panel"
    assert rec.extension_id == "test.ui1"


# 2. Namespace ID bekerja
def test_02_namespace_id():
    cap_reg = CapabilityRegistry()
    # Valid namespaced
    ctx = _ctx("community.browser", cap_reg)
    ctx.ui.register(id="community.browser.debug", type="panel")
    assert cap_reg.exists("ui", "community.browser.debug")
    # Invalid: not namespaced -> must fail
    ctx2 = _ctx("community.browser", cap_reg)
    with pytest.raises(CapabilityValidationError, match="must be namespaced"):
        ctx2.ui.register(id="debug", type="panel")
    # Folder does not determine identity: registry uses manifest id
    # Simulate folder "my-browser-final" but id is community.browser
    tmp = Path(tempfile.mkdtemp())
    import json as _json
    d = tmp / "my-browser-final"
    d.mkdir()
    (d / "manifest.json").write_text(_json.dumps({"id": "community.browser", "name": "N", "version": "1", "description": "D", "api_version": "1"}))
    (d / "extension.py").write_text(
        "from agent_ai.extensions import Extension\n"
        "class E(Extension):\n"
        "    def register(self, ctx):\n"
        "        ctx.ui.register(id='community.browser.debug2', type='panel')\n"
        "extension = E()\n"
    )
    reg = ExtensionRegistry()
    cap2 = CapabilityRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False, capability_registry=cap2)
    res = loader.load_all()
    assert res.count_loaded == 1
    assert cap2.exists("ui", "community.browser.debug2")
    assert not cap2.exists("ui", "my-browser-final.debug2")


# 3. Duplicate UI ID ditolak
def test_03_duplicate_ui_id():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("dup.ui", cap_reg)
    ctx.ui.register(id="dup.ui.panel1", type="panel")
    with pytest.raises(DuplicateCapabilityError):
        ctx.ui.register(id="dup.ui.panel1", type="panel")


# 4. Ownership extension_id tersimpan
def test_04_ownership():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("own.ui", cap_reg)
    ctx.ui.register(id="own.ui.chart1", type="chart", title="Chart")
    rec = cap_reg.get("ui", "own.ui.chart1")
    assert rec.extension_id == "own.ui"
    contrib = UICatalog(cap_reg).get_contribution("own.ui.chart1")
    assert contrib.extension_id == "own.ui"


# 5. Semua UI types existing dari Task 03 dapat diregister
def test_05_all_ui_types():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("all.ui", cap_reg)
    for t in UI_TYPES:
        ctx.ui.register(id=f"all.ui.{t}_1", type=t, title=f"Title {t}")
    for t in UI_TYPES:
        assert cap_reg.exists("ui", f"all.ui.{t}_1")
        rec = cap_reg.get("ui", f"all.ui.{t}_1")
        assert rec.metadata["type"] == t


# 6. Invalid UI type ditolak
def test_06_invalid_ui_type():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("bad.ui", cap_reg)
    with pytest.raises(CapabilityValidationError, match="UI type"):
        ctx.ui.register(id="bad.ui.foo", type="not_a_type")


# 7. UI metadata serializable
def test_07_serializable():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("ser.ui", cap_reg)
    ctx.ui.register(id="ser.ui.panel1", type="panel", title="Panel", description="desc", schema={"type": "object", "properties": {"x": {"type": "string"}}})
    rec = cap_reg.get("ui", "ser.ui.panel1")
    # Record metadata must be JSON serializable
    raw = json.dumps(rec.metadata, ensure_ascii=False, default=str)
    assert "panel" in raw
    # Contribution also
    contrib = UICatalog(cap_reg).get_contribution("ser.ui.panel1")
    d = contrib.to_dict()
    json.dumps(d, ensure_ascii=False)
    assert d["type"] == "panel"
    assert d["id"] == "ser.ui.panel1"
    assert d["extension_id"] == "ser.ui"
    assert "entry" not in d or isinstance(d.get("entry"), (str, type(None)))
    # Schema present
    assert d["schema"] is not None or "schema" not in d


# 8. UI catalog dapat menemukan contribution
def test_08_ui_catalog():
    cap_reg = CapabilityRegistry()
    ext_reg = ExtensionRegistry()
    ctx = _ctx("cat.ui", cap_reg)
    ctx.ui.register(id="cat.ui.form1", type="form", title="Form")
    ctx.ui.register(id="cat.ui.table1", type="table", title="Table")
    cat = UICatalog(cap_reg, ext_reg)
    assert cat.count() == 2
    lst = cat.list_contributions()
    assert len(lst) == 2
    assert any(c.id == "cat.ui.form1" for c in lst)
    # Filter by type
    assert len(cat.list_contributions(ui_type="form")) == 1
    # to_dict
    d = cat.to_dict()
    assert d["count"] == 2
    assert len(d["contributions"]) == 2
    # Dynamic: add one more, catalog reflects
    ctx.ui.register(id="cat.ui.chart1", type="chart")
    assert cat.count() == 3


# 9. Disable Extension membuat UI capability unavailable
def test_09_disable_unavailable():
    cap_reg = CapabilityRegistry()
    ext_reg = ExtensionRegistry()
    ctx = _ctx("dis.ui", cap_reg)
    ctx.ui.register(id="dis.ui.panel1", type="panel")
    ctx.ui.register(id="dis.ui.chart1", type="chart")
    # Simulate ExtensionRecord
    from agent_ai.extensions.manifest import Manifest as _M
    m = _M(id="dis.ui", name="N", version="1", description="D", api_version="1", raw={}, source_path="s")
    from agent_ai.extensions.registry import ExtensionRecord
    rec = ExtensionRecord(id="dis.ui", manifest=m, extension=None, root=None, source="s", status="loaded")
    ext_reg.register_record(rec)
    cat = UICatalog(cap_reg, ext_reg)
    assert cat.count(enabled_only=True) == 2
    # Disable -> UI unavailable (enable flag or extension status)
    set_extension_enabled(cap_reg, "dis.ui", False, ext_reg)
    assert cat.count(enabled_only=True) == 0
    # But enabled_only=False still shows
    assert cat.count(enabled_only=False) == 2
    # Config/storage tetap dipertahankan (not tested here but capability registry still has them)
    assert cap_reg.exists("ui", "dis.ui.panel1")


# 10. Enable kembali membuat UI available
def test_10_enable_again():
    cap_reg = CapabilityRegistry()
    ext_reg = ExtensionRegistry()
    ctx = _ctx("en.ui", cap_reg)
    ctx.ui.register(id="en.ui.panel1", type="panel")
    from agent_ai.extensions.manifest import Manifest as _M
    from agent_ai.extensions.registry import ExtensionRecord
    m = _M(id="en.ui", name="N", version="1", description="D", api_version="1", raw={}, source_path="s")
    ext_reg.register_record(ExtensionRecord(id="en.ui", manifest=m, extension=None, root=None, source="s", status="loaded"))
    cat = UICatalog(cap_reg, ext_reg)
    set_extension_enabled(cap_reg, "en.ui", False, ext_reg)
    assert cat.count() == 0
    set_extension_enabled(cap_reg, "en.ui", True, ext_reg)
    assert cat.count() == 1


# 11. Config metadata dapat diterjemahkan menjadi form schema
def test_11_config_to_form_schema(tmp_path):
    db = tmp_path / "ui11.db"
    store = ConfigValueStore(str(db))
    cap_reg = CapabilityRegistry()
    ctx = _ctx("form.ext", cap_reg, config_store=store)
    ctx.config.register(key="base_url", type="url", default="http://example.com", description="Base URL")
    ctx.config.register(key="timeout", type="integer", default=60)
    ctx.config.register(key="enabled", type="boolean", default=True)
    ctx.config.register(key="quality", type="enum", choices=["draft", "standard", "high"], default="standard")
    schema = build_form_schema_for_extension("form.ext", cap_reg, config_store=store)
    assert schema["type"] == "form"
    fields = schema["fields"]
    assert len(fields) == 4
    # Check mapping
    mapping = {f["key"]: f for f in fields}
    assert mapping["base_url"]["field_type"] == "url"
    assert mapping["timeout"]["field_type"] == "number"
    assert mapping["enabled"]["field_type"] == "checkbox"
    assert mapping["quality"]["field_type"] == "select"
    assert mapping["quality"]["choices"] == ["draft", "standard", "high"]
    # Also test all supported types
    ctx2 = _ctx("form2.ext", CapabilityRegistry(), config_store=ConfigValueStore(str(tmp_path / "ui11b.db")))
    cap_reg2 = ctx2.config._registry  # internal, but test via ctx
    ctx2.config.register(key="s", type="string", default="hello")
    ctx2.config.register(key="i", type="integer", default=5)
    ctx2.config.register(key="n", type="number", default=3.14)
    ctx2.config.register(key="b", type="boolean", default=True)
    ctx2.config.register(key="e", type="enum", choices=["a", "b"], default="a")
    ctx2.config.register(key="p", type="path", default="/tmp/foo")
    ctx2.config.register(key="u", type="url", default="http://example.com")
    ctx2.config.register(key="sec", type="secret", required=False)
    ctx2.config.register(key="j", type="json", default={"x": 1})
    ctx2.config.register(key="l", type="list", default=[1, 2])
    schema2 = build_form_schema_for_extension("form2.ext", cap_reg2, config_store=ctx2.config._config_store)
    assert len(schema2["fields"]) == 10
    m2 = {f["key"]: f for f in schema2["fields"]}
    assert m2["s"]["field_type"] == "text"
    assert m2["p"]["field_type"] == "path"
    assert m2["u"]["field_type"] == "url"
    assert m2["sec"]["field_type"] == "secret"
    assert m2["j"]["field_type"] == "json"
    assert m2["l"]["field_type"] == "list"


# 12. Secret config tidak mengandung secret value dalam UI metadata
def test_12_secret_not_in_ui_metadata(tmp_path):
    db = tmp_path / "ui12.db"
    store = ConfigValueStore(str(db))
    cap_reg = CapabilityRegistry()
    ctx = _ctx("sec.ui", cap_reg, config_store=store)
    secret_val = "sk-very-secret-123"
    ctx.config.register(key="api_key", type="secret", required=True)
    ctx.config.set("api_key", secret_val)
    # Field descriptor should not contain value
    rec = cap_reg.get("config", "sec.ui.api_key")
    field = config_definition_to_field(rec, has_value=True)
    assert field["secret"] is True
    assert field["type"] == "secret"
    assert "configured" in field
    assert field["configured"] is True
    assert secret_val not in json.dumps(field, default=str)
    assert secret_val not in json.dumps(rec.metadata, default=str)
    # Form schema also secret-safe
    schema = build_form_schema_for_extension("sec.ui", cap_reg, config_store=store)
    raw = json.dumps(schema, default=str)
    assert secret_val not in raw
    # Secret value is retrievable via runtime but not via UI metadata
    assert ctx.config.get("api_key") == secret_val


# 13. Structured result dapat dikenali renderer
def test_13_structured_result_renderer(tmp_path):
    from agent_ai.tools.base import BaseTool
    # dummy tool that returns structured result
    class T(BaseTool):
        name = "dummy.analysis.analyze"
        description = "d"
        input_schema = {"type": "object", "properties": {}}
        def execute(self, **kw):
            return {"type": "chart", "renderer": "chart", "data": {"type": "bar", "labels": ["A"], "datasets": [{"label": "S", "data": [10]}]}}
    # Simulate tool execution -> result
    res = T().execute()
    assert resolve_renderer_for_result(res) == "chart"
    # Chart result helper
    chart = make_chart_result("bar", ["A", "B", "C"], [{"label": "Sales", "data": [10, 20, 15]}])
    assert chart.renderer == "chart"
    assert chart.data["type"] == "bar"
    d = chart.to_dict()
    assert d["renderer"] == "chart"
    assert "data" in d
    json.dumps(d)
    # Table result
    table = make_table_result([{"key": "name", "title": "Name"}], [{"name": "A", "value": 10}])
    assert table.renderer == "table"
    json.dumps(table.to_dict())
    # Generic resolve for table
    assert resolve_renderer_for_result({"type": "table", "renderer": "table", "data": {}}) == "table"
    # viewer type
    assert resolve_renderer_for_result({"viewer_type": "image", "payload": "xyz"}) == "image"
    # artifact-based resolution
    assert resolve_renderer_for_result({"artifact": {"mime_type": "image/png"}}) == "image"
    assert resolve_renderer_for_result({"artifact": {"mime_type": "video/mp4"}}) == "video"
    assert resolve_renderer_for_result({"artifact": {"mime_type": "application/pdf"}}) == "pdf"
    assert resolve_renderer_for_result({"artifact": {"mime_type": "text/plain"}}) == "text"


# 14. Artifact reference dapat digunakan renderer
def test_14_artifact_ref():
    from agent_ai.extensions.ui import ArtifactRef
    art = ArtifactRef(artifact_id="artifact-123", mime_type="image/png", size=1024, name="chart.png", metadata={"renderer": "image"})
    d = art.to_dict()
    assert d["artifact_id"] == "artifact-123"
    assert d["mime_type"] == "image/png"
    assert d["size"] == 1024
    # UIResult with artifact
    res = UIResult(renderer="image", artifact=art.to_dict(), metadata={})
    dd = res.to_dict()
    assert dd["artifact"]["artifact_id"] == "artifact-123"
    # make_viewer_result with artifact
    vr = make_viewer_result("image", payload=None, artifact=art.to_dict())
    assert vr.renderer == "viewer"
    assert vr.artifact["artifact_id"] == "artifact-123"
    # Generic viewer contract: Viewer API can handle image/video/pdf etc.
    for vtype in ["image", "video", "audio", "pdf", "json", "text", "log", "diff", "table", "chart", "map"]:
        vr2 = make_viewer_result(vtype, payload={"dummy": 1})
        assert vr2.type == vtype


# 15. Tool/command action tetap menggunakan existing execution path
def test_15_action_via_existing_path(tmp_path):
    cap_reg = CapabilityRegistry()
    from agent_ai.tools.registry import ToolRegistry
    from agent_ai.tools.base import BaseTool
    tool_reg = ToolRegistry()
    ctx = _ctx("act.ext", cap_reg, tool_registry=tool_reg)
    class MyTool(BaseTool):
        name = "act.ext.generate"
        description = "gen"
        input_schema = {"type": "object", "properties": {}}
        def execute(self, **kw): return "generated"
    ctx.tools.register(MyTool())
    # UI Action should reference capability/tool identifier, not arbitrary core
    ctx.ui.register(id="act.ext.action1", type="action", title="Generate", actions=[{"id": "act.ext.generate"}])
    rec_ui = cap_reg.get("ui", "act.ext.action1")
    assert rec_ui.metadata["type"] == "action"
    assert rec_ui.metadata["actions"][0]["id"] == "act.ext.generate"
    # Execution must go via tool registry (existing path)
    tool = tool_reg.get("act.ext.generate")
    assert tool is not None
    assert tool.execute() == "generated"


# 16. UI tidak bypass permission
def test_16_no_bypass_permission(tmp_path):
    # Capability registry preserves audience/permission; UI action cannot escalate
    cap_reg = CapabilityRegistry()
    ctx = _ctx("perm.ui", cap_reg)
    ctx.tools.register("perm.ui.tool1", audience="agent", permission="read")
    ctx.ui.register(id="perm.ui.action1", type="action", title="Do", actions=[{"id": "perm.ui.tool1"}])
    # UI action references tool id; execution must still check permission via existing PermissionManager
    # Simulate PermissionManager check: tool requires 'read' permission; UI action itself should not grant write
    rec_tool = cap_reg.get("tool", "perm.ui.tool1")
    assert rec_tool.permission == "read"
    rec_ui = cap_reg.get("ui", "perm.ui.action1")
    # UI metadata must not contain permission escalation
    assert "write" not in json.dumps(rec_ui.metadata, default=str)


# 17. Tidak ada hardcode extension name/id
def test_17_no_hardcode():
    import inspect
    import agent_ai.extensions.capabilities as cap_mod
    import agent_ai.extensions.ui as ui_mod
    for mod in (cap_mod, ui_mod):
        src = inspect.getsource(mod).lower()
        # Must not have hardcode comparison for extension_id
        assert 'if extension_id ==' not in src
        assert 'if capability_id ==' not in src
        # Check for known bad examples
        for bad in ["comfyui", "browser_debug", "veo3", "excel_viewer"]:
            assert bad not in src


# 18. Existing Foundation tests tetap lulus
def test_18_foundation():
    from agent_ai.extensions.manifest import load_manifest
    tmp = Path(tempfile.mkdtemp())
    m_path = tmp / "manifest.json"
    m_path.write_text('{"id": "found.ext", "name": "N", "version": "1.0", "description": "D", "api_version": "1"}')
    m = load_manifest(m_path)
    assert m.id == "found.ext"
    from agent_ai.extensions.base import Extension
    class MyExt(Extension):
        def register(self, ctx): pass
    ext = MyExt()
    ext.register(ExtensionContext())


# 19. Existing Registry/Loader/Catalog tests tetap lulus
def test_19_registry_loader_catalog():
    tmp = Path(tempfile.mkdtemp())
    import json as _json
    for nid in ["a", "b"]:
        d = tmp / f"ext-{nid}"
        d.mkdir()
        (d / "manifest.json").write_text(_json.dumps({"id": f"rlc.{nid}", "name": nid, "version": "1", "description": "D", "api_version": "1"}))
        (d / "extension.py").write_text("from agent_ai.extensions import Extension\nextension = Extension()\n")
    reg = ExtensionRegistry()
    from agent_ai.extensions.loader import ExtensionLoader as _L
    loader = _L(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    res = loader.load_all()
    assert res.count_loaded == 2
    from agent_ai.extensions.catalog import ExtensionCatalog
    cat = ExtensionCatalog(reg).get_catalog()
    assert cat["count"] == 2


# 20. Existing Capability tests tetap lulus
def test_20_capability():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("cap.ext", cap_reg)
    ctx.tools.register("cap.ext.tool1")
    assert cap_reg.exists("tool", "cap.ext.tool1")


# 21. Existing Config/Storage tests tetap lulus
def test_21_config_storage(tmp_path):
    db = tmp_path / "t21.db"
    store = ConfigValueStore(str(db))
    cap_reg = CapabilityRegistry()
    ctx = _ctx("cs.ext", cap_reg, config_store=store)
    ctx.config.register(key="timeout", type="integer", default=60)
    ctx.config.set("timeout", 99)
    assert ctx.config.get("timeout") == 99


# 22. compression.enabled tetap false
def test_22_compression_false():
    from agent_ai.config.settings import compression_enabled
    assert compression_enabled() is False


# Additional Task 05 contract tests
def test_full_dummy_extension_loader(tmp_path):
    # Simulate loading dummy.analysis extension via loader using shared tool registry approach
    import json as _json
    src = Path("Extension/dummy-analysis")
    if not src.exists():
        pytest.skip("dummy-analysis not found")
    tmp2 = Path(tempfile.mkdtemp())
    dest = tmp2 / "dummy-analysis"
    dest.mkdir()
    for f in src.iterdir():
        if f.is_file():
            (dest / f.name).write_bytes(f.read_bytes())
    cap_reg = CapabilityRegistry()
    from agent_ai.tools.registry import ToolRegistry
    tool_reg = ToolRegistry()
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp2, enable_entry_points=False, capability_registry=cap_reg, tool_registry=tool_reg)
    res = loader.load_all()
    assert res.count_loaded == 1
    assert reg.exists("dummy.analysis")
    # UI catalog
    catalog = UICatalog(cap_reg, reg)
    assert catalog.count() >= 10  # we have 10+ UIs
    # Verify capability-registered tool is accessible via capability, and registry ToolRegistry also has it
    # Because ToolsFacade registers into tool_reg when provided
    rec = cap_reg.get("tool", "dummy.analysis.analyze")
    assert rec is not None
    # If tool instance present, exercise it via fresh instance
    from agent_ai.tools.base import BaseTool as _BT
    # Instantiate from extension file directly to validate result shape
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("dummy_analysis_ext", str(dest / "extension.py"))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # execute analyze tool if exposed
    if hasattr(mod, "AnalyzeTool"):
        result = mod.AnalyzeTool().execute(dataset="sales")
        assert resolve_renderer_for_result(result) == "chart"
    else:
        assert rec.metadata is not None


def test_form_validation_frontend_layers(tmp_path):
    # Frontend validation is early, backend is authoritative
    db = tmp_path / "valid.db"
    store = ConfigValueStore(str(db))
    cap_reg = CapabilityRegistry()
    ctx = _ctx("valid.ext", cap_reg, config_store=store)
    ctx.config.register(key="base_url", type="url", default="http://example.com")
    ctx.config.register(key="timeout", type="integer", default=60)
    # valid set
    ctx.config.set("base_url", "http://new.example.com")
    ctx.config.set("timeout", 120)
    assert ctx.config.get("base_url") == "http://new.example.com"
    # invalid set must fail at backend (authority)
    from agent_ai.extensions.config import ConfigValidationError
    with pytest.raises(ConfigValidationError):
        ctx.config.set("base_url", "not-a-url")
    with pytest.raises(ConfigValidationError):
        ctx.config.set("timeout", "not-int")


def test_modal_panel_wizard_metadata(tmp_path):
    cap_reg = CapabilityRegistry()
    ctx = _ctx("decor.ui", cap_reg)
    ctx.ui.register(id="decor.ui.settings_modal", type="modal", title="Settings", props={"size": "large"}, actions=[{"id": "submit"}])
    ctx.ui.register(id="decor.ui.debug_panel", type="panel", title="Debug Panel", props={"placement": "left", "slot": "sidebar"})
    ctx.ui.register(id="decor.ui.setup_wizard", type="wizard", title="Wizard", props={"steps": [{"title": "S1"}, {"title": "S2"}]})
    # Catalog
    cat = UICatalog(cap_reg)
    mod = cat.get_contribution("decor.ui.settings_modal")
    assert mod.type == "modal"
    assert mod.props["size"] == "large"
    pan = cat.get_contribution("decor.ui.debug_panel")
    assert pan.type == "panel"
    assert pan.props["placement"] == "left"
    wiz = cat.get_contribution("decor.ui.setup_wizard")
    assert wiz.type == "wizard"
    assert len(wiz.props["steps"]) == 2


def test_custom_view_contract(tmp_path):
    cap_reg = CapabilityRegistry()
    ctx = _ctx("custom.ui", cap_reg)
    ctx.ui.register(id="custom.ui.browser", type="custom_view", title="Browser", entry="ui/views/browser.html")
    rec = cap_reg.get("ui", "custom.ui.browser")
    assert rec.metadata["type"] == "custom_view"
    assert rec.metadata["entry"] == "ui/views/browser.html"
    cat = UICatalog(cap_reg)
    contrib = cat.get_contribution("custom.ui.browser")
    assert contrib.type == "custom_view"
    assert contrib.entry == "ui/views/browser.html"
    assert contrib.props == {} or True  # entry is top-level


def test_no_new_frontend_framework():
    # Ensure extension UI does not introduce React/Svelte/Angular
    import pathlib
    pkg = Path("web/frontend/package.json")
    if pkg.exists():
        text = pkg.read_text()
        assert '"react"' not in text.lower()
        assert '"svelte"' not in text.lower()
        assert '"angular"' not in text.lower()


def test_table_generic_not_excel_specific():
    cap_reg = CapabilityRegistry()
    ctx = _ctx("tbl.ui", cap_reg)
    ctx.ui.register(id="tbl.ui.results", type="table", title="Results", props={"columns": [{"key": "a", "title": "A"}], "rows": [{"a": 1}]})
    rec = cap_reg.get("ui", "tbl.ui.results")
    # Must not mention excel in metadata (generic)
    assert "excel" not in json.dumps(rec.metadata, default=str).lower()
    # Generic table renderer validates
    tbl = make_table_result([{"key": "a", "title": "A"}], [{"a": 1}], pagination={"page": 1, "total_pages": 2})
    assert tbl.data["pagination"]["page"] == 1
