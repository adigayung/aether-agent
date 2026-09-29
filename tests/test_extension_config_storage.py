"""Task 04 — Extension Config / Variables / Storage validation.

Covers 28 validation points from spec 28 (Config + Storage + Regression).
Uses temporary DB/filesystem isolation to not touch data/aether.db prod.
"""

import json
import tempfile
from pathlib import Path

import pytest

from agent_ai.extensions.capabilities import CapabilityRegistry, CapabilityValidationError
from agent_ai.extensions.config import ConfigValueStore, ConfigValidationError, RequiredConfigMissingError, _validate_type, get_config_store
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.manifest import Manifest
from agent_ai.extensions.storage import ExtensionStorage, get_extension_storage
from agent_ai.tools.base import ToolExecutionError
from agent_ai.extensions.loader import ExtensionLoader
from agent_ai.extensions.registry import ExtensionRegistry


def _make_manifest(ext_id: str) -> Manifest:
    return Manifest(id=ext_id, name="Test", version="1.0", description="Desc", api_version="1", raw={}, source_path="")


def _fresh_store_and_registry(tmp_path: Path):
    db = tmp_path / "test.db"
    # Clear singleton cache for this db path
    # ConfigValueStore is singleton per path; create new instance with tmp db
    store = ConfigValueStore(str(db))
    # Ensure table exists
    store._init_schema()
    cap_reg = CapabilityRegistry()
    return store, cap_reg, db


# --------------- Config tests ---------------

