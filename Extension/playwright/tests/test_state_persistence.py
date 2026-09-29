"""Task 05 — deterministic tests for browser state persistence (no real browser).

Exercises the storage-state save / restore / list / delete surface against the
in-memory Playwright double, including scope isolation (extension vs project),
survival across a simulated AETHER restart, and the extension's
disable / re-enable lifecycle. Also proves the AETHER Extension storage facade
(``context.storage``) is really used.
"""

import sys
import tempfile
import unittest
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXT_DIR.parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fake_playwright import FakePlaywright, add_page, make_service  # noqa: E402
from Extension.playwright.services.playwright_service import (  # noqa: E402
    PlaywrightService,
    StateNotFoundError,
    StateStoreError,
)


COOKIE_A = {"name": "sid", "value": "abc123", "domain": "local.test", "path": "/"}
COOKIE_B = {"name": "theme", "value": "dark", "domain": "local.test", "path": "/"}


def seed_state(service, session_id):
    """Give a session a cookie + localStorage entry to persist."""
    context = service.context_object(session_id)
    context.set_cookie(COOKIE_A)
    context.set_local_storage("http://local.test", [{"name": "token", "value": "xyz"}])
    return context


# ===========================================================================
# Save / load / list / delete
# ===========================================================================
class TestStateSaveLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        seed_state(self.service, self.session_id)

    def test_save_state_summary(self):
        summary = self.service.save_state(self.session_id, name="demo")
        self.assertTrue(summary["saved"])
        self.assertEqual(summary["name"], "demo")
        self.assertEqual(summary["session_id"], self.session_id)
        self.assertEqual(summary["scope"], "extension")
        self.assertEqual(summary["cookie_count"], 1)
        self.assertEqual(summary["origin_count"], 1)
        # no cookie values leak out of the summary
        self.assertNotIn("cookies", summary)

    def test_load_state_roundtrip(self):
        self.service.save_state(self.session_id, name="demo")
        self.assertTrue(self.service.has_state("demo"))
        self.assertEqual(self.service.list_saved_states(), ["demo"])
        state = self.service.load_state("demo")
        self.assertEqual(state["cookies"][0]["name"], "sid")
        self.assertEqual(state["origins"][0]["origin"], "http://local.test")

    def test_default_name_is_session_id(self):
        summary = self.service.save_state(self.session_id)
        self.assertEqual(summary["name"], self.session_id)
        self.assertIn(summary["name"], self.service.list_saved_states())

    def test_delete_state(self):
        self.service.save_state(self.session_id, name="demo")
        self.assertTrue(self.service.delete_saved_state("demo"))
        self.assertFalse(self.service.has_state("demo"))
        with self.assertRaises(StateNotFoundError):
            self.service.load_state("demo")

    def test_multiple_names_do_not_collide(self):
        self.service.save_state(self.session_id, name="one")
        self.service.save_state(self.session_id, name="two")
        self.assertEqual(self.service.list_saved_states(), ["one", "two"])


# ===========================================================================
# Restore
# ===========================================================================
class TestStateRestore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        seed_state(self.service, self.session_id)
        self.service.save_state(self.session_id, name="demo")

    def test_restore_creates_new_session(self):
        result = self.service.restore_state(name="demo")
        self.assertTrue(result["restored"])
        self.assertTrue(result["created_session"])
        self.assertEqual(result["method"], "new_context")
        self.assertNotEqual(result["session_id"], self.session_id)
        cookies = self.service.context_object(result["session_id"]).cookies()
        self.assertTrue(any(cookie["name"] == "sid" for cookie in cookies))

    def test_restore_into_existing_session(self):
        other = self.service.create_session()
        result = self.service.restore_state(name="demo", session_id=other)
        self.assertFalse(result["created_session"])
        self.assertEqual(result["method"], "add_cookies")
        self.assertTrue(result["cookies_applied"])
        cookies = self.service.context_object(other).cookies()
        self.assertTrue(any(cookie["name"] == "sid" for cookie in cookies))

    def test_restore_unknown_name_raises(self):
        with self.assertRaises(StateNotFoundError):
            self.service.restore_state(name="missing")


