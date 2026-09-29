"""Task 02 validation: Registry, Loader, Catalog (metadata-only, dynamic, no regression).

Minimal validasi per spec 18:
  A Discovery, B Load, C Register, D Identity, E Catalog,
  F Duplicate, G Invalid, H Startup, I Metadata Only, J Existing System
"""

import json
import tempfile
from pathlib import Path

import pytest

from agent_ai.extensions.catalog import ExtensionCatalog, get_catalog, get_extension, extension_exists
from agent_ai.extensions.discovery import ExtensionEntry
from agent_ai.extensions.loader import ExtensionLoader, load_all_extensions
from agent_ai.extensions.manifest import DuplicateExtensionError, Manifest
from agent_ai.extensions.registry import ExtensionRegistry
from agent_ai.extensions.base import Extension


def _make_ext(dir_path: Path, manifest: dict, extension_py: str = None):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "manifest.json").write_text(json.dumps(manifest))
    if extension_py is None:
        extension_py = "from agent_ai.extensions import Extension\nextension = Extension()\n"
    (dir_path / "extension.py").write_text(extension_py)


# A Discovery + B Load + C Register
def test_discovery_load_register():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "extension-a", {"id": "example.a", "name": "A", "version": "1.0.0", "description": "desc", "api_version": "1"})
    _make_ext(tmp / "extension-b", {"id": "example.b", "name": "B", "version": "1.0.0", "description": "desc", "api_version": "1"})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    res = loader.load_all()
    assert res.count_loaded == 2
    assert res.count_failed == 0
    assert reg.exists("example.a")
    assert reg.exists("example.b")
    assert len(reg.all()) == 2


# D Identity folder != manifest id
def test_identity_folder_not_id():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "my-random-folder", {"id": "example.real-id", "name": "Real", "version": "1.0", "description": "D", "api_version": "1"})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    res = loader.load_all()
    assert res.count_loaded == 1
    assert reg.exists("example.real-id")
    assert not reg.exists("my-random-folder")
    assert reg.get("example.real-id") is not None
    assert reg.get("example.real-id").manifest.id == "example.real-id"


# E Catalog metadata
def test_catalog_metadata():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "ext1", {"id": "cat.a", "name": "Cat A", "version": "1.0.0", "description": "desc a", "api_version": "1"})
    _make_ext(tmp / "ext2", {"id": "cat.b", "name": "Cat B", "version": "2.0.0", "description": "desc b", "api_version": "1"})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    loader.load_all()
    cat = ExtensionCatalog(reg).get_catalog()
    assert cat["count"] == 2
    ids = {e["id"] for e in cat["extensions"]}
    assert ids == {"cat.a", "cat.b"}
    for e in cat["extensions"]:
        assert "id" in e and "name" in e and "version" in e and "description" in e and "api_version" in e
        assert "status" in e


# F Duplicate
def test_duplicate_id():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "dup-a", {"id": "dup.id", "name": "N", "version": "1", "description": "D", "api_version": "1"})
    _make_ext(tmp / "dup-b", {"id": "dup.id", "name": "N2", "version": "1", "description": "D", "api_version": "1"})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    res = loader.load_all()
    assert res.count_loaded == 1
    assert res.count_failed == 1
    assert "Duplicate" in res.failed[0].error
    assert reg.exists("dup.id")
    assert len(reg.all()) == 1

    # direct registry duplicate also raises
    reg2 = ExtensionRegistry()
    m = Manifest(id="x", name="N", version="1", description="D", api_version="1", raw={}, source_path="s1")
    entry = ExtensionEntry(manifest=m, extension=Extension(manifest=m), source="/tmp/a")
    reg2.register(entry)
    m2 = Manifest(id="x", name="N2", version="1", description="D", api_version="1", raw={}, source_path="s2")
    entry2 = ExtensionEntry(manifest=m2, extension=Extension(manifest=m2), source="/tmp/b")
    with pytest.raises(DuplicateExtensionError):
        reg2.register(entry2)


# G Invalid -> fails but other continues
def test_invalid_does_not_block_others():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "valid", {"id": "valid.ext", "name": "V", "version": "1", "description": "D", "api_version": "1"})
    # invalid: missing required fields
    d = tmp / "invalid"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"id": "invalid.ext", "name": "N"}))
    (d / "extension.py").write_text("from agent_ai.extensions import Extension\nextension=Extension()\n")
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    res = loader.load_all()
    assert res.count_loaded == 1
    assert res.count_failed == 1
    assert reg.exists("valid.ext")
    assert not reg.exists("invalid.ext")

    # broken import
    tmp2 = Path(tempfile.mkdtemp())
    b = tmp2 / "broken"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({"id": "broken.ext", "name": "B", "version": "1", "description": "D", "api_version": "1"}))
    (b / "extension.py").write_text('raise RuntimeError("broken import")\n')
    g = tmp2 / "good"
    g.mkdir()
    (g / "manifest.json").write_text(json.dumps({"id": "good.ext", "name": "G", "version": "1", "description": "D", "api_version": "1"}))
    (g / "extension.py").write_text("from agent_ai.extensions import Extension\nextension=Extension()\n")
    reg3 = ExtensionRegistry()
    loader3 = ExtensionLoader(registry=reg3, extensions_dir=tmp2, enable_entry_points=False)
    res3 = loader3.load_all()
    assert res3.count_loaded == 1
    assert res3.count_failed == 1
    assert 'Failed to load extension "broken.ext"' in res3.failed[0].error
    assert reg3.exists("good.ext")


