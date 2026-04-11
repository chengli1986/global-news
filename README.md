# Global News Digest

Automated global news digest system that fetches from 40 sources (37 RSS feeds + 2 Sina Finance APIs + 1 HN Firebase API) and delivers HTML email reports three times daily, with LLM-based article classification, periodic health monitoring and automatic failover.

## Architecture

```
Cron (3x daily: 00:00, 08:00, 16:00 BJT)
 └── global-news-cron-wrapper.sh
      └── unified-global-news-sender.py
           ├── news-sources-config.json (40 sources)
           ├── Sina Finance JSON API (2 sources)
           ├── HN Firebase API (1 source, structured data with scores)
           └── RSS/Atom feeds (33 sources)

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

## News Sources (40)

**Chinese**: 新浪科技, 新浪财经, 界面新闻, 南方周末, 虎嗅, IT之家, 少数派, Solidot, 钛媒体, 36氪, 纽约时报中文, BBC中文, 日经中文, RTHK中文

**English**: BBC (World, Business, Technology), TechCrunch, CNBC, Bloomberg (Economics, Businessweek, Politics), SCMP, SCMP Hong Kong, CNA, FT, Hacker News (Firebase API), Ars Technica, The Verge, NYT (Business, Technology), Economist (Leaders, Finance, Business, Science), CBC Business, Globe & Mail, HKFP, Straits Times

## English Title Translation

English news titles are batch-translated to Chinese using GPT-4.1-mini (approximately 70 titles per send). The translated Chinese title is displayed as the primary headline, with the original English title shown as an italic subtitle below it.

## LLM-Based Article Classification

Articles from mixed-content sources are classified into correct sections using GPT-4.1-mini. The `classify_articles()` method sends all article titles (except locked sources: Canada, Economist) in a single API call, receiving a numbered JSON dict mapping each article to one of five categories: `tech`, `finance`, `politics`, `china`, `asia`. Falls back to keyword-based reclassification if the API call fails.

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
| `scripts/wrapper-autoresearch-news.sh` | Cron wrapper for automated experiments (daily 21:00 BJT) |
| `autoresearch/program.md` | Experiment program and hypothesis tracking |
| `autoresearch/results.tsv` | Experiment results log |

### How it works

1. **Fixture capture**: `tests/YYYY-MM-DD.json` snapshots of raw fetched articles
2. **Pipeline replay**: `digest_pipeline.py` processes fixtures with current tuning params
3. **Quality scoring**: `evaluate_digest.py` measures 5 dimensions, produces composite score
4. **Current score**: 0.8407+ (from baseline 0.8207, measured 2026-03-28)

## RSS Trial Manager

Automated source promotion pipeline that turns high-scoring discovery candidates into active sources:

- **Auto-promotion**: candidates with score ≥ 0.85 added to commodity tier automatically (1/day, max 3 active trials)
- **Graduation evaluation**: after 7 days, runs quality A/B test — no quality drop → promoted to standard; drop → removed
- **Script**: `rss-trial-manager.py` (state machine: `run` / `status` / `keep` / `remove`)
- **State file**: `config/trial-state.json`
- **Integration**: called daily by `scripts/rss-source-discovery.sh` at 04:15 BJT

## Scoring v2

Rebalanced weights (Apr 2026): reliability 0.25→0.10, content_quality 0.20→0.25, authority 0.20→0.30. New `content_depth` sub-dimension (avg description length post-HTML-strip) penalizes paywall summaries. Low-frequency correction: sources with ≤10 articles/check use gentle freshness decay (weekly journals not penalized).

### Tests

```bash
python3 -m pytest tests/ -q   # 36 tests (9 pipeline + 11 trial manager + 16 discovery)
```

## Development

This repo includes a `.claude/CLAUDE.md` with repo-specific context for [Claude Code](https://claude.ai/claude-code) — stdlib-only constraint, config source of truth, RSS failover mechanics, and pubDate parsing quirks. Claude Code agents automatically load this context when working in the repo.

## License

MIT