def test_01_register_config(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.ext1")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="api_key", type="secret", required=True, description="key")
    assert cap_reg.exists("config", "test.ext1.api_key")

def test_02_all_supported_types(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.types")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="s", type="string", default="hello")
    ctx.config.register(key="i", type="integer", default=5)
    ctx.config.register(key="n", type="number", default=3.14)
    ctx.config.register(key="b", type="boolean", default=True)
    ctx.config.register(key="e", type="enum", choices=["a", "b"], default="a")
    ctx.config.register(key="p", type="path", default="/tmp/foo")
    ctx.config.register(key="u", type="url", default="http://example.com")
    ctx.config.register(key="sec", type="secret", required=False)
    ctx.config.register(key="j", type="json", default={"x": 1})
    ctx.config.register(key="l", type="list", default=[1, 2])
    for k in ["s", "i", "n", "b", "e", "p", "u", "sec", "j", "l"]:
        assert cap_reg.exists("config", f"test.types.{k}")

def test_03_get_works(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.get")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="base_url", type="url", default="http://localhost:8188")
    # default
    assert ctx.config.get("base_url") == "http://localhost:8188"
    # set and get
    ctx.config.set("base_url", "http://example.com")
    assert ctx.config.get("base_url") == "http://example.com"

def test_04_set_works(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.set")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="timeout", type="integer", default=60)
    ctx.config.set("timeout", 120)
    assert ctx.config.get("timeout") == 120

def test_05_unknown_key_error(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.unknown")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="known", type="string", default="a")
    with pytest.raises((CapabilityValidationError, ConfigValidationError)):
        ctx.config.get("unknown_key")
    with pytest.raises((CapabilityValidationError, ConfigValidationError)):
        ctx.config.set("unknown_key", "val")

def test_06_invalid_type_error(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.invalid")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="timeout", type="integer", default=60)
    with pytest.raises(ConfigValidationError):
        ctx.config.set("timeout", "not-an-int")
    ctx.config.register(key="flag", type="boolean", default=False)
    with pytest.raises(ConfigValidationError):
        ctx.config.set("flag", "true")
    ctx.config.register(key="u", type="url", default="http://example.com")
    with pytest.raises(ConfigValidationError):
        ctx.config.set("u", "not-a-url")

def test_07_enum_choices_validated(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.enum")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="quality", type="enum", choices=["draft", "standard", "high"], default="standard")
    ctx.config.set("quality", "high")
    assert ctx.config.get("quality") == "high"
    with pytest.raises((ConfigValidationError, CapabilityValidationError)):
        ctx.config.set("quality", "ultra")
    # invalid choices on register
    with pytest.raises(CapabilityValidationError):
        ctx.config.register(key="bad_enum", type="enum", choices=[], default="a")
    with pytest.raises(CapabilityValidationError):
        ctx.config.register(key="bad_enum2", type="enum", default="a")  # no choices
    # default must be in choices
    with pytest.raises((CapabilityValidationError, ConfigValidationError)):
        ctx.config.register(key="bad_enum3", type="enum", choices=["a", "b"], default="c")

def test_08_default_value(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.default")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="timeout", type="integer", default=60)
    # before set, get returns default
    assert ctx.config.get("timeout") == 60
    ctx.config.set("timeout", 30)
    assert ctx.config.get("timeout") == 30
    # invalid default rejected
    with pytest.raises((CapabilityValidationError, ConfigValidationError)):
        ctx.config.register(key="bad_def", type="integer", default="not-int")
    # enum default invalid
    with pytest.raises((CapabilityValidationError, ConfigValidationError)):
        ctx.config.register(key="bad_e", type="enum", choices=["a", "b"], default="c")

def test_09_required_not_fail_startup(tmp_path):
    # Startup must not fail if required config missing
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    # Simulate loader with required secret not set
    import json as _json
    ext_dir = tmp_path / "ext-req"
    ext_dir.mkdir()
    (ext_dir / "manifest.json").write_text(_json.dumps({"id": "req.ext", "name": "N", "version": "1", "description": "D", "api_version": "1"}))
    (ext_dir / "extension.py").write_text(
        "from agent_ai.extensions import Extension\n"
        "class E(Extension):\n"
        "    def register(self, ctx):\n"
        "        ctx.config.register(key='api_key', type='secret', required=True)\n"
        "extension = E()\n"
    )
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp_path, enable_entry_points=False, capability_registry=cap_reg, config_store=store)
    res = loader.load_all()
    # Should still load successfully (required not set but startup OK)
    assert reg.exists("req.ext")
    assert res.count_failed == 0

def test_10_required_clear_error_on_request(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.req2")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="api_key", type="secret", required=True)
    with pytest.raises(ToolExecutionError) as excinfo:
        ctx.config.get("api_key")
    assert "Required config" in str(excinfo.value)
    assert "api_key" in str(excinfo.value)

def test_11_secret_not_in_logging(tmp_path):
    # Secrets should not appear in sanitized payload / activity log
    from agent_ai.core.observability import sanitize_payload
    secret_val = "sk-1234567890secret"
    payload = {"api_key": secret_val, "other": "hello", "nested": {"secret_token": secret_val}}
    cleaned = sanitize_payload(payload)
    # sanitized should redact
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["nested"]["secret_token"] == "[redacted]"
    assert cleaned["other"] == "hello"
    # Also ensure capability metadata does not contain secret value
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.sec_log")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="api_key", type="secret", required=True)
    ctx.config.set("api_key", secret_val)
    rec = cap_reg.get("config", "test.sec_log.api_key")
    # metadata should not contain value
    assert secret_val not in str(rec.metadata)
    assert secret_val not in json.dumps(rec.metadata, default=str)
    # but runtime get returns secret
    assert ctx.config.get("api_key") == secret_val

def test_12_secret_not_in_llm_context(tmp_path):
    # Secrets not automatically exposed to LLM (no tool schema leak)
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.llm_ctx")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="api_key", type="secret", required=True)
    ctx.config.set("api_key", "super-secret-val")
    # Tool specs should not contain secret values
    specs = ctx.tools.specs() if hasattr(ctx.tools, "specs") else []
    assert "super-secret-val" not in json.dumps(specs, default=str)
    # Config definitions list should be metadata only, not values
    defs = ctx.config.list_definitions()
    assert "super-secret-val" not in json.dumps(defs, default=str)
    # Capability records
    for rec in cap_reg.list("config"):
        assert "super-secret-val" not in json.dumps(rec.metadata, default=str)

def test_13_config_persistent(tmp_path):
    store, cap_reg, db = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.persist")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="timeout", type="integer", default=60)
    ctx.config.set("timeout", 99)
    # Simulate restart: new store instance same db, new registry with same definitions
    # ConfigValueStore singleton per path, but we create new CapabilityRegistry and re-register
    cap_reg2 = CapabilityRegistry()
    # Need to re-register config definitions for new registry
    manifest2 = _make_manifest("test.persist")
    # Use same store db
    store2 = ConfigValueStore(str(db))
    ctx2 = ExtensionContext(manifest=manifest2, capability_registry=cap_reg2, config_store=store2)
    ctx2.config.register(key="timeout", type="integer", default=60)
    # Value should persist (reads from SQLite)
    assert ctx2.config.get("timeout") == 99
    # task scope NOT persistent: after store recreated (but task is in-memory, we test task not persisted across new store instance?)
    # Our task scope is in-memory per store instance, so new instance loses it. That's expected per spec.
    cap_reg3 = CapabilityRegistry()
    store3 = ConfigValueStore(str(db))
    ctx3 = ExtensionContext(manifest=_make_manifest("test.persist"), capability_registry=cap_reg3, config_store=store3)
    ctx3.config.register(key="tmp_opt", type="string", default=None, scope="task")
    ctx3.config.set("tmp_opt", "temp-val", scope="task")
    assert ctx3.config.get("tmp_opt", scope="task") == "temp-val"
    # New store instance should NOT have task val (task is in-memory per instance)
    # But since ConfigValueStore is singleton per path, it shares _task_store. So we test via different db path for isolation.
    tmp2 = tmp_path / "other.db"
    store_other = ConfigValueStore(str(tmp2))
    cap_reg_other = CapabilityRegistry()
    ctx_other = ExtensionContext(manifest=_make_manifest("test.persist"), capability_registry=cap_reg_other, config_store=store_other)
    ctx_other.config.register(key="tmp_opt", type="string", default=None, scope="task")
    assert ctx_other.config.get("tmp_opt", scope="task") is None

def test_14_duplicate_config_key(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.dup")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="dup_key", type="string", default="a")
    from agent_ai.extensions.capabilities import DuplicateCapabilityError
    with pytest.raises(DuplicateCapabilityError):
        ctx.config.register(key="dup_key", type="string", default="b")

# --------------- Storage tests ---------------

def test_15_state_get_set_delete(tmp_path):
    storage = ExtensionStorage("test.storage1", aether_root=tmp_path)
    storage.set("last_job_id", "job-42")
    assert storage.get("last_job_id") == "job-42"
    assert storage.exists("last_job_id")
    assert storage.delete("last_job_id") is True
    assert not storage.exists("last_job_id")
    assert storage.get("last_job_id", default="fallback") == "fallback"

def test_16_cache_store_read(tmp_path):
    storage = ExtensionStorage("test.storage2", aether_root=tmp_path)
    storage.cache_set("meta", {"url": "http://example.com"})
    assert storage.cache_get("meta") == {"url": "http://example.com"}
    assert storage.cache_exists("meta")
    storage.cache_delete("meta")
    assert not storage.cache_exists("meta")

def test_17_temp_create_delete(tmp_path):
    storage = ExtensionStorage("test.storage3", aether_root=tmp_path)
    p = storage.temp_write("render.tmp", "hello temp")
    assert p.exists()
    assert storage.temp_exists("render.tmp")
    assert storage.temp_read("render.tmp") == "hello temp"
    assert storage.temp_delete("render.tmp") is True
    assert not storage.temp_exists("render.tmp")

def test_18_storage_isolation(tmp_path):
    sA = ExtensionStorage("ext.a", aether_root=tmp_path)
    sB = ExtensionStorage("ext.b", aether_root=tmp_path)
    sA.set("secret", "value-a")
    sB.set("secret", "value-b")
    assert sA.get("secret") == "value-a"
    assert sB.get("secret") == "value-b"
    # Isolation by path
    assert sA.get_storage_root() != sB.get_storage_root()

def test_19_no_cross_read_via_api(tmp_path):
    sA = ExtensionStorage("aaa.ext", aether_root=tmp_path)
    sB = ExtensionStorage("bbb.ext", aether_root=tmp_path)
    sA.set("mykey", {"x": 1})
    # B cannot read A's storage via normal API (different namespace)
    assert sB.get("mykey") is None
    # Also via cache
    sA.cache_set("c1", [1, 2, 3])
    assert sB.cache_get("c1") is None

def test_20_project_scoped_storage(tmp_path):
    # Two projects
    projA = tmp_path / "projA"
    projB = tmp_path / "projB"
    projA.mkdir()
    projB.mkdir()
    storage = ExtensionStorage("test.proj", aether_root=tmp_path)
    # Project-scoped via explicit project path
    storage.project(projA).set("profile", "profile-a")
    storage.project(projB).set("profile", "profile-b")
    assert storage.project(projA).get("profile") == "profile-a"
    assert storage.project(projB).get("profile") == "profile-b"
    # Must be separate from global storage
    storage.set("profile", "global-profile")
    assert storage.get("profile") == "global-profile"
    assert storage.project(projA).get("profile") != storage.get("profile")
    # Also global not polluted
    assert storage.project(projA).get("profile") == "profile-a"

def test_21_disable_not_delete(tmp_path):
    # Simulate disable: config/state should remain
    db = tmp_path / "disable.db"
    store = ConfigValueStore(str(db))
    cap_reg = CapabilityRegistry()
    manifest = _make_manifest("test.disable")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="api_key", type="string", default="default")
    ctx.config.set("api_key", "kept-value")
    storage = ExtensionStorage("test.disable", aether_root=tmp_path)
    storage.set("prefs", {"theme": "dark"})
    # Simulate disable -> we don't clear anything, just not removing
    # Verify still there after "disable" (no clear call)
    assert ctx.config.get("api_key") == "kept-value"
    assert storage.get("prefs") == {"theme": "dark"}
    # Verify enable again still has old config/state (re-create context with same ids)
    cap_reg2 = CapabilityRegistry()
    # Re-register same config definition
    store2 = ConfigValueStore(str(db))
    ctx2 = ExtensionContext(manifest=_make_manifest("test.disable"), capability_registry=cap_reg2, config_store=store2)
    ctx2.config.register(key="api_key", type="string", default="default")
    assert ctx2.config.get("api_key") == "kept-value"
    storage2 = ExtensionStorage("test.disable", aether_root=tmp_path)
    assert storage2.get("prefs") == {"theme": "dark"}

def test_22_path_safety(tmp_path):
    storage = ExtensionStorage("test.pathsafe", aether_root=tmp_path)
    # Attempt path traversal via key
    storage.set("normal", {"x": 1})
    # Keys with path traversal are sanitized to safe name, not escaping root
    storage.set("../../etc/passwd", "evil")
    # It should have been sanitized and stored as safe key "passwd" (or "etc_passwd")
    # Most importantly, it must not escape storage root
    root = storage.get_storage_root().resolve()
    # Check no file outside root was created
    assert not (tmp_path / "etc" / "passwd").exists()
    # All state files are inside root
    for p in storage.get_state_dir().glob("*.json"):
        assert root in p.resolve().parents or p.resolve() == root or str(p.resolve()).startswith(str(root))
    # Check storage.path does not escape for temp
    p = storage.temp_path("../../../evil")
    assert root in p.resolve().parents or str(p.resolve()).startswith(str(root))
    # Unknown key with slash should be sanitized, not error escaping
    storage.set("a/b/c", "val")
    assert storage.get("c") == "val"  # sanitized to last component

# --------------- Regression tests ---------------

def test_23_foundation_still_passes(tmp_path):
    from agent_ai.extensions.manifest import load_manifest
    m_path = tmp_path / "manifest.json"
    m_path.write_text('{"id": "test.ext", "name": "Test", "version": "1.0", "description": "Desc", "api_version": "1"}')
    manifest = load_manifest(m_path)
    assert manifest.id == "test.ext"
    from agent_ai.extensions.base import Extension
    from agent_ai.extensions.context import ExtensionContext
    ctx = ExtensionContext()
    assert ctx.is_placeholder("tools")
    class MyExt(Extension):
        def register(self, ctx): pass
    ext = MyExt()
    ext.register(ctx)

def test_24_registry_loader_catalog(tmp_path):
    from agent_ai.extensions.registry import ExtensionRegistry
    from agent_ai.extensions.loader import ExtensionLoader
    import json as _json
    for nid in ["a", "b"]:
        d = tmp_path / f"ext-{nid}"
        d.mkdir()
        (d / "manifest.json").write_text(_json.dumps({"id": f"reg.{nid}", "name": nid, "version": "1", "description": "D", "api_version": "1"}))
        (d / "extension.py").write_text("from agent_ai.extensions import Extension\nextension = Extension()\n")
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp_path, enable_entry_points=False)
    res = loader.load_all()
    assert res.count_loaded == 2

def test_25_capability_registration(tmp_path):
    cap_reg = CapabilityRegistry()
    manifest = _make_manifest("cap.test")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=ConfigValueStore(str(tmp_path / "t.db")))
    ctx.tools.register("cap.test.tool1")
    assert cap_reg.exists("tool", "cap.test.tool1")

def test_26_skill_system(tmp_path):
    from agent_ai.projects.skills import SkillStore
    store = SkillStore(tmp_path)
    store.ensure()
    s = store.create_skill(skill_id="my-skill", name="My Skill", description="desc", content="# hello")
    assert s.skill_id == "my-skill"

def test_27_tool_registry(tmp_path):
    from agent_ai.tools.registry import ToolRegistry
    from agent_ai.tools.base import BaseTool
    reg = ToolRegistry()
    class T(BaseTool):
        name = "test.tool"
        description = "d"
        input_schema = {"type": "object", "properties": {}}
        def execute(self, **kw): return "ok"
    reg.register(T())
    assert reg.has("test.tool")

def test_28_compression_still_false():
    from agent_ai.config.settings import compression_enabled
    assert compression_enabled() is False

# Additional scope tests
def test_scope_project_task(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.scope")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="global_key", type="string", default="g", scope="global")
    ctx.config.register(key="ext_key", type="string", default="e", scope="extension")
    ctx.config.register(key="proj_key", type="string", default=None, scope="project")
    ctx.config.register(key="task_key", type="string", default=None, scope="task")
    ctx.config.set("global_key", "gv", scope="global")
    ctx.config.set("ext_key", "ev", scope="extension")
    ctx.config.set("proj_key", "pv", scope="project", project_id="proj123")
    ctx.config.set("task_key", "tv", scope="task")
    assert ctx.config.get("global_key", scope="global") == "gv"
    assert ctx.config.get("ext_key", scope="extension") == "ev"
    assert ctx.config.get("proj_key", scope="project", project_id="proj123") == "pv"
    assert ctx.config.get("task_key", scope="task") == "tv"

def test_has_and_list_definitions(tmp_path):
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.has")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="k1", type="string", default="d")
    ctx.config.register(key="k2", type="integer", default=5)
    assert ctx.config.has("k1")  # default counts as has
    assert ctx.config.get_definition("k1")["type"] == "string"
    defs = ctx.config.list_definitions()
    assert len(defs) == 2

def test_secret_via_storage_not_leaked(tmp_path):
    # Verify secret values are stored but not exposed via config definitions
    store, cap_reg, _ = _fresh_store_and_registry(tmp_path)
    manifest = _make_manifest("test.sec2")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg, config_store=store)
    ctx.config.register(key="token", type="secret", required=True)
    ctx.config.set("token", "my-super-secret")
    # Definitions should not contain value
    for d in ctx.config.list_definitions():
        assert "my-super-secret" not in json.dumps(d, default=str)
    # Direct get works for extension runtime
    assert ctx.config.get("token") == "my-super-secret"
