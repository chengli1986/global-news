# Global News Digest

Automated global news digest system that fetches from 58 sources (55 RSS feeds + 2 Sina Finance APIs + 1 HN Firebase API; exact count drifts ±1–2 as trial sources rotate) and delivers HTML email reports three times daily, with LLM-based article classification, periodic health monitoring and automatic failover.

## Architecture

```
Cron (3x daily: 00:00, 08:00, 16:00 BJT)
 └── global-news-cron-wrapper.sh
      └── unified-global-news-sender.py
           ├── news-sources-config.json (58 sources)
           ├── Sina Finance JSON API (2 sources)
           ├── HN Firebase API (1 source, structured data with scores)
           └── RSS/Atom feeds (55 sources, includes active trials)

Cron (every 6h: 02:12, 08:12, 14:12, 20:12 BJT)
 └── rss-health-check.py
      ├── news-sources-config.json (reads + auto-edits on failover)
      └── logs/rss-health.json (consecutive failure state)
```

## Scripts

| Script | Description |
|--------|-------------|
| `unified-global-news-sender.py` | Core engine — parallel fetches from all sources via ThreadPoolExecutor, generates newspaper-style HTML email with per-article timestamps, sends via SMTP. Uses stdlib `html.escape()` with `quote=False` for title/text escaping (escapes `&<>` but leaves quotes and apostrophes as-is for email client compatibility) |
| `global-news-cron-wrapper.sh` | Cron wrapper — manages logging, config validation, environment setup, and error handling |
| `news-sources-config.json` | Central config for all news sources with per-source name, URL, type, keywords, article limit, and max age |
| `rss-health-check.py` | Health monitor — checks RSS sources in parallel, tracks consecutive failures, auto-swaps to fallback URLs after 3 failures, sends email alerts |

## RSS Health Monitor

`rss-health-check.py` runs every 6 hours and performs four checks per source:

1. **HTTP reachability** — GET with 10s timeout
2. **Parse validation** — valid XML (RSS/Atom) or JSON (Sina API)
3. **Article count** — at least 1 article present
4. **Freshness** — newest article within `max_age_hours` threshold

### Automatic failover

When a source fails **3 consecutive checks** and has a known fallback URL, the monitor:
- Edits `news-sources-config.json` directly (text-level URL swap, preserves formatting)
- Records the original URL in `logs/rss-health.json` for potential revert
- Sends an email alert

Sources with fallbacks (RSSHub mirrors):

| Source | Fallback |
|--------|----------|
| 虎嗅 | rsshub.rssforever.com/huxiu/article |
| IT之家 | rsshub.rssforever.com/ithome |
| 36氪 | rsshub.rssforever.com/36kr/news |
| 少数派 | rsshub.rssforever.com/sspai/matrix |
| 钛媒体 | rsshub.rssforever.com/tmtpost/recommend |
| 界面新闻 | rsshub.rssforever.com/jiemian/list/4 |
| Solidot | rsshub.rssforever.com/solidot |
| 南方周末 | rsshub.rssforever.com/infzm/2 |

Sources without fallbacks (BBC, NYT, Economist, Bloomberg, etc.) trigger alert-only — no auto-swap.

### Usage

```bash
# Console report
python3 rss-health-check.py

# Console report + email alert (if issues found)
python3 rss-health-check.py --email
```

## News Sources (58)

> Authoritative list lives in `news-sources-config.json`. Trial-promoted feeds rotate, so this list may drift ±1–2 between README updates. Counts below reflect the snapshot at last edit.

**Chinese (12)**: 中国科技/AI, 中国财经要闻 (Sina Finance APIs), 36氪, IT之家, RTHK中文, Solidot, 南方周末, 少数派, 纽约时报中文, 虎嗅, 钛媒体, BBC中文

**English (31)**:

- **Aggregators**: Hacker News (Firebase API)
- **BBC**: World, Business, Technology
- **NYT**: Business, Technology
- **Bloomberg**: Bloomberg, Econ, Politics
- **Economist**: Leaders, Finance, Business, Science
- **Tech**: TechCrunch, Ars Technica, The Verge, MIT Technology Review
- **Asia**: SCMP, SCMP Hong Kong, CNA, Straits Times, HKFP
- **Other**: CNBC, FT, CBC Business, Globe & Mail, The Guardian World, The New Yorker, STAT News, Politico Europe, El País English

