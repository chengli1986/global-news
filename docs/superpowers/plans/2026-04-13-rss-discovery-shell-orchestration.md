# RSS Discovery Shell Orchestration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all deterministic side effects (save, commit, push, report email) out of the Claude Code AI session and into the shell script, with artifact validation gates.

**Architecture:** The shell script becomes a two-phase orchestrator: Phase 1 runs Claude Code to produce a scored JSON artifact; Phase 2 validates the artifact and executes save → git → report → trial-manager in deterministic shell steps. AI never decides whether to send email.

**Tech Stack:** Bash 5.2, Python 3.12 (existing `rss-source-discovery.py` subcommands)

**Spec:** `docs/superpowers/specs/2026-04-13-rss-discovery-shell-orchestration-design.md`

---

## File Structure

Only one file is modified:

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/rss-source-discovery.sh` | Modify | Orchestration: AI session + post-AI save/git/report/trial |

No new files. No changes to `rss-source-discovery.py` or `rss-trial-manager.py`.

---

### Task 1: PID-scoped temp files and cleanup

**Files:**
- Modify: `scripts/rss-source-discovery.sh:1-20` (variables and cleanup trap)

- [ ] **Step 1: Add PID-scoped temp file variables after line 8 (LOG_PREFIX)**

Replace the current variable block (lines 6-8) and cleanup function (lines 10-19) with:

```bash
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

# Absolute paths — single source of truth
CATEGORIES_FILE="$REPO_DIR/config/rss-discovery-categories.json"
SOURCES_FILE="$REPO_DIR/news-sources-config.json"
CANDIDATES_FILE="$REPO_DIR/config/discovered-rss.json"
HELPER="$REPO_DIR/rss-source-discovery.py"
TRIAL_MANAGER="$REPO_DIR/rss-trial-manager.py"
ENV_FILE="$HOME/.stock-monitor.env"

# PID-scoped temp files to prevent concurrent-run collisions
RAW_JSON="/tmp/rss-raw-candidates.$$.json"
DEDUPED_JSON="/tmp/rss-deduped.$$.json"
VALIDATED_JSON="/tmp/rss-validated.$$.json"
SCORED_JSON="/tmp/rss-scored.$$.json"

