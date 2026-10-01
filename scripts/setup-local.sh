#!/usr/bin/env bash

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

ensure_uv_installed

if [[ ! -f .env ]]; then
  cp .env.example .env
  log "Created .env from .env.example"
fi

run_uv sync --frozen --all-extras
run_uv run personal-assistant bootstrap

log "Local setup complete."
log "Next: edit .env if needed, then run ./scripts/dev.sh"
