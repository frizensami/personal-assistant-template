#!/bin/sh

set -eu

if [ -z "${PUBLIC_BASE_URL:-}" ]; then
  echo "Caddy cannot start: PUBLIC_BASE_URL is missing from .env." >&2
  echo "Set PUBLIC_BASE_URL to your public HTTPS URL, for example https://apps.example.com." >&2
  exit 1
fi

case "${PUBLIC_BASE_URL}" in
  https://*)
    ;;
  *)
    echo "Caddy cannot start: PUBLIC_BASE_URL must begin with https://." >&2
    echo "Current value: ${PUBLIC_BASE_URL}" >&2
    exit 1
    ;;
esac

domain="${PUBLIC_BASE_URL#https://}"
domain="${domain%%/*}"

if [ -z "${domain}" ]; then
  echo "Caddy cannot start: could not extract a domain from PUBLIC_BASE_URL=${PUBLIC_BASE_URL}." >&2
  exit 1
fi

cat > /tmp/Caddyfile <<EOF
${domain} {
    handle /healthz {
        reverse_proxy api:8000
    }

    handle /telegram/webhook/* {
        reverse_proxy api:8000
    }

    handle {
        respond "Not Found" 404
    }
}
EOF

exec caddy run --config /tmp/Caddyfile --adapter caddyfile
