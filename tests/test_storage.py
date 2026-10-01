from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest

from personal_assistant.config import Settings
from personal_assistant.storage import Storage, _json_dumps
from personal_assistant.time_utils import now_utc


def test_task_create_complete_and_indexes(storage):
    reminder_at = now_utc() + timedelta(hours=1)
    task = storage.create_task("Write the weekly update", reminder_at=reminder_at)

    task_file = storage.tasks_dir / f"{task.id}.md"
    assert task_file.exists()

    tasks_index = json.loads(storage.tasks_index_path.read_text(encoding="utf-8"))
    assert tasks_index["tasks"][0]["title"] == "Write the weekly update"

    reminders = storage.list_reminders()
    assert reminders[0].source_id == task.id

    completed = storage.complete_task(task.id)
    assert completed is not None
    assert completed.status == "completed"
    assert storage.list_reminders() == []


def test_project_create_update_delete_and_indexes(storage):
    project = storage.create_project("Long term research", "Collect papers and ideas.")

    project_file = storage.projects_dir / f"{project.id}.md"
    assert project_file.exists()
    assert storage.find_project("research") is not None

    updated = storage.update_project(project.id, body="Draft the proposal.")
    assert updated is not None
    assert updated.body == "Draft the proposal."

    projects_index = json.loads(storage.projects_index_path.read_text(encoding="utf-8"))
    assert projects_index["projects"][0]["title"] == "Long term research"

    deleted = storage.delete_project(project.id)
    assert deleted is not None
    assert storage.list_projects() == []


def test_append_automation_idea_writes_notes_backlog(storage):
    storage.append_automation_idea(
        "complete the report-related task",
        {
            "title": "Semantic task selection command",
            "why": "AI searched tasks semantically.",
            "example_request": "complete the report-related task",
            "suggested_command": "complete tasks about <topic>",
            "suggested_code_path": "semantic_parser.py + assistant task filters",
        },
    )

    note = storage.find_note("AI automation backlog")
    assert note is not None
    assert "Semantic task selection command" in note.body
    assert "complete the report-related task" in note.body
    assert "Semantic task selection command" in storage.read_automation_ideas()


def test_rebuild_indexes_recovers_manual_task_edit(storage):
    task = storage.create_task("Original title")
    task_path = storage.tasks_dir / f"{task.id}.md"
    updated = task_path.read_text(encoding="utf-8").replace("Original title", "Edited by hand")
    task_path.write_text(updated, encoding="utf-8")

    storage.rebuild_indexes()

    tasks_index = json.loads(storage.tasks_index_path.read_text(encoding="utf-8"))
    assert tasks_index["tasks"][0]["title"] == "Edited by hand"


def test_transaction_recovery_finishes_commit(settings):
    storage = Storage(settings)
    storage.bootstrap()
    target = storage.notes_dir / "inbox.md"
    target.write_text("# Inbox\n\nold\n", encoding="utf-8")

    tx_dir = storage.transactions_dir / "dangling"
    (tx_dir / "new").mkdir(parents=True, exist_ok=True)
    (tx_dir / "apply").mkdir(parents=True, exist_ok=True)
    (tx_dir / "new" / "0.txt").write_text("# Inbox\n\nrecovered\n", encoding="utf-8")
    journal = {
        "phase": "commit",
        "operations": [
            {
                "target": str(target),
                "new_rel": "new/0.txt",
                "backup_rel": None,
            }
        ],
    }
    (tx_dir / "journal.json").write_text(_json_dumps(journal), encoding="utf-8")

    storage.recover_transactions()

    assert "recovered" in target.read_text(encoding="utf-8")
    assert not tx_dir.exists()


def test_transaction_recovery_leaves_inflight_directory_without_journal(settings):
    storage = Storage(settings)
    storage.bootstrap()

    tx_dir = storage.transactions_dir / "inflight"
    (tx_dir / "new").mkdir(parents=True, exist_ok=True)
    (tx_dir / "backups").mkdir(parents=True, exist_ok=True)
    (tx_dir / "new" / "0.txt").write_text("partial", encoding="utf-8")

    storage.recover_transactions()

    assert tx_dir.exists()


