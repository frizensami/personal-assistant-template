from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Callable
from typing import Any

SSH_HOST = "assistant-host"
REMOTE_REPO = "/srv/personal-assistant"
REQUEST_TIMEOUT_SECONDS = 5
SSH_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 256 * 1024

_API_PATHS = {
    "status": "/internal/v1/status",
    "tasks": "/internal/v1/tasks",
    "reminders": "/internal/v1/reminders",
}

Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _run_bounded(
    argv: list[str], *, timeout: float, max_stdout_bytes: int
) -> subprocess.CompletedProcess[bytes]:
    """Run a process while bounding captured output and discarding stderr."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
        process.kill()
        raise RuntimeError("Personal Assistant SSH request could not capture output.")

    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _mask in events:
                chunk = os.read(key.fd, min(64 * 1024, max_stdout_bytes + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > max_stdout_bytes:
                    raise RuntimeError(
                        f"Personal Assistant response exceeded the {max_stdout_bytes}-byte limit."
                    )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(argv, returncode, bytes(output), None)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()


def _remote_command(path: str) -> str:
    python = (
        "import http.client,os,sys; "
        f'connection=http.client.HTTPConnection("127.0.0.1",8000,timeout={REQUEST_TIMEOUT_SECONDS}); '
        f'connection.request("GET","{path}",'
        'headers={"Authorization":"Bearer "+os.environ["OPENCLAW_API_TOKEN"]}); '
        "response=connection.getresponse(); response.status==200 or sys.exit(1); "
        f"sys.stdout.buffer.write(response.read({MAX_RESPONSE_BYTES + 1}))"
    )
    return f"cd {REMOTE_REPO} && docker compose exec -T api python -c '{python}'"


def fetch_read_view(view: str, *, runner: Runner = _run_bounded) -> Any:
    """Fetch one fixed read-only API view through the configured SSH host."""
    try:
        path = _API_PATHS[view]
    except KeyError:
        raise ValueError(f"Unsupported Personal Assistant view: {view}") from None

    argv = ["ssh", "-o", "BatchMode=yes", SSH_HOST, _remote_command(path)]
    try:
        completed = runner(
            argv,
            timeout=SSH_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_RESPONSE_BYTES,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Personal Assistant SSH request timed out.") from None
    except OSError:
        raise RuntimeError("Personal Assistant SSH request could not start.") from None

    if completed.returncode != 0:
        raise RuntimeError(f"Personal Assistant SSH request failed with exit code {completed.returncode}.")
    if len(completed.stdout) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"Personal Assistant response exceeded the {MAX_RESPONSE_BYTES}-byte limit.")

    try:
        return json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Personal Assistant returned invalid JSON.") from None
