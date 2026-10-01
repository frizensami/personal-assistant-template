# Repository guidance

Deployment uses Docker Compose. Operational data belongs in ignored external state/runtime directories. Never add live tasks, messages, summaries, AI traces, identifiers, credentials, logs, keys or backups to this public source repository.

Backups are disabled by default. If enabled, use a recipient-owned private dedicated state repository; never back up state to the public code remote. Do not bake `.env` or SSH keys into images. Caddy exposes only `/healthz` and `/telegram/webhook/*`; internal read endpoints require separate bearer authentication and private reachability.

Run `uv run --frozen --extra dev pytest` after behavior changes. Use only synthetic fixtures. Do not perform live external integrations in tests. Preserve existing third-party notices and do not invent licensing authority.
