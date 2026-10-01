from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from personal_assistant.assistant import AssistantDependencies, AssistantService
from personal_assistant.models import AssistantPlan
from personal_assistant import assistant as assistant_module
from personal_assistant import storage as storage_module
from personal_assistant import time_utils as time_utils_module
from personal_assistant.time_utils import now_utc, parse_user_datetime
from personal_assistant.worker import BackgroundWorker


async def test_deterministic_task_flow_avoids_planner(assistant_bundle):
    assistant, _, _, planner = assistant_bundle

    create_reply = await assistant.handle_message(chat_id="chat-1", text="add task finish spec")
    list_reply = await assistant.handle_message(chat_id="chat-1", text="tasks")
    done_reply = await assistant.handle_message(chat_id="chat-1", text="done finish spec")

    assert "Created task" in create_reply
    assert "finish spec" in list_reply
    assert "completed" in done_reply
    assert planner.calls == []


async def test_ai_command_routes_to_planner(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    planner.next_plan = AssistantPlan(action="append_note", args={"text": "Book dentist appointment soon"})

    reply = await assistant.handle_message(chat_id="chat-1", text="ai Please remember this for me.")

    assert "inbox" in reply.lower()
    assert len(planner.calls) == 1
    assert planner.calls[0]["user_message"] == "Please remember this for me."
    inbox = (storage.notes_dir / "inbox.md").read_text(encoding="utf-8")
    assert "Book dentist appointment soon" in inbox


async def test_ai_command_without_openai_shows_helpful_message(assistant_bundle, settings, storage):
    settings.openai_api_key = ""
    assistant, _, _, planner = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="ai Do something fancy")

    assert "not configured" in reply.lower()
    assert "openai_api_key" in reply.lower()
    assert planner.calls == []
    assert storage.get_pending("chat-1") is None


async def test_unrecognised_request_does_not_route_to_planner_implicitly(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="What is the meaning of life?")

    assert "didn't recognise" in reply.lower()
    assert "ai <request>" in reply.lower()
    assert len(planner.calls) == 0
    assert storage.get_pending("chat-1") is None


async def test_ai_command_without_prompt_shows_usage(assistant_bundle):
    assistant, _, _, planner = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="/ai")

    assert "use `ai <request>`" in reply.lower()
    assert len(planner.calls) == 0


async def test_ai_trace_and_prompt_commands_show_last_call_details(assistant_bundle):
    assistant, _, _, planner = assistant_bundle
    planner.next_plan = AssistantPlan(action="append_note", args={"text": "Capture this"})

    await assistant.handle_message(chat_id="chat-1", text="ai Capture this")
    trace_reply = await assistant.handle_message(chat_id="chat-1", text="ai trace")
    prompt_reply = await assistant.handle_message(chat_id="chat-1", text="ai prompt")
    context_reply = await assistant.handle_message(chat_id="chat-1", text="ai context")

    assert "last openai trace" in trace_reply.lower()
    assert "append_note" in trace_reply
    assert "req_fake_123" in trace_reply
    assert "fake system" in prompt_reply
    assert "capture this" in prompt_reply.lower()
    assert "conversation summary" in context_reply.lower()


async def test_ai_status_and_recent_traces_show_observability_summary(assistant_bundle):
    assistant, _, _, planner = assistant_bundle
    planner.next_plan = AssistantPlan(action="append_note", args={"text": "One"})

    await assistant.handle_message(chat_id="chat-1", text="ai One")
    status_reply = await assistant.handle_message(chat_id="chat-1", text="ai status")
    traces_reply = await assistant.handle_message(chat_id="chat-1", text="ai traces")

    assert "openai status" in status_reply.lower()
    assert "configured: yes" in status_reply.lower()
    assert "last action: append_note" in status_reply.lower()
    assert "recent openai traces" in traces_reply.lower()
    assert "one" in traces_reply.lower()


@pytest.mark.asyncio
async def test_telegram_ai_request_runs_in_background_and_reports_running_status(assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle

    async def slow_run_ai_request(*, chat_id: str, user_message: str, sync_before_run: bool = True) -> str:
        await asyncio.sleep(0.05)
        return f"Finished: {user_message}"

    assistant._run_ai_request = slow_run_ai_request  # type: ignore[method-assign]
    assistant._sync_durable_state_now = lambda: asyncio.sleep(0, result="No durable state changes to sync.")  # type: ignore[method-assign]

    await assistant.process_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "text": "ai summarize my current priorities",
                "chat": {"id": "chat-1", "type": "private"},
                "from": {"id": assistant.settings.telegram_allowed_user_id},
            },
        }
    )

    running_reply = await assistant.handle_message(chat_id="chat-1", text="ai running")
    assert "status: syncing" in running_reply.lower() or "status: running" in running_reply.lower()

    await asyncio.sleep(0.08)

    latest_messages = [text for _, text in telegram.sent_messages]
    assert any("AI task started." in message for message in latest_messages)
    assert any("Finished: summarize my current priorities" in message for message in latest_messages)


async def test_auto_sync_is_throttled_to_hourly(assistant_bundle, settings):
    settings.git_backup_enabled = True
    settings.backup_interval_seconds = 300
    assistant, _, _, _ = assistant_bundle
    calls: list[str] = []
    sleeps: list[float] = []

    assistant.last_auto_sync_started_at = now_utc()

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    assistant.backup_service.backup_now = lambda: calls.append("backup") or True  # type: ignore[method-assign]
    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        await assistant._drain_auto_sync_requests()
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert calls == ["backup"]
    assert sleeps
    assert sleeps[0] >= 3599


async def test_ai_usage_and_cost_commands_surface_org_and_local_stats(assistant_bundle):
    assistant, _, _, planner = assistant_bundle
    planner.next_plan = AssistantPlan(action="append_note", args={"text": "Metered request"})
    planner.usage_summary = {
        "available": True,
        "num_model_requests": 12,
        "input_tokens": 345,
        "output_tokens": 67,
        "input_cached_tokens": 10,
    }
    planner.cost_summary = {
        "available": True,
        "total_cost": 1.2345,
        "currency": "usd",
        "line_items": 3,
    }

    await assistant.handle_message(chat_id="chat-1", text="ai Metered request")
    usage_reply = await assistant.handle_message(chat_id="chat-1", text="ai usage 3")
    costs_reply = await assistant.handle_message(chat_id="chat-1", text="ai costs 14")
    credits_reply = await assistant.handle_message(chat_id="chat-1", text="ai credits")

    assert "ai usage for last 3 day" in usage_reply.lower()
    assert "local traced calls: 1" in usage_reply.lower()
    assert "org model requests: 12" in usage_reply.lower()
    assert "ai costs for last 14 day" in costs_reply.lower()
    assert "org cost: 1.2345 usd" in costs_reply.lower()
    assert "exact remaining prepaid credits are not exposed" in credits_reply.lower()


