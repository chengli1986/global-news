# Global News Digest

Automated global news digest system that fetches from 33+ sources (RSS feeds + Sina Finance APIs) and delivers HTML email reports three times daily.

## Architecture

```
Cron (3x daily: 00:00, 08:00, 16:00 BJT)
 └── global-news-cron-wrapper.sh
      └── unified-global-news-sender.py
           ├── news-sources-config.json (33 sources)
           ├── Sina Finance JSON API (Chinese tech/finance)
           └── RSS feeds (31 sources)
```

## Scripts

| Script | Description |
|--------|-------------|
| `unified-global-news-sender.py` | Core engine — fetches from all sources, deduplicates, generates HTML email, sends via SMTP. Handles RSS and JSON APIs with flexible date parsing. |
| `global-news-cron-wrapper.sh` | Cron wrapper — manages logging, config validation, environment setup, and error handling. |
| `news-sources-config.json` | Central config for all news sources with per-source name, URL, type, keywords, article limit, and max age. |
| `send-global-news.sh` | Legacy standalone version (self-contained bash + inline Python). Kept for reference. |
| `integrated-news-fetcher.py` | Diagnostic tool — tests all configured sources for reachability and reports status. |

## News Sources (33+)

**Chinese**: 新浪科技, 新浪财经, 南方周末, 虎嗅, IT之家, 少数派, 钛媒体, 36氪, and more

**English**: BBC, TechCrunch, Bloomberg, The Verge, CNBC, Financial Times, Hacker News, The Economist, and more

## Time Slots

Each delivery is tagged by Beijing time:

| Slot | BJT | Label |
|------|-----|-------|
| Late night | 00:00 | 🌙 深夜档 |
| Morning | 08:00 | 🌅 早间档 |
| Afternoon | 16:00 | 🌆 午后档 |

## Requirements

- python3, curl
- Python packages: `requests`, `feedparser`
- SMTP credentials in `~/.stock-monitor.env`:
  ```
  SMTP_USER=your@email.com
  SMTP_PASS=your_app_password
  MAIL_TO=recipient@email.com
  ```

## Cron Schedule

```cron
# 3x daily at 00:00, 08:00, 16:00 BJT (16:00, 00:00, 08:00 UTC)
0 0,8,16 * * * /path/to/global-news-cron-wrapper.sh >> ~/logs/global-news.log 2>&1
```
