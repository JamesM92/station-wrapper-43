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
- **The services do not run as root.** The container starts as root only to hand `/config` and the catalog to a user and drop to
  it (`PUID` / `PGID`; unset, they are whoever owns your config folder, or 1000:1000 for a new one). The console, dashboard,
  sync and TubeArchivist jobs all run as that user, and what they write in your volumes is theirs. To never be root at all, start
  the container as that user (`user: "1000:1000"` in compose) with writable volumes. `PUID=0` keeps root and is not recommended.
  With `cap_drop: [ALL]` add back `CHOWN`, `SETUID`, `SETGID` (and `DAC_OVERRIDE` if the volumes are not yours), which the drop needs.
- **The services' signal files are in a private folder** (`/run/fs42`, mode 0700, that user only), not in `/tmp`.
- **Source addresses with a password** (a camera's address with a user name and password in it) are kept in `config/tuner/sources.json` and are never
  shown again in the pages. Keep backups of the config folder private.

## What is checked on every change

Static analysis of our code (bandit), an audit of the pinned dependencies (pip-audit), a secret scan against a reviewed baseline
(detect-secrets) and a repository scan (Trivy: dependencies, secrets, the Dockerfile) run on each push and weekly. See
`.github/workflows/security.yml`.
