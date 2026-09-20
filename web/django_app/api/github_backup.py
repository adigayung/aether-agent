"""GitHub Backup (checkpoint/recovery) — layer gateway AETHER.

Fitur OPTIONAL per project. Menggabungkan tiga kemampuan yang SUDAH ada /
diperluas secara ADDITIVE:

    1. Git Awareness Foundation (src/agent_ai/git)  -> DIPAKAI ULANG untuk
       operasi READ (status/log/is_repository/current_branch) lewat
       `GitRepositoryFacade` + `SubprocessGitClient` yang sudah ada. Paket
       `agent_ai.git` tetap READ-ONLY; operasi tulis Git ditambahkan di layer
       gateway (modul ini), bukan dengan menambah mutasi ke paket read-only.
    2. Project-local `.aether` store (src/agent_ai/projects/github_backup.py)
       -> lokasi metadata private per project (`.aether/github/`).
    3. Credential protection Windows (DPAPI) via `WindowsCredentialProtector`.

Boundary:
    - Token GitHub HANYA hidup di memory proses gateway selama request.
    - Token TIDAK pernah ditulis plaintext, tidak pernah dikembalikan ke
      frontend, tidak masuk commit message, tidak masuk error string (di-redact),
      dan tidak pernah dikirim ke LLM/Agent/Consultant.
    - Core `src/agent_ai` tidak bergantung pada Django; modul ini di gateway.
"""

from __future__ import annotations

import ctypes
import fnmatch
import os
import subprocess
import sys
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent_ai.git.models import GitCommit
from agent_ai.git.repository import GitRepositoryFacade
from agent_ai.projects.github_backup import (
    AETHER_DIR_NAME,
    GithubBackupConfig,
    GithubBackupStore,
    _normalize_exclude,
    aether_is_ignored,
    ensure_aether_ignored,
)


class GithubBackupError(Exception):
    """Base error fitur GitHub Backup (dipetakan ke HTTP oleh service)."""


class CredentialProtectionError(GithubBackupError):
    """Credential terenkripsi tidak dapat didekripsi di mesin/user ini."""


