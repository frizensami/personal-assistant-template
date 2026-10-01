from __future__ import annotations

import os
import stat
import subprocess

from personal_assistant.config import Settings
from personal_assistant.preflight import collect_preflight_issues, render_preflight_failure


def _init_repo(repo):
    init = subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=False, capture_output=True)
    if init.returncode != 0:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, capture_output=True)


def test_preflight_reports_missing_required_telegram_settings(tmp_path):
    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        telegram_bot_token="",
        telegram_webhook_secret="",
        telegram_allowed_user_id=0,
        public_base_url="",
        git_backup_enabled=False,
    )

    issues = collect_preflight_issues(settings, target="api")
    rendered = render_preflight_failure(issues)

    assert "TELEGRAM_BOT_TOKEN is missing" in rendered
    assert "TELEGRAM_WEBHOOK_SECRET is missing" in rendered
    assert "TELEGRAM_ALLOWED_USER_ID must be a positive integer" in rendered
    assert "PUBLIC_BASE_URL is missing" in rendered


def test_local_preflight_only_requires_local_smoke_test_settings(tmp_path):
    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        telegram_bot_token="",
        telegram_webhook_secret="local-secret",
        telegram_allowed_user_id=12345,
        public_base_url="",
        git_backup_enabled=False,
    )

    issues = collect_preflight_issues(settings, target="local")

    assert issues == []


def test_preflight_requires_ssh_remote_for_git_backup(tmp_path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/personal-assistant.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        telegram_bot_token="bot-token",
        telegram_webhook_secret="secret",
        telegram_allowed_user_id=123,
        public_base_url="https://apps.example.com",
        git_backup_enabled=True,
        git_backup_remote="origin",
    )

    issues = collect_preflight_issues(settings, target="worker")

    assert any("uses HTTPS" in issue.summary for issue in issues)


def test_preflight_reports_missing_key_and_known_hosts_for_backup(tmp_path):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/personal-assistant.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        telegram_bot_token="bot-token",
        telegram_webhook_secret="secret",
        telegram_allowed_user_id=123,
        public_base_url="https://apps.example.com",
        git_backup_enabled=True,
        git_backup_remote="origin",
        git_ssh_key_file=".github-deploy-key",
        git_ssh_known_hosts_file=".github-known-hosts",
    )

    issues = collect_preflight_issues(settings, target="service")

    assert any("Deploy key file" in issue.summary for issue in issues)
    assert any("Known hosts file" in issue.summary for issue in issues)


def test_preflight_checks_remote_access_once_git_auth_files_exist(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/personal-assistant.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    key_path = tmp_path / ".github-deploy-key"
    key_path.write_text("private-key", encoding="utf-8")
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)

    known_hosts_path = tmp_path / ".github-known-hosts"
    known_hosts_path.write_text("github.com ssh-ed25519 AAAA...", encoding="utf-8")

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if list(args[:3]) == ["git", "ls-remote", "origin"]:
            return subprocess.CompletedProcess(args, 128, "", "Permission denied (publickey).")
        return real_run(args, **kwargs)

    monkeypatch.setattr("personal_assistant.preflight.subprocess.run", fake_run)

    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        telegram_bot_token="bot-token",
        telegram_webhook_secret="secret",
        telegram_allowed_user_id=123,
        public_base_url="https://apps.example.com",
        git_backup_enabled=True,
        git_backup_remote="origin",
        git_ssh_key_file=".github-deploy-key",
        git_ssh_known_hosts_file=".github-known-hosts",
    )

    issues = collect_preflight_issues(settings, target="api")

    assert any("Cannot access git remote" in issue.summary for issue in issues)