## English Title Translation

English news titles are batch-translated to Chinese using GPT-4.1-mini (approximately 70 titles per send). The translated Chinese title is displayed as the primary headline, with the original English title shown as an italic subtitle below it.

## LLM-Based Article Classification

Articles from mixed-content sources are classified into correct sections using GPT-4.1-mini. The `classify_articles()` method sends all article titles (except locked sources: Canada, Economist) in a single API call, receiving a numbered JSON dict mapping each article to one of five categories: `tech`, `finance`, `politics`, `china`, `asia`. Falls back to keyword-based reclassification if the API call fails.

## LLM Fallback Chain

Both translation and classification use a multi-provider fallback chain to ensure resilience:

```
GPT-4.1-mini → Gemini 2.5 Flash → Gemini 2.5 Flash-Lite → keyword fallback
```

Retry behavior:
- HTTP 429 (rate limit): 1 retry after 2 seconds, then move to next provider.
- HTTP 5xx (transient server error): retried with exponential backoff (5s, 10s) on OpenAI; Gemini calls fast-fail with `max_retries=2` (one retry then move on) since flash 503 is a known regional capacity issue and waiting longer rarely recovers.
- Socket read timeout: retried 3s later (up to `max_retries`); avoids triggering Gemini fallback for a single slow OpenAI request.

The email includes an LLM Status banner when fallback is active (orange for FALLBACK, red for FAILED), hidden when all calls succeed via the primary provider.

## Cross-Send Deduplication

Articles are tracked across the three daily sends via `logs/sent-today-YYYY-MM-DD.json`. Previously sent articles are filtered out to avoid repetition. Premium sources (Economist, FT, Bloomberg, NYT) can resurface after a 4-hour cooldown period.

## Article Timestamps

Each news item displays its publication time and relative age alongside the source:

- **HTML email**: `via BBC World · 03/01 14:30 (2h ago)`
- **Console output**: `via BBC World [03/01 14:30]`

Timestamps are shown in Beijing Time (BJT). Relative age displays as minutes, hours, or days (e.g., `3m ago`, `5h ago`, `2d ago`). Publication dates are parsed from RSS `pubDate`/`published`/`updated` fields and Sina API `ctime` unix timestamps.

## Time Slots

Each delivery is tagged by Beijing time:

| Slot | BJT | Label |
|------|-----|-------|
| Late night | 00:00 | 🌙 深夜档 |
| Morning | 08:00 | 🌅 早间档 |
| Afternoon | 16:00 | 🌆 午后档 |

## Requirements

- Python 3, curl
- No external packages — stdlib only (`urllib`, `xml.etree`, `smtplib`, `concurrent.futures`)
- SMTP credentials in `~/.stock-monitor.env`:
  ```
  SMTP_USER=your@email.com
  SMTP_PASS=your_app_password
  MAIL_TO=recipient@email.com
  NEWS_MAIL_TO=user1@email.com,user2@email.com   # optional, falls back to MAIL_TO
  NEWS_MAIL_BCC=bcc@email.com                    # optional, BCC recipients
  ```

Current recipients (4 TO + 1 BCC):
- `ch_w10@outlook.com`, `sunying1588@163.com`, `liuzhiwen@shenyuanele.com`, `cjl1656@qq.com`
- BCC: `tangwanshan@outlook.com`

## Cron Schedule

```cron
# News digest: 3x daily at 08:00, 16:15, 00:10 BJT (via cron-wrapper)
0 0 * * * ~/cron-wrapper.sh --name global-news-00 --timeout 180 --lock -- ~/.openclaw/workspace/global-news-cron-wrapper.sh email
15 8 * * * ~/cron-wrapper.sh --name global-news-08 --timeout 180 --lock -- ~/.openclaw/workspace/global-news-cron-wrapper.sh email
10 16 * * * ~/cron-wrapper.sh --name global-news-16 --timeout 180 --lock -- ~/.openclaw/workspace/global-news-cron-wrapper.sh email

# RSS health check: every 6h at :20 past 0/6/12/18 UTC
20 0,6,12,18 * * * ~/cron-wrapper.sh --name rss-health --timeout 120 -- python3 ~/.openclaw/workspace/rss-health-check.py
```

