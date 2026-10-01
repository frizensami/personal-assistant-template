from __future__ import annotations

import shutil
import stat
import subprocess
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from personal_assistant.backup import GitBackupService
from personal_assistant.config import Settings


@dataclass(frozen=True)
class PreflightIssue:
    summary: str
    resolution: str

    def render(self) -> str:
        return f"- {self.summary}\n  Fix: {self.resolution}"


def collect_preflight_issues(settings: Settings, *, target: str) -> list[PreflightIssue]:
    target = target.strip().lower()
    if target not in {"api", "worker", "service", "local"}:
        return [
            PreflightIssue(
                f"Unknown preflight target {target!r}.",
                "Use one of: api, worker, service, local.",
            )
        ]

    issues: list[PreflightIssue] = []
    if target == "local":
        issues.extend(_collect_local_telegram_issues(settings))
    else:
        issues.extend(_collect_telegram_issues(settings))
    issues.extend(_collect_backup_issues(settings))
    return issues


def render_preflight_failure(issues: Iterable[PreflightIssue]) -> str:
    rendered = [issue.render() for issue in issues]
    return "Preflight failed. Fix the following before starting the service:\n" + "\n".join(rendered)


def _collect_telegram_issues(settings: Settings) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    required_text_values = {
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token,
        "TELEGRAM_WEBHOOK_SECRET": settings.telegram_webhook_secret,
        "PUBLIC_BASE_URL": settings.public_base_url,
    }
    for name, value in required_text_values.items():
        if value.strip():
            continue
        issues.append(
            PreflightIssue(
                f"{name} is missing.",
                f"Set {name} in .env, then rerun `docker compose up -d`.",
            )
        )

    if settings.telegram_allowed_user_id <= 0:
        issues.append(
            PreflightIssue(
                "TELEGRAM_ALLOWED_USER_ID must be a positive integer.",
                "Set TELEGRAM_ALLOWED_USER_ID to your numeric Telegram user id in .env.",
            )
        )

    if settings.public_base_url.strip():
        parsed = urlparse(settings.public_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            issues.append(
                PreflightIssue(
                    "PUBLIC_BASE_URL must be a public HTTPS URL.",
                    "Set PUBLIC_BASE_URL to something like https://apps.example.com.",
                )
            )
    return issues


def _collect_local_telegram_issues(settings: Settings) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    if not settings.telegram_webhook_secret.strip():
        issues.append(
            PreflightIssue(
                "TELEGRAM_WEBHOOK_SECRET is missing.",
                "Set TELEGRAM_WEBHOOK_SECRET in .env, for example `local-secret` for local smoke tests.",
            )
        )

    if settings.telegram_allowed_user_id <= 0:
        issues.append(
            PreflightIssue(
                "TELEGRAM_ALLOWED_USER_ID must be a positive integer.",
                "Set TELEGRAM_ALLOWED_USER_ID in .env to the user id you want local smoke tests to simulate.",
            )
        )
    return issues


def _collect_backup_issues(settings: Settings) -> list[PreflightIssue]:
    if not settings.git_backup_enabled:
        return []

    issues: list[PreflightIssue] = []
    if shutil.which("git") is None:
        issues.append(
            PreflightIssue(
                "git is not available in the container.",
                "Use the provided Docker image or install git in the runtime image.",
            )
        )
        return issues

    backup = GitBackupService(settings)
    try:
        repo_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=settings.repo_root,
            text=True,
            capture_output=True,
            check=False,
            env=backup.git_environment(),
        )
    except FileNotFoundError:
        issues.append(
            PreflightIssue(
                f"Repo root {settings.repo_root} does not exist inside the container.",
                "Mount the full repo into /workspace and keep REPO_ROOT set to /workspace.",
            )
        )
        return issues
    if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
        issues.append(
            PreflightIssue(
                f"{settings.repo_root} is not a usable git working tree.",
                "Mount the full repo into the container so /workspace includes .git and the checked-out files.",
            )
        )
        return issues

    remote_result = subprocess.run(
        ["git", "remote", "get-url", settings.git_backup_remote],
        cwd=settings.repo_root,
        text=True,
        capture_output=True,
        check=False,
        env=backup.git_environment(),
    )
    if remote_result.returncode != 0:
        issues.append(
            PreflightIssue(
                f"Git remote {settings.git_backup_remote!r} is not configured.",
                f"Run `git remote add {settings.git_backup_remote} git@github.com:<owner>/<repo>.git` in the repo root.",
            )
        )
        return issues

    remote_url = remote_result.stdout.strip()
    if remote_url.startswith(("http://", "https://")):
        issues.append(
            PreflightIssue(
                f"Git backup remote {remote_url} uses HTTPS, which will not use a deploy key.",
                f"Switch it to SSH with `git remote set-url {settings.git_backup_remote} git@github.com:<owner>/<repo>.git`.",
            )
        )
        return issues

    if not remote_url.startswith(("git@", "ssh://")):
        issues.append(
            PreflightIssue(
                f"Git backup remote {remote_url} is not an SSH remote.",
                f"Use an SSH remote on {settings.git_backup_remote} so the deploy key can authenticate pushes.",
            )
        )
        return issues

    key_path = settings.git_ssh_key_path
    if key_path is None:
        issues.append(
            PreflightIssue(
                "GIT_SSH_KEY_FILE is not configured.",
                "Set GIT_SSH_KEY_FILE in .env, for example `.github-deploy-key` in the repo root.",
            )
        )
    elif not key_path.exists():
        issues.append(
            PreflightIssue(
                f"Deploy key file {key_path} does not exist.",
                "Create the key file on the host, add the public key to GitHub with write access, and retry.",
            )
        )
    elif key_path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        issues.append(
            PreflightIssue(
                f"Deploy key file {key_path} is too permissive for SSH.",
                f"Run `chmod 600 {key_path}` on the host.",
            )
        )

    known_hosts_path = settings.git_ssh_known_hosts_path
    if known_hosts_path is None:
        issues.append(
            PreflightIssue(
                "GIT_SSH_KNOWN_HOSTS_FILE is not configured.",
                "Set GIT_SSH_KNOWN_HOSTS_FILE in .env, for example `.github-known-hosts` in the repo root.",
            )
        )
    elif not known_hosts_path.exists():
        issues.append(
            PreflightIssue(
                f"Known hosts file {known_hosts_path} does not exist.",
                f"Run `ssh-keyscan github.com > {known_hosts_path}` on the host.",
            )
        )

    if issues:
        return issues

    access_result = subprocess.run(
        ["git", "ls-remote", settings.git_backup_remote, "HEAD"],
        cwd=settings.repo_root,
        text=True,
        capture_output=True,
        check=False,
        env=backup.git_environment(),
    )
    if access_result.returncode != 0:
        stderr = access_result.stderr.strip() or "unknown error"
        issues.append(
            PreflightIssue(
                f"Cannot access git remote {settings.git_backup_remote!r}: {stderr}",
                "Verify the deploy key has write access, the remote URL is correct, and outbound SSH to GitHub is allowed.",
            )
        )
    return issues
