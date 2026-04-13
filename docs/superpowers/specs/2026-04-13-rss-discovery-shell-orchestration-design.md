# RSS Discovery Shell Orchestration — Design Spec

**Date:** 2026-04-13
**Scope:** `scripts/rss-source-discovery.sh` (single file)
**No changes to:** `rss-source-discovery.py`, `rss-trial-manager.py`, crontab

## Problem

Claude Code Sonnet session skips `python3 $HELPER report` (Step 6 in the prompt), so the
discovery report email is never sent. The root cause is a design flaw: deterministic side
effects (save, commit, push, email) are delegated to an AI session that may not execute them.

Secondary issue: the prompt uses relative paths (`config/news-sources-config.json`) causing
`No such file or directory` on line 152 when the AI session runs bash commands.

## Design Principle

AI is responsible only for discovery, validation, and scoring — producing a scored JSON file.
All deterministic side effects (save, git, email) are controlled by the shell script directly.

## Control Flow

```
1. Define absolute path variables (including PID-scoped temp file)
2. Run Claude Code session
   - Prompt covers Steps 1-5 only (read state → discover → dedup → validate → score)
   - All paths in prompt are absolute variables expanded by the shell
   - Final output: write scored JSON to $SCORED_JSON
3. Validate artifact
   - $SCORED_JSON exists, non-empty, valid JSON array
   - If invalid: warn, skip save/commit/report, proceed to trial manager
4. Save
   - python3 $HELPER save < "$SCORED_JSON"
   - Track exit code; if save fails: warn, skip commit/report
5. Git commit + push (only if save succeeded)
   - git add config/discovered-rss.json
   - If no diff: log "no changes", continue (not an error)
   - If diff: commit + push
   - Push failure: warn only, does not block report
6. Report email (only if save succeeded)
   - Source env file (check exists first; if missing: warn and skip report)
   - python3 $HELPER report
   - Report failure: warn only, exit 0
7. Trial manager (runs unconditionally, same as today)
8. Cleanup: remove $SCORED_JSON on EXIT trap
```

## Specific Changes

### 1. Temp file scoping

Replace fixed `/tmp/rss-scored.json` with PID-scoped:
```bash
SCORED_JSON="/tmp/rss-scored.$$.json"
```
Add to the existing `cleanup()` trap:
```bash
rm -f "$SCORED_JSON" "/tmp/rss-raw-candidates.$$.json" "/tmp/rss-deduped.$$.json" "/tmp/rss-validated.$$.json"
```

### 2. Prompt reduction

Remove Steps 6 (Save + Report) and 7 (Commit + Push) from the `PROMPT` variable.

Change Step 5 output path from `/tmp/rss-scored.json` to `$SCORED_JSON` (shell-expanded).

Replace all relative path references in the prompt with shell variables:
- `$SOURCES_FILE` (already absolute)
- `$CATEGORIES_FILE` (already absolute)
- `$CANDIDATES_FILE` for `config/discovered-rss.json`
- `$SCORED_JSON`, `$RAW_JSON`, `$DEDUPED_JSON`, `$VALIDATED_JSON` for all temp files

### 3. Artifact validation (new, after Claude Code exits)

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

### 4. Post-AI orchestration (new section, replaces current push logic)

```
if claude_exit == 0 AND validate_json_array($SCORED_JSON):
    save_exit = python3 $HELPER save < $SCORED_JSON
    if save_exit == 0:
        git add + commit (allow nothing-to-commit)
        git push (warn on failure)
        source env (check exists) → python3 $HELPER report (warn on failure)
    else:
        warn "save failed"
else:
    warn "no valid scored artifact"

# unconditional
python3 $TRIAL_MANAGER run
```

### 5. Env loading for report

```bash
if [ -f "$HOME/.stock-monitor.env" ]; then
    set +u; source "$HOME/.stock-monitor.env"; set -u
else
    echo "$LOG_PREFIX WARNING: ~/.stock-monitor.env not found, skipping report"
fi
```

`set +u` / `set -u` bracket because env files may reference undefined variables.

## Exit Code Semantics

| Scenario | Exit code |
|----------|-----------|
| Claude Code success + save + report all OK | 0 |
| Claude Code success + save OK + report fails | 0 (warn logged) |
| Claude Code success + save OK + push fails | 0 (warn logged) |
| Claude Code success + artifact invalid | 0 (warn logged, skip save/report) |
| Claude Code timeout (124) | 124 (propagated, cron-wrapper alerts) |
| Claude Code failure (non-zero, non-124) | propagated |
| Trial manager failure | warn only, does not change main exit |

Rationale: report and push are operational niceties, not correctness requirements.
The cron-wrapper should only alert on actual discovery failures.

## What This Does NOT Change

- `rss-source-discovery.py` — all subcommands (validate, dedup, save, report) stay as-is
- `rss-trial-manager.py` — still runs unconditionally at the end
- Cron schedule — same `20:15 UTC` entry
- `cmd_report()` logic — still reads from `discovered-rss.json`, generates HTML, sends via SMTP
