"""Project Discovery Engine.

Melakukan discovery terkontrol terhadap root project target untuk menghasilkan
context terstruktur (provider-agnostic) yang nantinya dikirim ke LLM guna
membangun Project Intelligence.

Alur:
    ProjectRegistry -> DiscoveryEngine -> DiscoveryResult -> LLM -> Intelligence

Prinsip:
    - READ-ONLY: tidak menulis/mengubah project target.
    - Tidak keluar dari project root (path traversal ditolak).
    - Tidak scan file binary; tidak membaca seluruh isi repository.
    - Abaikan .git/venv/node_modules/__pycache__/build/dist/cache, dll.
    - Batasi jumlah file & ukuran data.
    - Deterministic (urutan stabil).
    - Memakai helper filesystem yang sudah ada (tidak menduplikasi scanner).

Belum ada RAG/embeddings/vector DB/terminal/Git/autonomous learning/web search.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.filesystem import (
    _IGNORED_DIRS,
    _resolve_within_root,
)

# ---------------------------------------------------------------------------
# Batasan discovery (agar tidak membaca seluruh repository).
# ---------------------------------------------------------------------------
_DEFAULT_MAX_FILES = 500
_DEFAULT_MAX_DEPTH = 4
_DEFAULT_MAX_CONTENT_BYTES = 20_000  # per file penting
_DEFAULT_MAX_CONTENT_FILES = 8       # jumlah file yang isinya dibaca

# File penting yang dicari (deterministik).
_IMPORTANT_FILENAMES = {
    "readme.md",
    "readme.rst",
    "readme.txt",
    "readme",
    "license",
    "license.md",
    "license.txt",
    "makefile",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".gitignore",
    ".env.example",
    ".editorconfig",
}

# Manifest dependency / konfigurasi.
_MANIFEST_FILENAMES = {
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "tsconfig.json",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "gemfile",
    "gemfile.lock",
    "pubspec.yaml",
    "environment.yml",
    "conda.yml",
}

# File konfigurasi penting.
_CONFIG_FILENAMES = {
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "mypy.ini",
    ".flake8",
    ".pre-commit-config.yaml",
    "tsconfig.json",
    "webpack.config.js",
    "vite.config.js",
    "vite.config.ts",
    "next.config.js",
    "nuxt.config.js",
    "angular.json",
    "vue.config.js",
    "rollup.config.js",
    "jest.config.js",
    "babel.config.js",
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.js",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".env.example",
}

# Entry point yang umum terlihat.
_ENTRYPOINT_NAMES = {
    "main.py",
    "app.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "__main__.py",
    "cli.py",
    "run.py",
    "server.py",
    "index.js",
    "index.ts",
    "main.js",
    "main.ts",
    "app.js",
    "app.ts",
    "server.js",
    "server.ts",
    "main.go",
    "main.rs",
    "main.java",
    "program.cs",
}

# Ekstensi yang dianggap binary (tidak dibaca isinya).
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so",
    ".dylib", ".bin", ".pyc", ".pyo", ".class", ".jar", ".war", ".o", ".a",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".woff", ".woff2", ".ttf", ".eot",
    ".db", ".sqlite", ".sqlite3", ".lock",
}

# Pemetaan ekstensi -> bahasa.
_LANGUAGE_BY_EXTENSION = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sql": "SQL",
    ".dart": "Dart",
    ".lua": "Lua",
    ".r": "R",
    ".m": "Objective-C",
    ".vue": "Vue",
    ".svelte": "Svelte",
}

# Deteksi framework dari nama file manifest/config.
_FRAMEWORK_HINTS = {
    "manage.py": "Django",
    "next.config.js": "Next.js",
    "nuxt.config.js": "Nuxt",
    "angular.json": "Angular",
    "vue.config.js": "Vue",
    "svelte.config.js": "Svelte",
    "vite.config.js": "Vite",
    "vite.config.ts": "Vite",
    "webpack.config.js": "Webpack",
    "composer.json": "Composer/PHP",
    "gemfile": "Ruby/Bundler",
    "go.mod": "Go Modules",
    "cargo.toml": "Cargo/Rust",
    "pom.xml": "Maven/Java",
    "build.gradle": "Gradle/Java",
    "pubspec.yaml": "Flutter/Dart",
}


@dataclass
class DiscoveryResult:
    """Hasil discovery project (provider-agnostic, JSON-friendly)."""

    root: str
    name: str = ""
    directory_tree: List[Dict[str, Any]] = field(default_factory=list)
    important_files: List[str] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    dependency_files: List[str] = field(default_factory=list)
    config_files: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    frameworks: List[str] = field(default_factory=list)
    readme: Optional[Dict[str, Any]] = None
    file_contents: Dict[str, str] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "name": self.name,
            "directory_tree": self.directory_tree,
            "important_files": self.important_files,
            "entry_points": self.entry_points,
            "dependency_files": self.dependency_files,
            "config_files": self.config_files,
            "languages": self.languages,
            "frameworks": self.frameworks,
            "readme": self.readme,
            "file_contents": self.file_contents,
            "stats": self.stats,
            "truncated": self.truncated,
        }


class DiscoveryEngine:
    """Discovery terkontrol terhadap root project target (read-only).

    Args:
        max_files: batas jumlah file yang dipindai.
        max_depth: kedalaman maksimum directory tree.
        max_content_bytes: batas ukuran isi per file penting.
        max_content_files: batas jumlah file yang isinya dibaca.
    """

    def __init__(
        self,
        max_files: int = _DEFAULT_MAX_FILES,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        max_content_bytes: int = _DEFAULT_MAX_CONTENT_BYTES,
        max_content_files: int = _DEFAULT_MAX_CONTENT_FILES,
    ) -> None:
        self.max_files = max_files
        self.max_depth = max_depth
        self.max_content_bytes = max_content_bytes
        self.max_content_files = max_content_files

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def discover(self, root: str, name: str = "") -> DiscoveryResult:
        """Jalankan discovery terhadap root project.

        Args:
            root: absolute path root project target.
            name: nama project (opsional).

        Returns:
            DiscoveryResult.

        Raises:
            ToolValidationError: bila root tidak valid / bukan directory.
        """
        root_path = Path(root).resolve()
        if not root_path.exists() or not root_path.is_dir():
            from agent_ai.tools.base import ToolValidationError

            raise ToolValidationError(f"Root project tidak valid: {root}")

        result = DiscoveryResult(root=str(root_path), name=name)

        files: List[Path] = []
        tree: List[Dict[str, Any]] = []
        self._walk(root_path, root_path, 0, files, tree, result)

        result.directory_tree = tree
        result.stats = self._compute_stats(root_path, files)
        self._classify(root_path, files, result)
        self._detect_languages(files, result)
        self._detect_frameworks(result)
        self._collect_contents(root_path, result)
        return result

    # ------------------------------------------------------------------ #
    # Walk (deterministic, bounded, read-only)
    # ------------------------------------------------------------------ #
    def _walk(
        self,
        base: Path,
        root: Path,
        depth: int,
        files: List[Path],
        tree: List[Dict[str, Any]],
        result: DiscoveryResult,
    ) -> None:
        """Telusuri directory secara deterministik, melewati yang diabaikan."""
        if depth > self.max_depth:
            return
        try:
            children = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return

        for child in children:
            if child.name in _IGNORED_DIRS:
                continue
            if len(files) >= self.max_files:
                result.truncated = True
                return

            rel = str(child.relative_to(root)).replace("\\", "/")
            if child.is_dir():
                tree.append({"path": rel, "type": "dir", "depth": depth})
                self._walk(child, root, depth + 1, files, tree, result)
            elif child.is_file():
                files.append(child)
                tree.append({"path": rel, "type": "file", "depth": depth})

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    def _classify(self, root: Path, files: List[Path], result: DiscoveryResult) -> None:
        """Klasifikasikan file penting/entry point/dependency/config."""
        for path in files:
            rel = str(path.relative_to(root)).replace("\\", "/")
            lower = path.name.lower()

            if lower in _IMPORTANT_FILENAMES:
                result.important_files.append(rel)
            if lower in _ENTRYPOINT_NAMES:
                result.entry_points.append(rel)
            if lower in _MANIFEST_FILENAMES:
                result.dependency_files.append(rel)
            if lower in _CONFIG_FILENAMES:
                result.config_files.append(rel)

        result.important_files.sort()
        result.entry_points.sort()
        result.dependency_files.sort()
        result.config_files.sort()

    def _detect_languages(self, files: List[Path], result: DiscoveryResult) -> None:
        """Deteksi bahasa dari ekstensi file (deterministik)."""
        found: Dict[str, int] = {}
        for path in files:
            lang = _LANGUAGE_BY_EXTENSION.get(path.suffix.lower())
            if lang:
                found[lang] = found.get(lang, 0) + 1
        # Urutkan berdasarkan jumlah terbanyak lalu nama (deterministik).
        result.languages = [lang for lang, _ in sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))]

    def _detect_frameworks(self, result: DiscoveryResult) -> None:
        """Deteksi framework dari nama file manifest/config/entry point."""
        names = {
            Path(p).name.lower()
            for p in result.dependency_files + result.config_files + result.entry_points
        }
        frameworks = {_FRAMEWORK_HINTS[n] for n in names if n in _FRAMEWORK_HINTS}
        result.frameworks = sorted(frameworks)

    # ------------------------------------------------------------------ #
    # Content collection (bounded, no binary)
    # ------------------------------------------------------------------ #
    def _collect_contents(self, root: Path, result: DiscoveryResult) -> None:
        """Baca isi terbatas dari README + file penting (bukan binary)."""
        candidates: List[str] = []
        # README lebih dulu.
        for rel in result.important_files:
            if Path(rel).name.lower().startswith("readme"):
                candidates.append(rel)
        # Lalu dependency/config/entry point.
        for rel in result.dependency_files + result.config_files + result.entry_points:
            if rel not in candidates:
                candidates.append(rel)

        for rel in candidates[: self.max_content_files]:
            path = root / rel
            if path.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > self.max_content_bytes:
                text = text[: self.max_content_bytes]
                result.truncated = True
            result.file_contents[rel] = text
            if Path(rel).name.lower().startswith("readme"):
                result.readme = {"path": rel, "content": text}

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    def _compute_stats(self, root: Path, files: List[Path]) -> Dict[str, Any]:
        """Statistik umum project (jumlah file, ukuran, ekstensi)."""
        total_bytes = 0
        by_ext: Dict[str, int] = {}
        for path in files:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
            ext = path.suffix.lower() or "(none)"
            by_ext[ext] = by_ext.get(ext, 0) + 1

        top_ext = sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        return {
            "file_count": len(files),
            "total_bytes": total_bytes,
            "top_extensions": [{"ext": e, "count": c} for e, c in top_ext],
        }

    # ------------------------------------------------------------------ #
    # Safety helper (dipakai untuk validasi path relatif)
    # ------------------------------------------------------------------ #
    @staticmethod
    def resolve_within_root(path: str, root: str) -> Path:
        """Resolve path relatif terhadap root, tolak traversal keluar root."""
        return _resolve_within_root(path, Path(root))
