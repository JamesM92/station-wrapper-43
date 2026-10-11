#!/usr/bin/env bash
# Opt in to running scripts/preflight.sh before every `git push`.
# Skip once with `git push --no-verify`. Remove with: rm .git/hooks/pre-push
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cat > "$ROOT/.git/hooks/pre-push" <<'HOOK'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/preflight.sh"
HOOK
chmod +x "$ROOT/.git/hooks/pre-push"
echo "installed .git/hooks/pre-push"
