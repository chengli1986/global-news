# Global News Digest

Automated global news digest system that fetches from 33 sources (RSS feeds + Sina Finance APIs) and delivers HTML email reports three times daily, with periodic health monitoring and automatic failover.

## Architecture

```
Cron (3x daily: 00:00, 08:00, 16:00 BJT)
 └── global-news-cron-wrapper.sh
      └── unified-global-news-sender.py
           ├── news-sources-config.json (33 sources)
           ├── Sina Finance JSON API (2 sources)
           └── RSS/Atom feeds (31 sources)

Cron (every 6h: 02:12, 08:12, 14:12, 20:12 BJT)
 └── rss-health-check.py
      ├── news-sources-config.json (reads + auto-edits on failover)
      └── logs/rss-health.json (consecutive failure state)
```

## Scripts

| Script | Description |
|--------|-------------|
| `unified-global-news-sender.py` | Core engine — parallel fetches from all sources via ThreadPoolExecutor, generates newspaper-style HTML email with per-article timestamps, sends via SMTP |
| `global-news-cron-wrapper.sh` | Cron wrapper — manages logging, config validation, environment setup, and error handling |
| `news-sources-config.json` | Central config for all news sources with per-source name, URL, type, keywords, article limit, and max age |
| `rss-health-check.py` | Health monitor — checks all 33 sources in parallel, tracks consecutive failures, auto-swaps to fallback URLs after 3 failures, sends email alerts |

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

## News Sources (33)

**Chinese**: 新浪科技, 新浪财经, 界面新闻, 南方周末, 虎嗅, IT之家, 少数派, Solidot, 钛媒体, 36氪, 纽约时报中文, BBC中文, 日经中文

**English**: BBC (World, Business, Technology), TechCrunch, CNBC, Bloomberg, SCMP, CNA, FT, Hacker News, Ars Technica, The Verge, NYT (Business, Technology), Economist (Leaders, Finance, Business, Science), CBC Business, Globe & Mail

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
  ```

## Cron Schedule

```cron
# News digest: 3x daily at 00:04, 08:04, 16:04 BJT
4 0,8,16 * * * ~/.openclaw/workspace/global-news-cron-wrapper.sh email >> ~/logs/news-cron.log 2>&1

# RSS health check: every 6h at :12, offset from other monitors
12 0,6,12,18 * * * cd ~/.openclaw/workspace && python3 rss-health-check.py --email >> ~/logs/rss-health-cron.log 2>&1
```

## Development

This repo includes a `.claude/CLAUDE.md` with repo-specific context for [Claude Code](https://claude.ai/claude-code) — stdlib-only constraint, config source of truth, RSS failover mechanics, and pubDate parsing quirks. Claude Code agents automatically load this context when working in the repo.

## License

MIT
