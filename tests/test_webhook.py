from __future__ import annotations

import asyncio

import httpx

from personal_assistant.app import create_app
from personal_assistant.time_utils import now_utc


async def test_webhook_secret_validation(settings, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    app = create_app(settings=settings, assistant=assistant)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/telegram/webhook/wrong-secret", json={"update_id": 1})

    assert response.status_code == 403


async def test_webhook_allows_single_user_and_dedupes(settings, assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle
    app = create_app(settings=settings, assistant=assistant)
    payload = {
        "update_id": 9,
        "message": {
            "chat": {"id": "chat-1", "type": "private"},
            "from": {"id": settings.telegram_allowed_user_id},
            "text": "tasks",
        },
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response_one = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json=payload,
        )
        response_two = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json=payload,
        )

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    assert len(telegram.sent_messages) == 1


async def test_webhook_rejects_other_users_silently(settings, assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle
    app = create_app(settings=settings, assistant=assistant)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json={
                "update_id": 7,
                "message": {
                    "chat": {"id": "chat-1", "type": "private"},
                    "from": {"id": 999},
                    "text": "tasks",
                },
            },
        )

    assert response.status_code == 200
    assert telegram.sent_messages == []


async def test_process_update_is_serialized(assistant_bundle, settings):
    assistant, _, _, _ = assistant_bundle
    inflight = 0
    max_inflight = 0

    async def fake_handle_message(*, chat_id: str, text: str) -> str:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return "ok"

    assistant.handle_message = fake_handle_message  # type: ignore[method-assign]

    update_one = {
        "update_id": 100,
        "message": {
            "chat": {"id": "chat-1", "type": "private"},
            "from": {"id": settings.telegram_allowed_user_id},
            "text": "first",
        },
    }
    update_two = {
        "update_id": 101,
        "message": {
            "chat": {"id": "chat-1", "type": "private"},
            "from": {"id": settings.telegram_allowed_user_id},
            "text": "second",
        },
    }

    await asyncio.gather(assistant.process_update(update_one), assistant.process_update(update_two))

    assert max_inflight == 1


async def test_callback_updates_are_deduped(settings, assistant_bundle, storage):
    assistant, telegram, _, _ = assistant_bundle
    reminder = storage.create_reminder("Duplicate-safe", due_at=now_utc())
    storage.mark_reminder_sent(reminder.id)

    app = create_app(settings=settings, assistant=assistant)
    payload = {
        "update_id": 999,
        "callback_query": {
            "id": "cq-dedupe",
            "from": {"id": settings.telegram_allowed_user_id},
            "message": {
                "chat": {"id": "chat-1", "type": "private"},
                "message_id": 77,
                "text": "⏰ Reminder",
            },
            "data": f"ack:{reminder.id}",
        },
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response_one = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json=payload,
        )
        response_two = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json=payload,
        )

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    assert telegram.answered_callbacks.count("cq-dedupe") == 1


async def test_settings_command_sends_settings_markup(settings, assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json={
                "update_id": 1000,
                "message": {
                    "message_id": 88,
                    "chat": {"id": "chat-1", "type": "private"},
                    "from": {"id": settings.telegram_allowed_user_id},
                    "text": "settings",
                },
            },
        )

    assert response.status_code == 200
    assert len(telegram.sent_messages) == 1
    assert "settings" in telegram.sent_messages[0][1].lower()
    assert telegram.sent_markups[0] is not None
