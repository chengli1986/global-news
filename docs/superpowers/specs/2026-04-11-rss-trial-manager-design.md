# RSS Trial Manager — Auto-Promote with Trial Tier

**Date**: 2026-04-11
**Status**: Design approved (blocks 1-3), implementation pending
**Next step**: Write implementation plan, then build `rss-trial-manager.py`

## Problem

RSS discovery runs daily, finding high-quality candidates (50 so far, score 0.54-0.95). But promotion is manual (`rss-promote-candidate.py --name "..."`) and nobody does it. Result: good sources pile up unused.

## Solution

An independent `rss-trial-manager.py` script that automatically:
1. Admits high-scoring candidates into production as commodity tier (trial period)
2. After 7 days, evaluates their contribution via quality score comparison
3. Graduates useful sources to standard tier, removes useless ones

## Architecture

```
discovered-rss.json          news-sources-config.json       digest-tuning.json
  (candidates)          <->     (production feeds)            (source_tiers)
        |                            |                              |
        +------ rss-trial-manager.py -+------------------------------+
                      |
              config/trial-state.json
```

Runs as independent daily cron, separate from discovery and autoresearch.

## State Model — `config/trial-state.json`

```json
{
  "trials": [
    {
      "name": "Wired",
      "url": "https://www.wired.com/feed/rss",
      "started": "2026-04-11",
      "expires": "2026-04-18",
      "baseline_quality": 0.8714,
      "status": "active"
    }
  ],
  "graduated": ["France24 English"],
  "removed": ["Yahoo Finance"]
}
```

## Block 1: Entry Logic

**Trigger conditions** (ALL must be met):
- `discovered-rss.json` candidate with `scores.final >= 0.85`
- Not already promoted, rejected, or in trial
- `validation.parse_ok == true` and `validation.newest_age_hours <= 48`
- Active trial count < 3

**Entry actions** (one candidate per run, highest score first):
1. Append to `news-sources-config.json` rss_feeds: `{"name": ..., "url": ..., "keywords": [], "limit": 3}`
2. Append source name to `digest-tuning.json` source_tiers.commodity
3. Mark `"trial": true` in `discovered-rss.json`
4. Record in `trial-state.json` with baseline quality (current evaluate_digest average)
5. One candidate per day max

## Block 2: Graduation/Removal Logic

**Daily check** on all `status: "active"` trials:

**Not expired (< 7 days)**: skip

**Expired (>= 7 days)**:
1. Collect fixture files from trial period (started..expires date range)
2. Run `evaluate_digest.py` with current config → `quality_with`
3. Temporarily remove the trial source from config, re-run → `quality_without`
4. Decision:
   - `quality_with >= quality_without` → **Graduate**: move to standard tier in digest-tuning.json, mark `promoted: true` in discovered-rss.json, move to `graduated` in trial-state
   - `quality_with < quality_without` → **Remove**: delete from news-sources-config.json and digest-tuning.json commodity list, mark `rejected: true` in discovered-rss.json, move to `removed` in trial-state

## Block 3: Safety

- Fixture count < 10 during trial period → extend 3 days (don't judge on insufficient data)
- evaluate_digest errors → skip this run, retry next day
- Max 3 concurrent trials (prevent A/B noise)
- One admission per day (prevent flooding)
- All file writes use atomic temp-file + os.replace pattern (like existing promote tool)

## Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Graduation metric | quality score A/B (with vs without) | User preference — measures actual contribution, not proxy |
| Trial tier | commodity (boost 0.6) | Low priority prevents displacing proven sources |
| Trial duration | 7 days (~21 fixtures) | Enough data for stable comparison |
| Concurrent limit | 3 | Keeps A/B signal clean |
| Implementation | Independent script + cron | Separation of concerns from discovery |
| Admission rate | 1/day | Gradual rollout |

## Cron Schedule (proposed)

```
# RSS trial manager — daily 05:00 BJT (21:00 UTC)
# Runs after discovery (04:15 BJT) so new candidates are available
0 21 * * * ~/cron-wrapper.sh --name rss-trial-manager --timeout 300 --lock -- python3 ~/global-news/rss-trial-manager.py
```

## Files to Create/Modify

**New**:
- `rss-trial-manager.py` — main script
- `config/trial-state.json` — trial lifecycle state
- `tests/test_trial_manager.py` — unit tests

**Modify**:
- `news-sources-config.json` — add/remove trial feeds (automated)
- `digest-tuning.json` — add/remove from commodity/standard tier (automated)
- `discovered-rss.json` — mark trial/promoted/rejected (automated)
- crontab — add trial-manager job

## Qualified Candidates (as of 2026-04-10)

20 candidates score >= 0.85, top 10:

| Score | Name | Category |
|-------|------|----------|
| 0.953 | STAT News | healthcare |
| 0.946 | IEEE Spectrum | tech_ai |
| 0.946 | The Guardian World | europe |
| 0.943 | Foreign Policy | vertical |
| 0.930 | France24 English | europe |
| 0.929 | Endpoints News | healthcare |
| 0.925 | Wired | tech_ai |
| 0.921 | Science News | healthcare |
| 0.910 | RFI English | europe |
| 0.909 | ProPublica | vertical |
