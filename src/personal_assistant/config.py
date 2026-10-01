from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_allowed_user_id: int = 0

    operator_password: str = ""
    openclaw_api_token: str = ""
    openai_api_key: str = ""
    openai_admin_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    people_encryption_key: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_token_url: str = "https://oauth2.googleapis.com/token"
    calendar_id: str = "primary"

    public_base_url: str = ""
    default_timezone: str = "UTC"
    free_slot_start_hour: int = 9
    free_slot_end_hour: int = 18
    reminder_poll_seconds: int = 30
    reminder_ack_timeout_minutes: int = 5
    backup_interval_seconds: int = 300

    git_backup_enabled: bool = False
    git_backup_mode: Literal["dedicated_state_repo"] = "dedicated_state_repo"
    git_commit_name: str = "Personal Assistant Bot"
    git_commit_email: str = "assistant@example.com"
    git_backup_remote: str = "state-origin"
    git_backup_branch: str = "main"
    git_ssh_key_file: str = ".github-deploy-key"
    git_ssh_known_hosts_file: str = ".github-known-hosts"

    repo_root: Path = Field(default_factory=lambda: Path("."))
    state_dir: Path = Field(default_factory=lambda: Path("state"))
    runtime_dir: Path = Field(default_factory=lambda: Path("runtime"))

    @model_validator(mode="before")
    @classmethod
    def drop_blank_values_for_non_string_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        for key, value in list(normalized.items()):
            if value != "":
                continue
            field = cls.model_fields.get(key)
            if field is None or cls._annotation_accepts_string(field.annotation):
                continue
            normalized.pop(key)
        return normalized

    @model_validator(mode="after")
    def normalize_paths(self) -> "Settings":
        self.repo_root = self.repo_root.resolve()
        if not self.state_dir.is_absolute():
            self.state_dir = (self.repo_root / self.state_dir).resolve()
        else:
            self.state_dir = self.state_dir.resolve()
        if not self.runtime_dir.is_absolute():
            self.runtime_dir = (self.repo_root / self.runtime_dir).resolve()
        else:
            self.runtime_dir = self.runtime_dir.resolve()
        return self

    @property
    def webhook_url(self) -> str:
        if not self.public_base_url or not self.telegram_webhook_secret:
            return ""
        base = self.public_base_url.rstrip("/")
        return f"{base}/telegram/webhook/{self.telegram_webhook_secret}"

    @property
    def durable_pathspecs(self) -> list[str]:
        return ["state"]

    @property
    def git_uses_dedicated_state_repo(self) -> bool:
        return self.git_backup_mode == "dedicated_state_repo"

    @property
    def git_ssh_key_path(self) -> Path | None:
        return self._resolve_optional_repo_path(self.git_ssh_key_file)

    @property
    def git_ssh_known_hosts_path(self) -> Path | None:
        return self._resolve_optional_repo_path(self.git_ssh_known_hosts_file)

    @staticmethod
    def _annotation_accepts_string(annotation: Any) -> bool:
        if annotation is str:
            return True
        origin = get_origin(annotation)
        if origin is None:
            return False
        return any(Settings._annotation_accepts_string(arg) for arg in get_args(annotation))

    def _resolve_optional_repo_path(self, raw_path: str) -> Path | None:
        raw_path = raw_path.strip()
        if not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.repo_root / candidate
        return candidate.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
