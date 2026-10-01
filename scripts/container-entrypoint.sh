#!/usr/bin/env bash

set -euo pipefail

TARGET="${PA_TARGET:-service}"

printf 'Running preflight for %s\n' "${TARGET}"
personal-assistant preflight "${TARGET}"

printf 'Bootstrapping state tree\n'
personal-assistant bootstrap

exec "$@"