# ===========================================================================
# Isolation (name / extension scope / project scope)
# ===========================================================================
class TestStateIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        seed_state(self.service, self.session_id)

    def test_extension_and_project_scopes_are_separate(self):
        project = str(Path(self._tmp.name) / "proj-a")
        Path(project).mkdir(parents=True, exist_ok=True)
        self.service.save_state(self.session_id, name="demo")
        self.service.save_state(self.session_id, name="demo", project=project)
        self.assertIn("demo", self.service.list_saved_states())
        self.assertIn("demo", self.service.list_saved_states(project=project))
        # deleting the extension one leaves the project one intact
        self.service.delete_saved_state("demo")
        self.assertFalse(self.service.has_state("demo"))
        self.assertTrue(self.service.has_state("demo", project=project))

    def test_projects_are_isolated(self):
        proj_a = str(Path(self._tmp.name) / "proj-a")
        proj_b = str(Path(self._tmp.name) / "proj-b")
        Path(proj_a).mkdir(parents=True, exist_ok=True)
        Path(proj_b).mkdir(parents=True, exist_ok=True)
        self.service.save_state(self.session_id, name="shared", project=proj_a)
        self.assertFalse(self.service.has_state("shared", project=proj_b))
        self.assertTrue(self.service.has_state("shared", project=proj_a))


# ===========================================================================
# Survival across a simulated AETHER restart
# ===========================================================================
class TestRestartPersistence(unittest.TestCase):
    def test_state_survives_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service, session_id = make_service(tmp.name)
        seed_state(service, session_id)
        service.save_state(session_id, name="persist")
        service.shutdown()

        # "restart AETHER": a brand new service over the same state dir
        restarted = PlaywrightService(
            runtime_factory=FakePlaywright,
            artifact_dir=tmp.name,
            state_dir=tmp.name,
        )
        self.assertIn("persist", restarted.list_saved_states())
        restarted.launch_browser()
        result = restarted.restore_state(name="persist")
        cookies = restarted.context_object(result["session_id"]).cookies()
        self.assertTrue(any(cookie["name"] == "sid" for cookie in cookies))
        restarted.shutdown()


# ===========================================================================
# Extension lifecycle: disable / re-enable must not delete persisted state
# ===========================================================================
class TestExtensionLifecyclePersistence(unittest.TestCase):
    def _build_extension(self):
        from agent_ai.extensions.capabilities import CapabilityRegistry
        from agent_ai.extensions.context import ExtensionContext
        from agent_ai.extensions.manifest import load_manifest
        import importlib

        manifest = load_manifest(EXT_DIR / "manifest.json")
        registry = CapabilityRegistry()
        context = ExtensionContext(
            extension_root=EXT_DIR, manifest=manifest, capability_registry=registry
        )
        extension = importlib.import_module("Extension.playwright.extension").extension
        # reset the module singleton so each test wires its own context
        extension._service = None
        extension._tools_registered = False
        extension.register(context)
        return context, extension

    def test_get_service_wires_the_context_storage_facade(self):
        context, extension = self._build_extension()
        extension.enable(context)
        self.assertIsNotNone(extension.service._browser_state_store)

    def test_disable_and_reenable_keep_persisted_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        context, extension = self._build_extension()
        # Inject a fake-runtime service bound to the real context.storage facade
        # so no real browser is launched but the persistence path is genuine.
        extension._service = PlaywrightService(
            storage=context.storage,
            extension_id=extension.extension_id,
            runtime_factory=FakePlaywright,
            artifact_dir=tmp.name,
        )
        extension.enable(context)
        service = extension.service
        browser_id = service.launch_browser()
        session_id = service.create_session(browser_id)
        seed_state(service, session_id)
        # project scope keeps the data inside the temp dir
        project = str(Path(tmp.name) / "proj")
        Path(project).mkdir(parents=True, exist_ok=True)
        service.save_state(session_id, name="lifecycle", project=project)

        extension.disable(context)
        self.assertIn("lifecycle", extension.service.list_saved_states(project=project))

        extension.enable(context)
        self.assertIn("lifecycle", extension.service.list_saved_states(project=project))
        state = extension.service.load_state("lifecycle", project=project)
        self.assertEqual(state["cookies"][0]["name"], "sid")
        extension.service.shutdown()


