# Security

## Reporting a problem

Please report a vulnerability privately, not in a public issue: open a security advisory on the repository (or contact the
maintainer through the repository's profile). Include what you saw, how to reproduce it, and the version (the Status page shows it).
You will get an answer within a few days.

## What to know before exposing it

- **The dashboard has a login**, set up the first time it is opened (`DASHBOARD_LOGIN=1`, the default). The stream, guide and
  `/lineup.json` addresses are open on purpose: IPTV clients can't log in. Keep port 5004 on your own network, or put it behind a
  reverse proxy with its own authentication.
- **Do not publish ports 5004 or 4242 to the internet.** 4242 is FieldStation42's own console and has no login.
- **Your media is mounted read-only** and nothing is ever written there. Everything the wrapper writes lives in the `/config` and
  catalog volumes.
- **The container runs its services as root** so the files it writes in your volumes belong to you. Limit it with `--cap-drop ALL`,
  `no-new-privileges`, and read-only media mounts (the compose file shows them).
- **Source addresses with a password** (a camera's address with a user name and password in it) are kept in `config/tuner/sources.json` and are never
  shown again in the pages. Keep backups of the config folder private.

## What is checked on every change

Static analysis of our code (bandit), an audit of the pinned dependencies (pip-audit), a secret scan against a reviewed baseline
(detect-secrets) and a repository scan (Trivy: dependencies, secrets, the Dockerfile) run on each push and weekly. See
`.github/workflows/security.yml`.
