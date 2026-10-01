from __future__ import annotations

from pathlib import Path

import httpx

from personal_assistant.app import create_app
from personal_assistant.time_utils import now_utc


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


async def test_openclaw_endpoints_require_configured_bearer_token(settings, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    settings.openclaw_api_token = "test-openclaw-token"
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        for path in (
            "/internal/v1/status",
            "/internal/v1/tasks",
            "/internal/v1/reminders",
        ):
            missing = await client.get(path)
            query_token = await client.get(path, params={"token": "test-openclaw-token"})
            invalid = await client.get(path, headers={"Authorization": "Bearer wrong-token"})
            malformed = await client.get(path, headers={"Authorization": "Basic test-openclaw-token"})
            empty = await client.get(path, headers={"Authorization": "Bearer "})
            valid = await client.get(
                path,
                headers={"Authorization": "bearer test-openclaw-token"},
            )

            unauthorized = (missing, query_token, invalid, malformed, empty)
            assert all(response.status_code == 401 for response in unauthorized)
            assert all(
                response.json() == {"detail": "Invalid authentication credentials."}
                for response in unauthorized
            )
            assert all(response.headers["www-authenticate"] == "Bearer" for response in unauthorized)
            assert all(response.headers["cache-control"] == "no-store" for response in unauthorized)
            assert valid.status_code == 200
            assert valid.headers["cache-control"] == "no-store"
            assert "test-openclaw-token" not in valid.text


async def test_openclaw_endpoints_reject_write_methods(settings, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    settings.openclaw_api_token = "test-openclaw-token"
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        for path in (
            "/internal/v1/status",
            "/internal/v1/tasks",
            "/internal/v1/reminders",
        ):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                response = await client.request(
                    method,
                    path,
                    headers={"Authorization": "Bearer test-openclaw-token"},
                )
                assert response.status_code == 405


async def test_openclaw_api_is_disabled_without_a_token(settings, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    settings.openclaw_api_token = ""
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/internal/v1/status",
            headers={"Authorization": "Bearer any-token"},
        )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "OpenClaw API is not configured."}


async def test_openclaw_status_reports_only_read_capabilities(settings, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    settings.openclaw_api_token = "test-openclaw-token"
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/internal/v1/status",
            headers={"Authorization": "Bearer test-openclaw-token"},
        )

    assert response.json() == {
        "status": "ok",
        "api_version": "v1",
        "capabilities": ["tasks:read", "reminders:read"],
    }


async def test_existing_health_and_telegram_routes_remain_compatible(settings, assistant_bundle):
    assistant, telegram, _, _ = assistant_bundle
    settings.openclaw_api_token = "test-openclaw-token"
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        health = await client.get("/healthz")
        rejected = await client.post("/telegram/webhook/wrong-secret", json={"update_id": 1})
        accepted = await client.post(
            f"/telegram/webhook/{settings.telegram_webhook_secret}",
            json={
                "update_id": 2,
                "message": {
                    "chat": {"id": "chat-1", "type": "private"},
                    "from": {"id": settings.telegram_allowed_user_id},
                    "text": "tasks",
                },
            },
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    assert len(telegram.sent_messages) == 1


async def test_interactive_api_documentation_is_disabled(settings, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    settings.openclaw_api_token = "test-openclaw-token"
    app = create_app(settings=settings, assistant=assistant)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            response = await client.get(path)
            assert response.status_code == 404


async def test_openclaw_tasks_returns_active_self_tasks_only(settings, storage, assistant_bundle):
    assistant, _, _, _ = assistant_bundle
    settings.openclaw_api_token = "test-openclaw-token"
    settings.people_encryption_key = "private-password"
    own = storage.create_task("Visible own task", body="Ordinary details", tags=["test"])
    other = storage.create_task("Visible delegated task", kind="other")
    completed = storage.create_task("Completed task")
    storage.complete_task(completed.id)
    private = storage.create_private_task("Private task", password="private-password")
    app = create_app(settings=settings, assistant=assistant)

    before = _snapshot_tree(settings.state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/internal/v1/tasks",
            headers={"Authorization": "Bearer test-openclaw-token"},
        )
    after = _snapshot_tree(settings.state_dir)

    assert response.status_code == 200
    payload = response.json()
    returned_ids = {task["id"] for task in payload["tasks"]}
    assert payload["count"] == 1
    assert returned_ids == {own.id}
    assert other.id not in returned_ids
    assert completed.id not in returned_ids
    assert private.id not in returned_ids
    assert "Ordinary details" not in response.text
    assert set(payload["tasks"][0]) == {
        "id",
        "title",
        "tags",
        "priority",
        "is_current",
        "due_at",
        "reminder_at",
    }
    assert before == after


async def test_openclaw_reminders_returns_scheduled_ordinary_reminders_only(
    settings,
    storage,
    assistant_bundle,
):
    assistant, _, _, _ = assistant_bundle
    settings.openclaw_api_token = "test-openclaw-token"
    settings.people_encryption_key = "private-password"
    ordinary = storage.create_reminder("Visible reminder", due_at=now_utc())
    task = storage.create_task("Task with reminder", reminder_at=now_utc())
    cancelled = storage.create_reminder("Cancelled reminder", due_at=now_utc())
    storage.cancel_reminder(cancelled.id)
    sent = storage.create_reminder("Sent reminder", due_at=now_utc())
    storage.mark_reminder_sent(sent.id)
    completed = storage.create_reminder("Completed reminder", due_at=now_utc())
    storage.mark_reminder_sent(completed.id)
    storage.ack_reminder(completed.id)
    private = storage.create_private_reminder(
        "Private reminder",
        due_at=now_utc(),
        password="private-password",
    )
    app = create_app(settings=settings, assistant=assistant)

    before = _snapshot_tree(settings.state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/internal/v1/reminders",
            headers={"Authorization": "Bearer test-openclaw-token"},
        )
    after = _snapshot_tree(settings.state_dir)

    assert response.status_code == 200
    payload = response.json()
    returned_ids = {reminder["id"] for reminder in payload["reminders"]}
    assert payload["count"] == 2
    assert returned_ids == {ordinary.id, f"task-{task.id}"}
    assert cancelled.id not in returned_ids
    assert sent.id not in returned_ids
    assert completed.id not in returned_ids
    assert private.id not in returned_ids
    assert {reminder["source_type"] for reminder in payload["reminders"]} == {
        "standalone",
        "task",
    }
    assert set(payload["reminders"][0]) == {
        "id",
        "text",
        "due_at",
        "recurrence",
        "source_type",
        "source_id",
    }
    assert before == after
