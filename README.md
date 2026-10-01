# Personal Assistant Template

A single-user Telegram assistant built with Python, FastAPI and Docker Compose. It stores tasks, reminders, notes and preferences in files. Google Calendar, OpenAI, Git backup and read-only agent integrations are optional.

This template contains application code and synthetic tests only. Startup creates an empty state tree. Supply your own accounts and secrets. No hosted service or automatic deployment is included.

## Deploy your own instance

1. Install Docker Engine with the Compose plugin. Clone this repository, then copy `.env.example` to `.env`.
2. Create your own Telegram bot and set `TELEGRAM_BOT_TOKEN`. Set `TELEGRAM_ALLOWED_USER_ID` to your own numeric user ID. Generate a long random `TELEGRAM_WEBHOOK_SECRET`, for example with `openssl rand -hex 32`.
3. Set `PUBLIC_BASE_URL=https://assistant.example.com` to your actual domain. Point DNS to your server and permit inbound TCP 80/443. Set `DEFAULT_TIMEZONE` to your timezone.
4. Set `STATE_HOST_DIR` and `RUNTIME_HOST_DIR` to empty persistent directories owned by your deployment. Defaults are `./data/state` and `./data/runtime`, both ignored by Git. Never reuse somebody else's state.
5. Run `docker compose up -d --build`. Startup preflight rejects missing required configuration. Register the webhook with `docker compose run --rm api personal-assistant register-webhook`.

Caddy exposes only health checks and the authenticated Telegram webhook. FastAPI is published on host loopback; internal read routes require a separate bearer token and are excluded from the public proxy. This is a single-user deployment, without multi-user data isolation. Never expose the internal service directly on a public interface.

Webhook access logging is disabled because its URL contains a secret. Treat all logs as potentially sensitive. Keep `.env`, deploy keys, live data, AI traces, database exports and backups private; never include them in a repository or folder archive.

## Optional integrations

- Google Calendar: create your own OAuth client, configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REFRESH_TOKEN`. The helper `scripts/google-oauth-refresh-token.sh` obtains a token locally and prints it; protect terminal output. Google grants broad Calendar permissions even though the assistant restricts edits to its own events.
- OpenAI: set your own `OPENAI_API_KEY` for explicit `ai ...` requests. Requests can send local context to OpenAI and retain prompts in private state. `OPENAI_ADMIN_API_KEY` is optional organization-level usage access; leave it blank unless needed.
- Encrypted people entries: set your own strong `PEOPLE_ENCRYPTION_KEY`. Encryption does not make state suitable for publication.
- Manual sync: set your own `OPERATOR_PASSWORD`.
- Read API: set a high-entropy `OPENCLAW_API_TOKEN` and use a private connection. See `integrations/` for optional clients. Hermes uses the generic `assistant-host` SSH alias and `/srv/personal-assistant` path; adapt these to your own host. Never request or reuse another operator's SSH/tailnet credentials.

## Private backups

Backups are disabled by default. To enable them, create a separate **private** state repository you own and add its SSH remote as `state-origin`. Configure a write-scoped deploy key in the ignored `.github-deploy-key` file with mode 600 and verified GitHub host keys in `.github-known-hosts`. Set `GIT_BACKUP_ENABLED=true` and retain `GIT_BACKUP_MODE=dedicated_state_repo`.

Never target this public code repository as a state backup. The template rejects the legacy `same_repo` mode. The backup remote contains operational data, including prompts and chat metadata, and must remain private.

## Local development and checks

Install a reviewed pinned uv release; development scripts never download and execute an installer automatically.

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest
uv build
```

Tests use synthetic temporary state and fake API clients. Some backup tests create local temporary Git repositories. No external bot credentials are required.

For a local Docker smoke test, use a separate `.env` with an arbitrary synthetic allowed user ID, a random webhook secret, blank bot token and `GIT_BACKUP_ENABLED=false`, then run:

```bash
docker compose -f compose.yaml -f compose.local.yaml up --build api worker
TELEGRAM_WEBHOOK_SECRET=<your-local-secret> TELEGRAM_ALLOWED_USER_ID=<synthetic-id> ./scripts/smoke-test.sh "add task buy milk remind me in 10 minutes"
```

State and runtime remain in ignored data directories. Commands include `tasks`, `task <title>`, `reminders`, `remind me ...`, `agenda`, `note <title>`, `preference <title>`, `people`, `help`, and explicit `ai <request>`.

## Licensing

Application reuse licensing is pending an owner decision. Public visibility alone does not grant general reuse or redistribution permission. The OpenClaw integration's existing MIT license remains in its own directory. Dependencies retain their respective licenses; they are downloaded during installation rather than vendored here.
