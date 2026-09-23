"""Kebijakan tunggal (single source of truth) untuk batas pemindaian Project Map.

Modul ini menentukan **directory dan file apa yang TIDAK boleh dipindai** ketika
Atlas/RIG (dan fingerprint freshness AETHER) membangun Project Map.

Modul ini sengaja berdiri sendiri (stdlib-only, TANPA import paket ``agent_ai``)
agar dapat dimuat juga oleh engine vendored ``vendor/CODE_ATLAS`` dan
``vendor/MAP_CODE_RIG`` lewat path file (lihat ``AETHER_SCAN_POLICY_PATH_ENV``).
Dengan begitu hanya ada SATU daftar policy untuk AETHER, Atlas, dan RIG.

Prinsip:
    - Exclusion diputuskan **sebelum** rekursi (prune-then-descend), bukan
      setelah isi directory dipindai lalu dibuang.
    - Symlink / junction / reparse point TIDAK pernah ditelusuri, sehingga
      traversal tidak keluar dari workspace root.
    - Directory source yang ambigu (``src``, ``app``, ``lib``, ``libs``,
      ``core``, ``common``, ``packages``, ``modules``, ``vendor``, ``pkg``,
      ``external``, ``third_party``) **tidak** di-blacklist: bisa saja berisi
      source code project sendiri.

Catatan: ``dummy_test`` adalah workspace testing terisolasi milik AETHER
(gitignored, bukan bagian source AETHER) sehingga tidak ikut dipetakan.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

__all__ = [
    "EXCLUDED_DIR_NAMES",
    "EXCLUDED_FILE_SUFFIXES",
    "GENERATED_METADATA_DIR_SUFFIXES",
    "AETHER_SCAN_POLICY_ENV",
    "AETHER_SCAN_POLICY_PATH_ENV",
    "POLICY_VERSION",
    "is_excluded_dir",
    "is_excluded_file",
    "prune_dirnames",
    "is_reparse_point",
    "is_within_root",
    "walk_project",
    "iter_files",
    "iter_source_files",
    "to_payload",
    "module_path",
]

#: Versi skema payload env (naikkan bila format berubah).
POLICY_VERSION = 1

#: Environment variable berisi payload JSON policy (dibaca engine vendored).
AETHER_SCAN_POLICY_ENV = "AETHER_SCAN_POLICY"
#: Environment variable berisi path file policy ini (dibaca engine vendored).
AETHER_SCAN_POLICY_PATH_ENV = "AETHER_SCAN_POLICY_PATH"

#: Nama directory yang selalu di-exclude (dibandingkan case-insensitive).
#:
#: Dibagi per kelompok agar mudah dipelihara/diperluas. Semua nama di sini
#: adalah directory yang secara umum BUKAN source project (environment,
#: dependency, cache, build output, metadata VCS, metadata AETHER).
EXCLUDED_DIR_NAMES = frozenset(
    {
        # --- Version control / repository metadata -------------------------
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        "_darcs",
        "cvs",
        # --- Python environment & cache -----------------------------------
        "venv",
        ".venv",
        "virtualenv",
        "env",
        ".env",
        "conda",
        ".conda",
        "miniconda",
        "anaconda",
        "mamba",
        "site-packages",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".pytype",
        ".pyre",
        ".hypothesis",
        ".ipynb_checkpoints",
        ".eggs",
        ".tox",
        ".nox",
        ".pixi",
        # --- JavaScript / Node dependency ---------------------------------
        "node_modules",
        "bower_components",
        "jspm_packages",
        ".npm",
        ".yarn",
        ".pnpm",
        ".pnpm-store",
        ".node-gyp",
        ".parcel-cache",
        ".turbo",
        # --- Cache / package store (tooling umum) -------------------------
        ".cache",
        ".m2",
        ".ivy2",
        ".bundle",
        ".cargo",
        ".gradle",
        # --- Generated / framework cache ----------------------------------
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".angular",
        ".output",
        ".vercel",
        ".netlify",
        ".expo",
        ".dart_tool",
        ".pub-cache",
        ".terraform",
        ".serverless",
        ".cxx",
        ".externalnativebuild",
        "cmakefiles",
        # --- Build / generated output -------------------------------------
        "build",
        "dist",
        "out",
        "target",
        "bin",
        "obj",
        "coverage",
        "htmlcov",
        "_build",
        # --- Editor / IDE metadata ----------------------------------------
        ".idea",
        ".vscode",
        ".vs",
        ".settings",
        # --- Metadata AETHER (Project Map tidak memetakan dirinya sendiri) --
        ".aether",
        # --- Workspace testing terisolasi AETHER --------------------------
        "dummy_test",
    }
)

#: Suffix file yang TIDAK relevan untuk Project Map (binary/compiler/cache).
EXCLUDED_FILE_SUFFIXES = frozenset(
    {
        # Python bytecode / compiled
        ".pyc",
        ".pyo",
        ".pyd",
        ".pyz",
        # Compiler / native objects
        ".class",
        ".o",
        ".obj",
        ".a",
        ".lib",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".wasm",
        # Data / package / cache
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".whl",
        ".egg",
        ".jar",
        ".war",
        ".node",
        ".lock",
        ".log",
        ".cache",
        ".map",
        # Binary asset
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svg",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".tar",
        ".7z",
        ".rar",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
    }
)

#: Suffix directory metadata paket yang di-generate (mis. ``foo.egg-info``).
GENERATED_METADATA_DIR_SUFFIXES: Tuple[str, ...] = (".egg-info", ".dist-info")

#: Suffix file (tuple, di-precompute) untuk pencocokan cepat.
_EXCLUDED_FILE_SUFFIX_TUPLE: Tuple[str, ...] = tuple(sorted(EXCLUDED_FILE_SUFFIXES))


def module_path() -> Path:
    """Path absolut file policy ini (untuk diteruskan ke engine vendored)."""
    return Path(__file__).resolve()


def _as_name_set(names: Optional[Iterable[str]]) -> Set[str]:
    if not names:
        return set()
    return {str(name).strip().lower() for name in names if str(name).strip()}


def is_excluded_dir(name: str, extra: Optional[Iterable[str]] = None) -> bool:
    """Apakah nama directory ini di-exclude (case-insensitive)?"""
    if not name:
        return False
    lowered = str(name).lower()
    if lowered in EXCLUDED_DIR_NAMES:
        return True
    if lowered.endswith(GENERATED_METADATA_DIR_SUFFIXES):
        return True
    if extra and lowered in _as_name_set(extra):
        return True
    return False


def is_excluded_file(name: str, extra: Optional[Iterable[str]] = None) -> bool:
    """Apakah nama file ini di-exclude berdasarkan suffix-nya?"""
    if not name:
        return False
    lowered = str(name).lower()
    if lowered.endswith(_EXCLUDED_FILE_SUFFIX_TUPLE):
        return True
    if extra:
        extra_lower = tuple(str(x).lower() for x in extra)
        if extra_lower and lowered.endswith(extra_lower):
            return True
    return False


def is_reparse_point(path: Union[str, "os.PathLike[str]"]) -> bool:
    """Apakah ``path`` symlink atau junction/reparse point (Windows)?

    Dipakai untuk memastikan traversal tidak mengikuti link keluar workspace.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    mode = getattr(st, "st_mode", 0)
    if mode and stat.S_ISLNK(mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if flag and (attrs & flag):
        return True
    return False


def is_within_root(
    path: Union[str, "os.PathLike[str]"],
    root: Union[str, "os.PathLike[str]"],
) -> bool:
    """Apakah ``path`` berada di dalam ``root`` (perbandingan absolut)?"""
    try:
        candidate = os.path.abspath(path)
        boundary = os.path.abspath(root)
    except (OSError, ValueError):
        return False
    if candidate == boundary:
        return True
    return candidate.startswith(boundary.rstrip(os.sep) + os.sep)


def prune_dirnames(
    dirnames: List[str],
    extra: Optional[Iterable[str]] = None,
    parent: Optional[Union[str, "os.PathLike[str]"]] = None,
) -> List[str]:
    """Pangkas ``dirnames`` IN-PLACE (untuk ``os.walk``) sebelum rekursi.

    Bila ``parent`` diberikan, directory yang berupa symlink/junction juga
    dipangkas. Mengembalikan list yang sama agar mudah dirantai.
    """
    kept: List[str] = []
    for name in dirnames:
        if is_excluded_dir(name, extra):
            continue
        if parent is not None and is_reparse_point(os.path.join(str(parent), name)):
            continue
        kept.append(name)
    dirnames[:] = kept
    return dirnames


def walk_project(
    root: Union[str, "os.PathLike[str]"],
    extra_excluded_dirs: Optional[Iterable[str]] = None,
) -> Iterator[Tuple[str, List[str], List[str]]]:
    """``os.walk`` yang aman: prune directory ter-exclude + symlink/junction.

    Yields ``(dirpath, dirnames, filenames)`` seperti ``os.walk``. Directory
    yang di-exclude TIDAK pernah dimasuki (dipangkas lewat ``dirnames[:]``),
    dan traversal tidak pernah keluar dari ``root``.
    """
    root_str = str(root)
    root_abs = os.path.abspath(root_str)
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        prune_dirnames(dirnames, extra_excluded_dirs, parent=dirpath)
        dirnames.sort()
        if not is_within_root(dirpath, root_abs):
            # Pertahanan berlapis: lewati apa pun yang di luar workspace root.
            dirnames[:] = []
            continue
        yield dirpath, dirnames, sorted(filenames)


def iter_files(
    root: Union[str, "os.PathLike[str]"],
    suffixes: Optional[Sequence[str]] = None,
    extra_excluded_dirs: Optional[Iterable[str]] = None,
) -> Iterator[Path]:
    """Iterasi file di bawah ``root`` (setelah exclusion + boundary check)."""
    suffix_tuple: Optional[Tuple[str, ...]] = None
    if suffixes is not None:
        suffix_tuple = tuple(str(s).lower() for s in suffixes)
    for dirpath, _dirnames, filenames in walk_project(root, extra_excluded_dirs):
        for name in filenames:
            if is_excluded_file(name):
                continue
            if suffix_tuple is not None and not name.lower().endswith(suffix_tuple):
                continue
            yield Path(dirpath) / name


def iter_source_files(
    root: Union[str, "os.PathLike[str]"],
    suffixes: Sequence[str],
    extra_excluded_dirs: Optional[Iterable[str]] = None,
) -> List[Path]:
    """Daftar file source relevan (deterministik, urut menurut path)."""
    files = list(iter_files(root, suffixes, extra_excluded_dirs))
    files.sort(key=lambda path: path.as_posix().lower())
    return files


def to_payload() -> dict:
    """Payload JSON (dikirim ke engine vendored via environment variable)."""
    return {
        "version": POLICY_VERSION,
        "excluded_dir_names": sorted(EXCLUDED_DIR_NAMES),
        "excluded_file_suffixes": sorted(EXCLUDED_FILE_SUFFIXES),
        "generated_metadata_dir_suffixes": list(GENERATED_METADATA_DIR_SUFFIXES),
    }


def policy_from_env(environ: Optional[dict] = None) -> Tuple[Set[str], Set[str]]:
    """Baca policy dari ``AETHER_SCAN_POLICY`` (JSON) -> (dirs, suffixes)."""
    env = os.environ if environ is None else environ
    payload = env.get(AETHER_SCAN_POLICY_ENV)
    if not payload:
        return set(), set()
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return set(), set()
    if not isinstance(data, dict):
        return set(), set()
    dirs = _as_name_set(data.get("excluded_dir_names"))
    suffixes = _as_name_set(data.get("excluded_file_suffixes"))
    return dirs, suffixes
