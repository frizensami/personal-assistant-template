#!/usr/bin/env bash
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
: "${TELEGRAM_WEBHOOK_SECRET:?Set the webhook secret from your local .env}"
: "${TELEGRAM_ALLOWED_USER_ID:?Set the synthetic user ID from your local .env}"
export LOCAL_CHAT_ID="${LOCAL_CHAT_ID:-local-chat}"
export UPDATE_ID="${UPDATE_ID:-1}"
export SMOKE_MESSAGE="${1:-add task buy milk remind me in 10 minutes}"
python3 - <<'PYJSON' | curl --fail-with-body -X POST "http://127.0.0.1:${PORT:-8000}/telegram/webhook/${TELEGRAM_WEBHOOK_SECRET}" -H "content-type: application/json" -d @-
import json, os
print(json.dumps({"update_id": int(os.environ["UPDATE_ID"]), "message": {"chat": {"id": os.environ["LOCAL_CHAT_ID"], "type": "private"}, "from": {"id": int(os.environ["TELEGRAM_ALLOWED_USER_ID"])}, "text": os.environ["SMOKE_MESSAGE"]}}))
PYJSON
