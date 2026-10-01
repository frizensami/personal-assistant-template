from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from difflib import get_close_matches
from pathlib import Path
from typing import Any
from uuid import uuid4

from personal_assistant import __version__
from personal_assistant.backup import GitBackupService
from personal_assistant.calendar_client import GoogleCalendarClient
from personal_assistant.config import Settings
from personal_assistant.models import AssistantPlan, OpenAITraceRecord, ReminderRecord, TaskRecord, UserSettings
from personal_assistant.openai_client import OpenAIPlanner
from personal_assistant.semantic_parser import (
    SemanticMatch,
    parse_event_request,
    parse_reminder_request,
    parse_semantic_match,
)
from personal_assistant.storage import Storage
from personal_assistant.telegram_client import TelegramClient
from personal_assistant.time_utils import (
    format_local,
    format_note_local,
    interpret_user_datetime,
    interpret_user_datetime_range,
    local_day_window,
    now_utc,
    parse_duration_minutes,
    parse_user_datetime,
)

_MIN_AUTO_SYNC_INTERVAL_SECONDS = 60 * 60

TASK_CREATE_RE = re.compile(r"^(?:/task|task|add task|t)\s+(?P<rest>.+)$", re.IGNORECASE)
OTHER_TASK_CREATE_RE = re.compile(r"^(?:ot|other task|others task|others' task)\s+(?P<rest>.+)$", re.IGNORECASE)
PRIVATE_TASK_CREATE_RE = re.compile(r"^(?:/pt|pt|private task)\s+(?P<rest>.+)$", re.IGNORECASE)
TASK_DELETE_RE = re.compile(r"^(?:delete task|remove task|trash task)\s+(?P<identifier>.+)$", re.IGNORECASE)
TASK_CURRENT_RE = re.compile(r"^(?:curr|current)\s+(?:task\s+)?(?P<identifier>.+)$", re.IGNORECASE)
TASK_CURRENT_SUFFIX_RE = re.compile(
    r"^(?:make|set)\s+(?:task\s+)?(?P<identifier>.+?)\s+current$",
    re.IGNORECASE,
)
PRIVATE_TASK_DELETE_RE = re.compile(
    r"^(?:delete|remove|trash)\s+(?:private task|pt)\s+(?P<identifier>.+)$",
    re.IGNORECASE,
)
TASK_COMPLETE_RE = re.compile(r"^(?:d|done|complete(?: task)?|finish)\s+(?P<identifier>.+)$", re.IGNORECASE)
PRIVATE_TASK_COMPLETE_RE = re.compile(
    r"^(?:done|complete|finish)\s+(?:private task|pt)\s+(?P<identifier>.+)$",
    re.IGNORECASE,
)
TASK_UPDATE_RE = re.compile(
    r"^(?:update|change|set)\s+task\s+(?P<identifier>.+?)\s+"
    r"(?P<field>title|description|tags|priority|due|remind(?: me)?|reminder)\s+"
    r"(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
TASK_RETAG_RE = re.compile(
    r"^(?:re-?tag|change tag|replace tag|update tag)\s+"
    r"(?:(?:all\s+)?tasks?\s+)?(?:from\s+|that\s+have\s+(?:the\s+)?tag\s+)?#?(?P<old_tag>[\w-]+)"
    r"(?:\s*,)?\s+(?:to|with)\s+#?(?P<new_tag>[\w-]+)$",
    re.IGNORECASE,
)
TASK_RETAG_VERBOSE_RE = re.compile(
    r"^re-?tag\s+all\s+tasks?\s+that\s+have\s+(?:the\s+)?tag\s+#?(?P<old_tag>[\w-]+)"
    r"\s*,?\s+to\s+(?:the\s+)?tag\s+#?(?P<new_tag>[\w-]+)$",
    re.IGNORECASE,
)
TASK_MERGE_RE = re.compile(
    r"^(?:combine|merge)\s+(?P<first>.+?)\s+and\s+(?P<second>.+?)\s+"
    r"(?:tasks?\s+)?into\s+(?P<title>.+)$",
    re.IGNORECASE,
)
PRIVATE_TASK_UPDATE_RE = re.compile(
    r"^(?:update|change|set)\s+(?:private task|pt)\s+(?P<identifier>.+?)\s+"
    r"(?P<field>title|description|tags|priority|due|remind(?: me)?|reminder)\s+"
    r"(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
VIEW_TASK_RE = re.compile(r"^(?:view task|show task)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
VIEW_PRIVATE_TASK_RE = re.compile(r"^(?:view|show)\s+(?:private task|pt)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
REMINDER_CREATE_RE = re.compile(
    r"^(?:/remind|remind me(?: to)?|add reminder|r)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
PRIVATE_REMINDER_CREATE_RE = re.compile(
    r"^(?:/pr|pr|private reminder)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
REMINDER_UPDATE_RE = re.compile(
    r"^(?:update|change|set)\s+reminder\s+(?P<identifier>.+?)\s+"
    r"(?P<field>text|title|due|time|at|for|on|recurrence|repeat)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
PRIVATE_REMINDER_UPDATE_RE = re.compile(
    r"^(?:update|change|set)\s+(?:private reminder|pr)\s+(?P<identifier>.+?)\s+"
    r"(?P<field>text|title|due|time|at|for|on|recurrence|repeat)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
REMINDER_RESCHEDULE_RE = re.compile(
    r"^(?:move|reschedule|shift)\s+reminder\s+(?P<identifier>.+?)\s+(?:to|at|for|on)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
PRIVATE_REMINDER_RESCHEDULE_RE = re.compile(
    r"^(?:move|reschedule|shift)\s+(?:private reminder|pr)\s+(?P<identifier>.+?)\s+(?:to|at|for|on)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
REMINDER_CANCEL_RE = re.compile(
    r"^(?:cancel reminder|remove reminder|delete reminder)\s+(?P<identifier>.+)$",
    re.IGNORECASE,
)
PRIVATE_REMINDER_CANCEL_RE = re.compile(
    r"^(?:cancel|remove|delete)\s+(?:private reminder|pr)\s+(?P<identifier>.+)$",
    re.IGNORECASE,
)
REMINDER_ACK_RE = re.compile(
    r"^(?:ack|acknowledge|got it|done)\s+(?:reminder\s+)?(?P<identifier>\S[^\n]*)$",
    re.IGNORECASE,
)
PRIVATE_REMINDER_ACK_RE = re.compile(
    r"^(?:ack|acknowledge|got it|done)\s+(?:private reminder|pr)\s+(?P<identifier>\S[^\n]*)$",
    re.IGNORECASE,
)
VIEW_PRIVATE_REMINDER_RE = re.compile(
    r"^(?:view|show)\s+(?:private reminder|pr)\s+(?P<identifier>\S[^\n]*)$",
    re.IGNORECASE,
)
NOTE_APPEND_RE = re.compile(r"^(?:capture note|append note|inbox)\s+(?P<text>\S[^\n]*)$", re.IGNORECASE)
NOTE_RE = re.compile(r"^(?:note|add note|new note|n)\s+(?P<rest>\S[^\n]*)$", re.IGNORECASE)
NOTE_DELETE_RE = re.compile(r"^(?:delete note|remove note)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
NOTE_UPDATE_RE = re.compile(
    r"^update note\s+(?P<identifier>.+?)\s+(?P<field>title|description)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
VIEW_NOTE_RE = re.compile(r"^(?:view note|show note)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
PLAN_APPEND_RE = re.compile(r"^(?:append plan)\s+(?P<text>\S[^\n]*)$", re.IGNORECASE)
PLAN_RE = re.compile(r"^(?:plan|add plan|new plan)\s+(?P<rest>\S[^\n]*)$", re.IGNORECASE)
PLAN_DELETE_RE = re.compile(r"^(?:delete plan|remove plan)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
PLAN_UPDATE_RE = re.compile(
    r"^update plan\s+(?P<identifier>.+?)\s+(?P<field>title|description)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
VIEW_PLAN_RE = re.compile(r"^(?:view plan|show plan)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
PROJECT_APPEND_RE = re.compile(r"^(?:append project)\s+(?P<text>\S[^\n]*)$", re.IGNORECASE)
PROJECT_RE = re.compile(r"^(?:project|add project|new project|p)\s+(?P<rest>\S[^\n]*)$", re.IGNORECASE)
PROJECT_DELETE_RE = re.compile(r"^(?:delete project|remove project)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
PROJECT_UPDATE_RE = re.compile(
    r"^update project\s+(?P<identifier>.+?)\s+(?P<field>title|description)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
VIEW_PROJECT_RE = re.compile(r"^(?:view project|show project)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
PREFERENCE_APPEND_RE = re.compile(r"^(?:remember)\s+(?P<text>\S[^\n]*)$", re.IGNORECASE)
PREFERENCE_RE = re.compile(r"^(?:preference|add preference|new preference|pref)\s+(?P<rest>\S[^\n]*)$", re.IGNORECASE)
PREFERENCE_DELETE_RE = re.compile(r"^(?:delete preference|remove preference)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
PREFERENCE_UPDATE_RE = re.compile(
    r"^update preference\s+(?P<identifier>.+?)\s+(?P<field>title|description)\s+(?P<value>\S[^\n]*)$",
    re.IGNORECASE,
)
VIEW_PREFERENCE_RE = re.compile(r"^(?:view preference|show preference)\s+(?P<identifier>\S[^\n]*)$", re.IGNORECASE)
DELETE_ALL_RE = re.compile(
    r"^(?:delete|remove|clear)\s+all\s+(?P<entity>tasks?|notes?|plans?|projects?|preferences?|prefs?|reminders?)$",
    re.IGNORECASE,
)
EVENT_CREATE_RE = re.compile(
    r"^(?:schedule|create event)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
EVENT_UPDATE_RE = re.compile(
    r"^(?:move event|update event)\s+(?P<identifier>.+?)\s+(?:to|at)\s+(?P<start>.+?)(?:\s+for\s+(?P<duration>.+))?$",
    re.IGNORECASE,
)
TEMPORAL_HINT_RE = re.compile(
    r"\b(today|tomorrow|next|mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday|\d{1,2}(?::\d{2})?\s*(?:am|pm)?|am|pm)\b",
    re.IGNORECASE,
)
TEMPORAL_PREFIX_RE = re.compile(
    r"^(today|tomorrow|next|mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday|\d{4}-\d{2}-\d{2}|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.IGNORECASE,
)
PERSON_PREFIX_RE = re.compile(r"^(?:/person|person|pe)\s+", re.IGNORECASE)
PERSON_EDIT_RE = re.compile(
    r"^(?:edit person|update person)\s+(?P<person>.+?)\s+entry\s+(?P<entry>\d+)\s*:\s*(?P<info>\S[^\n]*)$",
    re.IGNORECASE,
)
PERSON_DELETE_RE = re.compile(
    r"^(?:delete person|remove person)\s+(?P<person>.+?)(?:\s+entry\s+(?P<entry>\d+))?$",
    re.IGNORECASE,
)
TAG_EXTRACT_RE = re.compile(r"#(\w+)")
PRIORITY_RE = re.compile(r"\b(?P<priority>high|medium|low)\s+priority\b", re.IGNORECASE)
TELEGRAM_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
AI_AGENT_EXECUTABLE_ACTIONS = {
    "create_task",
    "list_tasks",
    "view_task",
    "complete_task",
    "delete_task",
    "make_task_current",
    "update_task",
    "retag_tasks",
    "merge_tasks",
    "create_reminder",
    "list_reminders",
    "update_reminder",
    "cancel_reminder",
    "cancel_all_reminders",
    "ack_reminder",
    "append_project",
    "create_project",
    "list_projects",
    "view_project",
    "delete_project",
    "update_project",
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
    "show_all",
    "undo",
}
PENDING_ESCAPE_ACTIONS = {
    "show_help",
    "show_all",
    "show_menu",
    "show_settings",
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
    "list_tasks",
    "list_private_tasks",
    "list_reminders",
    "list_private_reminders",
    "upcoming_events",
    "list_notes",
    "list_preferences",
    "list_people",
    "show_debug_info",
    "undo",
}
TASK_PRIORITY_STYLES = {
    "high": "🔴 High",
    "medium": "🟡 Medium",
    "low": "🟢 Low",
}
TELEGRAM_MESSAGE_SOFT_LIMIT = 3500
HELP_TOPIC_ORDER = (
    "overview",
    "tasks",
    "reminders",
    "calendar",
    "notes",
    "preferences",
    "people",
    "projects",
    "system",
    "ai",
    "all",
)
HELP_TOPIC_ALIASES = {
    "task": "tasks",
    "tasks": "tasks",
    "reminder": "reminders",
    "reminders": "reminders",
    "calendar": "calendar",
    "events": "calendar",
    "event": "calendar",
    "agenda": "calendar",
    "note": "notes",
    "notes": "notes",
    "plan": "notes",
    "plans": "notes",
    "preference": "preferences",
    "preferences": "preferences",
    "prefs": "preferences",
    "memory": "preferences",
    "remember": "preferences",
    "personal": "preferences",
    "people": "people",
    "person": "people",
    "project": "projects",
    "projects": "projects",
    "system": "system",
    "settings": "system",
    "setting": "system",
    "debug": "system",
    "ai": "ai",
    "openai": "ai",
    "all": "all",
    "everything": "all",
    "full": "all",
    "overview": "overview",
    "start": "overview",
    "basics": "overview",
    "quickstart": "overview",
    "menu": "overview",
}
HELP_SECTIONS: dict[str, dict[str, Any]] = {
    "tasks": {
        "title": "Tasks",
        "lines": [
            ("`tasks`", "list open tasks, always with higher priorities first"),
            ("`ot <title>` · `others tasks`", "track something someone else owns and you need to check in on"),
            ("`tasks by due`, `tasks by remind`, `tasks by tag`, `tasks by priority`, `tasks tag <tag>`", "switch the task view"),
            ("`task <title> [priority <low|medium|high>] [due <date>] [remind me <date>] [#tag]`", "create a task"),
            ("`pt <title> [priority <low|medium|high>] [due <date>] [remind me <date>] [#tag]`", "create an encrypted private task, password required"),
            ("`pts`", "list encrypted private tasks, password required"),
            ("`show task <id or #>`", "view one task"),
            ("`update task <id or #> <field> <value>`", "edit `title`, `description`, `tags`, `priority`, `due`, or `remind me`"),
            ("`retag tasks <old> to <new>`", "replace a tag across tasks"),
            ("`combine <task> and <task> into one task`", "merge related tasks and complete the originals"),
            ("`current <id, # or title>` · `curr <id, # or title>`", "mark the task you are actively working on"),
            ("`done <id, # or title>`", "mark a task completed"),
            ("`delete task <id, # or title>`", "delete a task"),
            ("`delete all tasks`", "delete all tasks after confirmation"),
        ],
        "examples": [
            "Natural language also works:",
            "  `i need to buy groceries tomorrow`",
            "  `i have to file taxes by next tuesday`",
            "  `update task aba priority high`",
            "After `tasks`, short follow-ups work:",
            "  `done 1`",
            "  `delete it`",
        ],
    },
    "reminders": {
        "title": "Reminders",
        "lines": [
            ("`reminders`", "list active reminders"),
            ("`remind me to <text> <time>`", "create a reminder; supports `in one hour`, `30m`, `1h`, `0940`, and `9.40 am`"),
            ("`pr <text> <time>`", "create an encrypted private reminder, password required"),
            ("`prs`", "list encrypted private reminders, password required"),
            ("`update reminder <id or #> due <date>`", "reschedule a reminder"),
            ("`update reminder <id or #> text <new text>`", "rename a reminder"),
            ("`delete reminder <id, # or text>`", "cancel one reminder"),
            ("`cancel all reminders`", "cancel all active reminders"),
            ("`ack reminder <id or #>`", "acknowledge a sent reminder"),
        ],
        "examples": [
            "Natural language also works:",
            "  `schedule a reminder for tomorrow at 9am to pay rent`",
            "  `tmr 9am remind me pay rent`",
            "  `ping me about rent tomorrow morning at 9`",
            "  `what reminders do i have?`",
            "Recurring example:",
            "  `remind me to stretch every day at 6pm`",
            "Fired reminders now include a snooze option.",
        ],
    },
    "calendar": {
        "title": "Calendar",
        "lines": [
            ("`calendar`", "show upcoming events"),
            ("`schedule <title> <date/time> [for <duration>]`", "create an event"),
            ("`move event <id> to <date/time> [for <duration>]`", "move an assistant-created event"),
            ("`find a <N> minute free slot <day>`", "find open time"),
        ],
        "examples": [
            "Natural language also works:",
            "  `put demo review on my calendar tomorrow 3pm for 45 minutes`",
        ],
    },
    "notes": {
        "title": "Notes",
        "lines": [
            ("`notes`", "list saved notes"),
            ("`note <title>`", "create a note"),
            ("`note <title>: <description>`", "create a note with body"),
            ("`capture note <text>`", "quick-capture to the inbox"),
            ("`show note <id or #>`", "view a note"),
            ("`update note <id or #> <field> <value>`", "edit `title` or `description`"),
            ("`update note`", "start a guided note update flow"),
            ("`delete note <id or #>`", "delete a note"),
            ("`delete all notes`", "delete all notes after confirmation"),
        ],
        "examples": [
            "Note timestamps use human-friendly dates like `Today 09:58 PM` or `Tue 07 Apr 09:58 PM`.",
        ],
    },
    "projects": {
        "title": "Projects",
        "lines": [
            ("`projects` · `ps`", "list longer-term projects"),
            ("`project <title>` · `p <title>`", "create a project"),
            ("`project <title>: <description>`", "create a project with body"),
            ("`show project <id or #>`", "view a project"),
            ("`update project <id or #> <field> <value>`", "edit `title` or `description`"),
            ("`delete project <id or #>`", "delete a project"),
        ],
        "examples": [
            "Use projects for longer-term work that is bigger than a single task.",
        ],
    },
    "plans": {
        "title": "Plans",
        "lines": [
            ("`plans`, `list plans`, `show plans`, `pls`", "list saved plans"),
            ("`plan <title>`, `p <title>`", "create a plan"),
            ("`plan <title>: <description>`", "create a plan with body"),
            ("`append plan <text>`", "append to `plans.md`"),
            ("`view plan <id or #>`, `show plan <id or #>`", "view a plan"),
            ("`delete plan <id or #>`", "delete a plan"),
            ("`delete all plans`", "delete all plans after confirmation"),
            ("`update plan <id or #> title <new title>`", "rename a plan"),
            ("`update plan <id or #> description <new description>`", "change the body"),
        ],
        "examples": [],
    },
    "preferences": {
        "title": "Preferences",
        "lines": [
            ("`preferences`", "list saved preferences and remembered facts"),
            ("`preference <title>: <value>`", "save a structured preference"),
            ("`remember <text>`", "quickly save a fact to remember later"),
            ("`show preference <id or #>`", "view one preference"),
            ("`update preference <id or #> <field> <value>`", "edit `title` or `description`"),
            ("`delete preference <id or #>`", "delete a preference"),
            ("`delete all preferences`", "delete all preferences after confirmation"),
        ],
        "examples": [
            "Examples:",
            "  `preference coffee order: oat flat white`",
            "  `remember that i prefer aisle seats`",
        ],
    },
    "people": {
        "title": "People",
        "lines": [
            ("`person <name>: <info>`, `pe <name>: <info>`", "save a people entry, password required"),
            ("`people`", "view people entries, password required"),
            ("`edit person <person> entry <#>: <info>`", "update a people entry"),
            ("`delete person <person> [entry <#>]`", "delete a person or one entry"),
        ],
        "examples": [
            "Use people entries for private contact or relationship notes that need extra protection.",
            "Private tasks and reminders use the same password gate and encryption key.",
        ],
    },
    "system": {
        "title": "System",
        "lines": [
            ("`dashboard`, `show`", "show tasks and reminders together"),
            ("`menu`", "open the Telegram button menu"),
            ("`help <topic>`, `help all`", "show focused help or the full command list"),
            ("`undo`", "reverse the last successful change in this chat"),
            ("`system health`, `state health`", "show backup, AI, quarantine, and maintenance health"),
            ("`settings`", "show the settings menu"),
            ("`sync`, `git sync`, `backup now`", "force a durable git sync, password required"),
            ("`debug info`, `dev info`, `/debug`, `/dev`", "show runtime and git debug details"),
            ("`cancel`, `/cancel`", "cancel the current prompt"),
        ],
        "examples": [
            "If I am unsure which item you mean, I will ask a follow-up question in chat instead of guessing.",
            "Times use your configured timezone.",
        ],
    },
    "ai": {
        "title": "AI",
        "lines": [
            ("`ai <request>`", "run the AI agent for broader or multi-step requests"),
            ("`ai running`", "show whether an AI job is still running in this chat"),
            ("`ai status`", "show AI config, current run state, and the last-call summary"),
            ("`ai trace`, `ai traces`", "inspect the last AI call or recent calls"),
            ("`ai prompt`, `ai context`", "show the last prompts and injected context"),
            ("`ai backlog`", "show deterministic-feature ideas logged by the AI"),
            ("`ai usage [days]`, `ai costs [days]`, `ai credits`", "show usage and cost visibility"),
        ],
        "examples": [
            "AI is opt-in only. Unrecognised messages stay local unless you explicitly use `ai` or `/ai`.",
            "Use AI for broader searches and multi-step jobs that would be awkward as one normal command.",
        ],
    },
}


@dataclass
class AssistantDependencies:
    telegram: TelegramClient
    calendar: GoogleCalendarClient
    planner: OpenAIPlanner


class AssistantService:
    SENSITIVE_MESSAGE_TTL_SECONDS = 60

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        dependencies: AssistantDependencies | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        deps = dependencies or AssistantDependencies(
            telegram=TelegramClient(settings.telegram_bot_token),
            calendar=GoogleCalendarClient(settings),
            planner=OpenAIPlanner(settings),
        )
        self.telegram = deps.telegram
        self.calendar = deps.calendar
        self.planner = deps.planner
        self.backup_service = GitBackupService(settings)
        self.processing_lock = asyncio.Lock()
        self.backup_lock = asyncio.Lock()
        self.auto_sync_task: asyncio.Task[None] | None = None
        self.auto_sync_requested = False
        self.last_auto_sync_started_at = None

    async def process_update(self, update: dict[str, Any]) -> None:
        async with self.processing_lock:
            update_id = int(update.get("update_id", 0))
            if update_id and self.storage.has_processed_update(update_id):
                return

            # Handle callback_query (button presses).
            callback_query = update.get("callback_query")
            if isinstance(callback_query, dict):
                chat = (callback_query.get("message") or {}).get("chat") or {}
                callback_chat_id = str(chat.get("id", "")) or str((callback_query.get("from") or {}).get("id", ""))
                try:
                    await self._handle_callback_query(callback_query)
                except Exception as exc:
                    query_id = str(callback_query.get("id", ""))
                    if query_id:
                        with contextlib.suppress(Exception):
                            await self.telegram.answer_callback_query(query_id, "Something went wrong.")
                    if callback_chat_id:
                        with contextlib.suppress(Exception):
                            await self._send_message(callback_chat_id, self._user_safe_error(exc))
                if update_id:
                    self.storage.mark_update_processed(update_id, callback_chat_id)
                return

            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                return

            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            chat_id = str(chat.get("id", ""))
            user_id = int(sender.get("id", 0))
            message_id = int(message.get("message_id", 0))
            if not chat_id:
                return

            if chat.get("type") != "private":
                await self._send_message(chat_id, "I only support direct chats in v1.")
                return

            if self.settings.telegram_allowed_user_id <= 0 or user_id != self.settings.telegram_allowed_user_id:
                return

            text = self._normalize_user_text(message.get("text") or "")
            if not text:
                await self._send_message(chat_id, "Send me a text message and I can help.")
                if update_id:
                    self.storage.mark_update_processed(update_id, chat_id)
                return

            pending = self.storage.get_pending(chat_id)

            # Menu commands need to send a reply_markup — handle them before handle_message.
            lowered = text.strip().lower()
            if not pending and lowered in {"/start", "start", "menu", "/menu"}:
                try:
                    await self._send_message(
                        chat_id,
                        self._show_menu_text(),
                        reply_markup=self._main_menu_markup(),
                    )
                except Exception as exc:
                    print(f"Telegram send failed: {type(exc).__name__}")
                if update_id:
                    self.storage.mark_update_processed(update_id, chat_id)
                return
            if not pending and lowered in {"settings", "/settings"}:
                try:
                    user_settings = self.storage.get_user_settings()
                    await self._send_message(
                        chat_id,
                        self._settings_menu_text(user_settings),
                        reply_markup=self._settings_menu_markup(user_settings),
                    )
                except Exception as exc:
                    print(f"Telegram send failed: {type(exc).__name__}")
                if update_id:
                    self.storage.mark_update_processed(update_id, chat_id)
                return
            if not pending and self._should_run_ai_in_background(text):
                try:
                    await self._start_background_ai_request(chat_id=chat_id, user_message=self._extract_ai_prompt(text) or "")
                except Exception as exc:
                    print(f"Telegram AI start failed: {type(exc).__name__}")
                    await self._send_message(chat_id, self._user_safe_error(exc))
                if update_id:
                    self.storage.mark_update_processed(update_id, chat_id)
                return

            try:
                response = await self.handle_message(chat_id=chat_id, text=text)
            except Exception as exc:
                response = self._user_safe_error(exc)
            if message_id and self._should_delete_incoming_message(pending, text):
                with contextlib.suppress(Exception):
                    await self.telegram.delete_message(chat_id, message_id)
            try:
                reply_markup = self._response_reply_markup(chat_id=chat_id, request_text=text, response=response)
                await self._send_message(
                    chat_id,
                    response,
                    reply_markup=reply_markup,
                    parse_mode=self._response_parse_mode(text, response),
                    delete_after_seconds=self._response_auto_delete_seconds(pending, text),
                )
            except Exception as exc:
                print(f"Telegram send failed: {type(exc).__name__}")
            if update_id:
                self.storage.mark_update_processed(update_id, chat_id)

    def _should_run_ai_in_background(self, text: str) -> bool:
        if not self.settings.openai_api_key:
            return False
        ai_prompt = self._extract_ai_prompt(text)
        if ai_prompt is None or not ai_prompt:
            return False
        return self._deterministic_plan(text) is None

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        query_id = str(callback_query.get("id", ""))
        data = str(callback_query.get("data", ""))
        from_user = callback_query.get("from") or {}
        user_id = int(from_user.get("id", 0))
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        message_id = int(message.get("message_id", 0))

        if self.settings.telegram_allowed_user_id <= 0 or user_id != self.settings.telegram_allowed_user_id:
            await self.telegram.answer_callback_query(query_id, "Unauthorized.")
            return

        parts = data.split(":", 2)
        section = parts[0] if parts else ""
        action = parts[1] if len(parts) > 1 else ""
        param = parts[2] if len(parts) > 2 else ""

        if section == "private_reveal":
            reminder_id = action
            reminder = self.storage.find_private_reminder(reminder_id)
            if reminder is None:
                await self.telegram.answer_callback_query(query_id, "Private reminder not found.")
                return
            self.storage.set_pending(
                chat_id,
                {
                    "type": "private_auth",
                    "entity": "reminder",
                    "operation": "reveal",
                    "args": {"identifier": reminder_id},
                },
            )
            await self.telegram.answer_callback_query(query_id, "Enter your password in chat to reveal it.")
            await self._send_message(
                chat_id,
                "Enter the private-data password to reveal this reminder:",
                delete_after_seconds=self.SENSITIVE_MESSAGE_TTL_SECONDS,
            )
            return

        # Acknowledgement button on a fired reminder.
        if section == "ack":
            reminder_id = action
            reminder = next(
                (item for item in self.storage.list_reminders(include_inactive=True) if item.id == reminder_id),
                None,
            )
            if reminder is not None:
                undo_paths = [self.storage.reminders_path]
                if reminder.source_type == "task" and reminder.source_id:
                    undo_paths.extend(
                        [
                            self.storage.tasks_dir / f"{reminder.source_id}.md",
                            self.storage.tasks_index_path,
                        ]
                    )
                self._remember_path_undo(chat_id, f"acknowledge reminder '{reminder.text}'", undo_paths)
                acked = self.storage.ack_reminder(reminder_id)
            else:
                reminder = self.storage.find_private_reminder(reminder_id)
                if reminder is not None:
                    self._remember_path_undo(
                        chat_id,
                        f"acknowledge private reminder '{reminder.text}'",
                        [self.storage.private_reminders_enc_path, self.storage.private_tasks_enc_path],
                    )
                    acked = self.storage.ack_private_reminder(reminder_id, password=self._private_system_password())
                else:
                    acked = None
            if acked:
                await self.telegram.answer_callback_query(query_id, "✅ Reminder acknowledged!")
                with contextlib.suppress(Exception):
                    await self.telegram.edit_message_reply_markup(chat_id, message_id, None)
            else:
                await self.telegram.answer_callback_query(query_id, "Reminder not found.")
            return

        if section == "snooze":
            reminder_id = action
            reminder = next(
                (item for item in self.storage.list_reminders(include_inactive=True) if item.id == reminder_id),
                None,
            )
            pending_type = "reminder_snooze_duration"
            if reminder is None:
                reminder = self.storage.find_private_reminder(reminder_id)
                pending_type = "private_reminder_snooze_duration"
            if reminder is None:
                await self.telegram.answer_callback_query(query_id, "Reminder not found.")
                return
            self.storage.set_pending(chat_id, {"type": pending_type, "reminder_id": reminder_id})
            await self.telegram.answer_callback_query(query_id, "How long should I snooze it for?")
            await self._send_message(
                chat_id,
                f"How long should I snooze `{reminder.text}` for?\n"
                "Try `10 minutes`, `1 hour`, or `tomorrow 9am`.",
                delete_after_seconds=self.SENSITIVE_MESSAGE_TTL_SECONDS if pending_type == "private_reminder_snooze_duration" else None,
            )
            return

        # Menu navigation — edit the existing message in-place.
        if section == "menu":
            await self.telegram.answer_callback_query(query_id)
            if action == "main":
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    self._show_menu_text(),
                    reply_markup=self._main_menu_markup(),
                )
            elif action == "dashboard":
                await self._send_message(chat_id, self._show_all(chat_id=chat_id))
            elif action == "tasks":
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    "📋 Tasks",
                    reply_markup=self._tasks_menu_markup(),
                )
            elif action == "reminders":
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    "⏰ Reminders",
                    reply_markup=self._reminders_menu_markup(),
                )
            elif action == "calendar":
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    "📅 Calendar",
                    reply_markup=self._calendar_menu_markup(),
                )
            elif action == "people":
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    "👤 People",
                    reply_markup=self._people_menu_markup(),
                )
            elif action == "notes":
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    "📝 Notes",
                    reply_markup=self._notes_menu_markup(),
                )
            elif action == "plans":
                await self._send_message(chat_id, "Plans have been removed from the main interface. Use notes instead.")
            elif action == "preferences":
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    "⚙️ Preferences",
                    reply_markup=self._preferences_menu_markup(),
                )
            elif action == "settings":
                user_settings = self.storage.get_user_settings()
                await self.telegram.edit_message_text(
                    chat_id, message_id,
                    self._settings_menu_text(user_settings),
                    reply_markup=self._settings_menu_markup(user_settings),
                )
            elif action == "help":
                await self._send_message(
                    chat_id,
                    self._help_text("overview"),
                    reply_markup=self._help_menu_markup(),
                    parse_mode="Markdown",
                )
            elif action == "undo":
                await self._send_message(chat_id, await self._undo_last_action(chat_id))
            return

        if section == "help":
            await self.telegram.answer_callback_query(query_id)
            topic = HELP_TOPIC_ALIASES.get(action, "overview")
            if topic == "all":
                await self._send_message(
                    chat_id,
                    self._help_text(topic),
                    reply_markup=self._help_menu_markup(),
                    parse_mode="Markdown",
                )
            else:
                await self.telegram.edit_message_text(
                    chat_id,
                    message_id,
                    self._help_text(topic),
                    reply_markup=self._help_menu_markup(),
                    parse_mode="Markdown",
                )
            return

        # Section actions — send new messages for data output.
        if section == "tasks":
            await self.telegram.answer_callback_query(query_id)
            if action == "list":
                await self._send_message(chat_id, self._list_tasks(chat_id=chat_id))
            elif action == "new":
                self.storage.set_pending(chat_id, {"type": "task_input"})
                await self._send_message(
                    chat_id,
                    "Enter the task title (optionally add #tags, `priority <low|medium|high>`, `due <date>`, or `remind me <time>`):",
                )
            elif action == "done":
                self.storage.set_pending(chat_id, {"type": "task_complete_input"})
                await self._send_message(chat_id, "Enter the task number, id, or title to mark completed:")
            elif action == "delete":
                self.storage.set_pending(chat_id, {"type": "task_delete_input"})
                await self._send_message(chat_id, "Enter the task number, id, or title to delete:")
            elif action == "current":
                await self._send_message(chat_id, self._prompt_task_current(chat_id))
            elif action == "sort_due":
                await self._send_message(chat_id, self._list_tasks({"sort_by": "due"}, chat_id=chat_id))
            elif action == "sort_remind":
                await self._send_message(chat_id, self._list_tasks({"sort_by": "remind"}, chat_id=chat_id))
            elif action == "sort_priority":
                await self._send_message(chat_id, self._list_tasks({"sort_by": "priority"}, chat_id=chat_id))
            elif action == "sort_tag":
                await self._send_message(chat_id, self._list_tasks({"sort_by": "tag"}, chat_id=chat_id))
            elif action == "filter_tag":
                self.storage.set_pending(chat_id, {"type": "task_tag_filter_input"})
                await self._send_message(chat_id, "Enter a tag to filter by. Example: work")
            return

        if section == "reminders":
            if action == "cancel_all":
                reminders = self.storage.list_reminders()
                if reminders:
                    self._remember_path_undo(chat_id, "cancel all reminders", [self.storage.reminders_path])
                count = self.storage.cancel_all_reminders()
                await self.telegram.answer_callback_query(query_id, f"Cancelled {count} reminder(s).")
                await self._send_message(chat_id, f"Cancelled {count} active reminder(s).")
            else:
                await self.telegram.answer_callback_query(query_id)
                if action == "list":
                    await self._send_message(chat_id, self._list_reminders(chat_id=chat_id))
                elif action == "new":
                    self.storage.set_pending(chat_id, {"type": "reminder_input"})
                    await self._send_message(
                        chat_id,
                        "Enter reminder text and time (e.g. 'Call doctor at 3pm' or 'Buy milk in 30 minutes'):",
                    )
                elif action == "delete":
                    self.storage.set_pending(chat_id, {"type": "reminder_delete_input"})
                    await self._send_message(chat_id, "Enter the reminder number, id, or text to delete:")
            return

        if section == "calendar":
            await self.telegram.answer_callback_query(query_id)
            if action == "agenda":
                await self._send_message(chat_id, await self._upcoming_events(chat_id=chat_id))
            elif action == "free_slot":
                self.storage.set_pending(chat_id, {"type": "free_slot_input"})
                await self._send_message(chat_id, "Enter a time request. Example: 30 minutes tomorrow")
            elif action == "new":
                self.storage.set_pending(chat_id, {"type": "calendar_event_input"})
                await self._send_message(
                    chat_id,
                    "Enter the event title and time. Example: Demo review tomorrow 3pm for 45 minutes",
                )
            return

        if section == "people":
            await self.telegram.answer_callback_query(query_id)
            if action == "list":
                await self._send_message(
                    chat_id,
                    self._request_people_view(chat_id),
                    delete_after_seconds=self.SENSITIVE_MESSAGE_TTL_SECONDS,
                )
            elif action == "add":
                self.storage.set_pending(chat_id, {"type": "people_add_input"})
                await self._send_message(
                    chat_id,
                    "Enter the person as Name: info. Example: Alice Smith: met at Stripe",
                    delete_after_seconds=self.SENSITIVE_MESSAGE_TTL_SECONDS,
                )
            elif action == "edit":
                self.storage.set_pending(chat_id, {"type": "people_edit_input"})
                await self._send_message(
                    chat_id,
                    "Use: <person> entry <#>: <new text>. Example: Alice entry 1: now at OpenAI",
                    delete_after_seconds=self.SENSITIVE_MESSAGE_TTL_SECONDS,
                )
            elif action == "delete":
                self.storage.set_pending(chat_id, {"type": "people_delete_input"})
                await self._send_message(
                    chat_id,
                    "Use: <person> or <person> entry <#>. Example: Alice entry 2",
                    delete_after_seconds=self.SENSITIVE_MESSAGE_TTL_SECONDS,
                )
            return

        if section == "notes":
            await self.telegram.answer_callback_query(query_id)
            if action == "list":
                await self._send_message(chat_id, self._list_notes(chat_id=chat_id))
            elif action == "new":
                self.storage.set_pending(chat_id, {"type": "note_input"})
                await self._send_message(
                    chat_id,
                    "Enter note title (optionally add ': description'):",
                )
            elif action == "update":
                await self._send_message(chat_id, self._prompt_note_update(chat_id))
            elif action == "delete":
                self.storage.set_pending(chat_id, {"type": "note_delete_input"})
                await self._send_message(chat_id, "Enter the note number, id, or title to delete:")
            return

        if section == "plans":
            await self.telegram.answer_callback_query(query_id)
            await self._send_message(chat_id, "Plans have been removed from the main interface. Use notes instead.")
            return

        if section == "preferences":
            await self.telegram.answer_callback_query(query_id)
            if action == "list":
                await self._send_message(chat_id, self._list_preferences(chat_id=chat_id))
            elif action == "new":
                self.storage.set_pending(chat_id, {"type": "preference_input"})
                await self._send_message(chat_id, "Enter preference title (optionally add ': value'):")
            elif action == "delete":
                self.storage.set_pending(chat_id, {"type": "preference_delete_input"})
                await self._send_message(chat_id, "Enter the preference number, id, or title to delete:")
            return

        if section == "settings":
            await self.telegram.answer_callback_query(query_id)
            user_settings = self.storage.get_user_settings()
            if action == "ack_timeout":
                self.storage.set_pending(chat_id, {"type": "settings_input", "setting": "ack_timeout"})
                await self._send_message(
                    chat_id,
                    f"Current reminder ack timeout: {user_settings.reminder_ack_timeout_minutes} minutes.\n"
                    "Enter new value (minutes):",
                )
            elif action == "poll":
                self.storage.set_pending(chat_id, {"type": "settings_input", "setting": "poll"})
                await self._send_message(
                    chat_id,
                    f"Current reminder poll interval: {user_settings.reminder_poll_seconds} seconds.\n"
                    "Enter new value (seconds):",
                )
            return

        await self.telegram.answer_callback_query(query_id)

    async def handle_message(self, *, chat_id: str, text: str) -> str:
        text = self._normalize_user_text(text)
        plan = self._deterministic_plan(text)
        ai_prompt = self._extract_ai_prompt(text)

        # Let explicit navigation/list/help commands escape pending input mode.
        pending = self.storage.get_pending(chat_id)
        if pending is not None:
            if plan is not None and plan.action in PENDING_ESCAPE_ACTIONS:
                self.storage.clear_pending(chat_id)
                response = await self._execute_plan(plan, chat_id=chat_id)
                self._maybe_refresh_summary(chat_id, text, response)
                return response
            if ai_prompt is not None:
                self.storage.clear_pending(chat_id)
            else:
                response = await self._handle_pending(chat_id, text, pending)
                self._maybe_refresh_summary(chat_id, text, response)
                return response

        if plan is None and ai_prompt is not None:
            if not ai_prompt:
                return "Use `ai <request>` or `/ai <request>` to route a message through the AI tool-using agent."
            if not self.settings.openai_api_key:
                return "AI routing is not configured. Set OPENAI_API_KEY to enable `ai` / `/ai`."
            response = await self._run_ai_request(chat_id=chat_id, user_message=ai_prompt)
            self._maybe_refresh_summary(chat_id, text, response)
            return response

        if plan is None or self._prefer_semantic_match(plan):
            semantic = parse_semantic_match(
                text,
                self.settings.default_timezone,
                reference_context=self.storage.get_reference_context(chat_id),
            )
            if semantic is not None:
                if semantic.confidence == "medium":
                    self.storage.set_pending(
                        chat_id,
                        {"type": "semantic_confirm", "plan": semantic.plan.model_dump(mode="json")},
                    )
                    return (
                        f"I think you want me to {self._describe_plan(semantic.plan)}.\n"
                        "Reply yes to confirm, no to cancel, or help to see available commands."
                    )
                plan = semantic.plan
        if plan is None:
            contextual_follow_up = self._contextual_numeric_follow_up(chat_id, text)
            if contextual_follow_up is not None:
                return contextual_follow_up
            return (
                "I didn't recognise that as a known command.\n"
                "Send help to see available commands, or use `ai <request>` to route it through the AI tool-using agent."
            )
        response = await self._execute_plan(plan, chat_id=chat_id)
        self._maybe_refresh_summary(chat_id, text, response)
        return response

    async def _handle_pending(self, chat_id: str, text: str, pending: dict) -> str:
        kind = pending.get("type")
        lowered = text.strip().lower()

        if lowered in {"cancel", "/cancel", "stop", "never mind", "nevermind"}:
            self.storage.clear_pending(chat_id)
            return "Cancelled."

        if kind == "openai_confirm":
            self.storage.clear_pending(chat_id)
            if lowered in {"yes", "y", "ok", "sure", "yeah", "yep"}:
                original = pending.get("message", text)
                return await self._run_ai_request(chat_id=chat_id, user_message=str(original))
            if lowered in {"help", "/help"}:
                return self._help_text("overview")
            return "OK, cancelled. Send help to see available commands."

        if kind == "semantic_confirm":
            self.storage.clear_pending(chat_id)
            if lowered in {"yes", "y", "ok", "sure", "yeah", "yep"}:
                plan = AssistantPlan.model_validate(pending.get("plan") or {})
                return await self._execute_plan(plan, chat_id=chat_id)
            if lowered in {"help", "/help"}:
                return self._help_text("overview")
            return "OK, cancelled. Send help to see available commands."

        if kind == "entity_disambiguation":
            choices_payload = pending.get("choices") or []
            choices = [
                (str(item.get("id") or "").strip(), str(item.get("label") or "").strip())
                for item in choices_payload
                if isinstance(item, dict)
            ]
            if not choices:
                self.storage.clear_pending(chat_id)
                return "I lost track of the options. Please try the command again."
            resolved, display, follow_up = self._resolve_identifier_from_choices(text, choices)
            if follow_up is not None:
                return follow_up
            if resolved is None:
                return self._entity_disambiguation_reply(
                    entity_type=str(pending.get("entity_type") or "item"),
                    identifier=str(pending.get("identifier") or "").strip(),
                    choices=choices,
                    suggestion=bool(pending.get("suggestion")),
                )
            self.storage.clear_pending(chat_id)
            plan = AssistantPlan(
                action=str(pending.get("action") or "reply"),
                args=dict(pending.get("args") or {}),
            )
            plan.args["identifier"] = resolved
            plan.args["_resolved_identifier"] = True
            if display:
                plan.args["display"] = display
            return await self._execute_plan(plan, chat_id=chat_id)

        if kind == "datetime_clarification":
            plan = AssistantPlan.model_validate(pending.get("plan") or {})
            field = str(pending.get("field") or "").strip()
            label = str(pending.get("label") or "date/time").strip()
            require_time = bool(pending.get("require_time"))
            allow_clear = bool(pending.get("allow_clear"))
            if not field:
                self.storage.clear_pending(chat_id)
                return "I lost track of what needed clarification. Please try again."
            if allow_clear and self._is_clear_value(text):
                plan.args[field] = None
            else:
                parsed, prompt, note = self._resolve_datetime_value(text, label=label, require_time=require_time)
                if prompt is not None or parsed is None:
                    return prompt or self._datetime_clarification_prompt(
                        label=label,
                        raw_text=text,
                        require_time=require_time,
                    )
                plan.args[field] = parsed.isoformat()
                self._append_resolution_note(plan.args, note)
            self.storage.clear_pending(chat_id)
            return await self._execute_plan(plan, chat_id=chat_id)

        if kind == "private_auth":
            if not self.storage.private_password_valid(text.strip()):
                return "Wrong password. Try again or send /cancel."
            entity = str(pending.get("entity") or "").strip()
            operation = str(pending.get("operation") or "").strip()
            args = dict(pending.get("args") or {})
            self.storage.clear_pending(chat_id)
            if entity == "reminder" and operation == "reveal":
                return await self._send_private_revealed_reminder(
                    chat_id=chat_id,
                    reminder_id=str(args.get("identifier") or "").strip(),
                    password=text.strip(),
                )
            return self._execute_private_authenticated_operation(
                entity=entity,
                operation=operation,
                args=args,
                chat_id=chat_id,
                password=text.strip(),
            )

        if kind == "private_select":
            entity = str(pending.get("entity") or "").strip()
            operation = str(pending.get("operation") or "").strip()
            args = dict(pending.get("args") or {})
            ids = [str(item).strip() for item in pending.get("ids", []) if str(item).strip()]
            if not ids:
                self.storage.clear_pending(chat_id)
                return "I lost track of the private options. Please try again."
            selected: str | None = None
            raw = text.strip()
            if raw.isdigit():
                index = int(raw) - 1
                if 0 <= index < len(ids):
                    selected = ids[index]
            else:
                wanted = raw.lower()
                exact = [item_id for item_id in ids if item_id.lower() == wanted]
                if len(exact) == 1:
                    selected = exact[0]
                else:
                    prefix = [item_id for item_id in ids if item_id.lower().startswith(wanted)]
                    if len(prefix) == 1:
                        selected = prefix[0]
            if selected is None:
                return "Reply with the number or id from the private list, or /cancel."
            self.storage.clear_pending(chat_id)
            args["identifier"] = selected
            return self._execute_private_authenticated_operation(
                entity=entity,
                operation=operation,
                args=args,
                chat_id=chat_id,
                password=self._private_system_password(),
            )

        if kind == "private_datetime":
            entity = str(pending.get("entity") or "").strip()
            operation = str(pending.get("operation") or "").strip()
            args = dict(pending.get("args") or {})
            field = str(pending.get("field") or "").strip()
            label = str(pending.get("label") or "date/time").strip()
            require_time = bool(pending.get("require_time"))
            allow_clear = bool(pending.get("allow_clear"))
            if not field:
                self.storage.clear_pending(chat_id)
                return "I lost track of what needed clarification. Please try again."
            if allow_clear and self._is_clear_value(text):
                args[field] = None
            else:
                parsed, prompt, note = self._resolve_datetime_value(text, label=label, require_time=require_time)
                if prompt is not None or parsed is None:
                    return prompt or self._datetime_clarification_prompt(
                        label=label,
                        raw_text=text,
                        require_time=require_time,
                    )
                args[field] = parsed.isoformat()
                self._append_resolution_note(args, note)
            self.storage.clear_pending(chat_id)
            return self._execute_private_authenticated_operation(
                entity=entity,
                operation=operation,
                args=args,
                chat_id=chat_id,
                password=self._private_system_password(),
            )

        if kind == "people_view":
            if not self.storage.people_password_valid(text.strip()):
                return "Wrong password. Try again or send /cancel."
            self.storage.clear_pending(chat_id)
            try:
                content = self.storage.get_people(text.strip())
            except ValueError:
                return "Failed to decrypt people data. Please check the stored data."
            if content == "":
                return "No people entries yet."
            return self.storage.format_people(content)

        if kind == "people_add":
            name = pending.get("name", "")
            info = pending.get("info", "")
            if not self.storage.people_password_valid(text.strip()):
                return "Wrong password. Try again or send /cancel."
            self.storage.clear_pending(chat_id)
            try:
                self._remember_path_undo(
                    chat_id,
                    f"add person entry for {name}",
                    [self.storage.people_enc_path],
                )
                self.storage.append_person(name, info, text.strip())
            except ValueError:
                return "Failed to update people data because the existing file could not be decrypted."
            return f"Saved entry for {name}."

        if kind == "people_edit":
            person = str(pending.get("person", "")).strip()
            entry = int(pending.get("entry", 0))
            info = str(pending.get("info", "")).strip()
            if not self.storage.people_password_valid(text.strip()):
                return "Wrong password. Try again or send /cancel."
            self.storage.clear_pending(chat_id)
            try:
                self._remember_path_undo(
                    chat_id,
                    f"edit person entry {entry} for {person}",
                    [self.storage.people_enc_path],
                )
                updated = self.storage.update_person_entry(person, entry, info, text.strip())
            except ValueError:
                return "Failed to update people data because the existing file could not be decrypted."
            if updated is None:
                return "I couldn't find that person entry."
            return f"Updated entry {entry} for {updated}."

        if kind == "people_delete":
            person = str(pending.get("person", "")).strip()
            entry = pending.get("entry")
            if not self.storage.people_password_valid(text.strip()):
                return "Wrong password. Try again or send /cancel."
            self.storage.clear_pending(chat_id)
            try:
                label = (
                    f"delete person entry {entry} for {person}"
                    if entry is not None
                    else f"delete person {person}"
                )
                self._remember_path_undo(chat_id, label, [self.storage.people_enc_path])
                deleted = self.storage.delete_person(
                    person,
                    text.strip(),
                    entry_index=int(entry) if entry is not None else None,
                )
            except ValueError:
                return "Failed to update people data because the existing file could not be decrypted."
            if deleted is None:
                return "I couldn't find that person entry."
            if entry is not None:
                return f"Deleted entry {entry} for {deleted}."
            return f"Deleted person: {deleted}."

        if kind == "git_sync":
            if not self._operator_password_valid(text.strip()):
                return "Wrong password. Try again or send /cancel."
            self.storage.clear_pending(chat_id)
            return self._force_git_sync()

        if kind == "bulk_delete_confirm":
            entity = str(pending.get("entity") or "").strip()
            if lowered in {"yes", "y", "delete", "confirm"}:
                self.storage.clear_pending(chat_id)
                return self._perform_bulk_delete(entity, chat_id=chat_id)
            if lowered in {"help", "/help"}:
                return self._help_text("overview")
            self.storage.clear_pending(chat_id)
            return "OK, cancelled."

        if kind == "reminder_snooze_duration":
            reminder_id = str(pending.get("reminder_id") or "").strip()
            if not reminder_id:
                self.storage.clear_pending(chat_id)
                return "I lost track of which reminder to snooze."
            due_at = self._parse_snooze_due_at(text)
            if due_at is None:
                return "I need a clearer snooze time. Try `10 minutes`, `1 hour`, or `tomorrow 9am`."
            self.storage.clear_pending(chat_id)
            return self._snooze_reminder(reminder_id, due_at=due_at, chat_id=chat_id)

        if kind == "private_reminder_snooze_duration":
            reminder_id = str(pending.get("reminder_id") or "").strip()
            if not reminder_id:
                self.storage.clear_pending(chat_id)
                return "I lost track of which private reminder to snooze."
            due_at = self._parse_snooze_due_at(text)
            if due_at is None:
                return "I need a clearer snooze time. Try `10 minutes`, `1 hour`, or `tomorrow 9am`."
            self.storage.clear_pending(chat_id)
            reminder = self.storage.find_private_reminder(reminder_id)
            if reminder is not None:
                self._remember_path_undo(
                    chat_id,
                    f"snooze private reminder '{reminder.text}'",
                    [self.storage.private_reminders_enc_path, self.storage.private_tasks_enc_path],
                )
            updated = self.storage.snooze_private_reminder(reminder_id, password=self._private_system_password(), due_at=due_at)
            if updated is None:
                return "I couldn't find that private reminder."
            self._schedule_auto_sync()
            return (
                f"Snoozed private reminder: {updated.text}.\n"
                f"New time: {format_local(updated.due_at, self.settings.default_timezone)}."
            )

        # Input collected after pressing a menu button.
        if kind == "task_input":
            plan = self._task_plan(text.strip())
            if plan.action == "reply":
                return plan.response or "I couldn't understand that task."
            self.storage.clear_pending(chat_id)
            return await self._execute_plan(plan, chat_id=chat_id)

        if kind == "task_delete_input":
            self.storage.clear_pending(chat_id)
            return self._delete_task({"identifier": text.strip()}, chat_id=chat_id)

        if kind == "task_complete_input":
            self.storage.clear_pending(chat_id)
            return self._complete_task({"identifier": text.strip()}, chat_id=chat_id)

        if kind == "task_current_input":
            self.storage.clear_pending(chat_id)
            return self._make_task_current({"identifier": text.strip()}, chat_id=chat_id)

        if kind == "task_tag_filter_input":
            self.storage.clear_pending(chat_id)
            return self._list_tasks({"sort_by": "tag", "tag": text.strip()}, chat_id=chat_id)

        if kind == "reminder_input":
            plan = self._reminder_plan(text.strip())
            if plan is None:
                reminder_text = text.strip()
                if reminder_text and not self._looks_like_negative_reminder_text(reminder_text):
                    self.storage.set_pending(chat_id, {"type": "reminder_time_input", "text": reminder_text})
                    return (
                        f"When should I remind you about `{reminder_text}`?\n"
                        "Reply with something like `in 30m`, `in one hour`, `tomorrow 9am`, or `/cancel`."
                    )
                return "I couldn't parse that reminder. Try again or send /cancel.\nExample: Call doctor tomorrow at 3pm"
            self.storage.clear_pending(chat_id)
            return await self._execute_plan(plan, chat_id=chat_id)

        if kind == "reminder_time_input":
            reminder_text = str(pending.get("text") or "").strip()
            if not reminder_text:
                self.storage.clear_pending(chat_id)
                return "I lost track of the reminder text. Please try again."
            self.storage.clear_pending(chat_id)
            return await self._execute_plan(
                AssistantPlan(
                    action="create_reminder",
                    args={"text": reminder_text, "due_at": text.strip(), "recurrence": None},
                ),
                chat_id=chat_id,
            )

        if kind == "reminder_delete_input":
            self.storage.clear_pending(chat_id)
            return self._cancel_reminder({"identifier": text.strip()}, chat_id=chat_id)

        if kind == "note_input":
            self.storage.clear_pending(chat_id)
            plan = self._note_plan(text.strip())
            return await self._execute_plan(plan, chat_id=chat_id)

        if kind == "note_delete_input":
            self.storage.clear_pending(chat_id)
            return self._delete_note({"identifier": text.strip()}, chat_id=chat_id)

        if kind == "note_update_select":
            choices = self._entity_choices("note", chat_id=chat_id)
            resolved, display, follow_up = self._resolve_identifier_from_choices(text.strip(), choices)
            if resolved is None:
                if follow_up is not None:
                    return follow_up
                return "I couldn't find that note. Reply with a note number, id, title, or /cancel."
            self.storage.set_pending(
                chat_id,
                {"type": "note_update_value", "identifier": resolved, "display": display or resolved},
            )
            return (
                f"What should I update for `{display or resolved}`?\n"
                "Reply with `title <new title>` or `description <new text>`."
            )

        if kind == "note_update_value":
            identifier = str(pending.get("identifier") or "").strip()
            if not identifier:
                self.storage.clear_pending(chat_id)
                return "I lost track of which note to update. Please try again."
            match = re.match(r"^(?P<field>title|description|body)\s+(?P<value>\S[^\n]*)$", text.strip(), re.IGNORECASE)
            if match is None:
                return "Reply with `title <new title>` or `description <new text>`, or send /cancel."
            field = match.group("field").lower()
            if field == "body":
                field = "description"
            self.storage.clear_pending(chat_id)
            return self._update_note(
                {"identifier": identifier, "field": field, "value": match.group("value").strip()},
                chat_id=chat_id,
            )

        if kind == "plan_input":
            self.storage.clear_pending(chat_id)
            plan_entry = self._plan_entry_plan(text.strip())
            return await self._execute_plan(plan_entry, chat_id=chat_id)

        if kind == "plan_delete_input":
            self.storage.clear_pending(chat_id)
            return self._delete_plan({"identifier": text.strip()}, chat_id=chat_id)

        if kind == "preference_input":
            self.storage.clear_pending(chat_id)
            plan_entry = self._preference_plan(text.strip())
            return await self._execute_plan(plan_entry, chat_id=chat_id)

        if kind == "preference_delete_input":
            self.storage.clear_pending(chat_id)
            return self._delete_preference({"identifier": text.strip()}, chat_id=chat_id)

        if kind == "calendar_event_input":
            self.storage.clear_pending(chat_id)
            plan = self._event_create_plan(text.strip())
            if plan is None:
                return "I couldn't parse that event. Try again or send /cancel.\nExample: Put demo review on my calendar tomorrow 3pm for 45 minutes"
            return await self._execute_plan(plan, chat_id=chat_id)

        if kind == "free_slot_input":
            self.storage.clear_pending(chat_id)
            duration = parse_duration_minutes(text.strip(), default_minutes=30)
            return await self._find_free_slot({"duration_minutes": duration, "day": text.strip()})

        if kind == "people_add_input":
            plan = self._parse_people_add_input(text.strip())
            if plan is None:
                return "Use: Name: info. Example: Alice Smith: met at Stripe"
            self.storage.clear_pending(chat_id)
            return self._request_people_add(chat_id, plan)

        if kind == "people_edit_input":
            plan = self._parse_people_edit_input(text.strip())
            if plan is None:
                return "Use: <person> entry <#>: <new text>. Example: Alice entry 1: now at OpenAI"
            self.storage.clear_pending(chat_id)
            return self._request_people_edit(chat_id, plan)

        if kind == "people_delete_input":
            plan = self._parse_people_delete_input(text.strip())
            if plan is None:
                return "Use: <person> or <person> entry <#>. Example: Alice entry 2"
            self.storage.clear_pending(chat_id)
            return self._request_people_delete(chat_id, plan)

        if kind == "settings_input":
            setting = pending.get("setting", "")
            if not text.strip().isdigit():
                return "Please enter a valid whole number, or send /cancel."
            value = int(text.strip())
            user_settings = self.storage.get_user_settings()
            if setting == "ack_timeout":
                self._remember_path_undo(chat_id, "change reminder acknowledgement timeout", [self.storage.user_settings_path])
                user_settings.reminder_ack_timeout_minutes = max(1, value)
                self.storage.save_user_settings(user_settings)
                self.storage.clear_pending(chat_id)
                return (
                    f"Reminder acknowledgement timeout set to "
                    f"{user_settings.reminder_ack_timeout_minutes} minutes."
                )
            if setting == "poll":
                self._remember_path_undo(chat_id, "change reminder poll interval", [self.storage.user_settings_path])
                user_settings.reminder_poll_seconds = max(10, value)
                self.storage.save_user_settings(user_settings)
                self.storage.clear_pending(chat_id)
                return (
                    f"Reminder poll interval set to "
                    f"{user_settings.reminder_poll_seconds} seconds."
                )
            self.storage.clear_pending(chat_id)
            return "Unknown setting."

        # Unknown pending type — clear it.
        self.storage.clear_pending(chat_id)
        return "I lost track of what we were doing. Please try again."

    def _maybe_refresh_summary(self, chat_id: str, text: str, response: str) -> None:
        summary = self.storage.get_summary(chat_id)
        compact = f"Recent exchange: user='{text[:140]}' assistant='{response[:140]}'"
        if summary:
            merged = f"{summary}\n{compact}"
        else:
            merged = compact
        lines = merged.splitlines()[-8:]
        self.storage.save_summary(chat_id, "\n".join(lines))

    async def _start_background_ai_request(self, *, chat_id: str, user_message: str) -> None:
        if self._ai_run_is_active(chat_id):
            await self._send_message(chat_id, self._show_ai_running(chat_id))
            return
        self.storage.set_ai_run(
            chat_id,
            {
                "status": "syncing",
                "request": user_message,
                "started_at": now_utc().isoformat(),
                "sync_result": "",
                "result_preview": "",
                "error": "",
            },
        )
        await self._send_message(
            chat_id,
            "AI task started.\n"
            f"Request: {self._truncate_text(user_message, limit=240)}\n"
            "Syncing durable state first. Ask `ai running` if you want the current status.",
        )
        asyncio.create_task(self._complete_background_ai_request(chat_id=chat_id, user_message=user_message))

    async def _complete_background_ai_request(self, *, chat_id: str, user_message: str) -> None:
        try:
            sync_result = await self._sync_durable_state_now()
            current = self.storage.get_ai_run(chat_id) or {}
            current.update(
                {
                    "status": "running",
                    "request": user_message,
                    "started_at": str(current.get("started_at") or now_utc().isoformat()),
                    "sync_result": sync_result,
                    "error": "",
                }
            )
            self.storage.set_ai_run(chat_id, current)
            response = await self._run_ai_request(chat_id=chat_id, user_message=user_message, sync_before_run=False)
            self.storage.set_ai_run(
                chat_id,
                {
                    "status": "completed",
                    "request": user_message,
                    "started_at": str(current.get("started_at") or now_utc().isoformat()),
                    "ended_at": now_utc().isoformat(),
                    "sync_result": sync_result,
                    "result_preview": self._truncate_text(response, limit=400),
                    "error": "",
                },
            )
            await self._send_message(chat_id, response)
        except Exception as exc:
            self.storage.set_ai_run(
                chat_id,
                {
                    "status": "failed",
                    "request": user_message,
                    "started_at": str((self.storage.get_ai_run(chat_id) or {}).get("started_at") or now_utc().isoformat()),
                    "ended_at": now_utc().isoformat(),
                    "sync_result": str((self.storage.get_ai_run(chat_id) or {}).get("sync_result") or ""),
                    "result_preview": "",
                    "error": str(exc),
                },
            )
            with contextlib.suppress(Exception):
                await self._send_message(chat_id, self._user_safe_error(exc))

    def _ai_run_is_active(self, chat_id: str) -> bool:
        run = self.storage.get_ai_run(chat_id) or {}
        return str(run.get("status") or "").strip() in {"syncing", "running"}

    def _show_ai_running(self, chat_id: str) -> str:
        if not chat_id:
            return "AI run status is only tracked per chat."
        run = self.storage.get_ai_run(chat_id)
        if not run:
            return "No AI task has been started in this chat yet."
        status = str(run.get("status") or "unknown").strip() or "unknown"
        request = str(run.get("request") or "").strip() or "(empty)"
        lines = [
            "AI run status",
            "",
            f"Status: {status}",
            f"Request: {self._truncate_text(request, limit=240)}",
        ]
        started_at = str(run.get("started_at") or "").strip()
        if started_at:
            started = parse_user_datetime(started_at, self.settings.default_timezone)
            if started is not None:
                lines.append(f"Started: {format_local(started, self.settings.default_timezone)}")
        sync_result = str(run.get("sync_result") or "").strip()
        if sync_result:
            lines.append(f"Pre-run sync: {sync_result}")
        result_preview = str(run.get("result_preview") or "").strip()
        if result_preview and status == "completed":
            lines.extend(["", "Latest result:", result_preview])
        error = str(run.get("error") or "").strip()
        if error:
            lines.append(f"Error: {error}")
        return "\n".join(lines)

    async def _sync_durable_state_now(self) -> str:
        if not self.settings.git_backup_enabled:
            return "Git auto sync is disabled."
        try:
            return await asyncio.to_thread(self._force_git_sync)
        except Exception as exc:
            print(f"Auto sync error: {type(exc).__name__}")
            return f"Git sync failed: {exc}"

    def _schedule_auto_sync(self) -> None:
        if not self.settings.git_backup_enabled:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.auto_sync_task is not None and not self.auto_sync_task.done():
            self.auto_sync_requested = True
            return
        self.auto_sync_requested = False
        self.auto_sync_task = asyncio.create_task(self._drain_auto_sync_requests())

    async def _drain_auto_sync_requests(self) -> None:
        while True:
            if self.last_auto_sync_started_at is not None:
                elapsed = (now_utc() - self.last_auto_sync_started_at).total_seconds()
                min_interval = max(self.settings.backup_interval_seconds, _MIN_AUTO_SYNC_INTERVAL_SECONDS)
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
            try:
                async with self.backup_lock:
                    self.last_auto_sync_started_at = now_utc()
                    await asyncio.to_thread(self.backup_service.backup_now)
            except Exception as exc:
                print(f"Auto sync error: {type(exc).__name__}")
            if not self.auto_sync_requested:
                self.auto_sync_task = None
                return
            self.auto_sync_requested = False

    def _prompt_context(self, *, chat_id: str = "") -> list[str]:
        memory_path = self.storage.memory_dir / "instructions.md"
        notes_path = self.storage.notes_dir / "inbox.md"
        snippets = []
        for path in (memory_path, notes_path):
            if path.exists():
                snippets.append(path.read_text(encoding="utf-8")[:500])
        open_tasks = self.storage.list_tasks()
        if open_tasks:
            snippets.append(
                "Open tasks: " + ", ".join(f"{task.id}:{task.title}" for task in open_tasks[:5])
            )
        reminders = self.storage.list_reminders()
        if reminders:
            snippets.append(
                "Scheduled reminders: "
                + ", ".join(f"{reminder.id}:{reminder.text}" for reminder in reminders[:5])
            )
        other_tasks = self.storage.list_tasks(kind="other")
        if other_tasks:
            snippets.append(
                "Others' tasks: " + ", ".join(f"{task.id}:{task.title}" for task in other_tasks[:5])
            )
        projects = self.storage.list_projects()
        if projects:
            snippets.append(
                "Projects: " + ", ".join(f"{project.id}:{project.title}" for project in projects[:5])
            )
        if chat_id:
            reference_context = self.storage.get_reference_context(chat_id)
            if reference_context:
                snippets.append("Reference context: " + json.dumps(reference_context, sort_keys=True))
        return snippets

    @staticmethod
    def _prefer_semantic_match(plan: AssistantPlan) -> bool:
        identifier = str(plan.args.get("identifier", "")).strip().lower()
        if not identifier:
            return False
        if identifier.isdigit():
            return False
        return bool(
            re.search(
                r"\b(it|that|this|first|second|third|fourth|fifth|last|one|two|three|four|five)\b",
                identifier,
            )
        )

    def _remember_reference_context(
        self,
        chat_id: str,
        entity_type: str,
        items: list[tuple[str, str]],
        *,
        mode: str | None = None,
    ) -> None:
        if not chat_id or not items:
            return
        ids = [identifier for identifier, _ in items if identifier]
        labels = [label for _, label in items if label]
        if not ids:
            return
        self.storage.set_reference_context(
            chat_id,
            {
                "entity_type": entity_type,
                "ids": ids,
                "labels": labels,
                "mode": mode or ("single" if len(ids) == 1 else "list"),
            },
        )

    def _clear_reference_context(self, chat_id: str) -> None:
        if chat_id:
            self.storage.clear_reference_context(chat_id)

    @staticmethod
    def _describe_plan(plan: AssistantPlan) -> str:
        action = plan.action
        args = plan.args
        display = str(args.get("display") or args.get("text") or args.get("title") or args.get("identifier") or "").strip()
        if action == "create_task":
            return f"create the task '{display}'" if display else "create that task"
        if action == "delete_task":
            return f"delete '{display}'" if display else "delete that task"
        if action == "make_task_current":
            return f"make '{display}' current" if display else "make that task current"
        if action == "complete_task":
            return f"mark '{display}' as completed" if display else "mark that task as completed"
        if action == "view_task":
            return f"show '{display}'" if display else "show that task"
        if action == "retag_tasks":
            return "replace a task tag"
        if action == "merge_tasks":
            return "merge multiple tasks"
        if action == "create_reminder":
            return f"set a reminder for '{display}'" if display else "set that reminder"
        if action == "cancel_reminder":
            return f"cancel reminder '{display}'" if display else "cancel that reminder"
        if action == "ack_reminder":
            return f"acknowledge reminder '{display}'" if display else "acknowledge that reminder"
        if action == "create_calendar_event":
            return f"create the event '{display}'" if display else "create that calendar event"
        if action == "update_calendar_event":
            return f"move '{display}'" if display else "move that calendar event"
        if action == "list_tasks":
            return "list your tasks"
        if action == "list_reminders":
            return "list your reminders"
        if action == "upcoming_events":
            return "show your calendar"
        if action == "find_free_slot":
            return "look for a free slot"
        if action == "show_all":
            return "show your tasks, reminders, and projects"
        if action == "create_note":
            return f"save the note '{display}'" if display else "save that note"
        if action == "delete_note":
            return f"delete note '{display}'" if display else "delete that note"
        if action == "view_note":
            return f"show note '{display}'" if display else "show that note"
        if action == "create_project":
            return f"save the project '{display}'" if display else "save that project"
        if action == "delete_project":
            return f"delete project '{display}'" if display else "delete that project"
        if action == "view_project":
            return f"show project '{display}'" if display else "show that project"
        if action == "list_projects":
            return "list your projects"
        if action == "create_plan":
            return f"save the plan '{display}'" if display else "save that plan"
        if action == "delete_plan":
            return f"delete plan '{display}'" if display else "delete that plan"
        if action == "view_plan":
            return f"show plan '{display}'" if display else "show that plan"
        if action == "create_preference":
            return f"save preference '{display}'" if display else "save that preference"
        if action == "delete_preference":
            return f"delete preference '{display}'" if display else "delete that preference"
        if action == "view_preference":
            return f"show preference '{display}'" if display else "show that preference"
        return "do that"

    def _remember_path_undo(self, chat_id: str, label: str, paths: list) -> None:
        if not chat_id:
            return
        action = self.storage.build_path_undo_action(label, paths)
        self.storage.set_undo_action(chat_id, action)

    def _remember_undo_action(self, chat_id: str, action: dict[str, Any]) -> None:
        if not chat_id:
            return
        self.storage.set_undo_action(chat_id, action)

    async def _undo_last_action(self, chat_id: str) -> str:
        if not chat_id:
            return "Nothing to undo."
        action = self.storage.get_undo_action(chat_id)
        if action is None:
            return "Nothing to undo."

        kind = str(action.get("kind", "")).strip()
        if kind == "path_restore":
            label = self.storage.apply_path_undo_action(action)
            self.storage.clear_undo_action(chat_id)
            return f"Undid: {label}."

        if kind == "calendar_create":
            event_id = str(action.get("event_id", "")).strip()
            if not event_id:
                return "The last action cannot be undone cleanly."
            deleted = await self.calendar.delete_owned_event(event_id)
            if not deleted:
                return "I couldn't undo that calendar event because I couldn't find it anymore."
            link_restore = action.get("link_restore")
            if isinstance(link_restore, dict):
                self.storage.apply_path_undo_action(link_restore)
            self.storage.clear_undo_action(chat_id)
            label = str(action.get("label", "")).strip() or "calendar event"
            return f"Undid: {label}."

        if kind == "calendar_update":
            event_id = str(action.get("event_id", "")).strip()
            if not event_id:
                return "The last action cannot be undone cleanly."
            restored = await self.calendar.update_owned_event(
                event_id,
                title=str(action.get("title") or "").strip() or None,
                start=parse_user_datetime(str(action.get("start") or "").strip(), self.settings.default_timezone),
                end=parse_user_datetime(str(action.get("end") or "").strip(), self.settings.default_timezone),
                description=str(action.get("description") or ""),
            )
            if restored is None:
                return "I couldn't undo that calendar update because I couldn't find the event anymore."
            self.storage.clear_undo_action(chat_id)
            label = str(action.get("label", "")).strip() or "calendar change"
            return f"Undid: {label}."

        return "The last action cannot be undone."

    def _deterministic_plan(self, text: str) -> AssistantPlan | None:
        lowered = text.strip().lower()

        if ai_plan := self._ai_observability_plan(lowered):
            return ai_plan
        if lowered in {"/help", "help", "?"}:
            return AssistantPlan(action="show_help", args={"topic": "overview"})
        if match := re.fullmatch(r"(?:/)?help\s+(.+)", lowered):
            raw_topic = match.group(1).strip()
            if raw_topic in {"plan", "plans"}:
                topic = raw_topic
            else:
                topic = HELP_TOPIC_ALIASES.get(raw_topic, raw_topic)
            return AssistantPlan(action="show_help", args={"topic": topic})
        if lowered in {"undo", "/undo"}:
            return AssistantPlan(action="undo")
        if lowered in {"/start", "start", "menu", "/menu"}:
            return AssistantPlan(action="show_menu")
        if lowered in {"settings", "/settings"}:
            return AssistantPlan(action="show_settings")
        if lowered in {"system health", "state health", "health", "/health"}:
            return AssistantPlan(action="show_system_health")
        if lowered in {"new task", "add task"}:
            return AssistantPlan(action="prompt_task_create")
        if lowered in {"complete task", "finish task", "done task"}:
            return AssistantPlan(action="prompt_task_complete")
        if lowered in {"delete task", "remove task"}:
            return AssistantPlan(action="prompt_task_delete")
        if lowered in {"current", "curr", "current task", "curr task", "make current"}:
            return AssistantPlan(action="prompt_task_current")
        if lowered in {"new reminder", "add reminder"}:
            return AssistantPlan(action="prompt_reminder_create")
        if lowered in {"delete reminder", "remove reminder"}:
            return AssistantPlan(action="prompt_reminder_delete")
        if lowered in {"new note", "add note"}:
            return AssistantPlan(action="prompt_note_create")
        if lowered in {"delete note", "remove note"}:
            return AssistantPlan(action="prompt_note_delete")
        if lowered in {"update note", "edit note"}:
            return AssistantPlan(action="prompt_note_update")
        if lowered in {"new event", "add event", "schedule event"}:
            return AssistantPlan(action="prompt_calendar_create")
        if lowered in {"find free slot", "free slot"}:
            return AssistantPlan(action="prompt_free_slot")
        if lowered in {"debug info", "dev info", "debug", "dev", "/debug", "/dev"}:
            return AssistantPlan(action="show_debug_info")
        if lowered in {"show", "s", "show all", "dashboard", "/dashboard"}:
            return AssistantPlan(action="show_all")
        if lowered in {"/tasks", "tasks", "list tasks", "show tasks", "ts"}:
            return AssistantPlan(action="list_tasks", args={"sort_by": "default"})
        if lowered in {"others tasks", "other tasks", "others' tasks", "list others tasks", "show others tasks", "ots"}:
            return AssistantPlan(action="list_tasks", args={"sort_by": "default", "kind": "other"})
        if lowered in {"/pts", "pts", "private tasks", "list private tasks", "show private tasks"}:
            return AssistantPlan(action="list_private_tasks", args={"sort_by": "default"})
        if lowered in {"tasks by due", "ts due"}:
            return AssistantPlan(action="list_tasks", args={"sort_by": "due"})
        if lowered in {"tasks by remind", "tasks by reminder", "ts remind", "ts reminder"}:
            return AssistantPlan(action="list_tasks", args={"sort_by": "remind"})
        if lowered in {"tasks by priority", "ts priority"}:
            return AssistantPlan(action="list_tasks", args={"sort_by": "priority"})
        if lowered in {"pts by due", "private tasks by due"}:
            return AssistantPlan(action="list_private_tasks", args={"sort_by": "due"})
        if lowered in {"pts by remind", "pts by reminder", "private tasks by remind", "private tasks by reminder"}:
            return AssistantPlan(action="list_private_tasks", args={"sort_by": "remind"})
        if lowered in {"pts by priority", "private tasks by priority"}:
            return AssistantPlan(action="list_private_tasks", args={"sort_by": "priority"})
        if lowered in {"tasks by tag", "tasks by tags", "ts tag"}:
            return AssistantPlan(action="list_tasks", args={"sort_by": "tag"})
        if lowered.startswith("tasks tag ") or lowered.startswith("tasks tagged "):
            tag = text.split(maxsplit=2)[-1].strip()
            return AssistantPlan(action="list_tasks", args={"sort_by": "tag", "tag": tag})
        if lowered in {"/reminders", "reminders", "list reminders", "show reminders", "rs"}:
            return AssistantPlan(action="list_reminders")
        if lowered in {"/prs", "prs", "private reminders", "list private reminders", "show private reminders"}:
            return AssistantPlan(action="list_private_reminders")
        if lowered in {"/agenda", "agenda", "calendar", "upcoming events", "what's on my calendar", "ag"}:
            return AssistantPlan(action="upcoming_events")
        if lowered in {"people", "list people", "show people", "/people", "pel"}:
            return AssistantPlan(action="list_people")
        if lowered in {"sync", "git sync", "backup now", "/sync"}:
            return AssistantPlan(action="force_git_sync")
        if lowered in {"notes", "list notes", "show notes", "/notes", "ns"}:
            return AssistantPlan(action="list_notes")
        if lowered in {"plans", "list plans", "show plans", "/plans", "pls"}:
            return AssistantPlan(action="list_plans")
        if lowered in {"projects", "list projects", "show projects", "/projects", "ps"}:
            return AssistantPlan(action="list_projects")
        if lowered in {"preferences", "list preferences", "show preferences", "/preferences"}:
            return AssistantPlan(action="list_preferences")
        if lowered in {"prefs"}:
            return AssistantPlan(action="list_preferences")
        if lowered in {"cancel all reminders", "remove all reminders", "cancel reminders"}:
            return AssistantPlan(action="cancel_all_reminders")
        if match := DELETE_ALL_RE.match(text):
            entity = match.group("entity").strip().lower()
            if entity.startswith("task"):
                return AssistantPlan(action="reply", args={"bulk_delete": "tasks"})
            if entity.startswith("note"):
                return AssistantPlan(action="reply", args={"bulk_delete": "notes"})
            if entity.startswith("plan"):
                return AssistantPlan(action="reply", args={"bulk_delete": "plans"})
            if entity.startswith("project"):
                return AssistantPlan(action="reply", args={"bulk_delete": "projects"})
            if entity.startswith("pref"):
                return AssistantPlan(action="reply", args={"bulk_delete": "preferences"})
            if entity.startswith("reminder"):
                return AssistantPlan(action="reply", args={"bulk_delete": "reminders"})
        if "free slot" in lowered or "availability" in lowered:
            return AssistantPlan(
                action="find_free_slot",
                args={
                    "duration_minutes": parse_duration_minutes(text),
                    "day": text,
                },
            )

        if match := VIEW_PRIVATE_TASK_RE.match(text):
            return AssistantPlan(action="view_private_task", args={"identifier": match.group("identifier").strip()})
        if match := PRIVATE_TASK_DELETE_RE.match(text):
            return AssistantPlan(action="delete_private_task", args={"identifier": match.group("identifier").strip()})
        if match := PRIVATE_TASK_UPDATE_RE.match(text):
            return AssistantPlan(
                action="update_private_task",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": match.group("field").strip().lower(),
                    "value": match.group("value").strip(),
                },
            )
        if match := VIEW_PRIVATE_REMINDER_RE.match(text):
            return AssistantPlan(action="view_private_reminder", args={"identifier": match.group("identifier").strip()})
        if match := PRIVATE_REMINDER_UPDATE_RE.match(text):
            field = match.group("field").strip().lower()
            if field == "title":
                field = "text"
            elif field in {"time", "at", "for", "on"}:
                field = "due"
            elif field == "repeat":
                field = "recurrence"
            return AssistantPlan(
                action="update_private_reminder",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": field,
                    "value": match.group("value").strip(),
                },
            )
        if match := PRIVATE_REMINDER_RESCHEDULE_RE.match(text):
            return AssistantPlan(
                action="update_private_reminder",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": "due",
                    "value": match.group("value").strip(),
                },
            )
        if match := PRIVATE_REMINDER_CANCEL_RE.match(text):
            return AssistantPlan(
                action="cancel_private_reminder",
                args={"identifier": match.group("identifier").strip()},
            )
        if match := PRIVATE_REMINDER_ACK_RE.match(text):
            return AssistantPlan(
                action="ack_private_reminder",
                args={"identifier": match.group("identifier").strip()},
            )
        if match := VIEW_TASK_RE.match(text):
            return AssistantPlan(action="view_task", args={"identifier": match.group("identifier").strip()})
        if match := TASK_CURRENT_RE.match(text):
            return AssistantPlan(action="make_task_current", args={"identifier": match.group("identifier").strip()})
        if match := TASK_CURRENT_SUFFIX_RE.match(text):
            return AssistantPlan(action="make_task_current", args={"identifier": match.group("identifier").strip()})
        if match := TASK_RETAG_VERBOSE_RE.match(text):
            return AssistantPlan(
                action="retag_tasks",
                args={
                    "old_tag": match.group("old_tag").strip(),
                    "new_tag": match.group("new_tag").strip(),
                },
            )
        if match := TASK_RETAG_RE.match(text):
            return AssistantPlan(
                action="retag_tasks",
                args={
                    "old_tag": match.group("old_tag").strip(),
                    "new_tag": match.group("new_tag").strip(),
                },
            )
        if match := TASK_MERGE_RE.match(text):
            first = match.group("first").strip()
            second = match.group("second").strip()
            title = match.group("title").strip()
            if title.lower() in {"one task", "a task", "single task", "one"}:
                title = ""
            return AssistantPlan(
                action="merge_tasks",
                args={"source_identifiers": [first, second], "title": title},
            )
        if match := TASK_DELETE_RE.match(text):
            return AssistantPlan(action="delete_task", args={"identifier": match.group("identifier").strip()})
        if match := TASK_UPDATE_RE.match(text):
            return AssistantPlan(
                action="update_task",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": match.group("field").strip().lower(),
                    "value": match.group("value").strip(),
                },
            )
        if match := VIEW_NOTE_RE.match(text):
            return AssistantPlan(action="view_note", args={"identifier": match.group("identifier").strip()})
        if match := NOTE_DELETE_RE.match(text):
            return AssistantPlan(action="delete_note", args={"identifier": match.group("identifier").strip()})
        if match := NOTE_UPDATE_RE.match(text):
            return AssistantPlan(
                action="update_note",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": match.group("field").strip().lower(),
                    "value": match.group("value").strip(),
                },
            )
        if match := VIEW_PLAN_RE.match(text):
            return AssistantPlan(action="view_plan", args={"identifier": match.group("identifier").strip()})
        if match := PLAN_DELETE_RE.match(text):
            return AssistantPlan(action="delete_plan", args={"identifier": match.group("identifier").strip()})
        if match := PLAN_UPDATE_RE.match(text):
            return AssistantPlan(
                action="update_plan",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": match.group("field").strip().lower(),
                    "value": match.group("value").strip(),
                },
            )
        if match := VIEW_PROJECT_RE.match(text):
            return AssistantPlan(action="view_project", args={"identifier": match.group("identifier").strip()})
        if match := PROJECT_DELETE_RE.match(text):
            return AssistantPlan(action="delete_project", args={"identifier": match.group("identifier").strip()})
        if match := PROJECT_UPDATE_RE.match(text):
            return AssistantPlan(
                action="update_project",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": match.group("field").strip().lower(),
                    "value": match.group("value").strip(),
                },
            )
        if match := VIEW_PREFERENCE_RE.match(text):
            return AssistantPlan(action="view_preference", args={"identifier": match.group("identifier").strip()})
        if match := PREFERENCE_DELETE_RE.match(text):
            return AssistantPlan(action="delete_preference", args={"identifier": match.group("identifier").strip()})
        if match := PREFERENCE_UPDATE_RE.match(text):
            return AssistantPlan(
                action="update_preference",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": match.group("field").strip().lower(),
                    "value": match.group("value").strip(),
                },
            )

        if match := PRIVATE_TASK_CREATE_RE.match(text):
            rest = match.group("rest").strip()
            plan = self._task_plan(rest)
            plan.action = "create_private_task"
            return plan
        if match := OTHER_TASK_CREATE_RE.match(text):
            return self._task_plan(match.group("rest").strip(), kind="other")
        if match := PRIVATE_TASK_COMPLETE_RE.match(text):
            return AssistantPlan(action="complete_private_task", args={"identifier": match.group("identifier").strip()})
        if match := TASK_CREATE_RE.match(text):
            rest = match.group("rest").strip()
            # Smart: if rest matches an existing task ID exactly, view it instead of creating
            tasks = self.storage.list_tasks(include_completed=True)
            for task in tasks:
                if task.id == rest or task.id.startswith(rest):
                    return AssistantPlan(action="view_task", args={"identifier": rest})
            return self._task_plan(rest)
        if match := TASK_COMPLETE_RE.match(text):
            return AssistantPlan(action="complete_task", args={"identifier": match.group("identifier").strip()})
        if match := PRIVATE_REMINDER_CREATE_RE.match(text):
            plan = self._reminder_plan(match.group("rest"))
            if plan is not None:
                plan.action = "create_private_reminder"
            return plan
        if match := REMINDER_CREATE_RE.match(text):
            full_text_plan = parse_reminder_request(text, self.settings.default_timezone)
            if full_text_plan is not None:
                return full_text_plan
            return self._reminder_plan(match.group("rest"))
        if match := REMINDER_UPDATE_RE.match(text):
            field = match.group("field").strip().lower()
            if field == "title":
                field = "text"
            elif field in {"time", "at", "for", "on"}:
                field = "due"
            elif field == "repeat":
                field = "recurrence"
            return AssistantPlan(
                action="update_reminder",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": field,
                    "value": match.group("value").strip(),
                },
            )
        if match := REMINDER_RESCHEDULE_RE.match(text):
            return AssistantPlan(
                action="update_reminder",
                args={
                    "identifier": match.group("identifier").strip(),
                    "field": "due",
                    "value": match.group("value").strip(),
                },
            )
        if match := REMINDER_CANCEL_RE.match(text):
            identifier = match.group("identifier").strip()
            if identifier.lower() == "all":
                return AssistantPlan(action="cancel_all_reminders")
            return AssistantPlan(
                action="cancel_reminder",
                args={"identifier": identifier},
            )
        if match := REMINDER_ACK_RE.match(text):
            return AssistantPlan(
                action="ack_reminder",
                args={"identifier": match.group("identifier").strip()},
            )
        if match := NOTE_APPEND_RE.match(text):
            return AssistantPlan(action="append_note", args={"text": match.group("text").strip()})
        if match := NOTE_RE.match(text):
            return self._note_plan(match.group("rest").strip())
        if match := PLAN_APPEND_RE.match(text):
            return AssistantPlan(action="append_plan", args={"text": match.group("text").strip()})
        if match := PLAN_RE.match(text):
            return self._plan_entry_plan(match.group("rest").strip())
        if match := PROJECT_APPEND_RE.match(text):
            return AssistantPlan(action="append_project", args={"text": match.group("text").strip()})
        if match := PROJECT_RE.match(text):
            return self._project_plan(match.group("rest").strip())
        if match := PREFERENCE_APPEND_RE.match(text):
            return AssistantPlan(action="append_preference", args={"text": match.group("text").strip()})
        if match := PREFERENCE_RE.match(text):
            return self._preference_plan(match.group("rest").strip())
        if match := EVENT_CREATE_RE.match(text):
            rest = match.group("rest").strip()
            if re.search(r"\b(remind|reminder|remember|ping|nudge|alert|notify)\b", rest, re.IGNORECASE):
                reminder_plan = self._reminder_plan(rest)
                if reminder_plan is not None:
                    return reminder_plan
            return self._event_create_plan(rest)
        if match := EVENT_UPDATE_RE.match(text):
            return self._event_update_plan(
                match.group("identifier"),
                match.group("start"),
                match.group("duration"),
            )
        if match := PERSON_PREFIX_RE.match(text):
            rest = text[match.end():]
            colon_pos = rest.find(":")
            if colon_pos > 0:
                name = rest[:colon_pos].strip()
                info = rest[colon_pos + 1:].strip()
                if name and info:
                    return AssistantPlan(
                        action="add_person",
                        args={"name": name, "info": info},
                    )
        if match := PERSON_EDIT_RE.match(text):
            return AssistantPlan(
                action="edit_person",
                args={
                    "person": match.group("person").strip(),
                    "entry": int(match.group("entry")),
                    "info": match.group("info").strip(),
                },
            )
        if match := PERSON_DELETE_RE.match(text):
            entry = match.group("entry")
            args: dict[str, Any] = {"person": match.group("person").strip()}
            if entry:
                args["entry"] = int(entry)
            return AssistantPlan(action="delete_person", args=args)
        return None

    @staticmethod
    def _ai_observability_plan(lowered: str) -> AssistantPlan | None:
        if lowered in {"ai status", "/ai status"}:
            return AssistantPlan(action="show_ai_status")
        if lowered in {"ai running", "/ai running", "ai still running", "/ai still running", "is ai still running"}:
            return AssistantPlan(action="show_ai_running")
        if lowered in {"ai trace", "/ai trace", "ai trace last", "/ai trace last"}:
            return AssistantPlan(action="show_ai_trace")
        if lowered in {"ai traces", "/ai traces"}:
            return AssistantPlan(action="show_ai_traces")
        if lowered in {"ai prompt", "/ai prompt"}:
            return AssistantPlan(action="show_ai_prompt")
        if lowered in {"ai context", "/ai context"}:
            return AssistantPlan(action="show_ai_context")
        if lowered in {"ai backlog", "/ai backlog", "ai automations", "/ai automations", "ai ideas", "/ai ideas"}:
            return AssistantPlan(action="show_ai_backlog")
        if lowered in {"ai usage", "/ai usage"}:
            return AssistantPlan(action="show_ai_usage", args={"days": 7})
        if lowered in {"ai costs", "/ai costs", "ai spend", "/ai spend"}:
            return AssistantPlan(action="show_ai_costs", args={"days": 30})
        if lowered in {"ai credits", "/ai credits"}:
            return AssistantPlan(action="show_ai_credits", args={"days": 30})
        if match := re.fullmatch(r"(?:/)?ai usage (\d+)", lowered):
            return AssistantPlan(action="show_ai_usage", args={"days": int(match.group(1))})
        if match := re.fullmatch(r"(?:/)?ai (?:costs|spend) (\d+)", lowered):
            return AssistantPlan(action="show_ai_costs", args={"days": int(match.group(1))})
        return None

    @staticmethod
    def _parse_tags(text: str) -> list[str]:
        """Parse space/comma-separated tags, stripping leading '#' characters."""
        return [t.strip().lstrip("#") for t in text.replace(",", " ").split() if t.strip()]

    @staticmethod
    def _normalize_priority(value: str) -> str | None:
        lowered = value.strip().lower()
        aliases = {
            "high": "high",
            "p1": "high",
            "urgent": "high",
            "medium": "medium",
            "normal": "medium",
            "default": "medium",
            "p2": "medium",
            "low": "low",
            "p3": "low",
        }
        return aliases.get(lowered)

    @staticmethod
    def _format_priority_label(priority: str) -> str:
        return TASK_PRIORITY_STYLES.get(priority.strip().lower(), priority.strip().title() or "Medium")

    @staticmethod
    def _is_clear_value(value: str) -> bool:
        return value.strip().lower() in {"clear", "none", "remove", "unset"}

    def _datetime_clarification_prompt(
        self,
        *,
        label: str,
        raw_text: str,
        require_time: bool,
        detail: str | None = None,
    ) -> str:
        if detail:
            first_line = detail
        elif require_time:
            first_line = f"I need a specific time for that {label}."
        else:
            first_line = f"I couldn't understand that {label}."
        examples = (
            "Reply with something like `tomorrow 9am`, `12 Jan 3pm`, or `2026-01-12 15:00`."
            if require_time
            else "Reply with something like `tomorrow`, `12 Jan`, or `2026-01-12`."
        )
        return f"{first_line}\n{examples}"

    def _queue_datetime_clarification(
        self,
        *,
        chat_id: str,
        plan: AssistantPlan,
        field: str,
        label: str,
        raw_text: str,
        require_time: bool,
        allow_clear: bool = False,
        prompt: str | None = None,
    ) -> str:
        reply = prompt or self._datetime_clarification_prompt(
            label=label,
            raw_text=raw_text,
            require_time=require_time,
        )
        if chat_id:
            self.storage.set_pending(
                chat_id,
                {
                    "type": "datetime_clarification",
                    "plan": plan.model_dump(mode="json"),
                    "field": field,
                    "label": label,
                    "require_time": require_time,
                    "allow_clear": allow_clear,
                },
            )
        return reply

    def _resolve_datetime_value(
        self,
        raw_text: str,
        *,
        label: str,
        require_time: bool,
    ) -> tuple[Any | None, str | None, str | None]:
        interpretation = interpret_user_datetime(raw_text, self.settings.default_timezone)
        if interpretation.status == "ambiguous":
            return None, self._datetime_clarification_prompt(
                label=label,
                raw_text=raw_text,
                require_time=require_time,
                detail=interpretation.prompt or None,
            ), None
        if interpretation.status != "ok" or interpretation.value is None:
            return None, self._datetime_clarification_prompt(
                label=label,
                raw_text=raw_text,
                require_time=require_time,
            ), None
        if require_time and not interpretation.has_explicit_time:
            return None, self._datetime_clarification_prompt(
                label=label,
                raw_text=raw_text,
                require_time=True,
            ), None
        return interpretation.value, None, interpretation.note or None

    @staticmethod
    def _append_resolution_note(args: dict[str, Any], note: str | None) -> None:
        if not note:
            return
        notes = [str(item).strip() for item in (args.get("_resolution_notes") or []) if str(item).strip()]
        if note not in notes:
            notes.append(note)
        args["_resolution_notes"] = notes

    @staticmethod
    def _consume_resolution_notes(args: dict[str, Any]) -> list[str]:
        return [str(item).strip() for item in args.pop("_resolution_notes", []) if str(item).strip()]

    def _append_notes_to_reply(self, reply: str, notes: list[str]) -> str:
        clean_notes = [note for note in notes if note]
        if not clean_notes:
            return reply
        return reply + "\n\n" + "\n".join(clean_notes)

    def _contextual_numeric_follow_up(self, chat_id: str, text: str) -> str | None:
        context = self.storage.get_reference_context(chat_id)
        if not context:
            return None
        ids = [str(item).strip() for item in context.get("ids", []) if str(item).strip()]
        entity_type = str(context.get("entity_type") or "").strip()
        match = re.fullmatch(r"\s*(delete|remove|done|complete|finish|show|view|ack|acknowledge|cancel)\s+(\d+)\s*", text, re.IGNORECASE)
        if match is None or not ids or not entity_type:
            return None
        raw_number = int(match.group(2))
        if 1 <= raw_number <= len(ids):
            return None
        label = self._entity_label(entity_type)
        return (
            f"I only see {len(ids)} {label}(s) in the current list. "
            "Reply with a number from that list, or use the exact title."
        )

    @staticmethod
    def _looks_like_date_range(text: str) -> bool:
        lowered = text.strip().lower()
        return bool(
            re.search(r"\bbetween\b.+\band\b", lowered)
            or re.search(r"\bfrom\b.+\b(to|until|til|through|thru)\b", lowered)
            or re.search(r"\b(to|until|til|through|thru)\b", lowered)
            or re.fullmatch(r"(?:(?:this|next)\s+)?(?:week|month|year|weekend)", lowered)
        )

    def _task_plan(self, rest: str, *, kind: str = "self") -> AssistantPlan:
        due_at = None
        reminder_at = None
        priority = "medium"
        priority_match = re.search(r"\bpriority\s+(?P<priority>low|medium|high|normal|urgent|p1|p2|p3)\b", rest, re.IGNORECASE)
        if priority_match:
            normalized = self._normalize_priority(priority_match.group("priority"))
            if normalized is None:
                return AssistantPlan(action="reply", response="Use task priority low, medium, or high.")
            priority = normalized
            rest = re.sub(r"\bpriority\s+(low|medium|high|normal|urgent|p1|p2|p3)\b", " ", rest, flags=re.IGNORECASE)
        title = rest
        due_match = re.search(r"\bdue\s+(?P<due>.+?)(?=\s+remind(?: me)?\s+|$)", rest, re.IGNORECASE)
        reminder_match = re.search(r"\bremind(?: me)?\s+(?P<when>.+)$", rest, re.IGNORECASE)
        cut_positions = [match.start() for match in (due_match, reminder_match) if match]
        if cut_positions:
            title = rest[: min(cut_positions)]
        if due_match:
            due_at = due_match.group("due").strip()
        if reminder_match:
            reminder_at = reminder_match.group("when").strip()
        # Extract #tags from the title portion
        tags = TAG_EXTRACT_RE.findall(title)
        title = TAG_EXTRACT_RE.sub("", title).strip()
        return AssistantPlan(
            action="create_task",
            args={
                "title": title.strip(),
                "tags": tags,
                "kind": kind,
                "priority": priority,
                "due_at": due_at,
                "reminder_at": reminder_at,
            },
        )

    def _note_plan(self, rest: str) -> AssistantPlan:
        if ":" in rest:
            title, _, body = rest.partition(":")
            return AssistantPlan(action="create_note", args={"title": title.strip(), "body": body.strip()})
        return AssistantPlan(action="create_note", args={"title": rest.strip(), "body": ""})

    def _plan_entry_plan(self, rest: str) -> AssistantPlan:
        if ":" in rest:
            title, _, body = rest.partition(":")
            return AssistantPlan(action="create_plan", args={"title": title.strip(), "body": body.strip()})
        return AssistantPlan(action="create_plan", args={"title": rest.strip(), "body": ""})

    def _project_plan(self, rest: str) -> AssistantPlan:
        if ":" in rest:
            title, _, body = rest.partition(":")
            return AssistantPlan(action="create_project", args={"title": title.strip(), "body": body.strip()})
        return AssistantPlan(action="create_project", args={"title": rest.strip(), "body": ""})

    def _preference_plan(self, rest: str) -> AssistantPlan:
        if ":" in rest:
            title, _, body = rest.partition(":")
            return AssistantPlan(action="create_preference", args={"title": title.strip(), "body": body.strip()})
        return AssistantPlan(action="create_preference", args={"title": rest.strip(), "body": ""})

    def _parse_people_add_input(self, text: str) -> dict[str, Any] | None:
        if ":" not in text:
            return None
        name, _, info = text.partition(":")
        name = name.strip()
        info = info.strip()
        if not name or not info:
            return None
        return {"name": name, "info": info}

    def _parse_people_edit_input(self, text: str) -> dict[str, Any] | None:
        match = re.match(
            r"^(?P<person>.+?)\s+entry\s+(?P<entry>\d+)\s*:\s*(?P<info>\S[^\n]*)$",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        return {
            "person": match.group("person").strip(),
            "entry": int(match.group("entry")),
            "info": match.group("info").strip(),
        }

    def _parse_people_delete_input(self, text: str) -> dict[str, Any] | None:
        match = re.match(
            r"^(?P<person>.+?)(?:\s+entry\s+(?P<entry>\d+))?$",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        args: dict[str, Any] = {"person": match.group("person").strip()}
        entry = match.group("entry")
        if entry:
            args["entry"] = int(entry)
        return args

    def _reminder_plan(self, rest: str) -> AssistantPlan | None:
        plan = parse_reminder_request(rest, self.settings.default_timezone)
        if plan is not None:
            return plan
        plan = parse_reminder_request(f"remind me to {rest}", self.settings.default_timezone)
        if plan is not None:
            return plan
        recurrence = None
        recurrence_match = re.search(r"\bevery\s+(daily|weekly|monthly)\b", rest, re.IGNORECASE)
        if recurrence_match:
            recurrence = recurrence_match.group(1).lower()
            rest = rest[: recurrence_match.start()].strip()
        when_match = re.search(r"\s+(?:at|on|in)\s+(?P<when>.+)$", rest, re.IGNORECASE)
        if not when_match:
            return None
        reminder_text = rest[: when_match.start()].strip()
        due_at = when_match.group("when").strip()
        if not reminder_text or not due_at:
            return None
        return AssistantPlan(
            action="create_reminder",
            args={
                "text": reminder_text,
                "due_at": due_at,
                "recurrence": recurrence,
            },
        )

    @staticmethod
    def _looks_like_negative_reminder_text(text: str) -> bool:
        lowered = text.strip().lower()
        return bool(re.search(r"\bnot\s+(?:a\s+)?reminder\b|\bdon'?t\s+remind\b|\bdo\s+not\s+remind\b", lowered))

    def _event_create_plan(self, rest: str) -> AssistantPlan | None:
        semantic = parse_event_request(rest, self.settings.default_timezone)
        if semantic is not None:
            return semantic
        duration_minutes = 60
        duration_match = re.search(r"\s+for\s+(?P<duration>.+)$", rest, re.IGNORECASE)
        if duration_match:
            duration_minutes = parse_duration_minutes(duration_match.group("duration"), default_minutes=60)
            rest = rest[: duration_match.start()].strip()

        tokens = rest.replace(" at ", " ").replace(" on ", " ").split()
        for index in range(1, len(tokens)):
            title = " ".join(tokens[:index]).strip()
            start_text = " ".join(tokens[index:]).strip()
            if not TEMPORAL_HINT_RE.search(start_text):
                continue
            if not TEMPORAL_PREFIX_RE.search(start_text):
                continue
            if TEMPORAL_HINT_RE.search(title):
                continue
            start = parse_user_datetime(start_text, self.settings.default_timezone)
            if title and start is not None:
                return AssistantPlan(
                    action="create_calendar_event",
                    args={
                        "title": title,
                        "start": start_text,
                        "duration_minutes": duration_minutes,
                    },
                )
        return None

    def _event_update_plan(
        self,
        identifier: str,
        start_text: str,
        duration_text: str | None,
    ) -> AssistantPlan | None:
        start = parse_user_datetime(start_text, self.settings.default_timezone)
        if start is None:
            return None
        duration_minutes = parse_duration_minutes(duration_text or "", default_minutes=60)
        return AssistantPlan(
            action="update_calendar_event",
            args={
                "identifier": identifier.strip(),
                "start": start_text.strip(),
                "duration_minutes": duration_minutes,
            },
        )

    async def _execute_plan(self, plan: AssistantPlan, *, chat_id: str = "") -> str:
        action = plan.action
        args = dict(plan.args)

        if action == "reply":
            bulk_delete = str(args.get("bulk_delete") or "").strip()
            if bulk_delete:
                return self._request_bulk_delete(chat_id, bulk_delete)
            return plan.response or "I need a bit more detail to help with that."
        if action == "show_help":
            return self._help_text(str(args.get("topic") or "overview"))
        if action == "show_debug_info":
            return self._show_debug_info()
        if action == "show_system_health":
            return self._show_system_health()
        if action == "prompt_task_create":
            return self._prompt_task_create(chat_id)
        if action == "prompt_task_complete":
            return self._prompt_task_complete(chat_id)
        if action == "prompt_task_delete":
            return self._prompt_task_delete(chat_id)
        if action == "prompt_task_current":
            return self._prompt_task_current(chat_id)
        if action == "prompt_reminder_create":
            return self._prompt_reminder_create(chat_id)
        if action == "prompt_reminder_delete":
            return self._prompt_reminder_delete(chat_id)
        if action == "prompt_note_create":
            return self._prompt_note_create(chat_id)
        if action == "prompt_note_delete":
            return self._prompt_note_delete(chat_id)
        if action == "prompt_note_update":
            return self._prompt_note_update(chat_id)
        if action == "prompt_calendar_create":
            return self._prompt_calendar_create(chat_id)
        if action == "prompt_free_slot":
            return self._prompt_free_slot(chat_id)
        if action == "show_all":
            return self._show_all(chat_id=chat_id)
        if action == "show_ai_status":
            return self._show_ai_status(chat_id)
        if action == "show_ai_running":
            return self._show_ai_running(chat_id)
        if action == "show_ai_trace":
            return self._show_ai_trace()
        if action == "show_ai_traces":
            return self._show_ai_traces()
        if action == "show_ai_prompt":
            return self._show_ai_prompt()
        if action == "show_ai_context":
            return self._show_ai_context()
        if action == "show_ai_backlog":
            return self._show_ai_backlog()
        if action == "show_ai_usage":
            return await self._show_ai_usage(args)
        if action == "show_ai_costs":
            return await self._show_ai_costs(args)
        if action == "show_ai_credits":
            return await self._show_ai_credits(args)
        if action == "undo":
            return await self._undo_last_action(chat_id)
        if action == "show_menu":
            return self._show_menu_text()
        if action == "show_settings":
            return self._show_settings_text()
        if action == "create_task":
            return self._create_task(args, chat_id=chat_id)
        if action == "list_tasks":
            return self._list_tasks(args, chat_id=chat_id)
        if action == "view_task":
            return self._view_task(args, chat_id=chat_id)
        if action == "complete_task":
            return self._complete_task(args, chat_id=chat_id)
        if action == "delete_task":
            return self._delete_task(args, chat_id=chat_id)
        if action == "make_task_current":
            return self._make_task_current(args, chat_id=chat_id)
        if action == "update_task":
            return self._update_task(args, chat_id=chat_id)
        if action == "retag_tasks":
            return self._retag_tasks(args, chat_id=chat_id)
        if action == "merge_tasks":
            return self._merge_tasks(args, chat_id=chat_id)
        if action == "create_private_task":
            return self._request_private_task_action(chat_id, "create", args)
        if action == "list_private_tasks":
            return self._request_private_task_action(chat_id, "list", args)
        if action == "view_private_task":
            return self._request_private_task_action(chat_id, "view", args)
        if action == "complete_private_task":
            return self._request_private_task_action(chat_id, "complete", args)
        if action == "delete_private_task":
            return self._request_private_task_action(chat_id, "delete", args)
        if action == "update_private_task":
            return self._request_private_task_action(chat_id, "update", args)
        if action == "create_reminder":
            return self._create_reminder(args, chat_id=chat_id)
        if action == "list_reminders":
            return self._list_reminders(chat_id=chat_id)
        if action == "update_reminder":
            return self._update_reminder(args, chat_id=chat_id)
        if action == "cancel_reminder":
            return self._cancel_reminder(args, chat_id=chat_id)
        if action == "cancel_all_reminders":
            return self._cancel_all_reminders(chat_id=chat_id)
        if action == "ack_reminder":
            return self._ack_reminder(args, chat_id=chat_id)
        if action == "create_private_reminder":
            return self._request_private_reminder_action(chat_id, "create", args)
        if action == "list_private_reminders":
            return self._request_private_reminder_action(chat_id, "list", args)
        if action == "view_private_reminder":
            return self._request_private_reminder_action(chat_id, "view", args)
        if action == "update_private_reminder":
            return self._request_private_reminder_action(chat_id, "update", args)
        if action == "cancel_private_reminder":
            return self._request_private_reminder_action(chat_id, "cancel", args)
        if action == "ack_private_reminder":
            return self._request_private_reminder_action(chat_id, "ack", args)
        if action == "upcoming_events":
            return await self._upcoming_events(chat_id=chat_id)
        if action == "find_free_slot":
            return await self._find_free_slot(args)
        if action == "create_calendar_event":
            return await self._create_calendar_event(args, chat_id=chat_id)
        if action == "update_calendar_event":
            return await self._update_calendar_event(args, chat_id=chat_id)
        if action == "append_note":
            return self._append_note(args, chat_id=chat_id)
        if action == "create_note":
            return self._create_note(args, chat_id=chat_id)
        if action == "list_notes":
            return self._list_notes(chat_id=chat_id)
        if action == "view_note":
            return self._view_note(args, chat_id=chat_id)
        if action == "delete_note":
            return self._delete_note(args, chat_id=chat_id)
        if action == "update_note":
            return self._update_note(args, chat_id=chat_id)
        if action == "append_project":
            return self._append_project(args, chat_id=chat_id)
        if action == "create_project":
            return self._create_project(args, chat_id=chat_id)
        if action == "list_projects":
            return self._list_projects(chat_id=chat_id)
        if action == "view_project":
            return self._view_project(args, chat_id=chat_id)
        if action == "delete_project":
            return self._delete_project(args, chat_id=chat_id)
        if action == "update_project":
            return self._update_project(args, chat_id=chat_id)
        if action == "append_plan":
            return self._append_plan(args, chat_id=chat_id)
        if action == "create_plan":
            return self._create_plan(args, chat_id=chat_id)
        if action == "list_plans":
            return self._list_plans(chat_id=chat_id)
        if action == "view_plan":
            return self._view_plan(args, chat_id=chat_id)
        if action == "delete_plan":
            return self._delete_plan(args, chat_id=chat_id)
        if action == "update_plan":
            return self._update_plan(args, chat_id=chat_id)
        if action == "append_preference":
            return self._append_preference(args, chat_id=chat_id)
        if action == "create_preference":
            return self._create_preference(args, chat_id=chat_id)
        if action == "list_preferences":
            return self._list_preferences(chat_id=chat_id)
        if action == "view_preference":
            return self._view_preference(args, chat_id=chat_id)
        if action == "delete_preference":
            return self._delete_preference(args, chat_id=chat_id)
        if action == "update_preference":
            return self._update_preference(args, chat_id=chat_id)
        if action == "list_people":
            return self._request_people_view(chat_id)
        if action == "add_person":
            return self._request_people_add(chat_id, args)
        if action == "edit_person":
            return self._request_people_edit(chat_id, args)
        if action == "delete_person":
            return self._request_people_delete(chat_id, args)
        if action == "force_git_sync":
            return self._request_force_git_sync(chat_id)
        return "I couldn't map that request to a safe action."

    async def _run_ai_request(self, *, chat_id: str, user_message: str, sync_before_run: bool = True) -> str:
        if sync_before_run:
            await self._sync_durable_state_now()
        agent_runner = getattr(self.planner, "run_agent", None)
        if callable(agent_runner):
            summary = self.storage.get_summary(chat_id)
            snippets = self._prompt_context(chat_id=chat_id)

            async def tool_runner(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
                return await self._run_ai_tool(tool_name, tool_args, chat_id=chat_id)

            message, trace = await agent_runner(
                user_message=user_message,
                summary=summary,
                context_snippets=snippets,
                tools=self._ai_agent_tools(),
                tool_runner=tool_runner,
            )
            automation_candidate = trace.get("automation_candidate")
            if isinstance(automation_candidate, dict) and automation_candidate:
                self.storage.append_automation_idea(user_message, automation_candidate)
            self._record_openai_trace(chat_id, trace)
            return str(message or "Done.").strip() or "Done."

        plan = await self._plan_with_openai_trace(chat_id=chat_id, user_message=user_message)
        return await self._execute_plan(plan, chat_id=chat_id)

    async def _plan_with_openai_trace(self, *, chat_id: str, user_message: str) -> AssistantPlan:
        summary = self.storage.get_summary(chat_id)
        snippets = self._prompt_context(chat_id=chat_id)
        planner_with_trace = getattr(self.planner, "plan_with_trace", None)
        if callable(planner_with_trace):
            plan, trace = await planner_with_trace(
                user_message=user_message,
                summary=summary,
                context_snippets=snippets,
            )
        else:
            plan = await self.planner.plan(
                user_message=user_message,
                summary=summary,
                context_snippets=snippets,
            )
            trace = {
                "model": getattr(self.settings, "openai_model", ""),
                "base_url": getattr(self.settings, "openai_base_url", ""),
                "user_message": user_message,
                "summary": summary,
                "context_snippets": snippets,
                "parsed_plan": plan.model_dump(mode="json"),
            }
        self._record_openai_trace(chat_id, trace)
        return plan

    def _ai_agent_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "execute_plan",
                "description": (
                    "Execute an existing assistant action when a deterministic action already exists. "
                    "Use this for create/update/delete/list/view flows instead of hand-writing answers. "
                    "Set dry_run=true when the action is destructive, broad, or privacy-sensitive and you want a proposal instead."
                ),
                "args": {
                    "action": sorted(AI_AGENT_EXECUTABLE_ACTIONS),
                    "args": "object with the action arguments",
                    "dry_run": "boolean, default false",
                },
            },
            {
                "name": "execute_plan_batch",
                "description": (
                    "Run multiple deterministic assistant actions in order. "
                    "Use dry_run=true first when the request could change many items."
                ),
                "args": {
                    "plans": "list of {action, args}",
                    "dry_run": "boolean, default false",
                },
            },
            {
                "name": "query_state",
                "description": (
                    "Inspect tasks, reminders, notes, projects, plans, or preferences with optional text search and filters."
                ),
                "args": {
                    "entity": ["tasks", "reminders", "notes", "projects", "plans", "preferences"],
                    "query": "optional semantic or keyword query",
                    "limit": "integer, default 10",
                    "include_completed": "boolean, tasks only",
                    "include_inactive": "boolean, reminders only",
                    "tag": "optional task tag filter",
                    "kind": "optional tasks-only filter: self or other",
                },
            },
            {
                "name": "manage_task",
                "description": (
                    "Create, update, complete, or delete tasks directly. Use this when the request needs task fields "
                    "that do not map neatly to one existing assistant command, such as updating due_at and reminder_at together, "
                    "or merging several tasks into one consolidated task."
                ),
                "args": {
                    "operation": ["create", "update", "complete", "delete", "merge"],
                    "identifier": "task id, list number, or title for update/complete/delete",
                    "source_identifiers": "list of task ids or titles for merge",
                    "title": "task title",
                    "body": "optional task description",
                    "tags": "optional list of tags",
                    "kind": ["self", "other"],
                    "priority": ["high", "medium", "low"],
                    "due_at": "optional natural-language or ISO datetime, null to clear on update",
                    "reminder_at": "optional natural-language or ISO datetime, null to clear on update",
                    "complete_sources": "boolean, default true for merge",
                },
            },
            {
                "name": "manage_reminder",
                "description": "Create, cancel, or snooze reminders directly.",
                "args": {
                    "operation": ["create", "cancel", "snooze"],
                    "identifier": "reminder id, list number, or text for cancel/snooze",
                    "text": "reminder text for create",
                    "due_at": "natural-language or ISO datetime",
                    "recurrence": ["daily", "weekly", "monthly"],
                },
            },
            {
                "name": "manage_note",
                "description": "Create, update, or delete notes directly.",
                "args": {
                    "operation": ["create", "update", "delete"],
                    "identifier": "note id, list number, or title for update/delete",
                    "title": "note title",
                    "body": "note body",
                },
            },
            {
                "name": "manage_calendar_event",
                "description": "Create, update, or delete assistant-owned calendar events directly.",
                "args": {
                    "operation": ["create", "update", "delete"],
                    "identifier": "event id or title for update/delete",
                    "title": "event title",
                    "start": "natural-language or ISO datetime for create/update",
                    "duration_minutes": "integer duration for create/update, default 60",
                    "description": "optional event description",
                },
            },
            {
                "name": "search_state_files",
                "description": (
                    "Search raw text in durable state files such as notes inbox, routines, memory, preferences markdown, "
                    "automation ideas, and other markdown/json/text files under state/."
                ),
                "args": {
                    "query": "required keyword or semantic query",
                    "glob": "optional path glob relative to state/, default **/*",
                    "limit": "integer, default 8",
                },
            },
            {
                "name": "read_state_files",
                "description": (
                    "Read raw durable state files under state/. Use explicit relative paths or a glob when you need the "
                    "full text behind a search result."
                ),
                "args": {
                    "paths": "optional list of relative file paths under state/",
                    "glob": "optional path glob relative to state/",
                    "limit": "integer, default 5",
                    "max_chars": "integer, default 4000",
                },
            },
            {
                "name": "query_calendar_events",
                "description": (
                    "Search calendar events over a time window. Use query for semantic filtering over titles "
                    "and descriptions, for example 'talks', 'interviews', or 'demo'."
                ),
                "args": {
                    "query": "optional semantic or keyword query",
                    "days_past": "integer, default 30",
                    "days_future": "integer, default 365",
                    "limit": "integer, default 10",
                    "owned_only": "boolean",
                },
            },
            {
                "name": "find_free_slots",
                "description": "Find available calendar time slots in a date window.",
                "args": {
                    "duration_minutes": "integer",
                    "window": "optional natural-language date range such as 'tomorrow 2pm to 5pm' or 'next week'",
                    "start": "optional natural-language or ISO datetime",
                    "end": "optional natural-language or ISO datetime",
                    "days_ahead": "integer when start/end are omitted, default 7",
                    "limit": "integer, default 5",
                },
            },
        ]

    async def _run_ai_tool(self, tool_name: str, tool_args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        try:
            if tool_name == "execute_plan":
                return await self._ai_tool_execute_plan(tool_args, chat_id=chat_id)
            if tool_name == "execute_plan_batch":
                return await self._ai_tool_execute_plan_batch(tool_args, chat_id=chat_id)
            if tool_name == "query_state":
                return self._ai_tool_query_state(tool_args)
            if tool_name == "manage_task":
                return self._ai_tool_manage_task(tool_args, chat_id=chat_id)
            if tool_name == "manage_reminder":
                return self._ai_tool_manage_reminder(tool_args, chat_id=chat_id)
            if tool_name == "manage_note":
                return self._ai_tool_manage_note(tool_args, chat_id=chat_id)
            if tool_name == "manage_calendar_event":
                return await self._ai_tool_manage_calendar_event(tool_args, chat_id=chat_id)
            if tool_name == "search_state_files":
                return self._ai_tool_search_state_files(tool_args)
            if tool_name == "read_state_files":
                return self._ai_tool_read_state_files(tool_args)
            if tool_name == "query_calendar_events":
                return await self._ai_tool_query_calendar_events(tool_args)
            if tool_name == "find_free_slots":
                return await self._ai_tool_find_free_slots(tool_args)
            return {"ok": False, "error": f"Unknown AI tool: {tool_name}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _ai_tool_execute_plan(self, tool_args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        action = str(tool_args.get("action") or "").strip()
        if action not in AI_AGENT_EXECUTABLE_ACTIONS:
            return {"ok": False, "error": f"Action '{action}' is not available to the AI agent."}
        args = tool_args.get("args")
        dry_run = bool(tool_args.get("dry_run"))
        plan = AssistantPlan.model_validate(
            {
                "action": action,
                "args": args if isinstance(args, dict) else {},
            }
        )
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "action": action,
                "args": plan.args,
                "preview": self._describe_plan(plan),
            }
        result = await self._execute_plan(plan, chat_id=chat_id)
        return {
            "ok": True,
            "action": action,
            "args": plan.args,
            "result": result,
        }

    async def _ai_tool_execute_plan_batch(self, tool_args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        raw_plans = tool_args.get("plans")
        if not isinstance(raw_plans, list) or not raw_plans:
            return {"ok": False, "error": "execute_plan_batch requires a non-empty plans list."}
        dry_run = bool(tool_args.get("dry_run"))
        plans: list[AssistantPlan] = []
        for item in raw_plans:
            if not isinstance(item, dict):
                return {"ok": False, "error": "Each plan must be an object."}
            action = str(item.get("action") or "").strip()
            if action not in AI_AGENT_EXECUTABLE_ACTIONS:
                return {"ok": False, "error": f"Action '{action}' is not available to the AI agent."}
            plans.append(AssistantPlan.model_validate({"action": action, "args": item.get("args") or {}}))
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "count": len(plans),
                "previews": [self._describe_plan(plan) for plan in plans],
            }
        results = []
        for plan in plans:
            results.append(
                {
                    "action": plan.action,
                    "args": plan.args,
                    "result": await self._execute_plan(plan, chat_id=chat_id),
                }
            )
        return {"ok": True, "count": len(results), "results": results}

    def _ai_tool_manage_task(self, tool_args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        missing = object()
        operation = str(tool_args.get("operation") or "").strip().lower()
        if operation == "create":
            title = str(tool_args.get("title") or "").strip()
            if not title:
                return {"ok": False, "error": "manage_task create requires title."}
            args = {
                "title": title,
                "body": str(tool_args.get("body") or "").strip(),
                "tags": tool_args.get("tags") if isinstance(tool_args.get("tags"), list) else [],
                "kind": str(tool_args.get("kind") or "self"),
                "priority": str(tool_args.get("priority") or "medium"),
                "due_at": tool_args.get("due_at"),
                "reminder_at": tool_args.get("reminder_at"),
            }
            message = self._create_task(args, chat_id=chat_id)
            task = self.storage.find_task(title)
            return {"ok": True, "message": message, "task": self._serialize_task_for_ai(task) if task else None}

        if operation == "merge":
            raw_sources = tool_args.get("source_identifiers")
            if not isinstance(raw_sources, list) or len(raw_sources) < 2:
                return {"ok": False, "error": "manage_task merge requires at least two source_identifiers."}
            resolved_tasks: list[TaskRecord] = []
            seen_ids: set[str] = set()
            for item in raw_sources:
                source_identifier = str(item or "").strip()
                if not source_identifier:
                    continue
                resolved_info = self._resolve_entity_for_ai("task", source_identifier)
                if not resolved_info.get("ok"):
                    return resolved_info
                resolved_identifier = str(resolved_info.get("identifier") or "").strip()
                if not resolved_identifier or resolved_identifier in seen_ids:
                    continue
                task = self.storage.find_task(resolved_identifier)
                if task is None:
                    return {"ok": False, "error": f"I couldn't find task '{source_identifier}'."}
                seen_ids.add(task.id)
                resolved_tasks.append(task)
            if len(resolved_tasks) < 2:
                return {"ok": False, "error": "I need at least two distinct tasks to merge."}

            explicit_priority_raw = tool_args.get("priority")
            priority = self._normalize_priority(str(explicit_priority_raw)) if explicit_priority_raw is not None else None
            if explicit_priority_raw is not None and priority is None:
                return {"ok": False, "error": "Priority must be low, medium, or high."}
            priority_rank = {"high": 0, "medium": 1, "low": 2}
            merged_priority = priority or min(
                (task.priority for task in resolved_tasks),
                key=lambda item: priority_rank.get(item, 1),
            )

            due_value = tool_args.get("due_at", missing)
            reminder_value = tool_args.get("reminder_at", missing)
            due_at: Any = min((task.due_at for task in resolved_tasks if task.due_at is not None), default=None)
            reminder_at: Any = min((task.reminder_at for task in resolved_tasks if task.reminder_at is not None), default=None)
            if due_value is not missing:
                if due_value is None or self._is_clear_value(str(due_value)):
                    due_at = None
                else:
                    due_at, prompt, note = self._resolve_datetime_value(str(due_value), label="due date", require_time=False)
                    if prompt is not None or due_at is None:
                        return {"ok": False, "error": prompt or "I couldn't understand due_at."}
                    self._append_resolution_note(tool_args, note)
            if reminder_value is not missing:
                if reminder_value is None or self._is_clear_value(str(reminder_value)):
                    reminder_at = None
                else:
                    reminder_at, prompt, note = self._resolve_datetime_value(str(reminder_value), label="reminder time", require_time=True)
                    if prompt is not None or reminder_at is None:
                        return {"ok": False, "error": prompt or "I couldn't understand reminder_at."}
                    self._append_resolution_note(tool_args, note)

            merged_title = str(tool_args.get("title") or "").strip()
            if not merged_title:
                merged_title = " and ".join(task.title for task in resolved_tasks[:2])
                if len(resolved_tasks) > 2:
                    merged_title += " and related tasks"

            explicit_tags = tool_args.get("tags")
            if isinstance(explicit_tags, list):
                merged_tags = [str(tag).strip() for tag in explicit_tags if str(tag).strip()]
            else:
                merged_tags = list(dict.fromkeys(tag for task in resolved_tasks for tag in task.tags))

            explicit_body = str(tool_args.get("body") or "").strip()
            if explicit_body:
                merged_body = explicit_body
            else:
                body_lines = [f"- {task.title}" for task in resolved_tasks]
                detailed_bodies = [task for task in resolved_tasks if task.body.strip()]
                if detailed_bodies:
                    body_lines.append("")
                    for task in detailed_bodies:
                        body_lines.append(f"{task.title}: {task.body.strip()}")
                merged_body = "\n".join(body_lines).strip()

            new_task_id = self.storage._task_id_from_title(merged_title)
            undo_paths: list[Path] = [self.storage.tasks_index_path, self.storage.reminders_path]
            for task in resolved_tasks:
                undo_paths.append(self.storage.tasks_dir / f"{task.id}.md")
            undo_paths.append(self.storage.tasks_dir / f"{new_task_id}.md")
            self._remember_path_undo(
                chat_id,
                f"merge tasks into '{merged_title}'",
                undo_paths,
            )

            merged_task = self.storage.create_task(
                title=merged_title,
                body=merged_body,
                tags=merged_tags,
                priority=merged_priority,
                due_at=due_at,
                reminder_at=reminder_at,
            )
            complete_sources = bool(tool_args.get("complete_sources", True))
            if complete_sources:
                for task in resolved_tasks:
                    self.storage.complete_task(task.id)

            self._remember_reference_context(chat_id, "task", [(merged_task.id, merged_task.title)], mode="single")
            self._schedule_auto_sync()
            source_titles = ", ".join(task.title for task in resolved_tasks)
            message = (
                f"Merged {len(resolved_tasks)} tasks into: {merged_task.title}. "
                f"Sources: {source_titles}."
            )
            if complete_sources:
                message += " The original tasks were marked completed."
            return {
                "ok": True,
                "message": self._with_follow_up_list(message, self._list_tasks(chat_id=chat_id)),
                "task": self._serialize_task_for_ai(merged_task),
                "merged_from": [self._serialize_task_for_ai(task) for task in resolved_tasks],
            }

        identifier = str(tool_args.get("identifier") or "").strip()
        if not identifier:
            return {"ok": False, "error": "manage_task requires identifier for this operation."}
        resolved_info = self._resolve_entity_for_ai("task", identifier)
        if not resolved_info.get("ok"):
            return resolved_info
        resolved_identifier = str(resolved_info.get("identifier") or "").strip()

        if operation == "complete":
            message = self._complete_task({"identifier": resolved_identifier}, chat_id=chat_id)
            return {"ok": True, "message": message}
        if operation == "delete":
            message = self._delete_task({"identifier": resolved_identifier}, chat_id=chat_id)
            return {"ok": True, "message": message}
        if operation != "update":
            return {"ok": False, "error": f"Unsupported task operation: {operation}"}

        resolved = resolved_identifier
        existing = self.storage.find_task(resolved)
        if existing is None:
            return {"ok": False, "error": "I couldn't find that task."}
        self._remember_path_undo(
            chat_id,
            f"update task '{existing.title}'",
            [
                self.storage.tasks_dir / f"{existing.id}.md",
                self.storage.tasks_index_path,
                self.storage.reminders_path,
            ],
        )
        priority_raw = tool_args.get("priority")
        priority = self._normalize_priority(str(priority_raw)) if priority_raw is not None else None
        if priority_raw is not None and priority is None:
            return {"ok": False, "error": "Priority must be low, medium, or high."}

        due_value = tool_args.get("due_at", missing)
        reminder_value = tool_args.get("reminder_at", missing)
        due_at: Any = missing
        reminder_at: Any = missing
        if due_value is not missing:
            if due_value is None or self._is_clear_value(str(due_value)):
                due_at = None
            else:
                due_at, prompt, note = self._resolve_datetime_value(str(due_value), label="due date", require_time=False)
                if prompt is not None or due_at is None:
                    return {"ok": False, "error": prompt or "I couldn't understand due_at."}
                self._append_resolution_note(tool_args, note)
        if reminder_value is not missing:
            if reminder_value is None or self._is_clear_value(str(reminder_value)):
                reminder_at = None
            else:
                reminder_at, prompt, note = self._resolve_datetime_value(
                    str(reminder_value),
                    label="reminder time",
                    require_time=True,
                )
                if prompt is not None or reminder_at is None:
                    return {"ok": False, "error": prompt or "I couldn't understand reminder_at."}
                self._append_resolution_note(tool_args, note)

        update_kwargs: dict[str, Any] = {
            "title": str(tool_args.get("title")).strip() if tool_args.get("title") is not None else None,
            "body": str(tool_args.get("body")).strip() if tool_args.get("body") is not None else None,
            "tags": [str(tag).strip() for tag in tool_args.get("tags", [])] if isinstance(tool_args.get("tags"), list) else None,
            "priority": priority,
        }
        if due_at is not missing:
            update_kwargs["due_at"] = due_at
        if reminder_at is not missing:
            update_kwargs["reminder_at"] = reminder_at
        task = self.storage.update_task(
            resolved,
            **update_kwargs,
        )
        if task is None:
            return {"ok": False, "error": "I couldn't find that task."}
        self._remember_reference_context(chat_id, "task", [(task.id, task.title)], mode="single")
        self._schedule_auto_sync()
        return {"ok": True, "message": f"Updated task: {task.title}.", "task": self._serialize_task_for_ai(task)}

    def _ai_tool_manage_reminder(self, tool_args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        operation = str(tool_args.get("operation") or "").strip().lower()
        if operation == "create":
            text = str(tool_args.get("text") or "").strip()
            due_at = tool_args.get("due_at")
            if not text or due_at in {None, ""}:
                return {"ok": False, "error": "manage_reminder create requires text and due_at."}
            args = {
                "text": text,
                "due_at": str(due_at),
                "recurrence": str(tool_args.get("recurrence") or "").strip() or None,
            }
            message = self._create_reminder(args, chat_id=chat_id)
            reminder = next((item for item in self.storage.list_reminders() if item.text == text), None)
            return {"ok": True, "message": message, "reminder": self._serialize_reminder_for_ai(reminder) if reminder else None}

        identifier = str(tool_args.get("identifier") or "").strip()
        if not identifier:
            return {"ok": False, "error": "manage_reminder requires identifier for this operation."}
        resolved_info = self._resolve_entity_for_ai("reminder", identifier)
        if not resolved_info.get("ok"):
            return resolved_info
        resolved_identifier = str(resolved_info.get("identifier") or "").strip()
        if operation == "cancel":
            return {"ok": True, "message": self._cancel_reminder({"identifier": resolved_identifier}, chat_id=chat_id)}
        if operation == "snooze":
            due_at = tool_args.get("due_at")
            if due_at in {None, ""}:
                return {"ok": False, "error": "manage_reminder snooze requires due_at."}
            parsed, prompt, note = self._resolve_datetime_value(str(due_at), label="reminder time", require_time=True)
            if prompt is not None or parsed is None:
                return {"ok": False, "error": prompt or "I couldn't understand due_at."}
            self._append_resolution_note(tool_args, note)
            return {"ok": True, "message": self._snooze_reminder(resolved_identifier, due_at=parsed, chat_id=chat_id)}
        return {"ok": False, "error": f"Unsupported reminder operation: {operation}"}

    def _ai_tool_manage_note(self, tool_args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        operation = str(tool_args.get("operation") or "").strip().lower()
        if operation == "create":
            title = str(tool_args.get("title") or "").strip()
            if not title:
                return {"ok": False, "error": "manage_note create requires title."}
            message = self._create_note(
                {"title": title, "body": str(tool_args.get("body") or "").strip()},
                chat_id=chat_id,
            )
            note = self.storage.find_note(title)
            return {"ok": True, "message": message, "note": self._serialize_entry_for_ai(note) if note else None}

        identifier = str(tool_args.get("identifier") or "").strip()
        if not identifier:
            return {"ok": False, "error": "manage_note requires identifier for this operation."}
        resolved_info = self._resolve_entity_for_ai("note", identifier)
        if not resolved_info.get("ok"):
            return resolved_info
        resolved_identifier = str(resolved_info.get("identifier") or "").strip()
        if operation == "delete":
            return {"ok": True, "message": self._delete_note({"identifier": resolved_identifier}, chat_id=chat_id)}
        if operation != "update":
            return {"ok": False, "error": f"Unsupported note operation: {operation}"}

        resolved = resolved_identifier
        existing = self.storage.find_note(resolved)
        if existing is None:
            return {"ok": False, "error": "I couldn't find that note."}
        self._remember_path_undo(
            chat_id,
            f"update note '{existing.title}'",
            [self.storage.notes_dir / f"{existing.id}.md", self.storage.notes_index_path],
        )
        note = self.storage.update_note(
            resolved,
            title=str(tool_args.get("title")).strip() if tool_args.get("title") is not None else None,
            body=str(tool_args.get("body")).strip() if tool_args.get("body") is not None else None,
        )
        if note is None:
            return {"ok": False, "error": "I couldn't find that note."}
        self._remember_reference_context(chat_id, "note", [(note.id, note.title)], mode="single")
        self._schedule_auto_sync()
        return {"ok": True, "message": f"Updated note: {note.title}.", "note": self._serialize_entry_for_ai(note)}

    async def _ai_tool_manage_calendar_event(self, tool_args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        operation = str(tool_args.get("operation") or "").strip().lower()
        if operation == "create":
            title = str(tool_args.get("title") or "").strip()
            start_text = str(tool_args.get("start") or "").strip()
            if not title or not start_text:
                return {"ok": False, "error": "manage_calendar_event create requires title and start."}
            start, prompt, note = self._resolve_datetime_value(start_text, label="event time", require_time=True)
            if prompt is not None or start is None:
                return {"ok": False, "error": prompt or "I couldn't understand start."}
            self._append_resolution_note(tool_args, note)
            duration_minutes = min(max(int(tool_args.get("duration_minutes", 60) or 60), 5), 8 * 60)
            event = await self.calendar.create_event(
                title=title,
                start=start,
                end=start + timedelta(minutes=duration_minutes),
                description=str(tool_args.get("description") or "").strip(),
            )
            return {"ok": True, "event": self._serialize_event_for_ai(event), "message": f"Created event: {event.title}."}

        identifier = str(tool_args.get("identifier") or "").strip()
        if not identifier:
            return {"ok": False, "error": "manage_calendar_event requires identifier for update/delete."}
        if operation == "delete":
            deleted = await self.calendar.delete_owned_event(identifier)
            if not deleted:
                return {"ok": False, "error": "I couldn't find that assistant-owned event."}
            return {"ok": True, "message": f"Deleted event: {identifier}."}
        if operation != "update":
            return {"ok": False, "error": f"Unsupported calendar event operation: {operation}"}

        start_text = str(tool_args.get("start") or "").strip()
        duration_minutes_raw = tool_args.get("duration_minutes")
        start = None
        if start_text:
            start, prompt, note = self._resolve_datetime_value(start_text, label="event time", require_time=True)
            if prompt is not None or start is None:
                return {"ok": False, "error": prompt or "I couldn't understand start."}
            self._append_resolution_note(tool_args, note)
        duration_minutes = None
        if duration_minutes_raw is not None:
            duration_minutes = min(max(int(duration_minutes_raw or 60), 5), 8 * 60)
        end = start + timedelta(minutes=duration_minutes) if start is not None and duration_minutes is not None else None
        event = await self.calendar.update_owned_event(
            identifier,
            title=str(tool_args.get("title") or "").strip() or None,
            start=start,
            end=end,
            description=str(tool_args.get("description") or "").strip() or None,
        )
        if event is None:
            return {"ok": False, "error": "I couldn't find that assistant-owned event."}
        return {"ok": True, "event": self._serialize_event_for_ai(event), "message": f"Updated event: {event.title}."}

    def _ai_state_files(self, *, pattern: str = "**/*") -> list[Path]:
        state_root = self.settings.state_dir
        if not state_root.exists():
            return []
        try:
            iterator = state_root.glob(pattern or "**/*")
        except ValueError:
            iterator = state_root.glob("**/*")
        allowed_suffixes = {".md", ".json", ".txt"}
        paths: list[Path] = []
        for path in iterator:
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            if path.suffix.lower() == ".enc":
                continue
            if path.stat().st_size > 200_000:
                continue
            paths.append(path)
        return sorted(paths)

    def _state_relative_path(self, path: Path) -> str:
        return str(path.relative_to(self.settings.state_dir))

    def _ai_extract_state_snippet(self, text: str, query: str, *, limit: int = 220) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return ""
        query_tokens = [token for token in re.findall(r"[a-z0-9]+", query.lower()) if token]
        lowered = cleaned.lower()
        for token in query_tokens:
            position = lowered.find(token)
            if position >= 0:
                start = max(position - 80, 0)
                end = min(position + limit, len(cleaned))
                snippet = cleaned[start:end].strip()
                if start > 0:
                    snippet = "..." + snippet
                if end < len(cleaned):
                    snippet += "..."
                return snippet
        return cleaned[:limit] + ("..." if len(cleaned) > limit else "")

    def _ai_tool_search_state_files(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        query = str(tool_args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "search_state_files requires query."}
        limit = min(max(int(tool_args.get("limit", 8) or 8), 1), 20)
        paths = self._ai_state_files(pattern=str(tool_args.get("glob") or "**/*"))
        matches: list[tuple[int, dict[str, Any]]] = []
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            score = self._ai_search_score(query, text)
            if score <= 0:
                continue
            matches.append(
                (
                    score,
                    {
                        "path": self._state_relative_path(path),
                        "match_score": score,
                        "snippet": self._ai_extract_state_snippet(text, query),
                    },
                )
            )
        matches.sort(key=lambda item: (-item[0], item[1]["path"]))
        return {"ok": True, "count": len(matches), "items": [item for _, item in matches[:limit]]}

    def _ai_tool_read_state_files(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(tool_args.get("limit", 5) or 5), 1), 20)
        max_chars = min(max(int(tool_args.get("max_chars", 4000) or 4000), 200), 12000)
        requested_paths = [
            str(path).strip()
            for path in (tool_args.get("paths") or [])
            if str(path).strip()
        ] if isinstance(tool_args.get("paths"), list) else []
        resolved_paths: list[Path] = []
        state_root = self.settings.state_dir.resolve()
        for rel_path in requested_paths:
            candidate = (self.settings.state_dir / rel_path).resolve()
            if state_root not in candidate.parents and candidate != state_root:
                continue
            if candidate.is_file():
                resolved_paths.append(candidate)
        if not resolved_paths:
            resolved_paths = self._ai_state_files(pattern=str(tool_args.get("glob") or "**/*"))[:limit]
        items: list[dict[str, Any]] = []
        for path in resolved_paths[:limit]:
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                continue
            items.append(
                {
                    "path": self._state_relative_path(path),
                    "content": content[:max_chars] + ("..." if len(content) > max_chars else ""),
                }
            )
        return {"ok": True, "count": len(items), "items": items}

    def _ai_tool_query_state(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        entity = str(tool_args.get("entity") or "").strip().lower()
        query = str(tool_args.get("query") or "").strip()
        limit = min(max(int(tool_args.get("limit", 10) or 10), 1), 20)

        if entity == "tasks":
            task_kind = str(tool_args.get("kind") or "self").strip().lower()
            if task_kind not in {"self", "other"}:
                task_kind = "self"
            items = [
                self._serialize_task_for_ai(task)
                for task in self.storage.list_tasks(
                    include_completed=bool(tool_args.get("include_completed")),
                    sort_by=str(tool_args.get("sort_by") or "default"),
                    tag=str(tool_args.get("tag") or "").strip() or None,
                    kind=task_kind,
                )
            ]
        elif entity == "reminders":
            reminders = self.storage.list_reminders(include_inactive=bool(tool_args.get("include_inactive")))
            items = [self._serialize_reminder_for_ai(reminder) for reminder in reminders]
        elif entity == "notes":
            items = [self._serialize_entry_for_ai(note) for note in self.storage.list_notes()]
        elif entity == "projects":
            items = [self._serialize_entry_for_ai(project) for project in self.storage.list_projects()]
        elif entity == "plans":
            items = [self._serialize_entry_for_ai(plan) for plan in self.storage.list_plans()]
        elif entity == "preferences":
            items = [self._serialize_entry_for_ai(pref) for pref in self.storage.list_preferences()]
        else:
            return {"ok": False, "error": f"Unsupported entity: {entity}"}

        filtered = self._filter_ai_items(items, query=query)
        return {
            "ok": True,
            "entity": entity,
            "count": len(filtered),
            "items": filtered[:limit],
        }

    async def _ai_tool_query_calendar_events(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        query = str(tool_args.get("query") or "").strip()
        days_past = min(max(int(tool_args.get("days_past", 30) or 30), 0), 365)
        days_future = min(max(int(tool_args.get("days_future", 365) or 365), 0), 730)
        limit = min(max(int(tool_args.get("limit", 10) or 10), 1), 25)
        owned_only = bool(tool_args.get("owned_only"))
        time_min = now_utc() - timedelta(days=days_past)
        time_max = now_utc() + timedelta(days=days_future)
        fetch_limit = min(max(limit * 5, 20), 100)
        events = await self.calendar.list_upcoming_events(limit=fetch_limit, time_min=time_min, time_max=time_max)
        items = [
            self._serialize_event_for_ai(event)
            for event in events
            if not owned_only or event.owned_by_assistant
        ]
        filtered = self._filter_ai_items(items, query=query)
        return {
            "ok": True,
            "count": len(filtered),
            "items": filtered[:limit],
        }

    async def _ai_tool_find_free_slots(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        duration_minutes = min(max(int(tool_args.get("duration_minutes", 30) or 30), 5), 8 * 60)
        limit = min(max(int(tool_args.get("limit", 5) or 5), 1), 20)
        start_text = str(tool_args.get("start") or "").strip()
        end_text = str(tool_args.get("end") or "").strip()
        window_phrase = str(tool_args.get("window") or "").strip()
        if window_phrase:
            range_result = interpret_user_datetime_range(window_phrase, self.settings.default_timezone)
            if range_result.status == "ambiguous":
                return {"ok": False, "error": range_result.prompt or "I need a clearer free-slot window."}
            if range_result.status != "ok" or range_result.start is None or range_result.end is None:
                return {"ok": False, "error": "I couldn't understand the free-slot window."}
            window_start = range_result.start
            window_end = range_result.end
        elif start_text:
            window_start, prompt, _note = self._resolve_datetime_value(start_text, label="window start", require_time=False)
            if prompt is not None or window_start is None:
                return {"ok": False, "error": prompt or "I couldn't understand the free-slot window."}
        else:
            window_start = now_utc()
            window_end = None
        if window_phrase:
            pass
        elif end_text:
            window_end, prompt, _note = self._resolve_datetime_value(end_text, label="window end", require_time=False)
            if prompt is not None or window_end is None:
                return {"ok": False, "error": prompt or "I couldn't understand the free-slot window."}
        else:
            days_ahead = min(max(int(tool_args.get("days_ahead", 7) or 7), 1), 30)
            window_end = window_start + timedelta(days=days_ahead) if window_start else None
        if window_start is None or window_end is None or window_end <= window_start:
            return {"ok": False, "error": "I couldn't understand the free-slot window."}
        slots = await self.calendar.find_free_slots(
            window_start=window_start,
            window_end=window_end,
            duration_minutes=duration_minutes,
        )
        return {
            "ok": True,
            "count": len(slots),
            "items": [
                {
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "display_start": format_local(slot.start, self.settings.default_timezone),
                    "display_end": format_local(slot.end, self.settings.default_timezone),
                }
                for slot in slots[:limit]
            ],
        }

    def _record_openai_trace(self, chat_id: str, trace: dict[str, Any]) -> None:
        record = OpenAITraceRecord(
            id=str(trace.get("id") or uuid4().hex),
            chat_id=chat_id,
            created_at=now_utc(),
            mode=str(trace.get("mode") or "planner"),
            user_message=str(trace.get("user_message") or ""),
            summary=str(trace.get("summary") or ""),
            context_snippets=[str(item) for item in (trace.get("context_snippets") or [])],
            model=str(trace.get("model") or self.settings.openai_model),
            base_url=str(trace.get("base_url") or self.settings.openai_base_url),
            request_payload=dict(trace.get("request_payload") or {}),
            system_prompt=str(trace.get("system_prompt") or ""),
            user_prompt=str(trace.get("user_prompt") or ""),
            response_content=str(trace.get("response_content") or ""),
            parsed_plan=dict(trace.get("parsed_plan") or {}),
            usage=dict(trace.get("usage") or {}),
            response_headers={str(k): str(v) for k, v in dict(trace.get("response_headers") or {}).items()},
            rate_limits={str(k): str(v) for k, v in dict(trace.get("rate_limits") or {}).items()},
            tool_steps=[dict(step) for step in (trace.get("tool_steps") or []) if isinstance(step, dict)],
            final_response=str(trace.get("final_response") or ""),
            automation_candidate=dict(trace.get("automation_candidate") or {}),
            request_id=str(trace.get("request_id") or ""),
            error=str(trace.get("error") or ""),
            duration_ms=int(trace["duration_ms"]) if isinstance(trace.get("duration_ms"), int) else None,
        )
        self.storage.append_openai_trace(record)

    def _serialize_task_for_ai(self, task: TaskRecord) -> dict[str, Any]:
        return {
            "id": task.id,
            "title": task.title,
            "body": self._truncate_text(task.body or "", limit=300),
            "tags": list(task.tags),
            "kind": task.kind,
            "status": task.status,
            "priority": task.priority,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "reminder_at": task.reminder_at.isoformat() if task.reminder_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }

    def _serialize_reminder_for_ai(self, reminder: ReminderRecord) -> dict[str, Any]:
        return {
            "id": reminder.id,
            "text": reminder.text,
            "due_at": reminder.due_at.isoformat(),
            "status": reminder.status,
            "recurrence": reminder.recurrence,
            "source_type": reminder.source_type,
            "source_id": reminder.source_id,
        }

    def _serialize_entry_for_ai(self, record: Any) -> dict[str, Any]:
        return {
            "id": str(record.id),
            "title": str(record.title),
            "body": self._truncate_text(str(getattr(record, "body", "") or ""), limit=400),
            "updated_at": record.updated_at.isoformat(),
        }

    def _serialize_event_for_ai(self, event: Any) -> dict[str, Any]:
        return {
            "id": str(event.id),
            "title": str(event.title),
            "description": self._truncate_text(str(event.description or ""), limit=300),
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "owned_by_assistant": bool(event.owned_by_assistant),
        }

    def _filter_ai_items(self, items: list[dict[str, Any]], *, query: str) -> list[dict[str, Any]]:
        if not query:
            return items
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in items:
            haystacks = [
                str(value)
                for key, value in item.items()
                if key in {"title", "body", "text", "description", "id", "tags"}
            ]
            score = self._ai_search_score(query, " ".join(haystacks))
            if score > 0:
                enriched = dict(item)
                enriched["match_score"] = score
                scored.append((score, enriched))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("title") or pair[1].get("text") or pair[1].get("id") or "")))
        return [item for _, item in scored]

    @staticmethod
    def _ai_search_score(query: str, text: str) -> int:
        def normalize(value: str) -> list[str]:
            tokens = re.findall(r"[a-z0-9]+", value.lower())
            normalized: list[str] = []
            for token in tokens:
                normalized.append(token)
                if len(token) > 3 and token.endswith("s"):
                    normalized.append(token[:-1])
            return normalized

        query_tokens = normalize(query)
        text_tokens = normalize(text)
        if not query_tokens or not text_tokens:
            return 0
        joined = " ".join(text_tokens)
        score = 0
        for token in query_tokens:
            if token in text_tokens:
                score += 5
            elif token in joined:
                score += 3
        if score == 0 and query.strip().lower() in text.lower():
            score = 4
        return score

    def _show_ai_status(self, chat_id: str = "") -> str:
        planner_config = self._planner_config()
        traces = self.storage.list_openai_traces(limit=1)
        lines = [
            "OpenAI status",
            "",
            f"Configured: {'yes' if planner_config['configured'] else 'no'}",
            f"Admin stats key: {'yes' if planner_config['admin_configured'] else 'no'}",
            f"Model: {planner_config['model'] or '(unset)'}",
            f"Base URL: {planner_config['base_url'] or '(unset)'}",
            f"Stored traces: {len(self.storage.list_openai_traces(limit=50))}",
        ]
        if chat_id:
            current = self.storage.get_ai_run(chat_id)
            if current:
                lines.append(f"Current chat run: {str(current.get('status') or 'unknown')}")
        if not traces:
            lines.append("Last AI call: none recorded yet.")
            return "\n".join(lines)
        trace = traces[0]
        lines.extend(
            [
                f"Last AI call: {format_local(trace.created_at, self.settings.default_timezone)}",
                f"Last action: {self._trace_label(trace)}",
                f"Request ID: {trace.request_id or '(none)'}",
                f"Tokens: {self._format_usage_line(trace.usage)}",
            ]
        )
        if trace.tool_steps:
            lines.append(f"Tool steps: {len(trace.tool_steps)}")
        if trace.rate_limits:
            lines.append(f"Rate limits: {self._format_rate_limit_line(trace.rate_limits)}")
        if trace.error:
            lines.append(f"Last error: {trace.error}")
        return "\n".join(lines)

    def _show_debug_info(self) -> str:
        git = self._git_debug_info()
        lines = [
            "Debug info",
            "",
            f"Version: {__version__}",
            f"Repo root: {self.settings.repo_root}",
            f"Timezone: {self.settings.default_timezone}",
            f"Git backup enabled: {'yes' if self.settings.git_backup_enabled else 'no'}",
            f"Git branch: {git['branch']}",
            f"Git commit: {git['commit']}",
            f"Git short commit: {git['short_commit']}",
            f"Git dirty: {git['dirty']}",
        ]
        if git["commit_subject"]:
            lines.append(f"Commit subject: {git['commit_subject']}")
        if git["error"]:
            lines.append(f"Git error: {git['error']}")
        return "\n".join(lines)

    def _show_system_health(self) -> str:
        maintenance = self.storage.run_maintenance()
        backup = self.backup_service.read_status()
        ai_run = self.storage.get_ai_run(str(self.settings.telegram_allowed_user_id)) or {}
        quarantine = self.storage.list_quarantined_files()
        lines = [
            "System health",
            "",
            f"Backup mode: {self.settings.git_backup_mode}",
            f"Backup remote: {self.settings.git_backup_remote}/{self.settings.git_backup_branch}",
            f"Last backup status: {backup.get('status', 'unknown')}",
        ]
        if backup.get("updated_at"):
            backup_time = parse_user_datetime(backup["updated_at"], self.settings.default_timezone)
            if backup_time is not None:
                lines.append(f"Last backup update: {format_local(backup_time, self.settings.default_timezone)}")
        if backup.get("detail"):
            lines.append(f"Backup detail: {backup['detail']}")
        if backup.get("last_success_at"):
            success_time = parse_user_datetime(backup["last_success_at"], self.settings.default_timezone)
            if success_time is not None:
                lines.append(f"Last backup success: {format_local(success_time, self.settings.default_timezone)}")
        lines.extend(
            [
                f"Pending transactions: {maintenance['pending_transactions']}",
                f"Quarantined files: {maintenance['quarantine_count']}",
                f"Maintenance pruned: {maintenance['pruned_quarantine']}",
                f"Trace trims: {maintenance['trimmed_openai']}",
                f"Processed update trims: {maintenance['trimmed_updates']}",
            ]
        )
        repaired = [str(item) for item in maintenance.get("repaired") or [] if str(item)]
        if repaired:
            lines.append("Repaired on this check: " + ", ".join(repaired))
        if quarantine:
            latest = quarantine[0]
            modified_at = latest.get("modified_at")
            display_time = ""
            if hasattr(modified_at, "isoformat"):
                parsed = parse_user_datetime(modified_at.isoformat(), self.settings.default_timezone)
                if parsed is not None:
                    display_time = format_local(parsed, self.settings.default_timezone)
            lines.append(
                "Latest quarantine: "
                + str(latest.get("name") or "(unknown)")
                + (f" ({display_time})" if display_time else "")
            )
        if ai_run:
            lines.append(f"Last AI status: {str(ai_run.get('status') or 'unknown')}")
            if ai_run.get("updated_at"):
                updated = parse_user_datetime(str(ai_run["updated_at"]), self.settings.default_timezone)
                if updated is not None:
                    lines.append(f"Last AI update: {format_local(updated, self.settings.default_timezone)}")
        return "\n".join(lines)

    def _git_debug_info(self) -> dict[str, str]:
        branch = self._run_git_debug_command("rev-parse", "--abbrev-ref", "HEAD")
        commit = self._run_git_debug_command("rev-parse", "HEAD")
        short_commit = self._run_git_debug_command("rev-parse", "--short", "HEAD")
        status = self._run_git_debug_command("status", "--short")
        subject = self._run_git_debug_command("log", "-1", "--pretty=%s")
        error = ""
        if branch.startswith("error:"):
            error = branch.removeprefix("error:").strip()
        elif commit.startswith("error:"):
            error = commit.removeprefix("error:").strip()
        elif short_commit.startswith("error:"):
            error = short_commit.removeprefix("error:").strip()
        elif status.startswith("error:"):
            error = status.removeprefix("error:").strip()
        elif subject.startswith("error:"):
            error = subject.removeprefix("error:").strip()
        return {
            "branch": branch if not branch.startswith("error:") else "(unavailable)",
            "commit": commit if not commit.startswith("error:") else "(unavailable)",
            "short_commit": short_commit if not short_commit.startswith("error:") else "(unavailable)",
            "dirty": "yes" if status and not status.startswith("error:") else "no",
            "commit_subject": subject if not subject.startswith("error:") else "",
            "error": error,
        }

    def _run_git_debug_command(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.settings.repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                detail = (exc.stderr or exc.stdout or str(exc)).strip()
            else:
                detail = str(exc).strip()
            return f"error: {detail or 'git command failed'}"
        return result.stdout.strip()

    def _show_ai_trace(self) -> str:
        trace = self._latest_openai_trace()
        if trace is None:
            return "No AI calls have been logged yet."
        lines = [
            "Last OpenAI trace",
            "",
            f"Time: {format_local(trace.created_at, self.settings.default_timezone)}",
            f"Model: {trace.model or '(unset)'}",
            f"Request ID: {trace.request_id or '(none)'}",
            f"Duration: {trace.duration_ms or 0} ms",
            f"Usage: {self._format_usage_line(trace.usage)}",
            f"Action: {self._trace_label(trace)}",
            f"User message: {trace.user_message or '(empty)'}",
        ]
        if trace.rate_limits:
            lines.append(f"Rate limits: {self._format_rate_limit_line(trace.rate_limits)}")
        if trace.error:
            lines.append(f"Error: {trace.error}")
        if trace.tool_steps:
            lines.extend(["", "Tool steps:"])
            for step in trace.tool_steps[:8]:
                tool_name = str(step.get("tool") or "(unknown)")
                args = self._truncate_text(self._json_text(dict(step.get("args") or {})), limit=240)
                result = self._truncate_text(self._json_text(dict(step.get("result") or {})), limit=400)
                lines.extend(
                    [
                        f"- {tool_name}",
                        f"  args: {args}",
                        f"  result: {result}",
                    ]
                )
        lines.extend(
            [
                "",
                "Parsed plan:",
                self._truncate_text(self._json_text(trace.parsed_plan), limit=1200),
                "",
                "Raw model content:",
                self._truncate_text(trace.response_content or "(empty)", limit=1200),
            ]
        )
        if trace.final_response:
            lines.extend(["", "Final response:", self._truncate_text(trace.final_response, limit=800)])
        return "\n".join(lines)

    def _show_ai_traces(self) -> str:
        traces = self.storage.list_openai_traces(limit=5)
        if not traces:
            return "No AI calls have been logged yet."
        lines = ["Recent OpenAI traces", ""]
        for index, trace in enumerate(traces, start=1):
            lines.append(
                f"{index}. {format_local(trace.created_at, self.settings.default_timezone)} | "
                f"{self._trace_label(trace)} | "
                f"{self._format_usage_line(trace.usage)} | "
                f"{self._truncate_text(trace.user_message or '(empty)', limit=80)}"
            )
        return "\n".join(lines)

    def _show_ai_prompt(self) -> str:
        trace = self._latest_openai_trace()
        if trace is None:
            return "No AI calls have been logged yet."
        return "\n".join(
            [
                "Last OpenAI prompt",
                "",
                "System prompt:",
                self._truncate_text(trace.system_prompt or "(empty)", limit=1600),
                "",
                "User prompt:",
                self._truncate_text(trace.user_prompt or "(empty)", limit=1600),
            ]
        )

    def _show_ai_context(self) -> str:
        trace = self._latest_openai_trace()
        if trace is None:
            return "No AI calls have been logged yet."
        context_text = "\n".join(f"- {item}" for item in trace.context_snippets) or "- (none)"
        return "\n".join(
            [
                "Last OpenAI context",
                "",
                "Conversation summary:",
                self._truncate_text(trace.summary or "(empty)", limit=1200),
                "",
                "Context snippets:",
                self._truncate_text(context_text, limit=1400),
            ]
        )

    def _show_ai_backlog(self) -> str:
        backlog = self.storage.read_automation_ideas().strip()
        if not backlog or "\n## " not in backlog:
            return "No automation ideas have been logged yet."
        tail = backlog[-3000:] if len(backlog) > 3000 else backlog
        if len(backlog) > 3000:
            tail = "...[truncated]\n" + tail
        return "\n".join(["AI automation backlog", "", tail])

    async def _show_ai_usage(self, args: dict[str, Any]) -> str:
        days = max(int(args.get("days", 7) or 7), 1)
        local = self._local_openai_usage(days)
        lines = [
            f"AI usage for last {days} day(s)",
            "",
            f"Local traced calls: {local['calls']}",
            f"Local traced tokens: {local['total_tokens']} total "
            f"(prompt {local['prompt_tokens']}, completion {local['completion_tokens']})",
        ]
        getter = getattr(self.planner, "get_usage_summary", None)
        if callable(getter):
            try:
                org = await getter(days=days)
            except Exception as exc:
                lines.append(f"Org usage API: error: {exc}")
            else:
                if org.get("available"):
                    lines.extend(
                        [
                            f"Org model requests: {org.get('num_model_requests', 0)}",
                            f"Org input tokens: {org.get('input_tokens', 0)}",
                            f"Org output tokens: {org.get('output_tokens', 0)}",
                            f"Org cached input tokens: {org.get('input_cached_tokens', 0)}",
                        ]
                    )
                else:
                    lines.append(f"Org usage API: unavailable: {org.get('reason') or 'not available'}")
        return "\n".join(lines)

    async def _show_ai_costs(self, args: dict[str, Any]) -> str:
        days = max(int(args.get("days", 30) or 30), 1)
        lines = [f"AI costs for last {days} day(s)", ""]
        getter = getattr(self.planner, "get_cost_summary", None)
        if callable(getter):
            try:
                costs = await getter(days=days)
            except Exception as exc:
                lines.append(f"Org costs API: error: {exc}")
            else:
                if costs.get("available"):
                    lines.extend(
                        [
                            f"Org cost: {costs.get('total_cost', 0)} {str(costs.get('currency') or 'usd').upper()}",
                            f"Line items: {costs.get('line_items', 0)}",
                        ]
                    )
                else:
                    lines.append(f"Org costs API: unavailable: {costs.get('reason') or 'not available'}")
        else:
            lines.append("Org costs API: unavailable from the configured planner.")
        lines.append("Exact per-call cost is not estimated locally because pricing can change independently of this codebase.")
        return "\n".join(lines)

    async def _show_ai_credits(self, args: dict[str, Any]) -> str:
        days = max(int(args.get("days", 30) or 30), 1)
        lines = [
            "AI credits",
            "",
            "Exact remaining prepaid credits are not exposed through the documented OpenAI API used here.",
            "Closest available signals are organization costs, local traced token usage, and the latest rate-limit headers.",
            "",
        ]
        lines.append(await self._show_ai_costs({"days": days}))
        lines.append("")
        lines.append(await self._show_ai_usage({"days": min(days, 30)}))
        trace = self._latest_openai_trace()
        if trace is not None and trace.rate_limits:
            lines.extend(["", f"Latest rate limits: {self._format_rate_limit_line(trace.rate_limits)}"])
        return "\n".join(lines)

    def _planner_config(self) -> dict[str, Any]:
        describe = getattr(self.planner, "describe_config", None)
        if callable(describe):
            config = describe()
            if isinstance(config, dict):
                return {
                    "configured": bool(config.get("configured")),
                    "admin_configured": bool(config.get("admin_configured")),
                    "model": str(config.get("model") or self.settings.openai_model),
                    "base_url": str(config.get("base_url") or self.settings.openai_base_url),
                }
        return {
            "configured": bool(self.settings.openai_api_key),
            "admin_configured": bool(getattr(self.settings, "openai_admin_api_key", "")),
            "model": self.settings.openai_model,
            "base_url": self.settings.openai_base_url,
        }

    def _latest_openai_trace(self) -> OpenAITraceRecord | None:
        traces = self.storage.list_openai_traces(limit=1)
        return traces[0] if traces else None

    @staticmethod
    def _trace_label(trace: OpenAITraceRecord) -> str:
        action = str(trace.parsed_plan.get("action") or "").strip()
        if action:
            return action
        if trace.mode == "agent":
            return "agent"
        return "(unknown)"

    def _local_openai_usage(self, days: int) -> dict[str, int]:
        cutoff = now_utc() - timedelta(days=max(days, 1))
        traces = [trace for trace in self.storage.list_openai_traces(limit=50) if trace.created_at >= cutoff]
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        for trace in traces:
            usage = trace.usage
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)
        return {
            "calls": len(traces),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _json_text(payload: dict[str, Any]) -> str:
        return json.dumps(payload, indent=2, sort_keys=True)

    @staticmethod
    def _truncate_text(text: str, *, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(limit - 16, 0)].rstrip() + "\n...[truncated]"

    @staticmethod
    def _format_usage_line(usage: dict[str, Any]) -> str:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        if total_tokens <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
            return "no token data"
        return f"{total_tokens} total (prompt {prompt_tokens}, completion {completion_tokens})"

    @staticmethod
    def _format_rate_limit_line(rate_limits: dict[str, str]) -> str:
        pieces = []
        if rate_limits.get("remaining_requests") or rate_limits.get("limit_requests"):
            pieces.append(
                f"requests {rate_limits.get('remaining_requests', '?')}/{rate_limits.get('limit_requests', '?')}"
            )
        if rate_limits.get("remaining_tokens") or rate_limits.get("limit_tokens"):
            pieces.append(
                f"tokens {rate_limits.get('remaining_tokens', '?')}/{rate_limits.get('limit_tokens', '?')}"
            )
        if rate_limits.get("reset_requests"):
            pieces.append(f"request reset {rate_limits['reset_requests']}")
        if rate_limits.get("reset_tokens"):
            pieces.append(f"token reset {rate_limits['reset_tokens']}")
        return ", ".join(pieces) or "no rate-limit data"

    def _help_text(self, topic: str = "overview") -> str:
        raw_topic = topic.strip().lower()
        if raw_topic in {"plan", "plans"}:
            return "Plans have been removed from the main interface. Use notes instead."
        normalized = HELP_TOPIC_ALIASES.get(raw_topic)
        if normalized is None and raw_topic:
            return "\n".join(
                [
                    "I don't have a help topic by that name.",
                    "",
                    "Try one of:",
                    "`help tasks`, `help reminders`, `help calendar`, `help notes`",
                    "`help projects`, `help preferences`, `help people`, `help system`, `help ai`",
                    "`help all` shows the full command list.",
                ]
            )
        normalized = normalized or "overview"
        if normalized == "overview":
            return "\n".join(
                [
                    "Personal Assistant",
                    "",
                    "Daily actions:",
                    "`show` for your dashboard.",
                    "`tasks`, `reminders`, `calendar`, `notes` to browse each area.",
                    "`task <title>`, `remind me to <text> <time>`, `schedule <title> <time>`, `note <title>` to add things.",
                    "`done`, `delete`, and `undo` for follow-up actions.",
                    "`ai <request>` for broader or multi-step help, and `ai status` to inspect AI state.",
                    "",
                    "Telegram flow:",
                    "Use the menu and reply buttons where possible. After a list, short follow-ups like `done 1`, `delete it`, or `remind it tomorrow 9am` usually work.",
                    "If I am not sure what you mean, I will ask a follow-up question instead of guessing.",
                    "",
                    "Help topics:",
                    "`help tasks`, `help reminders`, `help calendar`, `help notes`",
                    "`help preferences`, `help people`, `help system`, `help ai`",
                    "`help all` shows the full command list.",
                ]
            )
        if normalized == "all":
            lines = [
                "Personal Assistant - full command list",
                "",
                "This teaches the main commands to learn first. Older aliases still work even when they are not listed here.",
                "",
            ]
            for section_name in HELP_TOPIC_ORDER:
                if section_name in {"overview", "all"}:
                    continue
                lines.extend(self._help_section_lines(section_name, expanded=True))
                lines.extend(["", ""])
            lines.append("Use `help <topic>` to show just one section.")
            return "\n".join(lines).rstrip()
        return "\n".join(self._help_section_lines(normalized))

    def _help_section_lines(self, topic: str, *, expanded: bool = False) -> list[str]:
        section = HELP_SECTIONS[topic]
        lines = [str(section["title"])]
        if expanded:
            lines.append("")
        for command_text, description in section["lines"]:
            lines.append(f"{command_text} - {description}")
            if expanded:
                lines.append("")
        examples = [str(item) for item in section.get("examples") or []]
        if examples:
            lines.append("")
            for example in examples:
                lines.append(example)
                if expanded:
                    lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    @staticmethod
    def _help_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "🧭 Start Here", "callback_data": "help:overview"},
                    {"text": "📋 Tasks", "callback_data": "help:tasks"},
                ],
                [
                    {"text": "⏰ Reminders", "callback_data": "help:reminders"},
                    {"text": "📅 Calendar", "callback_data": "help:calendar"},
                ],
                [
                    {"text": "📝 Notes", "callback_data": "help:notes"},
                    {"text": "⚙️ Preferences", "callback_data": "help:preferences"},
                ],
                [
                    {"text": "👤 People", "callback_data": "help:people"},
                    {"text": "🛠 System", "callback_data": "help:system"},
                ],
                [
                    {"text": "🤖 AI", "callback_data": "help:ai"},
                    {"text": "📚 Show All", "callback_data": "help:all"},
                ],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    def _show_all(self, *, chat_id: str = "") -> str:
        sections = [
            self._list_tasks(chat_id=chat_id, remember_context=False),
            self._list_tasks({"kind": "other"}, chat_id=chat_id, remember_context=False),
            self._list_reminders(chat_id=chat_id, remember_context=False),
            self._list_projects(chat_id=chat_id, remember_context=False),
        ]
        self._clear_reference_context(chat_id)
        return "\n\n".join(sections)

    @staticmethod
    def _with_follow_up_list(prefix: str, follow_up: str) -> str:
        return f"{prefix}\n\n{follow_up}"

    def _prompt_task_create(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "task_input"})
        return "Enter the task title. You can also add `#tags`, `priority <low|medium|high>`, `due <date>`, or `remind me <time>`."

    def _prompt_task_complete(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "task_complete_input"})
        return "Which task should I mark completed? Reply with the task number, id, or title."

    def _prompt_task_delete(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "task_delete_input"})
        return "Which task should I delete? Reply with the task number, id, or title."

    def _prompt_task_current(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "task_current_input"})
        tasks_view = self._list_tasks(chat_id=chat_id)
        return f"{tasks_view}\n\nWhich task should be current? Reply with the task number, id, or title."

    def _prompt_reminder_create(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "reminder_input"})
        return "What reminder should I set? Reply with the text and time, for example `Call doctor tomorrow 3pm`."

    def _prompt_reminder_delete(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "reminder_delete_input"})
        reminders_view = self._list_reminders(chat_id=chat_id)
        return f"{reminders_view}\n\nWhich reminder should I delete? Reply with the reminder number, id, or text."

    def _prompt_note_create(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "note_input"})
        return "What note should I save? Reply with `title` or `title: description`."

    def _prompt_note_delete(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "note_delete_input"})
        return "Which note should I delete? Reply with the note number, id, or title."

    def _prompt_note_update(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "note_update_select"})
        notes_view = self._list_notes(chat_id=chat_id)
        return f"{notes_view}\n\nWhich note should I update? Reply with the note number, id, or title."

    def _prompt_calendar_create(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "calendar_event_input"})
        return "What event should I create? Reply with something like `Demo review tomorrow 3pm for 45 minutes`."

    def _prompt_free_slot(self, chat_id: str) -> str:
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "free_slot_input"})
        return "What free slot should I look for? Reply with something like `30 minutes tomorrow afternoon`."

    def _private_feature_unavailable(self) -> str:
        return "Private data encryption is not configured (PEOPLE_ENCRYPTION_KEY not set)."

    def _private_system_password(self) -> str:
        return self.settings.people_encryption_key.strip()

    def _request_private_task_action(self, chat_id: str, operation: str, args: dict[str, Any]) -> str:
        if not self.settings.people_encryption_key:
            return self._private_feature_unavailable()
        if chat_id:
            self.storage.set_pending(
                chat_id,
                {"type": "private_auth", "entity": "task", "operation": operation, "args": dict(args)},
            )
        if operation == "list":
            return "Enter the private-data password to view private tasks:"
        if operation == "create":
            title = str(args.get("title") or "").strip()
            if not title:
                return "I need a private task title."
            return f"Enter the private-data password to create private task `{title}`:"
        identifier = str(args.get("identifier") or "").strip()
        labels = {
            "view": "view",
            "update": "update",
            "complete": "complete",
            "delete": "delete",
        }
        verb = labels.get(operation, "use")
        return f"Enter the private-data password to {verb} private task `{identifier}`:"

    def _request_private_reminder_action(self, chat_id: str, operation: str, args: dict[str, Any]) -> str:
        if not self.settings.people_encryption_key:
            return self._private_feature_unavailable()
        if chat_id:
            self.storage.set_pending(
                chat_id,
                {"type": "private_auth", "entity": "reminder", "operation": operation, "args": dict(args)},
            )
        if operation == "list":
            return "Enter the private-data password to view private reminders:"
        if operation == "create":
            text = str(args.get("text") or "").strip()
            if not text:
                return "I need private reminder text."
            return f"Enter the private-data password to create private reminder `{text}`:"
        if operation == "reveal":
            return "Enter the private-data password to reveal this reminder:"
        identifier = str(args.get("identifier") or "").strip()
        labels = {
            "view": "view",
            "update": "update",
            "cancel": "cancel",
            "ack": "acknowledge",
        }
        verb = labels.get(operation, "use")
        return f"Enter the private-data password to {verb} private reminder `{identifier}`:"

    @staticmethod
    def _private_entity_label(entity: str) -> str:
        return {
            "task": "private task",
            "reminder": "private reminder",
        }.get(entity, "private item")

    def _queue_private_selection(
        self,
        *,
        chat_id: str,
        entity: str,
        operation: str,
        args: dict[str, Any],
        choices: list[tuple[str, str]],
        suggestion: bool,
    ) -> str:
        trimmed = choices[:8]
        if chat_id:
            self.storage.set_pending(
                chat_id,
                {
                    "type": "private_select",
                    "entity": entity,
                    "operation": operation,
                    "args": dict(args),
                    "ids": [item_id for item_id, _ in trimmed],
                },
            )
        label = self._private_entity_label(entity)
        lines = [f"I found multiple {label}s. Which one did you mean?"]
        if suggestion:
            lines = [f"I couldn't find that {label} exactly. Did you mean one of these?"]
        for index, (_, title) in enumerate(trimmed, 1):
            lines.append(f"{index}. {title}")
        lines.append("Reply with the number or id, or /cancel.")
        return "\n".join(lines)

    def _resolve_private_identifier(
        self,
        *,
        chat_id: str,
        entity: str,
        operation: str,
        identifier: str,
        args: dict[str, Any],
        choices: list[tuple[str, str]],
    ) -> tuple[str | None, str | None]:
        resolved, _, follow_up = self._resolve_identifier_from_choices(identifier, choices)
        if resolved is not None:
            return resolved, None
        wanted = identifier.strip().lower()
        if follow_up == "ambiguous":
            matches = [
                (item_id, title)
                for item_id, title in choices
                if wanted and (wanted in title.lower() or title.lower().startswith(wanted) or item_id.lower().startswith(wanted))
            ]
            return None, self._queue_private_selection(
                chat_id=chat_id,
                entity=entity,
                operation=operation,
                args=args,
                choices=matches or choices,
                suggestion=False,
            )
        labels_by_lower = {title.lower(): (item_id, title) for item_id, title in choices}
        suggestions = [
            labels_by_lower[label]
            for label in get_close_matches(wanted, list(labels_by_lower), n=min(5, len(labels_by_lower)), cutoff=0.55)
        ] if wanted else []
        if suggestions:
            return None, self._queue_private_selection(
                chat_id=chat_id,
                entity=entity,
                operation=operation,
                args=args,
                choices=suggestions,
                suggestion=True,
            )
        return None, f"I couldn't find that {self._private_entity_label(entity)}."

    def _queue_private_datetime_clarification(
        self,
        *,
        chat_id: str,
        entity: str,
        operation: str,
        args: dict[str, Any],
        field: str,
        label: str,
        raw_text: str,
        require_time: bool,
        allow_clear: bool = False,
        prompt: str | None = None,
    ) -> str:
        reply = prompt or self._datetime_clarification_prompt(
            label=label,
            raw_text=raw_text,
            require_time=require_time,
        )
        if chat_id:
            self.storage.set_pending(
                chat_id,
                {
                    "type": "private_datetime",
                    "entity": entity,
                    "operation": operation,
                    "args": dict(args),
                    "field": field,
                    "label": label,
                    "require_time": require_time,
                    "allow_clear": allow_clear,
                },
            )
        return reply

    def _list_private_tasks_secure(
        self,
        password: str,
        *,
        args: dict[str, Any] | None = None,
    ) -> str:
        args = args or {}
        sort_by = str(args.get("sort_by", "default")).strip().lower() or "default"
        tag = str(args.get("tag", "")).strip() or None
        tasks = self.storage.list_private_tasks(password, sort_by=sort_by, tag=tag)
        if not tasks:
            if tag:
                return f"You have no private tasks tagged #{tag.lstrip('#')}."
            return "You have no private tasks."
        title = "Private tasks:"
        if tag:
            title = f"Private tasks tagged #{tag.lstrip('#')}:"
        elif sort_by == "due":
            title = "Private tasks by due date:"
        elif sort_by == "remind":
            title = "Private tasks by reminder:"
        elif sort_by == "priority":
            title = "Private tasks by priority:"
        lines = [title]
        for index, task in enumerate(tasks, 1):
            line = f"{index}. {self._format_priority_label(task.priority)} - {task.title}"
            if task.tags:
                line += f" [{', '.join(task.tags)}]"
            if task.due_at:
                line += f" (due {format_local(task.due_at, self.settings.default_timezone)})"
            if task.reminder_at:
                line += f" (remind {format_local(task.reminder_at, self.settings.default_timezone)})"
            lines.append(line)
        return "\n".join(lines)

    def _list_private_reminders_secure(self, password: str) -> str:
        reminders = self.storage.list_private_reminders(password)
        if not reminders:
            return "You have no active private reminders."
        lines = ["Private reminders:"]
        for index, reminder in enumerate(reminders, 1):
            line = f"{index}. {reminder.text} — {format_local(reminder.due_at, self.settings.default_timezone)}"
            if reminder.recurrence:
                line += f" ({reminder.recurrence})"
            lines.append(line)
        return "\n".join(lines)

    def _private_task_choices(self, password: str) -> list[tuple[str, str]]:
        return [(task.id, task.title) for task in self.storage.list_private_tasks(password, include_completed=True)]

    def _private_reminder_choices(self, password: str) -> list[tuple[str, str]]:
        return [(reminder.id, reminder.text) for reminder in self.storage.list_private_reminders(password, include_inactive=True)]

    def _create_private_task_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        title = str(args.get("title", "")).strip()
        if not title:
            return "I need a private task title."
        body = str(args.get("body", "")).strip()
        due_raw = str(args.get("due_at") or "").strip()
        reminder_raw = str(args.get("reminder_at") or "").strip()
        priority = self._normalize_priority(str(args.get("priority") or "medium")) or "medium"
        tags = [str(tag).strip() for tag in (args.get("tags") or []) if str(tag).strip()]
        resolution_notes = self._consume_resolution_notes(args)
        due_value = None
        if due_raw:
            due_value, prompt, note = self._resolve_datetime_value(due_raw, label="due date", require_time=False)
            if prompt is not None or due_value is None:
                return self._queue_private_datetime_clarification(
                    chat_id=chat_id,
                    entity="task",
                    operation="create",
                    args=args,
                    field="due_at",
                    label="due date",
                    raw_text=due_raw,
                    require_time=False,
                    prompt=prompt,
                )
            if note:
                resolution_notes.append(note)
        reminder_value = None
        if reminder_raw:
            reminder_value, prompt, note = self._resolve_datetime_value(reminder_raw, label="reminder time", require_time=True)
            if prompt is not None or reminder_value is None:
                return self._queue_private_datetime_clarification(
                    chat_id=chat_id,
                    entity="task",
                    operation="create",
                    args=args,
                    field="reminder_at",
                    label="reminder time",
                    raw_text=reminder_raw,
                    require_time=True,
                    prompt=prompt,
                )
            if note:
                resolution_notes.append(note)
        self._remember_path_undo(
            chat_id,
            f"create private task '{title}'",
            [self.storage.private_tasks_enc_path, self.storage.private_reminders_enc_path],
        )
        task = self.storage.create_private_task(
            title=title,
            password=password,
            body=body,
            tags=tags,
            priority=priority,
            due_at=due_value,
            reminder_at=reminder_value,
        )
        parts = [f"Created private task: {task.title}.", f"Priority: {self._format_priority_label(task.priority)}."]
        if task.tags:
            parts.append(f"Tags: {', '.join(task.tags)}.")
        if task.due_at:
            parts.append(f"Due {format_local(task.due_at, self.settings.default_timezone)}.")
        if task.reminder_at:
            parts.append(f"Reminder at {format_local(task.reminder_at, self.settings.default_timezone)}.")
        self._schedule_auto_sync()
        return self._append_notes_to_reply(
            self._with_follow_up_list(" ".join(parts), self._list_private_tasks_secure(password)),
            resolution_notes,
        )

    def _view_private_task_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        if not identifier:
            return "Tell me which private task to view."
        choices = self._private_task_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="task",
            operation="view",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        task = self.storage.find_private_task(str(resolved), password)
        if task is None:
            return "I couldn't find that private task."
        lines = [
            f"Private task: {task.title}",
            f"Status: {task.status}",
            f"Priority: {self._format_priority_label(task.priority)}",
        ]
        if task.tags:
            lines.append(f"Tags: {', '.join(task.tags)}")
        if task.due_at:
            lines.append(f"Due: {format_local(task.due_at, self.settings.default_timezone)}")
        if task.reminder_at:
            lines.append(f"Reminder: {format_local(task.reminder_at, self.settings.default_timezone)}")
        if task.body:
            lines.append(f"\nDescription:\n{task.body}")
        return "\n".join(lines)

    def _update_private_task_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        field = str(args.get("field") or "").strip().lower().replace("reminder", "remind").replace("remind me", "remind")
        value = str(args.get("value") or "").strip()
        resolution_notes = self._consume_resolution_notes(args)
        if not identifier or not field:
            return "I need a private task identifier and field to update."
        choices = self._private_task_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="task",
            operation="update",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        existing = self.storage.find_private_task(str(resolved), password)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"update private task '{existing.title}'",
                [self.storage.private_tasks_enc_path, self.storage.private_reminders_enc_path],
            )
        if field == "title":
            task = self.storage.update_private_task(str(resolved), password=password, title=value)
        elif field == "description":
            task = self.storage.update_private_task(str(resolved), password=password, body=value)
        elif field == "tags":
            task = self.storage.update_private_task(str(resolved), password=password, tags=self._parse_tags(value))
        elif field == "priority":
            priority = self._normalize_priority(value)
            if priority is None:
                return "Use priority low, medium, or high."
            task = self.storage.update_private_task(str(resolved), password=password, priority=priority)
        elif field == "due":
            if self._is_clear_value(value):
                due_at = None
            else:
                due_at, prompt, note = self._resolve_datetime_value(value, label="due date", require_time=False)
                if prompt is not None or due_at is None:
                    return self._queue_private_datetime_clarification(
                        chat_id=chat_id,
                        entity="task",
                        operation="update",
                        args={**args, "identifier": str(resolved)},
                        field="value",
                        label="due date",
                        raw_text=value,
                        require_time=False,
                        allow_clear=True,
                        prompt=prompt,
                    )
                if note:
                    resolution_notes.append(note)
            task = self.storage.update_private_task(str(resolved), password=password, due_at=due_at)
        elif field == "remind":
            if self._is_clear_value(value):
                reminder_at = None
            else:
                reminder_at, prompt, note = self._resolve_datetime_value(value, label="reminder time", require_time=True)
                if prompt is not None or reminder_at is None:
                    return self._queue_private_datetime_clarification(
                        chat_id=chat_id,
                        entity="task",
                        operation="update",
                        args={**args, "identifier": str(resolved)},
                        field="value",
                        label="reminder time",
                        raw_text=value,
                        require_time=True,
                        allow_clear=True,
                        prompt=prompt,
                    )
                if note:
                    resolution_notes.append(note)
            task = self.storage.update_private_task(str(resolved), password=password, reminder_at=reminder_at)
        else:
            return "Unknown field. Use title, description, tags, priority, due, or remind."
        if task is None:
            return "I couldn't find that private task."
        self._schedule_auto_sync()
        return self._append_notes_to_reply(
            self._with_follow_up_list(f"Updated private task: {task.title}.", self._list_private_tasks_secure(password)),
            resolution_notes,
        )

    def _complete_private_task_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        if not identifier:
            return "Tell me which private task to complete."
        choices = self._private_task_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="task",
            operation="complete",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        existing = self.storage.find_private_task(str(resolved), password)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"complete private task '{existing.title}'",
                [self.storage.private_tasks_enc_path, self.storage.private_reminders_enc_path],
            )
        task = self.storage.complete_private_task(str(resolved), password)
        if task is None:
            return "I couldn't find that private task."
        self._schedule_auto_sync()
        return self._with_follow_up_list(
            f"Marked private task '{task.title}' as completed.",
            self._list_private_tasks_secure(password),
        )

    def _delete_private_task_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        if not identifier:
            return "Tell me which private task to delete."
        choices = self._private_task_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="task",
            operation="delete",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        existing = self.storage.find_private_task(str(resolved), password)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"delete private task '{existing.title}'",
                [self.storage.private_tasks_enc_path, self.storage.private_reminders_enc_path],
            )
        task = self.storage.delete_private_task(str(resolved), password)
        if task is None:
            return "I couldn't find that private task."
        self._schedule_auto_sync()
        return self._with_follow_up_list(f"Deleted private task: {task.title}.", self._list_private_tasks_secure(password))

    def _create_private_reminder_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        text = str(args.get("text") or "").strip()
        due_raw = str(args.get("due_at") or "").strip()
        recurrence = args.get("recurrence")
        resolution_notes = self._consume_resolution_notes(args)
        if not text or not due_raw:
            return "I need both private reminder text and a time."
        due_at, prompt, note = self._resolve_datetime_value(due_raw, label="reminder time", require_time=True)
        if prompt is not None or due_at is None:
            return self._queue_private_datetime_clarification(
                chat_id=chat_id,
                entity="reminder",
                operation="create",
                args=args,
                field="due_at",
                label="reminder time",
                raw_text=due_raw,
                require_time=True,
                prompt=prompt,
            )
        if note:
            resolution_notes.append(note)
        self._remember_path_undo(chat_id, f"create private reminder '{text}'", [self.storage.private_reminders_enc_path])
        reminder = self.storage.create_private_reminder(text, password=password, due_at=due_at, recurrence=recurrence)
        reply = f"Private reminder set for {format_local(reminder.due_at, self.settings.default_timezone)}: {reminder.text}."
        if reminder.recurrence:
            reply += f" Repeats {reminder.recurrence}."
        self._schedule_auto_sync()
        return self._append_notes_to_reply(
            self._with_follow_up_list(reply, self._list_private_reminders_secure(password)),
            resolution_notes,
        )

    def _view_private_reminder_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        if not identifier:
            return "Tell me which private reminder to view."
        choices = self._private_reminder_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="reminder",
            operation="view",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        reminder = self.storage.find_private_reminder(str(resolved), password)
        if reminder is None:
            return "I couldn't find that private reminder."
        lines = [
            f"Private reminder: {reminder.text}",
            f"Time: {format_local(reminder.due_at, self.settings.default_timezone)}",
            f"Status: {reminder.status}",
        ]
        if reminder.recurrence:
            lines.append(f"Repeats: {reminder.recurrence}")
        return "\n".join(lines)

    def _update_private_reminder_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        field = str(args.get("field") or "").strip().lower()
        value = str(args.get("value") or "").strip()
        resolution_notes = self._consume_resolution_notes(args)
        if not identifier or not field:
            return "I need a private reminder identifier and field to update."
        choices = self._private_reminder_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="reminder",
            operation="update",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        reminder = self.storage.find_private_reminder(str(resolved), password)
        if reminder is None:
            return "I couldn't find that private reminder."
        self._remember_path_undo(
            chat_id,
            f"update private reminder '{reminder.text}'",
            [self.storage.private_reminders_enc_path, self.storage.private_tasks_enc_path],
        )
        if field == "text":
            updated = self.storage.update_private_reminder(reminder.id, password=password, text=value)
        elif field == "due":
            if self._is_clear_value(value):
                return "Reminder time cannot be cleared. Set it to a new date or time instead."
            due_at, prompt, note = self._resolve_datetime_value(value, label="reminder time", require_time=True)
            if prompt is not None or due_at is None:
                return self._queue_private_datetime_clarification(
                    chat_id=chat_id,
                    entity="reminder",
                    operation="update",
                    args={**args, "identifier": reminder.id},
                    field="value",
                    label="reminder time",
                    raw_text=value,
                    require_time=True,
                    prompt=prompt,
                )
            if note:
                resolution_notes.append(note)
            updated = self.storage.update_private_reminder(reminder.id, password=password, due_at=due_at)
        elif field == "recurrence":
            recurrence = None if self._is_clear_value(value) else value
            updated = self.storage.update_private_reminder(reminder.id, password=password, recurrence=recurrence)
        else:
            return "Unknown field. Use text, due, or recurrence."
        if updated is None:
            return "I couldn't find that private reminder."
        self._schedule_auto_sync()
        return self._append_notes_to_reply(
            self._with_follow_up_list(
                f"Updated private reminder: {updated.text}.",
                self._list_private_reminders_secure(password),
            ),
            resolution_notes,
        )

    def _cancel_private_reminder_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        if not identifier:
            return "Tell me which private reminder to cancel."
        choices = self._private_reminder_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="reminder",
            operation="cancel",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        reminder = self.storage.find_private_reminder(str(resolved), password)
        if reminder is None:
            return "I couldn't find that private reminder."
        self._remember_path_undo(
            chat_id,
            f"cancel private reminder '{reminder.text}'",
            [self.storage.private_reminders_enc_path, self.storage.private_tasks_enc_path],
        )
        updated = self.storage.cancel_private_reminder(reminder.id, password)
        if updated is None:
            return "I couldn't find that private reminder."
        self._schedule_auto_sync()
        return self._with_follow_up_list(
            f"Cancelled private reminder: {updated.text}.",
            self._list_private_reminders_secure(password),
        )

    def _ack_private_reminder_secure(self, args: dict[str, Any], *, chat_id: str, password: str) -> str:
        identifier = str(args.get("identifier") or "").strip()
        if not identifier:
            return "Tell me which private reminder to acknowledge."
        choices = self._private_reminder_choices(password)
        resolved, follow_up = self._resolve_private_identifier(
            chat_id=chat_id,
            entity="reminder",
            operation="ack",
            identifier=identifier,
            args=args,
            choices=choices,
        )
        if follow_up is not None:
            return follow_up
        reminder = self.storage.find_private_reminder(str(resolved), password)
        if reminder is None:
            return "I couldn't find that private reminder."
        self._remember_path_undo(
            chat_id,
            f"acknowledge private reminder '{reminder.text}'",
            [self.storage.private_reminders_enc_path, self.storage.private_tasks_enc_path],
        )
        updated = self.storage.ack_private_reminder(reminder.id, password)
        if updated is None:
            return "I couldn't find that private reminder."
        self._schedule_auto_sync()
        return self._with_follow_up_list(
            f"Acknowledged private reminder: {updated.text}.",
            self._list_private_reminders_secure(password),
        )

    def _private_revealed_reminder_markup(self, reminder_id: str) -> dict | None:
        reminder = self.storage.find_private_reminder(reminder_id)
        if reminder is None or reminder.recurrence:
            return None
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Got it", "callback_data": f"ack:{reminder.id}"},
                    {"text": "😴 Snooze", "callback_data": f"snooze:{reminder.id}"},
                ]
            ]
        }

    async def _send_private_revealed_reminder(self, *, chat_id: str, reminder_id: str, password: str) -> str:
        reminder = self.storage.find_private_reminder(reminder_id, password)
        if reminder is None:
            return "I couldn't find that private reminder."
        await self._send_message(
            chat_id,
            f"🔐 {self._format_due_reminder(reminder)}",
            reply_markup=self._private_revealed_reminder_markup(reminder.id),
            delete_after_seconds=self.SENSITIVE_MESSAGE_TTL_SECONDS,
        )
        return "Private reminder revealed."

    def _execute_private_authenticated_operation(
        self,
        *,
        entity: str,
        operation: str,
        args: dict[str, Any],
        chat_id: str,
        password: str,
    ) -> str:
        if entity == "task":
            if operation == "list":
                return self._list_private_tasks_secure(password, args=args)
            if operation == "create":
                return self._create_private_task_secure(args, chat_id=chat_id, password=password)
            if operation == "view":
                return self._view_private_task_secure(args, chat_id=chat_id, password=password)
            if operation == "update":
                return self._update_private_task_secure(args, chat_id=chat_id, password=password)
            if operation == "complete":
                return self._complete_private_task_secure(args, chat_id=chat_id, password=password)
            if operation == "delete":
                return self._delete_private_task_secure(args, chat_id=chat_id, password=password)
        if entity == "reminder":
            if operation == "list":
                return self._list_private_reminders_secure(password)
            if operation == "create":
                return self._create_private_reminder_secure(args, chat_id=chat_id, password=password)
            if operation == "view":
                return self._view_private_reminder_secure(args, chat_id=chat_id, password=password)
            if operation == "update":
                return self._update_private_reminder_secure(args, chat_id=chat_id, password=password)
            if operation == "cancel":
                return self._cancel_private_reminder_secure(args, chat_id=chat_id, password=password)
            if operation == "ack":
                return self._ack_private_reminder_secure(args, chat_id=chat_id, password=password)
        return "I couldn't handle that private action."

    def _entity_label(self, entity_type: str) -> str:
        return {
            "task": "task",
            "reminder": "reminder",
            "note": "note",
            "plan": "plan",
            "project": "project",
            "preference": "preference",
        }.get(entity_type, "item")

    def _reference_choices(self, chat_id: str, entity_type: str) -> list[tuple[str, str]]:
        context = self.storage.get_reference_context(chat_id)
        if not context or str(context.get("entity_type") or "").strip() != entity_type:
            return []
        ids = [str(item).strip() for item in context.get("ids", []) if str(item).strip()]
        labels = [str(item).strip() for item in context.get("labels", []) if str(item).strip()]
        return list(zip(ids, labels))

    def _entity_choices(self, entity_type: str, *, chat_id: str = "") -> list[tuple[str, str]]:
        reference_choices = self._reference_choices(chat_id, entity_type)
        if entity_type == "task":
            items = [(task.id, task.title) for task in self.storage.list_tasks()]
        elif entity_type == "reminder":
            items = [(reminder.id, reminder.text) for reminder in self.storage.list_reminders(include_inactive=True)]
        elif entity_type == "note":
            items = [(note.id, note.title) for note in self.storage.list_notes()]
        elif entity_type == "plan":
            items = [(plan.id, plan.title) for plan in self.storage.list_plans()]
        elif entity_type == "project":
            items = [(project.id, project.title) for project in self.storage.list_projects()]
        elif entity_type == "preference":
            items = [(pref.id, pref.title) for pref in self.storage.list_preferences()]
        else:
            items = []
        if not reference_choices:
            return items
        seen_ids = {identifier for identifier, _ in reference_choices}
        return reference_choices + [item for item in items if item[0] not in seen_ids]

    def _resolve_identifier_from_choices(
        self,
        identifier: str,
        choices: list[tuple[str, str]],
    ) -> tuple[str | None, str | None, str | None]:
        raw = identifier.strip()
        if not raw:
            return None, None, None
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(choices):
                selected = choices[index]
                return selected[0], selected[1], None
            return None, None, None

        wanted = raw.lower()

        def select(items: list[tuple[str, str]]) -> tuple[str | None, str | None, str | None]:
            if len(items) == 1:
                return items[0][0], items[0][1], None
            if len(items) > 1:
                return None, None, "ambiguous"
            return None, None, None

        exact_id = [item for item in choices if item[0].lower() == wanted]
        resolved = select(exact_id)
        if resolved[0] is not None or resolved[2] is not None:
            return resolved

        exact_label = [item for item in choices if item[1].strip().lower() == wanted]
        resolved = select(exact_label)
        if resolved[0] is not None or resolved[2] is not None:
            return resolved

        prefix_id = [item for item in choices if item[0].lower().startswith(wanted)]
        resolved = select(prefix_id)
        if resolved[0] is not None or resolved[2] is not None:
            return resolved

        startswith_label = [item for item in choices if item[1].strip().lower().startswith(wanted)]
        resolved = select(startswith_label)
        if resolved[0] is not None or resolved[2] is not None:
            return resolved

        substring_label = [item for item in choices if wanted in item[1].lower()]
        resolved = select(substring_label)
        if resolved[0] is not None or resolved[2] is not None:
            return resolved

        labels_by_lower = {label.lower(): (identifier_value, label) for identifier_value, label in choices}
        suggestions = get_close_matches(wanted, list(labels_by_lower), n=min(5, len(labels_by_lower)), cutoff=0.55)
        if suggestions:
            suggestion_choices = [labels_by_lower[label] for label in suggestions]
            return None, None, self._entity_disambiguation_reply(
                entity_type="item",
                identifier=raw,
                choices=suggestion_choices,
                suggestion=True,
            )
        return None, None, None

    def _queue_entity_disambiguation(
        self,
        *,
        chat_id: str,
        entity_type: str,
        identifier: str,
        action: str,
        args: dict[str, Any],
        choices: list[tuple[str, str]],
        suggestion: bool,
    ) -> str:
        trimmed_choices = choices[:8]
        self.storage.set_pending(
            chat_id,
            {
                "type": "entity_disambiguation",
                "entity_type": entity_type,
                "identifier": identifier,
                "action": action,
                "args": dict(args),
                "suggestion": suggestion,
                "choices": [{"id": item_id, "label": label} for item_id, label in trimmed_choices],
            },
        )
        return self._entity_disambiguation_reply(
            entity_type=entity_type,
            identifier=identifier,
            choices=trimmed_choices,
            suggestion=suggestion,
        )

    def _entity_disambiguation_reply(
        self,
        *,
        entity_type: str,
        identifier: str,
        choices: list[tuple[str, str]],
        suggestion: bool,
    ) -> str:
        label = self._entity_label(entity_type)
        if suggestion:
            lines = [f"I couldn't find that {label} exactly. Did you mean one of these?"]
        else:
            lines = [f"I found multiple {label}s matching `{identifier}`. Which one did you mean?"]
        for index, (_, choice_label) in enumerate(choices, 1):
            lines.append(f"{index}. {choice_label}")
        lines.append("Reply with the number or exact title, or /cancel.")
        return "\n".join(lines)

    def _resolve_entity_identifier(
        self,
        *,
        chat_id: str,
        entity_type: str,
        identifier: str,
        action: str,
        args: dict[str, Any] | None = None,
        prefer_numeric_index: bool = False,
    ) -> tuple[str | None, str | None]:
        live_choices = self._entity_choices(entity_type)
        if entity_type == "task":
            live_choices = [(task.id, task.title) for task in self.storage.list_tasks(include_completed=True, kind=None)]
        live_ids = {item_id for item_id, _ in live_choices}

        def resolve_snapshot(item_id: str) -> tuple[str | None, str | None]:
            if item_id in live_ids:
                return item_id, None
            return None, f"That {self._entity_label(entity_type)} is no longer available. List {self._entity_label(entity_type)}s again before choosing another item."

        if (args or {}).get("_resolved_identifier"):
            return resolve_snapshot(identifier)
        if identifier.strip().isdigit():
            reference_choices = self._reference_choices(chat_id, entity_type)
            label = self._entity_label(entity_type)
            context = self.storage.get_reference_context(chat_id) if chat_id else None
            context_entity = str((context or {}).get("entity_type") or "").strip()
            index = int(identifier.strip()) - 1
            if reference_choices:
                if 0 <= index < len(reference_choices):
                    return resolve_snapshot(reference_choices[index][0])
                return (
                    None,
                    f"I only see {len(reference_choices)} {label}(s) in the current list. Reply with a number from that list, or use the exact title.",
                )
            if context_entity and context_entity != entity_type:
                return (
                    None,
                    f"Use the {label} title or id, or list {label}s first and then reply with its number.",
                )
            if entity_type == "reminder":
                choices = [(reminder.id, reminder.text) for reminder in self.storage.list_reminders()]
            else:
                choices = self._entity_choices(entity_type)
            if not prefer_numeric_index:
                exact_id = [item for item in choices if item[0].lower() == identifier.strip().lower()]
                if len(exact_id) == 1:
                    return exact_id[0][0], None
            if 0 <= index < len(choices):
                return choices[index][0], None
            return (
                None,
                f"I only see {len(choices)} {label}(s). Reply with a number from that list, or use the exact title.",
            )
        choices = self._entity_choices(entity_type, chat_id=chat_id)
        resolved, display, follow_up = self._resolve_identifier_from_choices(identifier, choices)
        if resolved is not None:
            return resolve_snapshot(resolved)
        if follow_up == "ambiguous":
            ambiguous_choices = []
            wanted = identifier.strip().lower()
            for item_id, label in choices:
                if wanted in label.lower() or label.lower().startswith(wanted) or item_id.lower().startswith(wanted):
                    ambiguous_choices.append((item_id, label))
            return None, self._queue_entity_disambiguation(
                chat_id=chat_id,
                entity_type=entity_type,
                identifier=identifier,
                action=action,
                args=args or {},
                choices=ambiguous_choices or choices,
                suggestion=False,
            )
        if follow_up is not None:
            suggestion_choices = []
            labels_by_lower = {
                label.lower(): (item_id, label)
                for item_id, label in choices
            }
            for matched in get_close_matches(identifier.strip().lower(), list(labels_by_lower), n=min(5, len(labels_by_lower)), cutoff=0.55):
                suggestion_choices.append(labels_by_lower[matched])
            if suggestion_choices:
                return None, self._queue_entity_disambiguation(
                    chat_id=chat_id,
                    entity_type=entity_type,
                    identifier=identifier,
                    action=action,
                    args=args or {},
                    choices=suggestion_choices,
                    suggestion=True,
                )
            return None, follow_up
        return None, None

    def _resolve_entity_for_ai(self, entity_type: str, identifier: str) -> dict[str, Any]:
        if identifier.strip().isdigit():
            return {
                "ok": False,
                "error": (
                    f"Numeric {self._entity_label(entity_type)} references are only safe right after listing that entity in chat. "
                    f"Use the exact title or id instead."
                ),
            }
        choices = self._entity_choices(entity_type)
        resolved, display, follow_up = self._resolve_identifier_from_choices(identifier, choices)
        if resolved is not None:
            return {"ok": True, "identifier": resolved, "display": display or identifier}

        wanted = identifier.strip().lower()
        candidate_choices = [
            (item_id, label)
            for item_id, label in choices
            if wanted and (wanted in label.lower() or label.lower().startswith(wanted) or item_id.lower().startswith(wanted))
        ]
        if not candidate_choices and wanted:
            labels_by_lower = {label.lower(): (item_id, label) for item_id, label in choices}
            for matched in get_close_matches(wanted, list(labels_by_lower), n=min(5, len(labels_by_lower)), cutoff=0.55):
                candidate_choices.append(labels_by_lower[matched])
        if candidate_choices:
            return {
                "ok": False,
                "error": f"Ambiguous {self._entity_label(entity_type)} identifier.",
                "candidates": [{"id": item_id, "label": label} for item_id, label in candidate_choices[:8]],
            }
        if follow_up == "ambiguous":
            return {"ok": False, "error": f"Ambiguous {self._entity_label(entity_type)} identifier."}
        return {"ok": False, "error": f"I couldn't find that {self._entity_label(entity_type)}."}

    def _create_task(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        title = str(args.get("title", "")).strip()
        if not title:
            return "I need a task title."
        body = str(args.get("body", "")).strip()
        due_raw = str(args.get("due_at") or "").strip()
        reminder_raw = str(args.get("reminder_at") or "").strip()
        priority = self._normalize_priority(str(args.get("priority") or "medium")) or "medium"
        tags = [str(t).strip() for t in (args.get("tags") or []) if str(t).strip()]
        kind = str(args.get("kind") or "self").strip().lower()
        if kind not in {"self", "other"}:
            kind = "self"
        plan = AssistantPlan(
            action="create_task",
            args={
                "title": title,
                "body": body,
                "tags": tags,
                "kind": kind,
                "priority": priority,
                "due_at": due_raw or None,
                "reminder_at": reminder_raw or None,
            },
        )
        resolution_notes = self._consume_resolution_notes(args)
        due_value = None
        if due_raw:
            due_value, prompt, note = self._resolve_datetime_value(due_raw, label="due date", require_time=False)
            if prompt is not None or due_value is None:
                return self._queue_datetime_clarification(
                    chat_id=chat_id,
                    plan=plan,
                    field="due_at",
                    label="due date",
                    raw_text=due_raw,
                    require_time=False,
                    prompt=prompt,
                )
            if note:
                resolution_notes.append(note)
        reminder_value = None
        if reminder_raw:
            reminder_value, prompt, note = self._resolve_datetime_value(reminder_raw, label="reminder time", require_time=True)
            if prompt is not None or reminder_value is None:
                return self._queue_datetime_clarification(
                    chat_id=chat_id,
                    plan=plan,
                    field="reminder_at",
                    label="reminder time",
                    raw_text=reminder_raw,
                    require_time=True,
                    prompt=prompt,
                )
            if note:
                resolution_notes.append(note)
        task_id = self.storage._task_id_from_title(title)
        task_label = "others' task" if kind == "other" else "task"
        self._remember_path_undo(
            chat_id,
            f"create {task_label} '{title}'",
            [
                self.storage.tasks_dir / f"{task_id}.md",
                self.storage.tasks_index_path,
                self.storage.reminders_path,
            ],
        )
        task = self.storage.create_task(
            title=title,
            body=body,
            tags=tags,
            kind=kind,
            priority=priority,
            due_at=due_value,
            reminder_at=reminder_value,
        )
        self._remember_reference_context(chat_id, "task", [(task.id, task.title)], mode="single")
        parts = [f"Created {task_label}: {task.title}."]
        parts.append(f"Priority: {self._format_priority_label(task.priority)}.")
        if task.tags:
            parts.append(f"Tags: {', '.join(task.tags)}.")
        if task.due_at:
            parts.append(f"Due {format_local(task.due_at, self.settings.default_timezone)}.")
        if task.reminder_at:
            parts.append(f"Reminder at {format_local(task.reminder_at, self.settings.default_timezone)}.")
        self._schedule_auto_sync()
        follow_up = self._list_tasks({"kind": kind}, chat_id=chat_id)
        return self._append_notes_to_reply(
            self._with_follow_up_list(" ".join(parts), follow_up),
            resolution_notes,
        )

    def _resolve_task_identifier(self, identifier: str) -> str:
        """Convert a 1-based numeric index to the task's slug ID, or return the identifier as-is."""
        if identifier.isdigit():
            tasks = self.storage.list_tasks()
            idx = int(identifier) - 1
            if 0 <= idx < len(tasks):
                return tasks[idx].id
        return identifier

    def _resolve_reminder_identifier(self, identifier: str) -> str:
        """Convert a 1-based numeric index to the reminder's slug ID, or return as-is."""
        if identifier.isdigit():
            reminders = self.storage.list_reminders()
            idx = int(identifier) - 1
            if 0 <= idx < len(reminders):
                return reminders[idx].id
        return identifier

    def _resolve_note_identifier(self, identifier: str) -> str:
        if identifier.isdigit():
            notes = self.storage.list_notes()
            idx = int(identifier) - 1
            if 0 <= idx < len(notes):
                return notes[idx].id
        return identifier

    def _resolve_plan_identifier(self, identifier: str) -> str:
        if identifier.isdigit():
            plans = self.storage.list_plans()
            idx = int(identifier) - 1
            if 0 <= idx < len(plans):
                return plans[idx].id
        return identifier

    def _resolve_project_identifier(self, identifier: str) -> str:
        if identifier.isdigit():
            projects = self.storage.list_projects()
            idx = int(identifier) - 1
            if 0 <= idx < len(projects):
                return projects[idx].id
        return identifier

    def _resolve_preference_identifier(self, identifier: str) -> str:
        if identifier.isdigit():
            prefs = self.storage.list_preferences()
            idx = int(identifier) - 1
            if 0 <= idx < len(prefs):
                return prefs[idx].id
        return identifier

    def _view_task(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which task to view."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="task",
            identifier=identifier,
            action="view_task",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_task_identifier(identifier)
        task = self.storage.find_task(identifier)
        if task is None:
            return "I couldn't find that task."
        self._remember_reference_context(chat_id, "task", [(task.id, task.title)], mode="single")
        # Find its numeric position.
        tasks = self.storage.list_tasks()
        pos = next((i + 1 for i, t in enumerate(tasks) if t.id == task.id), None)
        pos_str = f"#{pos}" if pos else task.id
        lines = [
            f"Task {pos_str}: {task.title}",
            f"Status: {task.status}",
            f"Priority: {self._format_priority_label(task.priority)}",
        ]
        if task.is_current:
            lines.append("Current: yes")
        if task.tags:
            lines.append(f"Tags: {', '.join(task.tags)}")
        if task.due_at:
            lines.append(f"Due: {format_local(task.due_at, self.settings.default_timezone)}")
        if task.reminder_at:
            lines.append(f"Reminder: {format_local(task.reminder_at, self.settings.default_timezone)}")
        if task.body:
            lines.append(f"\nDescription:\n{task.body}")
        return "\n".join(lines)

    def _update_task(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        field = str(args.get("field", "")).strip().lower().replace("reminder", "remind").replace("remind me", "remind")
        value = str(args.get("value", "")).strip()
        resolution_notes = self._consume_resolution_notes(args)
        if not identifier or not field:
            return "I need a task identifier and field to update."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="task",
            identifier=identifier,
            action="update_task",
            args={**args, "field": field, "value": value},
            prefer_numeric_index=True,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_task_identifier(identifier)
        existing = self.storage.find_task(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"update task '{existing.title}'",
                [
                    self.storage.tasks_dir / f"{existing.id}.md",
                    self.storage.tasks_index_path,
                    self.storage.reminders_path,
                ],
            )
        if field == "title":
            task = self.storage.update_task(identifier, title=value)
        elif field == "description":
            task = self.storage.update_task(identifier, body=value)
        elif field == "tags":
            tags = self._parse_tags(value)
            task = self.storage.update_task(identifier, tags=tags)
        elif field == "priority":
            priority = self._normalize_priority(value)
            if priority is None:
                return "Use priority low, medium, or high."
            task = self.storage.update_task(identifier, priority=priority)
        elif field == "due":
            if self._is_clear_value(value):
                due_at = None
            else:
                due_at, prompt, note = self._resolve_datetime_value(value, label="due date", require_time=False)
                if prompt is not None or due_at is None:
                    return self._queue_datetime_clarification(
                        chat_id=chat_id,
                        plan=AssistantPlan(
                            action="update_task",
                            args={"identifier": identifier, "field": "due", "value": value},
                        ),
                        field="value",
                        label="due date",
                        raw_text=value,
                        require_time=False,
                        allow_clear=True,
                        prompt=prompt,
                    )
                if note:
                    resolution_notes.append(note)
            task = self.storage.update_task(identifier, due_at=due_at)
        elif field == "remind":
            if self._is_clear_value(value):
                reminder_at = None
            else:
                reminder_at, prompt, note = self._resolve_datetime_value(value, label="reminder time", require_time=True)
                if prompt is not None or reminder_at is None:
                    return self._queue_datetime_clarification(
                        chat_id=chat_id,
                        plan=AssistantPlan(
                            action="update_task",
                            args={"identifier": identifier, "field": "remind", "value": value},
                        ),
                        field="value",
                        label="reminder time",
                        raw_text=value,
                        require_time=True,
                        allow_clear=True,
                        prompt=prompt,
                    )
                if note:
                    resolution_notes.append(note)
            task = self.storage.update_task(identifier, reminder_at=reminder_at)
        else:
            return f"Unknown field '{field}'. Use title, description, tags, priority, due, or remind."
        if task is None:
            return "I couldn't find that task."
        self._remember_reference_context(chat_id, "task", [(task.id, task.title)], mode="single")
        self._schedule_auto_sync()
        return self._append_notes_to_reply(
            self._with_follow_up_list(f"Updated task: {task.title}.", self._list_tasks(chat_id=chat_id)),
            resolution_notes,
        )

    def _retag_tasks(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        old_tag = str(args.get("old_tag") or "").strip().lstrip("#")
        new_tag = str(args.get("new_tag") or "").strip().lstrip("#")
        if not old_tag or not new_tag:
            return "Tell me the old tag and the new tag."
        tasks = self.storage.list_tasks(include_completed=True, tag=old_tag)
        if not tasks:
            return f"I couldn't find any tasks tagged #{old_tag}."
        paths = [self.storage.tasks_index_path, *[self.storage.tasks_dir / f"{task.id}.md" for task in tasks]]
        self._remember_path_undo(chat_id, f"re-tag tasks #{old_tag} to #{new_tag}", paths)
        updated = self.storage.retag_tasks(old_tag, new_tag, include_completed=True)
        if not updated:
            return f"I couldn't find any tasks tagged #{old_tag}."
        self._schedule_auto_sync()
        self._remember_reference_context(chat_id, "task", [(task.id, task.title) for task in updated])
        return self._with_follow_up_list(
            f"Updated {len(updated)} task(s): replaced #{old_tag} with #{new_tag}.",
            self._list_tasks({"sort_by": "tag", "tag": new_tag}, chat_id=chat_id),
        )

    def _merge_tasks(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        raw_sources = args.get("source_identifiers")
        if not isinstance(raw_sources, list) or len(raw_sources) < 2:
            return "Tell me at least two tasks to merge."
        resolved_tasks: list[TaskRecord] = []
        seen_ids: set[str] = set()
        for item in raw_sources:
            identifier = re.sub(r"^(?:tasks?|task)\s+", "", str(item or "").strip(), flags=re.IGNORECASE)
            if not identifier:
                continue
            resolved, follow_up = self._resolve_entity_identifier(
                chat_id=chat_id,
                entity_type="task",
                identifier=identifier,
                action="merge_tasks",
                args=args,
            )
            if follow_up is not None:
                return follow_up
            task = self.storage.find_task(resolved or self._resolve_task_identifier(identifier))
            if task is None:
                return f"I couldn't find task '{identifier}'."
            if task.id in seen_ids:
                continue
            seen_ids.add(task.id)
            resolved_tasks.append(task)
        if len(resolved_tasks) < 2:
            return "I need at least two distinct tasks to merge."

        priority_rank = {"high": 0, "medium": 1, "low": 2}
        merged_priority = min(
            (task.priority for task in resolved_tasks),
            key=lambda item: priority_rank.get(item, 1),
        )
        merged_title = str(args.get("title") or "").strip()
        if not merged_title:
            merged_title = " and ".join(task.title for task in resolved_tasks[:2])
            if len(resolved_tasks) > 2:
                merged_title += " and related tasks"
        merged_tags = list(dict.fromkeys(tag for task in resolved_tasks for tag in task.tags))
        due_at = min((task.due_at for task in resolved_tasks if task.due_at is not None), default=None)
        reminder_at = min((task.reminder_at for task in resolved_tasks if task.reminder_at is not None), default=None)
        body_lines = [f"- {task.title}" for task in resolved_tasks]
        detailed_bodies = [task for task in resolved_tasks if task.body.strip()]
        if detailed_bodies:
            body_lines.append("")
            for task in detailed_bodies:
                body_lines.append(f"{task.title}: {task.body.strip()}")
        merged_body = "\n".join(body_lines).strip()

        new_task_id = self.storage._task_id_from_title(merged_title)
        undo_paths: list[Path] = [self.storage.tasks_index_path, self.storage.reminders_path]
        undo_paths.extend(self.storage.tasks_dir / f"{task.id}.md" for task in resolved_tasks)
        undo_paths.append(self.storage.tasks_dir / f"{new_task_id}.md")
        self._remember_path_undo(chat_id, f"merge tasks into '{merged_title}'", undo_paths)

        merged_task = self.storage.create_task(
            title=merged_title,
            body=merged_body,
            tags=merged_tags,
            priority=merged_priority,
            due_at=due_at,
            reminder_at=reminder_at,
        )
        for task in resolved_tasks:
            self.storage.complete_task(task.id)
        self._schedule_auto_sync()
        self._remember_reference_context(chat_id, "task", [(merged_task.id, merged_task.title)], mode="single")
        source_titles = ", ".join(task.title for task in resolved_tasks)
        return self._with_follow_up_list(
            f"Merged {len(resolved_tasks)} tasks into: {merged_task.title}. Sources: {source_titles}. The original tasks were marked completed.",
            self._list_tasks(chat_id=chat_id),
        )

    def _list_tasks(
        self,
        args: dict[str, Any] | None = None,
        *,
        chat_id: str = "",
        remember_context: bool = True,
    ) -> str:
        args = args or {}
        sort_by = str(args.get("sort_by", "default")).strip().lower() or "default"
        tag = str(args.get("tag", "")).strip() or None
        kind = str(args.get("kind") or "self").strip().lower()
        if kind not in {"self", "other"}:
            kind = "self"
        tasks = self.storage.list_tasks(sort_by=sort_by, tag=tag, kind=kind)
        if not tasks:
            if remember_context:
                self._clear_reference_context(chat_id)
            if tag:
                if kind == "other":
                    return f"You have no others' tasks tagged #{tag.lstrip('#')}."
                return f"You have no open tasks tagged #{tag.lstrip('#')}."
            if kind == "other":
                return "You have no others' tasks."
            return "You have no open tasks."
        if remember_context:
            self._remember_reference_context(chat_id, "task", [(task.id, task.title) for task in tasks])
        title = "Others' tasks:" if kind == "other" else "Open tasks:"
        if tag:
            title = f"Others' tasks tagged #{tag.lstrip('#')}:" if kind == "other" else f"Open tasks tagged #{tag.lstrip('#')}:"
        elif sort_by == "due":
            title = "Others' tasks by due date:" if kind == "other" else "Open tasks by due date:"
        elif sort_by == "remind":
            title = "Others' tasks by reminder:" if kind == "other" else "Open tasks by reminder:"
        elif sort_by == "tag":
            title = "Others' tasks by tag:" if kind == "other" else "Open tasks by tag:"
        elif sort_by == "priority":
            title = "Others' tasks by priority:" if kind == "other" else "Open tasks by priority:"
        lines = [title]
        for i, task in enumerate(tasks, 1):
            current_marker = "⭐ Current - " if task.is_current else ""
            line = f"{i}. {current_marker}{self._format_priority_label(task.priority)} - {task.title}"
            if task.tags:
                line += f" [{', '.join(task.tags)}]"
            if task.due_at:
                line += f" (due {format_local(task.due_at, self.settings.default_timezone)})"
            if task.reminder_at:
                line += f" (remind {format_local(task.reminder_at, self.settings.default_timezone)})"
            lines.append(line)
        return "\n".join(lines)

    def _complete_task(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which task to complete."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="task",
            identifier=identifier,
            action="complete_task",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_task_identifier(identifier)
        existing = self.storage.find_task(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"complete task '{existing.title}'",
                [
                    self.storage.tasks_dir / f"{existing.id}.md",
                    self.storage.tasks_index_path,
                    self.storage.reminders_path,
                ],
            )
        task = self.storage.complete_task(identifier)
        if task is None:
            return "I couldn't find that task."
        self._remember_reference_context(chat_id, "task", [(task.id, task.title)], mode="single")
        self._schedule_auto_sync()
        return self._with_follow_up_list(f"Marked '{task.title}' as completed.", self._list_tasks(chat_id=chat_id))

    def _make_task_current(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which task to make current."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="task",
            identifier=identifier,
            action="make_task_current",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_task_identifier(identifier)
        existing = self.storage.find_task(identifier)
        if existing is not None:
            task_paths = [self.storage.tasks_index_path]
            task_paths.extend(self.storage.tasks_dir / f"{task.id}.md" for task in self.storage.list_tasks(include_completed=True))
            self._remember_path_undo(chat_id, f"make task '{existing.title}' current", task_paths)
        task = self.storage.set_current_task(identifier)
        if task is None:
            return "I couldn't find that open task."
        self._remember_reference_context(chat_id, "task", [(task.id, task.title)], mode="single")
        self._schedule_auto_sync()
        return self._with_follow_up_list(f"Current task: {task.title}.", self._list_tasks(chat_id=chat_id))

    def _delete_task(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which task to delete."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="task",
            identifier=identifier,
            action="delete_task",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_task_identifier(identifier)
        existing = self.storage.find_task(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"delete task '{existing.title}'",
                [
                    self.storage.tasks_dir / f"{existing.id}.md",
                    self.storage.tasks_index_path,
                    self.storage.reminders_path,
                ],
            )
        task = self.storage.delete_task(identifier)
        if task is None:
            return "I couldn't find that task."
        self._schedule_auto_sync()
        follow_up = self._list_tasks(chat_id=chat_id)
        return f"Deleted task: {task.title}.\n\n{follow_up}"

    def _create_reminder(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        text = str(args.get("text", "")).strip()
        due_raw = str(args.get("due_at", "")).strip()
        recurrence = args.get("recurrence")
        resolution_notes = self._consume_resolution_notes(args)
        if not text or not due_raw:
            return "I need both reminder text and a time."
        due_at, prompt, note = self._resolve_datetime_value(due_raw, label="reminder time", require_time=True)
        if prompt is not None or due_at is None:
            return self._queue_datetime_clarification(
                chat_id=chat_id,
                plan=AssistantPlan(
                    action="create_reminder",
                    args={"text": text, "due_at": due_raw, "recurrence": recurrence},
                ),
                field="due_at",
                label="reminder time",
                raw_text=due_raw,
                require_time=True,
                prompt=prompt,
            )
        if note:
            resolution_notes.append(note)
        self._remember_path_undo(chat_id, f"create reminder '{text}'", [self.storage.reminders_path])
        reminder = self.storage.create_reminder(text, due_at=due_at, recurrence=recurrence)
        self._remember_reference_context(chat_id, "reminder", [(reminder.id, reminder.text)], mode="single")
        reply = f"Reminder set for {format_local(reminder.due_at, self.settings.default_timezone)}: {reminder.text}."
        if reminder.recurrence:
            reply += f" Repeats {reminder.recurrence}."
        self._schedule_auto_sync()
        tasks_view = self._list_tasks(chat_id=chat_id, remember_context=False)
        reminders_view = self._list_reminders(chat_id=chat_id, remember_context=False)
        self._remember_reference_context(chat_id, "reminder", [(reminder.id, reminder.text)], mode="single")
        return self._append_notes_to_reply(f"{reply}\n\n{tasks_view}\n\n{reminders_view}", resolution_notes)

    def _list_reminders(self, *, chat_id: str = "", remember_context: bool = True) -> str:
        reminders = self.storage.list_reminders()
        if not reminders:
            if remember_context:
                self._clear_reference_context(chat_id)
            return "You have no active reminders."
        if remember_context:
            self._remember_reference_context(chat_id, "reminder", [(reminder.id, reminder.text) for reminder in reminders])
        lines = ["Active reminders:"]
        for i, reminder in enumerate(reminders, 1):
            line = f"{i}. {reminder.text} — {format_local(reminder.due_at, self.settings.default_timezone)}"
            if reminder.recurrence:
                line += f" ({reminder.recurrence})"
            lines.append(line)
        return "\n".join(lines)

    def _update_reminder(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        field = str(args.get("field", "")).strip().lower()
        value = str(args.get("value", "")).strip()
        resolution_notes = self._consume_resolution_notes(args)
        if not identifier or not field:
            return "I need a reminder identifier and field to update."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="reminder",
            identifier=identifier,
            action="update_reminder",
            args={**args, "field": field, "value": value},
        )
        if follow_up is not None:
            return follow_up
        resolved = resolved or self._resolve_reminder_identifier(identifier)
        reminder = self.storage.find_reminder(resolved)
        if reminder is None:
            return "I couldn't find that reminder."
        undo_paths = [self.storage.reminders_path]
        if reminder.source_type == "task" and reminder.source_id:
            undo_paths.extend([self.storage.tasks_dir / f"{reminder.source_id}.md", self.storage.tasks_index_path])
        self._remember_path_undo(chat_id, f"update reminder '{reminder.text}'", undo_paths)
        if field == "text":
            updated = self.storage.update_reminder(reminder.id, text=value)
        elif field == "due":
            if self._is_clear_value(value):
                return "Reminder time cannot be cleared. Set it to a new date or time instead."
            due_at, prompt, note = self._resolve_datetime_value(value, label="reminder time", require_time=True)
            if prompt is not None or due_at is None:
                return self._queue_datetime_clarification(
                    chat_id=chat_id,
                    plan=AssistantPlan(
                        action="update_reminder",
                        args={"identifier": reminder.id, "field": "due", "value": value},
                    ),
                    field="value",
                    label="reminder time",
                    raw_text=value,
                    require_time=True,
                    prompt=prompt,
                )
            if note:
                resolution_notes.append(note)
            updated = self.storage.update_reminder(reminder.id, due_at=due_at)
        elif field == "recurrence":
            recurrence = None if self._is_clear_value(value) else value
            updated = self.storage.update_reminder(reminder.id, recurrence=recurrence)
        else:
            return "Unknown field. Use text, due, or recurrence."
        if updated is None:
            return "I couldn't find that reminder."
        self._remember_reference_context(chat_id, "reminder", [(updated.id, updated.text)], mode="single")
        self._schedule_auto_sync()
        return self._append_notes_to_reply(
            self._with_follow_up_list(
                f"Updated reminder: {updated.text}.",
                self._list_reminders(chat_id=chat_id),
            ),
            resolution_notes,
        )

    def _cancel_reminder(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which reminder to cancel."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="reminder",
            identifier=identifier,
            action="cancel_reminder",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        resolved = resolved or self._resolve_reminder_identifier(identifier)
        reminder = self.storage.find_reminder(resolved)
        if reminder is None:
            return "I couldn't find that reminder."
        self._remember_path_undo(chat_id, f"cancel reminder '{reminder.text}'", [self.storage.reminders_path])
        reminder = self.storage.cancel_reminder(reminder.id)
        if reminder is None:
            return "I couldn't find that reminder."
        self._schedule_auto_sync()
        follow_up = self._list_reminders(chat_id=chat_id)
        return f"Cancelled reminder: {reminder.text}.\n\n{follow_up}"

    def _cancel_all_reminders(self, *, chat_id: str = "") -> str:
        reminders = self.storage.list_reminders()
        if reminders:
            self._remember_path_undo(chat_id, "cancel all reminders", [self.storage.reminders_path])
        count = self.storage.cancel_all_reminders()
        if count == 0:
            return "You have no active reminders to cancel."
        self._schedule_auto_sync()
        self._clear_reference_context(chat_id)
        return f"Cancelled {count} active reminder(s)."

    def _ack_reminder(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which reminder to acknowledge."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="reminder",
            identifier=identifier,
            action="ack_reminder",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        resolved = resolved or self._resolve_reminder_identifier(identifier)
        # Also search in "sent" reminders (not in list_reminders which only shows "scheduled").
        reminder = None
        for r in self.storage.list_reminders(include_inactive=True):
            if r.id == resolved or r.id.startswith(resolved) or resolved in r.text.lower():
                reminder = r
                break
        if reminder is None:
            return "I couldn't find that reminder."
        undo_paths = [self.storage.reminders_path]
        if reminder.source_type == "task" and reminder.source_id:
            undo_paths.extend(
                [
                    self.storage.tasks_dir / f"{reminder.source_id}.md",
                    self.storage.tasks_index_path,
                ]
            )
        self._remember_path_undo(chat_id, f"acknowledge reminder '{reminder.text}'", undo_paths)
        self.storage.ack_reminder(reminder.id)
        self._remember_reference_context(chat_id, "reminder", [(reminder.id, reminder.text)], mode="single")
        self._schedule_auto_sync()
        return self._with_follow_up_list(
            f"Acknowledged reminder: {reminder.text}.",
            self._list_reminders(chat_id=chat_id),
        )

    def _parse_snooze_due_at(self, text: str) -> Any | None:
        cleaned = text.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            return now_utc() + timedelta(minutes=max(int(cleaned), 1))
        if re.search(r"\b\d+\s*(minutes?|mins?|min|m|hours?|hrs?|hr|h)\b", cleaned, re.IGNORECASE):
            return now_utc() + timedelta(minutes=parse_duration_minutes(cleaned, default_minutes=10))
        due_at, prompt, _note = self._resolve_datetime_value(cleaned, label="snooze time", require_time=True)
        if prompt is not None:
            return None
        if due_at is None or due_at <= now_utc():
            return None
        return due_at

    def _snooze_reminder(self, reminder_id: str, *, due_at: Any, chat_id: str = "") -> str:
        reminder = next(
            (item for item in self.storage.list_reminders(include_inactive=True) if item.id == reminder_id),
            None,
        )
        if reminder is None:
            return "I couldn't find that reminder."
        undo_paths = [self.storage.reminders_path]
        if reminder.source_type == "task" and reminder.source_id:
            undo_paths.extend([self.storage.tasks_dir / f"{reminder.source_id}.md", self.storage.tasks_index_path])
        self._remember_path_undo(chat_id, f"snooze reminder '{reminder.text}'", undo_paths)
        updated = self.storage.snooze_reminder(reminder_id, due_at=due_at)
        if updated is None:
            return "I couldn't find that reminder."
        self._remember_reference_context(chat_id, "reminder", [(updated.id, updated.text)], mode="single")
        self._schedule_auto_sync()
        return self._with_follow_up_list(
            f"Snoozed reminder: {updated.text}.\n"
            f"New time: {format_local(updated.due_at, self.settings.default_timezone)}.",
            self._list_reminders(chat_id=chat_id),
        )

    def _request_bulk_delete(self, chat_id: str, entity: str) -> str:
        normalized = entity.strip().lower()
        labels = {
            "tasks": "all tasks",
            "notes": "all notes",
            "plans": "all plans",
            "projects": "all projects",
            "preferences": "all preferences",
            "reminders": "all reminders",
        }
        if normalized not in labels:
            return "I don't know how to bulk-delete that item type."
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "bulk_delete_confirm", "entity": normalized})
        return f"Delete {labels[normalized]}? Reply yes to confirm or no to cancel."

    def _perform_bulk_delete(self, entity: str, *, chat_id: str = "") -> str:
        normalized = entity.strip().lower()
        if normalized == "tasks":
            tasks = self.storage.list_tasks(include_completed=True)
            if not tasks:
                return "You have no tasks to delete."
            paths = [
                self.storage.tasks_index_path,
                self.storage.reminders_path,
                *[self.storage.tasks_dir / f"{task.id}.md" for task in tasks],
            ]
            self._remember_path_undo(chat_id, "delete all tasks", paths)
            count = self.storage.delete_all_tasks()
            self._schedule_auto_sync()
            self._clear_reference_context(chat_id)
            return f"Deleted {count} task(s)."
        if normalized == "notes":
            notes = self.storage.list_notes()
            if not notes:
                return "You have no notes to delete."
            paths = [self.storage.notes_index_path, *[self.storage.notes_dir / f"{note.id}.md" for note in notes]]
            self._remember_path_undo(chat_id, "delete all notes", paths)
            count = self.storage.delete_all_notes()
            self._schedule_auto_sync()
            self._clear_reference_context(chat_id)
            return f"Deleted {count} note(s)."
        if normalized == "plans":
            plans = self.storage.list_plans()
            if not plans:
                return "You have no plans to delete."
            paths = [self.storage.plans_index_path, *[self.storage.plans_dir / f"{plan.id}.md" for plan in plans]]
            self._remember_path_undo(chat_id, "delete all plans", paths)
            count = self.storage.delete_all_plans()
            self._schedule_auto_sync()
            self._clear_reference_context(chat_id)
            return f"Deleted {count} plan(s)."
        if normalized == "projects":
            projects = self.storage.list_projects()
            if not projects:
                return "You have no projects to delete."
            paths = [self.storage.projects_index_path, *[self.storage.projects_dir / f"{project.id}.md" for project in projects]]
            self._remember_path_undo(chat_id, "delete all projects", paths)
            count = self.storage.delete_all_projects()
            self._schedule_auto_sync()
            self._clear_reference_context(chat_id)
            return f"Deleted {count} project(s)."
        if normalized == "preferences":
            prefs = self.storage.list_preferences()
            if not prefs:
                return "You have no preferences to delete."
            paths = [self.storage.preferences_index_path, *[self.storage.preferences_dir / f"{pref.id}.md" for pref in prefs]]
            self._remember_path_undo(chat_id, "delete all preferences", paths)
            count = self.storage.delete_all_preferences()
            self._schedule_auto_sync()
            self._clear_reference_context(chat_id)
            return f"Deleted {count} preference(s)."
        if normalized == "reminders":
            reminders = self.storage.list_reminders(include_inactive=True)
            if not reminders:
                return "You have no reminders to delete."
            self._remember_path_undo(chat_id, "delete all reminders", [self.storage.reminders_path])
            count = self.storage.cancel_all_reminders()
            self._schedule_auto_sync()
            self._clear_reference_context(chat_id)
            return f"Cancelled {count} reminder(s)."
        return "I don't know how to bulk-delete that item type."

    async def _upcoming_events(self, *, chat_id: str = "") -> str:
        events = await self.calendar.list_upcoming_events(limit=5)
        if not events:
            self._clear_reference_context(chat_id)
            return "Your calendar looks clear."
        self._remember_reference_context(chat_id, "calendar_event", [(event.id, event.title) for event in events[:5]])
        lines = ["Upcoming events:"]
        for event in events:
            line = f"- {event.title}: {format_local(event.start, self.settings.default_timezone)}"
            if event.owned_by_assistant:
                line += " [assistant]"
            lines.append(line)
        return "\n".join(lines)

    async def _find_free_slot(self, args: dict[str, Any]) -> str:
        duration_minutes = int(args.get("duration_minutes", 30))
        day_hint = self._free_slot_day_hint(str(args.get("day") or ""))
        if day_hint and self._looks_like_date_range(day_hint):
            range_result = interpret_user_datetime_range(day_hint, self.settings.default_timezone)
            if range_result.status == "ambiguous":
                return range_result.prompt or (
                    "I need a clearer date range. Try `tomorrow 2pm to 5pm` or `between next Tuesday and next Friday`."
                )
            if range_result.status == "ok" and range_result.start is not None and range_result.end is not None:
                start, end = range_result.start, range_result.end
            else:
                return "I couldn't understand that free-slot window."
        else:
            anchor = parse_user_datetime(
                day_hint,
                self.settings.default_timezone,
            ) or now_utc()
            start, end = local_day_window(
                anchor,
                self.settings.default_timezone,
                start_hour=self.settings.free_slot_start_hour,
                end_hour=self.settings.free_slot_end_hour,
            )
        slots = await self.calendar.find_free_slots(
            window_start=start,
            window_end=end,
            duration_minutes=duration_minutes,
        )
        if self._looks_like_date_range(day_hint):
            window_label = f"{format_local(start, self.settings.default_timezone)} to {format_local(end, self.settings.default_timezone)}"
        else:
            window_label = format_local(start, self.settings.default_timezone)
        if not slots:
            return f"I couldn't find a free {duration_minutes}-minute slot in {window_label}."
        best = slots[0]
        return (
            f"First free {duration_minutes}-minute slot: "
            f"{format_local(best.start, self.settings.default_timezone)}"
            f" to {format_local(best.end, self.settings.default_timezone)}."
        )

    @staticmethod
    def _free_slot_day_hint(text: str) -> str:
        hint = re.sub(r"\b\d+\s*(?:minutes?|mins?|hours?|hrs?|h)\b", " ", text, flags=re.IGNORECASE)
        hint = re.sub(r"\b(find(?: me)?|a|an|free slot|availability|slot)\b", " ", hint, flags=re.IGNORECASE)
        hint = re.sub(r"\s+", " ", hint).strip()
        return hint

    async def _create_calendar_event(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        title = str(args.get("title", "")).strip()
        start_raw = str(args.get("start", "")).strip()
        duration_minutes = int(args.get("duration_minutes", 60))
        resolution_notes = self._consume_resolution_notes(args)
        if not title or not start_raw:
            return "I need an event title and start time."
        start, prompt, note = self._resolve_datetime_value(start_raw, label="event time", require_time=True)
        if prompt is not None or start is None:
            return self._queue_datetime_clarification(
                chat_id=chat_id,
                plan=AssistantPlan(
                    action="create_calendar_event",
                    args={"title": title, "start": start_raw, "duration_minutes": duration_minutes},
                ),
                field="start",
                label="event time",
                raw_text=start_raw,
                require_time=True,
                prompt=prompt,
            )
        if note:
            resolution_notes.append(note)
        end = start + timedelta(minutes=duration_minutes)
        event = await self.calendar.create_event(title=title, start=start, end=end)
        self._remember_reference_context(chat_id, "calendar_event", [(event.id, event.title)], mode="single")
        source_id = str(args.get("source_id") or event.id)
        link_restore = self.storage.build_path_undo_action(
            f"create event '{event.title}'",
            [self.storage.calendar_links_path],
        )
        self.storage.remember_calendar_event(str(args.get("source_id") or event.id), event.id)
        self._remember_undo_action(
            chat_id,
            {
                "kind": "calendar_create",
                "label": f"create event '{event.title}'",
                "event_id": event.id,
                "source_id": source_id,
                "link_restore": link_restore,
            },
        )
        return self._append_notes_to_reply((
            f"Created event {event.title} for "
            f"{format_local(event.start, self.settings.default_timezone)}."
        ), resolution_notes)

    async def _update_calendar_event(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        start_raw = str(args.get("start", "")).strip()
        duration_minutes = int(args.get("duration_minutes", 60))
        resolution_notes = self._consume_resolution_notes(args)
        if not identifier or not start_raw:
            return "I need the event and its new time."
        start, prompt, note = self._resolve_datetime_value(start_raw, label="event time", require_time=True)
        if prompt is not None or start is None:
            return self._queue_datetime_clarification(
                chat_id=chat_id,
                plan=AssistantPlan(
                    action="update_calendar_event",
                    args={"identifier": identifier, "start": start_raw, "duration_minutes": duration_minutes},
                ),
                field="start",
                label="event time",
                raw_text=start_raw,
                require_time=True,
                prompt=prompt,
            )
        if note:
            resolution_notes.append(note)
        previous = await self.calendar.find_owned_event(identifier)
        if previous is None:
            return "I can only update assistant-created events, and I couldn't find a matching one."
        previous = previous.model_copy(deep=True)
        event = await self.calendar.update_owned_event(
            identifier,
            start=start,
            end=start + timedelta(minutes=duration_minutes),
        )
        if event is None:
            return "I can only update assistant-created events, and I couldn't find a matching one."
        self._remember_reference_context(chat_id, "calendar_event", [(event.id, event.title)], mode="single")
        self._remember_undo_action(
            chat_id,
            {
                "kind": "calendar_update",
                "label": f"update event '{event.title}'",
                "event_id": previous.id,
                "title": previous.title,
                "start": previous.start.isoformat(),
                "end": previous.end.isoformat(),
                "description": previous.description,
            },
        )
        return self._append_notes_to_reply((
            f"Updated {event.title} to "
            f"{format_local(event.start, self.settings.default_timezone)}."
        ), resolution_notes)

    def _request_people_view(self, chat_id: str) -> str:
        if not self.settings.people_encryption_key:
            return "People encryption is not configured (PEOPLE_ENCRYPTION_KEY not set)."
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "people_view"})
        return "Enter the people password to view entries:"

    def _request_force_git_sync(self, chat_id: str) -> str:
        if not self.settings.operator_password:
            return "Operator password is not configured (OPERATOR_PASSWORD not set)."
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "git_sync"})
        return "Enter the operator password to sync state to git:"

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    def _append_note(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        text = str(args.get("text", "")).strip()
        if not text:
            return "I need some note text."
        self._remember_path_undo(chat_id, "append to inbox", [self.storage.notes_dir / "inbox.md"])
        self.storage.append_note(text)
        return "Added that to your inbox."

    def _create_note(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        title = str(args.get("title") or args.get("text") or "").strip()
        if not title:
            return "I need a note title."
        body = str(args.get("body", "")).strip()
        note_id = self.storage._note_id(title)
        self._remember_path_undo(
            chat_id,
            f"create note '{title}'",
            [self.storage.notes_dir / f"{note_id}.md", self.storage.notes_index_path],
        )
        note = self.storage.create_note(title, body)
        self._remember_reference_context(chat_id, "note", [(note.id, note.title)], mode="single")
        return self._with_follow_up_list(
            f"Saved note: {note.title}. Updated {format_note_local(note.updated_at, self.settings.default_timezone)}.",
            self._list_notes(chat_id=chat_id),
        )

    def _list_notes(self, *, chat_id: str = "", remember_context: bool = True) -> str:
        notes = self.storage.list_notes()
        if not notes:
            if remember_context:
                self._clear_reference_context(chat_id)
            return "You have no saved notes."
        if remember_context:
            self._remember_reference_context(chat_id, "note", [(note.id, note.title) for note in notes])
        lines = ["Notes:"]
        for i, note in enumerate(notes, 1):
            preview = note.body[:60].replace("\n", " ") if note.body else ""
            line = f"{i}. {note.title} — {format_note_local(note.updated_at, self.settings.default_timezone)}"
            if preview:
                line += f" — {preview}{'…' if len(note.body) > 60 else ''}"
            lines.append(line)
        return "\n".join(lines)

    def _view_note(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which note to view."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="note",
            identifier=identifier,
            action="view_note",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_note_identifier(identifier)
        note = self.storage.find_note(identifier)
        if note is None:
            return "I couldn't find that note."
        self._remember_reference_context(chat_id, "note", [(note.id, note.title)], mode="single")
        lines = [
            f"Note: {note.title}",
            f"Created: {format_note_local(note.created_at, self.settings.default_timezone)}",
            f"Updated: {format_note_local(note.updated_at, self.settings.default_timezone)}",
        ]
        if note.body:
            lines.append(f"\nDescription:\n{note.body}")
        return "\n".join(lines)

    def _update_note(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        field = str(args.get("field", "")).strip().lower()
        value = str(args.get("value", "")).strip()
        if not identifier or not field:
            return "I need a note identifier and field to update."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="note",
            identifier=identifier,
            action="update_note",
            args={**args, "field": field, "value": value},
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_note_identifier(identifier)
        existing = self.storage.find_note(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"update note '{existing.title}'",
                [self.storage.notes_dir / f"{existing.id}.md", self.storage.notes_index_path],
            )
        if field == "title":
            note = self.storage.update_note(identifier, title=value)
        elif field == "description":
            note = self.storage.update_note(identifier, body=value)
        else:
            return f"Unknown field '{field}'. Use title or description."
        if note is None:
            return "I couldn't find that note."
        self._remember_reference_context(chat_id, "note", [(note.id, note.title)], mode="single")
        return self._with_follow_up_list(f"Updated note: {note.title}.", self._list_notes(chat_id=chat_id))

    def _delete_note(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which note to delete."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="note",
            identifier=identifier,
            action="delete_note",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_note_identifier(identifier)
        existing = self.storage.find_note(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"delete note '{existing.title}'",
                [self.storage.notes_dir / f"{existing.id}.md", self.storage.notes_index_path],
            )
        note = self.storage.delete_note(identifier)
        if note is None:
            return "I couldn't find that note."
        return self._with_follow_up_list(f"Deleted note: {note.title}.", self._list_notes(chat_id=chat_id))

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def _append_project(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        text = str(args.get("text", "")).strip()
        if not text:
            return "I need some project text."
        self._remember_path_undo(chat_id, "append to projects", [self.storage.projects_dir / "projects.md"])
        self.storage.append_project(text)
        return "Added that to your projects."

    def _create_project(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        title = str(args.get("title") or args.get("text") or "").strip()
        if not title:
            return "I need a project title."
        body = str(args.get("body", "")).strip()
        project_id = self.storage._project_id(title)
        self._remember_path_undo(
            chat_id,
            f"create project '{title}'",
            [self.storage.projects_dir / f"{project_id}.md", self.storage.projects_index_path],
        )
        project = self.storage.create_project(title, body)
        self._remember_reference_context(chat_id, "project", [(project.id, project.title)], mode="single")
        return self._with_follow_up_list(f"Saved project: {project.title}.", self._list_projects(chat_id=chat_id))

    def _list_projects(self, *, chat_id: str = "", remember_context: bool = True) -> str:
        projects = self.storage.list_projects()
        if not projects:
            if remember_context:
                self._clear_reference_context(chat_id)
            return "You have no saved projects."
        if remember_context:
            self._remember_reference_context(chat_id, "project", [(project.id, project.title) for project in projects])
        lines = ["Projects:"]
        for i, project in enumerate(projects, 1):
            preview = project.body[:60].replace("\n", " ") if project.body else ""
            line = f"{i}. {project.title}"
            if preview:
                line += f" — {preview}{'…' if len(project.body) > 60 else ''}"
            lines.append(line)
        return "\n".join(lines)

    def _view_project(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which project to view."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="project",
            identifier=identifier,
            action="view_project",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_project_identifier(identifier)
        project = self.storage.find_project(identifier)
        if project is None:
            return "I couldn't find that project."
        self._remember_reference_context(chat_id, "project", [(project.id, project.title)], mode="single")
        lines = [f"Project: {project.title}"]
        if project.body:
            lines.append(f"\nDescription:\n{project.body}")
        return "\n".join(lines)

    def _update_project(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        field = str(args.get("field", "")).strip().lower()
        value = str(args.get("value", "")).strip()
        if not identifier or not field:
            return "I need a project identifier and field to update."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="project",
            identifier=identifier,
            action="update_project",
            args={**args, "field": field, "value": value},
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_project_identifier(identifier)
        existing = self.storage.find_project(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"update project '{existing.title}'",
                [self.storage.projects_dir / f"{existing.id}.md", self.storage.projects_index_path],
            )
        if field == "title":
            project = self.storage.update_project(identifier, title=value)
        elif field == "description":
            project = self.storage.update_project(identifier, body=value)
        else:
            return f"Unknown field '{field}'. Use title or description."
        if project is None:
            return "I couldn't find that project."
        self._remember_reference_context(chat_id, "project", [(project.id, project.title)], mode="single")
        return self._with_follow_up_list(f"Updated project: {project.title}.", self._list_projects(chat_id=chat_id))

    def _delete_project(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which project to delete."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="project",
            identifier=identifier,
            action="delete_project",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_project_identifier(identifier)
        existing = self.storage.find_project(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"delete project '{existing.title}'",
                [self.storage.projects_dir / f"{existing.id}.md", self.storage.projects_index_path],
            )
        project = self.storage.delete_project(identifier)
        if project is None:
            return "I couldn't find that project."
        return self._with_follow_up_list(f"Deleted project: {project.title}.", self._list_projects(chat_id=chat_id))

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    def _append_plan(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        text = str(args.get("text", "")).strip()
        if not text:
            return "I need some plan text."
        self._remember_path_undo(chat_id, "append to plans", [self.storage.plans_dir / "plans.md"])
        self.storage.append_plan(text)
        return "Added that to your plans."

    def _create_plan(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        title = str(args.get("title") or args.get("text") or "").strip()
        if not title:
            return "I need a plan title."
        body = str(args.get("body", "")).strip()
        plan_id = self.storage._plan_id(title)
        self._remember_path_undo(
            chat_id,
            f"create plan '{title}'",
            [self.storage.plans_dir / f"{plan_id}.md", self.storage.plans_index_path],
        )
        plan = self.storage.create_plan(title, body)
        self._remember_reference_context(chat_id, "plan", [(plan.id, plan.title)], mode="single")
        return self._with_follow_up_list(f"Saved plan: {plan.title}.", self._list_plans(chat_id=chat_id))

    def _list_plans(self, *, chat_id: str = "") -> str:
        plans = self.storage.list_plans()
        if not plans:
            self._clear_reference_context(chat_id)
            return "You have no saved plans."
        self._remember_reference_context(chat_id, "plan", [(plan.id, plan.title) for plan in plans])
        lines = ["Plans:"]
        for i, plan in enumerate(plans, 1):
            preview = plan.body[:60].replace("\n", " ") if plan.body else ""
            line = f"{i}. {plan.title}"
            if preview:
                line += f" — {preview}{'…' if len(plan.body) > 60 else ''}"
            lines.append(line)
        return "\n".join(lines)

    def _view_plan(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which plan to view."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="plan",
            identifier=identifier,
            action="view_plan",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or identifier
        plan = self.storage.find_plan(identifier)
        if plan is None:
            return "I couldn't find that plan."
        self._remember_reference_context(chat_id, "plan", [(plan.id, plan.title)], mode="single")
        lines = [f"Plan: {plan.title}"]
        if plan.body:
            lines.append(f"\nDescription:\n{plan.body}")
        return "\n".join(lines)

    def _update_plan(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        field = str(args.get("field", "")).strip().lower()
        value = str(args.get("value", "")).strip()
        if not identifier or not field:
            return "I need a plan identifier and field to update."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="plan",
            identifier=identifier,
            action="update_plan",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or identifier
        existing = self.storage.find_plan(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"update plan '{existing.title}'",
                [self.storage.plans_dir / f"{existing.id}.md", self.storage.plans_index_path],
            )
        if field == "title":
            plan = self.storage.update_plan(identifier, title=value)
        elif field == "description":
            plan = self.storage.update_plan(identifier, body=value)
        else:
            return f"Unknown field '{field}'. Use title or description."
        if plan is None:
            return "I couldn't find that plan."
        self._remember_reference_context(chat_id, "plan", [(plan.id, plan.title)], mode="single")
        return self._with_follow_up_list(f"Updated plan: {plan.title}.", self._list_plans(chat_id=chat_id))

    def _delete_plan(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which plan to delete."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="plan",
            identifier=identifier,
            action="delete_plan",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or identifier
        existing = self.storage.find_plan(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"delete plan '{existing.title}'",
                [self.storage.plans_dir / f"{existing.id}.md", self.storage.plans_index_path],
            )
        plan = self.storage.delete_plan(identifier)
        if plan is None:
            return "I couldn't find that plan."
        return self._with_follow_up_list(f"Deleted plan: {plan.title}.", self._list_plans(chat_id=chat_id))

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _append_preference(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        text = str(args.get("text", "")).strip()
        if not text:
            return "I need some preference text."
        self._remember_path_undo(
            chat_id,
            "append to preferences",
            [self.storage.preferences_dir / "preferences.md"],
        )
        self.storage.append_preference(text)
        return "Saved that to your preferences."

    def _create_preference(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        title = str(args.get("title") or args.get("text") or "").strip()
        if not title:
            return "I need a preference title."
        body = str(args.get("body", "")).strip()
        pref_id = self.storage._preference_id(title)
        self._remember_path_undo(
            chat_id,
            f"create preference '{title}'",
            [self.storage.preferences_dir / f"{pref_id}.md", self.storage.preferences_index_path],
        )
        pref = self.storage.create_preference(title, body)
        self._remember_reference_context(chat_id, "preference", [(pref.id, pref.title)], mode="single")
        return self._with_follow_up_list(
            f"Saved preference: {pref.title}.",
            self._list_preferences(chat_id=chat_id),
        )

    def _list_preferences(self, *, chat_id: str = "") -> str:
        prefs = self.storage.list_preferences()
        if not prefs:
            self._clear_reference_context(chat_id)
            return "You have no saved preferences."
        self._remember_reference_context(chat_id, "preference", [(pref.id, pref.title) for pref in prefs])
        lines = ["Preferences:"]
        for i, pref in enumerate(prefs, 1):
            preview = pref.body[:60].replace("\n", " ") if pref.body else ""
            line = f"{i}. {pref.title}"
            if preview:
                line += f" — {preview}{'…' if len(pref.body) > 60 else ''}"
            lines.append(line)
        return "\n".join(lines)

    def _view_preference(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which preference to view."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="preference",
            identifier=identifier,
            action="view_preference",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_preference_identifier(identifier)
        pref = self.storage.find_preference(identifier)
        if pref is None:
            return "I couldn't find that preference."
        self._remember_reference_context(chat_id, "preference", [(pref.id, pref.title)], mode="single")
        lines = [f"Preference: {pref.title}"]
        if pref.body:
            lines.append(f"\nValue:\n{pref.body}")
        return "\n".join(lines)

    def _update_preference(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        field = str(args.get("field", "")).strip().lower()
        value = str(args.get("value", "")).strip()
        if not identifier or not field:
            return "I need a preference identifier and field to update."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="preference",
            identifier=identifier,
            action="update_preference",
            args={**args, "field": field, "value": value},
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_preference_identifier(identifier)
        existing = self.storage.find_preference(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"update preference '{existing.title}'",
                [self.storage.preferences_dir / f"{existing.id}.md", self.storage.preferences_index_path],
            )
        if field == "title":
            pref = self.storage.update_preference(identifier, title=value)
        elif field == "description":
            pref = self.storage.update_preference(identifier, body=value)
        else:
            return f"Unknown field '{field}'. Use title or description."
        if pref is None:
            return "I couldn't find that preference."
        self._remember_reference_context(chat_id, "preference", [(pref.id, pref.title)], mode="single")
        return self._with_follow_up_list(
            f"Updated preference: {pref.title}.",
            self._list_preferences(chat_id=chat_id),
        )

    def _delete_preference(self, args: dict[str, Any], *, chat_id: str = "") -> str:
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return "Tell me which preference to delete."
        resolved, follow_up = self._resolve_entity_identifier(
            chat_id=chat_id,
            entity_type="preference",
            identifier=identifier,
            action="delete_preference",
            args=args,
        )
        if follow_up is not None:
            return follow_up
        identifier = resolved or self._resolve_preference_identifier(identifier)
        existing = self.storage.find_preference(identifier)
        if existing is not None:
            self._remember_path_undo(
                chat_id,
                f"delete preference '{existing.title}'",
                [self.storage.preferences_dir / f"{existing.id}.md", self.storage.preferences_index_path],
            )
        pref = self.storage.delete_preference(identifier)
        if pref is None:
            return "I couldn't find that preference."
        return self._with_follow_up_list(
            f"Deleted preference: {pref.title}.",
            self._list_preferences(chat_id=chat_id),
        )

    def _request_people_add(self, chat_id: str, args: dict[str, Any]) -> str:
        if not self.settings.people_encryption_key:
            return "People encryption is not configured (PEOPLE_ENCRYPTION_KEY not set)."
        name = str(args.get("name", "")).strip()
        info = str(args.get("info", "")).strip()
        if not name or not info:
            return "I need a name and some info. Try: person <name>: <info>"
        if chat_id:
            self.storage.set_pending(chat_id, {"type": "people_add", "name": name, "info": info})
        return f"Enter the people password to save an entry for {name}:"

    def _request_people_edit(self, chat_id: str, args: dict[str, Any]) -> str:
        if not self.settings.people_encryption_key:
            return "People encryption is not configured (PEOPLE_ENCRYPTION_KEY not set)."
        person = str(args.get("person", "")).strip()
        entry = int(args.get("entry", 0))
        info = str(args.get("info", "")).strip()
        if not person or entry <= 0 or not info:
            return "Use: edit person <person> entry <#>: <new text>"
        if chat_id:
            self.storage.set_pending(
                chat_id,
                {"type": "people_edit", "person": person, "entry": entry, "info": info},
            )
        return f"Enter the people password to update entry {entry} for {person}:"

    def _request_people_delete(self, chat_id: str, args: dict[str, Any]) -> str:
        if not self.settings.people_encryption_key:
            return "People encryption is not configured (PEOPLE_ENCRYPTION_KEY not set)."
        person = str(args.get("person", "")).strip()
        entry = args.get("entry")
        if not person:
            return "Use: delete person <person> [entry <#>]"
        pending = {"type": "people_delete", "person": person}
        if entry is not None:
            pending["entry"] = int(entry)
        if chat_id:
            self.storage.set_pending(chat_id, pending)
        if entry is not None:
            return f"Enter the people password to delete entry {entry} for {person}:"
        return f"Enter the people password to delete {person}:"

    def _operator_password_valid(self, password: str) -> bool:
        expected = self.settings.operator_password
        if not expected:
            return False
        return hmac.compare_digest(password.strip(), expected)

    def _force_git_sync(self) -> str:
        try:
            if not self.backup_service.durable_changes_pending():
                return "No durable state changes to sync."
            synced = self.backup_service.backup_now(force=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            if detail:
                return f"Git sync failed: {detail.splitlines()[-1]}"
            return "Git sync failed."
        if not synced:
            return "No durable state changes to sync."
        return "Synced durable state to git."

    @staticmethod
    def _user_safe_error(exc: Exception) -> str:
        message = str(exc).strip()
        if message:
            return f"Error: {message}"
        return "Error: something went wrong while handling that request."

    @staticmethod
    def _normalize_user_text(text: str) -> str:
        cleaned = TELEGRAM_INVISIBLE_RE.sub("", str(text or "")).strip()
        if not cleaned.startswith("/"):
            return cleaned
        command, separator, remainder = cleaned.partition(" ")
        if "@" in command:
            command = command.split("@", 1)[0]
        if separator:
            return f"{command}{separator}{remainder}"
        return command

    @staticmethod
    def _extract_ai_prompt(text: str) -> str | None:
        lowered = text.lower()
        if lowered == "ai" or lowered == "/ai":
            return ""
        if lowered.startswith("ai "):
            return text[3:].strip()
        if lowered.startswith("/ai "):
            return text[4:].strip()
        return None

    @staticmethod
    def _is_people_command_text(text: str) -> bool:
        lowered = text.strip().lower()
        return (
            lowered in {"people", "list people", "show people", "/people", "pel"}
            or lowered.startswith(("person ", "/person ", "pe ", "edit person ", "update person ", "delete person ", "remove person "))
        )

    @staticmethod
    def _is_private_command_text(text: str) -> bool:
        lowered = text.strip().lower()
        if lowered in {
            "pts",
            "/pts",
            "private tasks",
            "list private tasks",
            "show private tasks",
            "prs",
            "/prs",
            "private reminders",
            "list private reminders",
            "show private reminders",
        }:
            return True
        return lowered.startswith(
            (
                "pt ",
                "/pt ",
                "private task ",
                "show pt ",
                "view pt ",
                "update pt ",
                "delete pt ",
                "remove pt ",
                "done pt ",
                "complete pt ",
                "finish pt ",
                "show private task ",
                "view private task ",
                "update private task ",
                "delete private task ",
                "remove private task ",
                "complete private task ",
                "pr ",
                "/pr ",
                "private reminder ",
                "show pr ",
                "view pr ",
                "update pr ",
                "delete pr ",
                "remove pr ",
                "cancel pr ",
                "ack pr ",
                "show private reminder ",
                "view private reminder ",
                "update private reminder ",
                "delete private reminder ",
                "remove private reminder ",
                "cancel private reminder ",
                "ack private reminder ",
            )
        )

    def _should_delete_incoming_message(self, pending: dict[str, Any] | None, text: str) -> bool:
        if pending is not None and pending.get("type") in {
            "people_view",
            "people_add",
            "people_edit",
            "people_delete",
            "git_sync",
            "private_auth",
            "private_select",
            "private_datetime",
            "private_reminder_snooze_duration",
        }:
            return True
        return self._is_people_command_text(text) or self._is_private_command_text(text)

    def _response_auto_delete_seconds(self, pending: dict[str, Any] | None, text: str) -> int | None:
        if pending is not None and pending.get("type") in {
            "people_view",
            "people_add",
            "people_edit",
            "people_delete",
            "people_add_input",
            "people_edit_input",
            "people_delete_input",
            "private_auth",
            "private_select",
            "private_datetime",
            "private_reminder_snooze_duration",
        }:
            return self.SENSITIVE_MESSAGE_TTL_SECONDS
        if self._is_people_command_text(text) or self._is_private_command_text(text):
            return self.SENSITIVE_MESSAGE_TTL_SECONDS
        return None

    @staticmethod
    def _response_parse_mode(text: str, response: str) -> str | None:
        lowered = text.strip().lower()
        if lowered in {"help", "/help", "?", "help all"} or lowered.startswith(("help ", "/help ")):
            return "Markdown"
        if response.startswith("Personal Assistant help") or response.startswith("Personal Assistant - full command list"):
            return "Markdown"
        return None

    def _response_reply_markup(self, *, chat_id: str, request_text: str, response: str) -> dict | None:
        lowered = request_text.strip().lower()
        pending = self.storage.get_pending(chat_id) if chat_id else None
        if pending is not None:
            kind = str(pending.get("type") or "").strip()
            if kind in {"semantic_confirm", "openai_confirm", "bulk_delete_confirm"}:
                return self._reply_keyboard_markup([["yes", "no"], ["help", "/cancel"]], placeholder="Reply yes or no")
            if kind == "entity_disambiguation":
                choices = pending.get("choices") or []
                rows: list[list[str]] = []
                current_row: list[str] = []
                for index, _choice in enumerate(choices[:6], start=1):
                    current_row.append(str(index))
                    if len(current_row) == 3:
                        rows.append(current_row)
                        current_row = []
                if current_row:
                    rows.append(current_row)
                rows.append(["/cancel"])
                return self._reply_keyboard_markup(rows, placeholder="Choose a number")
            if kind in {
                "datetime_clarification",
                "private_datetime",
                "reminder_time_input",
                "reminder_snooze_duration",
                "private_reminder_snooze_duration",
            }:
                return self._reply_keyboard_markup(
                    [["today 9am", "tomorrow 9am"], ["next monday 3pm", "/cancel"]],
                    placeholder="Reply with a date and time",
                )
            if kind == "note_update_select":
                return self._reply_keyboard_markup([["1", "2", "3"], ["notes", "/cancel"]], placeholder="Choose a note")
            if kind == "note_update_value":
                return self._reply_keyboard_markup(
                    [["title ", "description "], ["notes", "/cancel"]],
                    placeholder="Choose title or description",
                )
            if kind == "task_current_input":
                return self._reply_keyboard_markup([["1", "2", "3"], ["tasks", "/cancel"]], placeholder="Choose a task")
        if lowered in {"/start", "start", "menu", "/menu"}:
            return self._main_reply_keyboard_markup()
        if "Open tasks:" in response and "Active reminders:" in response:
            return self._reply_keyboard_markup(
                [
                    ["new task", "complete task", "delete task"],
                    ["new reminder", "delete reminder", "reminders"],
                    ["tasks", "show", "help"],
                ],
                placeholder="Choose an action",
            )
        if response.startswith("Open tasks"):
            return self._reply_keyboard_markup(
                [["new task", "complete task", "delete task"], ["current task", "tasks", "show"], ["help"]],
                placeholder="Choose a task action",
            )
        if response.startswith("Active reminders"):
            return self._reply_keyboard_markup(
                [["new reminder", "delete reminder", "calendar"], ["reminders", "show", "help"]],
                placeholder="Choose a reminder action",
            )
        if response.startswith("Notes:"):
            return self._reply_keyboard_markup(
                [["new note", "update note", "delete note"], ["notes", "show", "help"]],
                placeholder="Choose a notes action",
            )
        if response.startswith("System health"):
            return self._reply_keyboard_markup(
                [["show", "help system"], ["sync", "debug info"]],
                placeholder="Check or manage the system",
            )
        if response.startswith("Personal Assistant") or lowered.startswith("help"):
            return self._main_reply_keyboard_markup()
        if response.startswith("AI run status") or lowered.startswith("ai"):
            return self._reply_keyboard_markup(
                [["ai running", "ai status"], ["show", "help ai"]],
                placeholder="Ask about AI state",
            )
        return None

    @staticmethod
    def _reply_keyboard_markup(rows: list[list[str]], *, placeholder: str = "") -> dict:
        keyboard = [[{"text": item} for item in row if item] for row in rows if row]
        return {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "is_persistent": False,
            "one_time_keyboard": False,
            "input_field_placeholder": placeholder,
        }

    @classmethod
    def _main_reply_keyboard_markup(cls) -> dict:
        return cls._reply_keyboard_markup(
            [
                ["show", "tasks", "reminders"],
                ["calendar", "notes", "help"],
                ["undo", "ai running", "system health"],
            ],
            placeholder="Type or tap a daily action",
        )

    @staticmethod
    def _message_chunks(text: str, *, limit: int = TELEGRAM_MESSAGE_SOFT_LIMIT) -> list[str]:
        stripped = str(text or "")
        if len(stripped) <= limit:
            return [stripped]
        chunks: list[str] = []
        current = ""
        for line in stripped.splitlines():
            candidate = f"{current}\n{line}".strip("\n") if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(line) <= limit:
                current = line
                continue
            words = line.split(" ")
            partial = ""
            for word in words:
                candidate = f"{partial} {word}".strip()
                if len(candidate) <= limit:
                    partial = candidate
                    continue
                if partial:
                    chunks.append(partial)
                partial = word
            if partial:
                current = partial
        if current:
            chunks.append(current)
        return chunks or [stripped]

    async def _send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        delete_after_seconds: int | None = None,
    ) -> None:
        chunks = self._message_chunks(text)
        for index, chunk in enumerate(chunks):
            message_id = await self.telegram.send_message(
                chat_id,
                chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
                parse_mode=parse_mode,
            )
            if delete_after_seconds is not None and message_id is not None:
                self._schedule_message_delete(chat_id, message_id, delete_after_seconds)

    def _schedule_message_delete(self, chat_id: str | int, message_id: int, delay_seconds: int) -> None:
        asyncio.create_task(self._delete_message_later(chat_id, message_id, delay_seconds))

    async def _delete_message_later(self, chat_id: str | int, message_id: int, delay_seconds: int) -> None:
        await asyncio.sleep(max(delay_seconds, 0))
        with contextlib.suppress(Exception):
            await self.telegram.delete_message(chat_id, message_id)

    async def send_due_reminders(self) -> int:
        sent = 0
        for reminder in self.storage.due_reminders():
            # For non-recurring reminders, include an "acknowledge" button.
            reply_markup: dict | None = None
            if not reminder.recurrence:
                reply_markup = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Got it", "callback_data": f"ack:{reminder.id}"},
                            {"text": "😴 Snooze", "callback_data": f"snooze:{reminder.id}"},
                        ]
                    ]
                }
            await self.telegram.send_message(
                self.settings.telegram_allowed_user_id,
                self._format_due_reminder(reminder),
                reply_markup=reply_markup,
            )
            self.storage.mark_reminder_sent(reminder.id)
            sent += 1
        if self.settings.people_encryption_key:
            for reminder in self.storage.due_private_reminders():
                await self.telegram.send_message(
                    self.settings.telegram_allowed_user_id,
                    "🔐 You have a private reminder.",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "🔓 Reveal", "callback_data": f"private_reveal:{reminder.id}"}]
                        ]
                    },
                )
                self.storage.mark_private_reminder_sent(reminder.id, password=self._private_system_password())
                sent += 1
        return sent

    async def resend_unacked_reminders(self) -> int:
        """Re-send non-recurring reminders that haven't been acknowledged within the timeout."""
        user_settings = self.storage.get_user_settings()
        ack_timeout = (
            user_settings.reminder_ack_timeout_minutes
            if user_settings.reminder_ack_timeout_minutes is not None
            else self.settings.reminder_ack_timeout_minutes
        )
        resent = 0
        for reminder in self.storage.list_sent_unacked_reminders(ack_timeout_minutes=ack_timeout):
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Got it", "callback_data": f"ack:{reminder.id}"},
                        {"text": "😴 Snooze", "callback_data": f"snooze:{reminder.id}"},
                    ]
                ]
            }
            await self.telegram.send_message(
                self.settings.telegram_allowed_user_id,
                f"⏰ (Reminder) {self._format_due_reminder(reminder)}",
                reply_markup=reply_markup,
            )
            # Update last_sent_at so the timeout resets.
            self.storage.mark_reminder_sent(reminder.id)
            # Re-set status to "sent" (mark_reminder_sent for non-recurring sets to "sent", so this is idempotent).
            resent += 1
        if self.settings.people_encryption_key:
            for reminder in self.storage.list_private_sent_unacked_reminders(
                password=self._private_system_password(),
                ack_timeout_minutes=ack_timeout,
            ):
                await self.telegram.send_message(
                    self.settings.telegram_allowed_user_id,
                    "🔐 You still have an unacknowledged private reminder.",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "🔓 Reveal", "callback_data": f"private_reveal:{reminder.id}"}]
                        ]
                    },
                )
                self.storage.mark_private_reminder_sent(reminder.id, password=self._private_system_password())
                resent += 1
        return resent

    def _format_due_reminder(self, reminder: ReminderRecord) -> str:
        return f"⏰ Reminder: {reminder.text} ({format_local(reminder.due_at, self.settings.default_timezone)})"

    def webhook_secret_matches(self, candidate: str) -> bool:
        return bool(self.settings.telegram_webhook_secret.strip()) and hmac.compare_digest(candidate, self.settings.telegram_webhook_secret)

    # ------------------------------------------------------------------
    # Inline keyboard menu helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _main_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "🏠 Dashboard", "callback_data": "menu:dashboard"},
                    {"text": "↩ Undo", "callback_data": "menu:undo"},
                ],
                [
                    {"text": "📋 Tasks", "callback_data": "menu:tasks"},
                    {"text": "⏰ Reminders", "callback_data": "menu:reminders"},
                ],
                [
                    {"text": "📅 Calendar", "callback_data": "menu:calendar"},
                    {"text": "📝 Notes", "callback_data": "menu:notes"},
                ],
                [
                    {"text": "👤 People", "callback_data": "menu:people"},
                    {"text": "⚙️ Preferences", "callback_data": "menu:preferences"},
                ],
                [
                    {"text": "⚙️ Settings", "callback_data": "menu:settings"},
                    {"text": "📚 Help", "callback_data": "menu:help"},
                ],
            ]
        }

    @staticmethod
    def _tasks_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 List Tasks", "callback_data": "tasks:list"},
                    {"text": "➕ New Task", "callback_data": "tasks:new"},
                ],
                [
                    {"text": "✅ Done", "callback_data": "tasks:done"},
                    {"text": "⭐ Current", "callback_data": "tasks:current"},
                    {"text": "🗑 Delete", "callback_data": "tasks:delete"},
                ],
                [
                    {"text": "📆 By Due", "callback_data": "tasks:sort_due"},
                    {"text": "⏰ By Remind", "callback_data": "tasks:sort_remind"},
                ],
                [
                    {"text": "🔥 By Priority", "callback_data": "tasks:sort_priority"},
                    {"text": "🔎 Filter Tag", "callback_data": "tasks:filter_tag"},
                ],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    @staticmethod
    def _reminders_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 List", "callback_data": "reminders:list"},
                    {"text": "➕ New", "callback_data": "reminders:new"},
                ],
                [
                    {"text": "🗑 Delete One", "callback_data": "reminders:delete"},
                    {"text": "🧹 Cancel All", "callback_data": "reminders:cancel_all"},
                ],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    @staticmethod
    def _calendar_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📅 Agenda", "callback_data": "calendar:agenda"},
                    {"text": "🕳 Free Slot", "callback_data": "calendar:free_slot"},
                ],
                [
                    {"text": "➕ New Event", "callback_data": "calendar:new"},
                ],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    @staticmethod
    def _people_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 List People", "callback_data": "people:list"},
                    {"text": "➕ Add", "callback_data": "people:add"},
                ],
                [
                    {"text": "✏ Edit", "callback_data": "people:edit"},
                    {"text": "🗑 Delete", "callback_data": "people:delete"},
                ],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    @staticmethod
    def _notes_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 List Notes", "callback_data": "notes:list"},
                    {"text": "➕ New Note", "callback_data": "notes:new"},
                ],
                [
                    {"text": "✏ Update", "callback_data": "notes:update"},
                    {"text": "🗑 Delete", "callback_data": "notes:delete"},
                ],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    @staticmethod
    def _plans_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 List Plans", "callback_data": "plans:list"},
                    {"text": "➕ New Plan", "callback_data": "plans:new"},
                ],
                [{"text": "🗑 Delete", "callback_data": "plans:delete"}],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    @staticmethod
    def _preferences_menu_markup() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 List Prefs", "callback_data": "preferences:list"},
                    {"text": "➕ New Pref", "callback_data": "preferences:new"},
                ],
                [{"text": "🗑 Delete", "callback_data": "preferences:delete"}],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    @staticmethod
    def _settings_menu_text(user_settings: UserSettings) -> str:
        return (
            "⚙️ Settings\n\n"
            f"• Reminder ack timeout: {user_settings.reminder_ack_timeout_minutes} min\n"
            f"• Reminder poll interval: {user_settings.reminder_poll_seconds} sec"
        )

    @staticmethod
    def _settings_menu_markup(user_settings: UserSettings) -> dict:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"⏱ Ack Timeout: {user_settings.reminder_ack_timeout_minutes} min",
                        "callback_data": "settings:ack_timeout",
                    }
                ],
                [
                    {
                        "text": f"🔄 Poll: {user_settings.reminder_poll_seconds} sec",
                        "callback_data": "settings:poll",
                    }
                ],
                [{"text": "◀ Back", "callback_data": "menu:main"}],
            ]
        }

    def _show_menu_text(self) -> str:
        return (
            "👋 What can I help you with?\n\n"
            "Use `show` for your dashboard, then `tasks`, `reminders`, `calendar`, or `notes` for the main areas. "
            "Use `undo` to reverse your last change, `system health` for backup/state health, or `help` for the guide."
        )

    def _show_settings_text(self) -> str:
        user_settings = self.storage.get_user_settings()
        return self._settings_menu_text(user_settings)