def test_transaction_commit_works_when_runtime_and_state_are_on_different_mount_roots(tmp_path):
    repo_root = tmp_path / "app"
    state_root = tmp_path / "state-root"
    runtime_root = tmp_path / "runtime-root"
    repo_root.mkdir()
    state_root.mkdir()
    runtime_root.mkdir()

    storage = Storage(
        Settings(
            _env_file=None,
            repo_root=repo_root,
            state_dir=state_root,
            runtime_dir=runtime_root,
        )
    )
    storage.bootstrap()

    target = storage.state_dir / "indexes" / "tasks.json"
    storage._write_transaction({target: _json_dumps({"tasks": [{"id": "a", "title": "hello"}]})})

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["tasks"][0]["title"] == "hello"


def test_corrupt_telegram_state_json_is_quarantined_and_reset(storage):
    storage.bootstrap()
    storage.telegram_state_path.write_text('{"processed_update_ids": [1],\n', encoding="utf-8")

    state = storage.get_telegram_state()

    assert state.processed_update_ids == []
    quarantined = storage.list_quarantined_files()
    assert quarantined
    assert quarantined[0]["name"].startswith("state.json.")


def test_run_maintenance_prunes_old_quarantine_files(storage):
    storage.bootstrap()
    stale = storage.quarantine_dir / "old.json.corrupt"
    stale.write_text("bad", encoding="utf-8")
    old_time = (now_utc() - timedelta(days=30)).timestamp()
    os.utime(stale, (old_time, old_time))

    result = storage.run_maintenance()

    assert result["pruned_quarantine"] >= 1
    assert not stale.exists()


def test_concurrent_task_creates_leave_valid_index(storage):
    titles = [f"Task {index}" for index in range(5)]

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(storage.create_task, titles))

    tasks = storage.list_tasks()
    assert len(tasks) == 5
    tasks_index = json.loads(storage.tasks_index_path.read_text(encoding="utf-8"))
    assert len(tasks_index["tasks"]) == 5


def test_task_create_with_tags_and_update(storage):
    task = storage.create_task("Fix bug", tags=["dev"])
    assert task.tags == ["dev"]

    updated = storage.update_task(task.id, title="Fix critical bug", tags=["dev", "urgent"])
    assert updated is not None
    assert updated.title == "Fix critical bug"
    assert "urgent" in updated.tags

    reloaded = storage.find_task(task.id)
    assert reloaded.title == "Fix critical bug"
    assert "urgent" in reloaded.tags


def test_other_tasks_are_filterable_from_self_tasks(storage):
    own = storage.create_task("Write report")
    other = storage.create_task("Zhenkai sends slides", kind="other")

    assert [task.id for task in storage.list_tasks()] == [own.id]
    assert [task.id for task in storage.list_tasks(kind="other")] == [other.id]
    assert {task.id for task in storage.list_tasks(kind=None)} == {own.id, other.id}


def test_retag_tasks_updates_matching_tasks(storage):
    first = storage.create_task("Lecture prep", tags=["project-alpha", "teaching"])
    second = storage.create_task("Past task", tags=["PROJECT-ALPHA"])
    storage.complete_task(second.id)
    untouched = storage.create_task("Other", tags=["admin"])

    updated = storage.retag_tasks("project-alpha", "alpha")

    assert {task.id for task in updated} == {first.id, second.id}
    assert storage.find_task(first.id).tags == ["alpha", "teaching"]
    assert storage.find_task(second.id).tags == ["alpha"]
    assert storage.find_task(untouched.id).tags == ["admin"]


def test_task_title_update_refreshes_linked_reminder_text(storage):
    reminder_at = now_utc() + timedelta(hours=1)
    task = storage.create_task("Book dentist", reminder_at=reminder_at)

    updated = storage.update_task(task.id, title="Book dentist appointment")

    assert updated is not None
    reminders = storage.list_reminders(include_inactive=True)
    assert reminders[0].text == "Task reminder: Book dentist appointment"


