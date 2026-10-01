from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated

import typer
import uvicorn

from personal_assistant.assistant import AssistantService
from personal_assistant.backup import GitBackupService
from personal_assistant.config import get_settings
from personal_assistant.google_oauth import format_google_env_snippet, run_local_google_oauth_flow
from personal_assistant.preflight import collect_preflight_issues, render_preflight_failure
from personal_assistant.storage import Storage
from personal_assistant.telegram_client import TelegramClient
from personal_assistant.worker import BackgroundWorker
from personal_assistant.calendar_client import GoogleCalendarClient


app = typer.Typer(help="Personal assistant operator commands.")


def _build_services() -> tuple[Storage, AssistantService, BackgroundWorker]:
    settings = get_settings()
    storage = Storage(settings)
    storage.bootstrap()
    storage.recover_transactions()
    assistant = AssistantService(settings, storage)
    worker = BackgroundWorker(assistant, GitBackupService(settings))
    return storage, assistant, worker


def _resolve_google_oauth_inputs(
    *,
    client_id: str,
    client_secret: str,
    calendar_id: str,
) -> tuple[str, str, str]:
    settings = get_settings()
    resolved_client_id = client_id.strip() or settings.google_client_id.strip()
    resolved_client_secret = client_secret.strip() or settings.google_client_secret.strip()
    resolved_calendar_id = calendar_id.strip() or settings.calendar_id.strip() or "primary"
    return resolved_client_id, resolved_client_secret, resolved_calendar_id


@app.command()
def bootstrap() -> None:
    storage, _, _ = _build_services()
    storage.rebuild_indexes()
    typer.echo("State tree is ready.")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    uvicorn.run("personal_assistant.app:app", host=host, port=port, reload=False, access_log=False)


@app.command()
def dev(host: str = "127.0.0.1", port: int = 8000) -> None:
    asyncio.run(_run_dev(host=host, port=port))


@app.command("run-worker")
def run_worker() -> None:
    _, _, worker = _build_services()
    asyncio.run(worker.run_forever())


@app.command("register-webhook")
def register_webhook() -> None:
    settings = get_settings()
    if not settings.webhook_url:
        raise typer.BadParameter("PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET must be configured.")
    client = TelegramClient(settings.telegram_bot_token)
    result = asyncio.run(client.set_webhook(settings.webhook_url))
    typer.echo(str(result))


@app.command("validate-google")
def validate_google() -> None:
    settings = get_settings()
    client = GoogleCalendarClient(settings)
    asyncio.run(client.validate())
    typer.echo("Google Calendar credentials look valid.")


@app.command("google-oauth-refresh-token")
def google_oauth_refresh_token(
    client_id: str = typer.Option("", envvar="GOOGLE_CLIENT_ID"),
    client_secret: str = typer.Option("", envvar="GOOGLE_CLIENT_SECRET"),
    calendar_id: str = typer.Option("primary", envvar="CALENDAR_ID"),
    redirect_host: str = typer.Option("127.0.0.1"),
    redirect_port: int = typer.Option(8765, min=1, max=65535),
    timeout_seconds: int = typer.Option(180, min=30),
    open_browser: bool = typer.Option(True, "--open-browser/--no-browser"),
) -> None:
    client_id, client_secret, calendar_id = _resolve_google_oauth_inputs(
        client_id=client_id,
        client_secret=client_secret,
        calendar_id=calendar_id,
    )
    if not client_id:
        raise typer.BadParameter(
            "GOOGLE_CLIENT_ID must be set in .env or passed with --client-id."
        )
    if not client_secret:
        raise typer.BadParameter(
            "GOOGLE_CLIENT_SECRET must be set in .env or passed with --client-secret."
        )

    try:
        payload = run_local_google_oauth_flow(
            client_id=client_id,
            client_secret=client_secret,
            host=redirect_host,
            port=redirect_port,
            timeout_seconds=timeout_seconds,
            open_browser=open_browser,
        )
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    refresh_token = str(payload.get("refresh_token", "")).strip()
    if not refresh_token:
        typer.echo(
            "Error: Google did not return a refresh token. "
            "Remove the app from your Google account permissions and retry.",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo("Add these lines to .env:")
    typer.echo(format_google_env_snippet(refresh_token=refresh_token, calendar_id=calendar_id))


@app.command("preflight")
def preflight(target: Annotated[str, typer.Argument()] = "service") -> None:
    settings = get_settings()
    issues = collect_preflight_issues(settings, target=target)
    if issues:
        typer.echo(render_preflight_failure(issues))
        raise typer.Exit(code=1)
    typer.echo(f"Preflight passed for {target}.")


@app.command("rebuild-indexes")
def rebuild_indexes() -> None:
    storage, _, _ = _build_services()
    storage.rebuild_indexes()
    typer.echo("Rebuilt task and reminder indexes.")


@app.command("replay-reminders")
def replay_reminders() -> None:
    _, _, worker = _build_services()
    sent = asyncio.run(worker.replay_due_reminders())
    typer.echo(f"Sent {sent} reminder(s).")


async def _run_dev(*, host: str, port: int) -> None:
    storage, assistant, worker = _build_services()
    storage.rebuild_indexes()

    config = uvicorn.Config("personal_assistant.app:app", host=host, port=port, reload=False, access_log=False)
    server = uvicorn.Server(config)

    api_task = asyncio.create_task(server.serve())
    worker_task = asyncio.create_task(worker.run_forever())
    done, pending = await asyncio.wait(
        {api_task, worker_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in done:
        exc = task.exception()
        if exc is not None:
            server.should_exit = True
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            raise exc

    server.should_exit = True
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task
