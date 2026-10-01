from __future__ import annotations

import asyncio
import contextlib

from personal_assistant.assistant import AssistantService
from personal_assistant.backup import GitBackupService

# How often (in seconds) to check for unacknowledged reminders.
_ACK_CHECK_INTERVAL_SECONDS = 60
_MIN_BACKUP_LOOP_INTERVAL_SECONDS = 60 * 60
_MAINTENANCE_LOOP_INTERVAL_SECONDS = 6 * 60 * 60


class BackgroundWorker:
    def __init__(
        self,
        assistant: AssistantService,
        backup_service: GitBackupService,
    ) -> None:
        self.assistant = assistant
        self.backup_service = backup_service
        self.settings = assistant.settings
        self._last_backup_alert_status = str(self.backup_service.read_status().get("status") or "").strip()

    async def run_forever(self) -> None:
        reminder_task = asyncio.create_task(self._reminder_loop())
        ack_task = asyncio.create_task(self._ack_check_loop())
        backup_task = asyncio.create_task(self._backup_loop())
        maintenance_task = asyncio.create_task(self._maintenance_loop())
        try:
            await asyncio.gather(reminder_task, ack_task, backup_task, maintenance_task)
        finally:
            reminder_task.cancel()
            ack_task.cancel()
            backup_task.cancel()
            maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reminder_task
            with contextlib.suppress(asyncio.CancelledError):
                await ack_task
            with contextlib.suppress(asyncio.CancelledError):
                await backup_task
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance_task
            await asyncio.to_thread(self.backup_service.backup_now)

    async def replay_due_reminders(self) -> int:
        return await self.assistant.send_due_reminders()

    async def _reminder_loop(self) -> None:
        while True:
            try:
                await self.assistant.send_due_reminders()
            except Exception as exc:
                print(f"Reminder loop error: {type(exc).__name__}")
            poll_seconds = self.assistant.storage.get_user_settings().reminder_poll_seconds
            await asyncio.sleep(poll_seconds)

    async def _ack_check_loop(self) -> None:
        """Periodically re-send reminders that were fired but not yet acknowledged."""
        while True:
            try:
                await self.assistant.resend_unacked_reminders()
            except Exception as exc:
                print(f"Ack check loop error: {type(exc).__name__}")
            await asyncio.sleep(_ACK_CHECK_INTERVAL_SECONDS)

    async def _backup_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.backup_service.backup_now)
                await self._maybe_notify_backup_status()
            except Exception as exc:
                print(f"Backup loop error: {type(exc).__name__}")
                await self._maybe_notify_backup_status()
            await asyncio.sleep(max(self.settings.backup_interval_seconds, _MIN_BACKUP_LOOP_INTERVAL_SECONDS))

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.assistant.storage.run_maintenance)
            except Exception as exc:
                print(f"Maintenance loop error: {type(exc).__name__}")
            await asyncio.sleep(_MAINTENANCE_LOOP_INTERVAL_SECONDS)

    async def _maybe_notify_backup_status(self) -> None:
        if not self.settings.telegram_allowed_user_id:
            return
        status = self.backup_service.read_status()
        current = str(status.get("status") or "").strip()
        if current == self._last_backup_alert_status:
            return
        previous = self._last_backup_alert_status
        self._last_backup_alert_status = current
        if current == "failed":
            detail = str(status.get("detail") or "Backup failed.").strip()
            await self.assistant.telegram.send_message(
                self.settings.telegram_allowed_user_id,
                f"⚠️ Backup failed.\n{detail}",
            )
        elif current == "success" and previous == "failed":
            await self.assistant.telegram.send_message(
                self.settings.telegram_allowed_user_id,
                "✅ Backup recovered and completed successfully.",
            )
