# Global News

33-source RSS news digest system: fetches feeds in parallel, generates categorized HTML email, sends 3x daily (BJT 08/16/00).

## Architecture
- `unified-global-news-sender.py`: main script — stdlib only (no pip deps), uses `urllib.request`, `xml.etree.ElementTree`, `concurrent.futures`
- `rss-health-check.py`: monitors all 33 sources every 6h, auto-swaps failed feeds after 3 consecutive failures
- `news-sources-config.json`: single source of truth for all feed URLs and backup URLs
- Cron wrapper: `global-news-cron-wrapper.sh` with `SCRIPT_DIR` via `BASH_SOURCE[0]`

## Key Facts
- NO pip dependencies — runs with system Python 3 only
- `news-sources-config.json` is the authoritative feed config — edit here, not in Python
- Auto-swap state: `logs/rss-health.json` tracks failure counts — do NOT delete
- rsshub.app returns 403 from this EC2 — all rsshub feeds use rsshub.rssforever.com
- pubDate parsing: dual-format fallback — `parsedate_to_datetime` then `fromisoformat` with `Z→+00:00`
- Some feeds embed HTML in `<title>` — strip with `re.sub(r'<[^>]+>', '', text)`
- Weekend staleness expected: CBC/Globe (72-96h gap), weekly publishers (168h)

## Testing
- `python3 -m py_compile unified-global-news-sender.py`
- `python3 -m py_compile rss-health-check.py`
- `python3 -c "import json; json.load(open('news-sources-config.json'))"` — validate config
