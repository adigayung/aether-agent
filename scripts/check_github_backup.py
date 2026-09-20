"""Verifikasi GitHub Backup / Checkpoint / Recovery (per project).

Deterministik, OFFLINE (tanpa jaringan): repository remote disimulasikan dengan
bare repo lokal via URL `file://`. Fixture di `dummy_test/github_backup_fixture`
dan dibersihkan setelah test.

Menguji:
    1.  Project tanpa konfigurasi GitHub tetap usable (status not configured).
    2.  Configure GitHub -> config.json (non-secret) + credential.enc (encrypted).
    3.  Token TIDAK tersimpan plaintext (config/credential/semua file .aether).
    4.  Credential DPAPI dapat didekripsi lagi (roundtrip) di mesin/user ini.
    5.  `.aether/` selalu masuk `.gitignore` (dibuat/ditambah, tidak overwrite).
    6.  Rule mandatory `.aether/` tidak bisa dihapus lewat exclude.
    7.  Exclude optional bekerja (file ter-exclude tidak ikut backup).
    8.  Create checkpoint -> commit + push (git push ke remote bare).
    9.  `.aether/` TIDAK ikut commit (lokal maupun remote).
    10. Commit history tampil (list checkpoints).
    11. Test Connection sukses + gagal tanpa crash.
    12. Recovery (restore) dari checkpoint bekerja; dirty tree butuh force.
    13. Project lain punya konfigurasi GitHub TERPISAH (tidak tercampur).
    14. Restart (instance service baru) tidak merusak konfigurasi.
    15. Token TIDAK muncul di payload status/checkpoint/error.
    16. Endpoint HTTP (GET/POST) terpasang benar.

Jalankan:
    python scripts/check_github_backup.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE_A = DUMMY_ROOT / "github_backup_fixture_a"
FIXTURE_B = DUMMY_ROOT / "github_backup_fixture_b"
REMOTE_A = DUMMY_ROOT / "github_backup_remote_a.git"
REMOTE_B = DUMMY_ROOT / "github_backup_remote_b.git"

TOKEN_A = "ghp_TESTSECRET_TOKENAAAA_0123456789"
TOKEN_B = "ghp_TESTSECRET_TOKENBBBB_9876543210"


def _git(args, cwd=None, git_dir: Path | None = None):
    cmd = ["git"]
    if git_dir is not None:
        cmd += [f"--git-dir={git_dir}"]
    cmd += list(args)
    completed = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} gagal: {completed.stderr.strip()}"
        )
    return completed.stdout


def _rmtree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.rglob("*"):
        try:
            child.chmod(0o777)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def setup_fixture() -> None:
    for path in (FIXTURE_A, FIXTURE_B, REMOTE_A, REMOTE_B):
        _rmtree(path)
    FIXTURE_A.mkdir(parents=True, exist_ok=True)
    FIXTURE_B.mkdir(parents=True, exist_ok=True)
    REMOTE_A.mkdir(parents=True, exist_ok=True)
    REMOTE_B.mkdir(parents=True, exist_ok=True)

    for remote in (REMOTE_A, REMOTE_B):
        _git(["init", "--bare", "-b", "main"], cwd=remote)

    for fixture in (FIXTURE_A, FIXTURE_B):
        _git(["init", "-b", "main"], cwd=fixture)
        _git(["config", "user.email", "test@example.com"], cwd=fixture)
        _git(["config", "user.name", "Test User"], cwd=fixture)
        (fixture / "app.py").write_text("print('v1')\n", encoding="utf-8")
        _git(["add", "app.py"], cwd=fixture)
        _git(["commit", "-m", "initial commit"], cwd=fixture)


def teardown_fixture() -> None:
    for path in (FIXTURE_A, FIXTURE_B, REMOTE_A, REMOTE_B):
        _rmtree(path)


def _all_text_in_aether(root: Path) -> str:
    """Gabungkan semua teks file di `.aether/` (untuk cek kebocoran token)."""
    chunks = []
    aether = root / ".aether"
    if not aether.exists():
        return ""
    for path in aether.rglob("*"):
        if not path.is_file():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Paksa (bukan setdefault): .env bisa memuat nilai tanpa 'testserver'.
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()

    from django.test import Client

    import api.services as services_mod
    from agent_ai.projects.registry import ProjectRegistry
    from api.github_backup import GithubBackupService
    from api.project_store import ProjectStore

    workspace = DUMMY_ROOT / "github_backup_registry"
    _rmtree(workspace)
    registry = ProjectRegistry(workspace=workspace)
    tmp_store_dir = tempfile.mkdtemp(prefix="gh_backup_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    service = services_mod.GatewayService(
        project_registry=registry, project_store=store, auto_execute=False
    )
    services_mod._default_service = service
    client = Client()

    proj_a = service.create_project(name="BackupA", path=str(FIXTURE_A))
    pid_a = proj_a["id"]
    proj_b = service.create_project(name="BackupB", path=str(FIXTURE_B))
    pid_b = proj_b["id"]

    remote_a_url = REMOTE_A.as_uri()
    remote_b_url = REMOTE_B.as_uri()

    backup = GithubBackupService()

    # 1) Project tanpa konfigurasi GitHub -> not configured, TIDAK error.
    status = service.github_backup_status(pid_a)
    assert status["configured"] is False, status
    assert status["credential_set"] is False, status
    assert status["is_repository"] is True, status
    print("[1] project tanpa config -> not configured & tetap usable OK")

    # Checkpoint pada project belum dikonfigurasi -> error jelas (bukan crash).
    try:
        service.create_github_checkpoint(pid_a, "nope")
        raise AssertionError("checkpoint tanpa config seharusnya gagal")
    except services_mod.ValidationError as exc:
        assert "belum dikonfigurasi" in str(exc).lower(), str(exc)
    print("[1b] checkpoint tanpa config -> error jelas OK")

    # 2) Configure GitHub (token dienkripsi otomatis, tanpa password user).
    status = service.save_github_backup_config(
        pid_a,
        {
            "repository": remote_a_url,
            "branch": "main",
            "exclude": ["data/", "*.db"],
            "token": TOKEN_A,
        },
    )
    assert status["configured"] is True, status
    assert status["credential_set"] is True, status
    assert status["repository"] == remote_a_url, status
    assert status["branch"] == "main", status
    aether_github = FIXTURE_A / ".aether" / "github"
    assert (aether_github / "config.json").is_file()
    assert (aether_github / "credential.enc").is_file()
    print("[2] configure GitHub -> config.json + credential.enc dibuat OK")

    # 3) Token TIDAK plaintext di mana pun di .aether.
    config_raw = (aether_github / "config.json").read_text(encoding="utf-8")
    assert TOKEN_A not in config_raw, "token bocor di config.json"
    config_data = json.loads(config_raw)
    assert "token" not in config_data, config_data
    assert set(config_data) == {"enabled", "repository", "branch", "exclude"}, config_data
    enc_bytes = (aether_github / "credential.enc").read_bytes()
    assert TOKEN_A.encode("utf-8") not in enc_bytes, "token bocor plaintext di credential.enc"
    leaked = _all_text_in_aether(FIXTURE_A)
    assert TOKEN_A not in leaked, "token bocor pada salah satu file .aether"
    print("[3] token tersimpan TERENKRIPSI (tidak plaintext) OK")

    # 4) Credential DPAPI roundtrip.
    assert backup.protector.available is True, "DPAPI harus tersedia di Windows"
    decrypted = backup.protector.unprotect(enc_bytes)
    assert decrypted == TOKEN_A, "hasil dekripsi tidak sama dengan token asli"
    print("[4] credential DPAPI dapat didekripsi lagi (roundtrip) OK")

    # 5) `.aether/` selalu masuk .gitignore.
    gitignore_text = (FIXTURE_A / ".gitignore").read_text(encoding="utf-8")
    assert ".aether/" in gitignore_text, gitignore_text
    print("[5] .gitignore memuat .aether/ OK")

    # 6) Exclude tidak dapat menghapus rule mandatory.
    service.save_github_backup_config(
        pid_a,
        {
            "repository": remote_a_url,
            "branch": "main",
            "exclude": ["data/", "*.db", ".gitignore", ".aether/"],
        },
    )
    gitignore_text = (FIXTURE_A / ".gitignore").read_text(encoding="utf-8")
    assert ".aether/" in gitignore_text, "rule mandatory .aether/ hilang!"
    print("[6] rule mandatory .aether/ tidak bisa dihapus lewat exclude OK")

    # 7) Exclude optional bekerja (file ter-exclude tidak ikut backup).
    (FIXTURE_A / "cache.db").write_text("data\n", encoding="utf-8")
    (FIXTURE_A / "data").mkdir(exist_ok=True)
    (FIXTURE_A / "data" / "dump.bin").write_text("dump\n", encoding="utf-8")
    (FIXTURE_A / "app.py").write_text("print('v2')\n", encoding="utf-8")
    status = service.github_backup_status(pid_a)
    # app.py (modified) dihitung, cache.db + data/ TIDAK dihitung.
    assert status["changes"]["modified"] == 1, status["changes"]
    assert status["changes"]["total"] == 1, status["changes"]
    print("[7] exclude optional bekerja (file ter-exclude tidak dihitung) OK")

    # 8) Create checkpoint -> commit + push.
    result = service.create_github_checkpoint(pid_a, "Checkpoint pertama")
    assert result["committed"] is True, result
    assert result["pushed"] is True, result
    assert result["files_changed"] == 1, result
    checkpoint = result["checkpoint"]
    assert checkpoint["short_hash"], checkpoint
    print("[8] create checkpoint -> commit + push OK")

    # 9) `.aether/` & file ter-exclude TIDAK ikut commit (lokal + remote).
    tracked = _git(["ls-files"], cwd=FIXTURE_A).replace("\\", "/")
    assert ".aether/" not in tracked, tracked
    assert "cache.db" not in tracked, tracked
    assert "data/dump.bin" not in tracked, tracked
    remote_tree = _git(["ls-tree", "-r", "--name-only", "HEAD"], git_dir=REMOTE_A)
    assert ".aether/" not in remote_tree, remote_tree
    assert "app.py" in remote_tree, remote_tree
    print("[9] .aether/ + file ter-exclude tidak ikut commit (lokal & remote) OK")

    # 10) Commit history (checkpoints) tampil dari Git.
    data = service.list_github_checkpoints(pid_a)
    subjects = [c["subject"] for c in data["checkpoints"]]
    assert subjects[0] == "Checkpoint pertama", subjects
    assert "initial commit" in subjects, subjects
    for cp in data["checkpoints"]:
        assert cp["short_hash"] and cp["timestamp"] and cp["hash"], cp
    print("[10] checkpoint list (hash/time/description) dari Git OK")

    # 11) Test Connection sukses + gagal (tanpa crash).
    ok = service.test_github_backup_connection(pid_a, {})
    assert ok["ok"] is True, ok
    bad = service.test_github_backup_connection(
        pid_a, {"repository": (DUMMY_ROOT / "tidak_ada_repo.git").as_uri()}
    )
    assert bad["ok"] is False, bad
    assert TOKEN_A not in json.dumps(bad), bad
    print("[11] Test Connection sukses + gagal (token tidak bocor di error) OK")

    # 12) Recovery + safety dirty tree.
    (FIXTURE_A / "app.py").write_text("print('DIRTY')\n", encoding="utf-8")
    try:
        service.restore_github_checkpoint(pid_a, checkpoint["hash"], force=False)
        raise AssertionError("restore pada dirty tree harus ditolak tanpa force")
    except services_mod.ConflictError:
        pass
    restored = service.restore_github_checkpoint(pid_a, checkpoint["hash"], force=True)
    assert restored["restored"] is True, restored
    assert (FIXTURE_A / "app.py").read_text(encoding="utf-8").strip() == "print('v2')"
    print("[12] recovery dari checkpoint + safety dirty tree OK")

    # 13) Konfigurasi per project TERPISAH.
    service.save_github_backup_config(
        pid_b,
        {"repository": remote_b_url, "branch": "main", "exclude": [], "token": TOKEN_B},
    )
    status_a = service.github_backup_status(pid_a)
    status_b = service.github_backup_status(pid_b)
    assert status_a["repository"] != status_b["repository"], (status_a, status_b)
    token_b_decrypted = backup.protector.unprotect(
        (FIXTURE_B / ".aether" / "github" / "credential.enc").read_bytes()
    )
    assert token_b_decrypted == TOKEN_B, "credential project B salah"
    assert TOKEN_B not in _all_text_in_aether(FIXTURE_A), "credential B bocor ke project A"
    assert TOKEN_A not in _all_text_in_aether(FIXTURE_B), "credential A bocor ke project B"
    print("[13] project A & B punya konfigurasi GitHub terpisah OK")

    # 14) Restart (instance baru) tidak merusak konfigurasi.
    fresh = GithubBackupService()
    fresh_status = fresh.get_config(FIXTURE_A)
    assert fresh_status["configured"] is True, fresh_status
    assert fresh.protector.unprotect(
        (FIXTURE_A / ".aether" / "github" / "credential.enc").read_bytes()
    ) == TOKEN_A
    print("[14] restart (instance baru) membaca konfigurasi + credential OK")

    # 15) Token tidak muncul di payload status/checkpoint.
    assert TOKEN_A not in json.dumps(service.github_backup_status(pid_a))
    assert TOKEN_A not in json.dumps(service.list_github_checkpoints(pid_a))
    print("[15] token tidak muncul di payload status/checkpoint OK")

    # 16) Endpoint HTTP terpasang (GET/POST).
    resp = client.get(f"/api/projects/{pid_a}/github")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert TOKEN_A not in resp.content.decode("utf-8")
    resp = client.post(
        f"/api/projects/{pid_a}/github/test",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json().get("ok") is True, resp.content
    resp = client.get(f"/api/projects/{pid_a}/github/checkpoints")
    assert resp.status_code == 200, resp.content
    assert resp.json()["checkpoints"], resp.content
    # Buat perubahan agar checkpoint HTTP punya isi untuk di-commit.
    (FIXTURE_A / "notes.txt").write_text("catatan\n", encoding="utf-8")
    resp = client.post(
        f"/api/projects/{pid_a}/github/checkpoints",
        data=json.dumps({"description": "via HTTP"}),
        content_type="application/json",
    )
    assert resp.status_code in (200, 201), resp.content
    assert resp.json().get("committed") is True, resp.content
    # Restore via HTTP (force) harus sukses.
    resp = client.post(
        f"/api/projects/{pid_a}/github/restore",
        data=json.dumps({"commit": checkpoint["hash"], "force": True}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json().get("restored") is True, resp.content
    print("[16] endpoint HTTP GitHub Backup (GET/POST/test/restore) OK")

    # Cleanup.
    _rmtree(workspace)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] GitHub Backup / Checkpoint / Recovery bekerja (per project, encrypted, offline).")
    return 0


def main() -> int:
    print("=== Verifikasi GitHub Backup / Checkpoint / Recovery ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
