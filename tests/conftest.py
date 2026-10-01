from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from personal_assistant.assistant import AssistantDependencies, AssistantService
from personal_assistant.config import Settings
from personal_assistant.models import AssistantPlan, CalendarEventSummary, FreeSlot
from personal_assistant.storage import Storage
from personal_assistant.time_utils import now_utc


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str | int, str]] = []
        self.deleted_messages: list[tuple[str | int, int]] = []
        self.answered_callbacks: list[str] = []
        self.edited_messages: list[tuple[str | int, int, str]] = []
        self.sent_markups: list[dict | None] = []
        self.sent_disable_previews: list[bool] = []
        self.edited_disable_previews: list[bool] = []
        self.next_message_id = 1000

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> int:
        self.sent_messages.append((chat_id, text))
        self.sent_markups.append(reply_markup)
        self.sent_disable_previews.append(disable_web_page_preview)
        message_id = self.next_message_id
        self.next_message_id += 1
        return message_id

    async def delete_message(self, chat_id: str | int, message_id: int) -> None:
        self.deleted_messages.append((chat_id, message_id))

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.answered_callbacks.append(callback_query_id)

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        self.edited_messages.append((chat_id, message_id, text))
        self.edited_disable_previews.append(disable_web_page_preview)

    async def edit_message_reply_markup(
        self,
        chat_id: str | int,
        message_id: int,
        reply_markup: dict | None,
    ) -> None:
        pass

    async def set_webhook(self, webhook_url: str) -> dict:
        return {"ok": True, "url": webhook_url}


@dataclass
class FakeCalendarClient:
    upcoming_events: list[CalendarEventSummary] = field(default_factory=list)
    free_slots: list[FreeSlot] = field(default_factory=list)
    created_events: list[CalendarEventSummary] = field(default_factory=list)
    deleted_event_ids: list[str] = field(default_factory=list)

    async def list_upcoming_events(self, *, limit: int = 5, time_min=None, time_max=None):
        return self.upcoming_events[:limit]

    async def create_event(self, *, title: str, start, end, description: str = "", metadata=None):
        event = CalendarEventSummary(
            id=f"evt-{len(self.created_events) + 1}",
            title=title,
            start=start,
            end=end,
            description=description,
            owned_by_assistant=True,
        )
        self.created_events.append(event)
        return event

    async def update_owned_event(self, identifier: str, *, title=None, start=None, end=None, description=None):
        for event in self.created_events:
            if event.id == identifier or identifier.lower() in event.title.lower():
                if title:
                    event.title = title
                if start:
                    event.start = start
                if end:
                    event.end = end
                return event
        return None

    async def find_owned_event(self, identifier: str):
        for event in self.created_events:
            if event.id == identifier or identifier.lower() in event.title.lower():
                return event
        return None

    async def delete_owned_event(self, identifier: str) -> bool:
        event = await self.find_owned_event(identifier)
        if event is None:
            return False
        self.created_events = [item for item in self.created_events if item.id != event.id]
        self.deleted_event_ids.append(event.id)
        return True

    async def find_free_slots(self, *, window_start, window_end, duration_minutes: int):
        return self.free_slots

    async def validate(self):
        return True


