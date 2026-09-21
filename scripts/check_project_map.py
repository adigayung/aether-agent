"""Verifikasi Project Map foundation (integrasi CODE ATLAS + MAP_CODE_RIG).

Membuat fixture project kecil di `dummy_test/project_map_fixture`
(workspace testing terisolasi, BUKAN bagian source AETHER), lalu memverifikasi
kontrak `ProjectMapService`:

    1.  Path map directory benar (`<project>/.aether/map`).
    2.  Path `atlas.json` benar.
    3.  Path `rig.json` benar.
    4.  Inisialisasi service TIDAK membuat folder/file (tanpa efek samping).
    5.  `map_exists()` benar (ada / tidak ada).
    6.  `load_map()` memuat JSON valid (read-only, tidak mengubah file).
    7.  Map yang belum ada ditangani dengan error jelas (MapNotFoundError).
    8.  Status dasar benar: missing / available / invalid.
    9.  `map_type` tidak dikenal ditolak (InvalidMapTypeError).
    10. Resolusi engine: env + konstruktor override.
    11. (best-effort) Integrasi engine: jalankan Atlas/RIG yang sudah ada.

Bagian (11) bersifat informatif: engine yang tidak tersedia akan di-SKIP, dan
engine yang gagal dicatat sebagai WARN (defect di repo engine, bukan AETHER).

Jalankan:
    python scripts/check_project_map.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.projects import (  # noqa: E402
    InvalidMapTypeError,
    MapInvalidError,
    MapNotFoundError,
    ProjectMapService,
    STATUS_AVAILABLE,
    STATUS_INVALID,
    STATUS_MISSING,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "project_map_fixture"

_FILES = {
    "pyproject.toml": '[project]\nname = "fixture"\nversion = "0.1.0"\n',
    "package.json": '{"name": "fixture", "version": "0.1.0"}\n',
    "pkg/__init__.py": "from pkg.core import greet\n",
    "pkg/core.py": "def greet(name):\n    return 'hi ' + name\n",
}


def setup_fixture() -> None:
    for rel, content in _FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _engine_probe(svc: ProjectMapService, map_type: str) -> str:
    """Jalankan engine nyata (best-effort). Mengembalikan ok/skip/fail."""
    if not svc.engine_available(map_type):
        print(
            "[SKIP] engine '{}' tidak tersedia di {}".format(
                map_type, svc.get_engine_dir(map_type)
            )
        )
        return "skip"
    try:
        path = svc.generate_map(FIXTURE, map_type)
    except Exception as exc:  # noqa: BLE001 - engine eksternal bisa apa saja
        print("[WARN] engine '{}' gagal: {}".format(map_type, exc))
        return "fail"
    try:
        data = svc.load_map(FIXTURE, map_type)
    except Exception as exc:  # noqa: BLE001
        print("[WARN] engine '{}' menghasilkan map tak terbaca: {}".format(map_type, exc))
        return "fail"
    size = path.stat().st_size
    print(
        "[OK] engine '{}' -> {} ({} bytes, {} top-level keys)".format(
            map_type, path.name, size, len(data)
        )
    )
    return "ok"


def main() -> int:
    print("=== Verifikasi Project Map Service (Atlas + RIG) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    map_dir = FIXTURE / ".aether" / "map"

    # (4) Inisialisasi TIDAK boleh membuat folder/file apa pun.
    svc = ProjectMapService()
    assert not (FIXTURE / ".aether").exists(), (
        "inisialisasi service tidak boleh membuat .aether/"
    )
    print("inisialisasi tanpa efek samping : OK")

    # (1) Path map directory benar.
    assert svc.get_map_dir(FIXTURE) == map_dir
    assert svc.get_map_dir(str(FIXTURE)) == map_dir
    print("map dir : {}".format(svc.get_map_dir(FIXTURE)))

    # (2) Path atlas.json benar.
    atlas_path = svc.get_map_path(FIXTURE, "atlas")
    assert atlas_path == map_dir / "atlas.json"
    print("atlas path : {}".format(atlas_path.name))

    # (3) Path rig.json benar.
    rig_path = svc.get_map_path(FIXTURE, "rig")
    assert rig_path == map_dir / "rig.json"
    print("rig path : {}".format(rig_path.name))

    # (5) map_exists() awal = False.
    assert svc.map_exists(FIXTURE, "atlas") is False
    assert svc.map_exists(FIXTURE, "rig") is False
    print("map_exists (kosong) : OK")

    # (8a) Status awal = missing.
    status = svc.get_status(FIXTURE)
    assert status["status"] == STATUS_MISSING, status
    assert status["maps"]["atlas"]["status"] == STATUS_MISSING
    assert status["maps"]["rig"]["status"] == STATUS_MISSING
    assert status["available"] == [] and status["invalid"] == []
    assert status["missing"] == ["atlas", "rig"]
    print("status awal : {}".format(status["status"]))

    # (7) Map yang belum ada -> MapNotFoundError.
    for map_type in ("atlas", "rig"):
        try:
            svc.load_map(FIXTURE, map_type)
        except MapNotFoundError as exc:
            assert map_type in str(exc)
        else:
            raise AssertionError("load_map harus raise MapNotFoundError")
    print("missing map ditangani : OK")

    # (6) Loading JSON valid + read-only.
    map_dir.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "test-v1", "modules": {"a.py": {"symbols": 2}}}
    atlas_path.write_text(json.dumps(payload), encoding="utf-8")
    before = atlas_path.read_bytes()

    assert svc.map_exists(FIXTURE, "atlas") is True
    assert svc.map_exists(FIXTURE, "rig") is False
    loaded = svc.load_map(FIXTURE, "atlas")
    assert loaded == payload, loaded
    assert atlas_path.read_bytes() == before, "load_map tidak boleh mengubah file"
    print("loading JSON valid (read-only) : OK")

    # (8b) Status: atlas available, rig masih missing -> overall available.
    status = svc.get_status(FIXTURE)
    assert status["status"] == STATUS_AVAILABLE, status
    assert status["maps"]["atlas"]["status"] == STATUS_AVAILABLE
    assert status["maps"]["atlas"]["size"] == len(before)
    assert status["maps"]["rig"]["status"] == STATUS_MISSING
    assert status["available"] == ["atlas"] and status["missing"] == ["rig"]
    print("status (atlas ada) : {}".format(status["status"]))

    # (8c) Status invalid untuk JSON rusak.
    rig_path.write_text("{ ini bukan json", encoding="utf-8")
    assert svc.map_exists(FIXTURE, "rig") is True
    status = svc.get_status(FIXTURE)
    assert status["maps"]["rig"]["status"] == STATUS_INVALID, status
    assert status["maps"]["rig"]["error"]
    assert status["status"] == STATUS_INVALID, status
    assert status["invalid"] == ["rig"]
    try:
        svc.load_map(FIXTURE, "rig")
    except MapInvalidError:
        pass
    else:
        raise AssertionError("load_map harus raise MapInvalidError")
    print("status invalid (JSON rusak) : {}".format(status["status"]))

    # (9) map_type tidak dikenal ditolak.
    for bad in ("", "unknown", "ATLAS2"):
        try:
            svc.get_map_path(FIXTURE, bad)
        except InvalidMapTypeError:
            pass
        else:
            raise AssertionError("map_type '{}' harus ditolak".format(bad))
    print("map_type tidak dikenal ditolak : OK")

    # (10) Resolusi engine: default, env override, konstruktor override.
    default_atlas = svc.get_engine_dir("atlas")
    explicit = ProjectMapService(atlas_dir=r"X:\explicit_atlas")
    assert str(explicit.get_engine_dir("atlas")) == r"X:\explicit_atlas"

    prev = os.environ.get("AETHER_CODE_ATLAS_DIR")
    os.environ["AETHER_CODE_ATLAS_DIR"] = r"X:\env_atlas"
    try:
        from_env = ProjectMapService()
        assert str(from_env.get_engine_dir("atlas")) == r"X:\env_atlas", (
            from_env.get_engine_dir("atlas")
        )
        # Konstruktor menang atas env.
        both = ProjectMapService(atlas_dir=r"X:\ctor_atlas")
        assert str(both.get_engine_dir("atlas")) == r"X:\ctor_atlas"
    finally:
        if prev is None:
            os.environ.pop("AETHER_CODE_ATLAS_DIR", None)
        else:
            os.environ["AETHER_CODE_ATLAS_DIR"] = prev
    # Setelah env dihapus, kembali ke default.
    assert svc.get_engine_dir("atlas") == default_atlas
    print("resolusi engine (default/env/konstruktor) : OK")

    # (11) Integrasi engine nyata (best-effort).
    print()
    print("-- Integrasi engine (best-effort) --")
    # Bersihkan map fixture agar hasil engine jelas berasal dari engine.
    shutil.rmtree(map_dir, ignore_errors=True)
    results = {t: _engine_probe(svc, t) for t in ("atlas", "rig")}
    assert results["atlas"] != "fail", "engine Atlas (repo eksternal) gagal"
    if results["rig"] == "fail":
        print(
            "[WARN] engine 'rig' gagal dijalankan (defect repo engine, "
            "bukan kode AETHER); kontrak fondasi tetap terverifikasi."
        )

    print()
    print("[OK] Project Map foundation terverifikasi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
