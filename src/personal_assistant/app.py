from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status

from personal_assistant.assistant import AssistantService
from personal_assistant.config import Settings, get_settings
from personal_assistant.models import (
    OpenClawReminderResponse,
    OpenClawRemindersResponse,
    OpenClawStatusResponse,
    OpenClawTaskResponse,
    OpenClawTasksResponse,
)
from personal_assistant.storage import Storage


def create_app(
    *,
    settings: Settings | None = None,
    assistant: AssistantService | None = None,
) -> FastAPI:
    current_settings = settings or get_settings()
    storage = Storage(current_settings)
    storage.bootstrap()
    storage.recover_transactions()
    app = FastAPI(
        title="Personal Assistant",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    assistant_service = assistant or AssistantService(current_settings, storage)

    def require_openclaw_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = current_settings.openclaw_api_token
        scheme, separator, candidate = (authorization or "").partition(" ")
        token_matches = hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OpenClaw API is not configured.",
                headers={"Cache-Control": "no-store"},
            )
        if separator != " " or scheme.lower() != "bearer" or not candidate or not token_matches:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials.",
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/telegram/webhook/{secret}")
    async def telegram_webhook(secret: str, update: dict) -> dict[str, bool]:
        if not assistant_service.webhook_secret_matches(secret):
            raise HTTPException(status_code=403, detail="Invalid webhook secret.")
        await assistant_service.process_update(update)
        return {"ok": True}

    @app.get(
        "/internal/v1/status",
        response_model=OpenClawStatusResponse,
        dependencies=[Depends(require_openclaw_auth)],
    )
    async def openclaw_status(response: Response) -> OpenClawStatusResponse:
        response.headers["Cache-Control"] = "no-store"
        return OpenClawStatusResponse()

    @app.get(
        "/internal/v1/tasks",
        response_model=OpenClawTasksResponse,
        dependencies=[Depends(require_openclaw_auth)],
    )
    async def openclaw_tasks(response: Response) -> OpenClawTasksResponse:
        response.headers["Cache-Control"] = "no-store"
        tasks = [
            OpenClawTaskResponse.model_validate(
                task.model_dump(include=set(OpenClawTaskResponse.model_fields))
            )
            for task in storage.list_tasks(include_completed=False, kind="self")
        ]
        return OpenClawTasksResponse(tasks=tasks, count=len(tasks))

    @app.get(
        "/internal/v1/reminders",
        response_model=OpenClawRemindersResponse,
        dependencies=[Depends(require_openclaw_auth)],
    )
    async def openclaw_reminders(response: Response) -> OpenClawRemindersResponse:
        response.headers["Cache-Control"] = "no-store"
        reminders = [
            OpenClawReminderResponse.model_validate(
                reminder.model_dump(include=set(OpenClawReminderResponse.model_fields))
            )
            for reminder in storage.list_reminders()
            if reminder.source_type != "private_task"
        ]
        return OpenClawRemindersResponse(reminders=reminders, count=len(reminders))

    return app


app = create_app()
