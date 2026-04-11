#!/bin/bash
# Cron wrapper for news digest autoresearch
# Runs 2-3 experiments per session, 20-minute timeout
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"
MIN_FIXTURES="${MIN_FIXTURES:-6}"

cleanup() {
    local pids
    pids=$(jobs -p 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$LOG_PREFIX Cleaning up child processes..."
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "$LOG_PREFIX Starting news autoresearch session..."

# Cleanup fixtures older than 21 days before running experiments
"$SCRIPT_DIR/cleanup-old-fixtures.sh" 21 2>&1 || echo "$LOG_PREFIX WARNING: fixture cleanup failed (non-fatal)"

FIXTURE_COUNT=$(find "$REPO_DIR/fixtures" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)
if [ "$FIXTURE_COUNT" -lt "$MIN_FIXTURES" ]; then
    echo "$LOG_PREFIX SKIP: only $FIXTURE_COUNT fixtures available; need at least $MIN_FIXTURES before running autoresearch"
    echo "$LOG_PREFIX Waiting for more fixture snapshots to avoid overfitting the post-reset cycle"
    exit 0
fi

# CRITICAL: Unset API key so Claude uses Max plan auth (not paid API)
unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

PROGRAM_MD="$REPO_DIR/autoresearch/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- You have a MAXIMUM of 20 minutes for this session
- Run 2-3 experiments only, then stop
- Current fixture pool is $FIXTURE_COUNT files; do not treat this as a mature benchmark
- If the latest row in autoresearch/results.tsv is BASELINE_RESET, first log one fresh BASELINE on the current fixture pool before trying experiments
- After all experiments, if any commits were kept, run: cd ~/global-news && git push
"

# 20-minute timeout + 30s grace
# Use full path to avoid cron picking up stale /usr/bin/claude
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
timeout --kill-after=30 1200 "$CLAUDE_BIN" -p --model sonnet "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX News autoresearch TIMED OUT after 20 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX News autoresearch failed (exit code: $EXIT_CODE)"
else
    echo "$LOG_PREFIX News autoresearch finished successfully"
fi

# Push any kept commits
cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$LOG_PREFIX Pushing new commits..."
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push failed"
else
    echo "$LOG_PREFIX No new commits to push"
fi

# Sync experiment history to docs page
echo "$LOG_PREFIX Syncing experiment history to autoresearch.html..."
python3 /home/ubuntu/infra-scripts/sync-ar-history.py news "$REPO_DIR/autoresearch/results.tsv" Quality 2>&1 || echo "$LOG_PREFIX WARNING: history sync failed (non-fatal)"
