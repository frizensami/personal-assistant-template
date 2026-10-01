from __future__ import annotations

import base64
import hmac
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import fcntl
import yaml
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from personal_assistant.config import Settings
from personal_assistant.models import (
    ConversationSummary,
    NoteRecord,
    OpenAITraceRecord,
    PlanRecord,
    PreferenceRecord,
    ProjectRecord,
    ReminderRecord,
    TaskRecord,
    TaskSummary,
    TelegramState,
    UserSettings,
)
from personal_assistant.time_utils import now_utc


TASK_SLUG_RE = re.compile(r"[^a-z0-9]+")
TASK_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_UNSET = object()

_PEOPLE_KDF_SALT = b"personal-assistant-people-v1"
_QUARANTINE_RETENTION_DAYS = 14


def _json_dumps(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)


def _write_bytes_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)


def _slugify(text: str, fallback: str) -> str:
    slug = TASK_SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or fallback


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_dir = settings.state_dir
        self.runtime_dir = settings.runtime_dir
        self.locks_dir = self.runtime_dir / "locks"
        self.transactions_dir = self.runtime_dir / "transactions"
        self.tasks_dir = self.state_dir / "tasks"
        self.indexes_dir = self.state_dir / "indexes"
        self.notes_dir = self.state_dir / "notes"
        self.people_dir = self.state_dir / "people"
        self.private_dir = self.state_dir / "private"
        self.preferences_dir = self.state_dir / "preferences"
        self.projects_dir = self.state_dir / "projects"
        self.routines_dir = self.state_dir / "routines"
        self.plans_dir = self.state_dir / "plans"
        self.memory_dir = self.state_dir / "memory"
        self.reminders_path = self.state_dir / "reminders" / "schedule.json"
        self.tasks_index_path = self.indexes_dir / "tasks.json"
        self.notes_index_path = self.notes_dir / "index.json"
        self.plans_index_path = self.plans_dir / "index.json"
        self.projects_index_path = self.projects_dir / "index.json"
        self.preferences_index_path = self.preferences_dir / "index.json"
        self.calendar_links_path = self.state_dir / "calendar" / "event_links.json"
        self.conversation_path = self.state_dir / "conversation" / "summaries.json"
        self.openai_history_path = self.state_dir / "openai" / "history.json"
        self.automation_ideas_path = self.state_dir / "automation" / "ideas.md"
        self.telegram_state_path = self.state_dir / "telegram" / "state.json"
        self.global_lock_path = self.runtime_dir / "state.mutex"
        self.quarantine_dir = self.runtime_dir / "quarantine"
        self.people_enc_path = self.people_dir / "people.enc"
        self.private_tasks_enc_path = self.private_dir / "tasks.enc"
        self.private_reminders_enc_path = self.private_dir / "reminders.enc"
        self.user_settings_path = self.state_dir / "user_settings.json"

    def bootstrap(self) -> None:
        for path in (
            self.state_dir,
            self.runtime_dir,
            self.locks_dir,
            self.transactions_dir,
            self.quarantine_dir,
            self.tasks_dir,
            self.indexes_dir,
            self.notes_dir,
            self.people_dir,
            self.private_dir,
            self.preferences_dir,
            self.projects_dir,
            self.routines_dir,
            self.plans_dir,
            self.memory_dir,
            self.reminders_path.parent,
            self.calendar_links_path.parent,
            self.conversation_path.parent,
            self.openai_history_path.parent,
            self.automation_ideas_path.parent,
            self.telegram_state_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        text_defaults = {
            self.tasks_dir / ".gitkeep": "",
            self.notes_dir / "inbox.md": "# Inbox\n\nCaptured notes land here.\n",
            self.preferences_dir / "preferences.md": "# Preferences\n\nStable preferences and working assumptions.\n",
            self.projects_dir / "projects.md": "# Projects\n\nLonger-term work and outcomes.\n",
            self.routines_dir / "routines.md": "# Routines\n\nRecurring routines and habits.\n",
            self.plans_dir / "plans.md": "# Plans\n\nPlanning notes and draft plans.\n",
            self.memory_dir / "instructions.md": (
                "# Assistant Memory\n\n"
                "- You are a practical day-to-day assistant.\n"
                "- Prefer concise replies.\n"
                "- Google Calendar is the source of truth for scheduled time.\n"
                "- Only modify assistant-owned calendar events unless explicitly extended later.\n"
            ),
        }
        for path, content in text_defaults.items():
            if not path.exists():
                _write_text_file(path, content)
        if not self.tasks_index_path.exists():
            _write_text_file(self.tasks_index_path, _json_dumps({"tasks": []}))
        if not self.notes_index_path.exists():
            _write_text_file(self.notes_index_path, _json_dumps({"notes": []}))
        if not self.plans_index_path.exists():
            _write_text_file(self.plans_index_path, _json_dumps({"plans": []}))
        if not self.projects_index_path.exists():
            _write_text_file(self.projects_index_path, _json_dumps({"projects": []}))
        if not self.preferences_index_path.exists():
            _write_text_file(self.preferences_index_path, _json_dumps({"preferences": []}))
        if not self.reminders_path.exists():
            _write_text_file(self.reminders_path, _json_dumps({"reminders": []}))
        if not self.calendar_links_path.exists():
            _write_text_file(self.calendar_links_path, _json_dumps({"links": {}}))
        if not self.conversation_path.exists():
            _write_text_file(self.conversation_path, _json_dumps({"summaries": {}}))
        if not self.openai_history_path.exists():
            _write_text_file(self.openai_history_path, _json_dumps({"calls": []}))
        if not self.automation_ideas_path.exists():
            _write_text_file(
                self.automation_ideas_path,
                "# AI Automation Ideas\n\n"
                "Ideas logged by the AI agent when it solves something that looks worth hardening into a deterministic feature.\n",
            )
        if not self.telegram_state_path.exists():
            _write_text_file(
                self.telegram_state_path,
                _json_dumps(
                    {
                        "processed_update_ids": [],
                        "last_message_by_chat": {},
                        "pending_by_chat": {},
                        "undo_by_chat": {},
                        "references_by_chat": {},
                        "ai_runs_by_chat": {},
                    }
                ),
            )

    def recover_transactions(self) -> None:
        self.bootstrap()
        with self.mutation_lock():
            for tx_dir in sorted(self.transactions_dir.glob("*")):
                journal_path = tx_dir / "journal.json"
                # An active writer creates the transaction directory before the journal.
                # Do not treat a journal-less directory as garbage during recovery.
                if not journal_path.exists():
                    continue
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                phase = journal.get("phase", "prepare")
                if phase == "prepare":
                    shutil.rmtree(tx_dir, ignore_errors=True)
                    continue
                self._finish_commit(tx_dir, journal)

    @contextmanager
    def lock_paths(self, targets: list[Path]) -> Iterator[None]:
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        handles = []
        try:
            for target in sorted({path.resolve() for path in targets}, key=lambda path: str(path)):
                lock_name = target.as_posix().replace("/", "_").replace(":", "_")
                lock_path = self.locks_dir / f"{lock_name}.lock"
                handle = open(lock_path, "a+", encoding="utf-8")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handles.append(handle)
            yield
        finally:
            for handle in reversed(handles):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        with self.lock_paths([self.global_lock_path]):
            yield

    def _write_transaction(self, text_by_path: dict[Path, str | None]) -> None:
        targets = list(text_by_path.keys())
        with self.lock_paths(targets):
            tx_dir = self.transactions_dir / uuid4().hex
            new_dir = tx_dir / "new"
            new_dir.mkdir(parents=True, exist_ok=True)
            backup_dir = tx_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)

            operations: list[dict[str, object]] = []
            for index, target in enumerate(sorted(targets, key=lambda path: str(path))):
                target.parent.mkdir(parents=True, exist_ok=True)
                content = text_by_path[target]
                new_rel: str | None = None
                if content is not None:
                    staged_rel = Path("new") / f"{index}.txt"
                    new_path = tx_dir / staged_rel
                    _write_text_file(new_path, content)
                    new_rel = str(staged_rel)

                backup_rel: str | None = None
                if target.exists():
                    backup_rel = f"backups/{index}.bak"
                    shutil.copy2(target, tx_dir / backup_rel)

                operations.append(
                    {
                        "target": str(target),
                        "new_rel": new_rel,
                        "backup_rel": backup_rel,
                    }
                )

            journal_path = tx_dir / "journal.json"
            _write_text_file(journal_path, _json_dumps({"phase": "prepare", "operations": operations}))
            _write_text_file(journal_path, _json_dumps({"phase": "commit", "operations": operations}))
            self._finish_commit(tx_dir, {"phase": "commit", "operations": operations})

    def _finish_commit(self, tx_dir: Path, journal: dict[str, object]) -> None:
        for index, operation in enumerate(journal["operations"]):
            operation = dict(operation)
            target = Path(str(operation["target"]))
            new_rel = operation.get("new_rel")
            if new_rel is None:
                if target.exists():
                    target.unlink()
                continue
            new_source = tx_dir / str(new_rel)
            if not new_source.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                handle.write(new_source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
                stage_path = Path(handle.name)
            os.replace(stage_path, target)
        shutil.rmtree(tx_dir, ignore_errors=True)

    def _load_json(self, path: Path, default: object) -> object:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._quarantine_corrupt_json(path, default)
            return default

    @staticmethod
    def _json_default_text(default: object) -> str:
        return _json_dumps(default)

    def _quarantine_corrupt_json(self, path: Path, default: object) -> None:
        self.bootstrap()
        if not path.exists():
            return
        timestamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        quarantine_name = f"{path.name}.{timestamp}.corrupt"
        quarantine_path = self.quarantine_dir / quarantine_name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(quarantine_path))
        _write_text_file(path, self._json_default_text(default))

    def list_quarantined_files(self) -> list[dict[str, Any]]:
        self.bootstrap()
        items: list[dict[str, Any]] = []
        for path in sorted(self.quarantine_dir.glob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=now_utc().tzinfo),
                }
            )
        items.sort(key=lambda item: item["modified_at"], reverse=True)
        return items

    def run_maintenance(self) -> dict[str, Any]:
        self.bootstrap()
        repaired: list[str] = []
        for path, default in (
            (self.telegram_state_path, TelegramState().model_dump(mode="json")),
            (self.openai_history_path, {"calls": []}),
            (self.conversation_path, {"summaries": {}}),
            (self.reminders_path, {"reminders": []}),
            (self.calendar_links_path, {"links": {}}),
        ):
            before = len(self.list_quarantined_files())
            self._load_json(path, default)
            after = len(self.list_quarantined_files())
            if after > before:
                repaired.append(path.name)
        self.rebuild_indexes()
        trimmed_openai = 0
        trimmed_updates = 0
        with self.mutation_lock():
            history = self._load_json(self.openai_history_path, {"calls": []})
            items = history.get("calls", []) if isinstance(history, dict) else []
            if len(items) > 50:
                trimmed_openai = len(items) - 50
                self._write_transaction({self.openai_history_path: _json_dumps({"calls": items[-50:]})})
            state = self.get_telegram_state()
            if len(state.processed_update_ids) > 500:
                trimmed_updates = len(state.processed_update_ids) - 500
                state.processed_update_ids = state.processed_update_ids[-500:]
                self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})
        pruned_quarantine = self.prune_quarantine_files()
        return {
            "repaired": repaired,
            "trimmed_openai": trimmed_openai,
            "trimmed_updates": trimmed_updates,
            "pruned_quarantine": pruned_quarantine,
            "quarantine_count": len(self.list_quarantined_files()),
            "pending_transactions": len([p for p in self.transactions_dir.glob("*") if p.is_dir()]),
        }

    def prune_quarantine_files(self, *, retention_days: int = _QUARANTINE_RETENTION_DAYS) -> int:
        self.bootstrap()
        cutoff = now_utc() - timedelta(days=max(retention_days, 0))
        removed = 0
        for item in self.quarantine_dir.glob("*"):
            if not item.is_file():
                continue
            modified_at = datetime.fromtimestamp(item.stat().st_mtime, tz=now_utc().tzinfo)
            if modified_at >= cutoff:
                continue
            item.unlink(missing_ok=True)
            removed += 1
        return removed

    def _private_secret(self, password: str | None = None) -> str:
        secret = (password if password is not None else self.settings.people_encryption_key).strip()
        if not secret:
            raise ValueError("Private data encryption is not configured.")
        return secret

    def _encrypt_text(self, text: str, password: str) -> str:
        return self._fernet(password).encrypt(text.encode("utf-8")).decode("ascii")

    def _decrypt_text(self, encrypted_text: str, password: str) -> str:
        return self._fernet(password).decrypt(encrypted_text.encode("ascii")).decode("utf-8")

    def _load_private_items(
        self,
        path: Path,
        *,
        payload_key: str,
        password: str | None = None,
        model: type[TaskRecord] | type[ReminderRecord],
    ) -> list[TaskRecord] | list[ReminderRecord]:
        self.bootstrap()
        if not path.exists():
            return []
        secret = self._private_secret(password)
        try:
            encrypted = path.read_text(encoding="utf-8").strip()
            if not encrypted:
                return []
            payload = json.loads(self._decrypt_text(encrypted, secret))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Private data could not be decrypted.")
        items = payload.get(payload_key, []) if isinstance(payload, dict) else []
        return [model.model_validate(item) for item in items if isinstance(item, dict)]

    def _store_private_items(
        self,
        *,
        tasks: list[TaskRecord] | None = None,
        reminders: list[ReminderRecord] | None = None,
        password: str | None = None,
    ) -> None:
        secret = self._private_secret(password)
        payload: dict[Path, str] = {}
        if tasks is not None:
            payload[self.private_tasks_enc_path] = self._encrypt_text(
                _json_dumps({"tasks": [item.model_dump(mode="json") for item in tasks]}),
                secret,
            )
        if reminders is not None:
            payload[self.private_reminders_enc_path] = self._encrypt_text(
                _json_dumps({"reminders": [item.model_dump(mode="json") for item in reminders]}),
                secret,
            )
        if payload:
            self._write_transaction(payload)

    def _load_task_from_file(self, path: Path) -> TaskRecord:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(text)
        metadata = yaml.safe_load(frontmatter) or {}
        metadata["body"] = body.strip()
        return TaskRecord.model_validate(metadata)

    def _split_frontmatter(self, text: str) -> tuple[str, str]:
        if not text.startswith("---\n"):
            return "{}", text
        _, remainder = text.split("---\n", 1)
        frontmatter, body = remainder.split("\n---\n", 1)
        return frontmatter, body

    def _render_task(self, task: TaskRecord) -> str:
        metadata = task.model_dump(mode="json")
        body = metadata.pop("body", "").strip()
        frontmatter = yaml.safe_dump(metadata, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\n{body}\n"

    def list_tasks(
        self,
        *,
        include_completed: bool = False,
        sort_by: str = "default",
        tag: str | None = None,
        kind: str | None = "self",
    ) -> list[TaskRecord]:
        self.bootstrap()
        tasks: list[TaskRecord] = []
        for path in sorted(self.tasks_dir.glob("*.md")):
            task = self._load_task_from_file(path)
            if include_completed or task.status == "open":
                tasks.append(task)
        if kind is not None:
            wanted_kind = kind.strip().lower()
            tasks = [task for task in tasks if task.kind == wanted_kind]
        if tag:
            wanted = tag.strip().lstrip("#").lower()
            tasks = [task for task in tasks if any(t.lower() == wanted for t in task.tags)]
        return self._sort_tasks(tasks, sort_by=sort_by)

    def _sort_tasks(self, tasks: list[TaskRecord], *, sort_by: str = "default") -> list[TaskRecord]:
        def priority_key(task: TaskRecord) -> tuple[int]:
            return (TASK_PRIORITY_ORDER.get(task.priority, 1),)

        if sort_by == "priority":
            return sorted(
                tasks,
                key=lambda task: (
                    not task.is_current,
                    *priority_key(task),
                    task.due_at is None,
                    task.due_at or task.created_at,
                    task.created_at,
                ),
            )
        if sort_by == "due":
            return sorted(
                tasks,
                key=lambda task: (
                    not task.is_current,
                    *priority_key(task),
                    task.due_at is None,
                    task.due_at or task.created_at,
                    task.created_at,
                ),
            )
        if sort_by == "remind":
            return sorted(
                tasks,
                key=lambda task: (
                    not task.is_current,
                    *priority_key(task),
                    task.reminder_at is None,
                    task.reminder_at or task.created_at,
                    task.created_at,
                ),
            )
        if sort_by == "tag":
            return sorted(
                tasks,
                key=lambda task: (
                    not task.is_current,
                    *priority_key(task),
                    not task.tags,
                    task.tags[0].lower() if task.tags else "~",
                    task.title.lower(),
                ),
            )
        return sorted(
            tasks,
            key=lambda task: (
                not task.is_current,
                *priority_key(task),
                task.due_at or task.created_at,
                task.created_at,
            ),
        )

    def create_task(
        self,
        title: str,
        *,
        body: str = "",
        tags: list[str] | None = None,
        kind: str = "self",
        priority: str = "medium",
        due_at: datetime | None = None,
        reminder_at: datetime | None = None,
    ) -> TaskRecord:
        self.bootstrap()
        with self.mutation_lock():
            now = now_utc()
            task_id = self._task_id_from_title(title)
            task_kind = kind.strip().lower()
            task = TaskRecord(
                id=task_id,
                title=title.strip(),
                body=body.strip(),
                tags=tags or [],
                kind=task_kind if task_kind in {"self", "other"} else "self",
                status="open",
                priority=priority if priority in TASK_PRIORITY_ORDER else "medium",
                created_at=now,
                updated_at=now,
                due_at=due_at,
                reminder_at=reminder_at,
            )
            reminders = self._load_reminders()
            if reminder_at is not None:
                reminders.append(
                    ReminderRecord(
                        id=f"task-{task.id}",
                        text=f"Task reminder: {task.title}",
                        due_at=reminder_at,
                        source_type="task",
                        source_id=task.id,
                        created_at=now,
                        updated_at=now,
                    )
                )
            tasks_index = self._build_tasks_index(self.list_tasks(include_completed=True, kind=None) + [task])
            schedule_payload = {"reminders": [item.model_dump(mode="json") for item in reminders]}
            self._write_transaction(
                {
                    self.tasks_dir / f"{task.id}.md": self._render_task(task),
                    self.tasks_index_path: _json_dumps(tasks_index),
                    self.reminders_path: _json_dumps(schedule_payload),
                }
            )
        return task

    def complete_task(self, identifier: str) -> TaskRecord | None:
        with self.mutation_lock():
            task = self.find_task(identifier)
            if task is None:
                return None
            if task.status == "completed":
                return task
            now = now_utc()
            task.status = "completed"
            task.is_current = False
            task.completed_at = now
            task.updated_at = now

            existing_tasks = [item for item in self.list_tasks(include_completed=True, kind=None) if item.id != task.id]
            tasks_index = self._build_tasks_index(existing_tasks + [task])
            reminders = [item for item in self._load_reminders() if item.source_id != task.id]

            self._write_transaction(
                {
                    self.tasks_dir / f"{task.id}.md": self._render_task(task),
                    self.tasks_index_path: _json_dumps(tasks_index),
                    self.reminders_path: _json_dumps(
                        {"reminders": [item.model_dump(mode="json") for item in reminders]}
                    ),
                }
            )
        return task

    def set_current_task(self, identifier: str) -> TaskRecord | None:
        with self.mutation_lock():
            task = self.find_task(identifier)
            if task is None or task.status != "open" or task.kind != "self":
                return None
            now = now_utc()
            tasks = self.list_tasks(include_completed=True, kind=None)
            updated_tasks: list[TaskRecord] = []
            for item in tasks:
                should_be_current = item.id == task.id
                if item.is_current != should_be_current:
                    item.is_current = should_be_current
                    item.updated_at = now
                if should_be_current:
                    task = item
                updated_tasks.append(item)
            payload: dict[Path, str | None] = {
                self.tasks_index_path: _json_dumps(self._build_tasks_index(updated_tasks)),
            }
            for item in updated_tasks:
                payload[self.tasks_dir / f"{item.id}.md"] = self._render_task(item)
            self._write_transaction(payload)
        return task

    def find_task(self, identifier: str) -> TaskRecord | None:
        wanted = identifier.strip().lower()
        tasks = self.list_tasks(include_completed=True, kind=None)
        for task in tasks:
            if task.id == wanted:
                return task
        for task in tasks:
            if task.title.strip().lower() == wanted:
                return task
        matches = [item for item in tasks if wanted and item.id.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        for task in tasks:
            if wanted in task.title.lower():
                return task
        return None

    def update_task(
        self,
        identifier: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        priority: str | None = None,
        due_at: datetime | None | object = _UNSET,
        reminder_at: datetime | None | object = _UNSET,
    ) -> TaskRecord | None:
        with self.mutation_lock():
            task = self.find_task(identifier)
            if task is None:
                return None
            if title is not None:
                task.title = title.strip()
            if body is not None:
                task.body = body.strip()
            if tags is not None:
                task.tags = tags
            if priority is not None:
                task.priority = priority if priority in TASK_PRIORITY_ORDER else task.priority
            if due_at is not _UNSET:
                task.due_at = due_at
            if reminder_at is not _UNSET:
                task.reminder_at = reminder_at
            task.updated_at = now_utc()
            existing_tasks = [item for item in self.list_tasks(include_completed=True, kind=None) if item.id != task.id]
            tasks_index = self._build_tasks_index(existing_tasks + [task])
            payload = {
                self.tasks_dir / f"{task.id}.md": self._render_task(task),
                self.tasks_index_path: _json_dumps(tasks_index),
            }
            reminders = self._load_reminders()
            reminder_changed = False
            task_reminder: ReminderRecord | None = None
            filtered_reminders: list[ReminderRecord] = []
            for reminder in reminders:
                if reminder.source_id == task.id:
                    if task_reminder is None:
                        task_reminder = reminder
                    reminder_changed = True
                    continue
                filtered_reminders.append(reminder)
            if task.reminder_at is not None:
                if task_reminder is None:
                    task_reminder = ReminderRecord(
                        id=f"task-{task.id}",
                        text=f"Task reminder: {task.title}",
                        due_at=task.reminder_at,
                        source_type="task",
                        source_id=task.id,
                        created_at=task.created_at,
                        updated_at=task.updated_at,
                    )
                else:
                    task_reminder.text = f"Task reminder: {task.title}"
                    task_reminder.due_at = task.reminder_at
                    task_reminder.status = "scheduled"
                    task_reminder.last_sent_at = None
                    task_reminder.acked_at = None
                    task_reminder.updated_at = task.updated_at
                filtered_reminders.append(task_reminder)
                reminder_changed = True
            if reminder_changed:
                payload[self.reminders_path] = _json_dumps(
                    {"reminders": [item.model_dump(mode="json") for item in filtered_reminders]}
                )
            self._write_transaction(payload)
        return task

    def retag_tasks(self, old_tag: str, new_tag: str, *, include_completed: bool = True) -> list[TaskRecord]:
        old_clean = old_tag.strip().lstrip("#")
        new_clean = new_tag.strip().lstrip("#")
        if not old_clean or not new_clean:
            return []
        old_lower = old_clean.lower()
        with self.mutation_lock():
            tasks = self.list_tasks(include_completed=True, kind=None)
            changed: list[TaskRecord] = []
            current = now_utc()
            payload: dict[Path, str | None] = {}
            for task in tasks:
                if not include_completed and task.status != "open":
                    continue
                if not any(tag.lower() == old_lower for tag in task.tags):
                    continue
                updated_tags: list[str] = []
                seen: set[str] = set()
                for tag in task.tags:
                    replacement = new_clean if tag.lower() == old_lower else tag
                    key = replacement.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    updated_tags.append(replacement)
                task.tags = updated_tags
                task.updated_at = current
                changed.append(task)
                payload[self.tasks_dir / f"{task.id}.md"] = self._render_task(task)
            if not changed:
                return []
            payload[self.tasks_index_path] = _json_dumps(self._build_tasks_index(tasks))
            self._write_transaction(payload)
        return changed

    def delete_task(self, identifier: str) -> TaskRecord | None:
        with self.mutation_lock():
            task = self.find_task(identifier)
            if task is None:
                return None
            remaining_tasks = [item for item in self.list_tasks(include_completed=True, kind=None) if item.id != task.id]
            remaining_reminders = [item for item in self._load_reminders() if item.source_id != task.id]
            self._write_transaction(
                {
                    self.tasks_dir / f"{task.id}.md": None,
                    self.tasks_index_path: _json_dumps(self._build_tasks_index(remaining_tasks)),
                    self.reminders_path: _json_dumps(
                        {"reminders": [item.model_dump(mode="json") for item in remaining_reminders]}
                    ),
                }
            )
        return task

    def delete_all_tasks(self) -> int:
        with self.mutation_lock():
            tasks = self.list_tasks(include_completed=True, kind=None)
            if not tasks:
                return 0
            payload: dict[Path, str | None] = {
                self.tasks_index_path: _json_dumps(self._build_tasks_index([])),
                self.reminders_path: _json_dumps(
                    {"reminders": [item.model_dump(mode="json") for item in self._load_reminders() if item.source_type != "task"]}
                ),
            }
            for task in tasks:
                payload[self.tasks_dir / f"{task.id}.md"] = None
            self._write_transaction(payload)
        return len(tasks)

    # ------------------------------------------------------------------
    # Private tasks (encrypted)
    # ------------------------------------------------------------------

    def _private_task_id(self) -> str:
        return f"private-task-{uuid4().hex[:12]}"

    def _private_task_reminder_id(self, task_id: str) -> str:
        return f"private-task-reminder-{task_id}"

    def _load_private_tasks(self, password: str | None = None) -> list[TaskRecord]:
        items = self._load_private_items(
            self.private_tasks_enc_path,
            payload_key="tasks",
            password=password,
            model=TaskRecord,
        )
        return list(items)

    def _load_private_reminders(self, password: str | None = None) -> list[ReminderRecord]:
        items = self._load_private_items(
            self.private_reminders_enc_path,
            payload_key="reminders",
            password=password,
            model=ReminderRecord,
        )
        return list(items)

    def list_private_tasks(
        self,
        password: str | None = None,
        *,
        include_completed: bool = False,
        sort_by: str = "default",
        tag: str | None = None,
    ) -> list[TaskRecord]:
        tasks = self._load_private_tasks(password)
        if not include_completed:
            tasks = [task for task in tasks if task.status == "open"]
        if tag:
            wanted = tag.strip().lstrip("#").lower()
            tasks = [task for task in tasks if any(t.lower() == wanted for t in task.tags)]
        return self._sort_tasks(tasks, sort_by=sort_by)

    def find_private_task(self, identifier: str, password: str | None = None) -> TaskRecord | None:
        wanted = identifier.strip().lower()
        tasks = self.list_private_tasks(password, include_completed=True)
        for task in tasks:
            if task.id == wanted:
                return task
        for task in tasks:
            if task.title.strip().lower() == wanted:
                return task
        matches = [item for item in tasks if wanted and item.id.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        for task in tasks:
            if wanted in task.title.lower():
                return task
        return None

    def create_private_task(
        self,
        title: str,
        *,
        password: str | None = None,
        body: str = "",
        tags: list[str] | None = None,
        priority: str = "medium",
        due_at: datetime | None = None,
        reminder_at: datetime | None = None,
    ) -> TaskRecord:
        self.bootstrap()
        with self.mutation_lock():
            secret = self._private_secret(password)
            now = now_utc()
            task = TaskRecord(
                id=self._private_task_id(),
                title=title.strip(),
                body=body.strip(),
                tags=tags or [],
                status="open",
                priority=priority if priority in TASK_PRIORITY_ORDER else "medium",
                created_at=now,
                updated_at=now,
                due_at=due_at,
                reminder_at=reminder_at,
            )
            tasks = self._load_private_tasks(secret)
            reminders = self._load_private_reminders(secret)
            tasks.append(task)
            if reminder_at is not None:
                reminders.append(
                    ReminderRecord(
                        id=self._private_task_reminder_id(task.id),
                        text=f"Task reminder: {task.title}",
                        due_at=reminder_at,
                        source_type="private_task",
                        source_id=task.id,
                        created_at=now,
                        updated_at=now,
                    )
                )
            self._store_private_items(tasks=tasks, reminders=reminders, password=secret)
        return task

    def update_private_task(
        self,
        identifier: str,
        *,
        password: str | None = None,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        priority: str | None = None,
        due_at: datetime | None | object = _UNSET,
        reminder_at: datetime | None | object = _UNSET,
    ) -> TaskRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            tasks = self._load_private_tasks(secret)
            found: TaskRecord | None = None
            for task in tasks:
                if (
                    task.id == identifier.strip().lower()
                    or task.id.startswith(identifier.strip().lower())
                    or identifier.strip().lower() == task.title.strip().lower()
                    or identifier.strip().lower() in task.title.lower()
                ):
                    found = task
                    break
            if found is None:
                return None
            if title is not None:
                found.title = title.strip()
            if body is not None:
                found.body = body.strip()
            if tags is not None:
                found.tags = tags
            if priority is not None:
                found.priority = priority if priority in TASK_PRIORITY_ORDER else found.priority
            if due_at is not _UNSET:
                found.due_at = due_at
            if reminder_at is not _UNSET:
                found.reminder_at = reminder_at
            found.updated_at = now_utc()

            reminders = self._load_private_reminders(secret)
            filtered: list[ReminderRecord] = []
            linked: ReminderRecord | None = None
            for reminder in reminders:
                if reminder.source_id == found.id and reminder.source_type == "private_task":
                    if linked is None:
                        linked = reminder
                    continue
                filtered.append(reminder)
            if found.reminder_at is not None:
                if linked is None:
                    linked = ReminderRecord(
                        id=self._private_task_reminder_id(found.id),
                        text=f"Task reminder: {found.title}",
                        due_at=found.reminder_at,
                        source_type="private_task",
                        source_id=found.id,
                        created_at=found.created_at,
                        updated_at=found.updated_at,
                    )
                else:
                    linked.text = f"Task reminder: {found.title}"
                    linked.due_at = found.reminder_at
                    linked.status = "scheduled"
                    linked.last_sent_at = None
                    linked.acked_at = None
                    linked.updated_at = found.updated_at
                filtered.append(linked)
            self._store_private_items(tasks=tasks, reminders=filtered, password=secret)
        return found

    def complete_private_task(self, identifier: str, password: str | None = None) -> TaskRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            tasks = self._load_private_tasks(secret)
            found: TaskRecord | None = None
            for task in tasks:
                if (
                    task.id == identifier.strip().lower()
                    or task.id.startswith(identifier.strip().lower())
                    or identifier.strip().lower() == task.title.strip().lower()
                    or identifier.strip().lower() in task.title.lower()
                ):
                    found = task
                    break
            if found is None:
                return None
            if found.status != "completed":
                current = now_utc()
                found.status = "completed"
                found.completed_at = current
                found.updated_at = current
            reminders = [
                reminder
                for reminder in self._load_private_reminders(secret)
                if reminder.source_id != found.id
            ]
            self._store_private_items(tasks=tasks, reminders=reminders, password=secret)
        return found

    def delete_private_task(self, identifier: str, password: str | None = None) -> TaskRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            tasks = self._load_private_tasks(secret)
            found: TaskRecord | None = None
            remaining: list[TaskRecord] = []
            wanted = identifier.strip().lower()
            for task in tasks:
                if found is None and (
                    task.id == wanted
                    or task.id.startswith(wanted)
                    or wanted == task.title.strip().lower()
                    or wanted in task.title.lower()
                ):
                    found = task
                    continue
                remaining.append(task)
            if found is None:
                return None
            reminders = [
                reminder
                for reminder in self._load_private_reminders(secret)
                if reminder.source_id != found.id
            ]
            self._store_private_items(tasks=remaining, reminders=reminders, password=secret)
        return found

    def append_note(self, text: str) -> None:
        self._append_markdown_entry(self.notes_dir / "inbox.md", text)

    def append_plan(self, text: str) -> None:
        self._append_markdown_entry(self.plans_dir / "plans.md", text)

    def append_preference(self, text: str) -> None:
        self._append_markdown_entry(self.preferences_dir / "preferences.md", text)

    def _append_markdown_entry(self, path: Path, text: str) -> None:
        self.bootstrap()
        with self.mutation_lock():
            now = now_utc().strftime("%Y-%m-%d %H:%M UTC")
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            updated = current.rstrip() + f"\n\n- [{now}] {text.strip()}\n"
            self._write_transaction({path: updated})

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    def _render_entry(self, record: NoteRecord | PlanRecord | ProjectRecord | PreferenceRecord) -> str:
        metadata = record.model_dump(mode="json")
        body = metadata.pop("body", "").strip()
        frontmatter = yaml.safe_dump(metadata, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\n{body}\n"

    def _load_note_from_file(self, path: Path) -> NoteRecord:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(text)
        metadata = yaml.safe_load(frontmatter) or {}
        metadata["body"] = body.strip()
        return NoteRecord.model_validate(metadata)

    def _unique_slug_id(self, directory: Path, title: str, fallback: str) -> str:
        base = _slugify(title, fallback)[:40]
        candidate = base
        suffix = 2
        existing = {path.stem for path in directory.glob("*.md")}
        while candidate in existing:
            candidate = f"{base[:34]}-{suffix}"
            suffix += 1
        return candidate

    def _note_id(self, title: str) -> str:
        return self._unique_slug_id(self.notes_dir, title, "note")

    def _build_notes_index(self, notes: list[NoteRecord]) -> dict[str, object]:
        return {
            "notes": [
                {"id": n.id, "title": n.title, "updated_at": n.updated_at.isoformat()}
                for n in sorted(notes, key=lambda item: item.created_at)
            ]
        }

    def list_notes(self) -> list[NoteRecord]:
        self.bootstrap()
        notes: list[NoteRecord] = []
        for path in sorted(self.notes_dir.glob("*.md")):
            if path.name == "inbox.md":
                continue
            try:
                notes.append(self._load_note_from_file(path))
            except Exception:
                pass
        notes.sort(key=lambda n: n.updated_at, reverse=True)
        return notes

    def create_note(self, title: str, body: str = "") -> NoteRecord:
        self.bootstrap()
        with self.mutation_lock():
            now = now_utc()
            note_id = self._note_id(title)
            note = NoteRecord(id=note_id, title=title.strip(), body=body.strip(), created_at=now, updated_at=now)
            existing = self.list_notes()
            self._write_transaction(
                {
                    self.notes_dir / f"{note_id}.md": self._render_entry(note),
                    self.notes_index_path: _json_dumps(self._build_notes_index(existing + [note])),
                }
            )
        return note

    def find_note(self, identifier: str) -> NoteRecord | None:
        wanted = identifier.strip().lower()
        notes = self.list_notes()
        for note in notes:
            if note.id == wanted:
                return note
        for note in notes:
            if note.title.strip().lower() == wanted:
                return note
        matches = [item for item in notes if wanted and item.id.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        for note in notes:
            if wanted in note.title.lower():
                return note
        return None

    def update_note(self, identifier: str, *, title: str | None = None, body: str | None = None) -> NoteRecord | None:
        with self.mutation_lock():
            note = self.find_note(identifier)
            if note is None:
                return None
            if title is not None:
                note.title = title.strip()
            if body is not None:
                note.body = body.strip()
            note.updated_at = now_utc()
            existing = [n for n in self.list_notes() if n.id != note.id]
            self._write_transaction(
                {
                    self.notes_dir / f"{note.id}.md": self._render_entry(note),
                    self.notes_index_path: _json_dumps(self._build_notes_index(existing + [note])),
                }
            )
        return note

    def delete_note(self, identifier: str) -> NoteRecord | None:
        with self.mutation_lock():
            note = self.find_note(identifier)
            if note is None:
                return None
            remaining = [n for n in self.list_notes() if n.id != note.id]
            self._write_transaction(
                {
                    self.notes_dir / f"{note.id}.md": None,
                    self.notes_index_path: _json_dumps(self._build_notes_index(remaining)),
                }
            )
        return note

    def delete_all_notes(self) -> int:
        with self.mutation_lock():
            notes = self.list_notes()
            if not notes:
                return 0
            payload: dict[Path, str | None] = {
                self.notes_index_path: _json_dumps(self._build_notes_index([])),
            }
            for note in notes:
                payload[self.notes_dir / f"{note.id}.md"] = None
            self._write_transaction(payload)
        return len(notes)

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    def _load_plan_from_file(self, path: Path) -> PlanRecord:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(text)
        metadata = yaml.safe_load(frontmatter) or {}
        metadata["body"] = body.strip()
        return PlanRecord.model_validate(metadata)

    def _plan_id(self, title: str) -> str:
        return self._unique_slug_id(self.plans_dir, title, "plan")

    def _build_plans_index(self, plans: list[PlanRecord]) -> dict[str, object]:
        return {
            "plans": [
                {"id": p.id, "title": p.title, "updated_at": p.updated_at.isoformat()}
                for p in sorted(plans, key=lambda item: item.created_at)
            ]
        }

    def list_plans(self) -> list[PlanRecord]:
        self.bootstrap()
        plans: list[PlanRecord] = []
        for path in sorted(self.plans_dir.glob("*.md")):
            if path.name == "plans.md":
                continue
            try:
                plans.append(self._load_plan_from_file(path))
            except Exception:
                pass
        plans.sort(key=lambda p: p.updated_at, reverse=True)
        return plans

    def create_plan(self, title: str, body: str = "") -> PlanRecord:
        self.bootstrap()
        with self.mutation_lock():
            now = now_utc()
            plan_id = self._plan_id(title)
            plan = PlanRecord(id=plan_id, title=title.strip(), body=body.strip(), created_at=now, updated_at=now)
            existing = self.list_plans()
            self._write_transaction(
                {
                    self.plans_dir / f"{plan_id}.md": self._render_entry(plan),
                    self.plans_index_path: _json_dumps(self._build_plans_index(existing + [plan])),
                }
            )
        return plan

    def find_plan(self, identifier: str) -> PlanRecord | None:
        wanted = identifier.strip().lower()
        plans = self.list_plans()
        for plan in plans:
            if plan.id == wanted:
                return plan
        for plan in plans:
            if plan.title.strip().lower() == wanted:
                return plan
        matches = [item for item in plans if wanted and item.id.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        for plan in plans:
            if wanted in plan.title.lower():
                return plan
        return None

    def update_plan(self, identifier: str, *, title: str | None = None, body: str | None = None) -> PlanRecord | None:
        with self.mutation_lock():
            plan = self.find_plan(identifier)
            if plan is None:
                return None
            if title is not None:
                plan.title = title.strip()
            if body is not None:
                plan.body = body.strip()
            plan.updated_at = now_utc()
            existing = [p for p in self.list_plans() if p.id != plan.id]
            self._write_transaction(
                {
                    self.plans_dir / f"{plan.id}.md": self._render_entry(plan),
                    self.plans_index_path: _json_dumps(self._build_plans_index(existing + [plan])),
                }
            )
        return plan

    def delete_plan(self, identifier: str) -> PlanRecord | None:
        with self.mutation_lock():
            plan = self.find_plan(identifier)
            if plan is None:
                return None
            remaining = [p for p in self.list_plans() if p.id != plan.id]
            self._write_transaction(
                {
                    self.plans_dir / f"{plan.id}.md": None,
                    self.plans_index_path: _json_dumps(self._build_plans_index(remaining)),
                }
            )
        return plan

    def delete_all_plans(self) -> int:
        with self.mutation_lock():
            plans = self.list_plans()
            if not plans:
                return 0
            payload: dict[Path, str | None] = {
                self.plans_index_path: _json_dumps(self._build_plans_index([])),
            }
            for plan in plans:
                payload[self.plans_dir / f"{plan.id}.md"] = None
            self._write_transaction(payload)
        return len(plans)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def append_project(self, text: str) -> None:
        self._append_markdown_entry(self.projects_dir / "projects.md", text)

    def _load_project_from_file(self, path: Path) -> ProjectRecord:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(text)
        metadata = yaml.safe_load(frontmatter) or {}
        metadata["body"] = body.strip()
        return ProjectRecord.model_validate(metadata)

    def _project_id(self, title: str) -> str:
        return self._unique_slug_id(self.projects_dir, title, "project")

    def _build_projects_index(self, projects: list[ProjectRecord]) -> dict[str, object]:
        return {
            "projects": [
                {"id": p.id, "title": p.title, "updated_at": p.updated_at.isoformat()}
                for p in sorted(projects, key=lambda item: item.created_at)
            ]
        }

    def list_projects(self) -> list[ProjectRecord]:
        self.bootstrap()
        projects: list[ProjectRecord] = []
        for path in sorted(self.projects_dir.glob("*.md")):
            if path.name == "projects.md":
                continue
            try:
                projects.append(self._load_project_from_file(path))
            except Exception:
                pass
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def create_project(self, title: str, body: str = "") -> ProjectRecord:
        self.bootstrap()
        with self.mutation_lock():
            now = now_utc()
            project_id = self._project_id(title)
            project = ProjectRecord(
                id=project_id,
                title=title.strip(),
                body=body.strip(),
                created_at=now,
                updated_at=now,
            )
            existing = self.list_projects()
            self._write_transaction(
                {
                    self.projects_dir / f"{project_id}.md": self._render_entry(project),
                    self.projects_index_path: _json_dumps(self._build_projects_index(existing + [project])),
                }
            )
        return project

    def find_project(self, identifier: str) -> ProjectRecord | None:
        wanted = identifier.strip().lower()
        projects = self.list_projects()
        for project in projects:
            if project.id == wanted:
                return project
        for project in projects:
            if project.title.strip().lower() == wanted:
                return project
        matches = [item for item in projects if wanted and item.id.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        for project in projects:
            if wanted in project.title.lower():
                return project
        return None

    def update_project(
        self, identifier: str, *, title: str | None = None, body: str | None = None
    ) -> ProjectRecord | None:
        with self.mutation_lock():
            project = self.find_project(identifier)
            if project is None:
                return None
            if title is not None:
                project.title = title.strip()
            if body is not None:
                project.body = body.strip()
            project.updated_at = now_utc()
            existing = [p for p in self.list_projects() if p.id != project.id]
            self._write_transaction(
                {
                    self.projects_dir / f"{project.id}.md": self._render_entry(project),
                    self.projects_index_path: _json_dumps(self._build_projects_index(existing + [project])),
                }
            )
        return project

    def delete_project(self, identifier: str) -> ProjectRecord | None:
        with self.mutation_lock():
            project = self.find_project(identifier)
            if project is None:
                return None
            remaining = [p for p in self.list_projects() if p.id != project.id]
            self._write_transaction(
                {
                    self.projects_dir / f"{project.id}.md": None,
                    self.projects_index_path: _json_dumps(self._build_projects_index(remaining)),
                }
            )
        return project

    def delete_all_projects(self) -> int:
        with self.mutation_lock():
            projects = self.list_projects()
            if not projects:
                return 0
            payload: dict[Path, str | None] = {
                self.projects_index_path: _json_dumps(self._build_projects_index([])),
            }
            for project in projects:
                payload[self.projects_dir / f"{project.id}.md"] = None
            self._write_transaction(payload)
        return len(projects)

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _load_preference_from_file(self, path: Path) -> PreferenceRecord:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(text)
        metadata = yaml.safe_load(frontmatter) or {}
        metadata["body"] = body.strip()
        return PreferenceRecord.model_validate(metadata)

    def _preference_id(self, title: str) -> str:
        return self._unique_slug_id(self.preferences_dir, title, "preference")

    def _build_preferences_index(self, preferences: list[PreferenceRecord]) -> dict[str, object]:
        return {
            "preferences": [
                {"id": p.id, "title": p.title, "updated_at": p.updated_at.isoformat()}
                for p in sorted(preferences, key=lambda item: item.created_at)
            ]
        }

    def list_preferences(self) -> list[PreferenceRecord]:
        self.bootstrap()
        prefs: list[PreferenceRecord] = []
        for path in sorted(self.preferences_dir.glob("*.md")):
            if path.name == "preferences.md":
                continue
            try:
                prefs.append(self._load_preference_from_file(path))
            except Exception:
                pass
        prefs.sort(key=lambda p: p.updated_at, reverse=True)
        return prefs

    def create_preference(self, title: str, body: str = "") -> PreferenceRecord:
        self.bootstrap()
        with self.mutation_lock():
            now = now_utc()
            pref_id = self._preference_id(title)
            pref = PreferenceRecord(id=pref_id, title=title.strip(), body=body.strip(), created_at=now, updated_at=now)
            existing = self.list_preferences()
            self._write_transaction(
                {
                    self.preferences_dir / f"{pref_id}.md": self._render_entry(pref),
                    self.preferences_index_path: _json_dumps(self._build_preferences_index(existing + [pref])),
                }
            )
        return pref

    def find_preference(self, identifier: str) -> PreferenceRecord | None:
        wanted = identifier.strip().lower()
        prefs = self.list_preferences()
        for pref in prefs:
            if pref.id == wanted:
                return pref
        for pref in prefs:
            if pref.title.strip().lower() == wanted:
                return pref
        matches = [item for item in prefs if wanted and item.id.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        for pref in prefs:
            if wanted in pref.title.lower():
                return pref
        return None

    def update_preference(
        self, identifier: str, *, title: str | None = None, body: str | None = None
    ) -> PreferenceRecord | None:
        with self.mutation_lock():
            pref = self.find_preference(identifier)
            if pref is None:
                return None
            if title is not None:
                pref.title = title.strip()
            if body is not None:
                pref.body = body.strip()
            pref.updated_at = now_utc()
            existing = [p for p in self.list_preferences() if p.id != pref.id]
            self._write_transaction(
                {
                    self.preferences_dir / f"{pref.id}.md": self._render_entry(pref),
                    self.preferences_index_path: _json_dumps(self._build_preferences_index(existing + [pref])),
                }
            )
        return pref

    def delete_preference(self, identifier: str) -> PreferenceRecord | None:
        with self.mutation_lock():
            pref = self.find_preference(identifier)
            if pref is None:
                return None
            remaining = [p for p in self.list_preferences() if p.id != pref.id]
            self._write_transaction(
                {
                    self.preferences_dir / f"{pref.id}.md": None,
                    self.preferences_index_path: _json_dumps(self._build_preferences_index(remaining)),
                }
            )
        return pref

    def delete_all_preferences(self) -> int:
        with self.mutation_lock():
            prefs = self.list_preferences()
            if not prefs:
                return 0
            payload: dict[Path, str | None] = {
                self.preferences_index_path: _json_dumps(self._build_preferences_index([])),
            }
            for pref in prefs:
                payload[self.preferences_dir / f"{pref.id}.md"] = None
            self._write_transaction(payload)
        return len(prefs)

    def _load_reminders(self) -> list[ReminderRecord]:
        payload = self._load_json(self.reminders_path, {"reminders": []})
        return [ReminderRecord.model_validate(item) for item in payload.get("reminders", [])]

    def list_reminders(self, *, include_inactive: bool = False) -> list[ReminderRecord]:
        reminders = self._load_reminders()
        if include_inactive:
            return reminders
        return [item for item in reminders if item.status == "scheduled"]

    @staticmethod
    def _find_reminder_in(identifier: str, reminders: list[ReminderRecord]) -> ReminderRecord | None:
        wanted = identifier.strip().lower()
        if not wanted:
            return None
        for matches in (
            [item for item in reminders if item.id == wanted],
            [item for item in reminders if item.text.strip().lower() == wanted],
            [item for item in reminders if item.id.startswith(wanted)],
            [item for item in reminders if wanted in item.text.lower()],
        ):
            if matches:
                return matches[0] if len(matches) == 1 else None
        return None

    def find_reminder(self, identifier: str) -> ReminderRecord | None:
        return self._find_reminder_in(identifier, self.list_reminders())

    def create_reminder(
        self,
        text: str,
        *,
        due_at: datetime,
        recurrence: str | None = None,
    ) -> ReminderRecord:
        self.bootstrap()
        with self.mutation_lock():
            now = now_utc()
            reminder = ReminderRecord(
                id=f"reminder-{uuid4().hex[:12]}",
                text=text.strip(),
                due_at=due_at,
                recurrence=recurrence,
                created_at=now,
                updated_at=now,
            )
            reminders = self._load_reminders()
            reminders.append(reminder)
            self._write_transaction(
                {
                    self.reminders_path: _json_dumps(
                        {"reminders": [item.model_dump(mode="json") for item in reminders]}
                    )
                }
            )
        return reminder

    def update_reminder(
        self,
        identifier: str,
        *,
        text: str | None = None,
        due_at: datetime | None | object = _UNSET,
        recurrence: str | None | object = _UNSET,
    ) -> ReminderRecord | None:
        with self.mutation_lock():
            reminders = self._load_reminders()
            target = self._find_reminder_in(identifier, reminders)
            if target is None:
                return None
            current = now_utc()
            found: ReminderRecord | None = None
            payload: dict[Path, str | None] = {}
            for reminder in reminders:
                if found is not None:
                    continue
                if reminder.id == target.id:
                    if text is not None:
                        reminder.text = text.strip()
                    if due_at is not _UNSET:
                        reminder.due_at = due_at
                        reminder.status = "scheduled"
                        reminder.last_sent_at = None
                        reminder.acked_at = None
                    if recurrence is not _UNSET:
                        reminder.recurrence = recurrence
                    reminder.updated_at = current
                    found = reminder
            if found is None:
                return None
            payload[self.reminders_path] = _json_dumps(
                {"reminders": [item.model_dump(mode="json") for item in reminders]}
            )
            if found.source_type == "task" and found.source_id:
                task = self.find_task(found.source_id)
                if task is not None:
                    task.updated_at = current
                    if text is not None:
                        task.title = found.text.removeprefix("Task reminder: ").strip() or task.title
                    if due_at is not _UNSET:
                        task.reminder_at = due_at
                    existing_tasks = [item for item in self.list_tasks(include_completed=True, kind=None) if item.id != task.id]
                    payload[self.tasks_dir / f"{task.id}.md"] = self._render_task(task)
                    payload[self.tasks_index_path] = _json_dumps(self._build_tasks_index(existing_tasks + [task]))
            self._write_transaction(payload)
        return found

    def cancel_reminder(self, identifier: str) -> ReminderRecord | None:
        with self.mutation_lock():
            reminders = self._load_reminders()
            target = self._find_reminder_in(identifier, reminders)
            if target is None:
                return None
            updated: list[ReminderRecord] = []
            found: ReminderRecord | None = None
            now = now_utc()
            for reminder in reminders:
                if found is None and reminder.id == target.id:
                    reminder.status = "cancelled"
                    reminder.updated_at = now
                    found = reminder
                updated.append(reminder)
            if found is None:
                return None
            self._write_transaction(
                {
                    self.reminders_path: _json_dumps(
                        {"reminders": [item.model_dump(mode="json") for item in updated]}
                    )
                }
            )
        return found

    def due_reminders(self, now: datetime | None = None) -> list[ReminderRecord]:
        current = now or now_utc()
        return [
            reminder
            for reminder in self.list_reminders()
            if reminder.status == "scheduled" and reminder.due_at <= current
        ]

    def mark_reminder_sent(self, reminder_id: str, *, now: datetime | None = None) -> ReminderRecord | None:
        with self.mutation_lock():
            current = now or now_utc()
            reminders = self._load_reminders()
            found: ReminderRecord | None = None
            for reminder in reminders:
                if reminder.id != reminder_id:
                    continue
                reminder.last_sent_at = current
                reminder.updated_at = current
                if reminder.recurrence:
                    reminder.due_at = self._advance_recurrence(reminder.due_at, reminder.recurrence)
                else:
                    # Mark as "sent" – awaiting user acknowledgement.
                    reminder.status = "sent"
                found = reminder
                break
            if found is None:
                return None
            self._write_transaction(
                {
                    self.reminders_path: _json_dumps(
                        {"reminders": [item.model_dump(mode="json") for item in reminders]}
                    )
                }
            )
        return found

    def ack_reminder(self, reminder_id: str) -> ReminderRecord | None:
        """Mark a reminder as acknowledged, completing it."""
        with self.mutation_lock():
            current = now_utc()
            reminders = self._load_reminders()
            found: ReminderRecord | None = None
            payload: dict[Path, str | None] = {}
            for reminder in reminders:
                if reminder.id != reminder_id:
                    continue
                reminder.acked_at = current
                reminder.status = "completed"
                reminder.updated_at = current
                found = reminder
                break
            if found is None:
                return None
            payload[self.reminders_path] = _json_dumps(
                {"reminders": [item.model_dump(mode="json") for item in reminders]}
            )
            if found.source_type == "task" and found.source_id:
                task = self.find_task(found.source_id)
                if task is not None:
                    task.reminder_at = None
                    task.updated_at = current
                    existing_tasks = [item for item in self.list_tasks(include_completed=True, kind=None) if item.id != task.id]
                    payload[self.tasks_dir / f"{task.id}.md"] = self._render_task(task)
                    payload[self.tasks_index_path] = _json_dumps(self._build_tasks_index(existing_tasks + [task]))
            self._write_transaction(payload)
        return found

    def snooze_reminder(self, reminder_id: str, *, due_at: datetime) -> ReminderRecord | None:
        with self.mutation_lock():
            current = now_utc()
            reminders = self._load_reminders()
            found: ReminderRecord | None = None
            payload: dict[Path, str | None] = {}
            for reminder in reminders:
                if reminder.id != reminder_id:
                    continue
                reminder.due_at = due_at
                reminder.status = "scheduled"
                reminder.last_sent_at = None
                reminder.acked_at = None
                reminder.updated_at = current
                found = reminder
                break
            if found is None:
                return None
            payload[self.reminders_path] = _json_dumps(
                {"reminders": [item.model_dump(mode="json") for item in reminders]}
            )
            if found.source_type == "task" and found.source_id:
                task = self.find_task(found.source_id)
                if task is not None:
                    task.reminder_at = due_at
                    task.updated_at = current
                    existing_tasks = [item for item in self.list_tasks(include_completed=True, kind=None) if item.id != task.id]
                    payload[self.tasks_dir / f"{task.id}.md"] = self._render_task(task)
                    payload[self.tasks_index_path] = _json_dumps(self._build_tasks_index(existing_tasks + [task]))
            self._write_transaction(payload)
        return found

    def cancel_all_reminders(self) -> int:
        """Cancel all active (scheduled + sent) reminders. Returns the count cancelled."""
        with self.mutation_lock():
            reminders = self._load_reminders()
            now = now_utc()
            count = 0
            for reminder in reminders:
                if reminder.status in {"scheduled", "sent"}:
                    reminder.status = "cancelled"
                    reminder.updated_at = now
                    count += 1
            self._write_transaction(
                {
                    self.reminders_path: _json_dumps(
                        {"reminders": [item.model_dump(mode="json") for item in reminders]}
                    )
                }
            )
        return count

    def list_sent_unacked_reminders(
        self, *, ack_timeout_minutes: int = 5, now: datetime | None = None
    ) -> list[ReminderRecord]:
        """Return non-recurring reminders that were sent but not yet acknowledged within the timeout."""
        current = now or now_utc()
        threshold = current - timedelta(minutes=ack_timeout_minutes)
        return [
            r
            for r in self._load_reminders()
            if r.status == "sent"
            and r.acked_at is None
            and r.last_sent_at is not None
            and r.last_sent_at <= threshold
        ]

    # ------------------------------------------------------------------
    # Private reminders (encrypted)
    # ------------------------------------------------------------------

    def _private_reminder_id(self) -> str:
        return f"private-reminder-{uuid4().hex[:12]}"

    def list_private_reminders(
        self,
        password: str | None = None,
        *,
        include_inactive: bool = False,
    ) -> list[ReminderRecord]:
        reminders = self._load_private_reminders(password)
        if include_inactive:
            return reminders
        return [item for item in reminders if item.status == "scheduled"]

    def find_private_reminder(self, identifier: str, password: str | None = None) -> ReminderRecord | None:
        wanted = identifier.strip().lower()
        reminders = self.list_private_reminders(password, include_inactive=True)
        for reminder in reminders:
            if reminder.id == wanted:
                return reminder
        for reminder in reminders:
            if reminder.text.strip().lower() == wanted:
                return reminder
        matches = [item for item in reminders if wanted and item.id.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        for reminder in reminders:
            if wanted in reminder.text.lower():
                return reminder
        return None

    def create_private_reminder(
        self,
        text: str,
        *,
        password: str | None = None,
        due_at: datetime,
        recurrence: str | None = None,
    ) -> ReminderRecord:
        self.bootstrap()
        with self.mutation_lock():
            secret = self._private_secret(password)
            now = now_utc()
            reminder = ReminderRecord(
                id=self._private_reminder_id(),
                text=text.strip(),
                due_at=due_at,
                recurrence=recurrence,
                created_at=now,
                updated_at=now,
            )
            reminders = self._load_private_reminders(secret)
            reminders.append(reminder)
            self._store_private_items(reminders=reminders, password=secret)
        return reminder

    def update_private_reminder(
        self,
        identifier: str,
        *,
        password: str | None = None,
        text: str | None = None,
        due_at: datetime | None | object = _UNSET,
        recurrence: str | None | object = _UNSET,
    ) -> ReminderRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            reminders = self._load_private_reminders(secret)
            target = self._find_reminder_in(identifier, reminders)
            if target is None:
                return None
            current = now_utc()
            found: ReminderRecord | None = None
            for reminder in reminders:
                if found is not None:
                    continue
                if reminder.id == target.id:
                    if text is not None:
                        reminder.text = text.strip()
                    if due_at is not _UNSET:
                        reminder.due_at = due_at
                        reminder.status = "scheduled"
                        reminder.last_sent_at = None
                        reminder.acked_at = None
                    if recurrence is not _UNSET:
                        reminder.recurrence = recurrence
                    reminder.updated_at = current
                    found = reminder
            if found is None:
                return None
            tasks: list[TaskRecord] | None = None
            if found.source_type == "private_task" and found.source_id:
                tasks = self._load_private_tasks(secret)
                for task in tasks:
                    if task.id != found.source_id:
                        continue
                    task.updated_at = current
                    if text is not None:
                        task.title = found.text.removeprefix("Task reminder: ").strip() or task.title
                    if due_at is not _UNSET:
                        task.reminder_at = due_at
                    break
            self._store_private_items(tasks=tasks, reminders=reminders, password=secret)
        return found

    def cancel_private_reminder(self, identifier: str, password: str | None = None) -> ReminderRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            reminders = self._load_private_reminders(secret)
            target = self._find_reminder_in(identifier, reminders)
            if target is None:
                return None
            found: ReminderRecord | None = None
            now = now_utc()
            for reminder in reminders:
                if found is None and reminder.id == target.id:
                    reminder.status = "cancelled"
                    reminder.updated_at = now
                    found = reminder
            if found is None:
                return None
            self._store_private_items(reminders=reminders, password=secret)
        return found

    def due_private_reminders(self, now: datetime | None = None) -> list[ReminderRecord]:
        current = now or now_utc()
        return [
            reminder
            for reminder in self.list_private_reminders()
            if reminder.status == "scheduled" and reminder.due_at <= current
        ]

    def mark_private_reminder_sent(
        self,
        reminder_id: str,
        *,
        password: str | None = None,
        now: datetime | None = None,
    ) -> ReminderRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            current = now or now_utc()
            reminders = self._load_private_reminders(secret)
            found: ReminderRecord | None = None
            for reminder in reminders:
                if reminder.id != reminder_id:
                    continue
                reminder.last_sent_at = current
                reminder.updated_at = current
                if reminder.recurrence:
                    reminder.due_at = self._advance_recurrence(reminder.due_at, reminder.recurrence)
                else:
                    reminder.status = "sent"
                found = reminder
                break
            if found is None:
                return None
            self._store_private_items(reminders=reminders, password=secret)
        return found

    def ack_private_reminder(self, reminder_id: str, password: str | None = None) -> ReminderRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            current = now_utc()
            reminders = self._load_private_reminders(secret)
            found: ReminderRecord | None = None
            for reminder in reminders:
                if reminder.id != reminder_id:
                    continue
                reminder.acked_at = current
                reminder.status = "completed"
                reminder.updated_at = current
                found = reminder
                break
            if found is None:
                return None
            tasks: list[TaskRecord] | None = None
            if found.source_type == "private_task" and found.source_id:
                tasks = self._load_private_tasks(secret)
                for task in tasks:
                    if task.id == found.source_id:
                        task.reminder_at = None
                        task.updated_at = current
                        break
            self._store_private_items(tasks=tasks, reminders=reminders, password=secret)
        return found

    def snooze_private_reminder(
        self,
        reminder_id: str,
        *,
        password: str | None = None,
        due_at: datetime,
    ) -> ReminderRecord | None:
        with self.mutation_lock():
            secret = self._private_secret(password)
            current = now_utc()
            reminders = self._load_private_reminders(secret)
            found: ReminderRecord | None = None
            for reminder in reminders:
                if reminder.id != reminder_id:
                    continue
                reminder.due_at = due_at
                reminder.status = "scheduled"
                reminder.last_sent_at = None
                reminder.acked_at = None
                reminder.updated_at = current
                found = reminder
                break
            if found is None:
                return None
            tasks: list[TaskRecord] | None = None
            if found.source_type == "private_task" and found.source_id:
                tasks = self._load_private_tasks(secret)
                for task in tasks:
                    if task.id == found.source_id:
                        task.reminder_at = due_at
                        task.updated_at = current
                        break
            self._store_private_items(tasks=tasks, reminders=reminders, password=secret)
        return found

    def list_private_sent_unacked_reminders(
        self,
        *,
        password: str | None = None,
        ack_timeout_minutes: int = 5,
        now: datetime | None = None,
    ) -> list[ReminderRecord]:
        current = now or now_utc()
        threshold = current - timedelta(minutes=ack_timeout_minutes)
        return [
            reminder
            for reminder in self.list_private_reminders(password, include_inactive=True)
            if reminder.status == "sent"
            and reminder.acked_at is None
            and reminder.last_sent_at is not None
            and reminder.last_sent_at <= threshold
        ]

    def _advance_recurrence(self, due_at: datetime, recurrence: str) -> datetime:
        normalized = recurrence.strip().lower()
        if normalized == "daily":
            return due_at + timedelta(days=1)
        if normalized == "weekly":
            return due_at + timedelta(days=7)
        if normalized == "monthly":
            return due_at + timedelta(days=30)
        return due_at + timedelta(days=1)

    def get_summary(self, chat_id: str) -> str:
        payload = self._load_json(self.conversation_path, {"summaries": {}})
        summary = payload.get("summaries", {}).get(str(chat_id))
        if not summary:
            return ""
        return ConversationSummary.model_validate(summary).summary

    def save_summary(self, chat_id: str, summary: str) -> None:
        self.bootstrap()
        with self.mutation_lock():
            payload = self._load_json(self.conversation_path, {"summaries": {}})
            payload.setdefault("summaries", {})[str(chat_id)] = ConversationSummary(
                chat_id=str(chat_id),
                summary=summary.strip(),
                updated_at=now_utc(),
            ).model_dump(mode="json")
            self._write_transaction({self.conversation_path: _json_dumps(payload)})

    def get_telegram_state(self) -> TelegramState:
        payload = self._load_json(
            self.telegram_state_path,
            {
                "processed_update_ids": [],
                "last_message_by_chat": {},
                "pending_by_chat": {},
                "undo_by_chat": {},
                "references_by_chat": {},
                "ai_runs_by_chat": {},
            },
        )
        return TelegramState.model_validate(payload)

    def list_openai_traces(self, *, limit: int = 10) -> list[OpenAITraceRecord]:
        payload = self._load_json(self.openai_history_path, {"calls": []})
        items = payload.get("calls", []) if isinstance(payload, dict) else []
        traces = [
            OpenAITraceRecord.model_validate(item)
            for item in items
            if isinstance(item, dict)
        ]
        traces.sort(key=lambda item: item.created_at, reverse=True)
        return traces[: max(limit, 0)]

    def append_openai_trace(self, trace: OpenAITraceRecord, *, max_items: int = 50) -> None:
        self.bootstrap()
        with self.mutation_lock():
            payload = self._load_json(self.openai_history_path, {"calls": []})
            items = payload.get("calls", []) if isinstance(payload, dict) else []
            items.append(trace.model_dump(mode="json"))
            payload = {"calls": items[-max_items:]}
            self._write_transaction({self.openai_history_path: _json_dumps(payload)})

    def append_automation_idea(self, source_request: str, idea: dict[str, Any]) -> None:
        self.bootstrap()
        with self.mutation_lock():
            timestamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")
            title = str(idea.get("title") or "Potential deterministic feature").strip()
            why = str(idea.get("why") or "").strip()
            example_request = str(idea.get("example_request") or "").strip()
            suggested_command = str(idea.get("suggested_command") or "").strip()
            suggested_code_path = str(idea.get("suggested_code_path") or "").strip()
            entry_lines = [
                "",
                f"## {timestamp} - {title}",
                "",
                f"- Source request: {source_request.strip()}",
            ]
            if why:
                entry_lines.append(f"- Why: {why}")
            if example_request:
                entry_lines.append(f"- Example request: {example_request}")
            if suggested_command:
                entry_lines.append(f"- Suggested command: {suggested_command}")
            if suggested_code_path:
                entry_lines.append(f"- Suggested code path: {suggested_code_path}")
            entry = "\n".join(entry_lines).strip()
            note_title = "AI automation backlog"
            existing_note = self.find_note(note_title)
            payload: dict[Path, str | None] = {}
            if existing_note is None:
                now = now_utc()
                note_id = self._note_id(note_title)
                note = NoteRecord(
                    id=note_id,
                    title=note_title,
                    body=entry,
                    created_at=now,
                    updated_at=now,
                )
                existing_notes = self.list_notes()
                payload[self.notes_dir / f"{note.id}.md"] = self._render_entry(note)
                payload[self.notes_index_path] = _json_dumps(self._build_notes_index(existing_notes + [note]))
            else:
                existing_note.body = existing_note.body.rstrip() + "\n\n" + entry
                existing_note.updated_at = now_utc()
                existing_notes = [note for note in self.list_notes() if note.id != existing_note.id]
                payload[self.notes_dir / f"{existing_note.id}.md"] = self._render_entry(existing_note)
                payload[self.notes_index_path] = _json_dumps(self._build_notes_index(existing_notes + [existing_note]))
            self._write_transaction(payload)

    def read_automation_ideas(self) -> str:
        self.bootstrap()
        note = self.find_note("AI automation backlog")
        if note is not None and note.body.strip():
            return f"# {note.title}\n\n{note.body.strip()}\n"
        if not self.automation_ideas_path.exists():
            return ""
        return self.automation_ideas_path.read_text(encoding="utf-8")

    def has_processed_update(self, update_id: int) -> bool:
        state = self.get_telegram_state()
        return update_id in state.processed_update_ids

    def mark_update_processed(self, update_id: int, chat_id: str) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            state.processed_update_ids.append(update_id)
            state.processed_update_ids = state.processed_update_ids[-500:]
            state.last_message_by_chat[str(chat_id)] = now_utc()
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})

    def build_path_undo_action(self, label: str, paths: list[Path]) -> dict[str, Any]:
        snapshots: list[dict[str, Any]] = []
        for path in sorted({path.resolve() for path in paths}, key=str):
            if path.exists():
                snapshots.append(
                    {
                        "path": str(path),
                        "exists": True,
                        "content_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
                    }
                )
            else:
                snapshots.append({"path": str(path), "exists": False, "content_b64": ""})
        return {
            "kind": "path_restore",
            "label": label.strip(),
            "snapshots": snapshots,
        }

    def get_undo_action(self, chat_id: str) -> dict[str, Any] | None:
        state = self.get_telegram_state()
        payload = state.undo_by_chat.get(str(chat_id))
        if isinstance(payload, dict):
            return payload
        return None

    def set_undo_action(self, chat_id: str, action: dict[str, Any]) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            state.undo_by_chat[str(chat_id)] = action
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})

    def clear_undo_action(self, chat_id: str) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            state.undo_by_chat.pop(str(chat_id), None)
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})

    def get_reference_context(self, chat_id: str) -> dict[str, Any] | None:
        state = self.get_telegram_state()
        payload = state.references_by_chat.get(str(chat_id))
        if isinstance(payload, dict):
            return payload
        return None

    def set_reference_context(self, chat_id: str, context: dict[str, Any]) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            context = dict(context)
            context["updated_at"] = now_utc().isoformat()
            state.references_by_chat[str(chat_id)] = context
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})

    def clear_reference_context(self, chat_id: str) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            state.references_by_chat.pop(str(chat_id), None)
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})

    def get_ai_run(self, chat_id: str) -> dict[str, Any] | None:
        state = self.get_telegram_state()
        payload = state.ai_runs_by_chat.get(str(chat_id))
        if isinstance(payload, dict):
            return payload
        return None

    def set_ai_run(self, chat_id: str, payload: dict[str, Any]) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            record = dict(payload)
            record["updated_at"] = now_utc().isoformat()
            state.ai_runs_by_chat[str(chat_id)] = record
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})

    def apply_path_undo_action(self, action: dict[str, Any]) -> str:
        label = str(action.get("label", "")).strip() or "last action"
        snapshots = action.get("snapshots")
        if not isinstance(snapshots, list):
            raise ValueError("Undo snapshot is invalid.")

        payload: dict[Path, bytes | None] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            path_text = str(snapshot.get("path", "")).strip()
            if not path_text:
                continue
            target = Path(path_text)
            if snapshot.get("exists"):
                content_b64 = str(snapshot.get("content_b64", ""))
                payload[target] = base64.b64decode(content_b64.encode("ascii"))
            else:
                payload[target] = None

        if not payload:
            raise ValueError("Undo snapshot is empty.")
        with self.lock_paths(list(payload.keys())):
            for path, content in payload.items():
                if content is None:
                    if path.exists():
                        path.unlink()
                    continue
                _write_bytes_file(path, content)
        return label

    def remember_calendar_event(self, source_id: str, event_id: str) -> None:
        with self.mutation_lock():
            payload = self._load_json(self.calendar_links_path, {"links": {}})
            payload.setdefault("links", {})[str(source_id)] = event_id
            self._write_transaction({self.calendar_links_path: _json_dumps(payload)})

    def get_calendar_links(self) -> dict[str, str]:
        payload = self._load_json(self.calendar_links_path, {"links": {}})
        return dict(payload.get("links", {}))

    def rebuild_indexes(self) -> None:
        with self.mutation_lock():
            tasks = self.list_tasks(include_completed=True, kind=None)
            reminders = [item for item in self._load_reminders() if item.source_type == "standalone"]
            for task in tasks:
                if task.status == "open" and task.reminder_at:
                    reminders.append(
                        ReminderRecord(
                            id=f"task-{task.id}",
                            text=f"Task reminder: {task.title}",
                            due_at=task.reminder_at,
                            source_type="task",
                            source_id=task.id,
                            created_at=task.created_at,
                            updated_at=task.updated_at,
                        )
                    )
            self._write_transaction(
                {
                    self.tasks_index_path: _json_dumps(self._build_tasks_index(tasks)),
                    self.reminders_path: _json_dumps(
                        {"reminders": [item.model_dump(mode="json") for item in reminders]}
                    ),
                }
            )

    def _build_tasks_index(self, tasks: list[TaskRecord]) -> dict[str, object]:
        summaries = [
            TaskSummary(
                id=task.id,
                title=task.title,
                tags=task.tags,
                kind=task.kind,
                status=task.status,
                priority=task.priority,
                is_current=task.is_current,
                due_at=task.due_at,
                reminder_at=task.reminder_at,
                calendar_event_id=task.calendar_event_id,
                updated_at=task.updated_at,
            ).model_dump(mode="json")
            for task in sorted(tasks, key=lambda item: item.created_at)
        ]
        return {"tasks": summaries}

    def _task_id_from_title(self, title: str) -> str:
        return self._unique_slug_id(self.tasks_dir, title, "task")

    # ------------------------------------------------------------------
    # People (encrypted)
    # ------------------------------------------------------------------

    def _derive_fernet_key(self, password: str) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_PEOPLE_KDF_SALT,
            iterations=480_000,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode()))

    def _fernet(self, password: str) -> Fernet:
        return Fernet(self._derive_fernet_key(password))

    def people_password_valid(self, password: str) -> bool:
        key = self.settings.people_encryption_key
        if not key:
            return False
        return hmac.compare_digest(password.strip(), key)

    def private_password_valid(self, password: str) -> bool:
        return self.people_password_valid(password)

    def get_people(self, password: str) -> str:
        """Decrypt and return people file content.

        Returns an empty string when no people data exists yet.
        Raises ``ValueError`` when decryption fails.
        """
        if not self.people_enc_path.exists():
            return ""
        try:
            encrypted = self.people_enc_path.read_bytes()
            return self._fernet(password).decrypt(encrypted).decode()
        except (InvalidToken, UnicodeDecodeError):
            raise ValueError("People data could not be decrypted.")

    @staticmethod
    def format_people(content: str) -> str:
        sections = Storage._parse_people_sections(content)
        if not sections:
            return "No people entries yet."
        lines = ["People:"]
        for person_index, section in enumerate(sections, 1):
            lines.append(f"{person_index}. {section['name']}")
            for entry_index, entry in enumerate(section["entries"], 1):
                lines.append(f"   {entry_index}. {entry}")
        lines.append("")
        lines.append("Use: edit person <person> entry <#>: <new text>")
        lines.append("Use: delete person <person> or delete person <person> entry <#>")
        return "\n".join(lines)

    @staticmethod
    def _parse_people_sections(content: str) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in content.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped or stripped in {"# People", "People worth remembering.", "No entries yet."}:
                continue
            if stripped.startswith("## "):
                current = {"name": stripped[3:].strip(), "entries": []}
                sections.append(current)
                continue
            if current is None:
                continue
            if stripped.startswith("- "):
                current["entries"].append(stripped[2:].strip())
        return sections

    @staticmethod
    def _render_people_sections(sections: list[dict[str, Any]]) -> str:
        if not sections:
            return "# People\n\nPeople worth remembering.\n"
        parts = ["# People", "", "People worth remembering."]
        for section in sections:
            parts.extend(["", f"## {section['name']}"])
            parts.extend(f"- {entry}" for entry in section["entries"])
        return "\n".join(parts).rstrip() + "\n"

    @staticmethod
    def _resolve_people_section(
        sections: list[dict[str, Any]], person_identifier: str
    ) -> dict[str, Any] | None:
        wanted = person_identifier.strip()
        if not wanted:
            return None
        if wanted.isdigit():
            index = int(wanted) - 1
            if 0 <= index < len(sections):
                return sections[index]
        lowered = wanted.lower()
        for section in sections:
            if section["name"].strip().lower() == lowered:
                return section
        for section in sections:
            if lowered in section["name"].strip().lower():
                return section
        return None

    @staticmethod
    def _insert_into_person_section(content: str, name: str, bullet: str) -> str:
        """Append *bullet* under an existing ``## <name>`` section.

        If no such section exists a new one is created at the end of the file.
        The comparison is case-insensitive so "John Doe" and "john doe" share
        the same section.
        """
        heading = f"## {name.strip()}"
        pattern = re.compile(
            rf"^{re.escape(heading)}\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        match = pattern.search(content)
        if match is None:
            return content.rstrip() + f"\n\n{heading}\n{bullet}\n"

        section_start = match.end()
        # Detect the next ## heading (any whitespace after ##, but not ### or deeper).
        next_heading = re.search(r"^##(?!#)\s*\S", content[section_start:], re.MULTILINE)
        if next_heading:
            insert_pos = section_start + next_heading.start()
            return content[:insert_pos].rstrip() + f"\n{bullet}\n\n" + content[insert_pos:]
        return content.rstrip() + f"\n{bullet}\n"

    def append_person(self, name: str, info: str, password: str) -> None:
        self.bootstrap()
        with self.mutation_lock():
            current = self.get_people(password)
            if not current:
                current = "# People\n\nPeople worth remembering.\n"
            now = now_utc().strftime("%Y-%m-%d %H:%M UTC")
            bullet = f"- [{now}] {info.strip()}"
            updated = self._insert_into_person_section(current, name.strip(), bullet)
            encrypted = self._fernet(password).encrypt(updated.encode())
            self.people_enc_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", dir=self.people_enc_path.parent, delete=False
            ) as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            os.replace(temp_name, self.people_enc_path)

    def update_person_entry(self, person_identifier: str, entry_index: int, info: str, password: str) -> str | None:
        self.bootstrap()
        with self.mutation_lock():
            sections = self._parse_people_sections(self.get_people(password))
            section = self._resolve_people_section(sections, person_identifier)
            if section is None:
                return None
            index = entry_index - 1
            if index < 0 or index >= len(section["entries"]):
                return None
            now = now_utc().strftime("%Y-%m-%d %H:%M UTC")
            section["entries"][index] = f"[{now}] {info.strip()}"
            self._store_people_sections(sections, password)
            return section["name"]

    def delete_person(self, person_identifier: str, password: str, *, entry_index: int | None = None) -> str | None:
        self.bootstrap()
        with self.mutation_lock():
            sections = self._parse_people_sections(self.get_people(password))
            section = self._resolve_people_section(sections, person_identifier)
            if section is None:
                return None
            if entry_index is None:
                sections = [item for item in sections if item["name"] != section["name"]]
            else:
                index = entry_index - 1
                if index < 0 or index >= len(section["entries"]):
                    return None
                del section["entries"][index]
                if not section["entries"]:
                    sections = [item for item in sections if item["name"] != section["name"]]
            self._store_people_sections(sections, password)
            return section["name"]

    def _store_people_sections(self, sections: list[dict[str, Any]], password: str) -> None:
        rendered = self._render_people_sections(sections)
        encrypted = self._fernet(password).encrypt(rendered.encode())
        self.people_enc_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", dir=self.people_enc_path.parent, delete=False
        ) as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, self.people_enc_path)

    # ------------------------------------------------------------------
    # User settings (persisted per-deployment overrides)
    # ------------------------------------------------------------------

    def get_user_settings(self) -> UserSettings:
        payload = self._load_json(self.user_settings_path, {})
        defaults = {
            "reminder_ack_timeout_minutes": self.settings.reminder_ack_timeout_minutes,
            "reminder_poll_seconds": self.settings.reminder_poll_seconds,
        }
        defaults.update(payload)
        return UserSettings.model_validate(defaults)

    def save_user_settings(self, settings: UserSettings) -> None:
        self.bootstrap()
        with self.mutation_lock():
            self._write_transaction(
                {self.user_settings_path: _json_dumps(settings.model_dump(mode="json"))}
            )

    # ------------------------------------------------------------------
    # Pending action state (per chat)
    # ------------------------------------------------------------------

    def get_pending(self, chat_id: str) -> dict[str, Any] | None:
        state = self.get_telegram_state()
        return state.pending_by_chat.get(str(chat_id))

    def set_pending(self, chat_id: str, action: dict[str, Any]) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            state.pending_by_chat[str(chat_id)] = action
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})

    def clear_pending(self, chat_id: str) -> None:
        self.bootstrap()
        with self.mutation_lock():
            state = self.get_telegram_state()
            state.pending_by_chat.pop(str(chat_id), None)
            self._write_transaction({self.telegram_state_path: _json_dumps(state.model_dump(mode="json"))})
