"""Verification script for paginated backup commit history (read-only)."""
import subprocess, sys, tempfile, os, json
from pathlib import Path

# Use the project's virtualenv site-packages
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "web" / "django_app"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.conf import settings
if not settings.configured:
    settings.configure(DEBUG=True, DATABASES={}, INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth"], USE_TZ=True)

import django
django.setup()

from api.github_backup import GitBackupClient, GithubBackupService

def run_git(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout

# Build a temp git repo with commits
tmp = tempfile.mkdtemp()
run_git(["git", "init"], tmp)
run_git(["git", "config", "user.email", "t@t.com"], tmp)
run_git(["git", "config", "user.name", "Test"], tmp)

root = Path(tmp)
# Create 25 commits
for i in range(25):
    f = root / f"file_{i}.txt"
    f.write_text(f"content {i}")
    run_git(["git", "add", "."], tmp)
    run_git(["git", "commit", "-m", f"commit {i}"], tmp)

cli = GitBackupClient()
svc = GithubBackupService(git=cli)

# Page 1
page1 = svc.get_commits(root, page=1, per_page=20)
assert len(page1["commits"]) == 20, f"Expected 20, got {len(page1['commits'])}"
assert page1["total"] == 25
assert page1["page"] == 1
assert page1["per_page"] == 20
assert page1["has_next"] == True
assert page1["has_prev"] == False
print("Page 1 OK:", json.dumps({k: page1[k] for k in ["total", "page", "has_next", "has_prev"]}))

# Page 2
page2 = svc.get_commits(root, page=2, per_page=20)
assert len(page2["commits"]) == 5, f"Expected 5, got {len(page2['commits'])}"
assert page2["total"] == 25
assert page2["page"] == 2
assert page2["has_next"] == False
assert page2["has_prev"] == True
print("Page 2 OK:", json.dumps({k: page2[k] for k in ["total", "page", "has_next", "has_prev"]}))

# Commit object structure
c = page1["commits"][0]
assert "sha" in c and "message" in c and "author" in c and "date" in c and "branch" in c
print("Commit object keys OK:", list(c.keys()))

print("\nALL BACKEND TESTS PASSED")

# Edge case: empty repo (no commits)
tmp2 = tempfile.mkdtemp()
run_git(["git", "init"], tmp2)
run_git(["git", "config", "user.email", "t@t.com"], tmp2)
run_git(["git", "config", "user.name", "Test"], tmp2)
r2 = Path(tmp2)
empty_result = svc.get_commits(r2, page=1, per_page=20)
assert empty_result["commits"] == []
assert empty_result["total"] == 0
assert empty_result["has_next"] == False
assert empty_result["has_prev"] == False
assert empty_result["page"] == 1
print("Empty repo OK:", json.dumps({k: empty_result[k] for k in ["total", "page", "has_next", "has_prev"]}))

# Edge case: non-repo path
tmp3 = tempfile.mkdtemp()
r3 = Path(tmp3)
nonrepo_result = svc.get_commits(r3, page=1, per_page=20)
assert nonrepo_result["commits"] == []
assert nonrepo_result["total"] == 0
print("Non-repo path OK: empty result returned")

print("\nALL EDGE CASE TESTS PASSED")