# H Startup
def test_startup_simulation():
    tmp = Path(tempfile.mkdtemp())
    for nid in ["a", "b", "c"]:
        _make_ext(tmp / f"ext-{nid}", {"id": f"start.{nid}", "name": nid, "version": "1", "description": "D", "api_version": "1"})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    res = loader.startup()
    assert res.count_loaded == 3
    assert len(reg.all()) == 3
    # also via helper
    reg2, res2 = load_all_extensions(extensions_dir=tmp, enable_entry_points=False)
    assert res2.count_loaded == 3


# I Metadata only
def test_catalog_metadata_only():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "ext", {"id": "meta.ext", "name": "Meta", "version": "1", "description": "D", "api_version": "1", "publisher": "Acme", "capabilities": {"tools": ["x"]}})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    loader.load_all()
    cat = ExtensionCatalog(reg).get_catalog()
    s = json.dumps(cat)
    # publisher/capabilities may exist (metadata), but not source code
    assert cat["extensions"][0].get("publisher") == "Acme"
    assert cat["extensions"][0].get("capabilities") == {"tools": ["x"]}
    assert "class E" not in s
    assert "def register" not in s
    for e in cat["extensions"]:
        assert "content" not in e
        assert "implementation" not in e
        assert "skill content" not in s.lower()

    # APIs
    assert extension_exists(reg, "meta.ext")
    assert get_extension(reg, "meta.ext") is not None
    assert get_extension(reg, "missing") is None
    assert not extension_exists(reg, "missing")
    assert get_catalog(reg)["count"] == 1


# J Dynamic catalog reflects registry
def test_dynamic_catalog():
    tmp = Path(tempfile.mkdtemp())
    reg = ExtensionRegistry()
    cat_before = ExtensionCatalog(reg).get_catalog()
    assert cat_before["count"] == 0
    _make_ext(tmp / "new-ext", {"id": "new.ext", "name": "N", "version": "1", "description": "D", "api_version": "1"})
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    loader.load_all()
    cat_after = ExtensionCatalog(reg).get_catalog()
    assert cat_after["count"] == 1
    # failed not counted
    tmp2 = Path(tempfile.mkdtemp())
    d = tmp2 / "bad"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"id": "bad.ext", "name": "N"}))
    (d / "extension.py").write_text("from agent_ai.extensions import Extension\nextension=Extension()\n")
    reg2 = ExtensionRegistry()
    loader2 = ExtensionLoader(registry=reg2, extensions_dir=tmp2, enable_entry_points=False)
    res = loader2.load_all()
    assert res.count_failed == 1
    assert ExtensionCatalog(reg2).get_catalog()["count"] == 0


# Registry record exposes required fields
def test_registry_record_fields():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "ext", {"id": "fields.ext", "name": "Fields", "version": "1.2.3", "description": "Desc", "api_version": "1"})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    loader.load_all()
    rec = reg.get("fields.ext")
    assert rec is not None
    assert rec.id == "fields.ext"
    assert rec.manifest.id == "fields.ext"
    assert rec.extension is not None
    assert rec.root is not None
    assert rec.status == "loaded"
    assert rec.manifest.version == "1.2.3"


def test_registry_api_get_all_exists_remove():
    tmp = Path(tempfile.mkdtemp())
    _make_ext(tmp / "ext", {"id": "api.ext", "name": "A", "version": "1", "description": "D", "api_version": "1"})
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=tmp, enable_entry_points=False)
    loader.load_all()
    assert reg.exists("api.ext")
    assert reg.extension_exists("api.ext")
    assert reg.get_extension("api.ext") is not None
    assert len(reg.all()) == 1
    assert reg.remove("api.ext")
    assert not reg.exists("api.ext")
    assert reg.get("api.ext") is None


# Ensure existing Extension/test-extension loads via default dir when available
def test_existing_extension_folder_loads():
    from agent_ai.extensions.paths import get_extensions_dir
    ext_dir = get_extensions_dir()
    if not ext_dir.is_dir():
        pytest.skip("no Extension dir")
    reg = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, enable_entry_points=False)
    res = loader.load_all()
    # At least test-extension should be present
    assert reg.exists("aether.test-extension")
    cat = ExtensionCatalog(reg).get_catalog()
    assert any(e["id"] == "aether.test-extension" for e in cat["extensions"])
