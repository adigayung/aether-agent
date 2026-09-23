"""Verifikasi scan boundary Project Map (Atlas + RIG).

Membuat fixture project di `dummy_test/project_map_scan_fixture` (workspace
testing terisolasi, BUKAN source AETHER) dengan struktur yang memuat source
project ASLI dan directory environment/dependency/build/metadata, lalu
memverifikasi bahwa:

    1. Traversal Atlas memangkas directory ter-exclude SEBELUM rekursi.
    2. Traversal RIG tidak masuk dependency/environment tree.
    3. Source project valid TETAP terpetakan (src/, tests/, vendor/, lib/).
    4. Directory ambigu (vendor, lib) TIDAK dibuang membabi buta.
    5. `.aether/map/` tidak ikut dipindai.
    6. symlink/junction tidak ditelusuri (workspace boundary) [best-effort].
    7. Atlas & RIG tetap menghasilkan JSON valid.
    8. Query tools (atlas_query / rig_query / project_map_status) tetap bekerja.
    9. Engine tetap benar saat dijalankan LANGSUNG (tanpa env AETHER).

Fixture memakai engine Atlas/RIG vendored yang NYATA (bukan fake engine) supaya
yang diuji benar-benar policy traversal engine.

Jalankan:
    python scripts/check_project_map_scan_boundary.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.projects.project_map import (  # noqa: E402
    MAP_TYPE_ATLAS,
    MAP_TYPE_RIG,
    STATUS_AVAILABLE,
    ProjectMapService,
)
from agent_ai.projects.project_map_query import atlas_query, rig_query  # noqa: E402
from agent_ai.projects import scan_policy  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "project_map_scan_fixture"
EXTERNAL = DUMMY_ROOT / "project_map_scan_external"
CACHE_DIR = DUMMY_ROOT / "project_map_scan_engineout"

#: Path (relatif, '/' separated) yang HARUS masuk Project Map.
EXPECTED_PRESENT = (
    "src/main.py",
    "src/module.py",
    "tests/test_main.py",
    "vendor/internal_source.py",
    "lib/internal_lib.py",
)

#: Nama directory (lowercase) yang TIDAK boleh muncul sebagai komponen path.
EXCLUDED_COMPONENTS = (
    "conda",
    ".conda",
    "venv",
    ".venv",
    "env",
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "build",
    "dist",
    "target",
    "out",
    "bin",
    "obj",
    "coverage",
    "htmlcov",
    "site-packages",
    ".aether",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "bower_components",
    ".cache",
    ".next",
    ".nuxt",
    ".dart_tool",
    ".gradle",
)

_PRESENT_FILE = "def present_marker_function():\n    return 1\n"
_PRESENT_MAIN = (
    "from src.module import module_helper\n"
    "\n"
    "\n"
    "def main():\n"
    "    return module_helper()\n"
)
_PRESENT_MODULE = "def module_helper():\n    return 42\n"
_PRESENT_TEST = (
    "from src.main import main\n"
    "\n"
    "\n"
    "def test_main():\n"
    "    assert main() == 42\n"
)
_PRESENT_VENDOR = "def internal_source_function():\n    return 'vendor'\n"
_PRESENT_LIB = "def internal_lib_function():\n    return 'lib'\n"
_EXCLUDED_PY = "def excluded_dependency_function():\n    return 'should not be scanned'\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
def setup_fixture() -> None:
    teardown_fixture()

    # --- source project yang HARUS ikut terpetakan ------------------------
    _write(FIXTURE / "pyproject.toml", "[project]\nname = \"scan-fixture\"\nversion = \"0.1.0\"\n")
    _write(FIXTURE / "src" / "main.py", _PRESENT_MAIN)
    _write(FIXTURE / "src" / "module.py", _PRESENT_MODULE)
    _write(FIXTURE / "tests" / "test_main.py", _PRESENT_TEST)
    _write(FIXTURE / "vendor" / "internal_source.py", _PRESENT_VENDOR)
    _write(FIXTURE / "lib" / "internal_lib.py", _PRESENT_LIB)

    # --- directory yang TIDAK boleh dipindai ------------------------------
    _write(FIXTURE / "conda" / "Lib" / "site-packages" / "fake_dependency.py", _EXCLUDED_PY)
    _write(FIXTURE / "venv" / "Lib" / "site-packages" / "fake_dependency.py", _EXCLUDED_PY)
    _write(FIXTURE / ".venv" / "Lib" / "site-packages" / "fake_dependency.py", _EXCLUDED_PY)
    _write(FIXTURE / "node_modules" / "fake_package" / "package.js", "module.exports = 1;\n")
    _write(FIXTURE / "node_modules" / "fake_package" / "index.py", _EXCLUDED_PY)
    _write(FIXTURE / ".git" / "hooks" / "run.py", _EXCLUDED_PY)
    _write(FIXTURE / ".git" / "config", "[core]\n")
    _write(FIXTURE / "__pycache__" / "cached.py", _EXCLUDED_PY)
    _write(FIXTURE / "__pycache__" / "cached.pyc", "not really bytecode\n")
    _write(FIXTURE / "build" / "generated.py", _EXCLUDED_PY)
    _write(FIXTURE / "dist" / "bundle.py", _EXCLUDED_PY)
    _write(FIXTURE / "dist" / "bundle.js", "console.log(1);\n")
    _write(FIXTURE / "target" / "artifact.py", _EXCLUDED_PY)
    _write(FIXTURE / ".aether" / "map" / "atlas.json", "{}\n")
    _write(FIXTURE / ".aether" / "map" / "rig.json", "{}\n")
    _write(FIXTURE / ".aether" / "bible" / "facts.md", "# facts\n")

    # --- boundary test (best-effort): link ke tree eksternal --------------
    _write(EXTERNAL / "outside_source.py", _EXCLUDED_PY)
    _make_dir_link(FIXTURE / "linked_external", EXTERNAL)


def teardown_fixture() -> None:
    # Hapus link DULU agar rmtree tidak menelusuri tree eksternal.
    _remove_dir_link(FIXTURE / "linked_external")
    shutil.rmtree(FIXTURE, ignore_errors=True)
    shutil.rmtree(EXTERNAL, ignore_errors=True)
    shutil.rmtree(CACHE_DIR, ignore_errors=True)


def _make_dir_link(link_path: Path, target: Path) -> bool:
    """Buat junction/symlink directory (best-effort; False bila tidak didukung)."""
    if link_path.exists() or link_path.is_symlink():
        _remove_dir_link(link_path)
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return True
        except OSError:
            pass
    try:
        os.symlink(str(target), str(link_path), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def _remove_dir_link(link_path: Path) -> None:
    if not (link_path.is_symlink() or link_path.exists()):
        return
    try:
        os.rmdir(str(link_path))  # junction / dir-symlink / dir kosong
    except OSError:
        try:
            os.unlink(str(link_path))
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _collect_paths(obj: Any, keys: Iterable[str], out: Optional[List[str]] = None) -> List[str]:
    """Kumpulkan nilai string dari key tertentu secara rekursif."""
    if out is None:
        out = []
    key_set = set(keys)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in key_set and isinstance(value, str):
                out.append(value)
            else:
                _collect_paths(value, key_set, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_paths(item, key_set, out)
    return out


def _atlas_paths(map_data: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for name in map_data.get("files") or []:
        out.append(str(name))
    for module in (map_data.get("modules") or {}).values():
        if isinstance(module, dict) and module.get("file"):
            out.append(str(module["file"]))
    return out


def _rig_paths(map_data: Dict[str, Any]) -> List[str]:
    return _collect_paths(map_data, ("file_path", "path"))


def _normalize(paths: Iterable[str]) -> List[str]:
    normalized = []
    for path in paths:
        text = str(path).replace("\\", "/")
        if text.startswith("./"):
            text = text[2:]
        normalized.append(text)
    return normalized


def _violations(paths: Iterable[str]) -> List[str]:
    found = []
    for path in _normalize(paths):
        parts = {part.lower() for part in path.split("/") if part}
        for component in EXCLUDED_COMPONENTS:
            if component in parts:
                found.append(path)
                break
    return sorted(set(found))


def _missing_expected(paths: Iterable[str]) -> List[str]:
    present = set(_normalize(paths))
    return [expected for expected in EXPECTED_PRESENT if expected not in present]


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_engine_policy_alone() -> None:
    """Engine standalone (TANPA env AETHER) harus tetap memakai policy bersama."""
    print("-- engine standalone (tanpa env AETHER) --")
    out_dir = CACHE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    for map_type, filename in ((MAP_TYPE_ATLAS, "atlas.json"), (MAP_TYPE_RIG, "rig.json")):
        entry = PROJECT_ROOT / "vendor" / (
            "CODE_ATLAS" if map_type == MAP_TYPE_ATLAS else "MAP_CODE_RIG"
        ) / ("atlas.py" if map_type == MAP_TYPE_ATLAS else "rig.py")
        out_path = out_dir / filename
        cmd = [sys.executable, str(entry), str(FIXTURE), str(out_path)]
        if map_type == MAP_TYPE_RIG:
            cmd.append("--overwrite")
        env = os.environ.copy()
        env.pop(scan_policy.AETHER_SCAN_POLICY_ENV, None)
        env.pop(scan_policy.AETHER_SCAN_POLICY_PATH_ENV, None)
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        _expect(result.returncode == 0, "engine {} standalone gagal: {}".format(
            map_type, (result.stderr or "")[-400:]
        ))
        data = json.loads(out_path.read_text(encoding="utf-8"))
        paths = _atlas_paths(data) if map_type == MAP_TYPE_ATLAS else _rig_paths(data)
        bad = _violations(paths)
        _expect(not bad, "engine {} standalone memindai path ter-exclude: {}".format(
            map_type, bad[:10]
        ))
        missing = _missing_expected(paths)
        _expect(not missing, "engine {} standalone kehilangan source: {}".format(
            map_type, missing
        ))
        print("[OK] engine '{}' standalone: boundary benar + source lengkap".format(map_type))


def check_service_boundary() -> None:
    print()
    print("-- Project Map service (env boundary bridge) --")
    service = ProjectMapService()

    for map_type in (MAP_TYPE_ATLAS, MAP_TYPE_RIG):
        _expect(service.engine_available(map_type), "engine {} tidak tersedia".format(map_type))
        service.generate_map(FIXTURE, map_type)

    status = service.get_status(FIXTURE)
    _expect(
        status["maps"][MAP_TYPE_ATLAS]["status"] == STATUS_AVAILABLE,
        "atlas map tidak available",
    )
    _expect(
        status["maps"][MAP_TYPE_RIG]["status"] == STATUS_AVAILABLE,
        "rig map tidak available",
    )

    atlas = service.load_map(FIXTURE, MAP_TYPE_ATLAS)
    rig = service.load_map(FIXTURE, MAP_TYPE_RIG)
    atlas_paths = _normalize(_atlas_paths(atlas))
    rig_paths = _normalize(_rig_paths(rig))

    # (1) path ter-exclude tidak boleh masuk graph.
    for map_type, paths in ((MAP_TYPE_ATLAS, atlas_paths), (MAP_TYPE_RIG, rig_paths)):
        bad = _violations(paths)
        _expect(not bad, "{} memindai path ter-exclude: {}".format(map_type, bad[:15]))
        print("[OK] {} tidak memindai directory ter-exclude".format(map_type))

    # (2) dependency/environment tree spesifik tidak muncul sebagai source node.
    for needle in ("site-packages", "conda/", "venv/", "node_modules"):
        for map_type, paths in ((MAP_TYPE_ATLAS, atlas_paths), (MAP_TYPE_RIG, rig_paths)):
            _expect(
                not any(needle in path for path in paths),
                "{} memuat '{}' sebagai source node".format(map_type, needle),
            )
    print("[OK] site-packages/conda/venv/node_modules tidak muncul sebagai node")

    # (3) `.aether/map` tidak dipindai.
    for path in atlas_paths + rig_paths:
        _expect(
            not path.startswith(".aether/"),
            "map/dependency tidak boleh memuat .aether: {}".format(path),
        )
    print("[OK] .aether (termasuk .aether/map) tidak dipindai")

    # (4) source ambigu (vendor/lib) TIDAK dibuang membabi buta.
    for map_type, paths in ((MAP_TYPE_ATLAS, atlas_paths), (MAP_TYPE_RIG, rig_paths)):
        missing = _missing_expected(paths)
        _expect(
            not missing,
            "{} kehilangan source valid: {}".format(map_type, missing),
        )
        present = set(paths)
        _expect("vendor/internal_source.py" in present, "vendor/ dibuang (salah)")
        _expect("lib/internal_lib.py" in present, "lib/ dibuang (salah)")
        print("[OK] {} tetap menemukan source (src/tests/vendor/lib)".format(map_type))

    # (5) symlink/junction boundary (best-effort).
    linked = FIXTURE / "linked_external"
    if linked.exists() and (linked.is_symlink() or os.path.isdir(linked)):
        _expect(
            not any(path.startswith("linked_external/") for path in atlas_paths + rig_paths),
            "traversal mengikuti symlink/junction keluar workspace",
        )
        print("[OK] symlink/junction tidak ditelusuri (workspace boundary)")
    else:
        print("[SKIP] symlink/junction tidak dapat dibuat di environment ini")

    # (6) query tools tetap bekerja.
    atlas_result = atlas_query(service, FIXTURE, "internal_source_function")
    _expect(
        len(atlas_result.get("results") or []) >= 1,
        "atlas_query tidak menemukan symbol di vendor/ (results kosong)",
    )
    rig_result = rig_query(service, FIXTURE, "internal_lib_function")
    _expect(
        len(rig_result.get("results") or []) >= 1,
        "rig_query tidak menemukan entity di lib/",
    )
    print("[OK] atlas_query + rig_query tetap bekerja")

    # (7) freshness fingerprint tidak terpengaruh churn di dummy_test engine-out.
    fp_before, _ = service.compute_source_fingerprint(FIXTURE)
    out_dir = CACHE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cache_churn.py").write_text("x = 1\n", encoding="utf-8")
    fp_after, _ = service.compute_source_fingerprint(FIXTURE)
    _expect(fp_before == fp_after, "fingerprint berubah karena churn di luar fixture")
    print("[OK] fingerprint tidak terpengaruh directory di luar project")

    # (8) map tetap fresh setelah generate.
    freshness = service.get_status(FIXTURE)["freshness"]
    _expect(freshness == "fresh", "freshness={} (harus fresh)".format(freshness))
    print("[OK] freshness map = fresh setelah generate")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("=== Verifikasi scan boundary Project Map (Atlas + RIG) ===")
    print("fixture : {}".format(FIXTURE))
    setup_fixture()
    try:
        check_engine_policy_alone()
        check_service_boundary()
    finally:
        teardown_fixture()
    print()
    print("[OK] scan boundary Project Map terverifikasi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
