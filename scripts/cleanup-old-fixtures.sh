#!/bin/bash
# Cleanup news fixtures older than 21 days.
# Fixtures are renewable RSS snapshots — only recent ones matter for evaluation.
# Safe to run via cron or manually; skips if nothing to clean.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
FIXTURES_DIR="$REPO_DIR/fixtures"
KEEP_DAYS="${1:-21}"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

if [ ! -d "$FIXTURES_DIR" ]; then
    echo "$LOG_PREFIX No fixtures directory at $FIXTURES_DIR"
    exit 0
fi

# Find fixture files older than KEEP_DAYS
CUTOFF_DATE=$(date -d "$KEEP_DAYS days ago" +%Y-%m-%d)
REMOVED=0

cd "$REPO_DIR"

for f in "$FIXTURES_DIR"/*.json; do
    [ -f "$f" ] || continue
    # Extract date from filename: 2026-03-27-15.json -> 2026-03-27
    BASENAME=$(basename "$f" .json)
    FILE_DATE="${BASENAME:0:10}"

    # Validate date format
    if ! [[ "$FILE_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        echo "$LOG_PREFIX SKIP: $BASENAME (unrecognized date format)"
        continue
    fi

    if [[ "$FILE_DATE" < "$CUTOFF_DATE" ]]; then
        if git ls-files --error-unmatch "$f" &>/dev/null; then
            if git rm -q "$f"; then
                echo "$LOG_PREFIX REMOVE: $BASENAME (git rm)"
                REMOVED=$((REMOVED + 1))
            else
                echo "$LOG_PREFIX ERROR: failed to git rm $BASENAME"
            fi
        else
            echo "$LOG_PREFIX SKIP: $BASENAME (not tracked by git)"
        fi
    fi
done

if [ "$REMOVED" -eq 0 ]; then
    echo "$LOG_PREFIX Nothing to clean (all fixtures within $KEEP_DAYS days)"
    exit 0
fi

echo "$LOG_PREFIX Removed $REMOVED fixture(s) older than $KEEP_DAYS days"

# Commit if there are staged changes
if ! git diff --cached --quiet 2>/dev/null; then
    git commit -m "chore: cleanup $REMOVED fixture(s) older than $KEEP_DAYS days"
    echo "$LOG_PREFIX Committed cleanup"
fi