## AutoResearch — Digest Quality Pipeline

An automated experimentation system (Phase B) that tunes news digest quality through fixture-based replay and scoring.

### Components

| Script | Description |
|--------|-------------|
| `digest_pipeline.py` | Dedup (Jaccard bigram similarity >0.5), keyword ranking, region-based quotas |
| `evaluate_digest.py` | Replays fixture snapshots, scores on 5 dimensions (coverage, relevance, freshness, diversity, dedup) |
| `digest-tuning.json` | Tuning parameters — weights, thresholds, quota allocations |
| `scripts/wrapper-autoresearch-news.sh` | Cron wrapper for automated experiments (daily 13:00 BJT) |
| `scripts/rss-source-discovery.sh` | Fully automated RSS discovery pipeline — LLM candidate generation, scoring, trial promotion (daily 04:15 BJT, 25min budget) |
| `autoresearch/program.md` | Experiment program and hypothesis tracking |
| `autoresearch/results.tsv` | Experiment results log |

### How it works

1. **Fixture capture**: `tests/YYYY-MM-DD.json` snapshots of raw fetched articles
2. **Pipeline replay**: `digest_pipeline.py` processes fixtures with current tuning params
3. **Quality scoring**: `evaluate_digest.py` measures 5 dimensions, produces composite score
4. **Current score**: 0.8728 (from baseline 0.8207, latest as of 2026-04-12; AR in CONTROLLED PAUSE — auto-skips until 10 fixtures)

## RSS Source Discovery

Daily LLM-driven pipeline that surfaces new high-quality RSS candidates and queues them for trial:

- **Cron**: daily 04:15 BJT via `scripts/rss-source-discovery.sh` (25min budget, lock-protected)
- **Pipeline**: discover (Claude Code search) → dedupe → validate (HTTP/parse/freshness) → score (6 dimensions) → save into registry → trigger trial-manager
- **Categories** (9 total, `config/rss-discovery-categories.json`):
  - `global_finance`, `tech_ai`, `china_depth`, `hk_sea` (incl. Japan/Korea/India queries),
  - `europe`, `north_america`, `healthcare`, `vertical`, `global_south` (Latin America + Africa + Middle East — added Apr 29)
- **Score dimensions**: reliability, freshness, content_quality, content_depth, authority, uniqueness → weighted final
- **Pool cap**: top 70 candidates kept (raised 50→70 on 2026-05-26 — pool was permanently saturated, masking demand for under-represented categories); lowest-scoring auto-rejected as `pool-cap` when exceeded
- **Excellent badge**: score ≥ 0.85 = will auto-promote on next trial-manager run

## RSS Trial Manager

Automated source promotion pipeline that turns high-scoring discovery candidates into active sources, then graduates the ones that prove their value in real digest emails:

- **Auto-promotion**: candidates with score ≥ `PROMOTE_THRESHOLD` (0.85, lowered from 0.90 on Apr 29 — trial system arbitrates edge cases)
- **Concurrency**: up to `MAX_CONCURRENT_TRIALS` = 2 active trials simultaneously (Apr 25 upgrade), max 1 promotion per day, category mutex (no two trials in the same category at once)
- **Trial period**: `TRIAL_DAYS` = 3 days
- **Graduation rule** (Apr 30, stricter): source must pass **both** gates to auto-graduate:
  - **Volume**: `total selected ≥ AUTO_KEEP_MIN_SELECTED` (5, raised from 3)
  - **Distribution**: `days_with_content ≥ MIN_DAYS_WITH_CONTENT` (2) — at least N distinct days must have produced ≥ 1 selected article
  - Either gate failing → auto-removed. Distribution gate prevents promoting bursty sources that pass volume on a single spike day (Politico Europe pattern: 3 selected on day 1, 0 on days 2–3 under old rules)
