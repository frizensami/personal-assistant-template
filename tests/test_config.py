from __future__ import annotations

from personal_assistant.config import Settings


def test_blank_numeric_env_value_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "")

    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )

    assert settings.telegram_allowed_user_id == 0


def test_blank_string_env_value_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )

    assert settings.telegram_bot_token == ""


def test_openclaw_api_token_loads_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_API_TOKEN", "test-openclaw-token")

    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )

    assert settings.openclaw_api_token == "test-openclaw-token"


def test_git_ssh_paths_resolve_relative_to_repo_root(tmp_path):
    settings = Settings(
        _env_file=None,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        git_ssh_key_file=".github-deploy-key",
        git_ssh_known_hosts_file=".github-known-hosts",
    )

    assert settings.git_ssh_key_path == (tmp_path / ".github-deploy-key").resolve()
    assert settings.git_ssh_known_hosts_path == (tmp_path / ".github-known-hosts").resolve()
