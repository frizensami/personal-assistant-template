#!/usr/bin/env bash

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

run_uv run personal-assistant serve --host "${HOST}" --port "${PORT}"