# ===========================================================================
# AETHER Extension storage facade (context.storage) integration
# ===========================================================================
class TestFacadeBackedStore(unittest.TestCase):
    def test_extension_storage_facade_roundtrip_project_scoped(self):
        from agent_ai.extensions.storage import ExtensionStorage

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = str(Path(tmp.name) / "proj")
        Path(project).mkdir(parents=True, exist_ok=True)

        facade = ExtensionStorage(extension_id="aether.playwright")
        service = PlaywrightService(
            storage=facade,
            extension_id="aether.playwright",
            runtime_factory=FakePlaywright,
            artifact_dir=tmp.name,
        )
        service.launch_browser()
        session_id = service.create_session()
        seed_state(service, session_id)
        service.save_state(session_id, name="facade", project=project)
        self.assertTrue(service.has_state("facade", project=project))
        self.assertIn("facade", service.list_saved_states(project=project))
        # project-scoped data must NOT appear in the extension scope
        self.assertNotIn("facade", service.list_saved_states())
        restored = service.restore_state(name="facade", project=project)
        cookies = service.context_object(restored["session_id"]).cookies()
        self.assertTrue(any(cookie["name"] == "sid" for cookie in cookies))
        service.shutdown()

    def test_extension_storage_facade_extension_scope_is_cleaned_up(self):
        from agent_ai.extensions.storage import ExtensionStorage

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        facade = ExtensionStorage(extension_id="aether.playwright")
        service = PlaywrightService(
            storage=facade,
            extension_id="aether.playwright",
            runtime_factory=FakePlaywright,
            artifact_dir=tmp.name,
        )
        service.launch_browser()
        session_id = service.create_session()
        seed_state(service, session_id)
        name = "t05-extension-scope"
        try:
            service.save_state(session_id, name=name)
            self.assertTrue(service.has_state(name))
            self.assertIn(name, service.list_saved_states())
        finally:
            service.delete_saved_state(name)
            service.shutdown()
        self.assertFalse(service.has_state(name))


# ===========================================================================
# Errors
# ===========================================================================
class TestPersistenceErrors(unittest.TestCase):
    def test_no_store_configured_raises(self):
        service = PlaywrightService(runtime_factory=FakePlaywright)
        with self.assertRaises(StateStoreError):
            service.list_saved_states()

    def test_unknown_session_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = make_service(tmp)
            from Extension.playwright.services.playwright_service import SessionNotFoundError

            with self.assertRaises(SessionNotFoundError):
                service.save_state("session-does-not-exist")

    def test_unknown_state_name_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = make_service(tmp)
            with self.assertRaises(StateNotFoundError):
                service.load_state("nope")


# ===========================================================================
# Tool layer
# ===========================================================================
class TestPersistenceToolLayer(unittest.TestCase):
    def test_save_restore_list_delete_tools(self):
        from Extension.playwright.tools.playwright_tools import build_playwright_tools

        with tempfile.TemporaryDirectory() as tmp:
            service, session_id = make_service(tmp)
            seed_state(service, session_id)
            tools = {tool.name: tool for tool in build_playwright_tools(service)}

            saved = tools["aether.playwright.session_save_state"].execute(
                session_id=session_id, name="toollayer"
            )
            self.assertTrue(saved["saved"])

            listed = tools["aether.playwright.session_state_list"].execute()
            self.assertIn("toollayer", listed["states"])

            restored = tools["aether.playwright.session_restore_state"].execute(name="toollayer")
            self.assertTrue(restored["created_session"])

            deleted = tools["aether.playwright.session_state_delete"].execute(name="toollayer")
            self.assertTrue(deleted["deleted"])


if __name__ == "__main__":
    unittest.main()
