#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_UV_BIN="${HOME}/.local/bin/uv"

export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"

cd "${REPO_ROOT}"

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

resolve_uv_bin() {
  if [[ -n "${UV_BIN:-}" ]]; then
    printf '%s\n' "${UV_BIN}"
  elif [[ -x "${DEFAULT_UV_BIN}" ]]; then
    printf '%s\n' "${DEFAULT_UV_BIN}"
  else
    command -v uv 2>/dev/null || true
  fi
}

ensure_uv_installed() {
  local uv_bin
  uv_bin="$(resolve_uv_bin)"
  if [[ -n "${uv_bin}" ]]; then
    UV="${uv_bin}"
    return 0
  fi

  have_cmd curl || fail "curl is required to install uv."

  fail "uv is missing. Install a reviewed, pinned uv release before running this script."

  uv_bin="$(resolve_uv_bin)"
  [[ -n "${uv_bin}" ]] || fail "uv installation completed, but uv was not found on PATH."
  UV="${uv_bin}"
}

UV="$(resolve_uv_bin)"
if [[ -z "${UV}" ]]; then
  UV="uv"
fi

run_uv() {
  "${UV}" "$@"
}
