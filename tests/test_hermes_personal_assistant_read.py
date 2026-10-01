from __future__ import annotations

import json
import subprocess
import sys

import pytest

from personal_assistant.hermes_read_client import (
    MAX_RESPONSE_BYTES,
    SSH_HOST,
    _run_bounded,
    fetch_read_view,
)


def _completed(stdout: bytes, *, returncode: int = 0, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_fetch_read_view_uses_fixed_remote_route_and_parses_json() -> None:
    calls: list[tuple[list[str], float, int]] = []

    def runner(
        argv: list[str], *, timeout: float, max_stdout_bytes: int
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, timeout, max_stdout_bytes))
        return _completed(b'{"status":"ok"}\n')

    assert fetch_read_view("status", runner=runner) == {"status": "ok"}
    assert calls == [
        (
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                SSH_HOST,
                "cd /srv/personal-assistant && docker compose exec -T api "
                "python -c 'import http.client,os,sys; "
                'connection=http.client.HTTPConnection("127.0.0.1",8000,timeout=5); '
                'connection.request("GET","/internal/v1/status",'
                'headers={"Authorization":"Bearer "+os.environ["OPENCLAW_API_TOKEN"]}); '
                "response=connection.getresponse(); response.status==200 or sys.exit(1); "
                "sys.stdout.buffer.write(response.read(262145))'",
            ],
            15.0,
            MAX_RESPONSE_BYTES,
        )
    ]


def test_fetch_read_view_rejects_unknown_views_before_running_ssh() -> None:
    called = False

    def runner(argv: list[str], *, timeout: float, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        return _completed(b"{}")

    with pytest.raises(ValueError, match="Unsupported Personal Assistant view"):
        fetch_read_view("../../secrets", runner=runner)
    assert called is False


def test_fetch_read_view_redacts_remote_errors() -> None:
    def runner(argv: list[str], *, timeout: float, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return _completed(b"", returncode=1, stderr=b"remote leaked OPENCLAW_API_TOKEN=secret")

    with pytest.raises(RuntimeError, match="SSH request failed with exit code 1") as caught:
        fetch_read_view("tasks", runner=runner)
    assert "secret" not in str(caught.value)


def test_fetch_read_view_rejects_oversized_or_invalid_json() -> None:
    def oversized(argv: list[str], *, timeout: float, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return _completed(b"x" * (MAX_RESPONSE_BYTES + 1))

    with pytest.raises(RuntimeError, match="response exceeded"):
        fetch_read_view("reminders", runner=oversized)

    def invalid(argv: list[str], *, timeout: float, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return _completed(b"not-json")

    with pytest.raises(RuntimeError, match="invalid JSON"):
        fetch_read_view("reminders", runner=invalid)


def test_bounded_runner_accepts_exact_limit_and_rejects_one_extra_byte() -> None:
    exact = _run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 4096)"],
        timeout=5,
        max_stdout_bytes=4096,
    )
    assert len(exact.stdout) == 4096

    with pytest.raises(RuntimeError, match="response exceeded"):
        _run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 4097)"],
            timeout=5,
            max_stdout_bytes=4096,
        )


def test_bounded_runner_discards_excess_stderr() -> None:
    completed = _run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.buffer.write(b'secret' * 100000); sys.stdout.write('{}')",
        ],
        timeout=5,
        max_stdout_bytes=4096,
    )
    assert completed.stdout == b"{}"
    assert completed.stderr is None


def test_fetch_read_view_reports_timeout_without_command_details() -> None:
    def runner(argv: list[str], *, timeout: float, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, timeout, output=b"", stderr=b"token=secret")

    with pytest.raises(RuntimeError, match="timed out") as caught:
        fetch_read_view("status", runner=runner)
    assert "secret" not in str(caught.value)
