from __future__ import annotations

import subprocess

from personal_assistant.backup import GitBackupService
from personal_assistant.config import Settings


def test_unchanged_local_state_preserves_last_verified_backup(settings):
    backup = GitBackupService(settings)
    backup._record_status(status="success", detail="Pushed", commit="verified-commit")
    verified = backup.read_status()
    backup._record_status(status="no_changes", detail="No local changes")

    current = backup.read_status()
    assert current["last_success_commit"] == "verified-commit"
    assert current["last_success_at"] == verified["last_success_at"]






def _init_repo(repo):
    init = subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=False, capture_output=True)
    if init.returncode != 0:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, capture_output=True)












def test_dedicated_state_repo_backup_pushes_state_only(tmp_path):
    repo = tmp_path / "repo"
    remote = tmp_path / "state-remote.git"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    state_dir = repo / "state"
    (state_dir / "notes").mkdir(parents=True)
    (state_dir / "notes" / "inbox.md").write_text("# Inbox\n\nhello\n", encoding="utf-8")
    (repo / "code.txt").write_text("app code\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "remote", "add", "state-origin", str(remote)], cwd=repo, check=True, capture_output=True)

    (state_dir / "notes" / "inbox.md").write_text("# Inbox\n\nupdated dedicated\n", encoding="utf-8")

    settings = Settings(
        repo_root=repo,
        state_dir=repo / "state",
        runtime_dir=repo / "runtime",
        git_backup_enabled=True,
        git_backup_mode="dedicated_state_repo",
        git_backup_remote="state-origin",
        git_backup_branch="main",
        git_commit_name="Assistant",
        git_commit_email="assistant@example.com",
    )
    backup = GitBackupService(settings)

    assert backup.backup_now() is True

    clone = tmp_path / "state-clone"
    subprocess.run(["git", "clone", "-b", "main", str(remote), str(clone)], check=True, capture_output=True)
    assert (clone / "state" / "notes" / "inbox.md").read_text(encoding="utf-8") == "# Inbox\n\nupdated dedicated\n"
    assert not (clone / "code.txt").exists()
    assert backup.backup_runtime_path.exists()


def test_dedicated_state_repo_backup_uses_snapshot_hash_for_repeat_runs(tmp_path):
    repo = tmp_path / "repo"
    remote = tmp_path / "state-remote.git"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    state_dir = repo / "state"
    (state_dir / "notes").mkdir(parents=True)
    (state_dir / "notes" / "inbox.md").write_text("# Inbox\n\nhello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "remote", "add", "state-origin", str(remote)], cwd=repo, check=True, capture_output=True)

    settings = Settings(
        repo_root=repo,
        state_dir=repo / "state",
        runtime_dir=repo / "runtime",
        git_backup_enabled=True,
        git_backup_mode="dedicated_state_repo",
        git_backup_remote="state-origin",
        git_backup_branch="main",
        git_commit_name="Assistant",
        git_commit_email="assistant@example.com",
    )
    backup = GitBackupService(settings)

    assert backup.backup_now() is True
    assert backup.backup_now() is False

    (state_dir / "notes" / "inbox.md").write_text("# Inbox\n\nchanged again\n", encoding="utf-8")
    assert backup.backup_now() is True