async def test_ai_agent_can_query_state_execute_action_and_log_backlog(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    await assistant.handle_message(chat_id="chat-1", text="task Finish quarterly report")
    planner.agent_outputs = [
        {
            "type": "tool_call",
            "tool": "query_state",
            "args": {"entity": "tasks", "query": "report", "limit": 5},
        },
        {
            "type": "tool_call",
            "tool": "execute_plan",
            "args": {"action": "complete_task", "args": {"identifier": "Finish quarterly report"}},
        },
        {
            "type": "finish",
            "message": "I marked Finish quarterly report as completed.",
            "automation_candidate": {
                "title": "Semantic task selection command",
                "why": "The assistant used AI to search tasks semantically and then complete one.",
                "example_request": "complete the report-related task",
                "suggested_command": "complete tasks about <topic>",
                "suggested_code_path": "semantic_parser.py + assistant task filters",
            },
        },
    ]

    reply = await assistant.handle_message(chat_id="chat-1", text="ai complete the report-related task")
    trace_reply = await assistant.handle_message(chat_id="chat-1", text="ai trace")
    backlog_reply = await assistant.handle_message(chat_id="chat-1", text="ai backlog")

    task = storage.find_task("Finish quarterly report")
    assert task is not None
    assert task.status == "completed"
    assert "completed" in reply.lower()
    assert "query_state" in trace_reply
    assert "execute_plan" in trace_reply
    assert "semantic task selection command" in backlog_reply.lower()
    backlog_note = storage.find_note("AI automation backlog")
    assert backlog_note is not None
    assert "Semantic task selection command" in backlog_note.body


async def test_ai_agent_can_search_calendar_semantically(assistant_bundle):
    assistant, _, calendar, planner = assistant_bundle
    calendar.upcoming_events = [
        calendar.upcoming_events[0],
        calendar.upcoming_events[0].model_copy(update={"id": "evt-talk-1", "title": "Research talk with visiting speaker"}),
        calendar.upcoming_events[0].model_copy(update={"id": "evt-talk-2", "title": "ML talks planning", "description": "Prep for conference talks"}),
    ]
    planner.agent_outputs = [
        {
            "type": "tool_call",
            "tool": "query_calendar_events",
            "args": {"query": "talks", "days_past": 7, "days_future": 30, "limit": 10},
        },
        {
            "type": "finish",
            "message": "I found 2 talk-related events: Research talk with visiting speaker; ML talks planning.",
        },
    ]

    reply = await assistant.handle_message(chat_id="chat-1", text="ai find all events that have to do with talks")

    assert "2 talk-related events" in reply
    assert "research talk" in reply.lower()


async def test_ai_agent_can_execute_multiple_actions_in_one_request(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    planner.agent_outputs = [
        {
            "type": "tool_call",
            "tool": "execute_plan",
            "args": {"action": "create_task", "args": {"title": "Buy milk"}},
        },
        {
            "type": "tool_call",
            "tool": "execute_plan",
            "args": {
                "action": "create_reminder",
                "args": {"text": "Call mum", "due_at": (now_utc() + timedelta(hours=2)).isoformat()},
            },
        },
        {
            "type": "finish",
            "message": "Created the task and reminder.",
        },
    ]

    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="ai create a task to buy milk and remind me to call mum in 2 hours",
    )

    assert "created the task and reminder" in reply.lower()
    assert any(task.title == "Buy milk" for task in storage.list_tasks())
    assert any(reminder.text == "Call mum" for reminder in storage.list_reminders())


async def test_ai_agent_can_manage_task_directly(assistant_bundle, storage, settings):
    assistant, _, _, planner = assistant_bundle
    storage.create_task("Demo report")
    planner.agent_outputs = [
        {
            "type": "tool_call",
            "tool": "manage_task",
            "args": {"operation": "update", "identifier": "Demo report", "due_at": "thursday"},
        },
        {
            "type": "finish",
            "message": "Updated the Demo report task due date.",
        },
    ]

    reply = await assistant.handle_message(chat_id="chat-1", text="ai move the Demo report task due date to thursday")

    task = storage.find_task("Demo report")
    assert "updated the demo report task due date" in reply.lower()
    assert task is not None
    assert task.due_at is not None
    expected = parse_user_datetime("thursday", settings.default_timezone)
    assert expected is not None
    assert task.due_at.date() == expected.date()


async def test_ai_batch_tool_supports_dry_run(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    result = await assistant._ai_tool_execute_plan_batch(  # type: ignore[attr-defined]
        {
            "dry_run": True,
            "plans": [
                {"action": "create_task", "args": {"title": "Buy milk"}},
                {"action": "create_note", "args": {"title": "Call bank"}},
            ],
        },
        chat_id="chat-1",
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert len(result["previews"]) == 2


async def test_ai_manage_task_merge_creates_one_task_and_completes_sources(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("set project-alpha finals", tags=["project-alpha"], priority="high")
    storage.create_task("convert alpha mcqs to short answer", tags=["project-alpha"], priority="medium")

    result = assistant._ai_tool_manage_task(  # type: ignore[attr-defined]
        {
            "operation": "merge",
            "source_identifiers": ["set project-alpha finals", "convert alpha mcqs to short answer"],
            "title": "Project Alpha finals and MCQ tasks",
            "priority": "high",
        },
        chat_id="chat-1",
    )

    assert result["ok"] is True
    assert "merged 2 tasks" in result["message"].lower()
    merged = storage.find_task("Project Alpha finals and MCQ tasks")
    assert merged is not None
    assert merged.status == "open"
    assert merged.priority == "high"
    assert storage.find_task("set project-alpha finals") is not None
    assert storage.find_task("set project-alpha finals").status == "completed"
    assert storage.find_task("convert alpha mcqs to short answer") is not None
    assert storage.find_task("convert alpha mcqs to short answer").status == "completed"


async def test_ai_agent_can_search_and_read_raw_state_files(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    storage.append_note("call the bank tomorrow")
    planner.agent_outputs = [
        {
            "type": "tool_call",
            "tool": "search_state_files",
            "args": {"query": "bank", "glob": "notes/*.md", "limit": 5},
        },
        {
            "type": "tool_call",
            "tool": "read_state_files",
            "args": {"paths": ["notes/inbox.md"], "limit": 1},
        },
        {
            "type": "finish",
            "message": "I found that note in your inbox.",
        },
    ]

    reply = await assistant.handle_message(chat_id="chat-1", text="ai find the note about the bank")
    trace_reply = await assistant.handle_message(chat_id="chat-1", text="ai trace")

    assert "found that note" in reply.lower()
    assert "search_state_files" in trace_reply
    assert "read_state_files" in trace_reply


async def test_ai_agent_can_manage_calendar_events_directly(assistant_bundle):
    assistant, _, calendar, planner = assistant_bundle
    event = await calendar.create_event(
        title="Demo review",
        start=now_utc() + timedelta(days=1),
        end=now_utc() + timedelta(days=1, hours=1),
    )
    planner.agent_outputs = [
        {
            "type": "tool_call",
            "tool": "manage_calendar_event",
            "args": {"operation": "delete", "identifier": event.id},
        },
        {
            "type": "finish",
            "message": "Deleted the Demo review event.",
        },
    ]

    reply = await assistant.handle_message(chat_id="chat-1", text="ai delete the demo review event")

    assert "deleted the demo review event" in reply.lower()
    assert event.id in calendar.deleted_event_ids


async def test_help_command_returns_help_text(assistant_bundle):
    assistant, _, _, planner = assistant_bundle

    for cmd in ("help", "/help", "?"):
        reply = await assistant.handle_message(chat_id="chat-1", text=cmd)
        assert "Daily actions:" in reply
        assert "help tasks" in reply.lower()
        assert "show" in reply.lower()
        assert planner.calls == []


async def test_help_topic_returns_segmented_help(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="help notes")

    assert "Notes" in reply
    assert "`note <title>`" in reply
    assert "People And Sync" not in reply


async def test_help_projects_returns_project_commands(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="help projects")

    assert reply.startswith("Projects")
    assert "`projects`" in reply
    assert "`delete project <id or #>`" in reply


async def test_help_plans_reports_deprecation(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="help plans")

    assert "removed from the main interface" in reply.lower()


async def test_help_settings_alias_returns_system_help(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="help settings")

    assert reply.startswith("System")
    assert "`dashboard`, `show`" in reply
    assert "`system health`, `state health`" in reply


async def test_help_unknown_topic_returns_suggestions(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="help bananas")

    assert "don't have a help topic" in reply.lower()
    assert "help system" in reply.lower()


async def test_help_all_has_extra_spacing(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="help all")

    assert "Personal Assistant - full command list" in reply
    assert "Older aliases still work" in reply or "older aliases still work" in reply.lower()
    assert "\n\n`tasks` - list open tasks" in reply
    assert "\n\nSystem\n\n" in reply


async def test_debug_info_command_returns_runtime_git_metadata(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    assistant._git_debug_info = lambda: {  # type: ignore[method-assign]
        "branch": "main",
        "commit": "abcdef1234567890",
        "short_commit": "abcdef1",
        "dirty": "yes",
        "commit_subject": "Test commit subject",
        "error": "",
    }

    reply = await assistant.handle_message(chat_id="chat-1", text="debug info")

    assert "debug info" in reply.lower()
    assert "git branch: main" in reply.lower()
    assert "git commit: abcdef1234567890" in reply.lower()
    assert "git short commit: abcdef1" in reply.lower()
    assert "git dirty: yes" in reply.lower()
    assert "test commit subject" in reply.lower()


async def test_debug_info_command_surfaces_git_errors(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    assistant._git_debug_info = lambda: {  # type: ignore[method-assign]
        "branch": "(unavailable)",
        "commit": "(unavailable)",
        "short_commit": "(unavailable)",
        "dirty": "no",
        "commit_subject": "",
        "error": "not a git repository",
    }

    reply = await assistant.handle_message(chat_id="chat-1", text="/debug")

    assert "git commit: (unavailable)" in reply.lower()
    assert "git error: not a git repository" in reply.lower()


async def test_system_health_reports_backup_and_quarantine_status(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    assistant.backup_service._record_status(status="failed", detail="push rejected")  # type: ignore[attr-defined]
    stale = storage.quarantine_dir / "telegram.json.corrupt"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("bad", encoding="utf-8")

    reply = await assistant.handle_message(chat_id="chat-1", text="system health")

    assert "system health" in reply.lower()
    assert "last backup status: failed" in reply.lower()
    assert "quarantined files:" in reply.lower()


async def test_show_command_returns_tasks_reminders_and_projects(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Buy groceries", priority="high")
    storage.create_reminder("Call mum", due_at=now_utc() + timedelta(hours=1))
    storage.create_project("Project Beta slides", "Longer term presentation work.")
    storage.create_note("Project idea", "Build the parser.")

    reply = await assistant.handle_message(chat_id="chat-1", text="show")

    assert "Open tasks:" in reply
    assert "Active reminders:" in reply
    assert "Projects:" in reply
    assert "Buy groceries" in reply
    assert "Call mum" in reply
    assert "Project Beta slides" in reply
    assert "Project idea" not in reply


async def test_dashboard_command_alias_returns_tasks_reminders_and_projects(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Buy groceries", priority="high")
    storage.create_reminder("Call mum", due_at=now_utc() + timedelta(hours=1))
    storage.create_project("Language learning")

    reply = await assistant.handle_message(chat_id="chat-1", text="dashboard")

    assert "Open tasks:" in reply
    assert "Active reminders:" in reply
    assert "Projects:" in reply
    assert "Language learning" in reply


async def test_s_command_alias_returns_dashboard(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Buy groceries", priority="high")

    reply = await assistant.handle_message(chat_id="chat-1", text="s")

    assert "Open tasks:" in reply
    assert "Buy groceries" in reply


async def test_send_message_disables_link_previews(assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle

    await assistant._send_message("chat-1", "https://example.com")

    assert telegram.sent_disable_previews == [True]


async def test_recurring_reminder_replays_without_completing(assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle
    reminder = storage.create_reminder(
        "Stretch",
        due_at=now_utc() - timedelta(minutes=1),
        recurrence="daily",
    )

    sent = await assistant.send_due_reminders()

    assert sent == 1
    assert "Reminder: Stretch" in telegram.sent_messages[0][1]
    updated = storage.list_reminders(include_inactive=True)[0]
    assert updated.id == reminder.id
    assert updated.status == "scheduled"
    assert updated.due_at > now_utc()


async def test_free_slot_and_calendar_create(assistant_bundle):
    assistant, _, calendar, _ = assistant_bundle

    free_slot_reply = await assistant.handle_message(chat_id="chat-1", text="find a 30 minute free slot")
    event_reply = await assistant.handle_message(
        chat_id="chat-1",
        text="schedule Demo review tomorrow 3pm for 45 minutes",
    )

    assert "First free 30-minute slot" in free_slot_reply
    assert "Created event Demo review" in event_reply
    assert len(calendar.created_events) == 1


async def test_people_view_requires_password(assistant_bundle, settings):
    settings.people_encryption_key = "secret123"
    assistant, _, _, _ = assistant_bundle

    # First message: request people list
    prompt = await assistant.handle_message(chat_id="chat-1", text="people")
    assert "password" in prompt.lower()

    # Wrong password
    wrong_reply = await assistant.handle_message(chat_id="chat-1", text="wrongpassword")
    assert "wrong password" in wrong_reply.lower()

    # Correct password (no entries yet)
    prompt2 = await assistant.handle_message(chat_id="chat-1", text="people")
    correct_reply = await assistant.handle_message(chat_id="chat-1", text="secret123")
    assert "no people entries yet" in correct_reply.lower()


async def test_people_password_retry_does_not_require_reissuing_command(assistant_bundle, settings):
    settings.people_encryption_key = "secret123"
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="people")
    wrong_reply = await assistant.handle_message(chat_id="chat-1", text="wrongpassword")
    correct_reply = await assistant.handle_message(chat_id="chat-1", text="secret123")

    assert "try again" in wrong_reply.lower()
    assert "no people entries yet" in correct_reply.lower()


async def test_force_git_sync_requires_password(assistant_bundle, settings):
    settings.operator_password = "sync-secret"
    assistant, _, _, _ = assistant_bundle

    prompt = await assistant.handle_message(chat_id="chat-1", text="sync")
    assert "password" in prompt.lower()

    wrong = await assistant.handle_message(chat_id="chat-1", text="wrong")
    assert "wrong password" in wrong.lower()


async def test_force_git_sync_returns_no_changes_when_clean(assistant_bundle, settings):
    settings.operator_password = "sync-secret"
    assistant, _, _, _ = assistant_bundle
    assistant.backup_service.durable_changes_pending = lambda: False  # type: ignore[method-assign]

    await assistant.handle_message(chat_id="chat-1", text="sync")
    reply = await assistant.handle_message(chat_id="chat-1", text="sync-secret")
    assert "no durable state changes" in reply.lower()


async def test_person_add_and_view(assistant_bundle, settings):
    settings.people_encryption_key = "mysecret"
    assistant, _, _, _ = assistant_bundle

    # Add person using multi-word name via colon separator.
    prompt = await assistant.handle_message(chat_id="chat-1", text="person Alice Smith: loves hiking")
    assert "password" in prompt.lower()
    save_reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")
    assert "alice smith" in save_reply.lower()

    # View and verify entry is there
    await assistant.handle_message(chat_id="chat-1", text="people")
    view_reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")
    assert "# People" not in view_reply
    assert "Alice Smith" in view_reply
    assert "loves hiking" in view_reply


async def test_person_multiple_entries_same_person(assistant_bundle, settings):
    settings.people_encryption_key = "mysecret"
    assistant, _, _, _ = assistant_bundle

    # Add first entry for John Doe.
    await assistant.handle_message(chat_id="chat-1", text="person John Doe: loves coffee")
    await assistant.handle_message(chat_id="chat-1", text="mysecret")

    # Add a second entry for the same person.
    await assistant.handle_message(chat_id="chat-1", text="person John Doe: works at Acme Corp")
    await assistant.handle_message(chat_id="chat-1", text="mysecret")

    # View and confirm both entries appear under a single heading.
    await assistant.handle_message(chat_id="chat-1", text="people")
    view_reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")

    assert "John Doe" in view_reply
    assert "loves coffee" in view_reply
    assert "works at Acme Corp" in view_reply
    assert view_reply.count("John Doe") == 1
    # Entries should appear in the order they were added.
    assert view_reply.index("loves coffee") < view_reply.index("works at Acme Corp")


async def test_person_case_insensitive_grouping(assistant_bundle, settings):
    settings.people_encryption_key = "mysecret"
    assistant, _, _, _ = assistant_bundle

    # First entry uses title-case name.
    await assistant.handle_message(chat_id="chat-1", text="person Jane Doe: works at Initech")
    await assistant.handle_message(chat_id="chat-1", text="mysecret")

    # Second entry uses lower-case name — should land in the same section.
    await assistant.handle_message(chat_id="chat-1", text="person jane doe: favourite colour is blue")
    await assistant.handle_message(chat_id="chat-1", text="mysecret")

    await assistant.handle_message(chat_id="chat-1", text="people")
    view_reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")

    assert "works at Initech" in view_reply
    assert "favourite colour is blue" in view_reply
    assert view_reply.lower().count("jane doe") == 1


async def test_person_entry_can_be_updated(assistant_bundle, settings):
    settings.people_encryption_key = "mysecret"
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="person Alice: old info")
    await assistant.handle_message(chat_id="chat-1", text="mysecret")

    prompt = await assistant.handle_message(chat_id="chat-1", text="edit person Alice entry 1: new info")
    assert "password" in prompt.lower()
    reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")
    assert "updated entry 1" in reply.lower()

    await assistant.handle_message(chat_id="chat-1", text="people")
    view_reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")
    assert "new info" in view_reply
    assert "old info" not in view_reply


async def test_person_can_be_deleted(assistant_bundle, settings):
    settings.people_encryption_key = "mysecret"
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="person Alice: secret info")
    await assistant.handle_message(chat_id="chat-1", text="mysecret")

    prompt = await assistant.handle_message(chat_id="chat-1", text="delete person Alice")
    assert "password" in prompt.lower()
    reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")
    assert "deleted person" in reply.lower()

    await assistant.handle_message(chat_id="chat-1", text="people")
    view_reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")
    assert "alice" not in view_reply.lower()


async def test_people_not_configured_shows_error(assistant_bundle, settings):
    settings.people_encryption_key = ""
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="people")
    assert "not configured" in reply.lower()


async def test_people_view_reports_corrupt_data(assistant_bundle, settings, storage):
    settings.people_encryption_key = "secret123"
    storage.people_enc_path.parent.mkdir(parents=True, exist_ok=True)
    storage.people_enc_path.write_bytes(b"bad-data")
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="people")
    reply = await assistant.handle_message(chat_id="chat-1", text="secret123")

    assert "failed to decrypt" in reply.lower()


async def test_undo_reverts_last_created_task(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    create_reply = await assistant.handle_message(chat_id="chat-1", text="task buy milk")
    undo_reply = await assistant.handle_message(chat_id="chat-1", text="undo")

    assert "created task" in create_reply.lower()
    assert "undid" in undo_reply.lower()
    assert storage.list_tasks() == []


async def test_undo_reopens_completed_task(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="task finish spec")
    await assistant.handle_message(chat_id="chat-1", text="done finish spec")
    undo_reply = await assistant.handle_message(chat_id="chat-1", text="undo")

    task = storage.find_task("finish spec")
    assert "undid" in undo_reply.lower()
    assert task is not None
    assert task.status == "open"


async def test_undo_reverts_people_entry_without_reasking_for_password(assistant_bundle, settings):
    settings.people_encryption_key = "mysecret"
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="person Alice: private note")
    await assistant.handle_message(chat_id="chat-1", text="mysecret")
    undo_reply = await assistant.handle_message(chat_id="chat-1", text="undo")

    await assistant.handle_message(chat_id="chat-1", text="people")
    view_reply = await assistant.handle_message(chat_id="chat-1", text="mysecret")

    assert "undid" in undo_reply.lower()
    assert "no people entries yet" in view_reply.lower()


async def test_undo_deletes_last_created_calendar_event(assistant_bundle):
    assistant, _, calendar, _ = assistant_bundle

    create_reply = await assistant.handle_message(
        chat_id="chat-1",
        text="schedule Demo review tomorrow 3pm for 45 minutes",
    )
    undo_reply = await assistant.handle_message(chat_id="chat-1", text="undo")

    assert "created event" in create_reply.lower()
    assert "undid" in undo_reply.lower()
    assert calendar.created_events == []
    assert calendar.deleted_event_ids == ["evt-1"]


async def test_undo_command_accepts_telegram_bot_suffix(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="task buy milk")
    undo_reply = await assistant.handle_message(chat_id="chat-1", text="/undo@ss_personal_assistant_bot")

    assert "undid" in undo_reply.lower()
    assert storage.list_tasks() == []


async def test_process_update_normalizes_telegram_slash_command_variants(assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="12345", text="task buy milk")
    update = {
        "update_id": 601,
        "message": {
            "message_id": 301,
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "\u200e/undo@ss_personal_assistant_bot",
        },
    }

    await assistant.process_update(update)

    assert any("undid" in text.lower() for _, text in telegram.sent_messages)
    assert storage.list_tasks() == []


async def test_help_all_callback_sends_full_help_as_new_message(assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle

    update = {
        "update_id": 602,
        "callback_query": {
            "id": "cq-help-all",
            "from": {"id": 12345},
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "message_id": 302,
                "text": "Personal Assistant help",
            },
            "data": "help:all",
        },
    }

    await assistant.process_update(update)

    assert "cq-help-all" in telegram.answered_callbacks
    assert any("full command list" in text.lower() for _, text in telegram.sent_messages)


# ---------------------------------------------------------------------------
# Task tags and updates
# ---------------------------------------------------------------------------

async def test_task_create_with_tags(assistant_bundle):
    assistant, _, _, planner = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="task Buy groceries #shopping #errand")
    assert "Created task" in reply
    assert "shopping" in reply
    assert "errand" in reply
    assert planner.calls == []


async def test_task_list_shows_tags(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Fix bug", tags=["dev", "urgent"])

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks")
    assert "Fix bug" in reply
    assert "dev" in reply or "urgent" in reply


async def test_task_list_can_sort_by_due_date(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Later", due_at=now_utc() + timedelta(days=2))
    storage.create_task("Sooner", due_at=now_utc() + timedelta(hours=2))

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks by due")

    assert "by due date" in reply.lower()
    assert reply.index("Sooner") < reply.index("Later")


async def test_task_list_can_sort_by_reminder(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Later", reminder_at=now_utc() + timedelta(days=1))
    storage.create_task("Sooner", reminder_at=now_utc() + timedelta(hours=1))

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks by remind")

    assert "by reminder" in reply.lower()
    assert reply.index("Sooner") < reply.index("Later")


async def test_task_list_shows_more_than_ten_items(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    for index in range(11):
        storage.create_task(f"Task {index + 1}", priority="low")

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks")

    assert "11. 🟢 Low - Task 11" in reply


async def test_task_numeric_selection_uses_current_list_context(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Zulu task", tags=["z"])
    storage.create_task("Alpha task", tags=["a"])

    list_reply = await assistant.handle_message(chat_id="chat-1", text="tasks by tag")
    complete_reply = await assistant.handle_message(chat_id="chat-1", text="done 1")

    assert list_reply.index("Alpha task") < list_reply.index("Zulu task")
    assert "alpha task" in complete_reply.lower()
    open_titles = [task.title for task in storage.list_tasks()]
    assert "Zulu task" in open_titles
    assert "Alpha task" not in open_titles


async def test_delete_number_keeps_exact_id_when_another_task_has_that_prefix(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    draft = storage.create_task("Report draft", priority="high")
    report = storage.create_task("Report", priority="low")
    await assistant.handle_message(chat_id="chat-1", text="tasks")

    reply = await assistant.handle_message(chat_id="chat-1", text="delete task 2")

    assert "Deleted task: Report." in reply
    assert {task.id for task in storage.list_tasks()} == {draft.id}


async def test_delete_stale_list_number_does_not_match_another_task_prefix(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    report = storage.create_task("Report", priority="high")
    draft = storage.create_task("Report draft", priority="low")
    await assistant.handle_message(chat_id="chat-1", text="tasks")
    storage.delete_task(report.id)

    reply = await assistant.handle_message(chat_id="chat-1", text="delete task 1")

    assert "no longer" in reply.lower()
    assert {task.id for task in storage.list_tasks()} == {draft.id}


async def test_contextual_delete_numeric_slug_is_not_resolved_twice(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    keep = storage.create_task("Keep", priority="high")
    storage.create_task("1", priority="low")
    await assistant.handle_message(chat_id="chat-1", text="tasks")

    reply = await assistant.handle_message(chat_id="chat-1", text="delete the second one")

    assert "Deleted task: 1." in reply
    assert {task.id for task in storage.list_tasks()} == {keep.id}


async def test_bare_delete_number_is_position_even_when_a_title_is_numeric(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    keep = storage.create_task("2", priority="high")
    storage.create_task("Delete me", priority="low")
    await assistant.handle_message(chat_id="chat-1", text="tasks")

    reply = await assistant.handle_message(chat_id="chat-1", text="delete 2")

    assert "Deleted task: Delete me." in reply
    assert {task.id for task in storage.list_tasks()} == {keep.id}


async def test_numeric_id_from_disambiguation_is_not_a_list_position(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    keep = storage.create_task("Keep", priority="high")
    selected = storage.create_task("1", priority="low")
    storage.set_pending("chat-1", {
        "type": "entity_disambiguation", "entity_type": "task", "action": "delete_task",
        "choices": [{"id": selected.id, "label": selected.title}], "args": {},
    })

    reply = await assistant.handle_message(chat_id="chat-1", text="1")

    assert "Deleted task: 1." in reply
    assert {task.id for task in storage.list_tasks()} == {keep.id}


async def test_plan_number_uses_displayed_snapshot_after_new_plan(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    first = storage.create_plan("First")
    storage.create_plan("Selected")
    await assistant.handle_message(chat_id="chat-1", text="plans")
    keep = storage.create_plan("Keep")

    reply = await assistant.handle_message(chat_id="chat-1", text="delete plan 1")

    assert "Deleted plan: Selected." in reply
    assert {plan.id for plan in storage.list_plans()} == {first.id, keep.id}


async def test_delete_from_filtered_list_rejects_hidden_number(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Visible", tags=["work"])
    storage.create_task("Hidden", tags=["home"])
    await assistant.handle_message(chat_id="chat-1", text="tasks tag work")

    reply = await assistant.handle_message(chat_id="chat-1", text="delete task 2")

    assert "only see 1" in reply.lower()
    assert len(storage.list_tasks()) == 2


@pytest.mark.parametrize("entity", ["note", "plan", "project", "preference"])
async def test_delete_saved_item_number_prefers_exact_id(assistant_bundle, storage, entity):
    assistant, _, _, _ = assistant_bundle
    create = getattr(storage, f"create_{entity}")
    create("Report")
    draft = create("Report draft")
    await assistant.handle_message(chat_id="chat-1", text=f"{entity}s")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"delete {entity} 2")

    assert f"Deleted {entity}: Report." in reply
    assert {item.id for item in getattr(storage, f"list_{entity}s")()} == {draft.id}


async def test_cancel_task_reminder_number_prefers_exact_id(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Report draft", reminder_at=now_utc() + timedelta(hours=1))
    storage.create_task("Report", reminder_at=now_utc() + timedelta(hours=2))
    await assistant.handle_message(chat_id="chat-1", text="reminders")

    reply = await assistant.handle_message(chat_id="chat-1", text="delete reminder 2")

    assert "Cancelled reminder: Task reminder: Report." in reply
    assert [item.text for item in storage.list_reminders()] == ["Task reminder: Report draft"]


async def test_consecutive_deletions_use_the_refreshed_list(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("First")
    storage.create_task("Second")
    keep = storage.create_task("Third")
    await assistant.handle_message(chat_id="chat-1", text="tasks")

    first = await assistant.handle_message(chat_id="chat-1", text="delete task 1")
    second = await assistant.handle_message(chat_id="chat-1", text="delete task 1")

    assert "Deleted task: First." in first
    assert "Deleted task: Second." in second
    assert {item.id for item in storage.list_tasks()} == {keep.id}


async def test_done_numeric_without_task_list_context_asks_for_task_context(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Task one")
    storage.create_task("Task two")
    storage.create_reminder("Reminder one", due_at=now_utc() + timedelta(hours=1))

    await assistant.handle_message(chat_id="chat-1", text="reminders")
    reply = await assistant.handle_message(chat_id="chat-1", text="done 2")

    assert "list task" in reply.lower() or "task title or id" in reply.lower()
    open_titles = [task.title for task in storage.list_tasks()]
    assert "Task one" in open_titles
    assert "Task two" in open_titles


async def test_contextual_delete_out_of_range_number_does_not_guess_first_item(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_reminder("Reminder one", due_at=now_utc() + timedelta(hours=1))
    storage.create_reminder("Reminder two", due_at=now_utc() + timedelta(hours=2))

    await assistant.handle_message(chat_id="chat-1", text="reminders")
    reply = await assistant.handle_message(chat_id="chat-1", text="delete 14")

    assert "only see 2 reminder" in reply.lower()
    reminders = storage.list_reminders()
    assert [item.text for item in reminders] == ["Reminder one", "Reminder two"]


async def test_task_delete_ambiguous_identifier_asks_for_follow_up(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Write report")
    storage.create_task("Review report")

    prompt = await assistant.handle_message(chat_id="chat-1", text="delete task report")
    reply = await assistant.handle_message(chat_id="chat-1", text="2")

    assert "multiple tasks" in prompt.lower()
    assert "1. Write report" in prompt
    assert "2. Review report" in prompt
    assert "Deleted task: Review report." in reply
    assert storage.find_task("Write report") is not None
    assert storage.find_task("Review report") is None


async def test_task_priority_is_stored_and_sorted_first(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    await assistant.handle_message(chat_id="chat-1", text="task write report priority low")
    await assistant.handle_message(chat_id="chat-1", text="task pay tax priority high")
    await assistant.handle_message(chat_id="chat-1", text="task book travel priority medium")

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks")
    lines = reply.splitlines()

    assert "🔴 high - pay tax" in lines[1].lower()
    assert "🟡 medium - book travel" in lines[2].lower()
    assert "🟢 low - write report" in lines[3].lower()


async def test_tasks_by_priority_view(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Low item", priority="low")
    storage.create_task("High item", priority="high")

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks by priority")

    assert "by priority" in reply.lower()
    assert reply.splitlines()[1].endswith("High item")


async def test_task_list_can_filter_by_tag(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Work item", tags=["work"])
    storage.create_task("Home item", tags=["home"])

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks tag work")

    assert "work item" in reply.lower()
    assert "home item" not in reply.lower()


async def test_task_view_by_id(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("Write report", tags=["work"])

    reply = await assistant.handle_message(chat_id="chat-1", text=f"task {task.id}")
    assert "Write report" in reply
    assert "work" in reply
    assert task.id.startswith("write-report")


async def test_task_view_command(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("Read book", body="Start with chapter 1.")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"view task {task.id}")
    assert "Read book" in reply
    assert "Start with chapter 1." in reply


async def test_task_delete_command(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("Disposable")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"delete task {task.id}")

    assert "deleted task" in reply.lower()
    assert "no open tasks" in reply.lower()
    assert storage.find_task(task.id) is None


async def test_task_delete_shows_updated_list(assistant_bundle, storage):
    storage.create_task("Buy groceries")
    storage.create_task("Pay rent")
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="delete task buy groceries")

    assert "Deleted task: Buy groceries." in reply
    assert "Open tasks" in reply
    assert "Pay rent" in reply
    assert "Buy groceries" not in reply.split("\n\n", 1)[1]


async def test_d_alias_marks_task_done(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("Short alias task")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"d {task.id}")

    assert "completed" in reply.lower()
    assert storage.find_task(task.id).status == "completed"


async def test_delete_all_tasks_requires_confirmation(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("One")
    storage.create_task("Two")

    prompt = await assistant.handle_message(chat_id="chat-1", text="delete all tasks")

    assert "reply yes to confirm" in prompt.lower()

    reply = await assistant.handle_message(chat_id="chat-1", text="yes")

    assert "deleted 2 task" in reply.lower()
    assert storage.list_tasks(include_completed=True) == []


async def test_task_update_title(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("Old title")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update task {task.id} title New title")
    assert "Updated task" in reply
    updated = storage.find_task(task.id)
    assert updated.title == "New title"


async def test_task_update_description(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("My task")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update task {task.id} description This is a long description.")
    assert "Updated task" in reply
    updated = storage.find_task(task.id)
    assert updated.body == "This is a long description."


async def test_task_update_numeric_without_task_list_context_uses_visible_open_task_numbering(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    numeric = storage.create_task("1")
    target = storage.create_task("Project Beta final report", priority="high")

    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="update task 1 description project-beta final report grading",
    )

    assert "Updated task: Project Beta final report." in reply
    assert storage.find_task(numeric.id).body == ""
    assert storage.find_task(target.id).body == "project-beta final report grading"


async def test_task_update_numeric_uses_current_task_list_context(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    numeric = storage.create_task("1")
    target = storage.create_task("Project Beta final report", priority="high")

    await assistant.handle_message(chat_id="chat-1", text="tasks")
    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="update task 1 description project-beta final report grading",
    )

    assert "Updated task: Project Beta final report." in reply
    assert storage.find_task(target.id).body == "project-beta final report grading"
    assert storage.find_task(numeric.id).body == ""


async def test_task_update_tags(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("Tag me")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update task {task.id} tags alpha beta")
    assert "Updated task" in reply
    updated = storage.find_task(task.id)
    assert "alpha" in updated.tags
    assert "beta" in updated.tags


async def test_other_task_command_lists_separately_and_show_includes_it(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("My own work")

    create_reply = await assistant.handle_message(
        chat_id="chat-1",
        text="ot Zhenkai sends Project Beta slides remind me tomorrow 9am",
    )
    tasks_reply = await assistant.handle_message(chat_id="chat-1", text="tasks")
    others_reply = await assistant.handle_message(chat_id="chat-1", text="others tasks")
    show_reply = await assistant.handle_message(chat_id="chat-1", text="show")

    other = storage.find_task("Zhenkai sends Project Beta slides")
    assert "Created others' task: Zhenkai sends Project Beta slides." in create_reply
    assert other is not None
    assert other.kind == "other"
    assert other.reminder_at is not None
    assert "Zhenkai sends Project Beta slides" not in tasks_reply
    assert "Others' tasks:" in others_reply
    assert "Zhenkai sends Project Beta slides" in others_reply
    assert "Others' tasks:" in show_reply
    assert "Zhenkai sends Project Beta slides" in show_reply


async def test_retag_tasks_command_updates_all_matching_tasks(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    first = storage.create_task("Project Alpha lecture", tags=["project-alpha", "teaching"])
    second = storage.create_task("Project Alpha old", tags=["project-alpha"])
    storage.complete_task(second.id)
    untouched = storage.create_task("Admin", tags=["admin"])

    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="re-tag all tasks that have the tag project-alpha, to the tag alpha",
    )

    assert "Updated 2 task" in reply
    assert storage.find_task(first.id).tags == ["alpha", "teaching"]
    assert storage.find_task(second.id).tags == ["alpha"]
    assert storage.find_task(untouched.id).tags == ["admin"]


async def test_merge_tasks_command_combines_matches_and_completes_sources(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    first = storage.create_task("set project-alpha finals", tags=["project-alpha"], priority="high")
    second = storage.create_task("convert alpha mcqs to short answer", tags=["project-alpha"], priority="medium")

    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="combine project-alpha finals and mcq tasks into one task",
    )

    merged = storage.find_task("set project-alpha finals and convert alpha mcqs to short answer")
    assert "Merged 2 tasks" in reply
    assert merged is not None
    assert merged.status == "open"
    assert merged.tags == ["project-alpha"]
    assert merged.priority == "high"
    assert storage.find_task(first.id).status == "completed"
    assert storage.find_task(second.id).status == "completed"


async def test_task_update_priority(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    task = storage.create_task("Tag me")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update task {task.id} priority high")

    assert "Updated task" in reply
    assert "Open tasks:" in reply
    updated = storage.find_task(task.id)
    assert updated.priority == "high"


async def test_task_update_priority_by_partial_title_changes_listed_priority(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("poly aba grading", reminder_at=now_utc() + timedelta(hours=1))

    reply = await assistant.handle_message(chat_id="chat-1", text="update task aba priority high")

    updated = storage.find_task("poly aba grading")
    assert updated is not None
    assert updated.priority == "high"
    assert "🔴 High - poly aba grading" in reply


async def test_task_create_reports_invalid_due_date(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="task Buy milk due definitely-not-a-date")

    assert "couldn't understand that due date" in reply.lower()


async def test_task_input_pending_stays_active_after_invalid_due_date(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "task_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="Buy milk due definitely-not-a-date")

    assert "couldn't understand that due date" in reply.lower()
    pending = storage.get_pending("chat-1")
    assert pending is not None
    assert pending.get("type") == "datetime_clarification"


# ---------------------------------------------------------------------------
# Notes CRUD
# ---------------------------------------------------------------------------

async def test_note_create_and_list(assistant_bundle):
    assistant, _, _, planner = assistant_bundle

    create_reply = await assistant.handle_message(chat_id="chat-1", text="note Meeting summary")
    assert "Saved note" in create_reply

    list_reply = await assistant.handle_message(chat_id="chat-1", text="notes")
    assert "Meeting summary" in list_reply
    assert planner.calls == []


async def test_pending_task_input_allows_notes_command_escape(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "task_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="notes")

    assert "saved notes" in reply.lower()
    assert storage.get_pending("chat-1") is None
    assert storage.list_tasks() == []


async def test_note_create_with_description(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="note Project Alpha: Key milestones are Q1 and Q2.")
    assert "Saved note" in reply
    assert "Project Alpha" in reply


async def test_project_create_list_and_show(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    create_reply = await assistant.handle_message(
        chat_id="chat-1",
        text="p Project Beta slides: longer term presentation work",
    )
    list_reply = await assistant.handle_message(chat_id="chat-1", text="ps")
    show_reply = await assistant.handle_message(chat_id="chat-1", text="show")

    assert "Saved project: Project Beta slides." in create_reply
    assert "Projects:" in list_reply
    assert "Project Beta slides" in list_reply
    assert "longer term presentation work" in list_reply
    assert "Projects:" in show_reply
    assert "Project Beta slides" in show_reply
    assert storage.find_project("Project Beta slides") is not None


async def test_project_update_and_delete_by_list_number(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_project("Language learning", "Japanese study.")

    await assistant.handle_message(chat_id="chat-1", text="projects")
    update_reply = await assistant.handle_message(
        chat_id="chat-1",
        text="update project 1 description Japanese and Spanish study.",
    )
    await assistant.handle_message(chat_id="chat-1", text="projects")
    delete_reply = await assistant.handle_message(chat_id="chat-1", text="delete project 1")

    assert "Updated project: Language learning." in update_reply
    assert storage.find_project("Language learning") is None
    assert "Deleted project: Language learning." in delete_reply


async def test_capture_note_appends_to_inbox(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="capture note call the bank tomorrow")

    assert "inbox" in reply.lower()
    inbox = (storage.notes_dir / "inbox.md").read_text(encoding="utf-8")
    assert "call the bank tomorrow" in inbox


async def test_note_list_empty(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="notes")
    assert "no saved notes" in reply.lower()


async def test_note_view(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    note = storage.create_note("Dentist", "Call on Monday about the crown.")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"view note {note.id}")
    assert "Dentist" in reply
    assert "Created:" in reply
    assert "Updated:" in reply
    assert "Call on Monday about the crown." in reply


async def test_note_list_shows_human_friendly_timestamp(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_note("Dentist", "Call on Monday about the crown.")

    reply = await assistant.handle_message(chat_id="chat-1", text="notes")

    assert "Dentist" in reply
    assert any(token in reply for token in ("📍 Today ", "⏭️ Tomorrow ", "↩️ Yesterday ", " AM", " PM"))


def test_format_local_uses_relative_words_for_nearby_days():
    from personal_assistant.time_utils import format_local

    assert "📍 Today " in format_local(now_utc(), "Asia/Singapore")
    assert "⏭️ Tomorrow " in format_local(now_utc() + timedelta(days=1), "Asia/Singapore")
    assert "↩️ Yesterday " in format_local(now_utc() - timedelta(days=1), "Asia/Singapore")


async def test_note_update_title(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    note = storage.create_note("Old note title")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update note {note.id} title New note title")
    assert "Updated note" in reply
    assert "Notes:" in reply
    updated = storage.find_note(note.id)
    assert updated.title == "New note title"


async def test_note_list_shows_more_than_fifteen_items_and_view_uses_large_number(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    for index in range(16):
        storage.create_note(f"Note {index + 1}")

    list_reply = await assistant.handle_message(chat_id="chat-1", text="notes")
    view_reply = await assistant.handle_message(chat_id="chat-1", text="view note 16")

    assert "1. Note 16" in list_reply
    assert "16. Note 1" in list_reply
    assert "Note: Note 1" in view_reply


async def test_note_update_description(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    note = storage.create_note("My note")

    reply = await assistant.handle_message(
        chat_id="chat-1", text=f"update note {note.id} description Very long description goes here."
    )
    assert "Updated note" in reply
    updated = storage.find_note(note.id)
    assert updated.body == "Very long description goes here."


async def test_delete_all_notes_requires_confirmation(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_note("A")
    storage.create_note("B")

    prompt = await assistant.handle_message(chat_id="chat-1", text="delete all notes")
    assert "reply yes to confirm" in prompt.lower()

    reply = await assistant.handle_message(chat_id="chat-1", text="yes")
    assert "deleted 2 note" in reply.lower()
    assert storage.list_notes() == []


# ---------------------------------------------------------------------------
# Plans CRUD
# ---------------------------------------------------------------------------

async def test_plan_create_and_list(assistant_bundle):
    assistant, _, _, planner = assistant_bundle

    create_reply = await assistant.handle_message(chat_id="chat-1", text="plan Q3 roadmap")
    assert "Saved plan" in create_reply

    list_reply = await assistant.handle_message(chat_id="chat-1", text="plans")
    assert "Q3 roadmap" in list_reply
    assert planner.calls == []


async def test_plan_create_with_description(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="plan Fitness: Run 5K three times a week.")
    assert "Saved plan" in reply
    assert "Fitness" in reply


async def test_append_plan_writes_to_plans_markdown(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="append plan train for a half marathon")

    assert "plans" in reply.lower()
    plans_md = (storage.plans_dir / "plans.md").read_text(encoding="utf-8")
    assert "train for a half marathon" in plans_md


async def test_plan_list_empty(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="plans")
    assert "no saved plans" in reply.lower()


async def test_plan_view(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    plan = storage.create_plan("Learn Rust", "Complete the Rust book by end of month.")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"view plan {plan.id}")
    assert "Learn Rust" in reply
    assert "Complete the Rust book" in reply


async def test_plan_update_title(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    plan = storage.create_plan("Old plan title")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update plan {plan.id} title New plan title")
    assert "Updated plan" in reply
    assert "Plans:" in reply
    updated = storage.find_plan(plan.id)
    assert updated.title == "New plan title"


async def test_plan_update_description(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    plan = storage.create_plan("My plan")

    reply = await assistant.handle_message(
        chat_id="chat-1", text=f"update plan {plan.id} description A very detailed plan description."
    )
    assert "Updated plan" in reply
    updated = storage.find_plan(plan.id)
    assert updated.body == "A very detailed plan description."


# ---------------------------------------------------------------------------
# Preferences CRUD
# ---------------------------------------------------------------------------

async def test_preference_create_and_list(assistant_bundle):
    assistant, _, _, planner = assistant_bundle

    create_reply = await assistant.handle_message(chat_id="chat-1", text="preference Dark mode always")
    assert "Saved preference" in create_reply

    list_reply = await assistant.handle_message(chat_id="chat-1", text="preferences")
    assert "Dark mode always" in list_reply
    assert planner.calls == []


async def test_preference_create_with_description(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="preference Work hours: 9am to 6pm weekdays only.")
    assert "Saved preference" in reply
    assert "Work hours" in reply


async def test_remember_appends_to_preferences_markdown(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="remember i prefer walking meetings in the morning")

    assert "preferences" in reply.lower()
    prefs_md = (storage.preferences_dir / "preferences.md").read_text(encoding="utf-8")
    assert "walking meetings in the morning" in prefs_md


async def test_preference_list_empty(assistant_bundle):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="preferences")
    assert "no saved preferences" in reply.lower()


async def test_preference_view(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    pref = storage.create_preference("Time zone", "Always use Asia/Singapore timezone.")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"view preference {pref.id}")
    assert "Time zone" in reply
    assert "Asia/Singapore" in reply


async def test_preference_update_title(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    pref = storage.create_preference("Old pref title")

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update preference {pref.id} title New pref title")
    assert "Updated preference" in reply
    assert "Preferences:" in reply
    updated = storage.find_preference(pref.id)
    assert updated.title == "New pref title"


async def test_preference_update_description(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    pref = storage.create_preference("My preference")

    reply = await assistant.handle_message(
        chat_id="chat-1", text=f"update preference {pref.id} description Updated preference value here."
    )
    assert "Updated preference" in reply
    updated = storage.find_preference(pref.id)
    assert updated.body == "Updated preference value here."


# ---------------------------------------------------------------------------
# Time parsing fix: "10 am" should be 10:00, not 10:current-minute
# ---------------------------------------------------------------------------

def test_time_parsing_hour_only_zeros_minutes():
    """Specifying '10 am' should produce 10:00, not 10:<current-minute>."""
    from personal_assistant.time_utils import parse_user_datetime

    result = parse_user_datetime("10 am", "Asia/Singapore")
    assert result is not None
    assert result.hour == 10
    assert result.minute == 0


def test_time_parsing_explicit_minutes_preserved():
    """'10:30 am' should produce 10:30."""
    from personal_assistant.time_utils import parse_user_datetime

    result = parse_user_datetime("10:30 am", "Asia/Singapore")
    assert result is not None
    assert result.hour == 10
    assert result.minute == 30


# ---------------------------------------------------------------------------
# Numeric IDs in list views and commands
# ---------------------------------------------------------------------------

async def test_reminder_list_shows_numeric_ids(assistant_bundle, storage):
    storage.create_reminder("Buy milk", due_at=now_utc() + timedelta(hours=1))
    storage.create_reminder("Call doctor", due_at=now_utc() + timedelta(hours=2))
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="reminders")
    assert "1." in reply
    assert "2." in reply
    assert "Buy milk" in reply
    assert "Call doctor" in reply
    # Slug IDs must NOT appear in the list
    assert "reminder-" not in reply


async def test_cancel_reminder_by_number(assistant_bundle, storage):
    storage.create_reminder("Walk dog", due_at=now_utc() + timedelta(hours=1))
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="cancel reminder 1")
    assert "cancelled" in reply.lower()
    assert "no active reminders" in reply.lower()
    assert len(storage.list_reminders()) == 0


async def test_delete_number_after_adding_reminder_uses_visible_reminder_context(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    create_reply = await assistant.handle_message(chat_id="chat-1", text="remind me to call mum tomorrow 9am")
    delete_reply = await assistant.handle_message(chat_id="chat-1", text="delete 1")

    assert "Active reminders:" in create_reply
    assert "1. call mum" in create_reply.lower()
    assert "Cancelled reminder: call mum." in delete_reply
    assert storage.list_reminders() == []


async def test_cancel_reminder_pending_number_uses_active_reminder_not_inactive(assistant_bundle, storage):
    inactive = storage.create_reminder("do slides for project-beta", due_at=now_utc() + timedelta(hours=1))
    storage.cancel_reminder(inactive.id)
    active = storage.create_reminder("Call mum", due_at=now_utc() + timedelta(hours=2))
    assistant, _, _, _ = assistant_bundle

    prompt = await assistant.handle_message(chat_id="chat-1", text="delete reminder")
    reply = await assistant.handle_message(chat_id="chat-1", text="1")

    assert "which reminder" in prompt.lower()
    assert "1. Call mum" in prompt
    assert "Cancelled reminder: Call mum." in reply
    inactive_after = next(item for item in storage.list_reminders(include_inactive=True) if item.id == inactive.id)
    active_after = next(item for item in storage.list_reminders(include_inactive=True) if item.id == active.id)
    assert inactive_after.status == "cancelled"
    assert active_after.status == "cancelled"


async def test_cancel_reminder_by_name_is_case_insensitive_and_shows_list(assistant_bundle, storage):
    storage.create_reminder("Pay Rent", due_at=now_utc() + timedelta(hours=1))
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="delete reminder pay rent")

    assert "Cancelled reminder: Pay Rent." in reply
    assert "You have no active reminders." in reply


async def test_update_reminder_due_by_partial_text_shows_updated_list(assistant_bundle, storage, settings):
    assistant, _, _, _ = assistant_bundle
    reminder = storage.create_reminder("Pay Rent", due_at=now_utc() + timedelta(hours=1))

    reply = await assistant.handle_message(chat_id="chat-1", text="update reminder rent due tomorrow 9am")

    updated = next(item for item in storage.list_reminders(include_inactive=True) if item.id == reminder.id)
    expected = parse_user_datetime("tomorrow 9am", settings.default_timezone)
    assert expected is not None
    assert updated.due_at.year == expected.year
    assert updated.due_at.month == expected.month
    assert updated.due_at.day == expected.day
    assert updated.due_at.hour == expected.hour
    assert "Updated reminder: Pay Rent." in reply
    assert "Active reminders:" in reply
    assert "Tomorrow" in reply


async def test_ambiguous_numeric_reminder_date_asks_follow_up_then_creates(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    prompt = await assistant.handle_message(chat_id="chat-1", text="remind me to pay rent on 04/05/2026 9am")

    assert "which did you mean" in prompt.lower()
    assert storage.list_reminders() == []

    reply = await assistant.handle_message(chat_id="chat-1", text="05 Apr 2026 9am")

    assert "Reminder set" in reply
    reminders = storage.list_reminders()
    assert len(reminders) == 1
    assert reminders[0].due_at.month == 4
    assert reminders[0].due_at.day == 5


async def test_reminder_without_time_asks_follow_up_then_creates(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    prompt = await assistant.handle_message(chat_id="chat-1", text="remind me to pay rent tomorrow")

    assert "specific time" in prompt.lower()
    assert storage.list_reminders() == []

    reply = await assistant.handle_message(chat_id="chat-1", text="tomorrow 9am")

    assert "Reminder set" in reply
    reminders = storage.list_reminders()
    assert len(reminders) == 1
    assert reminders[0].due_at.hour == 9


async def test_update_reminder_text_renames_it(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    reminder = storage.create_reminder("Old Name", due_at=now_utc() + timedelta(hours=1))

    reply = await assistant.handle_message(chat_id="chat-1", text=f"update reminder {reminder.id} text New Name")

    updated = next(item for item in storage.list_reminders(include_inactive=True) if item.id == reminder.id)
    assert updated.text == "New Name"
    assert "Updated reminder: New Name." in reply


async def test_task_list_shows_numeric_ids(assistant_bundle, storage):
    storage.create_task("First task")
    storage.create_task("Second task")
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="tasks")
    assert "1." in reply
    assert "2." in reply
    assert "First task" in reply
    assert "Second task" in reply
    # Slug IDs must NOT appear in the list
    assert "first-task" not in reply


async def test_complete_task_by_number(assistant_bundle, storage):
    storage.create_task("Important thing")
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="done 1")
    assert "completed" in reply.lower()
    assert len(storage.list_tasks()) == 0


# ---------------------------------------------------------------------------
# Cancel all reminders
# ---------------------------------------------------------------------------

async def test_cancel_all_reminders_via_command(assistant_bundle, storage):
    storage.create_reminder("Reminder A", due_at=now_utc() + timedelta(hours=1))
    storage.create_reminder("Reminder B", due_at=now_utc() + timedelta(hours=2))
    storage.create_reminder("Reminder C", due_at=now_utc() + timedelta(hours=3))
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="cancel all reminders")
    assert "3" in reply
    assert "cancelled" in reply.lower()
    assert len(storage.list_reminders()) == 0


async def test_cancel_all_via_cancel_reminder_all(assistant_bundle, storage):
    storage.create_reminder("One", due_at=now_utc() + timedelta(hours=1))
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="cancel reminder all")
    assert "cancelled" in reply.lower()


# ---------------------------------------------------------------------------
# Non-recurring reminders: "sent" status and acknowledgement
# ---------------------------------------------------------------------------

async def test_non_recurring_reminder_marked_sent_after_firing(assistant_bundle, storage):
    storage.create_reminder("Take pills", due_at=now_utc() - timedelta(minutes=1))
    assistant, _, _, _ = assistant_bundle

    await assistant.send_due_reminders()

    reminders = storage.list_reminders(include_inactive=True)
    assert len(reminders) == 1
    assert reminders[0].status == "sent"
    assert reminders[0].acked_at is None


async def test_non_recurring_reminder_sends_ack_button(assistant_bundle, storage):
    storage.create_reminder("Water plants", due_at=now_utc() - timedelta(minutes=1))
    assistant, telegram, _, _ = assistant_bundle

    await assistant.send_due_reminders()

    assert len(telegram.sent_messages) == 1
    markup = telegram.sent_markups[0]
    assert markup is not None
    assert "inline_keyboard" in markup
    buttons = markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith("ack:")
    assert buttons[1]["callback_data"].startswith("snooze:")


async def test_reminder_ack_via_text_command(assistant_bundle, storage):
    storage.create_reminder("Read email", due_at=now_utc() - timedelta(minutes=1))
    assistant, _, _, _ = assistant_bundle

    # Fire the reminder (marks it as "sent")
    await assistant.send_due_reminders()

    # Now list the sent reminders; it won't appear in normal list
    sent = storage.list_reminders(include_inactive=True)
    assert sent[0].status == "sent"
    reminder_id = sent[0].id

    # Ack via text "ack reminder <id>"
    reply = await assistant.handle_message(chat_id="chat-1", text=f"ack reminder {reminder_id}")
    assert "acknowledged" in reply.lower()

    # Should now be completed
    final = storage.list_reminders(include_inactive=True)
    assert final[0].status == "completed"
    assert final[0].acked_at is not None


async def test_snooze_reminder_via_callback_and_follow_up_duration(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    reminder = storage.create_reminder("Read email", due_at=now_utc() - timedelta(minutes=1))

    await assistant.send_due_reminders()
    await assistant._handle_callback_query(
        {
            "id": "cb-1",
            "data": f"snooze:{reminder.id}",
            "from": {"id": assistant.settings.telegram_allowed_user_id},
            "message": {
                "message_id": 10,
                "chat": {"id": "chat-1"},
            },
        }
    )

    reply = await assistant.handle_message(chat_id="chat-1", text="10 minutes")

    assert "snoozed reminder" in reply.lower()
    updated = next(item for item in storage.list_reminders(include_inactive=True) if item.id == reminder.id)
    assert updated.status == "scheduled"
    assert updated.due_at > now_utc()


async def test_recurring_reminder_sends_without_ack_button(assistant_bundle, storage):
    """Recurring reminders advance automatically and don't get an ack button."""
    storage.create_reminder("Morning run", due_at=now_utc() - timedelta(minutes=1), recurrence="daily")
    assistant, telegram, _, _ = assistant_bundle

    await assistant.send_due_reminders()

    markup = telegram.sent_markups[0]
    assert markup is None  # No ack button for recurring reminders


async def test_resend_unacked_reminders_after_timeout(assistant_bundle, storage):
    """Unacked reminders should appear in list_sent_unacked_reminders after the timeout."""
    reminder = storage.create_reminder("Check oven", due_at=now_utc() - timedelta(minutes=2))
    assistant, telegram, _, _ = assistant_bundle

    # Fire the reminder
    await assistant.send_due_reminders()
    assert len(telegram.sent_messages) == 1
    assert storage.list_reminders(include_inactive=True)[0].status == "sent"

    # With ack_timeout_minutes=0, any recently-sent reminder is immediately "overdue"
    overdue = storage.list_sent_unacked_reminders(ack_timeout_minutes=0)
    assert len(overdue) == 1
    assert overdue[0].id == reminder.id


# ---------------------------------------------------------------------------
# Telegram menu: callback_query handling
# ---------------------------------------------------------------------------

async def test_callback_query_main_menu_navigation(assistant_bundle):
    """callback_query with menu:tasks should call edit_message_text."""
    assistant, telegram, _, _ = assistant_bundle

    update = {
        "update_id": 500,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": 12345},
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "message_id": 100,
                "text": "👋 What can I help you with?",
            },
            "data": "menu:tasks",
        },
    }
    await assistant.process_update(update)

    assert len(telegram.answered_callbacks) == 1
    assert telegram.answered_callbacks[0] == "cq-1"
    assert len(telegram.edited_messages) == 1
    _, msg_id, text = telegram.edited_messages[0]
    assert msg_id == 100
    assert "Tasks" in text


async def test_callback_query_dashboard_sends_current_overview(assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle
    storage.create_task("Buy groceries", priority="high")
    storage.create_reminder("Call mum", due_at=now_utc() + timedelta(hours=1))

    update = {
        "update_id": 5001,
        "callback_query": {
            "id": "cq-dashboard",
            "from": {"id": 12345},
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "message_id": 101,
                "text": "👋 What can I help you with?",
            },
            "data": "menu:dashboard",
        },
    }

    await assistant.process_update(update)

    assert "cq-dashboard" in telegram.answered_callbacks
    assert telegram.sent_messages
    assert "Open tasks:" in telegram.sent_messages[-1][1]
    assert "Active reminders:" in telegram.sent_messages[-1][1]


async def test_callback_query_reminder_cancel_all(assistant_bundle, storage):
    """cancel_all via callback should cancel reminders and answer the callback."""
    storage.create_reminder("Test", due_at=now_utc() + timedelta(hours=1))
    assistant, telegram, _, _ = assistant_bundle

    update = {
        "update_id": 501,
        "callback_query": {
            "id": "cq-2",
            "from": {"id": 12345},
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "message_id": 101,
                "text": "⏰ Reminders",
            },
            "data": "reminders:cancel_all",
        },
    }
    await assistant.process_update(update)

    assert len(storage.list_reminders()) == 0
    assert "cq-2" in telegram.answered_callbacks


async def test_callback_query_ack_reminder(assistant_bundle, storage):
    """Pressing 'Got it' on a fired reminder should ack it."""
    reminder = storage.create_reminder("Important", due_at=now_utc() - timedelta(minutes=1))
    assistant, telegram, _, _ = assistant_bundle

    await assistant.send_due_reminders()
    # Verify it's in "sent" state
    assert storage.list_reminders(include_inactive=True)[0].status == "sent"

    update = {
        "update_id": 502,
        "callback_query": {
            "id": "cq-3",
            "from": {"id": 12345},
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "message_id": 200,
                "text": "⏰ Reminder: Important",
            },
            "data": f"ack:{reminder.id}",
        },
    }
    await assistant.process_update(update)

    assert "cq-3" in telegram.answered_callbacks
    final = storage.list_reminders(include_inactive=True)[0]
    assert final.status == "completed"
    assert final.acked_at is not None


async def test_start_command_sends_menu(assistant_bundle):
    """/start and 'menu' commands should trigger the interactive button panel."""
    assistant, telegram, _, _ = assistant_bundle

    update = {
        "update_id": 600,
        "message": {
            "message_id": 300,
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "/start",
        },
    }
    await assistant.process_update(update)

    assert len(telegram.sent_messages) == 1
    assert "Use `show` for your dashboard" in telegram.sent_messages[0][1]
    markup = telegram.sent_markups[0]
    assert markup is not None
    assert "inline_keyboard" in markup
    button_texts = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert "🏠 Dashboard" in button_texts
    assert "📅 Calendar" in button_texts
    assert "👤 People" in button_texts
    assert "📚 Help" in button_texts


async def test_task_list_response_sends_reply_keyboard_shortcuts(assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle
    storage.create_task("Buy milk")

    update = {
        "update_id": 601,
        "message": {
            "message_id": 301,
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "tasks",
        },
    }
    await assistant.process_update(update)

    markup = telegram.sent_markups[-1]
    assert markup is not None
    assert "keyboard" in markup
    button_texts = [button["text"] for row in markup["keyboard"] for button in row]
    assert "new task" in button_texts
    assert "complete task" in button_texts
    assert "delete task" in button_texts


async def test_new_task_command_opens_task_prompt(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="new task")

    assert "enter the task title" in reply.lower()
    assert storage.get_pending("chat-1") == {"type": "task_input"}


async def test_new_reminder_command_opens_reminder_prompt(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="new reminder")

    assert "what reminder should i set" in reply.lower()
    assert storage.get_pending("chat-1") == {"type": "reminder_input"}


async def test_reminder_time_only_rolls_to_tomorrow_when_today_time_has_passed(assistant_bundle, storage, monkeypatch):
    assistant, _, _, _ = assistant_bundle

    fixed_now = lambda: time_utils_module.ensure_timezone(  # type: ignore[return-value]
        __import__("datetime").datetime(2026, 4, 8, 22, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Singapore")),
        "Asia/Singapore",
    )
    monkeypatch.setattr(time_utils_module, "now_utc", fixed_now)
    monkeypatch.setattr(storage_module, "now_utc", fixed_now)
    monkeypatch.setattr(assistant_module, "now_utc", fixed_now)

    reply = await assistant.handle_message(chat_id="chat-1", text="remind me to take food to the office at 9am")

    reminder = storage.list_reminders()[0]
    assert reminder.due_at.date().isoformat() == "2026-04-09"
    assert reminder.due_at.hour == 9
    assert "⏭️ Tomorrow 09:00 AM" in reply


async def test_late_night_tomorrow_is_treated_as_later_today_with_note(assistant_bundle, storage, monkeypatch):
    assistant, _, _, _ = assistant_bundle

    fixed_now = lambda: time_utils_module.ensure_timezone(  # type: ignore[return-value]
        __import__("datetime").datetime(2026, 4, 8, 1, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Singapore")),
        "Asia/Singapore",
    )
    monkeypatch.setattr(time_utils_module, "now_utc", fixed_now)
    monkeypatch.setattr(storage_module, "now_utc", fixed_now)
    monkeypatch.setattr(assistant_module, "now_utc", fixed_now)

    reply = await assistant.handle_message(chat_id="chat-1", text="remind me to take food to the office tomorrow 9am")

    reminder = storage.list_reminders()[0]
    assert reminder.due_at.date().isoformat() == "2026-04-08"
    assert reminder.due_at.hour == 9
    assert "before 4 am" in reply.lower()


async def test_reminder_list_response_sends_working_reply_keyboard_shortcuts(assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle
    storage.create_reminder("Call mum", due_at=now_utc() + timedelta(hours=1))

    update = {
        "update_id": 602,
        "message": {
            "message_id": 302,
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "reminders",
        },
    }
    await assistant.process_update(update)

    markup = telegram.sent_markups[-1]
    assert markup is not None
    assert "keyboard" in markup
    button_texts = [button["text"] for row in markup["keyboard"] for button in row]
    assert "new reminder" in button_texts
    assert "delete reminder" in button_texts
    assert "remind me to " not in button_texts


async def test_backup_recovered_notification_only_fires_after_failure(assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle
    worker = BackgroundWorker(assistant, assistant.backup_service)

    worker._last_backup_alert_status = "success"
    assistant.backup_service.read_status = lambda: {"status": "success"}  # type: ignore[method-assign]
    await worker._maybe_notify_backup_status()
    assert not telegram.sent_messages

    worker._last_backup_alert_status = "failed"
    await worker._maybe_notify_backup_status()
    assert telegram.sent_messages
    assert telegram.sent_messages[-1][1] == "✅ Backup recovered and completed successfully."


# ---------------------------------------------------------------------------
# Settings management
# ---------------------------------------------------------------------------

async def test_settings_update_ack_timeout(assistant_bundle, storage):
    """User should be able to update the ack timeout via text after pressing the settings button."""
    assistant, _, _, _ = assistant_bundle

    # Simulate the settings pending state (as if user clicked the button)
    storage.set_pending("chat-1", {"type": "settings_input", "setting": "ack_timeout"})
    reply = await assistant.handle_message(chat_id="chat-1", text="10")
    assert "10 minutes" in reply

    saved = storage.get_user_settings()
    assert saved.reminder_ack_timeout_minutes == 10


async def test_callback_query_people_menu_navigation(assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle

    update = {
        "update_id": 503,
        "callback_query": {
            "id": "cq-people",
            "from": {"id": 12345},
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "message_id": 111,
                "text": "👋 What can I help you with?",
            },
            "data": "menu:people",
        },
    }

    await assistant.process_update(update)

    assert "cq-people" in telegram.answered_callbacks
    assert any("People" in text for _, _, text in telegram.edited_messages)


async def test_callback_query_task_delete_sets_pending_prompt(assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle

    update = {
        "update_id": 504,
        "callback_query": {
            "id": "cq-task-delete",
            "from": {"id": 12345},
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "message_id": 112,
                "text": "📋 Tasks",
            },
            "data": "tasks:delete",
        },
    }

    await assistant.process_update(update)

    assert storage.get_pending("12345") == {"type": "task_delete_input"}
    assert any("delete" in text.lower() for _, text in telegram.sent_messages)


async def test_settings_update_poll_interval(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "settings_input", "setting": "poll"})
    reply = await assistant.handle_message(chat_id="chat-1", text="60")
    assert "60 seconds" in reply

    saved = storage.get_user_settings()
    assert saved.reminder_poll_seconds == 60


async def test_settings_update_requires_number(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "settings_input", "setting": "ack_timeout"})
    reply = await assistant.handle_message(chat_id="chat-1", text="not-a-number")
    assert "valid" in reply.lower()


async def test_settings_command_shows_current_values(assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    reply = await assistant.handle_message(chat_id="chat-1", text="settings")
    assert "settings" in reply.lower() or "ack" in reply.lower() or "poll" in reply.lower()


async def test_settings_defaults_follow_config(settings, storage):
    settings.reminder_ack_timeout_minutes = 9
    settings.reminder_poll_seconds = 45

    saved = storage.get_user_settings()

    assert saved.reminder_ack_timeout_minutes == 9
    assert saved.reminder_poll_seconds == 45


# ---------------------------------------------------------------------------
# Button-triggered task and reminder creation
# ---------------------------------------------------------------------------

async def test_task_input_pending_creates_task(assistant_bundle, storage):
    """After pressing 'New Task' button the next text message should create a task."""
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "task_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="Buy groceries #shopping")
    assert "Created task" in reply
    assert "Buy groceries" in reply
    tasks = storage.list_tasks()
    assert any("Buy groceries" in t.title for t in tasks)


async def test_reminder_input_pending_creates_reminder(assistant_bundle, storage):
    """After pressing 'New Reminder' button the next text message should create a reminder."""
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "reminder_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="Call dentist at 3pm")
    assert "Reminder set" in reply or "3" in reply
    assert "Open tasks:" in reply or "You have no open tasks." in reply
    assert "Active reminders:" in reply
    reminders = storage.list_reminders()
    assert any("Call dentist" in r.text for r in reminders)


async def test_semantic_reminder_parse_handles_natural_language(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle

    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="schedule a reminder for tomorrow at 9 am to pay rent",
    )

    assert "Reminder set" in reply
    reminders = storage.list_reminders()
    assert any(r.text == "pay rent" for r in reminders)
    assert planner.calls == []


async def test_reminder_creation_accepts_word_and_shorthand_relative_times(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle

    word_reply = await assistant.handle_message(chat_id="chat-1", text="remind me to email zhenkai in one hour")
    shorthand_reply = await assistant.handle_message(chat_id="chat-1", text="remind me to stretch 30m")

    reminders = storage.list_reminders()
    assert "Reminder set" in word_reply
    assert "Reminder set" in shorthand_reply
    assert any(reminder.text == "email zhenkai" for reminder in reminders)
    assert any(reminder.text == "stretch" for reminder in reminders)
    assert planner.calls == []


async def test_reminder_creation_preserves_dotted_and_compact_time_minutes(
    assistant_bundle,
    storage,
    monkeypatch,
):
    assistant, _, _, _ = assistant_bundle

    fixed_now = lambda: time_utils_module.ensure_timezone(  # type: ignore[return-value]
        __import__("datetime").datetime(2026, 4, 8, 8, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Singapore")),
        "Asia/Singapore",
    )
    monkeypatch.setattr(time_utils_module, "now_utc", fixed_now)
    monkeypatch.setattr(storage_module, "now_utc", fixed_now)
    monkeypatch.setattr(assistant_module, "now_utc", fixed_now)

    dotted_reply = await assistant.handle_message(
        chat_id="chat-1",
        text="remind me about switching arriving at 12.55pm",
    )
    compact_reply = await assistant.handle_message(chat_id="chat-1", text="remind me to call at 0940")

    reminders = storage.list_reminders()
    dotted = next(reminder for reminder in reminders if reminder.text == "about switching arriving")
    compact = next(reminder for reminder in reminders if reminder.text == "call")
    assert "12:55 PM" in dotted_reply
    assert dotted.due_at.hour == 12
    assert dotted.due_at.minute == 55
    assert "09:40 AM" in compact_reply
    assert compact.due_at.hour == 9
    assert compact.due_at.minute == 40


async def test_reminder_button_workflow_prompts_for_missing_time_then_accepts_shorthand(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "reminder_input"})

    prompt = await assistant.handle_message(chat_id="chat-1", text="Call dentist")
    reply = await assistant.handle_message(chat_id="chat-1", text="30m")

    assert "when should i remind" in prompt.lower()
    assert "Reminder set" in reply
    reminders = storage.list_reminders()
    assert len(reminders) == 1
    assert reminders[0].text == "Call dentist"


async def test_delete_task_number_from_show_list_and_numeric_id_without_context(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Keep first")
    numeric_id_task = storage.create_task("1")

    id_reply = await assistant.handle_message(chat_id="chat-1", text="delete task 1")
    storage.create_task("Delete from show")
    await assistant.handle_message(chat_id="chat-1", text="show")
    list_reply = await assistant.handle_message(chat_id="chat-1", text="delete task 2")

    assert f"Deleted task: {numeric_id_task.title}." in id_reply
    assert "Deleted task: Delete from show." in list_reply
    assert storage.find_task("Keep first") is not None
    assert storage.find_task(numeric_id_task.id) is None
    assert storage.find_task("Delete from show") is None


async def test_current_task_command_marks_task_and_sorts_it_first(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("High priority", priority="high")
    later = storage.create_task("Low but current", priority="low")

    reply = await assistant.handle_message(chat_id="chat-1", text="current 2")
    list_reply = await assistant.handle_message(chat_id="chat-1", text="tasks")

    updated = storage.find_task(later.id)
    assert updated is not None
    assert updated.is_current is True
    assert "Current task: Low but current." in reply
    assert list_reply.splitlines()[1].startswith("1. ⭐ Current")
    assert "Low but current" in list_reply.splitlines()[1]


async def test_note_update_guided_workflow_from_command(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    note = storage.create_note("Old note", "Old body")

    prompt = await assistant.handle_message(chat_id="chat-1", text="update note")
    field_prompt = await assistant.handle_message(chat_id="chat-1", text="1")
    reply = await assistant.handle_message(chat_id="chat-1", text="description New body")

    updated = storage.find_note(note.id)
    assert "which note should i update" in prompt.lower()
    assert "what should i update" in field_prompt.lower()
    assert "Updated note: Old note." in reply
    assert updated is not None
    assert updated.body == "New body"


async def test_show_response_keyboard_includes_task_and_reminder_actions(assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle
    storage.create_task("Buy milk")
    storage.create_reminder("Call mum", due_at=now_utc() + timedelta(hours=1))

    await assistant.process_update(
        {
            "update_id": 700,
            "message": {
                "message_id": 400,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345},
                "text": "show",
            },
        }
    )

    markup = telegram.sent_markups[-1]
    assert markup is not None
    button_texts = [button["text"] for row in markup["keyboard"] for button in row]
    assert "delete task" in button_texts
    assert "new reminder" in button_texts
    assert "delete reminder" in button_texts


async def test_semantic_reminder_parse_handles_sloppy_order_and_abbreviations(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="tmr 9am remind me pay rent")

    assert "Reminder set" in reply
    reminders = storage.list_reminders()
    assert any(r.text == "pay rent" for r in reminders)


def test_semantic_parser_does_not_treat_course_code_alpha_as_due_time():
    from personal_assistant.semantic_parser import parse_semantic_match

    match = parse_semantic_match("i need to prep alpha lecture", "Asia/Singapore")
    reminder_match = parse_semantic_match("remind me to prep alpha tomorrow", "Asia/Singapore")

    assert match is not None
    assert match.plan.action == "create_task"
    assert match.plan.args["title"] == "prep alpha lecture"
    assert match.plan.args["due_at"] is None
    assert reminder_match is not None
    assert reminder_match.plan.action == "create_reminder"
    assert reminder_match.plan.args["text"] == "prep alpha"
    assert reminder_match.plan.args["due_at"] == "tomorrow"


async def test_reminder_command_accepts_natural_text_after_command_prefix(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="remind me tomorrow at 9am to call dad")

    assert "Reminder set" in reply
    reminders = storage.list_reminders()
    assert any(r.text == "call dad" for r in reminders)


async def test_pending_reminder_input_accepts_semantic_phrase(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "reminder_input"})

    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="please ping me about rent tomorrow morning at 9",
    )

    assert "Reminder set" in reply
    assert "Active reminders:" in reply
    reminders = storage.list_reminders()
    assert any(r.text == "rent" for r in reminders)


async def test_semantic_event_parse_handles_calendar_language(assistant_bundle):
    assistant, _, calendar, planner = assistant_bundle

    reply = await assistant.handle_message(
        chat_id="chat-1",
        text="put demo review on my calendar tomorrow 3pm for 45 minutes",
    )

    assert "Created event" in reply
    assert calendar.created_events[-1].title == "demo review"
    assert int((calendar.created_events[-1].end - calendar.created_events[-1].start).total_seconds()) == 45 * 60
    assert planner.calls == []


async def test_semantic_list_reminders_understands_question_form(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    storage.create_reminder("Buy milk", due_at=now_utc() + timedelta(hours=1))

    reply = await assistant.handle_message(chat_id="chat-1", text="what reminders do i have?")

    assert "Active reminders:" in reply
    assert "Buy milk" in reply
    assert planner.calls == []


async def test_semantic_task_create_handles_obligation_language(assistant_bundle, storage, settings):
    assistant, _, _, planner = assistant_bundle

    reply = await assistant.handle_message(chat_id="chat-1", text="i need to buy groceries tomorrow")

    assert "Created task" in reply
    assert "Open tasks:" in reply
    task = storage.list_tasks()[0]
    assert task.title == "buy groceries"
    expected = parse_user_datetime("tomorrow", settings.default_timezone)
    assert expected is not None
    assert task.due_at is not None
    assert task.due_at.date() == expected.date()
    assert planner.calls == []


async def test_update_task_due_does_not_create_new_task(assistant_bundle, storage, settings):
    assistant, _, _, planner = assistant_bundle
    original = storage.create_task("Demo report")

    reply = await assistant.handle_message(chat_id="chat-1", text="update task Demo report due thursday")

    updated = storage.find_task(original.id)
    assert updated is not None
    assert "Updated task: Demo report." in reply
    assert len(storage.list_tasks(include_completed=True)) == 1
    expected = parse_user_datetime("thursday", settings.default_timezone)
    assert expected is not None
    assert updated.due_at is not None
    assert updated.due_at.date() == expected.date()
    assert planner.calls == []


async def test_semantic_list_tasks_understands_question_form(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    storage.create_task("Buy groceries")

    reply = await assistant.handle_message(chat_id="chat-1", text="show me my tasks")

    assert "Open tasks" in reply
    assert "Buy groceries" in reply
    assert planner.calls == []


async def test_contextual_delete_it_after_viewing_task(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    storage.create_task("Buy groceries")

    await assistant.handle_message(chat_id="chat-1", text="show task buy groceries")
    reply = await assistant.handle_message(chat_id="chat-1", text="delete it")

    assert "Deleted task" in reply
    assert storage.list_tasks() == []
    assert planner.calls == []


async def test_contextual_complete_first_item_after_listing_tasks(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.create_task("Buy groceries")
    storage.create_task("Pay rent")

    await assistant.handle_message(chat_id="chat-1", text="tasks")
    reply = await assistant.handle_message(chat_id="chat-1", text="finish the first one")

    assert "completed" in reply.lower()
    open_titles = [task.title for task in storage.list_tasks()]
    assert "Buy groceries" not in open_titles
    assert "Pay rent" in open_titles


async def test_low_confidence_semantic_guess_asks_for_confirmation(assistant_bundle, storage):
    assistant, _, _, planner = assistant_bundle
    storage.create_task("Buy groceries")

    await assistant.handle_message(chat_id="chat-1", text="show task buy groceries")
    prompt = await assistant.handle_message(chat_id="chat-1", text="delte it")

    assert "I think you want me to delete" in prompt
    assert storage.list_tasks()
    assert planner.calls == []

    reply = await assistant.handle_message(chat_id="chat-1", text="yes")
    assert "Deleted task" in reply
    assert storage.list_tasks() == []


async def test_numeric_done_uses_visible_open_task_numbering_not_hidden_completed_tasks(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle

    storage.create_task("id 1")
    storage.complete_task("id 1")
    for index in range(1, 10):
        storage.create_task(f"Visible task {index}")

    reply = await assistant.handle_message(chat_id="chat-1", text="done 9")

    assert "Visible task 9" in reply
    open_titles = [task.title for task in storage.list_tasks()]
    assert "Visible task 9" not in open_titles


async def test_reminder_input_pending_stays_active_after_parse_error(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "reminder_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="this is not a reminder")

    assert "try again" in reply.lower()
    assert storage.get_pending("chat-1") == {"type": "reminder_input"}


async def test_pending_flow_can_be_cancelled(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "reminder_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="/cancel")

    assert "cancelled" in reply.lower()
    assert storage.get_pending("chat-1") is None


async def test_free_slot_uses_configured_working_hours(settings, storage):
    captured: dict[str, object] = {}

    class RecordingCalendar:
        async def list_upcoming_events(self, *, limit: int = 5, time_min=None, time_max=None):
            return []

        async def create_event(self, *, title: str, start, end, description: str = "", metadata=None):
            raise AssertionError("not used")

        async def update_owned_event(self, identifier: str, *, title=None, start=None, end=None, description=None):
            raise AssertionError("not used")

        async def find_free_slots(self, *, window_start, window_end, duration_minutes: int):
            captured["window_start"] = window_start
            captured["window_end"] = window_end
            captured["duration_minutes"] = duration_minutes
            return []

        async def validate(self):
            return True

    assistant = AssistantService(
        settings,
        storage,
        AssistantDependencies(
            telegram=type("DummyTelegram", (), {"send_message": None})(),  # unused
            calendar=RecordingCalendar(),
            planner=type("DummyPlanner", (), {"plan": None})(),  # unused
        ),
    )
    settings.free_slot_start_hour = 9
    settings.free_slot_end_hour = 18

    reply = await assistant.handle_message(chat_id="chat-1", text="find a 30 minute free slot")

    assert "30-minute slot" in reply.lower()
    assert captured["duration_minutes"] == 30
    assert captured["window_start"].hour == 9
    assert captured["window_end"].hour == 18


async def test_free_slot_respects_requested_day(settings, storage):
    captured: dict[str, object] = {}

    class RecordingCalendar:
        async def list_upcoming_events(self, *, limit: int = 5, time_min=None, time_max=None):
            return []

        async def create_event(self, *, title: str, start, end, description: str = "", metadata=None):
            raise AssertionError("not used")

        async def update_owned_event(self, identifier: str, *, title=None, start=None, end=None, description=None):
            raise AssertionError("not used")

        async def find_free_slots(self, *, window_start, window_end, duration_minutes: int):
            captured["window_start"] = window_start
            captured["window_end"] = window_end
            return []

        async def validate(self):
            return True

    assistant = AssistantService(
        settings,
        storage,
        AssistantDependencies(
            telegram=type("DummyTelegram", (), {"send_message": None})(),  # unused
            calendar=RecordingCalendar(),
            planner=type("DummyPlanner", (), {"plan": None})(),  # unused
        ),
    )

    await assistant.handle_message(chat_id="chat-1", text="find a 30 minute free slot tomorrow")

    expected = parse_user_datetime("tomorrow", settings.default_timezone)
    assert expected is not None
    assert captured["window_start"].date() == expected.date()


async def test_free_slot_accepts_explicit_date_range(settings, storage):
    captured: dict[str, object] = {}

    class RecordingCalendar:
        async def list_upcoming_events(self, *, limit: int = 5, time_min=None, time_max=None):
            return []

        async def create_event(self, *, title: str, start, end, description: str = "", metadata=None):
            raise AssertionError("not used")

        async def update_owned_event(self, identifier: str, *, title=None, start=None, end=None, description=None):
            raise AssertionError("not used")

        async def find_free_slots(self, *, window_start, window_end, duration_minutes: int):
            captured["window_start"] = window_start
            captured["window_end"] = window_end
            captured["duration_minutes"] = duration_minutes
            return []

        async def validate(self):
            return True

    assistant = AssistantService(
        settings,
        storage,
        AssistantDependencies(
            telegram=type("DummyTelegram", (), {"send_message": None})(),  # unused
            calendar=RecordingCalendar(),
            planner=type("DummyPlanner", (), {"plan": None})(),  # unused
        ),
    )

    reply = await assistant.handle_message(chat_id="chat-1", text="find a 30 minute free slot between tomorrow 2pm and 5pm")

    assert "couldn't find a free 30-minute slot" in reply.lower()
    assert captured["duration_minutes"] == 30
    assert captured["window_start"].hour == 14
    assert captured["window_end"].hour == 17


async def test_note_input_pending_creates_note(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "note_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="Meeting notes: discussed Q3")
    assert "Saved note" in reply


async def test_plan_input_pending_creates_plan(assistant_bundle, storage):
    assistant, _, _, _ = assistant_bundle
    storage.set_pending("chat-1", {"type": "plan_input"})

    reply = await assistant.handle_message(chat_id="chat-1", text="Exercise plan: gym 3x week")
    assert "Saved plan" in reply


async def test_private_task_flow_requires_password_and_stays_off_public_views(assistant_bundle, settings, storage):
    settings.people_encryption_key = "secret123"
    assistant, _, _, _ = assistant_bundle

    prompt = await assistant.handle_message(chat_id="chat-1", text="pt Renew passport priority high")
    assert "password" in prompt.lower()

    reply = await assistant.handle_message(chat_id="chat-1", text="secret123")
    assert "Created private task" in reply
    assert "Renew passport" in reply

    private_tasks = storage.list_private_tasks("secret123")
    assert len(private_tasks) == 1
    assert private_tasks[0].title == "Renew passport"
    assert private_tasks[0].priority == "high"

    public_show = await assistant.handle_message(chat_id="chat-1", text="show")
    assert "Renew passport" not in public_show

    public_tasks = await assistant.handle_message(chat_id="chat-1", text="tasks")
    assert "Renew passport" not in public_tasks

    pts_prompt = await assistant.handle_message(chat_id="chat-1", text="pts")
    assert "password" in pts_prompt.lower()
    pts_reply = await assistant.handle_message(chat_id="chat-1", text="secret123")
    assert "Private tasks:" in pts_reply
    assert "Renew passport" in pts_reply


async def test_update_private_task_after_password(assistant_bundle, settings, storage):
    settings.people_encryption_key = "secret123"
    assistant, _, _, _ = assistant_bundle
    storage.create_private_task("Budget review", password="secret123")

    prompt = await assistant.handle_message(chat_id="chat-1", text="update pt budget priority high")
    assert "password" in prompt.lower()

    reply = await assistant.handle_message(chat_id="chat-1", text="secret123")
    assert "Updated private task" in reply
    assert "Budget review" in reply

    private_task = storage.list_private_tasks("secret123")[0]
    assert private_task.priority == "high"


async def test_private_reminder_due_notification_is_generic_and_reveals_after_password(assistant_bundle, settings, storage):
    settings.people_encryption_key = "secret123"
    assistant, telegram, _, _ = assistant_bundle
    reminder = storage.create_private_reminder(
        "Pay rent",
        password="secret123",
        due_at=now_utc() - timedelta(minutes=1),
    )

    sent = await assistant.send_due_reminders()

    assert sent == 1
    assert any(text == "🔐 You have a private reminder." for _, text in telegram.sent_messages)
    generic_markup = next(markup for markup in telegram.sent_markups if markup)
    assert generic_markup["inline_keyboard"][0][0]["callback_data"] == f"private_reveal:{reminder.id}"

    await assistant._handle_callback_query(
        {
            "id": "callback-1",
            "from": {"id": assistant.settings.telegram_allowed_user_id},
            "message": {
                "message_id": 88,
                "chat": {"id": "chat-1", "type": "private"},
            },
            "data": f"private_reveal:{reminder.id}",
        }
    )

    pending = storage.get_pending("chat-1")
    assert pending is not None
    assert pending["type"] == "private_auth"

    reply = await assistant.handle_message(chat_id="chat-1", text="secret123")

    assert reply == "Private reminder revealed."
    assert any("Pay rent" in text for _, text in telegram.sent_messages)
    revealed_markup = [markup for markup in telegram.sent_markups if markup and markup["inline_keyboard"][0][0]["callback_data"].startswith("ack:")]
    assert revealed_markup

    public_reminders = await assistant.handle_message(chat_id="chat-1", text="reminders")
    assert "Pay rent" not in public_reminders
