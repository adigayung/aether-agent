import unittest
import sys
from pathlib import Path

from agent_ai.extensions.manifest import load_manifest
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.capabilities import CapabilityRegistry

# Ensure the extension package is importable
import importlib

EXT_DIR = Path(__file__).resolve().parents[2] / "playwright"
MANIFEST_PATH = EXT_DIR / "manifest.json"

class TestPlaywrightExtensionFoundation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load manifest once
        cls.manifest = load_manifest(MANIFEST_PATH)
        # Import extension module
        sys.path.insert(0, str(EXT_DIR.parent.parent))  # Ensure root is on path
        cls.extension_mod = importlib.import_module("Extension.playwright.extension")
        cls.extension_instance = cls.extension_mod.extension

    def test_manifest_fields(self):
        self.assertEqual(self.manifest.id, "aether.playwright")
        self.assertEqual(self.manifest.api_version, "1")
        self.assertTrue(self.manifest.version)
        self.assertTrue(self.manifest.name)

    def test_extension_instance(self):
        # The exported variable should be an Extension subclass instance
        from agent_ai.extensions.base import Extension
        self.assertIsInstance(self.extension_instance, Extension)
        self.assertEqual(self.extension_instance.extension_id, "aether.playwright")

    def test_register_creates_config_and_service(self):
        reg = CapabilityRegistry()
        context = ExtensionContext(extension_root=EXT_DIR, manifest=self.manifest, capability_registry=reg)
        # Register should not raise
        self.extension_instance.register(context)
        # Config definitions exist
        browser_def = context.config.get_definition("browser")
        self.assertEqual(browser_def["type"], "enum")
        self.assertIn("chromium", browser_def["choices"])
        # Service capability registered
        service_cap = context.services.get(f"{self.manifest.id}.playwright_service")
        self.assertIsNotNone(service_cap)
        self.assertEqual(service_cap.type, "service")
        # Ensure no browser launched (service placeholder not instantiated)
        # The extension does not expose a service instance directly.
        self.assertFalse(hasattr(self.extension_instance, "_service") and self.extension_instance._service)

    def test_no_browser_launch_on_import(self):
        # Importing the extension should not have side effects.
        # Ensure that the PlaywrightService class does not attempt import.
        from Extension.playwright.services.playwright_service import PlaywrightService
        service = PlaywrightService()
        self.assertIsNone(service._browser)
        self.assertEqual(service._contexts, {})
        self.assertEqual(service._pages, {})

    def test_no_browser_launch_on_register(self):
        # Registering should not call launch_browser.
        reg = CapabilityRegistry()
        context = ExtensionContext(extension_root=EXT_DIR, manifest=self.manifest, capability_registry=reg)
        # Monkeypatch PlaywrightService.launch_browser to detect calls
        from Extension.playwright.services.playwright_service import PlaywrightService
        called = {"launch": False}
        orig_launch = PlaywrightService.launch_browser
        def fake_launch(self):
            called["launch"] = True
        PlaywrightService.launch_browser = fake_launch
        try:
            self.extension_instance.register(context)
        finally:
            PlaywrightService.launch_browser = orig_launch
        self.assertFalse(called["launch"], "launch_browser should not be called during registration")

if __name__ == "__main__":
    unittest.main()
