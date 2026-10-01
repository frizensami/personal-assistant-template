from __future__ import annotations

from personal_assistant.backup import GitBackupService


async def test_unconfigured_allowlist_denies_messages_and_callbacks(settings, assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle
    settings.telegram_allowed_user_id = 0
    await assistant.process_update({"update_id": 201, "message": {"chat": {"id": "synthetic-chat", "type": "private"}, "from": {"id": 12345}, "text": "add task forbidden"}})
    await assistant.process_update({"update_id": 202, "callback_query": {"id": "synthetic-callback", "from": {"id": 12345}, "message": {"chat": {"id": "synthetic-chat", "type": "private"}}, "data": "menu:tasks"}})
    assert telegram.sent_messages == []
    assert assistant.storage.list_tasks() == []


def test_empty_webhook_secret_never_authenticates(settings, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    settings.telegram_webhook_secret = ""
    assert assistant.webhook_secret_matches("") is False


def test_manual_force_cannot_enable_disabled_backup(settings):
    settings.git_backup_enabled = False
    backup = GitBackupService(settings)
    assert backup.backup_now(force=True) is False
    assert backup.read_status()["status"] == "disabled"


def test_template_rejects_backup_to_code_repository():
    import pytest
    from pydantic import ValidationError
    from personal_assistant.config import Settings
    with pytest.raises(ValidationError):
        Settings(_env_file=None, git_backup_mode="same_repo")