- **Backfill** (Apr 29): each `cmd_run` re-aggregates `[start_date, today]` from `logs/trial-source-log.jsonl` so any missed day (including the trial-creation day) is reconstructed idempotently
- **Script**: `rss-trial-manager.py` (subcommands: `run` / `status` / `keep [name]` / `remove [name]` / `retry name`)
- **State**: `config/rss-registry.json` (unified — replaced the old `trial-state.json` + `discovered-rss.json`)
- **Integration**: called automatically at the end of `scripts/rss-source-discovery.sh`

## Production Source Fitness (Phase 0 / 0.5)

Per-send telemetry that captures whether each production source is still pulling its weight — the long-term input to a future S&P-500-style rebalancing of the source list:

- **Phase 0** (2026-05-26): every send writes `(ts, source, fetched, selected)` to `logs/production-source-log.jsonl` for every registry production source
- **Phase 0.5** (2026-05-26): RSS sources additionally write 4 per-article quality signals — `avg_title_len`, `avg_desc_len`, `pct_with_desc`, `pct_with_author`
- **Coverage** (2026-05-27): registry production = 51 sources (18 AI-discovered + 33 legacy backfilled). Earlier coverage was 18/52 RSS — Bloomberg / FT / CNBC / BBC / Economist / SCMP and other pre-2026-04-21 sources had no registry entry until `scripts/backfill_legacy_to_registry.py` reconciled the two configs
- **Lifecycle tools**: `rss-promote-candidate.py` (discovered → production), `rss-demote-source.py` (production → rejected, syncs both `news-sources-config.json` and `rss-registry.json` to prevent drift), `scripts/backfill_legacy_to_registry.py` (one-time legacy reconciliation, idempotent)
- **Phase 0 scope**: data collection only; no automated action.
- **Phase 1** (2026-06-13, `rss-production-review.py`): a weekly evaluator now consumes this telemetry — see below. Demote stays human-confirmed.

### Production source review (`rss-production-review.py`)

Weekly in-production quality review (test-period cadence) that reads `logs/production-source-log.jsonl` + the registry and emails a report. It NEVER demotes anything itself.

- **A — zombie sources** (auto-flagged, suggests demote): production sources still publishing (`fetched>0`) but ~never selected (`selected≤1` over a 30-day window), gated by a 30-day on-tenure grace period and an `active_days≥7` sample floor so low-frequency sources aren't misjudged. `fetched==0` (source not publishing) is left to `rss-health-check`. Each candidate carries a ready-to-paste `rss-demote-source.py` command.
- **B — content degradation** (warning only): `pct_with_desc` / `avg_desc_len` / `pct_with_author` drifting down vs the source's OWN baseline (60-day cap, recent-7d vs prior) — never absolute thresholds, so natively-short-summary sources (Foreign Policy etc.) aren't penalised.
- **Action model**: report only — demote is human-confirmed via `rss-demote-source.py`. Test period emails every week (incl. a full-pool contribution snapshot); cadence and thresholds to be tuned after observation.
- **Spec**: `docs/superpowers/specs/2026-06-13-rss-production-quality-review-design.md`

## Scoring v2

Rebalanced weights (Apr 2026): reliability 0.25→0.10, content_quality 0.20→0.25, authority 0.20→0.30. New `content_depth` sub-dimension (avg description length post-HTML-strip) penalizes paywall summaries. Low-frequency correction: sources with ≤10 articles/check use gentle freshness decay (weekly journals not penalized).

### Tests

```bash
python3 -m pytest tests/ -q   # 281 tests (pipeline + trial manager + discovery + sender + rss_registry + demote + backfill + production-review + contract defenses)
./scripts/check-deleted-state-refs.sh            # pre-commit check: no refs to deleted state files
./scripts/check-shell-prompt-assignments.sh      # pre-commit check: multi-line shell VAR="..." must have : "${VAR:?...}" guard
```

## Development

This repo includes a `.claude/CLAUDE.md` with repo-specific context for [Claude Code](https://claude.ai/claude-code) — stdlib-only constraint, config source of truth, RSS failover mechanics, and pubDate parsing quirks. Claude Code agents automatically load this context when working in the repo.

## License

MIT
