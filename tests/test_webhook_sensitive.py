from __future__ import annotations

import httpx

from personal_assistant.app import create_app


async def test_password_messages_are_deleted_after_processing(settings, assistant_bundle):
    settings.people_encryption_key = "secret123"
    assistant, telegram, _, _ = assistant_bundle
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json={
                "update_id": 1,
                "message": {
                    "message_id": 100,
                    "chat": {"id": "chat-1", "type": "private"},
                    "from": {"id": settings.telegram_allowed_user_id},
                    "text": "people",
                },
            },
        )
        await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json={
                "update_id": 2,
                "message": {
                    "message_id": 101,
                    "chat": {"id": "chat-1", "type": "private"},
                    "from": {"id": settings.telegram_allowed_user_id},
                    "text": "secret123",
                },
            },
        )

    assert ("chat-1", 101) in telegram.deleted_messages


async def test_people_responses_are_marked_for_auto_delete(settings, assistant_bundle):
    settings.people_encryption_key = "secret123"
    assistant, _, _, _ = assistant_bundle
    scheduled: list[tuple[str | int, int, int]] = []

    def capture(chat_id: str | int, message_id: int, delay_seconds: int) -> None:
        scheduled.append((chat_id, message_id, delay_seconds))

    assistant._schedule_message_delete = capture  # type: ignore[method-assign]
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json={
                "update_id": 10,
                "message": {
                    "message_id": 110,
                    "chat": {"id": "chat-1", "type": "private"},
                    "from": {"id": settings.telegram_allowed_user_id},
                    "text": "person Alice: private detail",
                },
            },
        )

    assert scheduled
    assert scheduled[-1][2] == assistant.SENSITIVE_MESSAGE_TTL_SECONDS


async def test_runtime_errors_become_user_messages(settings, assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle
    app = create_app(settings=settings, assistant=assistant)

    async def boom(*, chat_id: str, text: str) -> str:
        raise RuntimeError("Google Calendar credentials are not configured.")

    assistant.handle_message = boom  # type: ignore[method-assign]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json={
                "update_id": 3,
                "message": {
                    "message_id": 102,
                    "chat": {"id": "chat-1", "type": "private"},
                    "from": {"id": settings.telegram_allowed_user_id},
                    "text": "agenda",
                },
            },
        )

    assert response.status_code == 200
    assert telegram.sent_messages[-1][1] == "Error: Google Calendar credentials are not configured."