# --------------------------------------------------------------------------- #
# Credential protection — Windows DPAPI (CryptProtectData/CryptUnprotectData).
# Tidak meminta password tambahan dari user; terikat ke Windows user/machine
# sehingga credential TIDAK otomatis dapat didekripsi di komputer/user lain.
# --------------------------------------------------------------------------- #
class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class WindowsCredentialProtector:
    """Proteksi credential memakai Windows DPAPI (user-scope).

    Encryption otomatis (tanpa password user). Bila project dipindahkan ke
    Windows user/machine lain, dekripsi GAGAL -> user diminta memasukkan token
    kembali (perilaku yang diinginkan).
    """

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    #: Entropy opsional (konstanta aplikasi) — bukan secret.
    _ENTROPY = b"AETHER_GITHUB_BACKUP_V1"

    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            self._crypt32 = None
            self._kernel32 = None
            return
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    @property
    def available(self) -> bool:
        return self._crypt32 is not None

    # -- helpers ---------------------------------------------------------- #
    def _make_blob(self, data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        blob = _DataBlob(
            len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
        )
        return blob, buf  # buf dijaga hidup selama pemanggilan API

    def _entropy_blob(self):
        return self._make_blob(self._ENTROPY)

    # -- API -------------------------------------------------------------- #
    def protect(self, secret: str) -> bytes:
        """Enkripsi secret -> bytes terproteksi DPAPI."""
        if self._crypt32 is None:
            raise CredentialProtectionError(
                "Enkripsi credential hanya didukung pada Windows (DPAPI)."
            )
        if not isinstance(secret, str) or not secret:
            raise CredentialProtectionError("Token tidak boleh kosong.")

        data_blob, _keep = self._make_blob(secret.encode("utf-8"))
        entropy_blob, _entropy_keep = self._entropy_blob()
        out_blob = _DataBlob()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(data_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            self.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise CredentialProtectionError(
                "Gagal mengenkripsi credential (DPAPI CryptProtectData)."
            )
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._kernel32.LocalFree(out_blob.pbData)

    def unprotect(self, blob: bytes) -> str:
        """Dekripsi bytes DPAPI -> secret (str).

        Raises:
            CredentialProtectionError: bila blob tidak dapat didekripsi
                (mis. dipindahkan ke Windows user/machine lain).
        """
        if self._crypt32 is None:
            raise CredentialProtectionError(
                "Dekripsi credential hanya didukung pada Windows (DPAPI)."
            )
        if not blob:
            raise CredentialProtectionError("Credential kosong.")

        data_blob, _keep = self._make_blob(bytes(blob))
        entropy_blob, _entropy_keep = self._entropy_blob()
        out_blob = _DataBlob()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(data_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            self.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise CredentialProtectionError(
                "Credential tersimpan tidak dapat didekripsi di mesin/user ini. "
                "Masukkan GitHub token kembali."
            )
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._kernel32.LocalFree(out_blob.pbData)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensif
            raise CredentialProtectionError(
                "Credential tersimpan rusak (bukan UTF-8)."
            ) from exc


# --------------------------------------------------------------------------- #
# Git write operations (reuse SubprocessGitClient untuk operasi READ).
# --------------------------------------------------------------------------- #
#: Git credential helper: membaca token dari ENV (bukan dari argv/URL), agar
#: token tidak muncul di daftar argumen proses maupun di URL remote.
_CREDENTIAL_HELPER = (
    "!f() { echo \"username=$AETHER_GIT_USERNAME\"; "
    "echo \"password=$AETHER_GIT_PASSWORD\"; }; f"
)


class GitBackupClient:
    """Operasi Git untuk backup (add/commit/push/restore).

    Operasi READ memakai `GitRepositoryFacade` / `SubprocessGitClient` AETHER
    yang SUDAH ada (bukan Git client kedua untuk read).
    """

    def __init__(
        self,
        facade: Optional[GitRepositoryFacade] = None,
        git_executable: str = "git",
        timeout: float = 60.0,
    ) -> None:
        self.git_executable = git_executable
        self.timeout = timeout
        self._facade = facade
        self._facade_root: Optional[Path] = None

    # -- read facade (reuse) --------------------------------------------- #
    def facade(self, root: Path) -> GitRepositoryFacade:
        """Facade read-only AETHER untuk root project (di-cache per root)."""
        if self._facade is not None:
            return self._facade
        if self._facade_root is None or self._facade_root != Path(root):
            self._facade = GitRepositoryFacade(root=Path(root))
            self._facade_root = Path(root)
        return self._facade

    # -- env / redaction -------------------------------------------------- #
    @staticmethod
    def _redact(text: str, token: Optional[str]) -> str:
        if token and token in text:
            text = text.replace(token, "***")
        return text

    def _env(self, token: Optional[str]) -> Dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if token:
            env["AETHER_GIT_USERNAME"] = "x-access-token"
            env["AETHER_GIT_PASSWORD"] = token
        return env

    def _credential_args(self, token: Optional[str]) -> List[str]:
        # `credential.helper=` (kosong) membersihkan helper existing agar
        # tidak ada credential store pihak ketiga yang ikut campur.
        args = ["-c", "credential.helper="]
        if token:
            args += ["-c", f"credential.helper={_CREDENTIAL_HELPER}"]
        return args

    def _run(
        self,
        args: List[str],
        cwd: Path,
        token: Optional[str] = None,
    ) -> str:
        """Jalankan git (write path). Token TIDAK pernah masuk argv."""
        cmd = [self.git_executable, *self._credential_args(token), *args]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
                env=self._env(token),
            )
        except FileNotFoundError as exc:
            raise GithubBackupError(
                f"Executable git tidak ditemukan: {self.git_executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GithubBackupError(
                f"Perintah git timeout: {' '.join(args)}"
            ) from exc
        except OSError as exc:
            raise GithubBackupError(f"Gagal menjalankan git: {exc}") from exc

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise GithubBackupError(
                self._redact(
                    f"git {' '.join(args)} gagal (exit {completed.returncode}): {message}",
                    token,
                )
            )
        return completed.stdout

    # -- identity --------------------------------------------------------- #
    def _config_value(self, cwd: Path, key: str) -> str:
        try:
            out = self._run(["config", "--get", key], cwd)
        except GithubBackupError:
            return ""
        return out.strip()

    def _identity_args(self, cwd: Path) -> List[str]:
        args: List[str] = []
        if not self._config_value(cwd, "user.name"):
            args += ["-c", "user.name=AETHER Backup"]
        if not self._config_value(cwd, "user.email"):
            args += ["-c", "user.email=aether@localhost"]
        return args

    # -- operations ------------------------------------------------------- #
    def add(self, root: Path, paths: List[str], token: Optional[str] = None) -> None:
        """Stage perubahan untuk path tertentu (deletions diikutkan via -A)."""
        if not paths:
            return
        self._run(["add", "-A", "--", *paths], root, token)

    def commit(self, root: Path, message: str, token: Optional[str] = None) -> str:
        """Commit staged changes. Returns hash HEAD setelah commit."""
        args = [*self._identity_args(root), "commit", "-m", message]
        self._run(args, root, token)
        return self._run(["rev-parse", "HEAD"], root, token).strip()

    def push(
        self,
        root: Path,
        repository: str,
        branch: str,
        token: Optional[str] = None,
    ) -> None:
        """Push HEAD ke remote branch (refspec eksplisit)."""
        target = repository or "origin"
        refspec = f"HEAD:refs/heads/{branch}"
        self._run(["push", target, refspec], root, token)

    def restore(self, root: Path, ref: str, token: Optional[str] = None) -> None:
        """Restore tracked files ke sebuah commit (history TIDAK ditulis ulang)."""
        self._run(["checkout", ref, "--", "."], root, token)

    def ls_remote(
        self,
        repository: str,
        branch: Optional[str] = None,
        token: Optional[str] = None,
        cwd: Optional[Path] = None,
    ) -> List[str]:
        """`git ls-remote --heads` (TIDAK commit/push)."""
        args = ["ls-remote", "--heads", repository]
        if branch:
            args.append(f"refs/heads/{branch}")
        out = self._run(args, cwd or Path.cwd(), token)
        return [line for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
def _normalize_rel(path: str) -> str:
    """Normalisasi path relatif ke bentuk POSIX tanpa prefix './'."""
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _matches_exclude(path: str, patterns: List[str]) -> bool:
    """Cocokkan path (gitignore-like sederhana) terhadap daftar exclude."""
    if not patterns:
        return False
    p = _normalize_rel(path)
    parts = [seg for seg in p.split("/") if seg]
    if not parts:
        return False
    for raw in patterns:
        pat = str(raw).strip()
        if not pat or pat.startswith("#"):
            continue
        if pat.endswith("/"):
            base = pat.rstrip("/")
            for seg in parts[:-1]:
                if fnmatch.fnmatch(seg, base):
                    return True
            if parts and fnmatch.fnmatch(parts[0], base):
                return True
            if p == base or p.startswith(base + "/"):
                return True
        else:
            if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(parts[-1], pat):
                return True
            if p == pat or p.startswith(pat + "/"):
                return True
    return False


def _is_aether_metadata(path: str) -> bool:
    """True bila path berada di dalam `.aether/` (metadata private AETHER)."""
    p = _normalize_rel(path)
    return p == AETHER_DIR_NAME or p.startswith(AETHER_DIR_NAME + "/")


def _github_owner_repo(url: str) -> Optional[Tuple[str, str]]:
    """Ambil (owner, repo) dari URL GitHub (None bila bukan GitHub)."""
    text = (url or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if "github.com" not in text:
        return None
    if text.startswith("git@github.com:"):
        remainder = text.split("git@github.com:", 1)[1]
    elif "github.com/" in text:
        remainder = text.split("github.com/", 1)[1]
    else:
        return None
    parts = [seg for seg in remainder.split("/") if seg]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


class GithubBackupService:
    """Orkestrasi GitHub Backup per project (config + checkpoint + recovery)."""

    def __init__(
        self,
        protector: Optional[WindowsCredentialProtector] = None,
        git: Optional[GitBackupClient] = None,
        checkpoint_limit: int = 30,
    ) -> None:
        self.protector = protector or WindowsCredentialProtector()
        self.git = git or GitBackupClient()
        self.checkpoint_limit = checkpoint_limit

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _store(root: Path) -> GithubBackupStore:
        return GithubBackupStore(root)

    def _read_facade(self, root: Path) -> GitRepositoryFacade:
        return self.git.facade(root)

    def _load_token(
        self, store: GithubBackupStore, override: Optional[str] = None
    ) -> Optional[str]:
        """Token efektif: override request -> credential tersimpan (decrypt)."""
        if override:
            return override
        blob = store.load_credential()
        if not blob:
            return None
        return self.protector.unprotect(blob)

    # -- config ----------------------------------------------------------- #
    def get_config(self, root: Path) -> Dict[str, Any]:
        """Status konfigurasi GitHub Backup + ringkasan changes (TANPA token)."""
        root = Path(root)
        store = self._store(root)
        facade = self._read_facade(root)
        is_repo = facade.is_repository()
        status = store.status()
        data: Dict[str, Any] = {
            **status,
            "is_repository": is_repo,
            "current_branch": facade.current_branch() if is_repo else None,
            "gitignore_ok": aether_is_ignored(root),
            "changes": {"modified": 0, "added": 0, "deleted": 0, "total": 0},
        }
        if is_repo:
            data["changes"] = self._changes_summary(root, store.load_config())
        return data

    def _changes_summary(
        self, root: Path, config: Optional[GithubBackupConfig]
    ) -> Dict[str, int]:
        """Ringkasan perubahan yang AKAN di-backup (mengikuti exclude)."""
        exclude = list(config.exclude) if config else []
        facade = self._read_facade(root)
        summary = {"modified": 0, "added": 0, "deleted": 0, "total": 0}
        try:
            status = facade.status()
        except Exception:  # noqa: BLE001 - status gagal bukan crash service
            return summary
        for f in status.files:
            if _is_aether_metadata(f.path):
                continue
            if _matches_exclude(f.path, exclude):
                continue
            code = (f.status or "").upper()
            if f.untracked or "A" in code:
                summary["added"] += 1
            elif "D" in code:
                summary["deleted"] += 1
            else:
                summary["modified"] += 1
            summary["total"] += 1
        return summary

    def save_config(
        self,
        root: Path,
        *,
        repository: Optional[str] = None,
        branch: Optional[str] = None,
        exclude: Optional[Union[List[str], str]] = None,
        token: Optional[str] = None,
        clear_token: bool = False,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Simpan konfigurasi project (token DIENKRIPSI, tidak plaintext)."""
        root = Path(root)
        store = self._store(root)

        existing = store.load_config() or GithubBackupConfig()
        repository_value = (repository if repository is not None else existing.repository) or ""
        branch_value = (branch if branch is not None else existing.branch) or ""
        repository_value = repository_value.strip()
        branch_value = branch_value.strip()
        if not repository_value:
            raise GithubBackupError("Field 'repository' wajib diisi.")
        if not branch_value:
            raise GithubBackupError("Field 'branch' wajib diisi.")

        config = GithubBackupConfig(
            enabled=bool(enabled) if enabled is not None else bool(existing.enabled),
            repository=repository_value,
            branch=branch_value,
            exclude=(
                existing.exclude if exclude is None else _normalize_exclude(exclude)
            ),
        )

        # Token: enkripsi otomatis (DPAPI). Tidak pernah masuk config.json.
        if clear_token:
            store.delete_credential()
        if token:
            store.save_credential(self.protector.protect(token))

        store.save_config(config)
        # Pastikan `.aether/` selalu di-ignore (rule mandatory, tidak bisa
        # dihapus lewat exclude).
        ensure_aether_ignored(root)
        return self.get_config(root)

    # -- test connection -------------------------------------------------- #
    def test_connection(
        self,
        root: Path,
        *,
        repository: Optional[str] = None,
        branch: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Uji token + akses repository + branch. TIDAK commit/push."""
        root = Path(root)
        store = self._store(root)
        config = store.load_config()
        repo_value = (repository if repository is not None else (config.repository if config else "")) or ""
        branch_value = (branch if branch is not None else (config.branch if config else "")) or ""
        repo_value = repo_value.strip()
        branch_value = branch_value.strip()
        if not repo_value:
            raise GithubBackupError("Field 'repository' wajib diisi.")

        try:
            effective_token = self._load_token(store, token)
        except CredentialProtectionError as exc:
            return {"ok": False, "message": str(exc), "repository": repo_value, "branch": branch_value}

        # 1) Verifikasi akses Git (repository + branch) — jalur yang dipakai push.
        try:
            refs = self.git.ls_remote(repo_value, branch_value or None, effective_token, root)
        except GithubBackupError as exc:
            return {
                "ok": False,
                "message": str(exc),
                "repository": repo_value,
                "branch": branch_value,
            }
        if branch_value and not refs:
            return {
                "ok": False,
                "message": f"Branch '{branch_value}' tidak ditemukan pada repository.",
                "repository": repo_value,
                "branch": branch_value,
            }

        # 2) Verifikasi token GitHub (best-effort; hanya untuk github.com).
        token_state = self._check_github_token(repo_value, effective_token)
        if token_state is False:
            return {
                "ok": False,
                "message": "Token GitHub tidak valid atau tidak memiliki akses ke repository.",
                "repository": repo_value,
                "branch": branch_value,
                "token_checked": True,
            }

        return {
            "ok": True,
            "message": "Connected successfully",
            "repository": repo_value,
            "branch": branch_value,
            "refs": len(refs),
            "token_checked": token_state is True,
        }

    def _check_github_token(self, repository: str, token: Optional[str]) -> Optional[bool]:
        """True=valid, False=invalid, None=tidak dapat diperiksa (lewati)."""
        if not token:
            return None
        owner_repo = _github_owner_repo(repository)
        if owner_repo is None:
            return None
        owner, repo = owner_repo
        url = f"https://api.github.com/repos/{owner}/{repo}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "AETHER",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= int(getattr(response, "status", 200)) < 300
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False
            if exc.code in (403, 404):
                # 403: rate-limit/scope; 404: repo private/tidak ada. Jangan
                # menyimpulkan token invalid dari kode ini.
                return None
            return None
        except Exception:  # noqa: BLE001 - jaringan tidak tersedia -> lewati
            return None

    # -- checkpoints ------------------------------------------------------ #
    def list_checkpoints(self, root: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Daftar checkpoint (langsung dari Git history, bukan DB kedua)."""
        root = Path(root)
        facade = self._read_facade(root)
        if not facade.is_repository():
            return []
        commits: List[GitCommit] = facade.log(limit=limit or self.checkpoint_limit)
        return [c.to_dict() for c in commits]

    def create_checkpoint(
        self,
        root: Path,
        description: Optional[str],
        *,
        exclude: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Buat checkpoint: ensure ignore -> add -> commit -> push."""
        root = Path(root)
        store = self._store(root)
        config = store.load_config()
        if config is None or not config.repository or not config.branch:
            raise GithubBackupError(
                "GitHub Backup belum dikonfigurasi untuk project ini."
            )

        message = (description or "").strip()
        if not message:
            raise GithubBackupError("Field 'description' (commit message) wajib diisi.")

        facade = self._read_facade(root)
        if not facade.is_repository():
            raise GithubBackupError("Project ini bukan repository Git.")

        # 1) Pastikan `.aether/` di-ignore (mandatory).
        ensure_aether_ignored(root)

        # 2) Tentukan path yang akan di-commit (selalu kecualikan `.aether/`).
        exclude_patterns = list(config.exclude if exclude is None else exclude)
        try:
            status = facade.status()
        except Exception as exc:  # noqa: BLE001
            raise GithubBackupError(f"Gagal membaca status Git: {exc}") from exc

        staged: List[str] = []
        for f in status.files:
            if _is_aether_metadata(f.path):
                continue
            if _matches_exclude(f.path, exclude_patterns):
                continue
            staged.append(f.path)

        if not staged:
            return {
                "committed": False,
                "pushed": False,
                "message": "Tidak ada perubahan untuk di-backup.",
                "checkpoint": None,
            }

        token = self._load_token(store)

        # 3) add -> commit -> push (token via credential helper/env).
        self.git.add(root, staged, token)
        commit_hash = self.git.commit(root, message, token)

        last = facade.log(limit=1)
        checkpoint = last[0].to_dict() if last else {"hash": commit_hash, "short_hash": commit_hash[:7]}
        checkpoint["files_changed"] = len(staged)

        pushed = False
        push_error: Optional[str] = None
        try:
            self.git.push(root, config.repository, config.branch, token)
            pushed = True
        except GithubBackupError as exc:
            push_error = str(exc)

        result: Dict[str, Any] = {
            "committed": True,
            "pushed": pushed,
            "message": (
                "Checkpoint dibuat dan di-upload."
                if pushed
                else "Checkpoint dibuat lokal, tetapi push gagal."
            ),
            "checkpoint": checkpoint,
            "files_changed": len(staged),
        }
        if push_error:
            result["push_error"] = push_error
        return result

    # -- recovery --------------------------------------------------------- #
    def restore_checkpoint(
        self,
        root: Path,
        commit: Optional[str],
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Restore working tree ke sebuah checkpoint (history tidak ditulis ulang)."""
        root = Path(root)
        ref = (commit or "").strip()
        if not ref:
            raise GithubBackupError("Field 'commit' wajib diisi.")

        facade = self._read_facade(root)
        if not facade.is_repository():
            raise GithubBackupError("Project ini bukan repository Git.")

        # Safety: jangan buang perubahan yang belum aman secara diam-diam.
        status = facade.status()
        if not status.clean and not force:
            raise GithubBackupError(
                "Working tree memiliki perubahan yang belum diamankan. "
                "Konfirmasi restore (force) untuk melanjutkan."
            )

        store = self._store(root)
        try:
            token = self._load_token(store)
        except CredentialProtectionError:
            token = None

        self.git.restore(root, ref, token)

        return {
            "restored": True,
            "commit": ref,
            "message": f"Project dikembalikan ke checkpoint {ref[:7]}.",
            "forced": bool(force),
        }