class FakePlanner:
    def __init__(self, next_plan: AssistantPlan | None = None) -> None:
        self.next_plan = next_plan or AssistantPlan(action="reply", response="Need more detail.")
        self.calls: list[dict] = []
        self.agent_outputs: list[dict] = []
        self.usage_summary: dict = {
            "available": False,
            "reason": "OPENAI_ADMIN_API_KEY is not configured.",
        }
        self.cost_summary: dict = {
            "available": False,
            "reason": "OPENAI_ADMIN_API_KEY is not configured.",
        }

    async def plan(self, *, user_message: str, summary: str, context_snippets: list[str]) -> AssistantPlan:
        self.calls.append(
            {
                "user_message": user_message,
                "summary": summary,
                "context_snippets": context_snippets,
            }
        )
        return self.next_plan

    async def plan_with_trace(self, *, user_message: str, summary: str, context_snippets: list[str]):
        plan = await self.plan(user_message=user_message, summary=summary, context_snippets=context_snippets)
        return plan, {
            "mode": "planner",
            "model": "fake-openai-model",
            "base_url": "https://api.openai.test/v1",
            "user_message": user_message,
            "summary": summary,
            "context_snippets": context_snippets,
            "request_payload": {"messages": [{"role": "system", "content": "fake system"}]},
            "system_prompt": "fake system",
            "user_prompt": f"User message:\n{user_message}",
            "response_content": plan.model_dump_json(),
            "parsed_plan": plan.model_dump(mode="json"),
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "rate_limits": {"remaining_requests": "99", "limit_requests": "100"},
            "request_id": "req_fake_123",
            "duration_ms": 42,
        }

    async def run_agent(self, *, user_message: str, summary: str, context_snippets: list[str], tools, tool_runner, max_steps: int = 8):
        self.calls.append(
            {
                "user_message": user_message,
                "summary": summary,
                "context_snippets": context_snippets,
                "mode": "agent",
            }
        )
        tool_steps: list[dict] = []
        if self.agent_outputs:
            final_output = {
                "type": "finish",
                "message": "Done.",
            }
            for step_index, output in enumerate(self.agent_outputs, start=1):
                if output.get("type") == "tool_call":
                    tool_name = str(output.get("tool") or "")
                    args = dict(output.get("args") or {})
                    result = await tool_runner(tool_name, args)
                    tool_steps.append(
                        {
                            "step": step_index,
                            "tool": tool_name,
                            "args": args,
                            "result": result,
                            "llm_output": output,
                            "llm_duration_ms": 17,
                        }
                    )
                    continue
                final_output = output
                break
            message = str(final_output.get("message") or "Done.")
            return message, {
                "mode": "agent",
                "model": "fake-openai-model",
                "base_url": "https://api.openai.test/v1",
                "user_message": user_message,
                "summary": summary,
                "context_snippets": context_snippets,
                "request_payload": {"mode": "agent", "tools": tools, "max_steps": max_steps},
                "system_prompt": "fake system",
                "user_prompt": f"User request:\n{user_message}",
                "response_content": message,
                "parsed_plan": {"action": "agent"},
                "usage": {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34},
                "rate_limits": {"remaining_requests": "99", "limit_requests": "100"},
                "request_id": "req_fake_agent_123",
                "duration_ms": 84,
                "tool_steps": tool_steps,
                "final_response": message,
                "automation_candidate": dict(final_output.get("automation_candidate") or {}),
            }

        result = await tool_runner(
            "execute_plan",
            {"action": self.next_plan.action, "args": self.next_plan.args},
        )
        message = str(result.get("result") or self.next_plan.response or "Done.")
        return message, {
            "mode": "agent",
            "model": "fake-openai-model",
            "base_url": "https://api.openai.test/v1",
            "user_message": user_message,
            "summary": summary,
            "context_snippets": context_snippets,
            "request_payload": {"mode": "agent", "tools": tools, "max_steps": max_steps},
            "system_prompt": "fake system",
            "user_prompt": f"User request:\n{user_message}",
            "response_content": self.next_plan.model_dump_json(),
            "parsed_plan": self.next_plan.model_dump(mode="json"),
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "rate_limits": {"remaining_requests": "99", "limit_requests": "100"},
            "request_id": "req_fake_123",
            "duration_ms": 42,
            "tool_steps": [
                {
                    "step": 1,
                    "tool": "execute_plan",
                    "args": {"action": self.next_plan.action, "args": self.next_plan.args},
                    "result": result,
                    "llm_output": {"type": "tool_call", "tool": "execute_plan"},
                    "llm_duration_ms": 17,
                }
            ],
            "final_response": message,
        }

    def describe_config(self) -> dict:
        return {
            "configured": True,
            "admin_configured": self.usage_summary.get("available", False) or self.cost_summary.get("available", False),
            "model": "fake-openai-model",
            "base_url": "https://api.openai.test/v1",
        }

    async def get_usage_summary(self, *, days: int = 7) -> dict:
        return dict(self.usage_summary, days=days)

    async def get_cost_summary(self, *, days: int = 30) -> dict:
        return dict(self.cost_summary, days=days)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        telegram_webhook_secret="secret-token",
        telegram_allowed_user_id=12345,
        default_timezone="Asia/Singapore",
        openai_api_key="test-key",
        git_backup_enabled=False,
    )


@pytest.fixture()
def storage(settings):
    instance = Storage(settings)
    instance.bootstrap()
    instance.recover_transactions()
    return instance


@pytest.fixture()
def assistant_bundle(settings, storage):
    telegram = FakeTelegramClient()
    calendar = FakeCalendarClient(
        upcoming_events=[
            CalendarEventSummary(
                id="evt-upcoming",
                title="Weekly sync",
                start=now_utc() + timedelta(hours=1),
                end=now_utc() + timedelta(hours=2),
                owned_by_assistant=False,
            )
        ],
        free_slots=[
            FreeSlot(
                start=now_utc() + timedelta(hours=3),
                end=now_utc() + timedelta(hours=4),
            )
        ],
    )
    planner = FakePlanner()
    assistant = AssistantService(
        settings,
        storage,
        AssistantDependencies(telegram=telegram, calendar=calendar, planner=planner),
    )
    assistant.SENSITIVE_MESSAGE_TTL_SECONDS = 0
    return assistant, telegram, calendar, planner
