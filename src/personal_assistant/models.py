from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TaskStatus = Literal["open", "completed"]
TaskPriority = Literal["low", "medium", "high"]
TaskKind = Literal["self", "other"]
ReminderStatus = Literal["scheduled", "sent", "completed", "cancelled"]
ReminderSourceType = Literal["task", "standalone", "private_task"]
AssistantAction = Literal[
    "reply",
    "show_ai_status",
    "show_ai_running",
    "show_ai_trace",
    "show_ai_prompt",
    "show_ai_context",
    "show_ai_traces",
    "show_ai_backlog",
    "show_ai_usage",
    "show_ai_costs",
    "show_ai_credits",
    "show_debug_info",
    "show_system_health",
    "prompt_task_create",
    "prompt_task_complete",
    "prompt_task_delete",
    "prompt_task_current",
    "prompt_reminder_create",
    "prompt_reminder_delete",
    "prompt_note_create",
    "prompt_note_delete",
    "prompt_note_update",
    "prompt_calendar_create",
    "prompt_free_slot",
    "append_project",
    "create_project",
    "list_projects",
    "view_project",
    "delete_project",
    "update_project",
    "create_task",
    "list_tasks",
    "view_task",
    "complete_task",
    "delete_task",
    "make_task_current",
    "update_task",
    "retag_tasks",
    "merge_tasks",
    "create_private_task",
    "list_private_tasks",
    "view_private_task",
    "complete_private_task",
    "delete_private_task",
    "update_private_task",
    "create_reminder",
    "list_reminders",
    "update_reminder",
    "cancel_reminder",
    "cancel_all_reminders",
    "ack_reminder",
    "create_private_reminder",
    "list_private_reminders",
    "view_private_reminder",
    "update_private_reminder",
    "cancel_private_reminder",
    "ack_private_reminder",
    "upcoming_events",
    "find_free_slot",
    "create_calendar_event",
    "update_calendar_event",
    "append_note",
    "create_note",
    "list_notes",
    "view_note",
    "delete_note",
    "update_note",
    "append_plan",
    "create_plan",
    "list_plans",
    "view_plan",
    "delete_plan",
    "update_plan",
    "append_preference",
    "create_preference",
    "list_preferences",
    "view_preference",
    "delete_preference",
    "update_preference",
    "add_person",
    "list_people",
    "edit_person",
    "delete_person",
    "force_git_sync",
    "undo",
    "show_help",
    "show_all",
    "show_menu",
    "show_settings",
]


class AppModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskRecord(AppModel):
    id: str
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    kind: TaskKind = "self"
    status: TaskStatus = "open"
    priority: TaskPriority = "medium"
    is_current: bool = False
    created_at: datetime
    updated_at: datetime
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    calendar_event_id: str | None = None
    completed_at: datetime | None = None


class TaskSummary(AppModel):
    id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    kind: TaskKind = "self"
    status: TaskStatus
    priority: TaskPriority = "medium"
    is_current: bool = False
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    calendar_event_id: str | None = None
    updated_at: datetime


class ReminderRecord(AppModel):
    id: str
    text: str
    due_at: datetime
    status: ReminderStatus = "scheduled"
    recurrence: str | None = None
    source_type: ReminderSourceType = "standalone"
    source_id: str | None = None
    last_sent_at: datetime | None = None
    acked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OpenClawStatusResponse(AppModel):
    status: Literal["ok"] = "ok"
    api_version: Literal["v1"] = "v1"
    capabilities: list[str] = Field(default_factory=lambda: ["tasks:read", "reminders:read"])


class OpenClawTaskResponse(AppModel):
    id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    priority: TaskPriority
    is_current: bool
    due_at: datetime | None = None
    reminder_at: datetime | None = None


class OpenClawTasksResponse(AppModel):
    tasks: list[OpenClawTaskResponse]
    count: int


class OpenClawReminderResponse(AppModel):
    id: str
    text: str
    due_at: datetime
    recurrence: str | None = None
    source_type: Literal["task", "standalone"]
    source_id: str | None = None


class OpenClawRemindersResponse(AppModel):
    reminders: list[OpenClawReminderResponse]
    count: int


class TelegramState(AppModel):
    processed_update_ids: list[int] = Field(default_factory=list)
    last_message_by_chat: dict[str, datetime] = Field(default_factory=dict)
    pending_by_chat: dict[str, Any] = Field(default_factory=dict)
    undo_by_chat: dict[str, Any] = Field(default_factory=dict)
    references_by_chat: dict[str, Any] = Field(default_factory=dict)
    ai_runs_by_chat: dict[str, Any] = Field(default_factory=dict)


class ConversationSummary(AppModel):
    chat_id: str
    summary: str
    updated_at: datetime


class OpenAITraceRecord(AppModel):
    id: str
    chat_id: str = ""
    created_at: datetime
    mode: str = "planner"
    user_message: str = ""
    summary: str = ""
    context_snippets: list[str] = Field(default_factory=list)
    model: str = ""
    base_url: str = ""
    request_payload: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str = ""
    user_prompt: str = ""
    response_content: str = ""
    parsed_plan: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    response_headers: dict[str, str] = Field(default_factory=dict)
    rate_limits: dict[str, str] = Field(default_factory=dict)
    tool_steps: list[dict[str, Any]] = Field(default_factory=list)
    final_response: str = ""
    automation_candidate: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""
    error: str = ""
    duration_ms: int | None = None


class CalendarEventSummary(AppModel):
    id: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    owned_by_assistant: bool = False


class FreeSlot(AppModel):
    start: datetime
    end: datetime


class AssistantPlan(AppModel):
    action: AssistantAction = "reply"
    args: dict[str, Any] = Field(default_factory=dict)
    response: str | None = None


class NoteRecord(AppModel):
    id: str
    title: str
    body: str = ""
    created_at: datetime
    updated_at: datetime


class PlanRecord(AppModel):
    id: str
    title: str
    body: str = ""
    created_at: datetime
    updated_at: datetime


class ProjectRecord(AppModel):
    id: str
    title: str
    body: str = ""
    created_at: datetime
    updated_at: datetime


class PreferenceRecord(AppModel):
    id: str
    title: str
    body: str = ""
    created_at: datetime
    updated_at: datetime


class UserSettings(AppModel):
    reminder_ack_timeout_minutes: int = 5
    reminder_poll_seconds: int = 30