cleanup() {
    local pids
    pids=$(jobs -p 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$LOG_PREFIX Cleaning up child processes..."
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
    rm -f "$RAW_JSON" "$DEDUPED_JSON" "$VALIDATED_JSON" "$SCORED_JSON"
}
trap cleanup EXIT
```

This replaces the old lines 6-19 which defined `SCRIPT_DIR`, `REPO_DIR`, `LOG_PREFIX`, and `cleanup()` without temp file scoping or centralized path variables.

- [ ] **Step 2: Remove the old standalone variable definitions (lines 30-32)**

The old lines:
```bash
CATEGORIES_FILE="$REPO_DIR/config/rss-discovery-categories.json"
SOURCES_FILE="$REPO_DIR/news-sources-config.json"
HELPER="$REPO_DIR/rss-source-discovery.py"
```

These are now defined in the top variable block. Delete them.

- [ ] **Step 3: Verify syntax**

Run: `bash -n scripts/rss-source-discovery.sh`
Expected: no output (clean parse)

- [ ] **Step 4: Commit**

```bash
git add scripts/rss-source-discovery.sh
git commit -m "refactor(discovery): centralize path vars + PID-scoped temp files"
```

---

### Task 2: Trim the Claude Code prompt to Steps 1-5 only

**Files:**
- Modify: `scripts/rss-source-discovery.sh:46-152` (the PROMPT variable)

- [ ] **Step 1: Rewrite the PROMPT variable**

Replace the entire `PROMPT="..."` block (lines 46-152) with the following. Key changes:
- All temp file paths use the shell variables (`$RAW_JSON`, `$DEDUPED_JSON`, `$VALIDATED_JSON`, `$SCORED_JSON`)
- All config paths use shell variables (`$SOURCES_FILE`, `$CATEGORIES_FILE`, `$CANDIDATES_FILE`)
- Steps 6 (Save + Report) and 7 (Commit + Push) are removed entirely
- A clear final instruction tells the AI to write output to `$SCORED_JSON` and stop

```bash
PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

# RSS Source Discovery Task

You are an RSS feed discovery agent. Find high-quality RSS feeds across 8 categories, validate them, and score them.

## Current State
- Existing sources: $EXISTING_COUNT
- Categories file: $CATEGORIES_FILE
- Helper script: $HELPER (subcommands: validate, dedup)
- Working directory: $REPO_DIR

## Steps

### Step 1: Read current state
Read these files to understand what exists:
- $CATEGORIES_FILE — 8 categories with search queries
- $SOURCES_FILE — current source pool (avoid duplicates)
- $CANDIDATES_FILE — prior candidates (avoid re-recommending rejected ones)

### Step 2: Discover candidates (dual-channel)
For EACH of the 8 categories in the categories file:
1. Use web search with the provided search queries to find RSS feed URLs
2. Extract actual feed URLs (look for URLs ending in /rss, /feed, .xml, or containing 'rss', 'feed', 'atom')
3. Collect candidates as JSON: {\"name\": \"...\", \"url\": \"...\", \"language\": \"en|cn\", \"category\": \"...\", \"discovered_via\": \"ai_search\"}

Also check the directory_urls in the categories file for curated RSS lists.

Target: 5-15 new candidates total across all categories.

### Step 3: Dedup
Write your candidates as a JSON array to $RAW_JSON, then:
\`\`\`bash
cat $RAW_JSON | python3 $HELPER dedup > $DEDUPED_JSON
echo \"Deduped: \$(python3 -c \"import json; print(len(json.load(open('$DEDUPED_JSON'))))\" ) candidates\"
\`\`\`

### Step 4: Validate
\`\`\`bash
cat $DEDUPED_JSON | python3 $HELPER validate > $VALIDATED_JSON
\`\`\`

### Step 5: Score
For each validated candidate where parse_ok=true and article_count>0, you need to provide two AI judgments:

- **authority** (0.0-1.0): Editorial credibility and brand recognition within its coverage domain.
  - 0.90-1.0: Major international outlets — BBC, Reuters, AP, NYT, FT, The Guardian, Bloomberg, Economist, WSJ
  - 0.85-0.89: Strong regional or specialty outlets — SCMP, Foreign Policy, The Diplomat, IEEE Spectrum, Nature, Science, ProPublica, NPR
  - 0.75-0.84: Established mid-tier outlets — Axios, Politico, The Atlantic, France24, RFI, CNA, PBS NewsHour
  - 0.65-0.74: Respected niche outlets — The Register, Ars Technica, VentureBeat, STAT News, Endpoints News
  - Chinese-language reference: 财新/南方周末 = 0.85+; 36氪/虎嗅/澎湃 = 0.75; IT之家/少数派 = 0.65
  - 0.50-0.64: Smaller niche or regional publications with limited broader recognition
  - 0.30-0.49: Unknown / unverifiable sources
  - **Paywall note**: Heavy paywalls (New Yorker, Foreign Policy, IEEE Spectrum) do NOT reduce authority — authority reflects editorial quality. Content depth is captured separately via avg_description_length.

- **uniqueness** (0.0-1.0): How much NEW coverage this source adds vs the existing pool.
  - 0.90: Covers a region/topic/language entirely absent from current pool
  - 0.70-0.85: Meaningful differentiation — different editorial angle, unique specialisation, underrepresented region
  - 0.50-0.65: Partial overlap — similar topic area but different outlet or perspective
  - 0.20-0.40: Heavy overlap — same region AND same topic as 2+ existing sources
  - **0.10-0.20: Sub-feed of an existing outlet** — HARD PENALTY. Apply when the candidate is a section/topic feed from an outlet already in the pool (e.g., BBC Technology when BBC World/BBC Business already exist; Bloomberg Markets when Bloomberg already exists; NYT Science when NYT Business already exists). The parent outlet already covers the same editorial voice.
  - **0.15-0.25: Same media group, different brand** — e.g., Guardian US when Guardian World exists; CNBC Asia when CNBC already exists.
  - **Rule**: Always check $SOURCES_FILE (existing pool) before scoring. If the candidate shares a root domain or parent brand with an existing source, apply the sub-feed penalty unless it covers a clearly distinct language or region not represented.

Then compute scores:
\`\`\`python
python3 << 'PYEOF'
import json, sys, os, importlib.util
spec = importlib.util.spec_from_file_location(\"m\", \"$HELPER\")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
candidates = json.load(open(\"$VALIDATED_JSON\"))
scored = []
for c in candidates:
    v = c.get(\"validation\", {})
    if v.get(\"parse_ok\") and v.get(\"article_count\", 0) > 0:
        # YOU MUST REPLACE these with your actual authority/uniqueness judgments per feed:
        authority = 0.5  # REPLACE per candidate
        uniqueness = 0.5  # REPLACE per candidate
        c[\"scores\"] = m.compute_scores(v, authority=authority, uniqueness=uniqueness)
        scored.append(c)
json.dump(scored, open(\"$SCORED_JSON\", \"w\"), indent=2, ensure_ascii=False)
print(f\"Scored {len(scored)} candidates\")
PYEOF
\`\`\`

IMPORTANT: Do NOT use the placeholder values above. For EACH candidate, assess authority and uniqueness based on your knowledge of the publication.

### Done
After Step 5, your work is complete. Do NOT run save, commit, push, or report — the orchestration shell handles those.

## Constraints
- Maximum 30 minutes for this session
- Target: 5-15 new candidates per run
- Only recommend feeds that parse successfully and have recent articles
- Do NOT modify $SOURCES_FILE (promotion is handled by the trial manager)
- Do NOT use placeholder authority/uniqueness scores — assess each feed individually
"
```

- [ ] **Step 2: Verify syntax**

Run: `bash -n scripts/rss-source-discovery.sh`
Expected: no output (clean parse)

- [ ] **Step 3: Commit**

```bash
git add scripts/rss-source-discovery.sh
git commit -m "refactor(discovery): trim AI prompt to Steps 1-5 only

Save/commit/push/report removed from prompt — shell handles them."
```

---

### Task 3: Add artifact validation function

**Files:**
- Modify: `scripts/rss-source-discovery.sh` (add after the cleanup trap, before `echo "$LOG_PREFIX Starting..."`)

- [ ] **Step 1: Add the validation helper function**

Insert after the `trap cleanup EXIT` line and before `echo "$LOG_PREFIX Starting RSS source discovery..."`:

```bash
validate_json_array() {
    local f="$1"
    [ -s "$f" ] && python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
assert isinstance(d, list) and len(d) > 0
" "$f" 2>/dev/null
}
```

- [ ] **Step 2: Verify syntax**

Run: `bash -n scripts/rss-source-discovery.sh`
Expected: no output

- [ ] **Step 3: Commit**

```bash
git add scripts/rss-source-discovery.sh
git commit -m "feat(discovery): add JSON array validation helper"
```

---

### Task 4: Replace post-AI logic with deterministic orchestration

This is the core change. Replace everything after `timeout ... "$CLAUDE_BIN"` (the current lines 158-194 handling exit code, push, and trial manager) with the new deterministic flow.

**Files:**
- Modify: `scripts/rss-source-discovery.sh:155-194` (everything after the Claude Code invocation)

- [ ] **Step 1: Replace the post-AI block**

Remove everything from `EXIT_CODE=$?` (line 158) through end of file (line 194). Replace with:

```bash
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX RSS discovery TIMED OUT after 30 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX RSS discovery failed (exit code: $EXIT_CODE)"
fi

# ============================================================
# Phase 2: Deterministic post-AI orchestration
# ============================================================

SAVE_OK=false

if [ $EXIT_CODE -eq 0 ] && validate_json_array "$SCORED_JSON"; then
    echo "$LOG_PREFIX Artifact valid: $SCORED_JSON"

    # --- Save ---
    if python3 "$HELPER" save < "$SCORED_JSON" 2>&1; then
        SAVE_OK=true
        echo "$LOG_PREFIX Save succeeded"
    else
        echo "$LOG_PREFIX WARNING: save failed (exit $?), skipping commit/report"
    fi
else
    if [ $EXIT_CODE -eq 0 ]; then
        echo "$LOG_PREFIX WARNING: AI exited 0 but no valid scored artifact at $SCORED_JSON"
    fi
fi

# --- Git commit + push (only if save succeeded) ---
if $SAVE_OK; then
    cd "$REPO_DIR"
    git add config/discovered-rss.json
    if git diff --cached --quiet 2>/dev/null; then
        echo "$LOG_PREFIX No changes to commit (candidates already in pool)"
    else
        git commit -m "data(discovery): update RSS candidates $(TZ='Asia/Shanghai' date '+%Y-%m-%d')" 2>&1
        if ! git push 2>&1; then
            echo "$LOG_PREFIX WARNING: git push failed (will retry next run)"
        fi
    fi
fi

# --- Report email (only if save succeeded) ---
if $SAVE_OK; then
    if [ -f "$ENV_FILE" ]; then
        set +u; source "$ENV_FILE"; set -u
        if ! python3 "$HELPER" report 2>&1; then
            echo "$LOG_PREFIX WARNING: report email failed (exit $?)"
        fi
    else
        echo "$LOG_PREFIX WARNING: $ENV_FILE not found, skipping report email"
    fi
fi

# --- Trial manager (unconditional) ---
echo "$LOG_PREFIX Running RSS trial manager..."
python3 "$TRIAL_MANAGER" run 2>&1
TRIAL_EXIT=$?
if [ $TRIAL_EXIT -ne 0 ]; then
    echo "$LOG_PREFIX WARNING: trial manager exited with code $TRIAL_EXIT"
fi

# Commit trial state changes if any
cd "$REPO_DIR"
if ! git diff --quiet config/trial-state.json news-sources-config.json 2>/dev/null; then
    git add config/trial-state.json news-sources-config.json
    git diff --cached --quiet || git commit -m "trial: update trial state $(TZ='Asia/Shanghai' date '+%Y-%m-%d')"
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push (trial) failed"
fi

# Propagate AI exit code for cron-wrapper alerting
if [ $EXIT_CODE -ne 0 ]; then
    exit $EXIT_CODE
fi
```

- [ ] **Step 2: Verify syntax**

Run: `bash -n scripts/rss-source-discovery.sh`
Expected: no output

- [ ] **Step 3: Commit**

```bash
git add scripts/rss-source-discovery.sh
git commit -m "feat(discovery): deterministic post-AI orchestration

Save/commit/push/report now controlled by shell with gates:
- Artifact validation (exists + non-empty + valid JSON array)
- Save success required before commit/report
- Push/report failures are warnings, not errors
- PID-scoped temp files for concurrency safety"
```

---

### Task 5: End-to-end manual verification

- [ ] **Step 1: Full syntax check**

Run: `bash -n scripts/rss-source-discovery.sh`
Expected: no output

- [ ] **Step 2: Dry-run the report email manually to confirm it still works**

```bash
cd ~/global-news && source ~/.stock-monitor.env && python3 rss-source-discovery.py report
```

Expected: `Report email sent to ch_w10@outlook.com`

- [ ] **Step 3: Test artifact validation function in isolation**

```bash
cd ~/global-news
# Valid case
echo '[{"name":"test"}]' > /tmp/rss-test-valid.json
bash -c 'source scripts/rss-source-discovery.sh 2>/dev/null; exit 0' || true
python3 -c "
import json, sys
d = json.load(open('/tmp/rss-test-valid.json'))
assert isinstance(d, list) and len(d) > 0
print('PASS: valid array')
"

# Empty file case
> /tmp/rss-test-empty.json
python3 -c "
import json, sys
try:
    d = json.load(open('/tmp/rss-test-empty.json'))
    assert isinstance(d, list) and len(d) > 0
    print('FAIL: should have rejected')
except:
    print('PASS: empty file rejected')
"

# Not-array case
echo '{"key":"val"}' > /tmp/rss-test-obj.json
python3 -c "
import json, sys
try:
    d = json.load(open('/tmp/rss-test-obj.json'))
    assert isinstance(d, list) and len(d) > 0
    print('FAIL: should have rejected')
except:
    print('PASS: non-array rejected')
"

rm -f /tmp/rss-test-*.json
```

Expected: 3x PASS

- [ ] **Step 4: Review the complete script one final time**

Read the full file and verify:
1. No relative path references remain in the PROMPT
2. All temp files use `$$` suffix
3. `cleanup()` removes all temp files
4. `SAVE_OK` gates both git and report
5. Trial manager section is unchanged in behavior

- [ ] **Step 5: Push**

```bash
cd ~/global-news && git push
```
