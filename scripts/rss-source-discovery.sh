#!/bin/bash
# RSS Source Discovery — Claude Code session driver
# Cron: daily 03:30 BJT via cron-wrapper.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

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

echo "$LOG_PREFIX Starting RSS source discovery..."

# Use Max plan auth
unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

CATEGORIES_FILE="$REPO_DIR/config/rss-discovery-categories.json"
SOURCES_FILE="$REPO_DIR/news-sources-config.json"
HELPER="$REPO_DIR/rss-source-discovery.py"

if [ ! -f "$CATEGORIES_FILE" ]; then
    echo "$LOG_PREFIX ERROR: $CATEGORIES_FILE not found"
    exit 1
fi

if [ ! -f "$HELPER" ]; then
    echo "$LOG_PREFIX ERROR: $HELPER not found"
    exit 1
fi

EXISTING_COUNT=$(python3 -c "import json; d=json.load(open('$SOURCES_FILE')); print(sum(len(d['news_sources'][k]) for k in d['news_sources']))" 2>/dev/null || echo "?")

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

# RSS Source Discovery Task

You are an RSS feed discovery agent. Find high-quality RSS feeds across 8 categories, validate them, score them, and generate a report.

## Current State
- Existing sources: $EXISTING_COUNT
- Categories file: $CATEGORIES_FILE
- Helper script: $HELPER (subcommands: validate, dedup, save, report)
- Working directory: $REPO_DIR

## Steps

### Step 1: Read current state
Read these files to understand what exists:
- $CATEGORIES_FILE — 8 categories with search queries
- $SOURCES_FILE — current source pool (avoid duplicates)
- config/discovered-rss.json — prior candidates (avoid re-recommending rejected ones)

### Step 2: Discover candidates (dual-channel)
For EACH of the 8 categories in the categories file:
1. Use web search with the provided search queries to find RSS feed URLs
2. Extract actual feed URLs (look for URLs ending in /rss, /feed, .xml, or containing 'rss', 'feed', 'atom')
3. Collect candidates as JSON: {\"name\": \"...\", \"url\": \"...\", \"language\": \"en|cn\", \"category\": \"...\", \"discovered_via\": \"ai_search\"}

Also check the directory_urls in the categories file for curated RSS lists.

Target: 5-15 new candidates total across all categories.

### Step 3: Dedup
Write your candidates as a JSON array to /tmp/rss-raw-candidates.json, then:
\`\`\`bash
cat /tmp/rss-raw-candidates.json | python3 $HELPER dedup > /tmp/rss-deduped.json
echo \"Deduped: \$(python3 -c \"import json; print(len(json.load(open('/tmp/rss-deduped.json'))))\" ) candidates\"
\`\`\`

### Step 4: Validate
\`\`\`bash
cat /tmp/rss-deduped.json | python3 $HELPER validate > /tmp/rss-validated.json
\`\`\`

### Step 5: Score
For each validated candidate where parse_ok=true and article_count>0, you need to provide two AI judgments:
- **authority** (0.0-1.0): 0.9+ for major outlets (BBC, NYT, Reuters, FT), 0.7-0.8 for established publications, 0.5-0.6 for smaller/niche, 0.3 for unknown
- **uniqueness** (0.0-1.0): 0.9 if covers topic/region not in current pool, 0.5-0.7 if partially overlaps, 0.2-0.3 if heavily overlaps with existing sources

Then compute scores:
\`\`\`python
python3 << 'PYEOF'
import json, sys, os, importlib.util
spec = importlib.util.spec_from_file_location(\"m\", \"$HELPER\")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
candidates = json.load(open(\"/tmp/rss-validated.json\"))
scored = []
for c in candidates:
    v = c.get(\"validation\", {})
    if v.get(\"parse_ok\") and v.get(\"article_count\", 0) > 0:
        # YOU MUST REPLACE these with your actual authority/uniqueness judgments per feed:
        authority = 0.5  # REPLACE per candidate
        uniqueness = 0.5  # REPLACE per candidate
        c[\"scores\"] = m.compute_scores(v, authority=authority, uniqueness=uniqueness)
        scored.append(c)
json.dump(scored, open(\"/tmp/rss-scored.json\", \"w\"), indent=2, ensure_ascii=False)
print(f\"Scored {len(scored)} candidates\")
PYEOF
\`\`\`

IMPORTANT: Do NOT use the placeholder values above. For EACH candidate, assess authority and uniqueness based on your knowledge of the publication.

### Step 6: Save + Report
\`\`\`bash
cat /tmp/rss-scored.json | python3 $HELPER save
python3 $HELPER report
\`\`\`

### Step 7: Commit + Push
\`\`\`bash
cd $REPO_DIR
git add config/discovered-rss.json
git diff --cached --quiet || git commit -m \"data(discovery): update RSS candidates \$(TZ='Asia/Shanghai' date '+%Y-%m-%d')\"
\`\`\`

## Constraints
- Maximum 30 minutes for this session
- Target: 5-15 new candidates per run
- Only recommend feeds that parse successfully and have recent articles
- Do NOT modify news-sources-config.json (promotion is manual)
- Do NOT use placeholder authority/uniqueness scores — assess each feed individually
"

# 30-minute timeout + 30s grace
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
timeout --kill-after=30 1800 "$CLAUDE_BIN" -p --model sonnet "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX RSS discovery TIMED OUT after 30 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX RSS discovery failed (exit code: $EXIT_CODE)"
else
    echo "$LOG_PREFIX RSS discovery finished successfully"
fi

# Ensure any commits are pushed
cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$LOG_PREFIX Pushing new commits..."
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push failed"
fi
