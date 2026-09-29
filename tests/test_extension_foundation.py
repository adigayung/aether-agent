import pytest
from pathlib import Path
import sys
import shutil

from agent_ai.extensions.manifest import (
    load_manifest,
    ManifestValidationError,
    DuplicateExtensionError,
    detect_duplicate_ids,
)
from agent_ai.extensions.context import ExtensionContext, _Placeholder
from agent_ai.extensions.paths import get_aether_root, get_extensions_dir
from agent_ai.extensions.base import Extension
from agent_ai.extensions.discovery import load_extension_from_dir, discover_extensions_from_dir
from agent_ai.extensions.api_version import CURRENT_API_VERSION

def test_manifest_validation(tmp_path):
    # Valid manifest
    m_path = tmp_path / "manifest.json"
    m_path.write_text('{"id": "test.ext", "name": "Test", "version": "1.0", "description": "Desc", "api_version": "1"}')
    manifest = load_manifest(m_path)
    assert manifest.id == "test.ext"
    assert manifest.api_version == "1"

    # Missing field
    m_path.write_text('{"id": "test.ext", "name": "Test"}')
    with pytest.raises(ManifestValidationError, match='missing required field "version"'):
        load_manifest(m_path)

    # Invalid ID
    m_path.write_text('{"id": "test ext", "name": "Test", "version": "1.0", "description": "D", "api_version": "1"}')
    with pytest.raises(ManifestValidationError, match='has invalid format "test ext"'):
        load_manifest(m_path)

def test_duplicate_detection():
    from agent_ai.extensions.manifest import Manifest
    m1 = Manifest(id="a", name="A", version="1", description="D", api_version="1", raw={}, source_path="s1")
    m2 = Manifest(id="a", name="B", version="1", description="D", api_version="1", raw={}, source_path="s2")
    with pytest.raises(DuplicateExtensionError, match='Duplicate Extension id "a"'):
        detect_duplicate_ids([m1, m2])

def test_extension_context_placeholders():
    ctx = ExtensionContext()
    assert ctx.is_placeholder("tools")
    assert ctx.is_placeholder("config")
    
    # Accessing placeholder should not crash if just checking bool
    assert not ctx.tools
    
    # Accessing attribute should raise
    with pytest.raises(AttributeError, match="tools.register not yet implemented"):
        _ = ctx.tools.register

def test_extension_lifecycle():
    class MyExt(Extension):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = []
        def register(self, ctx): self.calls.append("reg")
        def enable(self, ctx): self.calls.append("en")
        def disable(self, ctx): self.calls.append("dis")

    ext = MyExt()
    ctx = ExtensionContext()
    
    ext.register(ctx)
    ext.enable(ctx)
    ext.disable(ctx)
    
    assert ext.calls == ["reg", "en", "dis"]

def test_discovery_load_from_dir(tmp_path):
    ext_dir = tmp_path / "my-ext-folder"
    ext_dir.mkdir()
    (ext_dir / "manifest.json").write_text('{"id": "real.id", "name": "N", "version": "V", "description": "D", "api_version": "1"}')
    (ext_dir / "extension.py").write_text('''
from agent_ai.extensions import Extension
class MyExt(Extension):
    def register(self, ctx): pass
extension = MyExt()
''')
    
    entry = load_extension_from_dir(ext_dir)
    assert entry.manifest.id == "real.id"
    assert entry.extension.extension_id == "real.id"
    # Identity comes from manifest, not folder
    assert ext_dir.name == "my-ext-folder" 

def test_paths_no_hardcode():
    root = get_aether_root()
    ext_dir = get_extensions_dir(root)
    assert ext_dir == root / "Extension"
    
    # Test override
    custom_root = Path("/tmp/aether")
    assert get_aether_root(custom_root) == custom_root
    assert get_extensions_dir(custom_root) == custom_root / "Extension"

def test_api_version():
    assert CURRENT_API_VERSION == "1"

if __name__ == "__main__":
    pytest.main([__file__])
