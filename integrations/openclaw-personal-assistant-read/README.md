# OpenClaw Personal Assistant Read

A minimal OpenClaw tool-only plugin that grants an agent three narrow, read-only views of a Personal Assistant API.

## Tools and fixed HTTP contracts

| Tool | Request |
| --- | --- |
| `personal_assistant_status` | `GET /internal/v1/status` |
| `personal_assistant_active_tasks` | `GET /internal/v1/tasks` |
| `personal_assistant_scheduled_reminders` | `GET /internal/v1/reminders` |

All tools have a closed, empty parameter schema. The agent cannot supply a URL, path, query, method, body, or headers. Each request sends only `Accept: application/json` and `Authorization: Bearer <token>`.

## Security properties

- The bearer token is a manifest-declared `secretInputs` field and supports OpenClaw `SecretRef` values, including the shared `store` provider. Runtime code receives the resolved string and never logs it.
- `baseUrl` is non-secret and must be an origin only (no credentials, path, query, or fragment).
- HTTPS is mandatory. Plain HTTP is accepted only for literal loopback names/addresses (`localhost`, `*.localhost`, `127.0.0.0/8`, or `::1`) to support local development.
- Redirect following is disabled, preventing bearer-token forwarding to another origin.
- Requests time out after 5 seconds, including response-body reads.
- Responses are limited to 256 KiB and must be valid JSON.
- Non-success responses expose only the HTTP status, not the upstream body.

This plugin does not create, update, complete, delete, or execute tasks/reminders. API-side authorization should still scope its token to these three GET routes.

## Requirements

- OpenClaw 2026.9.4 or compatible (`>=2026.5.17` plugin API)
- Node.js 24.16+ or Node.js 26.1+

## Build and verify

```bash
npm install
npm run plugin:build
npm run plugin:validate
npm test
npm run verify
```

`plugin:build` compiles TypeScript and regenerates the OpenClaw manifest metadata. `verify` typechecks, checks that generated metadata is current, validates the runtime entry, and runs the tests.

## Install and configure

These commands are intentionally documentation only; this repository does not install or configure itself.

```bash
cd /path/to/openclaw-personal-assistant-read
npm install
npm run plugin:build
openclaw plugins install /path/to/openclaw-personal-assistant-read --force
```

Store the API token through a masked prompt or safe stdin (never put it in shell history), then bind it to the exact private API host:

```bash
openclaw secrets store set OPENCLAW_PA_API_TOKEN \
  --kind secret \
  --allow-host assistant.example.com
```

When this host is behind the network that blocks Tailscale control, install the supplied user services. They keep a narrow SSH SOCKS proxy and userspace Tailscale client running; the plugin uses only the client's loopback HTTP proxy:

```bash
install -Dm644 systemd/user/openclaw-tailnet-proxy.service ~/.config/systemd/user/openclaw-tailnet-proxy.service
install -Dm644 systemd/user/openclaw-userspace-tailscale.service ~/.config/systemd/user/openclaw-userspace-tailscale.service
systemctl --user daemon-reload
systemctl --user enable --now openclaw-tailnet-proxy.service openclaw-userspace-tailscale.service
```

Configure the plugin atomically with the protected store `SecretRef`:

```bash
openclaw config set plugins.entries.openclaw-personal-assistant-read '{"enabled":true,"config":{"baseUrl":"https://assistant.example.com","proxyUrl":"http://127.0.0.1:11056","token":{"source":"store","provider":"default","id":"OPENCLAW_PA_API_TOKEN"}}}' --strict-json
openclaw secrets reload
openclaw plugins inspect openclaw-personal-assistant-read --runtime
```

`proxyUrl`, when configured, must be an HTTP loopback origin. Use `http://127.0.0.1:<port>` for `baseUrl` only when the API itself is on the same host. No OpenClaw configuration is changed by build or test commands.

## Development

```bash
npm test
npm run build
npm run plugin:build
npm run plugin:validate
```

Tests exercise fixed routes and GET semantics against a loopback HTTP server, HTTPS/loopback URL policy, bearer authentication, timeout behavior, response-size enforcement, redacted errors, and generated tool metadata.
