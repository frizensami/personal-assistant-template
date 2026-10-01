from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from personal_assistant.cli import _resolve_google_oauth_inputs
from personal_assistant.google_oauth import (
    GOOGLE_CALENDAR_SCOPE,
    build_google_auth_url,
    format_google_env_snippet,
    parse_google_callback_path,
)


def test_build_google_auth_url_requests_offline_calendar_access():
    url = build_google_auth_url(
        client_id="client-id",
        redirect_uri="http://127.0.0.1:8765/oauth2/callback",
        state="test-state",
    )

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert params["client_id"] == ["client-id"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8765/oauth2/callback"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == [GOOGLE_CALENDAR_SCOPE]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["state"] == ["test-state"]


def test_format_google_env_snippet_uses_primary_when_calendar_id_is_blank():
    snippet = format_google_env_snippet(refresh_token="refresh-token", calendar_id="  ")

    assert snippet == "GOOGLE_REFRESH_TOKEN=refresh-token\nCALENDAR_ID=primary"


def test_parse_google_callback_path_reads_code_and_error_fields():
    success = parse_google_callback_path("/oauth2/callback?code=abc123&state=xyz")
    failure = parse_google_callback_path("/oauth2/callback?error=access_denied&error_description=nope")

    assert success is not None
    assert success.code == "abc123"
    assert success.state == "xyz"

    assert failure is not None
    assert failure.error == "access_denied"
    assert failure.error_description == "nope"


def test_google_oauth_cli_inputs_fall_back_to_settings_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOGLE_CLIENT_ID=env-client\n"
        "GOOGLE_CLIENT_SECRET=env-secret\n"
        "CALENDAR_ID=work\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from personal_assistant.config import get_settings

    get_settings.cache_clear()
    try:
        client_id, client_secret, calendar_id = _resolve_google_oauth_inputs(
            client_id="",
            client_secret="",
            calendar_id="",
        )
    finally:
        get_settings.cache_clear()

    assert client_id == "env-client"
    assert client_secret == "env-secret"
    assert calendar_id == "work"
