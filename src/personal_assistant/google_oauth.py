from __future__ import annotations

import contextlib
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from typing import Any, Sequence
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GOOGLE_CALLBACK_PATH = "/oauth2/callback"


@dataclass(frozen=True)
class OAuthCallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None


def build_google_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: Sequence[str] = (GOOGLE_CALENDAR_SCOPE,),
) -> str:
    return f"{GOOGLE_AUTH_URL}?{urlencode(_build_google_auth_params(client_id=client_id, redirect_uri=redirect_uri, state=state, scopes=scopes))}"


def format_google_env_snippet(*, refresh_token: str, calendar_id: str) -> str:
    calendar_value = calendar_id.strip() or "primary"
    return (
        f"GOOGLE_REFRESH_TOKEN={refresh_token}\n"
        f"CALENDAR_ID={calendar_value}"
    )


def run_local_google_oauth_flow(
    *,
    client_id: str,
    client_secret: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    timeout_seconds: int = 180,
    open_browser: bool = True,
    scopes: Sequence[str] = (GOOGLE_CALENDAR_SCOPE,),
) -> dict[str, Any]:
    redirect_uri = f"http://{host}:{port}{GOOGLE_CALLBACK_PATH}"
    state = secrets.token_urlsafe(24)
    callback_queue: Queue[OAuthCallbackResult] = Queue(maxsize=1)

    try:
        server = _OAuthCallbackServer((host, port), callback_queue)
    except OSError as exc:
        raise RuntimeError(
            f"Could not bind the local OAuth callback server on {host}:{port}: {exc}."
        ) from exc

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        auth_url = build_google_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
        )
        print("Open this URL to authorize Google Calendar access:")
        print(auth_url)
        if open_browser and not webbrowser.open(auth_url):
            print("Browser launch was not available. Open the URL manually.")
        elif not open_browser:
            print("Browser launch is disabled. Open the URL manually.")

        try:
            callback = callback_queue.get(timeout=timeout_seconds)
        except Empty as exc:
            raise RuntimeError(
                "Timed out waiting for the Google OAuth callback. "
                "Retry and complete the consent screen in the opened browser."
            ) from exc

        if callback.state != state:
            raise RuntimeError("Google OAuth state verification failed.")
        if callback.error:
            detail = f": {callback.error_description}" if callback.error_description else ""
            raise RuntimeError(f"Google OAuth returned {callback.error}{detail}")
        if not callback.code:
            raise RuntimeError("Google OAuth callback did not include an authorization code.")

        return exchange_google_authorization_code(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            code=callback.code,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def exchange_google_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    try:
        response = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Google token exchange failed: {exc}") from exc
    return response.json()


def parse_google_callback_path(path: str) -> OAuthCallbackResult | None:
    parsed = urlparse(path)
    if parsed.path != GOOGLE_CALLBACK_PATH:
        return None
    params = parse_qs(parsed.query)
    return OAuthCallbackResult(
        code=_first(params.get("code")),
        state=_first(params.get("state")),
        error=_first(params.get("error")),
        error_description=_first(params.get("error_description")),
    )


def _build_google_auth_params(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: Sequence[str],
) -> dict[str, str]:
    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]


class _OAuthCallbackServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], callback_queue: Queue[OAuthCallbackResult]) -> None:
        super().__init__(server_address, _OAuthCallbackHandler)
        self.callback_queue = callback_queue


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        result = parse_google_callback_path(self.path)
        if result is None:
            self.send_error(404, "Not found")
            return

        with contextlib.suppress(Exception):
            self.server.callback_queue.put_nowait(result)  # type: ignore[attr-defined]

        if result.error:
            message = "Google returned an error. You can close this window and return to the terminal."
        else:
            message = "Authorization received. You can close this window and return to the terminal."
        body = (
            "<html><body><h1>Personal Assistant</h1>"
            f"<p>{message}</p>"
            "</body></html>"
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return
