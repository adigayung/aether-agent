"""Git Awareness Foundation AETHER.

Membuat AETHER memahami repository Git tanpa menjadikan Git sebagai dependency
wajib untuk Agent Core. Read-only: tidak ada operasi mutasi Git.

    from agent_ai.git import GitRepositoryFacade, SubprocessGitClient

    repo = GitRepositoryFacade(root=workspace)
    if repo.is_repository():
        print(repo.status())

Git dan ChangeTracker memiliki responsibility berbeda:
    ChangeTracker -> apa yang berubah selama task
    Git           -> bagaimana repository melihat perubahan tersebut
"""

from agent_ai.git.client import (
    GitClient,
    GitCommandError,
    GitError,
    GitNotAvailableError,
    SubprocessGitClient,
)
from agent_ai.git.models import (
    GitCommit,
    GitDiffSummary,
    GitFileStatus,
    GitRepository,
    GitStatus,
)
from agent_ai.git.repository import GitRepositoryFacade

__all__ = [
    "GitRepository",
    "GitStatus",
    "GitFileStatus",
    "GitCommit",
    "GitDiffSummary",
    "GitClient",
    "SubprocessGitClient",
    "GitRepositoryFacade",
    "GitError",
    "GitNotAvailableError",
    "GitCommandError",
]