def test_acking_task_reminder_clears_task_reminder_at_and_survives_rebuild(storage):
    reminder_at = now_utc() + timedelta(hours=1)
    task = storage.create_task("Pay rent", reminder_at=reminder_at)
    reminder = storage.list_reminders(include_inactive=True)[0]

    acked = storage.ack_reminder(reminder.id)

    assert acked is not None
    reloaded_task = storage.find_task(task.id)
    assert reloaded_task is not None
    assert reloaded_task.reminder_at is None

    storage.rebuild_indexes()
    reminders = storage.list_reminders(include_inactive=True)
    assert not any(item.source_id == task.id and item.status == "scheduled" for item in reminders)


def test_duplicate_titles_get_unique_slug_ids(storage):
    first_task = storage.create_task("Buy milk")
    second_task = storage.create_task("Buy milk")
    first_note = storage.create_note("Project alpha")
    second_note = storage.create_note("Project alpha")

    assert first_task.id == "buy-milk"
    assert second_task.id == "buy-milk-2"
    assert first_note.id == "project-alpha"
    assert second_note.id == "project-alpha-2"


def test_note_crud(storage):
    note = storage.create_note("Project Alpha", "Key milestones are Q1 and Q2.")
    assert note.id == "project-alpha"
    assert note.title == "Project Alpha"
    assert note.body == "Key milestones are Q1 and Q2."

    notes = storage.list_notes()
    assert len(notes) == 1
    assert notes[0].id == note.id

    found = storage.find_note(note.id)
    assert found is not None
    assert found.title == "Project Alpha"

    updated = storage.update_note(note.id, title="Project Beta", body="New description.")
    assert updated is not None
    assert updated.title == "Project Beta"

    reloaded = storage.find_note(note.id)
    assert reloaded.title == "Project Beta"
    assert reloaded.body == "New description."


def test_plan_crud(storage):
    plan = storage.create_plan("Learn Rust", "Complete the book.")
    assert plan.id == "learn-rust"

    plans = storage.list_plans()
    assert len(plans) == 1

    updated = storage.update_plan(plan.id, title="Learn Go")
    assert updated is not None
    assert updated.title == "Learn Go"

    reloaded = storage.find_plan(plan.id)
    assert reloaded.title == "Learn Go"


def test_preference_crud(storage):
    pref = storage.create_preference("Dark mode", "Always on.")
    assert pref.id == "dark-mode"

    prefs = storage.list_preferences()
    assert len(prefs) == 1

    updated = storage.update_preference(pref.id, body="Only at night.")
    assert updated is not None
    assert updated.body == "Only at night."

    reloaded = storage.find_preference(pref.id)
    assert reloaded.body == "Only at night."


def test_corrupt_people_file_raises_instead_of_looking_empty(storage, settings):
    settings.people_encryption_key = "secret123"
    storage.bootstrap()
    storage.people_enc_path.write_bytes(b"not-valid-fernet")

    with pytest.raises(ValueError):
        storage.get_people("secret123")


def test_private_task_and_private_reminder_crud(storage, settings):
    settings.people_encryption_key = "secret123"
    reminder_at = now_utc() + timedelta(hours=2)

    task = storage.create_private_task(
        "Renew passport",
        password="secret123",
        priority="high",
        reminder_at=reminder_at,
    )

    tasks = storage.list_private_tasks("secret123")
    assert len(tasks) == 1
    assert tasks[0].title == "Renew passport"
    assert tasks[0].priority == "high"

    reminders = storage.list_private_reminders("secret123", include_inactive=True)
    assert len(reminders) == 1
    assert reminders[0].source_id == task.id
    assert reminders[0].source_type == "private_task"

    updated = storage.update_private_task(task.id, password="secret123", priority="low")
    assert updated is not None
    assert updated.priority == "low"

    snoozed = storage.snooze_private_reminder(reminders[0].id, password="secret123", due_at=reminder_at + timedelta(hours=1))
    assert snoozed is not None
    assert snoozed.due_at == reminder_at + timedelta(hours=1)

    acked = storage.ack_private_reminder(reminders[0].id, password="secret123")
    assert acked is not None
    assert acked.status == "completed"

    reloaded_task = storage.find_private_task(task.id, "secret123")
    assert reloaded_task is not None
    assert reloaded_task.reminder_at is None
