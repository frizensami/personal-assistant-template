# Security and privacy

This is a single-user self-hosted template. Use a positive Telegram allowed user ID and high-entropy webhook secret; missing allowlist configuration denies updates. Keep the internal API on loopback/private networks and use a separate read token.

Use independent accounts and credentials. Operational state, backups, prompts and logs are private. Git ignore rules do not sanitize an archive of a live deployment. Back up only to a private dedicated repository; never to the public code repository.

Review dependency updates and network access before deployment. Containers require access to the mounted application and data; host/container compromise may expose credentials. No claim of vulnerability-free software or exhaustive security certification is made.

Report suspected vulnerabilities privately to the repository owner before publishing credential material or personal data. Never include secrets in public issues.
