#!/usr/bin/env bash
# Replace fieldstation42/src/ with an unmodified copy of FieldStation42 at a
# given ref, show what changed, and run the schema contract check.
#
# Usage: scripts/sync-fs42.sh [ref]
#   ref defaults to the commit recorded in fieldstation42/FS42_REF.
#
# Nothing in fieldstation42/src/ is ours to edit. Local changes there are
# overwritten on sync, by design -- that's what keeps updates a clean replace.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/fieldstation42/src"
PIN="$ROOT/fieldstation42/FS42_REF"
REPO=https://github.com/shane-mason/FieldStation42.git

REF="${1:-$(cat "$PIN")}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git init -q "$TMP/new"
git -C "$TMP/new" remote add origin "$REPO"
git -C "$TMP/new" fetch -q --depth 1 origin "$REF"
git -C "$TMP/new" checkout -q FETCH_HEAD
SHA="$(git -C "$TMP/new" rev-parse HEAD)"
rm -rf "$TMP/new/.git"

echo "Syncing FieldStation42 @ $SHA"
if [ -d "$DEST" ]; then
  echo "Changes against the current copy:"
  diff -rq "$DEST" "$TMP/new" || true
else
  echo "No existing copy; this is the first sync."
fi

rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$TMP/new/." "$DEST/"
echo "$SHA" > "$PIN"

echo
echo "Running contract check..."
python3 "$ROOT/scripts/check-fs42-contract.py" "$DEST"

echo
echo "Running generator tests (includes the station schema check)..."
if python3 -c "import defusedxml" 2>/dev/null; then
  python3 "$ROOT/scripts/test_build_channels.py" 2>&1 | tail -4
else
  echo "skipped: pip install defusedxml jsonschema, then run scripts/test_build_channels.py" >&2
fi
