from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from personal_assistant.config import Settings


class GitBackupService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backup_runtime_path = self.settings.runtime_dir / "backup" / "state_repo.json"
        self.status_runtime_path = self.settings.runtime_dir / "backup" / "status.json"

    def git_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        ssh_command = self.settings_git_ssh_command()
        if ssh_command:
            env["GIT_SSH_COMMAND"] = ssh_command
        return env

    def settings_git_ssh_command(self) -> str:
        key_path = self.settings.git_ssh_key_path
        if key_path is None:
            return ""

        parts = [
            "ssh",
            "-i",
            shlex.quote(str(key_path)),
            "-o",
            "IdentitiesOnly=yes",
        ]
        known_hosts_path = self.settings.git_ssh_known_hosts_path
        if known_hosts_path is not None:
            parts.extend(
                [
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={shlex.quote(str(known_hosts_path))}",
                ]
            )
        return " ".join(parts)

    def _run_git(
        self,
        *args: str,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.settings.repo_root,
            text=True,
            capture_output=True,
            check=check,
            env=self.git_environment(),
        )

    def durable_changes_pending(self) -> bool:
        if not self.settings.state_dir.exists():
            return False
        return self._state_snapshot_hash() != self._last_backed_up_state_hash()



    def _commit_all(self, *, repo_root: Path, timestamp: str) -> bool:
        self._run_git("add", "-A", cwd=repo_root)
        staged = self._run_git("diff", "--cached", "--name-only", cwd=repo_root)
        if not staged.stdout.strip():
            return False
        self._run_git(
            "-c",
            f"user.name={self.settings.git_commit_name}",
            "-c",
            f"user.email={self.settings.git_commit_email}",
            "commit",
            "-m",
            f"assistant state backup {timestamp}",
            cwd=repo_root,
        )
        return True



    def _state_snapshot_hash(self) -> str:
        digest = hashlib.sha256()
        state_root = self.settings.state_dir
        if not state_root.exists():
            digest.update(b"missing-state")
            return digest.hexdigest()
        for path in sorted(p for p in state_root.rglob("*") if p.is_file()):
            rel = path.relative_to(state_root).as_posix().encode("utf-8")
            digest.update(rel)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _last_backed_up_state_hash(self) -> str:
        if not self.backup_runtime_path.exists():
            return ""
        try:
            payload = json.loads(self.backup_runtime_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return ""
        return str(payload.get("state_hash") or "")

    def _record_backed_up_state(self, *, state_hash: str, commit: str = "") -> None:
        self.backup_runtime_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state_hash": state_hash,
            "commit": commit,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temp_path = self.backup_runtime_path.with_name(f"{self.backup_runtime_path.name}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_path, self.backup_runtime_path)

    def _record_status(self, *, status: str, detail: str, commit: str = "") -> None:
        self.status_runtime_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read_status()
        payload = {
            "status": status,
            "detail": detail,
            "commit": commit,
            "mode": self.settings.git_backup_mode,
            "remote": self.settings.git_backup_remote,
            "branch": self.settings.git_backup_branch,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_success_at": existing.get("last_success_at", ""),
            "last_success_commit": existing.get("last_success_commit", ""),
        }
        if status == "success" or (status == "no_changes" and commit):
            payload["last_success_at"] = payload["updated_at"]
            payload["last_success_commit"] = commit
        temp_path = self.status_runtime_path.with_name(f"{self.status_runtime_path.name}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_path, self.status_runtime_path)

    def read_status(self) -> dict[str, str]:
        if not self.status_runtime_path.exists():
            return {}
        try:
            payload = json.loads(self.status_runtime_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def _prepare_dedicated_state_repo(self, repo_root: Path, remote_url: str) -> None:
        self._run_git("init", cwd=repo_root)
        self._run_git("remote", "add", self.settings.git_backup_remote, remote_url, cwd=repo_root)
        fetch = self._run_git(
            "fetch",
            self.settings.git_backup_remote,
            self.settings.git_backup_branch,
            check=False,
            cwd=repo_root,
        )
        if fetch.returncode == 0:
            self._run_git("checkout", "-B", self.settings.git_backup_branch, "FETCH_HEAD", cwd=repo_root)
        else:
            self._run_git("checkout", "--orphan", self.settings.git_backup_branch, cwd=repo_root)
        for path in repo_root.iterdir():
            if path.name == ".git":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        target_state = repo_root / "state"
        if target_state.exists():
            shutil.rmtree(target_state)
        if self.settings.state_dir.exists():
            shutil.copytree(self.settings.state_dir, target_state)



    def backup_now(self, *, force: bool = False) -> bool:
        try:
            if not self.settings.git_backup_enabled:
                self._record_status(status="disabled", detail="Git backup is disabled.")
                return False
            return self._backup_to_dedicated_state_repo(force=force)
        except Exception as exc:
            self._record_status(status="failed", detail=type(exc).__name__)
            raise

    def _backup_to_dedicated_state_repo(self, *, force: bool = False) -> bool:
        state_hash = self._state_snapshot_hash()
        if not force and state_hash == self._last_backed_up_state_hash():
            self._record_status(status="no_changes", detail="No state changes to back up.")
            return False

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        remote_url = self._run_git("remote", "get-url", self.settings.git_backup_remote).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="assistant-state-backup-") as temp_dir_text:
            temp_root = Path(temp_dir_text) / "state-repo"
            temp_root.mkdir(parents=True, exist_ok=True)
            self._prepare_dedicated_state_repo(temp_root, remote_url)
            committed = self._commit_all(repo_root=temp_root, timestamp=timestamp)
            if not committed:
                commit = self._run_git("rev-parse", "HEAD", cwd=temp_root).stdout.strip()
                self._record_backed_up_state(state_hash=state_hash, commit=commit)
                self._record_status(status="no_changes", detail="No state changes were staged for commit.", commit=commit)
                return False
            push = self._run_git(
                "push",
                self.settings.git_backup_remote,
                f"HEAD:{self.settings.git_backup_branch}",
                cwd=temp_root,
                check=False,
            )
            if push.returncode != 0:
                raise subprocess.CalledProcessError(
                    push.returncode,
                    push.args,
                    output=push.stdout,
                    stderr=push.stderr,
                )
            commit = self._run_git("rev-parse", "HEAD", cwd=temp_root).stdout.strip()
            self._record_backed_up_state(state_hash=state_hash, commit=commit)
            self._record_status(status="success", detail="Dedicated state backup pushed successfully.", commit=commit)
            return True
